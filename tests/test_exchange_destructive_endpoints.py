"""
Tests for W2b: destructive mail operations + audit log + new endpoints.

Covers:
  * exchange_storage.log_audit / list_audit — round-trip and hash contract
    (raw item_id MUST NOT leak into the audit row).
  * exchange_mail_service.send_message — success + error paths with a fake
    Message factory; never touches the real Exchange.
  * exchange_mail_service.reply_message — message_ref + legacy raw item_id
    paths, target-not-found path.
  * /send_message and /reply_message HTTP endpoints — 200 success shape,
    400 missing identifier, 503 when VPN/account unavailable, 404 target
    not found. VPN-down must NOT instantiate a Message or call .send().
  * /send_work_email and /reply_work_email — legacy endpoints still
    registered and still return their old success shape.
  * OpenAPI: both new endpoints listed.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path + env bootstrap — must run BEFORE importing main / storage.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("EXCHANGE_USER", r"DOMAIN\testuser")
os.environ.setdefault("EXCHANGE_PASS", "x" * 8)
os.environ.setdefault("EXCHANGE_EMAIL", "test@example.com")
os.environ.setdefault("EXCHANGE_SERVER", "owa.example.com")


# =====================================================================
# exchange_storage.audit tests — pure stdlib, fast.
# =====================================================================
import exchange_storage as storage_mod  # noqa: E402
from exchange_storage import (  # noqa: E402
    ExchangeStorage,
    hash_item_id,
    open_storage,
    reset_storage_for_tests,
)


@pytest.fixture
def tmp_storage(tmp_path):
    """Fresh ExchangeStorage on tmp DB; reset singleton after."""
    reset_storage_for_tests()
    s = ExchangeStorage(str(tmp_path / "w2b_audit.db"))
    yield s
    reset_storage_for_tests()


def test_log_audit_basic_roundtrip(tmp_storage):
    """log_audit writes a row with hashed item_id and decoded JSON lists."""
    rid = tmp_storage.log_audit(
        operation="send",
        status="success",
        item_id="RAW-LEAK-123",
        to_recipients=["a@example.com", "b@example.com"],
        cc_recipients=[],
        bcc_recipients=[],
        subject="hello",
    )
    assert isinstance(rid, int) and rid > 0
    rows = tmp_storage.list_audit()
    assert len(rows) == 1
    row = rows[0]
    assert row["operation"] == "send"
    assert row["status"] == "success"
    assert row["subject"] == "hello"
    assert row["to_recipients"] == ["a@example.com", "b@example.com"]
    assert row["cc_recipients"] == []
    assert row["bcc_recipients"] == []
    assert row["item_id_hash"] == hash_item_id("RAW-LEAK-123")
    # RAW item_id MUST NOT appear anywhere in the row.
    assert "RAW-LEAK-123" not in str(row)


def test_log_audit_accepts_single_string_recipient(tmp_storage):
    """to/cc/bcc accept either a list OR a single string."""
    rid = tmp_storage.log_audit(
        operation="send",
        status="success",
        item_id="x",
        to_recipients="solo@example.com",
    )
    rows = tmp_storage.list_audit()
    assert rows[0]["to_recipients"] == ["solo@example.com"]
    assert rid > 0


def test_log_audit_hash_matches_hash_item_id(tmp_storage):
    """item_id_hash column must equal hash_item_id for the same input."""
    h = hash_item_id("AAMkADI==")
    tmp_storage.log_audit(
        operation="reply",
        status="success",
        item_id="AAMkADI==",
        subject="RE: hi",
    )
    row = tmp_storage.list_audit()[0]
    assert row["item_id_hash"] == h
    assert row["item_id_hash"] != "AAMkADI=="  # never the raw value


def test_log_audit_writes_error_status(tmp_storage):
    rid = tmp_storage.log_audit(
        operation="send_error",
        status="error",
        item_id="LEAKY",
        to_recipients=["x@y"],
        error="boom",
    )
    rows = tmp_storage.list_audit(operation="send_error")
    assert rows[0]["status"] == "error"
    assert rows[0]["error"] == "boom"
    assert "LEAKY" not in str(rows[0])


def test_list_audit_filter_by_operation(tmp_storage):
    tmp_storage.log_audit(operation="send", status="success", item_id="a")
    tmp_storage.log_audit(operation="send_error", status="error", item_id="b", error="x")
    tmp_storage.log_audit(operation="reply", status="success", item_id="c")
    assert len(tmp_storage.list_audit(operation="send")) == 1
    assert len(tmp_storage.list_audit(operation="reply")) == 1
    assert len(tmp_storage.list_audit(operation="send_error")) == 1
    assert len(tmp_storage.list_audit()) == 3


def test_log_audit_rejects_bad_status(tmp_storage):
    """log_audit is strict: status must be success/error."""
    with pytest.raises(ValueError):
        tmp_storage.log_audit(operation="send", status="WAT")


def test_log_audit_rejects_missing_operation(tmp_storage):
    with pytest.raises(ValueError):
        tmp_storage.log_audit(operation="", status="success")


def test_log_audit_id_optional(tmp_storage):
    """When no item_id is provided (e.g. pre-validation failure), audit row
    must still be writable with item_id_hash=NULL."""
    rid = tmp_storage.log_audit(
        operation="send_error",
        status="error",
        to_recipients=["x"],
        error="missing_recipient",
    )
    row = tmp_storage.list_audit()[0]
    assert row["item_id_hash"] is None
    assert rid > 0


# =====================================================================
# exchange_mail_service.send_message — pure unit tests with fakes.
# =====================================================================
import exchange_mail_service as mail_svc  # noqa: E402


class _FakeSentMessage:
    """Fake Message produced by the message_factory passed to send_message."""

    instances: list = []  # class-level capture for assertions

    def __init__(self, account, *, subject, body, to_recipients,
                 cc_recipients=None, bcc_recipients=None, **kwargs):
        self.account = account
        self.subject = subject
        self.body = body
        self.to_recipients = list(to_recipients or [])
        self.cc_recipients = list(cc_recipients or [])
        self.bcc_recipients = list(bcc_recipients or [])
        self.sent = False
        type(self).instances.append(self)

    def send(self):
        self.sent = True


class _FakeAccount:
    def __init__(self):
        self.inbox = types.SimpleNamespace()


@pytest.fixture
def fake_account():
    return _FakeAccount()


@pytest.fixture
def clear_fake_msg():
    _FakeSentMessage.instances = []
    yield
    _FakeSentMessage.instances = []


def test_send_message_success_writes_audit(tmp_storage, fake_account, clear_fake_msg):
    result = mail_svc.send_message(
        fake_account,
        tmp_storage,
        to=["a@example.com", "b@example.com"],
        subject="hi",
        body="hello",
        message_factory=_FakeSentMessage,
    )
    assert result["status"] == "success"
    assert result["operation"] == "send"
    assert result["subject"] == "hi"
    assert result["to_recipients"] == ["a@example.com", "b@example.com"]
    # exactly one Message built and sent
    assert len(_FakeSentMessage.instances) == 1
    assert _FakeSentMessage.instances[0].sent is True
    assert _FakeSentMessage.instances[0].subject == "hi"
    # audit row written
    rows = tmp_storage.list_audit(operation="send")
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["subject"] == "hi"
    assert rows[0]["to_recipients"] == ["a@example.com", "b@example.com"]


def test_send_message_supports_cc_and_bcc(tmp_storage, fake_account, clear_fake_msg):
    result = mail_svc.send_message(
        fake_account,
        tmp_storage,
        to=["to@x"],
        cc=["cc@x"],
        bcc=["bcc1@x", "bcc2@x"],
        subject="s",
        body="b",
        message_factory=_FakeSentMessage,
    )
    assert result["status"] == "success"
    assert _FakeSentMessage.instances[0].cc_recipients == ["cc@x"]
    assert _FakeSentMessage.instances[0].bcc_recipients == ["bcc1@x", "bcc2@x"]
    row = tmp_storage.list_audit(operation="send")[0]
    assert row["bcc_recipients"] == ["bcc1@x", "bcc2@x"]


def test_send_message_accepts_single_string_recipient(
    tmp_storage, fake_account, clear_fake_msg
):
    result = mail_svc.send_message(
        fake_account, tmp_storage,
        to="solo@x.com", subject="s", body="b",
        message_factory=_FakeSentMessage,
    )
    assert result["status"] == "success"
    assert _FakeSentMessage.instances[0].to_recipients == ["solo@x.com"]


class _BoomMessage(_FakeSentMessage):
    def send(self):
        raise RuntimeError("EWS down")


def test_send_message_error_writes_audit_and_raises(
    tmp_storage, fake_account, clear_fake_msg
):
    """Error path: audit row with status=error and MailSendError raised."""
    _BoomMessage.instances = []
    try:
        with pytest.raises(mail_svc.MailSendError):
            mail_svc.send_message(
                fake_account, tmp_storage,
                to=["a@x"], subject="s", body="b",
                message_factory=_BoomMessage,
            )
        # No success row, exactly one error row.
        assert tmp_storage.list_audit(operation="send") == []
        rows = tmp_storage.list_audit(operation="send_error")
        assert len(rows) == 1
        assert rows[0]["status"] == "error"
        assert "EWS down" in rows[0]["error"]
        # No Message was successfully "sent".
        assert all(m.sent is False for m in _BoomMessage.instances)
    finally:
        _FakeSentMessage.instances = []


def test_send_message_no_recipient_writes_audit_and_raises(
    tmp_storage, fake_account, clear_fake_msg
):
    """Empty `to` is a programmer error — audit + MailSendError."""
    _FakeSentMessage.instances = []
    try:
        with pytest.raises(mail_svc.MailSendError):
            mail_svc.send_message(
                fake_account, tmp_storage,
                to=[], subject="s", body="b",
                message_factory=_FakeSentMessage,
            )
        rows = tmp_storage.list_audit(operation="send_error")
        assert len(rows) == 1
        assert rows[0]["error"] == "missing_recipient"
        assert _FakeSentMessage.instances == []
    finally:
        _FakeSentMessage.instances = []


# =====================================================================
# exchange_mail_service.reply_message — fakes for inbox/archive lookups.
# =====================================================================
class _FakeReplyableItem:
    def __init__(self, item_id, subject="orig subj"):
        self.id = item_id
        self.subject = subject
        self.replied: list = []  # list of (subject, body) tuples

    def reply(self, subject, body, **kwargs):
        self.replied.append({"subject": subject, "body": body, "kwargs": kwargs})


class _FakeReplyAccount:
    def __init__(self, inbox_items=None, archive_items=None):
        self.inbox_items = {x.id: x for x in (inbox_items or [])}
        self.archive_items = {x.id: x for x in (archive_items or [])}
        # Expose the dict under a real-ish `.inbox.get(id=...)` interface
        # that the default lookup understands.
        self.inbox = types.SimpleNamespace(get=self._inbox_get)
        self.archive_msg_folder_root = types.SimpleNamespace()
        # archive fallback uses _get_archive_inbox which checks this attr
        self.archive_inbox_obj = self

    def _inbox_get(self, id):
        if id in self.inbox_items:
            return self.inbox_items[id]
        # Match exchangelib's "not found" by raising
        raise RuntimeError(f"inbox miss: {id}")


def test_reply_message_via_message_ref(tmp_storage, clear_fake_msg):
    """Resolves raw item_id through storage, calls .reply() on the inbox item."""
    item = _FakeReplyableItem("RAW-INBOX-1", subject="orig")
    account = _FakeReplyAccount(inbox_items=[item])

    ref = tmp_storage.upsert_message({
        "folder": "inbox", "item_id": "RAW-INBOX-1", "subject": "orig",
        "snippet": "x", "has_attachments": False, "synced_at": "2026-01-01T00:00:00+00:00",
    })
    result = mail_svc.reply_message(
        account, tmp_storage,
        message_ref=ref, body="hello",
        inbox_lookup=lambda acc, iid: account.inbox_items.get(iid),
        archive_lookup=lambda acc, iid: None,
    )
    assert result["status"] == "success"
    assert result["operation"] == "reply"
    assert result["message_ref"] == ref
    assert result["subject"].startswith("RE: ")
    # Reply was issued once with correct subject+body.
    assert len(item.replied) == 1
    assert item.replied[0]["subject"] == "RE: orig"
    assert item.replied[0]["body"] == "hello"
    # Audit row written.
    rows = tmp_storage.list_audit(operation="reply")
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["message_ref"] == ref
    assert rows[0]["item_id_hash"] == hash_item_id("RAW-INBOX-1")
    assert "RAW-INBOX-1" not in str(rows[0])


def test_reply_message_via_legacy_raw_item_id(tmp_storage):
    """Legacy fallback: caller passes raw item_id directly."""
    item = _FakeReplyableItem("LEGACY-ID", subject="hi")
    account = _FakeReplyAccount(inbox_items=[item])
    result = mail_svc.reply_message(
        account, tmp_storage,
        message_ref=None, item_id="LEGACY-ID", body="ok",
        inbox_lookup=lambda acc, iid: account.inbox_items.get(iid),
        archive_lookup=lambda acc, iid: None,
    )
    assert result["status"] == "success"
    assert len(item.replied) == 1
    row = tmp_storage.list_audit(operation="reply")[0]
    assert row["item_id_hash"] == hash_item_id("LEGACY-ID")
    assert row["message_ref"] is None


def test_reply_message_falls_back_to_archive(tmp_storage):
    """When inbox lookup returns None, archive lookup is tried."""
    inbox_item = _FakeReplyableItem("not-here")
    archive_item = _FakeReplyableItem("ARCH-ID", subject="archived")
    account = _FakeReplyAccount(archive_items=[archive_item])

    result = mail_svc.reply_message(
        account, tmp_storage,
        item_id="ARCH-ID", body="r",
        inbox_lookup=lambda acc, iid: None,
        archive_lookup=lambda acc, iid: account.archive_items.get(iid),
    )
    assert result["status"] == "success"
    assert archive_item.replied and archive_item.replied[0]["body"] == "r"
    assert inbox_item.replied == []


def test_reply_message_uses_archive_root_fallback(
    tmp_storage,
):
    """Regression: when `archive_msg_folder_root.children` is empty (e.g.
    legacy mailbox without an Inbox child) but `account.archive_root.get(id=...)`
    can still resolve the item, the default archive lookup MUST call
    `archive_root.get(id=item_id)` — not return the folder itself.

    Pre-fix behaviour: lookup returned `account.archive_root` (a folder),
    then reply_message called `.reply()` on the folder and raised
    MailSendError("'ArchiveRoot' object has no attribute 'reply'").
    """
    archive_item = _FakeReplyableItem("ARCH-ID", subject="archived subject")

    class _AlwaysMissInbox:
        def get(self, id):
            raise RuntimeError(f"inbox miss: {id}")

    class _EmptyArchiveChildren:
        children = []

    class _ArchiveRootWithItem:
        def __init__(self, item):
            self._item = item

        def get(self, id):
            if id == self._item.id:
                return self._item
            return None

    class _ArchOnlyAccount:
        def __init__(self):
            self.inbox = _AlwaysMissInbox()
            self.archive_msg_folder_root = _EmptyArchiveChildren()
            self.archive_root = _ArchiveRootWithItem(archive_item)

    account = _ArchOnlyAccount()

    # Do NOT inject archive_lookup — exercise the real default
    # `_default_archive_inbox_lookup` from the service layer.
    result = mail_svc.reply_message(
        account, tmp_storage,
        message_ref=None, item_id="ARCH-ID", body="reply body",
        inbox_lookup=mail_svc._default_inbox_lookup,
        archive_lookup=mail_svc._default_archive_inbox_lookup,
    )
    assert result["status"] == "success", result
    assert result["operation"] == "reply"
    assert result["subject"] == "RE: archived subject"

    # The item itself was replied to (not a folder/root).
    assert len(archive_item.replied) == 1, (
        "archive_root.get item was not replied — default archive lookup "
        "likely returned the folder instead of resolving the item."
    )
    assert archive_item.replied[0]["subject"] == "RE: archived subject"
    assert archive_item.replied[0]["body"] == "reply body"

    # Audit row was written as a success.
    rows = tmp_storage.list_audit(operation="reply")
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["item_id_hash"] == hash_item_id("ARCH-ID")
    assert rows[0]["message_ref"] is None


def test_reply_message_archive_root_fallback_returns_none_when_missing(
    tmp_storage,
):
    """If neither archive_msg_folder_root.children nor
    archive_root.get(id=item_id) can resolve the item, the lookup returns
    None and reply_message raises MailTargetNotFound (404 in main.py)."""
    class _AlwaysMissInbox:
        def get(self, id):
            raise RuntimeError("miss")

    class _EmptyArchiveChildren:
        children = []

    class _EmptyArchiveRoot:
        def get(self, id):
            return None

    class _NoItemAccount:
        def __init__(self):
            self.inbox = _AlwaysMissInbox()
            self.archive_msg_folder_root = _EmptyArchiveChildren()
            self.archive_root = _EmptyArchiveRoot()

    account = _NoItemAccount()
    with pytest.raises(mail_svc.MailTargetNotFound):
        mail_svc.reply_message(
            account, tmp_storage,
            message_ref=None, item_id="ghost", body="r",
            inbox_lookup=mail_svc._default_inbox_lookup,
            archive_lookup=mail_svc._default_archive_inbox_lookup,
        )
    rows = tmp_storage.list_audit(operation="reply_error")
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert rows[0]["error"] == "target_not_found"


def test_reply_message_target_not_found_writes_audit_error(tmp_storage):
    """Both inbox and archive return None → MailTargetNotFound + audit error."""
    account = _FakeReplyAccount()
    with pytest.raises(mail_svc.MailTargetNotFound):
        mail_svc.reply_message(
            account, tmp_storage,
            item_id="ghost", body="r",
            inbox_lookup=lambda acc, iid: None,
            archive_lookup=lambda acc, iid: None,
        )
    rows = tmp_storage.list_audit(operation="reply_error")
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert rows[0]["error"] == "target_not_found"
    assert rows[0]["item_id_hash"] == hash_item_id("ghost")


def test_reply_message_unknown_message_ref_writes_audit_error(tmp_storage):
    """message_ref not in cache → MailTargetNotFound + audit error."""
    account = _FakeReplyAccount()
    with pytest.raises(mail_svc.MailTargetNotFound):
        mail_svc.reply_message(
            account, tmp_storage,
            message_ref="0" * 40, body="r",
            inbox_lookup=lambda acc, iid: None,
            archive_lookup=lambda acc, iid: None,
        )
    rows = tmp_storage.list_audit(operation="reply_error")
    assert rows[0]["error"] == "message_ref_not_in_cache"


def test_reply_message_respects_explicit_subject(tmp_storage):
    """If subject is passed, it overrides the auto-generated RE: prefix."""
    item = _FakeReplyableItem("X", subject="orig")
    account = _FakeReplyAccount(inbox_items=[item])
    result = mail_svc.reply_message(
        account, tmp_storage,
        item_id="X", body="b", subject="Custom subject",
        inbox_lookup=lambda acc, iid: account.inbox_items.get(iid),
        archive_lookup=lambda acc, iid: None,
    )
    assert result["subject"] == "Custom subject"
    assert item.replied[0]["subject"] == "Custom subject"


def test_reply_message_re_does_not_double_prefix(tmp_storage):
    """Auto-subject must not double-prefix when original already has RE:."""
    item = _FakeReplyableItem("X", subject="RE: already")
    account = _FakeReplyAccount(inbox_items=[item])
    result = mail_svc.reply_message(
        account, tmp_storage,
        item_id="X", body="b",
        inbox_lookup=lambda acc, iid: account.inbox_items.get(iid),
        archive_lookup=lambda acc, iid: None,
    )
    assert result["subject"] == "RE: already"


# =====================================================================
# HTTP layer — FastAPI TestClient with fakes for destructive endpoints.
# =====================================================================
from fastapi.testclient import TestClient  # noqa: E402


class _FakeMessage:
    """Used by message_factory injection in main.py tests via patching."""

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.send_called = False

    def send(self):
        self.send_called = True


class _HttpAccount:
    def __init__(self, inbox_items=None, archive_items=None):
        self.inbox_items = {x.id: x for x in (inbox_items or [])}
        self.archive_items = {x.id: x for x in (archive_items or [])}
        self.inbox = types.SimpleNamespace(get=self._inbox_get)
        self.archive_msg_folder_root = types.SimpleNamespace()
        self.archive_root = types.SimpleNamespace(get=self._archive_get)
        self.archive_inbox_obj = self

    def _inbox_get(self, id):
        if id in self.inbox_items:
            return self.inbox_items[id]
        raise RuntimeError("miss")

    def _archive_get(self, id):
        if id in self.archive_items:
            return self.archive_items[id]
        return None


def _make_inbox_item(item_id, subject="orig"):
    item = _FakeReplyableItem(item_id, subject=subject)
    return item


@pytest.fixture
def fake_message_factory(monkeypatch):
    """Replace main._send_message_service to use our fake Message class."""
    import main

    factory_calls: list = []

    def factory(account, *, subject, body, to_recipients, cc_recipients=None,
                bcc_recipients=None):
        msg = _FakeMessage(
            account=account, subject=subject, body=body,
            to_recipients=to_recipients,
            cc_recipients=cc_recipients, bcc_recipients=bcc_recipients,
        )
        factory_calls.append(msg)
        return msg

    # Patch the bound name inside main.
    monkeypatch.setattr(main, "_send_message_service",
                        lambda account, storage, **kw: mail_svc.send_message(
                            account, storage, message_factory=factory, **kw,
                        ))
    return factory_calls


@pytest.fixture(scope="module")
def app_module():
    import main  # noqa: F401
    reset_storage_for_tests()
    return main


@pytest.fixture
def client(app_module, tmp_path, monkeypatch, fake_message_factory):
    """TestClient with VPN up + fake account + tmp DB."""
    main = app_module
    tmp_db = str(tmp_path / "w2b_http.db")

    monkeypatch.setattr(main, "_open_exchange_storage",
                        lambda: open_storage(tmp_db))
    monkeypatch.setattr(main, "is_vpn_active", lambda: True)

    fake = _HttpAccount(inbox_items=[_make_inbox_item("ARCH-1", subject="orig")])
    monkeypatch.setattr(main, "get_account", lambda: fake)

    with TestClient(main.app) as c:
        yield c


# ----- /send_message --------------------------------------------------
def test_send_message_route_registered(client):
    routes = {r.path for r in client.app.routes}
    assert "/send_message" in routes
    assert "/reply_message" in routes


def test_send_message_openapi(client):
    schema = client.get("/openapi.json").json()
    assert "/send_message" in schema["paths"]
    assert "/reply_message" in schema["paths"]
    assert "post" in schema["paths"]["/send_message"]
    assert "post" in schema["paths"]["/reply_message"]


def test_send_message_success(client, fake_message_factory):
    r = client.post("/send_message", json={
        "to": ["a@example.com"], "cc": ["cc@x"], "bcc": ["bcc@x"],
        "subject": "hi", "body": "hello",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success"
    assert body["operation"] == "send"
    assert body["subject"] == "hi"
    assert body["to_recipients"] == ["a@example.com"]
    # exactly one Message was constructed and sent
    assert len(fake_message_factory) == 1
    assert fake_message_factory[0].send_called is True


def test_send_message_legacy_endpoints_still_present(client):
    """W1/W2a — and the legacy /send_work_email / /reply_work_email —
    must remain registered."""
    routes = {r.path for r in client.app.routes}
    assert "/send_work_email" in routes
    assert "/reply_work_email" in routes


def test_send_work_email_legacy_shape_under_fake(
    client, app_module, monkeypatch, tmp_path
):
    """/send_work_email must keep its OLD success response shape."""
    main = app_module
    # Re-bind a fresh fake with captured Message factory.
    captured: list = []

    def factory(account, *, subject, body, to_recipients, cc_recipients=None,
                bcc_recipients=None):
        msg = _FakeMessage(
            account=account, subject=subject, body=body,
            to_recipients=to_recipients,
        )
        captured.append(msg)
        return msg

    monkeypatch.setattr(main, "_send_message_service",
                        lambda account, storage, **kw: mail_svc.send_message(
                            account, storage, message_factory=factory, **kw,
                        ))
    # tmp DB so audit doesn't pollute.
    tmp_db = str(tmp_path / "legacy_send.db")
    main._open_exchange_storage = lambda: open_storage(tmp_db)

    fake = _HttpAccount()
    monkeypatch.setattr(main, "get_account", lambda: fake)

    with TestClient(main.app) as c:
        r = c.post("/send_work_email", json={
            "to_email": "alice@example.com",
            "subject": "s", "body": "b",
        })
    assert r.status_code == 200
    body = r.json()
    # OLD shape — must remain exactly this.
    assert body == {"status": "success", "message": "Sent"}
    assert captured and captured[0].send_called is True


def test_reply_work_email_legacy_shape_under_fake(
    client, app_module, monkeypatch, tmp_path
):
    """/reply_work_email must keep its OLD success response shape."""
    main = app_module
    tmp_db = str(tmp_path / "legacy_reply.db")
    main._open_exchange_storage = lambda: open_storage(tmp_db)

    item = _make_inbox_item("LEG-1", subject="orig")
    fake = _HttpAccount(inbox_items=[item])
    monkeypatch.setattr(main, "get_account", lambda: fake)

    with TestClient(main.app) as c:
        r = c.post("/reply_work_email", json={
            "message_id": "LEG-1", "body": "r",
        })
    assert r.status_code == 200
    body = r.json()
    # OLD shape — must remain exactly this.
    assert body == {"status": "success", "message": "Replied"}
    assert item.replied and item.replied[0]["body"] == "r"


def test_send_message_vpn_down_503_no_message_built(app_module, monkeypatch):
    """VPN down → 503 with structured reason, NO Message constructed."""
    import main
    main = app_module

    monkeypatch.setattr(main, "is_vpn_active", lambda: False)
    monkeypatch.setattr(main, "get_account", lambda: None)
    tmp_db = str(Path(__file__).parent / "_unused_w2b.db")
    monkeypatch.setattr(main, "_open_exchange_storage",
                        lambda: open_storage(tmp_db))
    reset_storage_for_tests()

    sentinel_constructed: list = []

    class _SentinelMessage:
        def __init__(self, *a, **kw):
            sentinel_constructed.append(self)
        def send(self):
            raise AssertionError("send() MUST NOT be called when VPN is down")

    # Even if the service were called, the sentinel would fail loudly.
    monkeypatch.setattr(main, "_send_message_service",
                        lambda account, storage, **kw: mail_svc.send_message(
                            account, storage, message_factory=_SentinelMessage,
                            **kw,
                        ))

    with TestClient(main.app) as c:
        r = c.post("/send_message", json={
            "to": ["x@y"], "subject": "s", "body": "b",
        })
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["reason"] == "vpn_down"
    assert sentinel_constructed == []


def test_reply_message_vpn_down_503_no_reply_called(app_module, monkeypatch):
    """VPN down → 503 with structured reason, NO inbox.get() attempted."""
    import main
    main = app_module

    monkeypatch.setattr(main, "is_vpn_active", lambda: False)
    monkeypatch.setattr(main, "get_account", lambda: None)
    monkeypatch.setattr(main, "_open_exchange_storage",
                        lambda: open_storage(str(Path(__file__).parent / "_unused_w2b.db")))
    reset_storage_for_tests()

    with TestClient(main.app) as c:
        r = c.post("/reply_message", json={
            "message_ref": "a" * 40, "body": "r",
        })
    assert r.status_code == 503
    assert r.json()["detail"]["reason"] == "vpn_down"


# ----- /reply_message --------------------------------------------------
def test_reply_message_success_via_message_ref(
    client, app_module, tmp_path, monkeypatch
):
    """End-to-end: cache a row, then reply through /reply_message."""
    main = app_module
    tmp_db = str(tmp_path / "reply_e2e.db")
    main._open_exchange_storage = lambda: open_storage(tmp_db)

    item = _make_inbox_item("RID-1", subject="orig")
    fake = _HttpAccount(inbox_items=[item])
    monkeypatch.setattr(main, "get_account", lambda: fake)

    # seed the cache
    storage = open_storage(tmp_db)
    ref = storage.upsert_message({
        "folder": "inbox", "item_id": "RID-1", "subject": "orig",
        "snippet": "x", "has_attachments": False,
        "synced_at": "2026-01-01T00:00:00+00:00",
    })

    with TestClient(main.app) as c:
        r = c.post("/reply_message", json={
            "message_ref": ref, "body": "hello",
        })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success"
    assert body["operation"] == "reply"
    assert body["message_ref"] == ref
    assert body["subject"].startswith("RE: ")
    assert item.replied and item.replied[0]["body"] == "hello"

    # Audit row written, raw id NOT present.
    rows = storage.list_audit(operation="reply")
    assert len(rows) == 1
    assert "RID-1" not in str(rows[0])
    assert rows[0]["item_id_hash"] == hash_item_id("RID-1")


def test_reply_message_success_via_legacy_item_id(
    client, app_module, monkeypatch, tmp_path
):
    main = app_module
    tmp_db = str(tmp_path / "reply_legacy.db")
    main._open_exchange_storage = lambda: open_storage(tmp_db)

    item = _make_inbox_item("LEGACY-1")
    fake = _HttpAccount(inbox_items=[item])
    monkeypatch.setattr(main, "get_account", lambda: fake)

    with TestClient(main.app) as c:
        r = c.post("/reply_message", json={
            "item_id": "LEGACY-1", "body": "x",
        })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success"
    assert body["message_ref"] is None
    assert item.replied


def test_reply_message_target_not_found_returns_404(
    client, app_module, monkeypatch, tmp_path
):
    """Inbox and archive both miss → 404, audit error row written."""
    main = app_module
    tmp_db = str(tmp_path / "reply_miss.db")
    main._open_exchange_storage = lambda: open_storage(tmp_db)

    fake = _HttpAccount()  # empty
    monkeypatch.setattr(main, "get_account", lambda: fake)

    with TestClient(main.app) as c:
        r = c.post("/reply_message", json={
            "item_id": "ghost", "body": "x",
        })
    assert r.status_code == 404
    storage = open_storage(tmp_db)
    err_rows = storage.list_audit(operation="reply_error")
    assert len(err_rows) == 1
    assert err_rows[0]["error"] == "target_not_found"
    assert err_rows[0]["status"] == "error"


def test_reply_message_unknown_ref_returns_404(
    client, app_module, monkeypatch, tmp_path
):
    """message_ref not in cache → 404 (no inbox lookup attempted)."""
    main = app_module
    tmp_db = str(tmp_path / "reply_unknown_ref.db")
    main._open_exchange_storage = lambda: open_storage(tmp_db)
    fake = _HttpAccount()
    monkeypatch.setattr(main, "get_account", lambda: fake)

    with TestClient(main.app) as c:
        r = c.post("/reply_message", json={
            "message_ref": "f" * 40, "body": "x",
        })
    assert r.status_code == 404


def test_reply_message_missing_identifier_400(client):
    """Neither message_ref nor item_id → 400 (no service call)."""
    r = client.post("/reply_message", json={"body": "x"})
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "missing_identifier"


def test_send_message_no_recipient_returns_5xx_and_audit(
    client, app_module, monkeypatch, tmp_path
):
    """Empty to=[] → service raises MailSendError → 500 + audit row."""
    import main
    main = app_module
    tmp_db = str(tmp_path / "send_norec.db")
    main._open_exchange_storage = lambda: open_storage(tmp_db)
    fake = _HttpAccount()
    monkeypatch.setattr(main, "get_account", lambda: fake)
    factory_calls: list = []
    monkeypatch.setattr(main, "_send_message_service",
                        lambda account, storage, **kw: mail_svc.send_message(
                            account, storage,
                            message_factory=lambda *a, **k: factory_calls.append(1) or _FakeMessage(*a, **k),
                            **kw,
                        ))
    with TestClient(main.app) as c:
        r = c.post("/send_message", json={
            "to": [], "subject": "s", "body": "b",
        })
    assert r.status_code == 500
    # factory must NOT have been called (fail-fast before Message build)
    assert factory_calls == []
    storage = open_storage(tmp_db)
    err = storage.list_audit(operation="send_error")
    assert len(err) == 1
    assert err[0]["error"] == "missing_recipient"


# ----- backward-compat check -------------------------------------------
def test_all_old_and_new_endpoints_registered(client):
    routes = {r.path for r in client.app.routes}
    expected = [
        "/health", "/ready",
        "/list_messages", "/search_messages", "/get_message",
        "/send_message", "/reply_message",
        "/check_archive_new", "/reply_work_email", "/send_work_email",
        "/list_folders", "/download_dataset", "/download_status",
        "/add_event", "/delete_event", "/get_calendar",
        "/search_calendar", "/sync_emails",
    ]
    for p in expected:
        assert p in routes, f"missing route: {p}"

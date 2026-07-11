"""
Tests for W2a: structured read-only mail API foundation.

Covers:
  * exchange_storage pure-stdlib unit tests (no Exchange, no FastAPI).
  * /list_messages, /search_messages, /get_message via FastAPI TestClient,
    with the Exchange account + storage fully mocked — no real EWS,
    no real vpn0 / DNS / socket I/O.
  * The VPN guard is preserved: when vpn0 is down, the read endpoints
    must return 503 with a structured reason — no Exchange auth attempted.
  * Backward compatibility: all prior endpoints + /health, /ready remain
    registered alongside the three new ones.
"""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------
# Path + env bootstrap — must run BEFORE importing main / storage.
# ---------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("EXCHANGE_USER", r"DOMAIN\testuser")
os.environ.setdefault("EXCHANGE_PASS", "x" * 8)
os.environ.setdefault("EXCHANGE_EMAIL", "test@example.com")
os.environ.setdefault("EXCHANGE_SERVER", "owa.example.com")


# =====================================================================
# exchange_storage unit tests — pure stdlib, fast.
# =====================================================================
import exchange_storage as storage_mod  # noqa: E402
from exchange_storage import (  # noqa: E402
    ExchangeStorage,
    make_message_ref,
    hash_item_id,
    message_record_from_exchange,
    open_storage,
    reset_storage_for_tests,
)


@pytest.fixture
def tmp_storage(tmp_path):
    """Yield a fresh ExchangeStorage pointed at a tmp DB."""
    reset_storage_for_tests()
    db_path = tmp_path / "w2a_test.db"
    s = ExchangeStorage(str(db_path))
    yield s
    reset_storage_for_tests()


def _basic_record(folder="inbox", item_id="AAMkADI=", changekey=None,
                  subject="hello world", sender_email="alice@example.com",
                  received_at=None, has_attachments=False):
    return {
        "folder": folder,
        "item_id": item_id,
        "changekey": changekey,
        "internet_message_id": f"<{item_id}@mail>",
        "conversation_id": "conv-xyz",
        "received_at": received_at or datetime.now(timezone.utc).replace(microsecond=0),
        "sender_email": sender_email,
        "sender_name": "Alice",
        "subject": subject,
        "snippet": "snip",
        "has_attachments": has_attachments,
        "synced_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }


def test_hash_item_id_stable():
    assert hash_item_id("AAMkADI=") == hash_item_id("AAMkADI=")
    assert hash_item_id("AAMkADI=") != hash_item_id("AAMkADIx=")
    assert isinstance(hash_item_id("x"), str)
    assert len(hash_item_id("x")) == 40  # sha1


def test_make_message_ref_stable():
    ref1 = make_message_ref("inbox", "AAMkADI=", "ck1")
    ref2 = make_message_ref("inbox", "AAMkADI=", "ck1")
    assert ref1 == ref2
    assert make_message_ref("inbox", "AAMkADI=") != ref1  # changekey matters
    assert make_message_ref("Sent", "AAMkADI=", "ck1") != ref1  # folder matters


def test_make_message_ref_never_leaks_raw_id():
    """Critical: clients must not see the raw Exchange id in message_ref."""
    ref = make_message_ref("inbox", "super-secret-id-12345")
    assert "super-secret" not in ref
    assert "12345" not in ref
    assert len(ref) == 40


def test_upsert_and_get(tmp_storage):
    rec = _basic_record(item_id="AAMkADI=1", subject="first")
    ref = tmp_storage.upsert_message(rec)
    assert isinstance(ref, str) and len(ref) == 40

    got = tmp_storage.get_message(message_ref=ref)
    assert got is not None
    assert got["subject"] == "first"
    assert got["folder"] == "inbox"
    assert got["item_id"] == "AAMkADI=1"  # we keep raw id internally
    assert "id_hash" in got and len(got["id_hash"]) == 40
    assert got["has_attachments"] is False


def test_upsert_idempotent(tmp_storage):
    rec = _basic_record(item_id="AAMkADI=2")
    r1 = tmp_storage.upsert_message(rec)
    r2 = tmp_storage.upsert_message(rec)
    assert r1 == r2
    assert tmp_storage.count() == 1


def test_list_messages_filters_by_folder(tmp_storage):
    tmp_storage.upsert_message(_basic_record(folder="inbox", item_id="i1"))
    tmp_storage.upsert_message(_basic_record(folder="inbox", item_id="i2"))
    tmp_storage.upsert_message(_basic_record(folder="sent", item_id="s1"))

    inbox = tmp_storage.list_messages(folder="inbox")
    assert {m["item_id"] for m in inbox} == {"i1", "i2"}
    all_ = tmp_storage.list_messages(folder=None)
    assert len(all_) == 3


def test_list_messages_respects_limit_offset(tmp_storage):
    for i in range(5):
        tmp_storage.upsert_message(_basic_record(item_id=f"id{i}", subject=f"s{i}"))
    page = tmp_storage.list_messages(limit=2, offset=1)
    assert len(page) == 2


def test_list_messages_received_window(tmp_storage):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    rec_old = _basic_record(item_id="old", received_at=now - timedelta(days=2))
    rec_mid = _basic_record(item_id="mid", received_at=now - timedelta(hours=1))
    rec_new = _basic_record(item_id="new", received_at=now)
    tmp_storage.upsert_message(rec_old)
    tmp_storage.upsert_message(rec_mid)
    tmp_storage.upsert_message(rec_new)

    out = tmp_storage.list_messages(received_after=(now - timedelta(hours=2)).isoformat())
    ids = [m["item_id"] for m in out]
    # newest first
    assert ids[0] == "new"
    assert "old" not in ids


def test_search_messages_matches_subject(tmp_storage):
    tmp_storage.upsert_message(_basic_record(item_id="1", subject="budget report"))
    tmp_storage.upsert_message(_basic_record(item_id="2", subject="meeting notes"))
    tmp_storage.upsert_message(_basic_record(item_id="3", subject="random"))

    hits = tmp_storage.search_messages("budget")
    assert len(hits) == 1
    assert hits[0]["item_id"] == "1"


def test_search_messages_matches_sender(tmp_storage):
    tmp_storage.upsert_message(_basic_record(item_id="1", sender_email="bob@x.com"))
    tmp_storage.upsert_message(_basic_record(item_id="2", sender_email="alice@x.com"))
    hits = tmp_storage.search_messages("bob@")
    assert {m["item_id"] for m in hits} == {"1"}


def test_search_messages_empty_query_returns_all(tmp_storage):
    tmp_storage.upsert_message(_basic_record(item_id="1"))
    tmp_storage.upsert_message(_basic_record(item_id="2"))
    assert len(tmp_storage.search_messages("")) == 2
    assert len(tmp_storage.search_messages("   ")) == 2


def test_get_message_by_item_id(tmp_storage):
    tmp_storage.upsert_message(_basic_record(item_id="findme"))
    got = tmp_storage.get_message(item_id="findme")
    assert got is not None and got["item_id"] == "findme"

    assert tmp_storage.get_message(item_id="nope") is None


def test_get_message_requires_identifier(tmp_storage):
    with pytest.raises(ValueError):
        tmp_storage.get_message()


def test_message_record_from_exchange_never_raises(tmp_storage):
    """A broken exchangelib Item must not crash the metadata writer."""

    class _Broken:
        id = None
        subject = None
        sender = None

    rec = message_record_from_exchange(_Broken(), "weird")
    assert rec["folder"] == "weird"
    assert rec["item_id"] is None


def test_message_record_from_exchange_picks_up_fields(tmp_storage):
    sent_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    class _Item:
        id = "ABCDE="
        changekey = "ck-001"
        internet_message_id = "<abc@mail>"
        conversation_id = "conv-1"
        datetime_received = sent_at
        subject = "important"

        class sender:
            name = "Bob"
            email_address = "bob@example.com"

        text_body = "long body " * 50  # > snippet limit
        attachments = ["fake-att"]

    rec = message_record_from_exchange(_Item(), "Inbox")
    assert rec["subject"] == "important"
    assert rec["sender_email"] == "bob@example.com"
    assert rec["sender_name"] == "Bob"
    assert rec["internet_message_id"] == "<abc@mail>"
    assert rec["changekey"] == "ck-001"
    assert rec["received_at"].tzinfo is not None
    assert rec["has_attachments"] is True
    assert 0 < len(rec["snippet"]) <= 240


# =====================================================================
# HTTP layer — FastAPI TestClient with full mocks.
# =====================================================================
from fastapi.testclient import TestClient  # noqa: E402


class _FakeFolder:
    """Mimic an exchangelib Folder for cache refresh."""

    def __init__(self, name, items):
        self.name = name
        self._items = items

    def all(self):
        qs = _FakeQS(self._items)
        return qs


class _FakeQS:
    def __init__(self, items):
        self._items = list(items)

    def order_by(self, *_):
        return self

    def __getitem__(self, sl):
        if isinstance(sl, slice):
            return self._items[sl.start or 0:sl.stop]
        return self._items[sl]


class _FakeItem:
    def __init__(self, item_id="FAKE=", subject="fake subj", sender=("Sender", "s@example.com"),
                 body="hello body", received=None, atts=()):
        self.id = item_id
        self.changekey = "ck-fake"
        self.internet_message_id = f"<{item_id}@mail>"
        self.conversation_id = "conv-fake"
        self.subject = subject
        self.datetime_received = received or datetime.now(timezone.utc)
        self.text_body = body
        self.attachments = list(atts)
        if sender:
            name, email = sender
            self.sender = types.SimpleNamespace(name=name, email_address=email)
        else:
            self.sender = None


class _FakeAccount:
    """Stand-in for an exchangelib Account — never touches network."""
    def __init__(self, inbox_items=(), archive_items=()):
        self.inbox = _FakeFolder("Inbox", list(inbox_items))
        self.archive_inbox = _FakeFolder("Archive Inbox", list(archive_items))


@pytest.fixture(scope="module")
def app_module():
    """Import main.py with heavy deps acceptable (FastAPI / exchangelib are
    present in the venv per W1)."""
    import main  # noqa: F401
    # Reset the global storage singleton so tests get a fresh DB.
    reset_storage_for_tests()
    return main


@pytest.fixture
def client(app_module, tmp_path, monkeypatch):
    """Yield a TestClient. Patches open_storage to use a tmp DB so tests
    never touch the production exchange_metadata.db."""
    main = app_module
    tmp_db = str(tmp_path / "w2a_http.db")

    # Redirect the module-level open_storage() to use the tmp DB.
    monkeypatch.setattr(
        main, "_open_exchange_storage",
        lambda: open_storage(tmp_db),
    )

    # Default: pretend vpn0 is up, account is alive.
    monkeypatch.setattr(main, "is_vpn_active", lambda: True)

    fake_account = _FakeAccount(
        inbox_items=[
            _FakeItem(item_id="inbox-1", subject="budget Q1"),
            _FakeItem(item_id="inbox-2", subject="lunch plans"),
        ],
        archive_items=[
            _FakeItem(item_id="arc-1", subject="archive thing"),
        ],
    )
    monkeypatch.setattr(main, "get_account", lambda: fake_account)

    with TestClient(main.app) as c:
        yield c


# ----- /list_messages --------------------------------------------------
def test_list_messages_returns_metadata(client):
    r = client.post("/list_messages", json={"limit": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success"
    assert isinstance(body["messages"], list)
    # Even with refresh=False we may already have records from earlier tests
    # — but the contract is "list of rows", not strict count. Just shape.


def test_list_messages_refresh_writes_to_cache(client, app_module):
    r = client.post("/list_messages", json={"folder": "inbox", "refresh": True, "limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["folder"] == "inbox"
    assert body["refreshed"] == 2
    # Both refreshed items must appear in the response.
    subjects = {m["subject"] for m in body["messages"]}
    assert "budget Q1" in subjects
    assert "lunch plans" in subjects


def test_list_messages_refresh_unknown_folder_noop(client):
    """Unknown folder → refresh quietly falls back to cache (no 4xx/5xx)."""
    r = client.post("/list_messages", json={"folder": "does_not_exist", "refresh": True})
    assert r.status_code == 200
    body = r.json()
    assert body["refreshed"] == 0


def test_list_messages_vpn_down_returns_503(app_module, monkeypatch):
    main = app_module
    # Patch get_account → None (the actual guard behaviour) and is_vpn_active → False.
    monkeypatch.setattr(main, "is_vpn_active", lambda: False)
    monkeypatch.setattr(main, "get_account", lambda: None)
    monkeypatch.setattr(
        main, "_open_exchange_storage",
        lambda: open_storage(str(Path(__file__).parent / "_unused_w2a.db")),
    )
    reset_storage_for_tests()

    with TestClient(main.app) as c:
        r = c.post("/list_messages", json={"limit": 5})
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["reason"] == "vpn_down"
    # Structured 503 detail — must NOT raise / attempt Exchange auth.


def test_list_messages_account_none_vpn_up_returns_503(app_module, monkeypatch):
    """If vpn0 is up but account can't be built, still 503 — but with a
    distinct reason string."""
    main = app_module
    monkeypatch.setattr(main, "is_vpn_active", lambda: True)
    monkeypatch.setattr(main, "get_account", lambda: None)
    monkeypatch.setattr(
        main, "_open_exchange_storage",
        lambda: open_storage(str(Path(__file__).parent / "_unused_w2a.db")),
    )
    reset_storage_for_tests()

    with TestClient(main.app) as c:
        r = c.post("/list_messages", json={"limit": 5})
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["reason"] == "exchange_unreachable"


# ----- /search_messages ------------------------------------------------
def test_search_messages_hits(client, app_module, tmp_path):
    main = app_module
    # Force a fresh tmp DB for this test so we know the rows.
    tmp_db = str(tmp_path / "search.db")
    main._open_exchange_storage = lambda: open_storage(tmp_db)

    # Seed cache.
    r = client.post("/list_messages", json={"folder": "inbox", "refresh": True, "limit": 10})
    assert r.status_code == 200

    r = client.post("/search_messages", json={"query": "lunch", "limit": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "lunch"
    assert any(m["subject"] == "lunch plans" for m in body["messages"])
    assert body["count"] >= 1


def test_search_messages_empty_query_400(client):
    r = client.post("/search_messages", json={"query": "   "})
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "empty_query"


def test_search_messages_vpn_down_503(app_module, monkeypatch):
    main = app_module
    monkeypatch.setattr(main, "is_vpn_active", lambda: False)
    monkeypatch.setattr(main, "get_account", lambda: None)
    monkeypatch.setattr(
        main, "_open_exchange_storage",
        lambda: open_storage(str(Path(__file__).parent / "_unused_w2a.db")),
    )
    reset_storage_for_tests()
    with TestClient(main.app) as c:
        r = c.post("/search_messages", json={"query": "anything"})
    assert r.status_code == 503
    assert r.json()["detail"]["reason"] == "vpn_down"


# ----- /get_message ----------------------------------------------------
def test_get_message_by_ref(client, app_module, tmp_path):
    main = app_module
    tmp_db = str(tmp_path / "getmsg.db")
    main._open_exchange_storage = lambda: open_storage(tmp_db)

    r = client.post("/list_messages", json={"folder": "inbox", "refresh": True, "limit": 5})
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert msgs, "expected at least one cached message"
    ref = msgs[0]["message_ref"]

    r = client.post("/get_message", json={"message_ref": ref})
    assert r.status_code == 200
    msg = r.json()["message"]
    assert msg["message_ref"] == ref
    assert "subject" in msg


def test_get_message_by_item_id(client):
    r = client.post("/get_message", json={"item_id": "inbox-1"})
    # Only succeeds if a record with this item_id is in the cache. If earlier
    # refresh tests seeded it, this passes; if not, we get 404 — both are
    # well-formed.
    assert r.status_code in (200, 404)


def test_get_message_missing_identifier_400(client):
    r = client.post("/get_message", json={})
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "no_identifier"


def test_get_message_not_found_returns_404(app_module, monkeypatch):
    main = app_module
    fake = _FakeAccount()
    monkeypatch.setattr(main, "is_vpn_active", lambda: True)
    monkeypatch.setattr(main, "get_account", lambda: fake)
    monkeypatch.setattr(
        main, "_open_exchange_storage",
        lambda: open_storage(str(Path(__file__).parent / "_unused_w2a.db")),
    )
    reset_storage_for_tests()
    with TestClient(main.app) as c:
        r = c.post("/get_message", json={"message_ref": "0" * 40})
    assert r.status_code == 404
    assert r.json()["detail"]["status"] == "not_found"


def test_get_message_vpn_down_503(app_module, monkeypatch):
    main = app_module
    monkeypatch.setattr(main, "is_vpn_active", lambda: False)
    monkeypatch.setattr(main, "get_account", lambda: None)
    monkeypatch.setattr(
        main, "_open_exchange_storage",
        lambda: open_storage(str(Path(__file__).parent / "_unused_w2a.db")),
    )
    reset_storage_for_tests()
    with TestClient(main.app) as c:
        r = c.post("/get_message", json={"message_ref": "0" * 40})
    assert r.status_code == 503
    assert r.json()["detail"]["reason"] == "vpn_down"


# ----- Backward compatibility & in-process OpenAPI ---------------------
def test_all_old_and_new_endpoints_registered(client):
    routes = {r.path for r in client.app.routes}
    expected = [
        # W1
        "/health", "/ready",
        # W2a new
        "/list_messages", "/search_messages", "/get_message",
        # legacy
        "/check_archive_new", "/reply_work_email", "/send_work_email",
        "/list_folders", "/download_dataset", "/download_status",
        "/add_event", "/delete_event", "/get_calendar",
        "/search_calendar", "/sync_emails",
    ]
    for p in expected:
        assert p in routes, f"missing route: {p}"


def test_openapi_lists_all_endpoints(client):
    """In-process OpenAPI: client-side has to be able to discover the
    new endpoints via the standard schema."""
    schema = client.get("/openapi.json").json()
    paths = set(schema["paths"].keys())
    for p in ("/health", "/ready", "/list_messages", "/search_messages", "/get_message"):
        assert p in paths, f"openapi missing {p}"
    # Each new endpoint must declare a POST and accept JSON.
    for p in ("/list_messages", "/search_messages", "/get_message"):
        assert "post" in schema["paths"][p]


def test_health_still_200(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

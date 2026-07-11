"""
Tests for W2c: attachment metadata cache + service layer + new REST endpoints.

Covers:
  * v2→v3 schema migration preserves messages + audit_log and adds attachments.
  * exchange_storage.make_attachment_ref is stable and opaque — no plaintext
    raw id / attachment_id / filename leaks into the ref.
  * upsert_attachment + list_attachments + get_attachment round-trip;
    public rows never expose raw item_id / attachment_id.
  * exchange_attachment_service.list_attachments_for_message — works with
    fake account + fake message + fake attachments, writes metadata rows.
  * exchange_attachment_service.download_attachment — saves bytes to a tmp
    target dir, sanitises path-traversal filenames, returns safe local_path.
  * service errors: AttachmentNotFound on missing target / missing selector /
    missing attachment → 404 mapping.
  * FastAPI: /list_attachments and /download_attachment routes are
    registered, listed in OpenAPI, and honour the VPN guard (503 when
    account is None, no folder / attachment is touched).
  * Legacy /download_dataset and /download_status remain registered.
"""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path + env bootstrap — must run BEFORE importing main / storage / service.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("EXCHANGE_USER", r"DOMAIN\testuser")
os.environ.setdefault("EXCHANGE_PASS", "x" * 8)
os.environ.setdefault("EXCHANGE_EMAIL", "test@example.com")
os.environ.setdefault("EXCHANGE_SERVER", "owa.example.com")


# =====================================================================
# exchange_storage: schema migration + attachment helpers (pure stdlib).
# =====================================================================
import exchange_storage as storage_mod  # noqa: E402
from exchange_storage import (  # noqa: E402
    ExchangeStorage,
    hash_item_id,
    make_message_ref,
    make_attachment_ref,
    open_storage,
    reset_storage_for_tests,
)


@pytest.fixture
def tmp_storage(tmp_path):
    """Fresh ExchangeStorage on tmp DB; reset singleton after."""
    reset_storage_for_tests()
    s = ExchangeStorage(str(tmp_path / "w2c_storage.db"))
    yield s
    reset_storage_for_tests()


def test_schema_version_is_4_after_w2d(tmp_storage):
    """After W2d, SCHEMA_VERSION is bumped to 4 and a freshly-opened DB
    reflects that. The test id was previously ``test_schema_version_is_3``
    during the W2c phase; we renamed it to keep the W2c-era history
    documented in the diff."""
    assert storage_mod.SCHEMA_VERSION == 4
    with storage_mod.sqlite3.connect(tmp_storage.db_path) as conn:
        cur = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    assert cur == storage_mod.SCHEMA_VERSION


# Backward-compatible alias: the W2c-era test id stays importable.
def test_schema_version_is_3(tmp_storage):
    """Legacy alias preserved after the W2d version bump — forwards to
    the canonical assertion that SCHEMA_VERSION (now 4) matches the
    PRAGMA stored on disk."""
    assert test_schema_version_is_4_after_w2d(tmp_storage) is None


def test_attachments_table_exists_with_expected_columns(tmp_storage):
    with storage_mod.sqlite3.connect(tmp_storage.db_path) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(attachments)").fetchall()]
    for col in (
        "attachment_ref",
        "message_ref",
        "item_id_hash",
        "attachment_id_hash",
        "filename",
        "content_type",
        "size",
        "local_path",
        "downloaded_at",
        "synced_at",
    ):
        assert col in cols, f"missing column: {col}"


def test_v2_to_v3_migration_preserves_messages_and_audit(tmp_path):
    """Open a v2 DB, then a v3 ExchangeStorage — both old tables survive."""
    import sqlite3
    db_path = tmp_path / "v2_to_v3.db"

    # Build a hand-crafted v2 database (messages + audit_log) — no v3 tables.
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 2")
    conn.execute(
        """
        CREATE TABLE messages (
            message_ref        TEXT PRIMARY KEY,
            folder             TEXT NOT NULL,
            item_id            TEXT NOT NULL,
            exchange_id_hash   TEXT NOT NULL,
            changekey          TEXT,
            internet_message_id TEXT,
            conversation_id    TEXT,
            received_at        TEXT,
            sender_email       TEXT,
            sender_name        TEXT,
            subject            TEXT,
            snippet            TEXT,
            has_attachments    INTEGER NOT NULL DEFAULT 0,
            synced_at          TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE audit_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT NOT NULL,
            operation       TEXT NOT NULL,
            message_ref     TEXT,
            item_id_hash    TEXT,
            to_recipients   TEXT,
            cc_recipients   TEXT,
            bcc_recipients  TEXT,
            subject         TEXT,
            status          TEXT NOT NULL,
            error           TEXT
        )
        """
    )
    # Seed one row each so we can prove they survive the migration.
    seed_ref = "f" * 40
    conn.execute(
        "INSERT INTO messages (message_ref, folder, item_id, exchange_id_hash, "
        "has_attachments, synced_at) VALUES (?, 'inbox', 'OLD-ID', ?, 0, ?)",
        (seed_ref, hash_item_id("OLD-ID"), "2026-01-01T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO audit_log (ts, operation, status, subject) "
        "VALUES (?, 'send', 'success', 'seeded')",
        ("2026-01-01T00:00:00+00:00",),
    )
    conn.commit()
    conn.close()

    # Now open with the v3 ExchangeStorage — must migrate without losing data.
    reset_storage_for_tests()
    storage = ExchangeStorage(str(db_path))

    # Old rows are still there.
    msg = storage.get_message(message_ref=seed_ref)
    assert msg is not None
    assert msg["id_hash"] == hash_item_id("OLD-ID")
    rows = storage.list_audit(operation="send")
    assert len(rows) == 1
    assert rows[0]["subject"] == "seeded"

    # New table exists and works.
    ref = storage.upsert_attachment({
        "item_id": "NEW-ID",
        "attachment_id": "ATT-1",
        "filename": "post-migrate.pdf",
        "content_type": "application/pdf",
        "size": 1024,
        "message_ref": seed_ref,
    })
    assert isinstance(ref, str) and len(ref) == 40
    listed = storage.list_attachments(message_ref=seed_ref)
    assert len(listed) == 1
    assert listed[0]["filename"] == "post-migrate.pdf"

    reset_storage_for_tests()


def test_make_attachment_ref_is_stable_and_opaque(tmp_storage):
    """make_attachment_ref must be stable for identical inputs and opaque."""
    a = make_attachment_ref(
        message_ref="a" * 40, item_id="RAW-ITEM-XYZ", attachment_id="ATT-9",
        filename="report.pdf",
    )
    b = make_attachment_ref(
        message_ref="a" * 40, item_id="RAW-ITEM-XYZ", attachment_id="ATT-9",
        filename="report.pdf",
    )
    assert a == b
    assert len(a) == 40
    assert a.isalnum()

    # None of the secret-ish inputs leak into the output.
    for token in ("RAW-ITEM", "RAW-ITEM-XYZ", "ATT-9", "report.pdf"):
        assert token not in a, f"ref leaks token: {token}"


def test_make_attachment_ref_differs_by_attachment_id():
    """Same item_id but different attachment_id → different ref."""
    r1 = make_attachment_ref(item_id="X", attachment_id="A")
    r2 = make_attachment_ref(item_id="X", attachment_id="B")
    assert r1 != r2


def test_make_attachment_ref_differs_by_item_id():
    """Different item_id → different ref (even with same attachment_id)."""
    r1 = make_attachment_ref(item_id="X", attachment_id="A")
    r2 = make_attachment_ref(item_id="Y", attachment_id="A")
    assert r1 != r2


def test_make_attachment_ref_filename_does_not_change_ref():
    """Filenames are plaintext user data — renaming must NOT change the ref."""
    r1 = make_attachment_ref(item_id="X", attachment_id="A", filename="a.pdf")
    r2 = make_attachment_ref(item_id="X", attachment_id="A", filename="b.pdf")
    assert r1 == r2


def test_make_attachment_ref_handles_missing_attachment_id():
    """When attachment_id is None, filename acts as a stable fallback."""
    r1 = make_attachment_ref(item_id="X", attachment_id=None, filename="doc.txt")
    r2 = make_attachment_ref(item_id="X", attachment_id="", filename="doc.txt")
    r3 = make_attachment_ref(item_id="X", attachment_id=None, filename="other.txt")
    assert r1 == r2
    assert r1 != r3


# =====================================================================
# upsert_attachment / list_attachments / get_attachment round-trip.
# =====================================================================
def test_upsert_attachment_round_trip(tmp_storage):
    ref = tmp_storage.upsert_attachment({
        "item_id": "ITEM-1",
        "attachment_id": "ATT-1",
        "filename": "hello.pdf",
        "content_type": "application/pdf",
        "size": 1234,
    })
    assert isinstance(ref, str) and len(ref) == 40

    row = tmp_storage.get_attachment(ref)
    assert row is not None
    assert row["attachment_ref"] == ref
    assert row["filename"] == "hello.pdf"
    assert row["content_type"] == "application/pdf"
    assert row["size"] == 1234
    # Public row MUST NOT contain the raw id columns — only hashes.
    assert "item_id" not in row
    assert "attachment_id" not in row
    # Hashes (sha1 hex) ARE exposed for symmetry with messages.id_hash.
    assert row["item_id_hash"] == hash_item_id("ITEM-1")
    assert row["attachment_id_hash"] == hash_item_id("ATT-1")


def test_upsert_attachment_requires_item_id(tmp_storage):
    with pytest.raises(ValueError):
        tmp_storage.upsert_attachment({"filename": "x.pdf"})


def test_upsert_attachment_requires_filename(tmp_storage):
    with pytest.raises(ValueError):
        tmp_storage.upsert_attachment({"item_id": "X"})
    with pytest.raises(ValueError):
        tmp_storage.upsert_attachment({"item_id": "X", "filename": ""})


def test_upsert_attachment_is_idempotent(tmp_storage):
    """Re-upsert with same (item, attachment_id, filename) → same ref."""
    r1 = tmp_storage.upsert_attachment(
        {"item_id": "X", "attachment_id": "A", "filename": "f.pdf"}
    )
    r2 = tmp_storage.upsert_attachment(
        {"item_id": "X", "attachment_id": "A", "filename": "f.pdf",
         "content_type": "application/pdf", "size": 42}
    )
    assert r1 == r2
    row = tmp_storage.get_attachment(r1)
    assert row["content_type"] == "application/pdf"
    assert row["size"] == 42


def test_list_attachments_by_message_ref(tmp_storage):
    msg_ref = make_message_ref("inbox", "ITEM-1")
    r1 = tmp_storage.upsert_attachment({
        "message_ref": msg_ref, "item_id": "ITEM-1",
        "attachment_id": "A1", "filename": "a.pdf", "size": 10,
    })
    r2 = tmp_storage.upsert_attachment({
        "message_ref": msg_ref, "item_id": "ITEM-1",
        "attachment_id": "A2", "filename": "b.pdf", "size": 20,
    })
    other = tmp_storage.upsert_attachment({
        "item_id": "ITEM-2", "attachment_id": "Z1", "filename": "z.pdf",
    })

    rows = tmp_storage.list_attachments(message_ref=msg_ref)
    refs = {r["attachment_ref"] for r in rows}
    assert refs == {r1, r2}
    # No raw ids in public rows.
    for row in rows:
        assert "item_id" not in row
        assert "attachment_id" not in row
        assert "item_id_hash" not in row
        assert "attachment_id_hash" not in row


def test_list_attachments_by_item_id(tmp_storage):
    r1 = tmp_storage.upsert_attachment(
        {"item_id": "ITEM-1", "attachment_id": "A1", "filename": "a.pdf"}
    )
    r2 = tmp_storage.upsert_attachment(
        {"item_id": "ITEM-1", "attachment_id": "A2", "filename": "b.pdf"}
    )
    tmp_storage.upsert_attachment(
        {"item_id": "ITEM-2", "attachment_id": "Z1", "filename": "z.pdf"}
    )
    rows = tmp_storage.list_attachments(item_id="ITEM-1")
    refs = {r["attachment_ref"] for r in rows}
    assert refs == {r1, r2}


def test_list_attachments_requires_identifier(tmp_storage):
    with pytest.raises(ValueError):
        tmp_storage.list_attachments()


def test_get_attachment_unknown_returns_none(tmp_storage):
    assert tmp_storage.get_attachment("0" * 40) is None


def test_get_attachment_requires_ref(tmp_storage):
    with pytest.raises(ValueError):
        tmp_storage.get_attachment("")


# =====================================================================
# exchange_attachment_service — fakes for account + attachments.
# =====================================================================
import exchange_attachment_service as att_svc  # noqa: E402


class _FakeAttachment:
    """Duck-typed stand-in for exchangelib.FileAttachment."""

    def __init__(
        self,
        name: str,
        content: bytes,
        size: int | None = None,
        content_type: str | None = None,
        attachment_id: str | None = None,
    ):
        self.name = name
        self.content = content
        self.size = size if size is not None else len(content)
        self.content_type = content_type
        self.attachment_id = attachment_id


class _FakeMessage:
    """Duck-typed stand-in for an exchangelib Message."""

    def __init__(self, item_id: str, attachments: list[_FakeAttachment]):
        self.id = item_id
        self.subject = "fake-msg"
        self.attachments = attachments


class _FakeAccount:
    def __init__(self, inbox_items=None, archive_items=None):
        self._inbox = {x.id: x for x in (inbox_items or [])}
        self._archive = {x.id: x for x in (archive_items or [])}
        self.inbox = types.SimpleNamespace(get=self._inbox_get)
        self.archive_msg_folder_root = types.SimpleNamespace(children=[])
        self.archive_root = types.SimpleNamespace(get=self._archive_get)

    def _inbox_get(self, id):
        if id in self._inbox:
            return self._inbox[id]
        raise RuntimeError(f"inbox miss: {id}")

    def _archive_get(self, id):
        return self._archive.get(id)


def test_safe_filename_strips_path_traversal():
    """The single most important safety property — traversal must die."""
    assert att_svc.safe_filename("..\\..\\secret.txt") == "secret.txt"
    assert att_svc.safe_filename("../../../etc/passwd") == "passwd"
    assert att_svc.safe_filename("/etc/shadow") == "shadow"
    assert att_svc.safe_filename("C:\\Windows\\System32\\evil.exe") == "evil.exe"
    assert att_svc.safe_filename("subdir/foo.pdf") == "foo.pdf"
    assert att_svc.safe_filename("..") == att_svc.safe_filename("..")
    assert att_svc.safe_filename("") == "attachment"
    assert att_svc.safe_filename(None) == "attachment"
    assert att_svc.safe_filename("normal.pdf") == "normal.pdf"
    # NUL bytes are nuked.
    assert "\x00" not in att_svc.safe_filename("evil\x00.txt")
    # Reserved characters become underscore.
    out = att_svc.safe_filename('a<b>c:d"e/f\\g|h?i*.txt')
    for ch in '<>:"/\\|?*':
        assert ch not in out


def test_safe_filename_truncates_long_names():
    long = "a" * 500 + ".pdf"
    out = att_svc.safe_filename(long, max_len=120)
    assert len(out) <= 120


def test_resolve_target_path_stays_inside_target(tmp_path):
    abs_dir, full = att_svc._resolve_target_path(str(tmp_path), "report.pdf")
    assert abs_dir == str(tmp_path.resolve())
    assert full.startswith(abs_dir + os.sep)


def test_resolve_target_path_clamps_traversal(tmp_path):
    abs_dir, full = att_svc._resolve_target_path(
        str(tmp_path), "../../../etc/passwd"
    )
    assert abs_dir == str(tmp_path.resolve())
    assert full.startswith(abs_dir + os.sep)


def test_list_attachments_for_message_writes_rows(tmp_storage):
    msg = _FakeMessage("RAW-INBOX-1", [
        _FakeAttachment("a.pdf", b"AAA", content_type="application/pdf",
                        attachment_id="att-A"),
        _FakeAttachment("b.txt", b"BBBBB", content_type="text/plain",
                        attachment_id="att-B"),
        # nameless — should be skipped
        _FakeAttachment("", b"x"),
    ])
    account = _FakeAccount(inbox_items=[msg])

    result = att_svc.list_attachments_for_message(
        account, tmp_storage, item_id="RAW-INBOX-1",
    )
    assert result["status"] == "success"
    assert result["operation"] == "list_attachments"
    assert result["count"] == 2  # nameless skipped
    assert result["item_id_hash"] == hash_item_id("RAW-INBOX-1")
    assert result["message_ref"] is None

    filenames = [a["filename"] for a in result["attachments"]]
    assert "a.pdf" in filenames
    assert "b.txt" in filenames

    # Rows persisted in SQLite.
    rows = tmp_storage.list_attachments(item_id="RAW-INBOX-1")
    assert len(rows) == 2
    for row in rows:
        assert "item_id" not in row
        assert "attachment_id" not in row


def test_list_attachments_for_message_sanitizes_public_and_stored_filename(
    tmp_storage,
):
    """Traversal names are matched internally but never exposed or stored raw."""
    msg = _FakeMessage("RAW-TRAVERSAL-LIST", [
        _FakeAttachment("../../secret.txt", b"SECRET", content_type="text/plain",
                        attachment_id="att-secret"),
    ])
    account = _FakeAccount(inbox_items=[msg])

    result = att_svc.list_attachments_for_message(
        account, tmp_storage, item_id="RAW-TRAVERSAL-LIST",
    )

    assert result["count"] == 1
    public_name = result["attachments"][0]["filename"]
    assert public_name == "secret.txt"
    assert ".." not in public_name
    assert "/" not in public_name

    rows = tmp_storage.list_attachments(item_id="RAW-TRAVERSAL-LIST")
    assert len(rows) == 1
    assert rows[0]["filename"] == "secret.txt"


def test_list_attachments_updates_existing_raw_cached_ref_to_sanitized_filename(
    tmp_storage,
):
    """Existing raw traversal cache rows are updated, not duplicated."""
    msg = _FakeMessage("RAW-OLD-LIST", [
        _FakeAttachment("../../secret.txt", b"SECRET", attachment_id=None),
    ])
    account = _FakeAccount(inbox_items=[msg])
    old_ref = tmp_storage.upsert_attachment({
        "item_id": "RAW-OLD-LIST",
        "attachment_id": None,
        "filename": "../../secret.txt",
    })

    result = att_svc.list_attachments_for_message(
        account, tmp_storage, item_id="RAW-OLD-LIST",
    )

    assert result["count"] == 1
    assert result["attachments"][0]["attachment_ref"] == old_ref
    assert result["attachments"][0]["filename"] == "secret.txt"
    rows = tmp_storage.list_attachments(item_id="RAW-OLD-LIST")
    assert len(rows) == 1
    assert rows[0]["attachment_ref"] == old_ref
    assert rows[0]["filename"] == "secret.txt"


def test_list_attachments_for_message_resolves_via_message_ref(tmp_storage):
    msg = _FakeMessage("RAW-2", [
        _FakeAttachment("only.pdf", b"X", attachment_id="att-X"),
    ])
    account = _FakeAccount(inbox_items=[msg])
    msg_ref = tmp_storage.upsert_message({
        "folder": "inbox", "item_id": "RAW-2", "subject": "s",
        "snippet": "", "has_attachments": True,
        "synced_at": "2026-01-01T00:00:00+00:00",
    })
    result = att_svc.list_attachments_for_message(
        account, tmp_storage, message_ref=msg_ref,
    )
    assert result["status"] == "success"
    assert result["message_ref"] == msg_ref
    assert result["count"] == 1
    rows = tmp_storage.list_attachments(message_ref=msg_ref)
    assert len(rows) == 1


def test_list_attachments_for_message_falls_back_to_archive(tmp_storage):
    inbox_msg = _FakeMessage("not-here", [])
    archive_msg = _FakeMessage("ARCH-1", [
        _FakeAttachment("z.pdf", b"Z", attachment_id="att-Z"),
    ])
    account = _FakeAccount(
        inbox_items=[inbox_msg], archive_items=[archive_msg],
    )
    result = att_svc.list_attachments_for_message(
        account, tmp_storage, item_id="ARCH-1",
        inbox_lookup=lambda acc, iid: None,
        archive_lookup=lambda acc, iid: account._archive.get(iid),
    )
    assert result["status"] == "success"
    assert result["count"] == 1


def test_list_attachments_target_not_found_raises(tmp_storage):
    """Empty account → AttachmentNotFound("target_not_found")."""
    account = _FakeAccount()
    with pytest.raises(att_svc.AttachmentNotFound) as ei:
        att_svc.list_attachments_for_message(
            account, tmp_storage, item_id="ghost",
            inbox_lookup=lambda a, i: None,
            archive_lookup=lambda a, i: None,
        )
    assert ei.value.reason == "target_not_found"


def test_list_attachments_missing_identifier_raises(tmp_storage):
    account = _FakeAccount()
    with pytest.raises(att_svc.AttachmentNotFound):
        att_svc.list_attachments_for_message(
            account, tmp_storage,
            inbox_lookup=lambda a, i: None,
            archive_lookup=lambda a, i: None,
        )


def test_list_attachments_unknown_message_ref_raises(tmp_storage):
    """message_ref that does not exist in cache → typed error."""
    account = _FakeAccount()
    with pytest.raises(att_svc.AttachmentNotFound) as ei:
        att_svc.list_attachments_for_message(
            account, tmp_storage,
            message_ref="0" * 40,
            inbox_lookup=lambda a, i: None,
            archive_lookup=lambda a, i: None,
        )
    assert ei.value.reason == "message_ref_not_in_cache"


# ----- download_attachment ----------------------------------------------
def test_download_attachment_by_filename_saves_to_target(
    tmp_storage, tmp_path
):
    msg = _FakeMessage("DL-1", [
        _FakeAttachment("hello.pdf", b"PDF-BYTES",
                        content_type="application/pdf", attachment_id="att-1"),
    ])
    account = _FakeAccount(inbox_items=[msg])
    target = tmp_path / "attachments"

    result = att_svc.download_attachment(
        account, tmp_storage,
        item_id="DL-1", filename="hello.pdf",
        target_dir=str(target),
    )
    assert result["status"] == "success"
    assert result["operation"] == "download_attachment"
    assert result["filename"] == "hello.pdf"
    assert result["size"] == len(b"PDF-BYTES")
    assert result["content_type"] == "application/pdf"
    assert result["local_path"].startswith(str(target.resolve()))
    # File actually exists with the right bytes.
    on_disk = Path(result["local_path"])
    assert on_disk.is_file()
    assert on_disk.read_bytes() == b"PDF-BYTES"
    # Row persisted.
    row = tmp_storage.get_attachment(result["attachment_ref"])
    assert row is not None
    assert row["local_path"] == result["local_path"]
    assert row["size"] == len(b"PDF-BYTES")


def test_download_attachment_sanitises_path_traversal(
    tmp_storage, tmp_path
):
    """A malicious filename like ../../secret.txt MUST stay inside target_dir."""
    msg = _FakeMessage("DL-2", [
        _FakeAttachment("../../etc/passwd", b"X", attachment_id="att-x"),
    ])
    account = _FakeAccount(inbox_items=[msg])
    target = tmp_path / "safe_target"

    result = att_svc.download_attachment(
        account, tmp_storage,
        item_id="DL-2", filename="../../etc/passwd",
        target_dir=str(target),
    )
    assert result["status"] == "success"
    # Path is resolved to be inside target_dir.
    resolved = Path(result["local_path"]).resolve()
    assert str(resolved).startswith(str(target.resolve()) + os.sep) or \
           str(resolved) == str(target.resolve())
    # The original traversal tokens are NOT in the saved filename.
    assert ".." not in Path(result["local_path"]).name
    assert "/etc/passwd" not in result["local_path"]
    # The file exists and holds the bytes.
    assert resolved.is_file()
    assert resolved.read_bytes() == b"X"


def test_download_attachment_sanitizes_public_and_stored_filename(
    tmp_storage, tmp_path
):
    """download_attachment must not return/store traversal filename metadata."""
    msg = _FakeMessage("DL-TRAVERSAL", [
        _FakeAttachment("../evil.txt", b"E", content_type="text/plain",
                        attachment_id="att-evil"),
    ])
    account = _FakeAccount(inbox_items=[msg])
    target = tmp_path / "target"

    result = att_svc.download_attachment(
        account, tmp_storage,
        item_id="DL-TRAVERSAL", filename="../evil.txt",
        target_dir=str(target),
    )

    assert result["filename"] == "evil.txt"
    assert ".." not in result["filename"]
    assert "/" not in result["filename"]

    local_path = Path(result["local_path"]).resolve()
    assert str(local_path).startswith(str(target.resolve()) + os.sep)
    assert local_path.name == "evil.txt"
    assert local_path.is_file()
    assert local_path.read_bytes() == b"E"

    row = tmp_storage.get_attachment(result["attachment_ref"])
    assert row is not None
    assert row["filename"] == "evil.txt"


def test_download_attachment_by_attachment_id(tmp_storage, tmp_path):
    msg = _FakeMessage("DL-3", [
        _FakeAttachment("a.pdf", b"A", attachment_id="A"),
        _FakeAttachment("b.pdf", b"BB", attachment_id="B"),
    ])
    account = _FakeAccount(inbox_items=[msg])
    target = tmp_path / "t3"
    result = att_svc.download_attachment(
        account, tmp_storage,
        item_id="DL-3", attachment_id="B",
        target_dir=str(target),
    )
    assert result["filename"] == "b.pdf"
    assert result["size"] == 2


def test_download_attachment_by_attachment_ref(tmp_storage, tmp_path):
    msg = _FakeMessage("DL-4", [
        _FakeAttachment("report.pdf", b"BYTES",
                        attachment_id="att-report"),
    ])
    account = _FakeAccount(inbox_items=[msg])
    # Pre-seed the metadata row so we can pass attachment_ref.
    ref = tmp_storage.upsert_attachment({
        "item_id": "DL-4", "attachment_id": "att-report",
        "filename": "report.pdf",
    })
    target = tmp_path / "t4"
    result = att_svc.download_attachment(
        account, tmp_storage,
        item_id="DL-4", attachment_ref=ref,
        target_dir=str(target),
    )
    assert result["filename"] == "report.pdf"
    assert result["attachment_ref"] == ref
    assert Path(result["local_path"]).read_bytes() == b"BYTES"


def test_download_attachment_by_ref_after_sanitized_list_without_attachment_id(
    tmp_storage, tmp_path
):
    """Sanitizing filename metadata must not break cached-ref selection."""
    msg = _FakeMessage("DL-REF-NO-ID", [
        _FakeAttachment("../evil.txt", b"E", attachment_id=None),
    ])
    account = _FakeAccount(inbox_items=[msg])

    listed = att_svc.list_attachments_for_message(
        account, tmp_storage, item_id="DL-REF-NO-ID",
    )
    ref = listed["attachments"][0]["attachment_ref"]
    assert listed["attachments"][0]["filename"] == "evil.txt"

    result = att_svc.download_attachment(
        account, tmp_storage,
        item_id="DL-REF-NO-ID", attachment_ref=ref,
        target_dir=str(tmp_path / "t-ref-no-id"),
    )

    assert result["attachment_ref"] == ref
    assert result["filename"] == "evil.txt"
    assert Path(result["local_path"]).name == "evil.txt"
    assert Path(result["local_path"]).read_bytes() == b"E"


def test_download_attachment_by_existing_raw_cached_ref_sanitizes_without_changing_ref(
    tmp_storage, tmp_path
):
    """Old cached refs with raw traversal names must still select and update."""
    msg = _FakeMessage("DL-OLD-RAW-REF", [
        _FakeAttachment("../evil.txt", b"E", attachment_id=None),
    ])
    account = _FakeAccount(inbox_items=[msg])
    old_ref = tmp_storage.upsert_attachment({
        "item_id": "DL-OLD-RAW-REF",
        "attachment_id": None,
        "filename": "../evil.txt",  # simulates a pre-repair cached row
    })
    assert tmp_storage.get_attachment(old_ref)["filename"] == "../evil.txt"

    result = att_svc.download_attachment(
        account, tmp_storage,
        item_id="DL-OLD-RAW-REF", attachment_ref=old_ref,
        target_dir=str(tmp_path / "t-old-raw-ref"),
    )

    assert result["attachment_ref"] == old_ref
    assert result["filename"] == "evil.txt"
    assert Path(result["local_path"]).name == "evil.txt"
    row = tmp_storage.get_attachment(old_ref)
    assert row is not None
    assert row["filename"] == "evil.txt"


def test_download_attachment_unknown_ref_returns_not_found(tmp_storage, tmp_path):
    """attachment_ref unknown to the cache → typed AttachmentNotFound."""
    account = _FakeAccount()
    target = tmp_path / "t5"
    with pytest.raises(att_svc.AttachmentNotFound) as ei:
        att_svc.download_attachment(
            account, tmp_storage,
            item_id="anything", attachment_ref="0" * 40,
            target_dir=str(target),
            inbox_lookup=lambda a, i: None,
            archive_lookup=lambda a, i: None,
        )
    assert ei.value.reason == "attachment_ref_unknown"


def test_download_attachment_no_selector_raises(tmp_storage, tmp_path):
    """No attachment_ref / attachment_id / filename → 400 in main, raise here."""
    account = _FakeAccount()
    with pytest.raises(att_svc.AttachmentNotFound) as ei:
        att_svc.download_attachment(
            account, tmp_storage,
            item_id="X", target_dir=str(tmp_path),
            inbox_lookup=lambda a, i: None,
            archive_lookup=lambda a, i: None,
        )
    assert ei.value.reason == "no_selector"


def test_download_attachment_target_not_found_raises(tmp_storage, tmp_path):
    account = _FakeAccount()
    with pytest.raises(att_svc.AttachmentNotFound) as ei:
        att_svc.download_attachment(
            account, tmp_storage,
            item_id="ghost", filename="a.pdf",
            target_dir=str(tmp_path),
            inbox_lookup=lambda a, i: None,
            archive_lookup=lambda a, i: None,
        )
    assert ei.value.reason == "target_not_found"


def test_download_attachment_attachment_mismatch_raises(tmp_storage, tmp_path):
    msg = _FakeMessage("DL-X", [
        _FakeAttachment("a.pdf", b"X", attachment_id="A"),
    ])
    account = _FakeAccount(inbox_items=[msg])
    with pytest.raises(att_svc.AttachmentNotFound):
        att_svc.download_attachment(
            account, tmp_storage,
            item_id="DL-X", filename="nope.pdf",
            target_dir=str(tmp_path),
        )


def test_download_attachment_missing_message_identifier_raises(
    tmp_storage, tmp_path
):
    account = _FakeAccount()
    with pytest.raises(att_svc.AttachmentNotFound) as ei:
        att_svc.download_attachment(
            account, tmp_storage,
            filename="x.pdf", target_dir=str(tmp_path),
            inbox_lookup=lambda a, i: None,
            archive_lookup=lambda a, i: None,
        )
    assert ei.value.reason == "missing_identifier"


def test_download_attachment_unknown_message_ref_raises(tmp_storage, tmp_path):
    account = _FakeAccount()
    with pytest.raises(att_svc.AttachmentNotFound) as ei:
        att_svc.download_attachment(
            account, tmp_storage,
            message_ref="0" * 40, filename="x.pdf",
            target_dir=str(tmp_path),
            inbox_lookup=lambda a, i: None,
            archive_lookup=lambda a, i: None,
        )
    assert ei.value.reason == "message_ref_not_in_cache"


# =====================================================================
# FastAPI TestClient layer — new endpoints + VPN guard.
# =====================================================================
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def app_module():
    import main  # noqa: F401
    reset_storage_for_tests()
    return main


@pytest.fixture
def client(app_module, tmp_path, monkeypatch):
    """TestClient with VPN up + fake account + tmp DB."""
    main = app_module
    tmp_db = str(tmp_path / "w2c_http.db")

    monkeypatch.setattr(main, "_open_exchange_storage",
                        lambda: open_storage(tmp_db))
    monkeypatch.setattr(main, "is_vpn_active", lambda: True)

    fake = _FakeAccount(inbox_items=[
        _FakeMessage("HTTP-1", [
            _FakeAttachment("doc.pdf", b"PDF", content_type="application/pdf",
                            attachment_id="att-http-1"),
            _FakeAttachment("notes.txt", b"NOTES", content_type="text/plain",
                            attachment_id="att-http-2"),
        ])
    ])
    monkeypatch.setattr(main, "get_account", lambda: fake)
    # Patch the service-layer functions so we exercise main.py route plumbing
    # but stay in pure-stdlib land.
    import exchange_attachment_service as _att
    monkeypatch.setattr(main, "_list_attachments_service",
                        _att.list_attachments_for_message)
    monkeypatch.setattr(main, "_download_attachment_service",
                        _att.download_attachment)
    with TestClient(main.app) as c:
        yield c, tmp_db


def test_routes_are_registered(app_module):
    """Both new endpoints and all legacy endpoints must be present."""
    import main
    routes = {r.path for r in main.app.routes}
    for required in (
        "/health", "/ready",
        "/list_messages", "/search_messages", "/get_message",
        "/send_message", "/reply_message",
        "/list_attachments", "/download_attachment",
        "/download_dataset", "/download_status",
    ):
        assert required in routes, f"missing route: {required}"


def test_openapi_lists_new_endpoints(client):
    c, _ = client
    schema = c.get("/openapi.json").json()
    for path in ("/list_attachments", "/download_attachment"):
        assert path in schema["paths"], f"OpenAPI missing {path}"
        assert "post" in schema["paths"][path], f"OpenAPI missing POST {path}"


def test_list_attachments_success(client, app_module, tmp_path, monkeypatch):
    c, tmp_db = client
    main = app_module
    # Seed cache so we can pass message_ref instead of raw item_id.
    msg_ref = make_message_ref("inbox", "HTTP-1")
    storage = open_storage(tmp_db)
    storage.upsert_message({
        "folder": "inbox", "item_id": "HTTP-1", "subject": "s",
        "snippet": "", "has_attachments": True,
        "synced_at": "2026-01-01T00:00:00+00:00",
    })

    r = c.post("/list_attachments", json={"message_ref": msg_ref})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "success"
    assert body["operation"] == "list_attachments"
    assert body["count"] == 2
    filenames = {a["filename"] for a in body["attachments"]}
    assert filenames == {"doc.pdf", "notes.txt"}


def test_list_attachments_via_legacy_item_id(client, app_module, tmp_path, monkeypatch):
    c, tmp_db = client
    r = c.post("/list_attachments", json={"item_id": "HTTP-1"})
    assert r.status_code == 200
    assert r.json()["count"] == 2


def test_list_attachments_missing_identifier_400(client):
    c, _ = client
    r = c.post("/list_attachments", json={})
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "missing_identifier"


def test_list_attachments_target_not_found_404(client):
    c, _ = client
    r = c.post("/list_attachments", json={"message_ref": "0" * 40})
    assert r.status_code == 404


def test_download_attachment_success(client, app_module, tmp_path, monkeypatch):
    c, tmp_db = client
    target = tmp_path / "downloads"
    r = c.post("/download_attachment", json={
        "item_id": "HTTP-1",
        "filename": "doc.pdf",
        "target_dir": str(target),
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "success"
    assert body["operation"] == "download_attachment"
    assert body["filename"] == "doc.pdf"
    assert body["size"] == 3  # len(b"PDF")
    # Path is inside target_dir.
    assert Path(body["local_path"]).resolve().parent == target.resolve()
    # File actually written.
    assert Path(body["local_path"]).read_bytes() == b"PDF"


def test_download_attachment_sanitises_traversal_in_route(
    app_module, tmp_path, monkeypatch
):
    """The HTTP route must also sanitise traversal filenames end-to-end."""
    import main
    main = app_module

    # Re-bind a fake account whose ONLY attachment has a traversal filename.
    traversal_msg = _FakeMessage("TRAV-1", [
        _FakeAttachment("../../../etc/passwd", b"X",
                        content_type="text/plain", attachment_id="att-trav"),
    ])
    traversal_account = _FakeAccount(inbox_items=[traversal_msg])

    target = tmp_path / "downloads_safe"
    monkeypatch.setattr(main, "is_vpn_active", lambda: True)
    monkeypatch.setattr(main, "get_account", lambda: traversal_account)
    monkeypatch.setattr(main, "_open_exchange_storage",
                        lambda: open_storage(str(tmp_path / "trav.db")))
    reset_storage_for_tests()

    import exchange_attachment_service as _att
    monkeypatch.setattr(main, "_download_attachment_service", _att.download_attachment)

    with TestClient(main.app) as c:
        r = c.post("/download_attachment", json={
            "item_id": "TRAV-1",
            "filename": "../../../etc/passwd",
            "target_dir": str(target),
        })
    assert r.status_code == 200, r.text
    body = r.json()
    # No traversal in the saved filename.
    assert ".." not in Path(body["local_path"]).name
    assert "/etc/passwd" not in body["local_path"]
    # Inside target_dir.
    assert str(Path(body["local_path"]).resolve()).startswith(str(target.resolve()))


def test_download_attachment_missing_message_identifier_400(client):
    c, _ = client
    r = c.post("/download_attachment", json={"filename": "x.pdf"})
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "missing_identifier"


def test_download_attachment_missing_selector_400(client):
    c, _ = client
    r = c.post("/download_attachment", json={"item_id": "HTTP-1"})
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "no_attachment_selector"


def test_download_attachment_target_not_found_404(client):
    c, _ = client
    r = c.post("/download_attachment", json={
        "item_id": "ghost", "filename": "a.pdf",
    })
    assert r.status_code == 404


def test_download_attachment_unknown_ref_404(client):
    c, _ = client
    r = c.post("/download_attachment", json={
        "item_id": "HTTP-1", "attachment_ref": "0" * 40,
    })
    assert r.status_code == 404


# ----- VPN guard ---------------------------------------------------------
def test_list_attachments_vpn_down_503_no_folder_touched(
    app_module, tmp_path, monkeypatch
):
    """VPN down → 503 with structured reason, NO fake lookup is called."""
    import main
    main = app_module
    monkeypatch.setattr(main, "is_vpn_active", lambda: False)
    monkeypatch.setattr(main, "get_account", lambda: None)
    tmp_db = str(tmp_path / "vpn_down_list.db")
    monkeypatch.setattr(main, "_open_exchange_storage",
                        lambda: open_storage(tmp_db))
    reset_storage_for_tests()

    inbox_called: list = []
    archive_called: list = []

    def fake_list(account, storage, **_kw):
        # Should never be reached when VPN is down.
        inbox_called.append(account)
        return {"status": "success", "operation": "list_attachments",
                "count": 0, "attachments": []}

    monkeypatch.setattr(main, "_list_attachments_service", fake_list)

    with TestClient(main.app) as c:
        r = c.post("/list_attachments", json={"item_id": "HTTP-1"})
    assert r.status_code == 503
    assert r.json()["detail"]["reason"] == "vpn_down"
    assert inbox_called == []  # service NEVER reached
    assert archive_called == []


def test_download_attachment_vpn_down_503_no_folder_touched(
    app_module, tmp_path, monkeypatch
):
    """VPN down → 503, the fake service is never called, no file is written."""
    import main
    main = app_module
    target = tmp_path / "vpn_down_downloads"

    monkeypatch.setattr(main, "is_vpn_active", lambda: False)
    monkeypatch.setattr(main, "get_account", lambda: None)
    tmp_db = str(tmp_path / "vpn_down_dl.db")
    monkeypatch.setattr(main, "_open_exchange_storage",
                        lambda: open_storage(tmp_db))
    reset_storage_for_tests()

    service_called: list = []

    def fake_dl(*_a, **_kw):
        service_called.append(True)
        return {"status": "success", "operation": "download_attachment"}

    monkeypatch.setattr(main, "_download_attachment_service", fake_dl)

    with TestClient(main.app) as c:
        r = c.post("/download_attachment", json={
            "item_id": "HTTP-1", "filename": "doc.pdf",
            "target_dir": str(target),
        })
    assert r.status_code == 503
    assert r.json()["detail"]["reason"] == "vpn_down"
    assert service_called == []
    # No file written.
    assert not target.exists() or not any(target.iterdir())


# ----- legacy endpoints still present ------------------------------------
def test_legacy_dataset_routes_still_registered(app_module):
    import main
    routes = {r.path for r in main.app.routes}
    assert "/download_dataset" in routes
    assert "/download_status" in routes


def test_download_status_returns_legacy_shape(app_module):
    """Legacy /download_status keeps its existing shape."""
    import main
    with TestClient(main.app) as c:
        r = c.get("/download_status")
    assert r.status_code == 200
    body = r.json()
    assert "running" in body
    assert "folder" in body
    assert "progress" in body

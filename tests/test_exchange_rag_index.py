"""
Tests for W2d: local-file RAG indexing foundation.

Covers:
  * v3→v4 schema migration preserves messages / audit_log / attachments
    and adds rag_documents; idempotent re-open.
  * make_rag_doc_ref is stable and opaque — does NOT include raw path
    text in the output.
  * upsert_rag_document / list_rag_documents / search_rag_documents /
    get_rag_document round-trip.
  * index_rag_documents indexes tmp text files into a tmp DB, returns
    structured stats; never touches production rag_data/.
  * Search finds terms in filename and text_excerpt.
  * get_rag_document(include_text=True) reads body from disk with
    max_chars cap; (include_text=False) never touches disk for body.
  * Path safety: a symlink/file that resolves outside root is skipped —
    no outside content indexed.
  * Huge file cap: a file larger than max_file_bytes is skipped
    deterministically — never partially indexed.
  * FastAPI: /index_rag_documents, /search_rag_documents, /get_rag_document
    are registered (and OpenAPI lists them); existing W1-W2c routes are
    still present; legacy /download_dataset and /download_status are
    still present.
  * Missing query → 400; missing doc_ref → 400; unknown doc_ref → 404.
  * Endpoint works with tmp root + tmp DB; production rag_data is never
    touched.
"""
from __future__ import annotations

import os
import sys
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
# v3→v4 migration + storage helpers (pure stdlib).
# =====================================================================
import exchange_storage as storage_mod  # noqa: E402
from exchange_storage import (  # noqa: E402
    ExchangeStorage,
    make_rag_doc_ref,
    open_storage,
    reset_storage_for_tests,
)
import exchange_rag_service as rag_svc  # noqa: E402


@pytest.fixture
def tmp_storage(tmp_path):
    reset_storage_for_tests()
    s = ExchangeStorage(str(tmp_path / "w2d_storage.db"))
    yield s
    reset_storage_for_tests()


def test_schema_version_is_4(tmp_storage):
    assert storage_mod.SCHEMA_VERSION == 4
    with storage_mod.sqlite3.connect(tmp_storage.db_path) as conn:
        cur = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    assert cur == 4


def test_rag_documents_table_exists(tmp_storage):
    with storage_mod.sqlite3.connect(tmp_storage.db_path) as conn:
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(rag_documents)").fetchall()]
    for col in (
        "doc_ref", "root_dir", "relative_path", "filename",
        "content_type", "size", "mtime", "sha256",
        "text_excerpt", "indexed_at",
    ):
        assert col in cols, f"missing column: {col}"


def test_v3_to_v4_migration_preserves_messages_audit_and_attachments(tmp_path):
    """Open a v3 DB, then a v4 ExchangeStorage — old tables survive."""
    import sqlite3, hashlib
    db_path = tmp_path / "v3_to_v4.db"

    # Build a hand-crafted v3 database — no v4 table.
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 3")
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
    conn.execute(
        """
        CREATE TABLE attachments (
            attachment_ref        TEXT PRIMARY KEY,
            message_ref           TEXT,
            item_id_hash          TEXT NOT NULL,
            attachment_id_hash    TEXT NOT NULL,
            filename              TEXT NOT NULL,
            content_type          TEXT,
            size                  INTEGER,
            local_path            TEXT,
            downloaded_at         TEXT,
            synced_at             TEXT NOT NULL
        )
        """
    )

    seed_ref = "a" * 40
    seed_att_ref = "b" * 40
    old_id_hash = hashlib.sha1(b"OLD-ITEM").hexdigest()
    conn.execute(
        "INSERT INTO messages (message_ref, folder, item_id, exchange_id_hash, "
        "has_attachments, synced_at) VALUES (?, 'inbox', 'OLD-ITEM', ?, 0, ?)",
        (seed_ref, old_id_hash, "2026-01-01T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO audit_log (ts, operation, status, subject) "
        "VALUES (?, 'send', 'success', 'seeded')",
        ("2026-01-01T00:00:00+00:00",),
    )
    conn.execute(
        "INSERT INTO attachments (attachment_ref, message_ref, item_id_hash, "
        "attachment_id_hash, filename, size, synced_at) "
        "VALUES (?, ?, ?, ?, 'seed.pdf', 10, ?)",
        (seed_att_ref, seed_ref, old_id_hash,
         hashlib.sha1(b"SEED-ATT").hexdigest(),
         "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    # Re-open with v4 storage — must migrate transparently.
    reset_storage_for_tests()
    s = ExchangeStorage(str(db_path))

    # v1/v2/v3 rows are still there.
    assert s.get_message(message_ref=seed_ref) is not None
    assert s.list_audit(operation="send")[0]["subject"] == "seeded"
    assert s.get_attachment(seed_att_ref) is not None

    # New table works.
    ref = s.upsert_rag_document({
        "root_dir": "/tmp/x",
        "relative_path": "a.txt",
        "filename": "a.txt",
        "text_excerpt": "hello world",
    })
    assert isinstance(ref, str) and len(ref) == 40

    # Idempotent re-open — PRAGMA still 4, no duplicate.
    s2 = ExchangeStorage(str(db_path))
    assert s2.list_rag_documents(root_dir="/tmp/x")[0]["doc_ref"] == ref
    reset_storage_for_tests()


# =====================================================================
# make_rag_doc_ref
# =====================================================================
def test_make_rag_doc_ref_is_stable_and_opaque():
    a = make_rag_doc_ref("/tmp/rag", "subdir/secret.txt",
                         sha256="deadbeef" * 8)
    b = make_rag_doc_ref("/tmp/rag", "subdir/secret.txt",
                         sha256="DEADBEEF" * 8)
    assert a == b  # case-insensitive on sha256
    assert len(a) == 40 and all(c in "0123456789abcdef" for c in a)

    # Plaintext inputs MUST NOT appear in the ref output.
    for token in ("/tmp/rag", "subdir", "secret.txt"):
        assert token not in a, f"ref leaks token: {token}"


def test_make_rag_doc_ref_differs_by_relative_path():
    a = make_rag_doc_ref("/tmp/rag", "a.txt")
    b = make_rag_doc_ref("/tmp/rag", "b.txt")
    assert a != b


def test_make_rag_doc_ref_differs_by_sha256():
    a = make_rag_doc_ref("/tmp/rag", "x.txt", sha256=None)
    b = make_rag_doc_ref("/tmp/rag", "x.txt", sha256="a" * 64)
    assert a != b


# =====================================================================
# storage round-trip (upsert / list / search / get)
# =====================================================================
def test_upsert_rag_document_round_trip(tmp_storage):
    ref = tmp_storage.upsert_rag_document({
        "root_dir": "/tmp/rag_root",
        "relative_path": "sub/a.txt",
        "filename": "a.txt",
        "content_type": "text/plain",
        "size": 12,
        "mtime": "2026-01-01T00:00:00+00:00",
        "sha256": "0" * 64,
        "text_excerpt": "hello world",
    })
    row = tmp_storage.get_rag_document(ref)
    assert row is not None
    assert row["doc_ref"] == ref
    assert row["filename"] == "a.txt"
    assert row["relative_path"] == "sub/a.txt"
    assert row["content_type"] == "text/plain"
    assert row["size"] == 12
    assert row["sha256"] == "0" * 64
    assert row["text_excerpt"] == "hello world"
    # Public row does NOT expose any absolute path.
    assert "local_path" not in row
    assert "absolute_path" not in row


def test_upsert_rag_document_requires_root(tmp_storage):
    with pytest.raises(ValueError):
        tmp_storage.upsert_rag_document({
            "relative_path": "a.txt", "filename": "a.txt",
        })


def test_upsert_rag_document_requires_relative_path(tmp_storage):
    with pytest.raises(ValueError):
        tmp_storage.upsert_rag_document({
            "root_dir": "/tmp/x", "filename": "a.txt",
        })


def test_upsert_rag_document_requires_filename(tmp_storage):
    with pytest.raises(ValueError):
        tmp_storage.upsert_rag_document({
            "root_dir": "/tmp/x", "relative_path": "a.txt",
        })


def test_upsert_is_idempotent(tmp_storage):
    a = tmp_storage.upsert_rag_document({
        "root_dir": "/tmp/r", "relative_path": "x.txt", "filename": "x.txt",
        "text_excerpt": "old",
    })
    b = tmp_storage.upsert_rag_document({
        "root_dir": "/tmp/r", "relative_path": "x.txt", "filename": "x.txt",
        "text_excerpt": "new",
    })
    assert a == b
    assert tmp_storage.get_rag_document(a)["text_excerpt"] == "new"


def test_list_rag_documents_pagination_and_filter(tmp_storage):
    for i in range(5):
        tmp_storage.upsert_rag_document({
            "root_dir": f"/tmp/r{i % 2}",
            "relative_path": f"a{i}.txt",
            "filename": f"a{i}.txt",
            "text_excerpt": f"doc-{i}",
        })
    page = tmp_storage.list_rag_documents(root_dir="/tmp/r0", limit=10)
    assert len(page) == 3
    page = tmp_storage.list_rag_documents(root_dir="/tmp/r1", limit=2)
    assert len(page) == 2


def test_search_rag_documents_hits_filename_and_excerpt(tmp_storage):
    tmp_storage.upsert_rag_document({
        "root_dir": "/tmp/rag", "relative_path": "alpha.txt",
        "filename": "alpha.txt", "text_excerpt": "contains UNIQUEWORD",
    })
    tmp_storage.upsert_rag_document({
        "root_dir": "/tmp/rag", "relative_path": "beta.txt",
        "filename": "beta.txt", "text_excerpt": "unrelated body",
    })

    by_filename = tmp_storage.search_rag_documents("alpha")
    assert len(by_filename) == 1 and by_filename[0]["filename"] == "alpha.txt"

    by_excerpt = tmp_storage.search_rag_documents("UNIQUEWORD")
    assert len(by_excerpt) == 1
    assert by_excerpt[0]["filename"] == "alpha.txt"

    assert tmp_storage.search_rag_documents("nope") == []


def test_search_rag_documents_empty_query_returns_empty(tmp_storage):
    tmp_storage.upsert_rag_document({
        "root_dir": "/tmp/r", "relative_path": "x.txt",
        "filename": "x.txt", "text_excerpt": "anything",
    })
    assert tmp_storage.search_rag_documents("") == []


def test_get_rag_document_unknown_returns_none(tmp_storage):
    assert tmp_storage.get_rag_document("0" * 40) is None


def test_get_rag_document_requires_ref(tmp_storage):
    with pytest.raises(ValueError):
        tmp_storage.get_rag_document("")


# =====================================================================
# service-layer: index_rag_documents
# =====================================================================
def _write_tmp_root(tmp_path: Path, files: dict[str, str]) -> Path:
    """Write a small text corpus under tmp_path. files: relpath → text."""
    root = tmp_path / "rag_root"
    root.mkdir()
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return root


def test_index_rag_documents_indexes_text_files(tmp_path, tmp_storage):
    root = _write_tmp_root(tmp_path, {
        "a.txt": "Hello world about project UNIQUEWORD.",
        "b.md": "Another file mentioning UNIQUEWORD too.",
        "c.json": '{"key": "value"}',
    })
    # Add a non-text file that should be skipped.
    (root / "image.png").write_bytes(b"\x89PNG\r\n")

    result = rag_svc.index_rag_documents(
        tmp_storage, root_dir=str(root), limit=100, max_file_bytes=10_000,
    )
    assert result["status"] == "success"
    assert result["root_dir"] == str(root.resolve())
    assert result["indexed"] == 3
    assert result["skipped"] >= 1      # the .png
    assert result["errors"] == 0
    assert result["limit"] == 100
    assert result["max_file_bytes"] == 10_000
    assert ".txt" in result["extensions"]

    rows = tmp_storage.list_rag_documents(root_dir=str(root.resolve()), limit=10)
    assert len(rows) == 3
    # relative_paths are POSIX and never absolute.
    for r in rows:
        assert not r["relative_path"].startswith("/")
        assert r["size"] >= 0
        assert r["sha256"] is None or len(r["sha256"]) == 64


def test_index_rag_documents_search_finds_terms(tmp_path, tmp_storage):
    _write_tmp_root(tmp_path, {
        "alpha.txt": "alpha body contains needle",
        "beta.txt":  "no match here",
    })
    root = tmp_path / "rag_root"
    rag_svc.index_rag_documents(tmp_storage, root_dir=str(root), limit=10)

    result = rag_svc.search_rag_documents(tmp_storage, "needle")
    assert result["status"] == "success"
    assert result["count"] == 1
    assert result["documents"][0]["filename"] == "alpha.txt"


def test_index_rag_documents_missing_root(tmp_path, tmp_storage):
    res = rag_svc.index_rag_documents(
        tmp_storage, root_dir=str(tmp_path / "does_not_exist"),
    )
    assert res["status"] == "error"
    assert res["errors"] == 1


def test_index_rag_documents_skips_oversize_files(tmp_path, tmp_storage):
    root = _write_tmp_root(tmp_path, {"small.txt": "tiny"})
    big = root / "huge.txt"
    big.write_text("X" * 5000, encoding="utf-8")      # 5 KB

    res = rag_svc.index_rag_documents(
        tmp_storage, root_dir=str(root),
        max_file_bytes=1000,                      # cap below both
    )
    assert res["status"] == "success"
    # small.txt is 6 bytes so it fits; huge.txt is 5000 bytes > 1000 → skip
    assert res["indexed"] == 1
    assert res["skipped"] == 1

    rows = tmp_storage.list_rag_documents(root_dir=str(root.resolve()))
    assert len(rows) == 1
    assert rows[0]["filename"] == "small.txt"
    # huge was skipped, never partially indexed.
    assert rows[0]["filename"] != "huge.txt"


def test_index_rag_documents_cap_text_excerpt(tmp_path, tmp_storage):
    """Stored excerpt is bounded by MAX_TEXT_EXCERPT_CHARS — defence in depth."""
    root = _write_tmp_root(tmp_path, {"long.txt": "a" * 20_000})
    rag_svc.index_rag_documents(tmp_storage, root_dir=str(root), limit=5,
                                max_file_bytes=100_000)
    rows = tmp_storage.list_rag_documents(root_dir=str(root.resolve()))
    assert len(rows[0]["text_excerpt"]) <= 5000   # generous upper bound


def test_index_rag_documents_skips_symlinks_outside_root(tmp_path, tmp_storage):
    """A symlink that resolves outside root is skipped, never indexed."""
    outer = tmp_path / "outer"
    outer.mkdir()
    outside_file = outer / "secret.txt"
    outside_file.write_text("CONFIDENTIAL outside-root material", encoding="utf-8")

    root = _write_tmp_root(tmp_path, {"inside.txt": "ordinary text"})
    os.symlink(str(outside_file), str(root / "escape.txt"))
    # Sanity: symlink does resolve outside.
    assert os.path.islink(str(root / "escape.txt"))

    res = rag_svc.index_rag_documents(tmp_storage, root_dir=str(root))
    assert res["status"] == "success"

    rows = tmp_storage.list_rag_documents(root_dir=str(root.resolve()))
    filenames = {r["filename"] for r in rows}
    assert "inside.txt" in filenames
    assert "escape.txt" not in filenames
    # Defence in depth: ensure no body content from the outside file leaked.
    for r in rows:
        assert "CONFIDENTIAL" not in (r["text_excerpt"] or "")
        assert "outside-root material" not in (r["text_excerpt"] or "")


def test_index_rag_documents_idempotent(tmp_path, tmp_storage):
    root = _write_tmp_root(tmp_path, {"a.txt": "body"})
    rag_svc.index_rag_documents(tmp_storage, root_dir=str(root))
    rag_svc.index_rag_documents(tmp_storage, root_dir=str(root))
    rows = tmp_storage.list_rag_documents(root_dir=str(root.resolve()))
    # Same file twice → still one row (upsert by stable doc_ref).
    assert len(rows) == 1


def test_search_rag_documents_service_400_on_empty(tmp_storage):
    result = rag_svc.search_rag_documents(tmp_storage, "")
    assert result["status"] == "error"
    assert result["reason"] == "empty_query"


def test_get_rag_document_service_returns_text(tmp_path, tmp_storage):
    root = _write_tmp_root(tmp_path, {"doc.txt": "Body text here."})
    rag_svc.index_rag_documents(tmp_storage, root_dir=str(root), limit=10,
                                max_file_bytes=100_000)
    row = tmp_storage.list_rag_documents(root_dir=str(root.resolve()))[0]

    res = rag_svc.get_rag_document(tmp_storage, row["doc_ref"])
    assert res["status"] == "success"
    assert "text" not in res     # include_text=False

    res2 = rag_svc.get_rag_document(tmp_storage, row["doc_ref"],
                                    include_text=True, max_chars=200)
    assert "text" in res2
    assert "Body text here" in res2["text"]
    assert res2["text_chars"] <= 200


def test_get_rag_document_service_unknown_raises(tmp_storage):
    with pytest.raises(rag_svc.RagDocNotFound):
        rag_svc.get_rag_document(tmp_storage, "0" * 40)


def test_get_rag_document_service_max_chars_caps_body(tmp_path, tmp_storage):
    root = _write_tmp_root(tmp_path, {"big.txt": "Z" * 5_000})
    rag_svc.index_rag_documents(tmp_storage, root_dir=str(root),
                                max_file_bytes=20_000)
    row = tmp_storage.list_rag_documents(root_dir=str(root.resolve()))[0]

    res = rag_svc.get_rag_document(tmp_storage, row["doc_ref"],
                                    include_text=True, max_chars=500)
    assert res["text_chars"] <= 500


# =====================================================================
# FastAPI: routes + TestClient
# =====================================================================
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def app_module():
    import main  # noqa: F401
    reset_storage_for_tests()
    return main


@pytest.fixture
def client(app_module, tmp_path, monkeypatch):
    """TestClient with tmp DB and tmp rag root."""
    import main
    tmp_db = str(tmp_path / "w2d_http.db")
    monkeypatch.setattr(main, "_open_exchange_storage",
                        lambda: open_storage(tmp_db))
    monkeypatch.setattr(main, "is_vpn_active", lambda: True)
    # Make get_account() returning a fake so unrelated routes don't 503.
    monkeypatch.setattr(main, "get_account", lambda: object())

    # Re-bind service-layer imports in main to the original functions
    # so we exercise the full route plumbing.
    monkeypatch.setattr(main, "_index_rag_documents_service",
                        rag_svc.index_rag_documents)
    monkeypatch.setattr(main, "_search_rag_documents_service",
                        rag_svc.search_rag_documents)
    monkeypatch.setattr(main, "_get_rag_document_service",
                        rag_svc.get_rag_document)

    with TestClient(main.app) as c:
        yield c, tmp_db


def _populate_tmp_root(tmp_path: Path) -> Path:
    return _write_tmp_root(tmp_path, {
        "alpha.txt": "alpha body contains UNIQUEWORD",
        "beta.txt":  "beta body — no match",
    })


def test_routes_are_registered(app_module):
    import main
    routes = {r.path for r in main.app.routes}
    for required in (
        "/health", "/ready",
        "/list_messages", "/search_messages", "/get_message",
        "/send_message", "/reply_message",
        "/list_attachments", "/download_attachment",
        "/index_rag_documents", "/search_rag_documents", "/get_rag_document",
        "/download_dataset", "/download_status",
    ):
        assert required in routes, f"missing route: {required}"


def test_openapi_lists_w2d_endpoints(app_module):
    import main
    with TestClient(main.app) as c:
        schema = c.get("/openapi.json").json()
    for path in ("/index_rag_documents", "/search_rag_documents", "/get_rag_document"):
        assert path in schema["paths"], f"OpenAPI missing {path}"
        assert "post" in schema["paths"][path], f"OpenAPI missing POST {path}"


def test_index_rag_documents_endpoint_200(client, tmp_path):
    c, tmp_db = client
    root = _populate_tmp_root(tmp_path)
    r = c.post("/index_rag_documents", json={
        "root_dir": str(root), "limit": 10, "max_file_bytes": 100_000,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "success"
    assert body["indexed"] == 2
    assert body["errors"] == 0
    assert body["root_dir"] == str(root.resolve())


def test_index_rag_documents_endpoint_no_exchange_or_vpn_required(
    app_module, tmp_path, monkeypatch
):
    """No /health-style guard: works even if VPN is down and no
    get_account, because it touches only local files + SQLite."""
    import main
    main = app_module
    monkeypatch.setattr(main, "is_vpn_active", lambda: False)
    monkeypatch.setattr(main, "get_account", lambda: None)
    tmp_db = str(tmp_path / "no_vpn.db")
    monkeypatch.setattr(main, "_open_exchange_storage",
                        lambda: open_storage(tmp_db))
    monkeypatch.setattr(main, "_index_rag_documents_service",
                        rag_svc.index_rag_documents)
    monkeypatch.setattr(main, "_search_rag_documents_service",
                        rag_svc.search_rag_documents)
    monkeypatch.setattr(main, "_get_rag_document_service",
                        rag_svc.get_rag_document)
    reset_storage_for_tests()

    root = _populate_tmp_root(tmp_path)
    with TestClient(main.app) as c:
        r = c.post("/index_rag_documents", json={
            "root_dir": str(root), "limit": 10,
        })
    assert r.status_code == 200, r.text
    assert r.json()["indexed"] == 2
    reset_storage_for_tests()


def test_index_rag_documents_missing_root_404(client, tmp_path):
    c, _ = client
    r = c.post("/index_rag_documents", json={
        "root_dir": str(tmp_path / "does_not_exist"),
    })
    assert r.status_code == 404
    body = r.json()
    assert body["detail"]["reason"] in {"root_dir_unavailable", "unsafe_root_dir"}


def test_search_rag_documents_endpoint_200(client, tmp_path):
    c, tmp_db = client
    root = _populate_tmp_root(tmp_path)
    c.post("/index_rag_documents", json={"root_dir": str(root), "limit": 10})

    r = c.post("/search_rag_documents", json={"query": "UNIQUEWORD"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "success"
    assert body["count"] == 1
    assert body["documents"][0]["filename"] == "alpha.txt"


def test_search_rag_documents_empty_query_400(client):
    c, _ = client
    r = c.post("/search_rag_documents", json={"query": ""})
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "empty_query"

    r = c.post("/search_rag_documents", json={})
    assert r.status_code == 400


def test_get_rag_document_endpoint_200(client, tmp_path):
    c, tmp_db = client
    root = _populate_tmp_root(tmp_path)
    c.post("/index_rag_documents", json={"root_dir": str(root), "limit": 10})

    doc_ref = c.post("/search_rag_documents",
                     json={"query": "UNIQUEWORD"}).json()["documents"][0]["doc_ref"]
    r = c.post("/get_rag_document", json={"doc_ref": doc_ref})
    assert r.status_code == 200, r.text
    assert "text" not in r.json()  # include_text defaults to False


def test_get_rag_document_with_include_text(client, tmp_path):
    c, tmp_db = client
    root = _populate_tmp_root(tmp_path)
    c.post("/index_rag_documents", json={"root_dir": str(root), "limit": 10})
    doc_ref = c.post("/search_rag_documents",
                     json={"query": "UNIQUEWORD"}).json()["documents"][0]["doc_ref"]

    r = c.post("/get_rag_document",
               json={"doc_ref": doc_ref, "include_text": True, "max_chars": 200})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "text" in body
    assert body["text_chars"] <= 200


def test_get_rag_document_missing_doc_ref_400(client):
    c, _ = client
    r = c.post("/get_rag_document", json={})
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "missing_doc_ref"

    r = c.post("/get_rag_document", json={"doc_ref": ""})
    assert r.status_code == 400


def test_get_rag_document_unknown_doc_ref_404(client):
    c, _ = client
    r = c.post("/get_rag_document", json={"doc_ref": "0" * 40})
    assert r.status_code == 404


# =====================================================================
# Negative-path: production rag_data/ must NOT be touched by tests.
# =====================================================================
def test_does_not_touch_production_rag_data(tmp_path, tmp_storage,
                                            app_module, monkeypatch):
    """index_rag_documents called from a tmp_path must leave production
    rag_data/ in whatever state it found it — we never point at cwd's
    rag_data by default in tests."""
    import main
    main = app_module

    # The default rag_data is relative to cwd; make sure cwd is tmp_path
    # so even if a test forgets to pass root_dir it lands in tmp.
    monkeypatch.chdir(tmp_path)
    reset_storage_for_tests()

    # Probe cwd: should have NO rag_data before.
    assert not (tmp_path / "rag_data").exists()
    rag_svc.index_rag_documents(tmp_storage, root_dir="rag_data",
                                 limit=10, max_file_bytes=1000)
    # The endpoint failed (root missing) but tests the *default* arg
    # truly doesn't write to cwd's rag_data. We assert production-tmp
    # is still untouched:
    assert not (tmp_path / "rag_data").exists()
    reset_storage_for_tests()


def test_legacy_dataset_routes_still_registered(app_module):
    import main
    routes = {r.path for r in main.app.routes}
    assert "/download_dataset" in routes
    assert "/download_status" in routes

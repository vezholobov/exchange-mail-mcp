"""
exchange_storage.py — read-only Exchange message metadata cache (W2a).

A small stdlib-only SQLite layer that indexes Exchange items by a stable
`message_ref` so the API can list/search/fetch metadata without re-hitting
Exchange for every call. The actual mail body / attachments still live in
Exchange (or in rag_data/) — this module only stores *metadata*.

Design constraints:
  * stdlib only (sqlite3) — no ORM, no FastAPI/exchangelib imports.
    Importable from tests without network or auth side effects.
  * NEVER sends, replies to, marks as read, or deletes Exchange items —
    it only reads metadata. Reads themselves are driven by main.py.
  * Does NOT import or read any value from .env. Operates only on a
    caller-supplied DB path (default: <cwd>/exchange_metadata.db).
  * Tables / schemas are versioned via PRAGMA user_version. Adding a
    column is a future migration; for W2a we use a single fresh table.

Schema (v1):
    messages(
        message_ref        TEXT PRIMARY KEY,   -- stable hash, see make_message_ref
        folder             TEXT NOT NULL,
        item_id            TEXT NOT NULL,
        exchange_id_hash   TEXT NOT NULL,      -- SHA1(item_id), no plaintext id leak
        changekey          TEXT,                -- may be NULL for read-only folders
        internet_message_id TEXT,
        conversation_id    TEXT,
        received_at        TEXT,                -- ISO-8601 UTC
        sender_email       TEXT,
        sender_name        TEXT,
        subject            TEXT,
        snippet            TEXT,                -- <= ~200 chars
        has_attachments    INTEGER NOT NULL,   -- 0 / 1
        synced_at          TEXT NOT NULL       -- ISO-8601 UTC
    )

Schema (v2, additive — W2b):
    audit_log(
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              TEXT NOT NULL,         -- ISO-8601 UTC
        operation       TEXT NOT NULL,         -- send / reply / send_error / reply_error
        message_ref     TEXT,                  -- NULL if no cache row was found
        item_id_hash    TEXT,                  -- SHA1 of raw item id; raw id is NEVER stored
        to_recipients   TEXT,                  -- JSON list (no secrets)
        cc_recipients   TEXT,                  -- JSON list
        bcc_recipients  TEXT,                  -- JSON list
        subject         TEXT,
        status          TEXT NOT NULL,         -- success | error
        error           TEXT                   -- human-readable error string, may be NULL
    )

Schema (v3, additive — W2c):
    attachments(
        attachment_ref        TEXT PRIMARY KEY,  -- stable opaque ref, no raw id leak
        message_ref           TEXT,              -- nullable link to messages.message_ref
        item_id_hash          TEXT NOT NULL,     -- SHA1 of raw item id
        attachment_id_hash    TEXT NOT NULL,     -- SHA1 of raw attachment id OR fallback name hash
        filename              TEXT NOT NULL,
        content_type          TEXT,
        size                  INTEGER,
        local_path            TEXT,
        downloaded_at         TEXT,
        synced_at             TEXT NOT NULL      -- ISO-8601 UTC
    )

Schema (v4, additive — W2d):
    rag_documents(
        doc_ref        TEXT PRIMARY KEY,        -- opaque stable hash of root+relpath+sha256
        root_dir       TEXT NOT NULL,           -- resolved absolute root path
        relative_path  TEXT NOT NULL,           -- path under root (POSIX)
        filename       TEXT NOT NULL,
        content_type   TEXT,
        size           INTEGER,
        mtime          TEXT,
        sha256         TEXT,
        text_excerpt   TEXT,
        indexed_at     TEXT NOT NULL
    )

Indexes:
    idx_messages_folder       (folder)
    idx_messages_received_at  (received_at)
    idx_messages_sender       (sender_email)
    idx_audit_log_ts          (ts)
    idx_audit_log_op          (operation)
    idx_attachments_message   (message_ref)
    idx_attachments_item      (item_id_hash)
    idx_rag_documents_root    (root_dir)

Public API:
    open_storage(db_path=None) -> ExchangeStorage
    ExchangeStorage.upsert_message(record) -> str  (returns message_ref)
    ExchangeStorage.list_messages(folder=None, limit=50, offset=0,
                                  received_after=None, received_before=None)
        -> list[dict]
    ExchangeStorage.search_messages(query, folder=None, limit=50, offset=0,
                                    received_after=None, received_before=None)
        -> list[dict]
    ExchangeStorage.get_message(message_ref=None, item_id=None)
        -> Optional[dict]
    ExchangeStorage.count(folder=None, received_after=None,
                          received_before=None) -> int
    ExchangeStorage.log_audit(...) -> int                  (returns row id, W2b)
    ExchangeStorage.list_audit(limit=50, operation=None)  (W2b)
    ExchangeStorage.upsert_attachment(record) -> str      (returns attachment_ref, W2c)
    ExchangeStorage.list_attachments(message_ref=None,
                                     item_id=None)        (W2c)
    ExchangeStorage.get_attachment(attachment_ref) -> Optional[dict]  (W2c)
    make_message_ref(folder, item_id, changekey=None) -> str
    make_attachment_ref(message_ref=None, item_id=None,
                        attachment_id=None, filename=None) -> str  (W2c)
    message_record_from_exchange(item, folder_name) -> dict
    make_rag_doc_ref(root_dir, relative_path, sha256=None) -> str  (W2d)
    ExchangeStorage.upsert_rag_document(record) -> str              (W2d)
    ExchangeStorage.list_rag_documents(limit=50, offset=0,
                                       root_dir=None)               (W2d)
    ExchangeStorage.search_rag_documents(query, limit=20,
                                         root_dir=None)             (W2d)
    ExchangeStorage.get_rag_document(doc_ref) -> Optional[dict]     (W2d)
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

logger = logging.getLogger("exchange_storage")

SCHEMA_VERSION = 4

DEFAULT_DB_PATH = "exchange_metadata.db"

# Max snippet length (chars). Defensive — we never want a 5MB body in SQLite.
SNIPPET_MAX_LEN = 240


# -----------------------------------------------------------------------------
# Reference hashing — stable across processes.
# -----------------------------------------------------------------------------
def hash_item_id(item_id: Any) -> str:
    """Stable SHA1 of a raw Exchange item_id (which may be a class with
    .id/.changekey, an opaque string, or bytes). Returns hex."""
    if item_id is None:
        raw = ""
    elif isinstance(item_id, bytes):
        raw = item_id.decode("utf-8", errors="replace")
    elif isinstance(item_id, str):
        raw = item_id
    else:
        # exchangelib ItemId objects typically stringify predictably;
        # fall back to repr so we never crash the metadata write.
        raw = str(item_id)
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()


def make_message_ref(folder: str, item_id: Any, changekey: Optional[str] = None) -> str:
    """Stable opaque ref for a (folder, item, changekey) tuple.

    We hash the input tuple so the API exposes no plaintext Exchange id —
    callers must resolve via get_message() or use the ref returned by
    list/search, which decouples the client from raw IDs.
    """
    folder_norm = (folder or "").strip().lower()
    id_hash = hash_item_id(item_id)
    ck = (changekey or "").strip()
    payload = f"{folder_norm}|{id_hash}|{ck}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def make_attachment_ref(
    message_ref: Optional[str] = None,
    item_id: Any = None,
    attachment_id: Any = None,
    filename: Optional[str] = None,
) -> str:
    """Stable opaque ref for an attachment.

    Hashes the (message_ref|item_id_hash|attachment_id_hash) tuple. The
    filename is intentionally NOT part of the hash — it is plaintext user
    data, not a secret, and we want renaming an attachment to NOT change its
    ref. If attachment_id is missing (some EWS endpoints only expose a
    `name`), we fall back to the message_ref hash so the ref remains
    collision-free but stable.

    NEVER returns the raw item_id / attachment_id / filename in the output —
    the entire payload is a hex SHA1 string.
    """
    id_hash = hash_item_id(item_id) if item_id is not None else ""
    # Hash attachment_id when available; otherwise synthesise a stable
    # fallback from the filename (we still don't want to leak the plaintext
    # filename into the ref).
    if attachment_id is not None and attachment_id != "":
        att_hash = hash_item_id(attachment_id)
    elif filename:
        att_hash = hash_item_id(filename)
    else:
        att_hash = ""
    msg_ref = (message_ref or "").strip()
    payload = f"att|{msg_ref}|{id_hash}|{att_hash}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def make_rag_doc_ref(
    root_dir: str,
    relative_path: str,
    sha256: Optional[str] = None,
) -> str:
    """Stable opaque ref for a RAG document (W2d).

    Hashes the (root_dir, relative_path, sha256) tuple. The plaintext
    root_dir or relative_path is NEVER leaked into the output — the entire
    payload is a hex SHA1 string. sha256 is optional so callers can
    pre-hash the file body before insertion; when absent, the ref is
    based only on (root_dir|relative_path).

    sha256 is normalised to lowercase before hashing so callers that
    pass hex-case-agnostic digests still get a stable ref.
    """
    root_norm = (root_dir or "").strip()
    rel_norm = (relative_path or "").strip().replace("\\", "/")
    digest = (sha256 or "").strip().lower()
    payload = f"rag|{root_norm}|{rel_norm}|{digest}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


# -----------------------------------------------------------------------------
# Date utilities — accept datetime or ISO string, normalise to UTC ISO.
# -----------------------------------------------------------------------------
def _to_iso_utc(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        # accept ISO with or without 'Z'
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    elif isinstance(value, datetime):
        dt = value
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat()


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# -----------------------------------------------------------------------------
# Storage class — one instance per process. Thread-safe via per-call
# connections (sqlite3 default threadsafety=1; opening per request is fine
# for the expected read-only traffic of W2a).
# -----------------------------------------------------------------------------
class ExchangeStorage:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        # Ensure schema exists.
        with self._connect() as conn:
            self._ensure_schema(conn)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        # Read current version so we can migrate step-by-step.
        try:
            cur = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
        except Exception:
            cur = 0

        # v1: messages table (idempotent — IF NOT EXISTS is safe).
        if cur < 1:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
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
                "CREATE INDEX IF NOT EXISTS idx_messages_folder "
                "ON messages(folder)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_received_at "
                "ON messages(received_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_sender "
                "ON messages(sender_email)"
            )
            conn.execute("PRAGMA user_version = 1")

        # v2: audit_log table (W2b). Raw item_id is NEVER stored — only SHA1.
        if cur < 2:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
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
                "CREATE INDEX IF NOT EXISTS idx_audit_log_ts "
                "ON audit_log(ts)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_log_op "
                "ON audit_log(operation)"
            )
            conn.execute("PRAGMA user_version = 2")

        # v3: attachments table (W2c). Raw item_id / attachment_id are NEVER
        # stored — only SHA1 hashes. filename is plaintext (it's just metadata
        # like "report.pdf"), but the path of the file on disk is also stored.
        if cur < 3:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS attachments (
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
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_attachments_message "
                "ON attachments(message_ref)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_attachments_item "
                "ON attachments(item_id_hash)"
            )
            conn.execute("PRAGMA user_version = 3")

        # v4: rag_documents table (W2d). Local-file index of the rag_data
        # tree. Public rows expose `relative_path` (safe — under root) but
        # never the absolute on-disk path.
        if cur < 4:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_documents (
                    doc_ref        TEXT PRIMARY KEY,
                    root_dir       TEXT NOT NULL,
                    relative_path  TEXT NOT NULL,
                    filename       TEXT NOT NULL,
                    content_type   TEXT,
                    size           INTEGER,
                    mtime          TEXT,
                    sha256         TEXT,
                    text_excerpt   TEXT,
                    indexed_at     TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rag_documents_root "
                "ON rag_documents(root_dir)"
            )
            conn.execute("PRAGMA user_version = 4")

        # Always re-assert in case the file already had v2 set but missing indexes.
        if cur < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # ----- writes ----------------------------------------------------------
    def upsert_message(self, record: Dict[str, Any]) -> str:
        """Insert or update one message row. Returns the message_ref.

        Required keys in record: folder, item_id. The rest is optional.
        Raises ValueError if the record is malformed.
        """
        folder = (record.get("folder") or "").strip()
        item_id = record.get("item_id")
        if not folder:
            raise ValueError("record.folder is required")
        if item_id is None:
            raise ValueError("record.item_id is required")

        ref = make_message_ref(folder, item_id, record.get("changekey"))
        exchange_id_hash = hash_item_id(item_id)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO messages (
                    message_ref, folder, item_id, exchange_id_hash,
                    changekey, internet_message_id, conversation_id,
                    received_at, sender_email, sender_name, subject,
                    snippet, has_attachments, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_ref) DO UPDATE SET
                    folder             = excluded.folder,
                    item_id            = excluded.item_id,
                    exchange_id_hash   = excluded.exchange_id_hash,
                    changekey          = excluded.changekey,
                    internet_message_id = excluded.internet_message_id,
                    conversation_id    = excluded.conversation_id,
                    received_at        = excluded.received_at,
                    sender_email       = excluded.sender_email,
                    sender_name        = excluded.sender_name,
                    subject            = excluded.subject,
                    snippet            = excluded.snippet,
                    has_attachments    = excluded.has_attachments,
                    synced_at          = excluded.synced_at
                """,
                (
                    ref,
                    folder,
                    str(item_id),
                    exchange_id_hash,
                    record.get("changekey"),
                    record.get("internet_message_id"),
                    record.get("conversation_id"),
                    _to_iso_utc(record.get("received_at")),
                    record.get("sender_email"),
                    record.get("sender_name"),
                    record.get("subject"),
                    (record.get("snippet") or "")[:SNIPPET_MAX_LEN],
                    1 if record.get("has_attachments") else 0,
                    _to_iso_utc(record.get("synced_at")) or _iso_now(),
                ),
            )
        return ref

    def upsert_many(self, records: Iterable[Dict[str, Any]]) -> List[str]:
        refs = []
        for r in records:
            refs.append(self.upsert_message(r))
        return refs

    # ----- reads -----------------------------------------------------------
    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        # rename internal exchange_id_hash to a stable API field
        d["id_hash"] = d.pop("exchange_id_hash")
        d["has_attachments"] = bool(d.get("has_attachments", 0))
        return d

    def _select_where(
        self,
        *,
        folder: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        received_after: Optional[str] = None,
        received_before: Optional[str] = None,
        like_clause: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses, args = [], []
        if folder:
            clauses.append("folder = ?")
            args.append(folder)
        if received_after:
            clauses.append("received_at >= ?")
            args.append(_to_iso_utc(received_after))
        if received_before:
            clauses.append("received_at <= ?")
            args.append(_to_iso_utc(received_before))
        if like_clause:
            clauses.append(
                "(" + " OR ".join(like_clause[0]) + ")"
            )
            args.extend(like_clause[1])
        sql = (
            "SELECT * FROM messages"
            + (" WHERE " + " AND ".join(clauses) if clauses else "")
            + " ORDER BY received_at DESC, message_ref ASC"
            + " LIMIT ? OFFSET ?"
        )
        args.extend([int(limit), int(offset)])
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def list_messages(
        self,
        folder: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        received_after: Optional[str] = None,
        received_before: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self._select_where(
            folder=folder,
            limit=limit,
            offset=offset,
            received_after=received_after,
            received_before=received_before,
        )

    def search_messages(
        self,
        query: str,
        folder: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        received_after: Optional[str] = None,
        received_before: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return self.list_messages(
                folder=folder,
                limit=limit,
                offset=offset,
                received_after=received_after,
                received_before=received_before,
            )
        like = (
            ("subject LIKE ?", "sender_name LIKE ?", "sender_email LIKE ?", "snippet LIKE ?"),
            (f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"),
        )
        return self._select_where(
            folder=folder,
            limit=limit,
            offset=offset,
            received_after=received_after,
            received_before=received_before,
            like_clause=like,
        )

    def get_message(
        self,
        *,
        message_ref: Optional[str] = None,
        item_id: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        if not message_ref and item_id is None:
            raise ValueError("either message_ref or item_id is required")
        with self._connect() as conn:
            if message_ref:
                row = conn.execute(
                    "SELECT * FROM messages WHERE message_ref = ?", (message_ref,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM messages WHERE exchange_id_hash = ? "
                    "ORDER BY synced_at DESC LIMIT 1",
                    (hash_item_id(item_id),),
                ).fetchone()
        return self._row_to_dict(row) if row else None

    def count(
        self,
        folder: Optional[str] = None,
        received_after: Optional[str] = None,
        received_before: Optional[str] = None,
    ) -> int:
        clauses, args = [], []
        if folder:
            clauses.append("folder = ?")
            args.append(folder)
        if received_after:
            clauses.append("received_at >= ?")
            args.append(_to_iso_utc(received_after))
        if received_before:
            clauses.append("received_at <= ?")
            args.append(_to_iso_utc(received_before))
        sql = (
            "SELECT COUNT(*) FROM messages"
            + (" WHERE " + " AND ".join(clauses) if clauses else "")
        )
        with self._connect() as conn:
            return int(conn.execute(sql, args).fetchone()[0])

    # ----- audit log (W2b) -------------------------------------------------
    def log_audit(
        self,
        *,
        operation: str,
        status: str,
        message_ref: Optional[str] = None,
        item_id: Any = None,
        to_recipients: Any = None,
        cc_recipients: Any = None,
        bcc_recipients: Any = None,
        subject: Optional[str] = None,
        error: Optional[str] = None,
        ts: Optional[str] = None,
    ) -> int:
        """Write one audit row. Returns the new row id.

        `item_id` is hashed via hash_item_id before insert — the raw id is
        NEVER persisted. Callers may pass either a string or an exchangelib
        ItemId; the hashing helper handles both.

        `to/cc/bcc_recipients` accept either a list or a single string; both
        are serialised to JSON so callers do not need to pre-format.
        """
        if not operation:
            raise ValueError("operation is required")
        if status not in ("success", "error"):
            raise ValueError("status must be 'success' or 'error'")

        item_id_hash = hash_item_id(item_id) if item_id is not None else None
        ts_val = ts or _iso_now()

        def _serialise_recipients(value: Any) -> Optional[str]:
            if value is None:
                return None
            if isinstance(value, str):
                payload = [value]
            else:
                payload = list(value)
            try:
                return json.dumps(payload, ensure_ascii=False)
            except Exception:
                # Never let a recipient serialization error crash audit writes.
                return json.dumps([str(x) for x in payload], ensure_ascii=False)

        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO audit_log (
                    ts, operation, message_ref, item_id_hash,
                    to_recipients, cc_recipients, bcc_recipients,
                    subject, status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts_val,
                    operation,
                    message_ref,
                    item_id_hash,
                    _serialise_recipients(to_recipients),
                    _serialise_recipients(cc_recipients),
                    _serialise_recipients(bcc_recipients),
                    subject,
                    status,
                    error,
                ),
            )
            return int(cur.lastrowid)

    def list_audit(
        self,
        *,
        limit: int = 50,
        operation: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return audit rows ordered newest first. Useful for tests and
        future admin/debug endpoints. The caller is expected NOT to expose
        raw item ids — there are none in this table.
        """
        clauses, args = [], []
        if operation:
            clauses.append("operation = ?")
            args.append(operation)
        sql = (
            "SELECT id, ts, operation, message_ref, item_id_hash, "
            "to_recipients, cc_recipients, bcc_recipients, subject, "
            "status, error FROM audit_log"
            + (" WHERE " + " AND ".join(clauses) if clauses else "")
            + " ORDER BY id DESC LIMIT ?"
        )
        args.append(int(max(1, min(limit, 1000))))
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            # Decode JSON recipient lists so callers don't have to.
            for key in ("to_recipients", "cc_recipients", "bcc_recipients"):
                v = d.get(key)
                if v:
                    try:
                        d[key] = json.loads(v)
                    except Exception:
                        d[key] = None
                else:
                    d[key] = None
            out.append(d)
        return out

    # ----- attachments (W2c) --------------------------------------------
    def upsert_attachment(self, record: Dict[str, Any]) -> str:
        """Insert or update one attachment row. Returns attachment_ref.

        Required keys in record:
            item_id          — raw Exchange item id (hashed before storage)
            attachment_id    — raw Exchange attachment id (hashed before
                               storage). May be None; in that case
                               `filename` is used as a stable fallback hash.
            filename         — attachment filename (plaintext, stored)

        Optional keys:
            message_ref      — opaque link to messages.message_ref (nullable)
            content_type     — MIME type or None
            size             — int or None
            local_path       — absolute path on disk once downloaded
            downloaded_at    — ISO-8601 UTC timestamp (set by service on save)
            synced_at        — ISO-8601 UTC timestamp (defaults to now)

        Public rows never expose raw item_id / attachment_id — only hashes.
        """
        item_id = record.get("item_id")
        if item_id is None:
            raise ValueError("record.item_id is required for attachments")
        filename = record.get("filename")
        if not filename or not str(filename).strip():
            raise ValueError("record.filename is required for attachments")

        attachment_id = record.get("attachment_id")
        message_ref = record.get("message_ref")

        explicit_ref = record.get("attachment_ref")
        if explicit_ref:
            ref = str(explicit_ref).strip()
            if len(ref) != 40 or any(ch not in "0123456789abcdef" for ch in ref):
                raise ValueError("record.attachment_ref must be a SHA1 hex ref")
        else:
            ref = make_attachment_ref(
                message_ref=message_ref,
                item_id=item_id,
                attachment_id=attachment_id,
                filename=str(filename),
            )
        item_id_hash = hash_item_id(item_id)
        attachment_id_hash = hash_item_id(
            attachment_id if attachment_id not in (None, "") else filename
        )

        size_val = record.get("size")
        try:
            size_int = int(size_val) if size_val is not None else None
        except Exception:
            size_int = None

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO attachments (
                    attachment_ref, message_ref, item_id_hash,
                    attachment_id_hash, filename, content_type,
                    size, local_path, downloaded_at, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(attachment_ref) DO UPDATE SET
                    message_ref        = excluded.message_ref,
                    item_id_hash       = excluded.item_id_hash,
                    attachment_id_hash = excluded.attachment_id_hash,
                    filename           = excluded.filename,
                    content_type       = excluded.content_type,
                    size               = excluded.size,
                    local_path         = excluded.local_path,
                    downloaded_at      = excluded.downloaded_at,
                    synced_at          = excluded.synced_at
                """,
                (
                    ref,
                    message_ref,
                    item_id_hash,
                    attachment_id_hash,
                    str(filename),
                    record.get("content_type"),
                    size_int,
                    record.get("local_path"),
                    _to_iso_utc(record.get("downloaded_at")),
                    _to_iso_utc(record.get("synced_at")) or _iso_now(),
                ),
            )
        return ref

    def list_attachments(
        self,
        *,
        message_ref: Optional[str] = None,
        item_id: Any = None,
    ) -> List[Dict[str, Any]]:
        """Return attachment rows for a given message.

        Both message_ref and item_id are optional but at least one must be
        provided. item_id is hashed internally — raw id is never queried.
        Public rows do NOT include raw item_id / attachment_id.
        """
        if not message_ref and item_id is None:
            raise ValueError("either message_ref or item_id is required")

        clauses, args = [], []
        if message_ref:
            clauses.append("message_ref = ?")
            args.append(message_ref)
        if item_id is not None:
            clauses.append("item_id_hash = ?")
            args.append(hash_item_id(item_id))

        sql = (
            "SELECT attachment_ref, message_ref, filename, content_type, "
            "size, local_path, downloaded_at, synced_at FROM attachments"
            + (" WHERE " + " AND ".join(clauses) if clauses else "")
            + " ORDER BY attachment_ref ASC"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def get_attachment(self, attachment_ref: str) -> Optional[Dict[str, Any]]:
        """Fetch one attachment row by opaque ref.

        Returns None if not found. Public shape exposes hashes (item_id_hash,
        attachment_id_hash) for symmetry with the messages table — but NEVER
        the raw item_id / attachment_id columns.
        """
        if not attachment_ref:
            raise ValueError("attachment_ref is required")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT attachment_ref, message_ref, item_id_hash, "
                "attachment_id_hash, filename, content_type, "
                "size, local_path, downloaded_at, synced_at "
                "FROM attachments WHERE attachment_ref = ?",
                (attachment_ref,),
            ).fetchone()
        return dict(row) if row else None

    # ----- rag_documents (W2d) ------------------------------------------
    def upsert_rag_document(self, record: Dict[str, Any]) -> str:
        """Insert or update one RAG document row. Returns doc_ref.

        Required keys in record:
            root_dir         — resolved absolute root path
            relative_path    — POSIX-style path under root
            filename         — basename of relative_path

        Optional keys:
            content_type     — MIME guess or None
            size             — int or None
            mtime            — ISO-8601 UTC or None
            sha256           — hex sha256 of file bytes or None
            text_excerpt     — short preview (callers should cap length)
            indexed_at       — ISO-8601 UTC (defaults to now)

        The doc_ref is opaque — we hash (root + relative_path + sha256)
        so the API never leaks the on-disk path. Public rows never expose
        an absolute `local_path` column.
        """
        root_dir = (record.get("root_dir") or "").strip()
        relative_path = (record.get("relative_path") or "").strip()
        filename = (record.get("filename") or "").strip()
        if not root_dir:
            raise ValueError("record.root_dir is required")
        if not relative_path:
            raise ValueError("record.relative_path is required")
        if not filename:
            raise ValueError("record.filename is required")

        # Normalise: store POSIX-style separators in relative_path.
        rel_norm = relative_path.replace("\\", "/")

        sha256_val = (record.get("sha256") or "")
        if sha256_val:
            sha256_val = str(sha256_val).strip().lower()
        size_val = record.get("size")
        try:
            size_int = int(size_val) if size_val is not None else None
        except Exception:
            size_int = None

        ref = make_rag_doc_ref(root_dir, rel_norm, sha256_val or None)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rag_documents (
                    doc_ref, root_dir, relative_path, filename,
                    content_type, size, mtime, sha256,
                    text_excerpt, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(doc_ref) DO UPDATE SET
                    root_dir      = excluded.root_dir,
                    relative_path = excluded.relative_path,
                    filename      = excluded.filename,
                    content_type  = excluded.content_type,
                    size          = excluded.size,
                    mtime         = excluded.mtime,
                    sha256        = excluded.sha256,
                    text_excerpt  = excluded.text_excerpt,
                    indexed_at    = excluded.indexed_at
                """,
                (
                    ref,
                    root_dir,
                    rel_norm,
                    filename,
                    record.get("content_type"),
                    size_int,
                    _to_iso_utc(record.get("mtime")),
                    sha256_val or None,
                    record.get("text_excerpt"),
                    _to_iso_utc(record.get("indexed_at")) or _iso_now(),
                ),
            )
        return ref

    def list_rag_documents(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        root_dir: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return RAG document rows ordered newest indexed first.

        Public shape: never exposes any absolute file path — only the
        `relative_path` (under the configured root) and the opaque
        `doc_ref`. Safe to return directly to API clients.
        """
        clauses, args = [], []
        if root_dir:
            clauses.append("root_dir = ?")
            args.append(root_dir)
        sql = (
            "SELECT doc_ref, root_dir, relative_path, filename, "
            "content_type, size, mtime, sha256, text_excerpt, indexed_at "
            "FROM rag_documents"
            + (" WHERE " + " AND ".join(clauses) if clauses else "")
            + " ORDER BY indexed_at DESC, doc_ref ASC"
            + " LIMIT ? OFFSET ?"
        )
        args.append(int(max(1, min(limit, 1000))))
        args.append(int(max(0, offset)))
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def search_rag_documents(
        self,
        query: str,
        *,
        limit: int = 20,
        root_dir: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """LIKE search over filename + text_excerpt.

        v4 only stores an excerpt (no FTS table). When sqlite is built
        without FTS5 (or to keep W2d bounded) we use a plain LIKE clause.
        Empty query → empty list (caller should validate first).
        """
        q = (query or "").strip()
        if not q:
            return []
        like = f"%{q}%"
        clauses, args = ["(filename LIKE ? OR text_excerpt LIKE ?)"], [like, like]
        if root_dir:
            clauses.append("root_dir = ?")
            args.append(root_dir)
        sql = (
            "SELECT doc_ref, root_dir, relative_path, filename, "
            "content_type, size, mtime, sha256, text_excerpt, indexed_at "
            "FROM rag_documents"
            + " WHERE " + " AND ".join(clauses)
            + " ORDER BY indexed_at DESC, doc_ref ASC"
            + " LIMIT ?"
        )
        args.append(int(max(1, min(limit, 1000))))
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def get_rag_document(self, doc_ref: str) -> Optional[Dict[str, Any]]:
        """Fetch one RAG document row by opaque ref.

        Returns None if not found. Public shape mirrors list_rag_documents:
        safe relative_path only, no absolute path leakage.
        """
        if not doc_ref:
            raise ValueError("doc_ref is required")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT doc_ref, root_dir, relative_path, filename, "
                "content_type, size, mtime, sha256, text_excerpt, indexed_at "
                "FROM rag_documents WHERE doc_ref = ?",
                (doc_ref,),
            ).fetchone()
        return dict(row) if row else None


# -----------------------------------------------------------------------------
# Helpers — turn an Exchange Item into a metadata record (pure function).
# -----------------------------------------------------------------------------
def _sender_email(item) -> Optional[str]:
    try:
        s = item.sender
    except Exception:
        s = None
    if s is None:
        return None
    try:
        return s.email_address
    except Exception:
        return None


def _sender_name(item) -> Optional[str]:
    try:
        s = item.sender
    except Exception:
        s = None
    if s is None:
        return None
    try:
        return s.name
    except Exception:
        return None


def _snippet(item) -> str:
    body = ""
    try:
        body = item.text_body or ""
    except Exception:
        body = ""
    if not body:
        try:
            # Fallback: strip tags very lightly from .body if it's HTML.
            import re
            raw = str(item.body or "")
            body = re.sub(r"<[^>]+>", " ", raw)
        except Exception:
            body = ""
    body = (body or "").replace("\r", " ").replace("\n", " ").strip()
    return body[:SNIPPET_MAX_LEN]


def _has_attachments(item) -> bool:
    try:
        return bool(item.attachments)
    except Exception:
        return False


def _received_at(item) -> Optional[datetime]:
    try:
        dt = item.datetime_received
    except Exception:
        return None
    if dt is None:
        return None
    # exchangelib returns timezone-aware datetimes; if naive, assume UTC.
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def message_record_from_exchange(item, folder_name: str) -> Dict[str, Any]:
    """Build a metadata dict from an exchangelib Item.

    Never raises — broken items surface as rows with missing fields so that
    upstream search still works.

    `folder_name` is normalized to lowercase so list/search filters work
    case-insensitively regardless of how Exchange spelled the folder
    ("Inbox" vs "inbox" vs "Входящие").
    """
    folder_norm = (folder_name or "").strip().lower() or "unknown"
    try:
        changekey = getattr(item, "changekey", None)
    except Exception:
        changekey = None
    try:
        imid = getattr(item, "internet_message_id", None)
    except Exception:
        imid = None
    try:
        conv = getattr(item, "conversation_id", None)
    except Exception:
        conv = None

    return {
        "folder": folder_norm,
        "item_id": getattr(item, "id", None) or getattr(item, "item_id", None),
        "changekey": changekey,
        "internet_message_id": imid,
        "conversation_id": conv,
        "received_at": _received_at(item),
        "sender_email": _sender_email(item),
        "sender_name": _sender_name(item),
        "subject": getattr(item, "subject", None),
        "snippet": _snippet(item),
        "has_attachments": _has_attachments(item),
        "synced_at": _iso_now(),
    }


# -----------------------------------------------------------------------------
# Module-level singleton — main.py imports this.
# -----------------------------------------------------------------------------
_storage_singleton: Optional[ExchangeStorage] = None


def open_storage(db_path: Optional[str] = None) -> ExchangeStorage:
    """Lazy singleton. Pass db_path only from tests; production uses default."""
    global _storage_singleton
    if _storage_singleton is None or (db_path and db_path != _storage_singleton.db_path):
        _storage_singleton = ExchangeStorage(db_path or DEFAULT_DB_PATH)
    return _storage_singleton


def reset_storage_for_tests() -> None:
    """Drop the cached singleton — tests use a tmp DB."""
    global _storage_singleton
    _storage_singleton = None

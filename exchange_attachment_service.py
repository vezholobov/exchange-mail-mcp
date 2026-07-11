"""
exchange_attachment_service.py — testable attachment operations layer (W2c).

This module wraps the EWS attachment access patterns so the new
``/list_attachments`` and ``/download_attachment`` endpoints can be driven
from unit tests without booting FastAPI, exchanging EWS messages, or
touching the network.

It is intentionally a sibling of ``exchange_mail_service.py`` — same shape,
same audit-and-error conventions — but it does NOT mutate the mailbox
(sending, replying, marking as read, deleting). The only filesystem write
is the optional save of the attachment's binary content into a target
directory, which the caller fully controls.

Public API:
    list_attachments_for_message(account, storage, *,
                                 message_ref=None, item_id=None,
                                 inbox_lookup=None,
                                 archive_lookup=None) -> dict

    download_attachment(account, storage, *,
                        message_ref=None, item_id=None,
                        attachment_ref=None, attachment_id=None,
                        filename=None,
                        target_dir='rag_data/attachments',
                        inbox_lookup=None, archive_lookup=None,
                        file_attachment_type=None) -> dict

    AttachmentNotFound   — typed exception for "no such attachment" (404)
    AttachmentSourceError — typed exception for transport-level failures
                            when fetching the attachment payload (500)

Design constraints:
    * stdlib only (sqlite3, os, hashlib). exchangelib is imported lazily
      inside the default inbox/archive lookup helpers so tests can run
      without the network stack.
    * NEVER stores or returns raw Exchange item_id / attachment_id through
      public output. Both are hashed via exchange_storage helpers before
      touching SQLite, and public dicts only carry hashes.
    * No archive extraction in W2c — that path stays with the legacy
      ``/download_dataset`` endpoint. We only persist the raw bytes.
    * Safe filename handling: path-traversal filenames (../../etc/passwd,
      /etc/shadow, NUL bytes) are stripped before they can reach the
      filesystem. The resolved path is verified to be inside ``target_dir``.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from exchange_storage import (
    hash_item_id,
    make_attachment_ref,
)

logger = logging.getLogger("exchange_attachment_service")


# -----------------------------------------------------------------------------
# Typed errors — distinct from arbitrary RuntimeError so main.py can map them
# to the right HTTP status code without inspecting strings.
# -----------------------------------------------------------------------------
class AttachmentNotFound(Exception):
    """No attachment matched the selector (404 in main.py)."""

    def __init__(self, identifier: str, reason: str = "not_found"):
        super().__init__(f"attachment not found: {identifier} ({reason})")
        self.identifier = identifier
        self.reason = reason


class AttachmentSourceError(Exception):
    """EWS attachment fetch failed at transport level (500 in main.py)."""

    def __init__(self, original: Optional[BaseException] = None):
        msg = (
            f"attachment fetch failed: {type(original).__name__ if original else 'unknown'}"
        )
        if original is not None:
            msg = f"{msg}: {original}"
        super().__init__(msg)
        self.original = original


# -----------------------------------------------------------------------------
# Filename sanitisation — strip path traversal, absolute paths, NUL bytes.
# Pure function; safe to import and unit-test independently.
# -----------------------------------------------------------------------------
_BAD_PATH_CHARS = set('\\:*?"<>|\x00')


def safe_filename(name: Any, fallback: str = "attachment", max_len: int = 200) -> str:
    """Return a filename that cannot escape its parent directory.

    Rules:
        * ``None`` / empty / non-string → ``fallback``.
        * Normalise path separators first: replace ``\\`` with ``/`` so that
          ``..\\..\\secret.txt`` is treated identically to ``../../secret.txt``.
        * Drop any path component: ``basename(name)`` first.
        * Reject absolute paths: if ``os.path.isabs`` returns True, strip
          leading separators by re-running ``basename``.
        * Drop NUL bytes and platform-reserved characters.
        * Collapse any remaining ``/`` or ``\\`` separators (defence in
          depth in case basename somehow let them through on a weird OS).
        * Reject names that are pure traversal tokens ("..", ".", "") and
          return ``fallback`` for them.
        * Cap at ``max_len`` chars. The legacy ``clean_filename`` in
          main.py uses 100; we keep 200 for attachment downloads so
          "Quarterly Report — final (v3) — Confidential.pdf" still fits.
    """
    if not name or not isinstance(name, str):
        return fallback
    # Strip NUL bytes first; basename on Windows refuses them outright.
    s = name.replace("\x00", "").strip()
    if not s:
        return fallback
    # Normalise Windows-style separators to POSIX so os.path.basename
    # behaves the same on Linux as it would on Windows.
    s = s.replace("\\", "/")
    # Strip directory parts.
    s = os.path.basename(s)
    # Reject absolute paths.
    if os.path.isabs(s):
        s = os.path.basename(s)
        if not s:
            return fallback
    # Reject pure traversal tokens BEFORE any replacement, so that
    # "../" doesn't sneak back as "_./".
    if s in (".", ".."):
        return fallback
    # Replace reserved characters with underscore.
    s = "".join("_" if ch in _BAD_PATH_CHARS else ch for ch in s)
    # Defence in depth: collapse any remaining separators.
    s = s.replace("/", "_").replace("\\", "_")
    s = s.strip(" .")  # Windows hates trailing dots/spaces
    if not s:
        return fallback
    return s[:max_len]


def _resolve_target_path(
    target_dir: str,
    filename: str,
    *,
    filename_is_safe: bool = False,
) -> Tuple[str, str]:
    """Resolve a safe on-disk path inside ``target_dir``.

    Returns ``(absolute_dir, full_path)``. The full_path is verified to be
    inside the resolved ``absolute_dir``; if it escapes, we fall back to
    a synthetic name. Pure function; no I/O beyond Path.resolve() which
    does not require the directory to exist.
    """
    safe_name = filename if filename_is_safe else safe_filename(filename)
    abs_dir = os.path.realpath(target_dir)
    full = os.path.realpath(os.path.join(abs_dir, safe_name))
    # If the resolved path escaped (e.g. due to a weird symlink layout),
    # replace with a unique-ish synthetic name inside the directory.
    if not (full == abs_dir or full.startswith(abs_dir + os.sep)):
        # hash the bad filename so it remains identifiable in logs but
        # cannot escape the directory.
        import hashlib as _h
        digest = _h.sha1(str(filename).encode("utf-8", "replace")).hexdigest()[:16]
        safe_name = f"attachment_{digest}.bin"
        full = os.path.join(abs_dir, safe_name)
    return abs_dir, full


# -----------------------------------------------------------------------------
# Internal: extract a filename / size / content_type / content from an
# attachment-shaped object. Supports exchangelib's FileAttachment and the
# duck-typed fakes used in tests.
# -----------------------------------------------------------------------------
def _extract_attachment_metadata(att: Any) -> Dict[str, Any]:
    """Return a metadata dict for a single attachment object.

    Reads ``name``, ``content_type``, ``size``, ``attachment_id`` /
    ``_id`` (whichever exists) — fields exchangelib FileAttachment and
    the test fakes both expose. Never raises.
    """
    name = None
    try:
        name = getattr(att, "name", None)
    except Exception:
        name = None

    content_type = None
    for attr in ("content_type", "mime_type"):
        try:
            v = getattr(att, attr, None)
        except Exception:
            v = None
        if v:
            content_type = v
            break

    size = None
    for attr in ("size", "length"):
        try:
            v = getattr(att, attr, None)
        except Exception:
            v = None
        if v is not None:
            try:
                size = int(v)
                break
            except Exception:
                continue

    attachment_id = None
    for attr in ("attachment_id", "_id", "id"):
        try:
            v = getattr(att, attr, None)
        except Exception:
            v = None
        if v is not None and v != "":
            attachment_id = v
            break

    return {
        "name": name,
        "content_type": content_type,
        "size": size,
        "attachment_id": attachment_id,
    }


def _extract_attachment_content(att: Any) -> bytes:
    """Return the raw bytes of an attachment.

    exchangelib's ``FileAttachment.content`` is bytes; test fakes expose
    ``content`` or ``payload``. Try them in order and return whatever we
    get — never raises (empty bytes if all fail).
    """
    for attr in ("content", "payload", "data", "blob"):
        try:
            v = getattr(att, attr, None)
        except Exception:
            v = None
        if v is None:
            continue
        if isinstance(v, bytes):
            return v
        if isinstance(v, str):
            try:
                return v.encode("utf-8")
            except Exception:
                continue
        # Last resort: object with .read()
        if hasattr(v, "read"):
            try:
                return v.read()
            except Exception:
                continue
    return b""


def _select_attachment_from_list(
    attachments: List[Any],
    *,
    attachment_id: Any = None,
    filename: Optional[str] = None,
) -> Tuple[Any, Dict[str, Any]]:
    """Pick the attachment matching the given selector.

    Selection order:
        1. attachment_id match
        2. filename match (case-insensitive on the basename)

    Raises AttachmentNotFound if no match.
    """
    if not attachments:
        raise AttachmentNotFound("<empty>", reason="no_attachments")

    metas = [_extract_attachment_metadata(a) for a in attachments]

    if attachment_id is not None and attachment_id != "":
        for att, meta in zip(attachments, metas):
            if meta.get("attachment_id") == attachment_id:
                return att, meta
        raise AttachmentNotFound(str(attachment_id), reason="attachment_id_mismatch")

    if filename:
        wanted = filename.strip().lower()
        if wanted:
            wanted_safe = safe_filename(filename).strip().lower()
            for att, meta in zip(attachments, metas):
                n = (meta.get("name") or "").strip().lower()
                if n == wanted:
                    return att, meta
            # Fallback: basename match (caller may have passed "subdir/foo.pdf")
            wanted_base = os.path.basename(wanted)
            for att, meta in zip(attachments, metas):
                n = (meta.get("name") or "").strip().lower()
                if os.path.basename(n) == wanted_base:
                    return att, meta
            # Public API returns sanitised basenames. Allow callers to pass
            # that public filename while still matching the raw Exchange name
            # internally (including Windows-style traversal separators).
            for att, meta in zip(attachments, metas):
                n = meta.get("name") or ""
                if safe_filename(n).strip().lower() == wanted_safe:
                    return att, meta
            raise AttachmentNotFound(filename, reason="filename_mismatch")

    raise AttachmentNotFound("<none>", reason="no_selector")


# -----------------------------------------------------------------------------
# Default inbox / archive lookup — mirrors exchange_mail_service.
# -----------------------------------------------------------------------------
def _default_inbox_lookup(account: Any, item_id: str) -> Any:
    try:
        return account.inbox.get(id=item_id)
    except Exception as e:
        logger.info("attachment inbox lookup failed: %s", e)
        return None


def _default_archive_lookup(account: Any, item_id: str) -> Any:
    """Walk the archive folder root for an Inbox child, then try
    archive_root.get(id=...) as a last resort. Returns None if nothing
    matches. Never raises.
    """
    try:
        archive_root = account.archive_msg_folder_root
    except Exception as e:
        logger.info("attachment archive root lookup failed: %s", e)
    else:
        try:
            for child in archive_root.children:
                if (
                    getattr(child, "folder_class", None) == "IPF.Note"
                    and (child.name or "").lower() in ("inbox", "входящие")
                ):
                    try:
                        return child.get(id=item_id)
                    except Exception as e:
                        logger.info("attachment archive inbox get(%s) failed: %s", item_id, e)
                        return None
        except Exception as e:
            logger.info("attachment archive children iteration failed: %s", e)
    # Fallback — try archive_root directly.
    try:
        return account.archive_root.get(id=item_id)
    except Exception as e:
        logger.info("attachment archive_root.get(%s) failed: %s", item_id, e)
        return None


def _resolve_source_item(
    account: Any,
    raw_item_id: str,
    inbox_lookup: Optional[Callable[[Any, str], Any]],
    archive_lookup: Optional[Callable[[Any, str], Any]],
) -> Any:
    inbox_fn = inbox_lookup or _default_inbox_lookup
    archive_fn = archive_lookup or _default_archive_lookup
    item = inbox_fn(account, raw_item_id)
    if item is None:
        item = archive_fn(account, raw_item_id)
    return item


# -----------------------------------------------------------------------------
# list_attachments_for_message
# -----------------------------------------------------------------------------
def list_attachments_for_message(
    account: Any,
    storage: Any,
    *,
    message_ref: Optional[str] = None,
    item_id: Optional[str] = None,
    inbox_lookup: Optional[Callable[[Any, str], Any]] = None,
    archive_lookup: Optional[Callable[[Any, str], Any]] = None,
) -> Dict[str, Any]:
    """Return a structured dict describing all attachments for one message.

    Resolution:
        1. If ``message_ref`` is given, resolve it via ``storage.get_message``
           to recover the raw ``item_id``. The raw id is kept internal.
        2. If ``item_id`` is also passed, it overrides the cache lookup.
        3. The item is fetched from Exchange via inbox → archive lookups.
           Inbox wins.

    The function NEVER returns raw item_id / attachment_id — only hashes.
    Raises:
        AttachmentNotFound  — message not in cache and no item_id given,
                               or item not findable in inbox/archive.
    """
    resolved_message_ref: Optional[str] = message_ref
    raw_item_id: Optional[str] = item_id

    if message_ref and not raw_item_id:
        try:
            rec = storage.get_message(message_ref=message_ref)
        except Exception as e:
            raise AttachmentNotFound(message_ref, reason=f"cache_error:{type(e).__name__}")
        if rec is None:
            raise AttachmentNotFound(message_ref, reason="message_ref_not_in_cache")
        raw_item_id = rec.get("item_id")
        if not raw_item_id:
            raise AttachmentNotFound(message_ref, reason="cache_row_missing_item_id")
    elif not raw_item_id:
        raise AttachmentNotFound("<none>", reason="missing_identifier")

    item = _resolve_source_item(
        account, raw_item_id, inbox_lookup, archive_lookup
    )
    if item is None:
        raise AttachmentNotFound(raw_item_id, reason="target_not_found")

    # Pull the attachment list defensively — never crash on a broken item.
    try:
        attachments = list(item.attachments or [])
    except Exception as e:
        logger.warning("attachments iteration failed for %s: %s",
                       hash_item_id(raw_item_id)[:10], e)
        attachments = []

    rows: List[Dict[str, Any]] = []
    for att in attachments:
        meta = _extract_attachment_metadata(att)
        raw_name = meta.get("name")
        if not raw_name:
            # Skip nameless attachments — we have nothing safe to store.
            continue
        filename_safe = safe_filename(raw_name)
        cached_ref = None
        try:
            att_id = meta.get("attachment_id")
            if att_id not in (None, ""):
                candidate_hashes = {hash_item_id(att_id)}
            else:
                candidate_hashes = {
                    hash_item_id(raw_name),
                    hash_item_id(filename_safe),
                }
            for cached_row in storage.list_attachments(item_id=raw_item_id):
                cached = storage.get_attachment(cached_row.get("attachment_ref")) or {}
                if cached.get("attachment_id_hash") in candidate_hashes:
                    cached_ref = cached.get("attachment_ref")
                    break
        except Exception as e:
            logger.info("attachment cache lookup skipped: %s", e)
        try:
            ref = storage.upsert_attachment({
                "attachment_ref": cached_ref,
                "message_ref": resolved_message_ref,
                "item_id": raw_item_id,
                "attachment_id": meta.get("attachment_id"),
                "filename": filename_safe,
                "content_type": meta.get("content_type"),
                "size": meta.get("size"),
                "synced_at": None,  # storage will fill with _iso_now()
            })
        except ValueError as e:
            logger.info("skip attachment: %s", e)
            continue
        # Public rows: only ref + filename + size + content_type.
        rows.append({
            "attachment_ref": ref,
            "filename": filename_safe,
            "size": meta.get("size"),
            "content_type": meta.get("content_type"),
        })

    return {
        "status": "success",
        "operation": "list_attachments",
        "message_ref": resolved_message_ref,
        "item_id_hash": hash_item_id(raw_item_id),
        "count": len(rows),
        "attachments": rows,
    }


# -----------------------------------------------------------------------------
# download_attachment
# -----------------------------------------------------------------------------
def download_attachment(
    account: Any,
    storage: Any,
    *,
    message_ref: Optional[str] = None,
    item_id: Optional[str] = None,
    attachment_ref: Optional[str] = None,
    attachment_id: Optional[str] = None,
    filename: Optional[str] = None,
    target_dir: str = "rag_data/attachments",
    inbox_lookup: Optional[Callable[[Any, str], Any]] = None,
    archive_lookup: Optional[Callable[[Any, str], Any]] = None,
    file_attachment_type: Optional[type] = None,
) -> Dict[str, Any]:
    """Download a single attachment to disk and update its metadata row.

    Selector precedence (most specific → least):
        1. ``attachment_ref`` (opaque hash from storage)
        2. ``attachment_id`` (raw EWS attachment id)
        3. ``filename`` (plaintext filename match)

    Exactly one of (attachment_ref, attachment_id, filename) should be
    provided; if none are given we raise AttachmentNotFound("no_selector").

    After a successful save:
        * the target directory is created if missing
        * bytes are written to ``<target_dir>/<safe_filename>``
        * an ``attachments`` row is upserted with ``local_path``,
          ``downloaded_at``, and refreshed size/content_type

    Returns a structured dict. Raises:
        AttachmentNotFound    — message / attachment not found (404)
        AttachmentSourceError — EWS fetch raised (500)
    """
    resolved_message_ref: Optional[str] = message_ref
    raw_item_id: Optional[str] = item_id

    if message_ref and not raw_item_id:
        try:
            rec = storage.get_message(message_ref=message_ref)
        except Exception as e:
            raise AttachmentNotFound(message_ref, reason=f"cache_error:{type(e).__name__}")
        if rec is None:
            raise AttachmentNotFound(message_ref, reason="message_ref_not_in_cache")
        raw_item_id = rec.get("item_id")
        if not raw_item_id:
            raise AttachmentNotFound(message_ref, reason="cache_row_missing_item_id")
    elif not raw_item_id:
        raise AttachmentNotFound("<none>", reason="missing_identifier")

    if not attachment_ref and not attachment_id and not filename:
        raise AttachmentNotFound("<none>", reason="no_selector")

    # Cache pre-check when an attachment_ref was supplied — gives a clean 404
    # without paying for an EWS round trip if the row is already gone.
    if attachment_ref and not attachment_id and not filename:
        cached = storage.get_attachment(attachment_ref)
        if cached is None:
            raise AttachmentNotFound(attachment_ref, reason="attachment_ref_unknown")

    item = _resolve_source_item(
        account, raw_item_id, inbox_lookup, archive_lookup
    )
    if item is None:
        raise AttachmentNotFound(raw_item_id, reason="target_not_found")

    # Pull attachments list defensively.
    try:
        attachments = list(item.attachments or [])
    except Exception as e:
        logger.warning("attachments iteration failed for %s: %s",
                       hash_item_id(raw_item_id)[:10], e)
        attachments = []

    # If we have an attachment_ref but no other selector, try to find the
    # matching attachment by attachment_id_hash recorded in the cache row.
    if attachment_ref and not attachment_id and not filename:
        cached = storage.get_attachment(attachment_ref) or {}
        cached_id_hash = cached.get("attachment_id_hash") if cached else None
        match = None
        match_meta = None
        for att in attachments:
            meta = _extract_attachment_metadata(att)
            att_id = meta.get("attachment_id")
            if att_id not in (None, ""):
                candidate_hashes = {hash_item_id(att_id)}
            else:
                raw_name = meta.get("name")
                candidate_hashes = {hash_item_id(raw_name)}
                safe_name = safe_filename(raw_name)
                candidate_hashes.add(hash_item_id(safe_name))
            if cached_id_hash in candidate_hashes:
                match = att
                match_meta = meta
                break
        if match is None:
            raise AttachmentNotFound(attachment_ref, reason="attachment_ref_mismatch")
        att, meta = match, match_meta
    else:
        try:
            att, meta = _select_attachment_from_list(
                attachments,
                attachment_id=attachment_id,
                filename=filename,
            )
        except AttachmentNotFound:
            raise

    # Read bytes (catch transport failures).
    try:
        content = _extract_attachment_content(att)
    except Exception as e:
        logger.warning("attachment content fetch failed: %s", e)
        raise AttachmentSourceError(e)

    # Resolve safe on-disk path and public metadata name. The raw Exchange
    # name is only used above for selector matching; from this point on the
    # filename that leaves the service or enters SQLite is the sanitised
    # basename.
    target_name = meta.get("name") or filename or "attachment"
    filename_safe = safe_filename(target_name)
    abs_dir, full_path = _resolve_target_path(
        target_dir, filename_safe, filename_is_safe=True
    )
    try:
        os.makedirs(abs_dir, exist_ok=True)
    except Exception as e:
        raise AttachmentSourceError(e)
    try:
        with open(full_path, "wb") as f:
            f.write(content)
    except Exception as e:
        raise AttachmentSourceError(e)

    # Upsert metadata row.
    from datetime import datetime, timezone
    downloaded_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    try:
        ref = storage.upsert_attachment({
            "attachment_ref": attachment_ref,
            "message_ref": resolved_message_ref,
            "item_id": raw_item_id,
            "attachment_id": meta.get("attachment_id"),
            "filename": filename_safe,
            "content_type": meta.get("content_type"),
            "size": len(content),
            "local_path": full_path,
            "downloaded_at": downloaded_at,
            "synced_at": downloaded_at,
        })
    except ValueError as e:
        raise AttachmentSourceError(e)

    return {
        "status": "success",
        "operation": "download_attachment",
        "message_ref": resolved_message_ref,
        "attachment_ref": ref,
        "filename": filename_safe,
        "size": len(content),
        "content_type": meta.get("content_type"),
        "local_path": full_path,
    }

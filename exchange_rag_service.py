"""
exchange_rag_service.py — local-file RAG indexing foundation (W2d).

This module wraps safe filesystem walks over a configured ``root_dir``
(typically ``rag_data/``) and indexes text-like documents into the
exchange_storage.rag_documents table. The service:

    * Never touches Exchange — operates only on files already downloaded
      by the legacy ``/download_dataset`` flow.
    * Resolves ``root_dir`` to a realpath and refuses to walk outside it.
    * Skips symlinks that resolve outside root (no follow-symlink
      traversal).
    * Caps per-file reads at ``max_file_bytes``; never loads huge files
      fully.
    * Computes sha256 over the *bounded* read; large files are skipped
      deterministically when over the cap.
    * Stores a short ``text_excerpt`` (default ≤4000 chars) so search
      can hit on the body without leaking full documents.

Public API:
    index_rag_documents(storage, root_dir='rag_data',
                         limit=1000, max_file_bytes=2_000_000,
                         extensions=None, force=False) -> dict
    search_rag_documents(storage, query, limit=20,
                         root_dir=None) -> dict
    get_rag_document(storage, doc_ref, include_text=False,
                     max_chars=20000,
                     root_dir_fallback='rag_data') -> dict

Design constraints:
    * stdlib only (os, hashlib, mimetypes, datetime). No FastAPI/
      exchangelib imports — safe to run in tests.
    * All public dicts expose only ``relative_path`` (under root), not
      the absolute disk path. The ``text`` key is omitted unless
      ``include_text=True`` and the file is safe to read.
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from exchange_storage import make_rag_doc_ref  # noqa: F401  (re-export)

logger = logging.getLogger("exchange_rag_service")

# Default text-like extensions for W2d. Binary attachments can be added
# later, but for W2d we keep indexing bounded to text-readable files so
# the corpus stays inspectable.
DEFAULT_TEXT_EXTENSIONS = (
    ".txt", ".md", ".eml", ".json", ".csv", ".log",
)

# Hard upper bounds — defence in depth so callers can't OOM the process.
MAX_FILE_BYTES_HARD_CAP = 50 * 1024 * 1024     # 50 MB
MAX_TEXT_EXCERPT_CHARS = 4000                  # ~4 KB in SQLite
MAX_INCLUDE_TEXT_CHARS = 200_000               # /get_rag_document body cap
DEFAULT_MAX_FILE_BYTES = 2_000_000             # 2 MB


# -----------------------------------------------------------------------------
# Public exceptions — typed errors main.py can map to HTTP status codes.
# -----------------------------------------------------------------------------
class RagRootMissing(Exception):
    """The configured root_dir does not exist / is not a directory."""


class RagRootUnsafe(Exception):
    """The configured root_dir failed a safety check (e.g. inside-out)."""


class RagDocNotFound(Exception):
    """The requested doc_ref is unknown to the index."""


# -----------------------------------------------------------------------------
# Path safety — resolve and verify every candidate stays inside root.
# -----------------------------------------------------------------------------
def _resolve_root(root_dir: str) -> str:
    """Resolve ``root_dir`` to a realpath. Caller is responsible for
    ensuring root_dir is sane (no path-traversal inputs). Returns the
    resolved absolute path. Raises RagRootMissing if absent, RagRootUnsafe
    if it cannot be resolved.
    """
    if not root_dir or not isinstance(root_dir, str):
        raise RagRootUnsafe("root_dir is empty")
    try:
        return os.path.realpath(root_dir)
    except Exception as e:
        raise RagRootUnsafe(f"cannot resolve root_dir: {e}")


def _is_inside(child: str, parent: str) -> bool:
    """True iff ``child`` (resolved) is the same as or under ``parent``.

    Pure string check on realpath outputs — avoids edge cases with
    Path.resolve() on files that don't exist yet.
    """
    if not child or not parent:
        return False
    if child == parent:
        return True
    return child.startswith(parent + os.sep)


def _safe_relative_path(root_resolved: str, candidate: str) -> Optional[str]:
    """Compute the relative POSIX path of candidate under root, or None.

    Uses realpath of the candidate (so symlinks are resolved) — if the
    real path escapes root we return None. Callers should ``continue``.
    """
    try:
        # os.path.realpath follows symlinks; safe for both files and dirs
        # as long as the path exists.
        real = os.path.realpath(candidate)
    except Exception:
        return None
    if not _is_inside(real, root_resolved):
        return None
    try:
        rel = os.path.relpath(real, root_resolved)
    except Exception:
        return None
    if rel.startswith("..") or os.path.isabs(rel):
        return None
    return rel.replace("\\", "/")


# -----------------------------------------------------------------------------
# Content / metadata extraction.
# -----------------------------------------------------------------------------
def _detect_content_type(filename: str, explicit: Optional[str] = None) -> Optional[str]:
    """Best-effort MIME-type guess. Never raises."""
    if explicit:
        return explicit
    if not filename:
        return None
    ctype, _ = mimetypes.guess_type(filename)
    return ctype


def _safe_read_text(
    abs_path: str,
    *,
    max_bytes: int,
) -> Tuple[Optional[bytes], bool]:
    """Read up to ``max_bytes`` from ``abs_path``. Return (bytes, truncated).

    Returns ``(None, False)`` on error. If the file is larger than the cap
    we still return the cap's worth of bytes (so the caller can hash /
    excerpt something deterministic) and ``truncated=True`` so the caller
    can decide whether to skip or store a placeholder.
    """
    try:
        # Open in binary mode so we never crash on weird encoding bytes.
        with open(abs_path, "rb") as f:
            data = f.read(int(max(1, max_bytes)))
        truncated = os.path.getsize(abs_path) > len(data)
        return data, truncated
    except Exception as e:
        logger.info("rag: read failed for %s: %s", abs_path, e)
        return None, False


def _decode_excerpt(raw: bytes, max_chars: int) -> str:
    """Decode bytes → text with a forgiving fallback.

    Strips NUL bytes (some Outlook exports include them) and replaces
    other undecodable sequences with the unicode replacement char.
    """
    if not raw:
        return ""
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        try:
            text = raw.decode("latin-1", errors="replace")
        except Exception:
            return ""
    text = text.replace("\x00", "")
    if len(text) > max_chars:
        return text[:max_chars]
    return text


# -----------------------------------------------------------------------------
# index_rag_documents — the main entry point.
# -----------------------------------------------------------------------------
def index_rag_documents(
    storage: Any,
    *,
    root_dir: str = "rag_data",
    limit: int = 1000,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    extensions: Optional[List[str]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Walk ``root_dir`` and upsert every text-like file into the index.

    Args:
        storage:           ExchangeStorage (or duck-typed equivalent).
        root_dir:          Local directory to walk. Default ``rag_data``.
                           Caller-supplied; tests should pass a tmp dir.
        limit:             Hard cap on the number of files indexed per call.
        max_file_bytes:    Per-file read cap. Files whose on-disk size
                           exceeds this are *skipped* (counted in
                           ``skipped``) — we never partially index them
                           under W2d, because ``sha256`` would otherwise
                           be misleading.
        extensions:        Override the default text-like extension list.
        force:             If True, reindex all matching files even if
                           a row with the same doc_ref exists. Default
                           False — we re-read and re-upsert but skip
                           no-op work via the ON CONFLICT clause.

    Returns a structured dict:
        {
          "status": "success" | "error",
          "root_dir": "...",        # resolved absolute path
          "indexed":  N,             # rows upserted
          "skipped":  N,             # files skipped (size, bad path, etc.)
          "errors":   N,
          "errors_detail": [...],    # optional structured errors
          "limit":    N,
          "max_file_bytes": N,
          "extensions": [...]
        }
    """
    # Validate inputs.
    try:
        limit_i = max(1, min(int(limit), 100_000))
    except Exception:
        limit_i = 1000
    try:
        max_bytes = max(1, min(int(max_file_bytes), MAX_FILE_BYTES_HARD_CAP))
    except Exception:
        max_bytes = DEFAULT_MAX_FILE_BYTES

    if extensions is not None:
        exts = tuple(
            e.strip().lower() if e else "" for e in extensions
        )
        exts = tuple(e for e in exts if e)
        if not exts:
            exts = DEFAULT_TEXT_EXTENSIONS
    else:
        exts = DEFAULT_TEXT_EXTENSIONS

    # Resolve root.
    try:
        root_resolved = _resolve_root(root_dir)
    except (RagRootMissing, RagRootUnsafe) as e:
        return {
            "status": "error",
            "root_dir": root_dir,
            "indexed": 0,
            "skipped": 0,
            "errors": 1,
            "errors_detail": [{"path": root_dir, "reason": str(e)}],
            "limit": limit_i,
            "max_file_bytes": max_bytes,
            "extensions": list(exts),
        }
    if not os.path.isdir(root_resolved):
        return {
            "status": "error",
            "root_dir": root_resolved,
            "indexed": 0,
            "skipped": 0,
            "errors": 1,
            "errors_detail": [{"path": root_resolved, "reason": "not_a_directory"}],
            "limit": limit_i,
            "max_file_bytes": max_bytes,
            "extensions": list(exts),
        }

    indexed = 0
    skipped = 0
    errors_list: List[Dict[str, Any]] = []

    # Walk. We do NOT follow symlinks (followlinks=False) and we
    # explicitly re-check each entry below in case a symlink slipped
    # in via a path the os.walk layer didn't catch (it does, but we
    # double-check).
    for dirpath, dirnames, filenames in os.walk(
        root_resolved, followlinks=False
    ):
        # Filter out symlinked dirs that escape root by re-checking
        # each directory entry.
        safe_dirs = []
        for d in list(dirnames):
            candidate = os.path.join(dirpath, d)
            rel = _safe_relative_path(root_resolved, candidate)
            if rel is None:
                # Directory is a symlink that escapes root — drop it
                # from the walk so os.walk does not descend into it.
                dirnames.remove(d)
                skipped += 1
            else:
                safe_dirs.append(d)
        # Iterate files (skip symlinks — see filter below).
        for fn in filenames:
            if indexed >= limit_i:
                break
            abs_path = os.path.join(dirpath, fn)
            # Reject symlinks entirely (we only index regular files).
            try:
                if os.path.islink(abs_path):
                    skipped += 1
                    continue
            except Exception:
                pass

            # Extension filter — count non-matching files in ``skipped``
            # so callers see total unindexed count, not just errors.
            ext_lower = os.path.splitext(fn)[1].lower()
            if ext_lower not in exts:
                skipped += 1
                continue

            # Path safety: even with followlinks=False on os.walk, the
            # entry was returned; the realpath check below keeps us safe
            # against symlinked parents that the walker may have
            # descended into before our dirnames filter ran.
            rel = _safe_relative_path(root_resolved, abs_path)
            if rel is None:
                skipped += 1
                continue

            try:
                size = os.path.getsize(abs_path)
            except Exception as e:
                errors_list.append(
                    {"path": rel, "reason": f"stat_failed:{type(e).__name__}"}
                )
                continue

            # Hard skip on oversize files (we promised no partial
            # indexing under W2d — would give misleading sha256).
            if size > max_bytes:
                skipped += 1
                continue

            raw, _truncated = _safe_read_text(abs_path, max_bytes=max_bytes)
            if raw is None:
                errors_list.append(
                    {"path": rel, "reason": "read_failed"}
                )
                continue

            sha256_hex = hashlib.sha256(raw).hexdigest()
            excerpt = _decode_excerpt(raw, MAX_TEXT_EXCERPT_CHARS)
            content_type = _detect_content_type(fn)
            try:
                mtime_ts = os.path.getmtime(abs_path)
                mtime_iso = (
                    datetime.fromtimestamp(mtime_ts, tz=timezone.utc)
                    .replace(microsecond=0)
                    .isoformat()
                )
            except Exception:
                mtime_iso = None

            try:
                storage.upsert_rag_document({
                    "root_dir": root_resolved,
                    "relative_path": rel,
                    "filename": fn,
                    "content_type": content_type,
                    "size": size,
                    "mtime": mtime_iso,
                    "sha256": sha256_hex,
                    "text_excerpt": excerpt,
                    "indexed_at": None,  # storage fills with _iso_now()
                })
                indexed += 1
            except Exception as e:
                errors_list.append(
                    {"path": rel, "reason": f"upsert_failed:{type(e).__name__}:{e}"}
                )

        if indexed >= limit_i:
            break

    return {
        "status": "success",
        "root_dir": root_resolved,
        "indexed": indexed,
        "skipped": skipped,
        "errors": len(errors_list),
        "errors_detail": errors_list,
        "limit": limit_i,
        "max_file_bytes": max_bytes,
        "extensions": list(exts),
        "force": bool(force),
    }


# -----------------------------------------------------------------------------
# search_rag_documents — thin wrapper over ExchangeStorage.search_rag_documents.
# -----------------------------------------------------------------------------
def search_rag_documents(
    storage: Any,
    query: str,
    *,
    limit: int = 20,
    root_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Search the index by substring match on filename + excerpt.

    Returns:
        {
          "status":   "success",
          "query":    "...",
          "root_dir": "..." (optional, after resolution),
          "limit":    N,
          "count":    N,
          "documents": [<row>, ...]
        }
    """
    q = (query or "").strip()
    if not q:
        return {
            "status": "error",
            "reason": "empty_query",
            "query": q,
            "count": 0,
            "documents": [],
        }
    try:
        limit_i = max(1, min(int(limit), 1000))
    except Exception:
        limit_i = 20

    root_resolved: Optional[str] = None
    if root_dir:
        try:
            root_resolved = _resolve_root(root_dir)
        except (RagRootMissing, RagRootUnsafe) as e:
            return {
                "status": "error",
                "reason": f"bad_root_dir:{e}",
                "query": q,
                "count": 0,
                "documents": [],
            }

    rows = storage.search_rag_documents(
        query=q,
        limit=limit_i,
        root_dir=root_resolved,
    )
    return {
        "status": "success",
        "query": q,
        "root_dir": root_resolved,
        "limit": limit_i,
        "count": len(rows),
        "documents": rows,
    }


# -----------------------------------------------------------------------------
# get_rag_document — fetch one row, optionally with body text from disk.
# -----------------------------------------------------------------------------
def get_rag_document(
    storage: Any,
    doc_ref: str,
    *,
    include_text: bool = False,
    max_chars: int = 20000,
    root_dir_fallback: str = "rag_data",
) -> Dict[str, Any]:
    """Fetch one RAG document row by doc_ref.

    With ``include_text=True`` we additionally read the file body from
    disk (capped at ``max_chars``) and include a ``text`` field in the
    result. The on-disk read is path-safe: the resolved file MUST be
    inside the indexed ``root_dir`` from the row — otherwise we refuse
    to return its bytes.

    Raises:
        RagDocNotFound if the doc_ref is unknown.
        RagRootUnsafe if the file resolves outside its root_dir.
    """
    if not doc_ref:
        raise RagDocNotFound("<empty>")
    row = storage.get_rag_document(doc_ref)
    if row is None:
        raise RagDocNotFound(doc_ref)

    out: Dict[str, Any] = {
        "status": "success",
        "doc_ref": row["doc_ref"],
        "document": row,
    }
    if include_text:
        root = row.get("root_dir") or root_dir_fallback
        rel = row.get("relative_path") or ""
        # Compose the path and immediately enforce that the realpath
        # is inside the indexed root.
        candidate = os.path.join(root, rel)
        try:
            abs_root = os.path.realpath(root)
            abs_full = os.path.realpath(candidate)
        except Exception as e:
            raise RagRootUnsafe(f"cannot resolve path: {e}")
        if not _is_inside(abs_full, abs_root):
            raise RagRootUnsafe("doc path escaped root_dir")
        # Cap and read.
        cap = max(1, min(int(max_chars), MAX_INCLUDE_TEXT_CHARS))
        raw, _ = _safe_read_text(abs_full, max_bytes=cap)
        if raw is None:
            out["text"] = None
            out["text_truncated"] = False
            out["text_chars"] = 0
        else:
            text = _decode_excerpt(raw, cap)
            out["text"] = text
            out["text_chars"] = len(text)
            try:
                out["text_truncated"] = os.path.getsize(abs_full) > len(raw)
            except Exception:
                out["text_truncated"] = False
    return out

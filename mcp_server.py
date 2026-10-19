"""
mcp_server.py — Exchange connector exposed as MCP tools over stdio (W3).

This is the **transport layer** for W3. It is intentionally thin: every
tool wraps a single HTTP request to the existing FastAPI service on
``http://127.0.0.1:5015`` so all Exchange business logic, storage, audit,
attachment, and RAG behaviour stays exactly as accepted in W1/W2.

Hard constraints (per W3 task brief):
    * No real email is sent from here.
    * No real Exchange connection from here.
    * This file does not touch the running uvicorn/systemd service.
    * The server is stdlib + the official ``mcp`` SDK only.

Base URL: env ``EXCHANGE_CONNECTOR_BASE_URL`` (default ``http://127.0.0.1:5015``).
Timeout: env ``EXCHANGE_CONNECTOR_MCP_TIMEOUT`` (default 120s).

Run as a stdio MCP server (the default transport for ``mcp.run()``)::

    EXCHANGE_CONNECTOR_BASE_URL=http://127.0.0.1:5015 \\
        ./.venv/bin/python mcp_server.py
"""

import json
import logging
import os
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

# NOTE: ``from __future__ import annotations`` is intentionally NOT
# used here. FastMCP's ``Tool.from_function`` inspects parameters via
# ``inspect.signature(fn)`` (without ``eval_str=True``) and then runs
# ``issubclass(param.annotation, Context)``. With PEP-563 forward refs,
# ``param.annotation`` is the string ``"Optional[str]"`` which makes
# ``issubclass`` raise ``TypeError``. Keeping annotations as real
# objects at function definition time means FastMCP sees
# ``typing.Optional[str]`` (a ``_UnionGenericAlias``) and its
# ``get_origin(...) is not None`` guard correctly skips it.

# The official MCP SDK is an *optional* import. We always import it
# here because the task brief explicitly authorises installing it and
# the stdio smoke verifier depends on it.
try:
    from mcp.server.fastmcp import FastMCP  # type: ignore
except Exception as exc:  # pragma: no cover — see tests
    _MCP_IMPORT_ERROR: Optional[BaseException] = exc
    FastMCP = None  # type: ignore
else:
    _MCP_IMPORT_ERROR = None


logger = logging.getLogger("exchange_mcp_server")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_BASE_URL = "http://127.0.0.1:5015"
DEFAULT_TIMEOUT = 120  # seconds


def _inside_docker() -> bool:
    """Best-effort runtime check for Docker/container execution.

    Host Hermes should use 127.0.0.1. Dockerized bots need the host gateway
    instead, because their 127.0.0.1 is the container itself.
    """
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8", errors="ignore") as f:
            data = f.read().lower()
        return any(token in data for token in ("docker", "containerd", "kubepods"))
    except Exception:
        return False


def _docker_host_alias() -> str:
    """Return a host address usable from a Linux Docker container.

    Docker Desktop / containers with extra_hosts can resolve
    host.docker.internal. Current OpenClaw bot containers mostly do not, but
    172.17.0.1 was verified from openclaw-bot* containers on this host.
    """
    try:
        socket.gethostbyname("host.docker.internal")
        return "host.docker.internal"
    except Exception:
        return "172.17.0.1"


def _default_base_url() -> str:
    if _inside_docker():
        return f"http://{_docker_host_alias()}:5015"
    return DEFAULT_BASE_URL


def _base_url() -> str:
    """Resolve the upstream REST base URL.

    Order of precedence:
        1. ``EXCHANGE_CONNECTOR_BASE_URL`` env var
        2. Docker host gateway when running inside a container
        3. ``DEFAULT_BASE_URL`` (``http://127.0.0.1:5015``)
    Trailing slash is stripped so URL joining is deterministic.
    """
    raw = os.environ.get("EXCHANGE_CONNECTOR_BASE_URL")
    if raw and raw.strip():
        return raw.strip().rstrip("/")
    return _default_base_url()


def _timeout() -> float:
    """Resolve the HTTP timeout for the upstream REST service.

    Order:
        1. ``EXCHANGE_CONNECTOR_MCP_TIMEOUT`` env var
        2. ``DEFAULT_TIMEOUT`` (120s)
    A non-positive value falls back to the default — we never want a
    zero/negative timeout that would fail every request instantly.
    """
    try:
        val = float(os.environ.get("EXCHANGE_CONNECTOR_MCP_TIMEOUT", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        return float(DEFAULT_TIMEOUT)
    if val <= 0:
        return float(DEFAULT_TIMEOUT)
    return val


# ---------------------------------------------------------------------------
# HTTP helper — stdlib-only urllib
# ---------------------------------------------------------------------------
class McpHttpError(Exception):
    """Structured error returned to MCP tools for non-2xx HTTP responses."""

    def __init__(self, status: int, path: str, body: Any, message: str = ""):
        self.status = status
        self.path = path
        self.body = body
        self.message = message or f"HTTP {status} from {path}"
        super().__init__(self.message)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": "http_error",
            "status": self.status,
            "path": self.path,
            "message": self.message,
            "body": self.body,
        }


def _http_request(
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    timeout: Optional[float] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Issue a single HTTP request against the upstream REST service.

    Returns the parsed JSON body as a ``dict`` on success. On non-2xx
    responses or transport errors, returns a structured error dict
    (``error``/``status``/``path``/``message``/``body``) — never raises
    back into the MCP tool layer, never exposes a Python traceback.

    Parameters are kwargs-only for ``timeout`` and ``base_url`` so tests
    can monkeypatch without touching ``_base_url()`` / ``_timeout()``
    global state.
    """
    base = (base_url or _base_url()).rstrip("/") or DEFAULT_BASE_URL
    if not path.startswith("/"):
        path = "/" + path
    url = base + path
    method_u = method.upper()
    body_bytes: Optional[bytes] = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method_u)

    try:
        with urllib.request.urlopen(req, timeout=timeout or _timeout()) as resp:
            raw = resp.read()
            status = getattr(resp, "status", 200) or 200
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read() or b""
        except Exception:
            raw = b""
        body = _safe_json_loads(raw)
        return McpHttpError(
            status=int(getattr(exc, "code", 0) or 0),
            path=path,
            body=body,
            message=f"HTTP {getattr(exc, 'code', '?')} from {path}",
        ).to_dict()
    except urllib.error.URLError as exc:
        return McpHttpError(
            status=0,
            path=path,
            body=None,
            message=f"URLError: {getattr(exc, 'reason', exc)}",
        ).to_dict()
    except Exception as exc:  # pragma: no cover — defensive
        return McpHttpError(
            status=0,
            path=path,
            body=None,
            message=f"{type(exc).__name__}: {exc}",
        ).to_dict()

    body = _safe_json_loads(raw)
    if status < 200 or status >= 300:
        return McpHttpError(
            status=status, path=path, body=body,
            message=f"HTTP {status} from {path}",
        ).to_dict()
    if body is None:
        return {"status": "raw_non_json", "raw": _safe_decode(raw), "path": path}
    return body


def _safe_json_loads(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return {"_decode_error": True, "raw": _safe_decode(raw)}


def _safe_decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return repr(raw)


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------
def _build_mcp() -> Any:
    """Instantiate the FastMCP server.

    Raises ``RuntimeError`` if the SDK is not importable so the failure
    mode is loud instead of silently producing an object with no tools.
    """
    if FastMCP is None:
        raise RuntimeError(
            f"mcp SDK is not importable: {_MCP_IMPORT_ERROR!r}. "
            "Run '.venv/bin/python -m pip install \"mcp>=1.0\"'."
        )
    return FastMCP("exchange-connector")


# Build lazily so that ``import mcp_server`` does not fail when the SDK
# is missing — only ``mcp.run()``/``main()`` raises. The smoke verifier
# patches ``mcp_server._http_request`` and never calls ``mcp.run()``, so
# this lazy build keeps import cheap and tests happy.
_mcp_instance: Any = None


def _mcp() -> Any:
    global _mcp_instance
    if _mcp_instance is None:
        _mcp_instance = _build_mcp()
    return _mcp_instance


# Tool registration uses the SDK's ``@mcp.tool()`` decorator. We wrap
# each tool in a helper that:
#   * builds the request payload from kwargs (drops ``None`` values);
#   * delegates the HTTP call to ``_http_request`` (monkeypatchable);
#   * returns the structured dict straight through.

def _clean(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Strip keys whose value is ``None`` so the wire payload stays clean."""
    return {k: v for k, v in payload.items() if v is not None}


# ---- Mail / readiness -----------------------------------------------------
def work_mail_health() -> Dict[str, Any]:
    """GET /health — liveness probe. Always cheap; never touches VPN."""
    return _http_request("GET", "/health")


def work_mail_ready() -> Dict[str, Any]:
    """GET /ready — readiness probe (VPN/DNS/route/account)."""
    return _http_request("GET", "/ready")


def work_mail_list_messages(
    folder: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    received_after: Optional[str] = None,
    received_before: Optional[str] = None,
    refresh: bool = False,
) -> Dict[str, Any]:
    """POST /list_messages — paginated list of cached message metadata."""
    return _http_request("POST", "/list_messages", _clean({
        "folder": folder,
        "limit": limit,
        "offset": offset,
        "received_after": received_after,
        "received_before": received_before,
        "refresh": refresh,
    }))


def work_mail_search_messages(
    query: str,
    folder: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    received_after: Optional[str] = None,
    received_before: Optional[str] = None,
) -> Dict[str, Any]:
    """POST /search_messages — substring search over cached messages."""
    payload = {"query": query, "limit": limit, "offset": offset}
    if folder is not None:
        payload["folder"] = folder
    if received_after is not None:
        payload["received_after"] = received_after
    if received_before is not None:
        payload["received_before"] = received_before
    return _http_request("POST", "/search_messages", payload)


def work_mail_get_message(
    message_ref: Optional[str] = None,
    item_id: Optional[str] = None,
) -> Dict[str, Any]:
    """POST /get_message — fetch a single message by ref or raw item id."""
    return _http_request("POST", "/get_message", _clean({
        "message_ref": message_ref,
        "item_id": item_id,
    }))


# ---- Destructive mail -----------------------------------------------------
def work_mail_send_message(
    to: List[str],
    subject: str,
    body: str,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """POST /send_message — send a new email.

    NOTE: This delegates to the upstream REST service, which itself
    enforces the VPN guard. W3 does not bypass that guard.
    """
    payload: Dict[str, Any] = {"to": list(to), "subject": subject, "body": body}
    if cc:
        payload["cc"] = list(cc)
    if bcc:
        payload["bcc"] = list(bcc)
    return _http_request("POST", "/send_message", payload)


def work_mail_reply_message(
    message_ref: Optional[str] = None,
    item_id: Optional[str] = None,
    body: str = "",
    subject: Optional[str] = None,
) -> Dict[str, Any]:
    """POST /reply_message — reply to an existing message."""
    payload: Dict[str, Any] = {"body": body}
    if message_ref is not None:
        payload["message_ref"] = message_ref
    if item_id is not None:
        payload["item_id"] = item_id
    if subject is not None:
        payload["subject"] = subject
    return _http_request("POST", "/reply_message", payload)


# ---- Attachments ----------------------------------------------------------
def work_mail_list_attachments(
    message_ref: Optional[str] = None,
    item_id: Optional[str] = None,
) -> Dict[str, Any]:
    """POST /list_attachments — list attachments of a message."""
    return _http_request("POST", "/list_attachments", _clean({
        "message_ref": message_ref,
        "item_id": item_id,
    }))


def work_mail_download_attachment(
    message_ref: Optional[str] = None,
    item_id: Optional[str] = None,
    attachment_ref: Optional[str] = None,
    attachment_id: Optional[str] = None,
    filename: Optional[str] = None,
    target_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """POST /download_attachment — download one attachment to local fs."""
    return _http_request("POST", "/download_attachment", _clean({
        "message_ref": message_ref,
        "item_id": item_id,
        "attachment_ref": attachment_ref,
        "attachment_id": attachment_id,
        "filename": filename,
        "target_dir": target_dir,
    }))


# ---- RAG / local index ----------------------------------------------------
def work_rag_index_documents(
    root_dir: str = "rag_data",
    limit: int = 1000,
    max_file_bytes: int = 2_000_000,
    extensions: Optional[List[str]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """POST /index_rag_documents — walk ``root_dir`` and index text files."""
    payload: Dict[str, Any] = {
        "root_dir": root_dir,
        "limit": limit,
        "max_file_bytes": max_file_bytes,
        "force": force,
    }
    if extensions:
        payload["extensions"] = list(extensions)
    return _http_request("POST", "/index_rag_documents", payload)


def work_rag_search_documents(
    query: str,
    limit: int = 20,
    root_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """POST /search_rag_documents — substring search over indexed files."""
    payload: Dict[str, Any] = {"query": query, "limit": limit}
    if root_dir:
        payload["root_dir"] = root_dir
    return _http_request("POST", "/search_rag_documents", payload)


def work_rag_get_document(
    doc_ref: str,
    include_text: bool = False,
    max_chars: int = 20000,
) -> Dict[str, Any]:
    """POST /get_rag_document — fetch indexed doc by opaque ref."""
    return _http_request("POST", "/get_rag_document", {
        "doc_ref": doc_ref,
        "include_text": include_text,
        "max_chars": max_chars,
    })


# ---- Legacy / ops ---------------------------------------------------------
def work_mail_list_folders(target: str = "archive") -> Dict[str, Any]:
    """POST /list_folders — folder tree (legacy MCP-shaped wrapper)."""
    return _http_request("POST", "/list_folders", {"target": target})


def work_mail_download_status() -> Dict[str, Any]:
    """GET /download_status — current background download task state."""
    return _http_request("GET", "/download_status")


def work_mail_start_dataset_download(
    limit_per_folder: int = 100,
    folder: str = "archive_inbox",
    download_attachments: bool = True,
    extract_archives: bool = True,
    recursive: bool = True,
) -> Dict[str, Any]:
    """POST /download_dataset — HEAVY background dataset download.

    Marked separately from the rest of the tool surface because it
    triggers a long-running background task. Agents should opt in
    explicitly (e.g. via a config flag) rather than call it by default.
    """
    return _http_request("POST", "/download_dataset", {
        "limit_per_folder": limit_per_folder,
        "folder": folder,
        "download_attachments": download_attachments,
        "extract_archives": extract_archives,
        "recursive": recursive,
    })


# ---- Calendar wrappers (legacy but useful) -------------------------------
def work_calendar_get(date: str = "today") -> Dict[str, Any]:
    """POST /get_calendar — calendar overview for a given date."""
    return _http_request("POST", "/get_calendar", {"date": date})


def work_calendar_search(query: str, days: int = 30) -> Dict[str, Any]:
    """POST /search_calendar — calendar search by subject/location."""
    return _http_request("POST", "/search_calendar", {"query": query, "days": days})


def work_calendar_add(
    subject: str,
    start_time: str,
    end_time: str,
    body: str = "",
    location: str = "",
    attendees: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """POST /add_event — create a calendar event."""
    payload: Dict[str, Any] = {
        "subject": subject,
        "start_time": start_time,
        "end_time": end_time,
        "body": body,
        "location": location,
    }
    if attendees:
        payload["attendees"] = list(attendees)
    return _http_request("POST", "/add_event", payload)


def work_calendar_delete(subject: str) -> Dict[str, Any]:
    """POST /delete_event — delete event(s) matching subject."""
    return _http_request("POST", "/delete_event", {"subject": subject})


def work_calendar_events(
    start: str = "",
    end: str = "",
    days_back: int = 7,
    days_forward: int = 60,
) -> Dict[str, Any]:
    """POST /calendar_events_json — structured events for calendar sync.

    Returns expanded occurrences with ``uid``/``recurrence_id`` so external
    calendars (Google) can build an idempotent mapping. Read-only.
    """
    payload: Dict[str, Any] = {
        "days_back": days_back,
        "days_forward": days_forward,
    }
    if start:
        payload["start"] = start
    if end:
        payload["end"] = end
    return _http_request("POST", "/calendar_events_json", payload)


# ---------------------------------------------------------------------------
# Tool registration — single source of truth for the exposed tool list.
# ---------------------------------------------------------------------------
# Keep this ordered so smoke tests can assert exact tool names.
TOOL_REGISTRY: List[Dict[str, str]] = [
    {"name": "work_mail_health", "impl": "work_mail_health", "kind": "tool"},
    {"name": "work_mail_ready", "impl": "work_mail_ready", "kind": "tool"},
    {"name": "work_mail_list_messages", "impl": "work_mail_list_messages", "kind": "tool"},
    {"name": "work_mail_search_messages", "impl": "work_mail_search_messages", "kind": "tool"},
    {"name": "work_mail_get_message", "impl": "work_mail_get_message", "kind": "tool"},
    {"name": "work_mail_send_message", "impl": "work_mail_send_message", "kind": "tool"},
    {"name": "work_mail_reply_message", "impl": "work_mail_reply_message", "kind": "tool"},
    {"name": "work_mail_list_attachments", "impl": "work_mail_list_attachments", "kind": "tool"},
    {"name": "work_mail_download_attachment", "impl": "work_mail_download_attachment", "kind": "tool"},
    {"name": "work_rag_index_documents", "impl": "work_rag_index_documents", "kind": "tool"},
    {"name": "work_rag_search_documents", "impl": "work_rag_search_documents", "kind": "tool"},
    {"name": "work_rag_get_document", "impl": "work_rag_get_document", "kind": "tool"},
    {"name": "work_mail_list_folders", "impl": "work_mail_list_folders", "kind": "tool"},
    {"name": "work_mail_download_status", "impl": "work_mail_download_status", "kind": "tool"},
    {"name": "work_mail_start_dataset_download", "impl": "work_mail_start_dataset_download", "kind": "tool"},
    {"name": "work_calendar_get", "impl": "work_calendar_get", "kind": "tool"},
    {"name": "work_calendar_search", "impl": "work_calendar_search", "kind": "tool"},
    {"name": "work_calendar_add", "impl": "work_calendar_add", "kind": "tool"},
    {"name": "work_calendar_delete", "impl": "work_calendar_delete", "kind": "tool"},
    {"name": "work_calendar_events", "impl": "work_calendar_events", "kind": "tool"},
]


def _register_tools(server: Any) -> None:
    """Bind every entry in ``TOOL_REGISTRY`` to the FastMCP server."""
    for entry in TOOL_REGISTRY:
        impl_name = entry["impl"]
        impl = globals()[impl_name]
        # ``@mcp.tool()`` registers the function as a tool. The wrapper
        # functions above are independent of the SDK and unit-tested
        # directly; the decorator simply attaches them to the server.
        server.tool()(impl)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Run the MCP stdio server.

    ``FastMCP.run()`` dispatches to stdio by default (``transport="stdio"``)
    which is exactly what the W3 smoke verifier expects.
    """
    server = _mcp()
    _register_tools(server)
    server.run()


if __name__ == "__main__":
    main()

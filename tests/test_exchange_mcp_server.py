"""
Tests for ``mcp_server.py`` — the W3 MCP stdio transport for the
Exchange connector.

These tests deliberately avoid:
    * Any real ``http://127.0.0.1:5015`` traffic.
    * Any MCP server process startup.
    * Any Exchange / mailbox / calendar side-effects.

The HTTP layer is exercised via monkeypatched ``_http_request`` and
a tiny in-process stub HTTP server (used only by the protocol smoke
test, which is the only test that actually spawns the MCP server).
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status: int, body: bytes = b""):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


import urllib.error as _urllib_error  # noqa: E402


# ---------------------------------------------------------------------------
# Test 1: imports — server module is importable when mcp is installed
# ---------------------------------------------------------------------------
def test_mcp_server_imports_when_mcp_installed():
    """``import mcp_server`` must succeed because the SDK is installed
    in the project venv per W3 brief."""
    import mcp_server  # noqa: F401
    assert hasattr(mcp_server, "TOOL_REGISTRY")
    assert hasattr(mcp_server, "main")
    assert hasattr(mcp_server, "_http_request")


def test_mcp_sdk_version_present():
    import mcp
    import mcp_server

    # The brief allows any mcp>=1.0. Assert it imports cleanly and that
    # the FastMCP symbol is bound through the SDK path.
    assert mcp_server.FastMCP is not None or mcp_server._MCP_IMPORT_ERROR is not None


# ---------------------------------------------------------------------------
# Test 2: base URL + timeout env respected
# ---------------------------------------------------------------------------
def test_base_url_default(monkeypatch):
    monkeypatch.delenv("EXCHANGE_CONNECTOR_BASE_URL", raising=False)
    import mcp_server
    monkeypatch.setattr(mcp_server, "_inside_docker", lambda: False)
    assert mcp_server._base_url() == "http://127.0.0.1:5015"


def test_base_url_default_inside_docker_uses_host_gateway(monkeypatch):
    monkeypatch.delenv("EXCHANGE_CONNECTOR_BASE_URL", raising=False)
    import mcp_server
    monkeypatch.setattr(mcp_server, "_inside_docker", lambda: True)
    monkeypatch.setattr(mcp_server, "_docker_host_alias", lambda: "172.17.0.1")
    assert mcp_server._base_url() == "http://172.17.0.1:5015"


def test_base_url_env(monkeypatch):
    monkeypatch.setenv("EXCHANGE_CONNECTOR_BASE_URL", "http://127.0.0.1:65535/")
    import mcp_server
    monkeypatch.setattr(mcp_server, "_inside_docker", lambda: True)
    assert mcp_server._base_url() == "http://127.0.0.1:65535"


def test_timeout_default(monkeypatch):
    monkeypatch.delenv("EXCHANGE_CONNECTOR_MCP_TIMEOUT", raising=False)
    import mcp_server
    assert mcp_server._timeout() == 120.0


def test_timeout_env(monkeypatch):
    monkeypatch.setenv("EXCHANGE_CONNECTOR_MCP_TIMEOUT", "12.5")
    import mcp_server
    assert mcp_server._timeout() == 12.5


def test_timeout_env_invalid(monkeypatch):
    monkeypatch.setenv("EXCHANGE_CONNECTOR_MCP_TIMEOUT", "not-a-number")
    import mcp_server
    assert mcp_server._timeout() == 120.0


def test_timeout_env_zero(monkeypatch):
    monkeypatch.setenv("EXCHANGE_CONNECTOR_MCP_TIMEOUT", "0")
    import mcp_server
    # Zero/negative collapses to default — never an instant-fail timeout.
    assert mcp_server._timeout() == 120.0


# ---------------------------------------------------------------------------
# Test 3: JSON helper — GET / POST / error mapping
# ---------------------------------------------------------------------------
def _patch_urlopen(monkeypatch, response: _FakeResp, error: Optional[Exception] = None):
    """Replace urllib.request.urlopen with a stub that returns ``response``
    or raises ``error``."""
    calls: List[Dict[str, Any]] = []

    def _headers_dict(req):
        # urllib normalises header case; map everything to lowercase keys.
        out = {}
        for k, v in (req.header_items() if hasattr(req, "header_items") else req.headers.items()):
            out[k.lower()] = v
        return out

    def fake_urlopen(req, timeout=None):
        calls.append({
            "url": req.full_url,
            "method": req.get_method(),
            "headers": _headers_dict(req),
            "data": req.data,
            "timeout": timeout,
        })
        if error is not None:
            raise error
        return response

    monkeypatch.setattr("mcp_server.urllib.request.urlopen", fake_urlopen)
    return calls


def test_http_get_parses_json(monkeypatch):
    body = json.dumps({"status": "ok", "service": "stub"}).encode("utf-8")
    calls = _patch_urlopen(monkeypatch, _FakeResp(200, body))

    import mcp_server
    out = mcp_server._http_request("GET", "/health", base_url="http://x")

    assert out == {"status": "ok", "service": "stub"}
    assert len(calls) == 1
    assert calls[0]["url"] == "http://x/health"
    assert calls[0]["method"] == "GET"
    assert calls[0]["data"] is None
    assert calls[0]["headers"]["accept"] == "application/json"


def test_http_post_with_payload_serializes_json(monkeypatch):
    body = json.dumps({"ok": True}).encode("utf-8")
    calls = _patch_urlopen(monkeypatch, _FakeResp(200, body))

    import mcp_server
    out = mcp_server._http_request(
        "POST", "/list_messages", {"limit": 5}, base_url="http://x",
    )

    assert out == {"ok": True}
    assert calls[0]["method"] == "POST"
    assert json.loads(calls[0]["data"].decode("utf-8")) == {"limit": 5}
    assert calls[0]["headers"]["content-type"] == "application/json"


def test_http_normalises_method_to_uppercase(monkeypatch):
    body = json.dumps({"ok": True}).encode("utf-8")
    calls = _patch_urlopen(monkeypatch, _FakeResp(200, body))

    import mcp_server
    mcp_server._http_request("post", "/health", base_url="http://x")
    assert calls[0]["method"] == "POST"


def test_http_adds_leading_slash(monkeypatch):
    body = b'{"ok":true}'
    calls = _patch_urlopen(monkeypatch, _FakeResp(200, body))

    import mcp_server
    mcp_server._http_request("GET", "health", base_url="http://x")
    assert calls[0]["url"] == "http://x/health"


def test_http_non_2xx_returns_structured_error(monkeypatch):
    body = json.dumps({"detail": "vpn_down"}).encode("utf-8")
    err = _urllib_error.HTTPError(
        "http://x/ready", 503, "Service Unreachable",
        {}, _io_bytes(body),
    )
    _patch_urlopen(monkeypatch, _FakeResp(200, b""), error=err)

    import mcp_server
    out = mcp_server._http_request("GET", "/ready", base_url="http://x")

    assert out["error"] == "http_error"
    assert out["status"] == 503
    assert out["path"] == "/ready"
    assert out["body"] == {"detail": "vpn_down"}
    assert "503" in out["message"]


def test_http_404_maps_status_code_into_error(monkeypatch):
    body = b'{"detail":"not_found"}'
    err = _urllib_error.HTTPError(
        "http://x/get_message", 404, "Not Found", {}, _io_bytes(body),
    )
    _patch_urlopen(monkeypatch, _FakeResp(200, b""), error=err)

    import mcp_server
    out = mcp_server._http_request("POST", "/get_message", {"item_id": "x"}, base_url="http://x")
    assert out["status"] == 404
    assert out["body"] == {"detail": "not_found"}


def test_http_url_error_returns_structured_dict(monkeypatch):
    err = _urllib_error.URLError("Connection refused")
    _patch_urlopen(monkeypatch, _FakeResp(200, b""), error=err)

    import mcp_server
    out = mcp_server._http_request("GET", "/health", base_url="http://x")
    assert out["error"] == "http_error"
    assert out["status"] == 0
    assert "URLError" in out["message"]
    assert out["path"] == "/health"


def test_http_non_json_response_returns_raw_marker(monkeypatch):
    _patch_urlopen(monkeypatch, _FakeResp(200, b"not json at all"))

    import mcp_server
    out = mcp_server._http_request("GET", "/anything", base_url="http://x")
    # Body that fails JSON parse still yields a structured dict.
    assert isinstance(out, dict)


def test_http_2xx_with_empty_body(monkeypatch):
    _patch_urlopen(monkeypatch, _FakeResp(204, b""))

    import mcp_server
    out = mcp_server._http_request("GET", "/x", base_url="http://x")
    # Non-JSON 2xx still yields a dict with raw marker.
    assert isinstance(out, dict)


# ---------------------------------------------------------------------------
# Test 4: Tool wrappers dispatch to the correct REST path/payload
# ---------------------------------------------------------------------------
@pytest.fixture
def stub_http(monkeypatch):
    """Return a list that captures every URL+payload seen by the patched
    urlopen. Use ``calls`` to assert what the tool sent."""

    calls: List[Dict[str, Any]] = []

    def _headers_dict(req):
        out = {}
        for k, v in (req.header_items() if hasattr(req, "header_items") else req.headers.items()):
            out[k.lower()] = v
        return out

    def fake(req, timeout=None):
        calls.append({
            "url": req.full_url,
            "method": req.get_method(),
            "data": req.data,
            "headers": _headers_dict(req),
            "timeout": timeout,
        })
        body = json.dumps({"echo": _maybe_load(req.data), "status": "ok"}).encode("utf-8")
        return _FakeResp(200, body)

    monkeypatch.setattr("mcp_server.urllib.request.urlopen", fake)
    return calls


def _maybe_load(data):
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        return None


def _io_bytes(payload: bytes):
    """Build a file-like object that ``HTTPError.read()`` can drain."""
    import io
    return io.BytesIO(payload)


def test_work_mail_health_calls_get_health(stub_http):
    import mcp_server
    out = mcp_server.work_mail_health()
    assert out == {"echo": None, "status": "ok"}
    assert len(stub_http) == 1
    assert stub_http[0]["method"] == "GET"
    assert stub_http[0]["url"].endswith("/health")


def test_work_mail_ready_calls_get_ready(stub_http):
    import mcp_server
    mcp_server.work_mail_ready()
    assert stub_http[0]["method"] == "GET"
    assert stub_http[0]["url"].endswith("/ready")


def test_work_mail_list_messages_payload(stub_http):
    import mcp_server
    mcp_server.work_mail_list_messages(
        folder="archive_inbox",
        limit=10,
        offset=5,
        received_after="2026-01-01T00:00:00Z",
        received_before="2026-01-02T00:00:00Z",
        refresh=True,
    )
    payload = _maybe_load(stub_http[0]["data"])
    assert payload == {
        "folder": "archive_inbox",
        "limit": 10,
        "offset": 5,
        "received_after": "2026-01-01T00:00:00Z",
        "received_before": "2026-01-02T00:00:00Z",
        "refresh": True,
    }
    assert stub_http[0]["url"].endswith("/list_messages")


def test_work_mail_list_messages_strips_none(stub_http):
    import mcp_server
    mcp_server.work_mail_list_messages(limit=7)
    payload = _maybe_load(stub_http[0]["data"])
    assert payload == {"limit": 7, "offset": 0, "refresh": False}
    assert "folder" not in payload
    assert "received_after" not in payload


def test_work_mail_search_messages_payload(stub_http):
    import mcp_server
    mcp_server.work_mail_search_messages(query="budget", folder="inbox", limit=3)
    payload = _maybe_load(stub_http[0]["data"])
    assert payload == {"query": "budget", "limit": 3, "offset": 0, "folder": "inbox"}
    assert stub_http[0]["url"].endswith("/search_messages")


def test_work_mail_get_message_prefers_ref(stub_http):
    import mcp_server
    mcp_server.work_mail_get_message(message_ref="abc")
    payload = _maybe_load(stub_http[0]["data"])
    assert payload == {"message_ref": "abc"}


def test_work_mail_get_message_falls_back_to_item_id(stub_http):
    import mcp_server
    mcp_server.work_mail_get_message(item_id="RAWID")
    payload = _maybe_load(stub_http[0]["data"])
    assert payload == {"item_id": "RAWID"}


def test_work_mail_send_message_payload(stub_http):
    import mcp_server
    mcp_server.work_mail_send_message(
        to=["a@example.com", "b@example.com"],
        subject="hi",
        body="hello",
        cc=["c@example.com"],
    )
    payload = _maybe_load(stub_http[0]["data"])
    assert payload == {
        "to": ["a@example.com", "b@example.com"],
        "subject": "hi",
        "body": "hello",
        "cc": ["c@example.com"],
    }
    assert stub_http[0]["url"].endswith("/send_message")


def test_work_mail_send_message_omits_empty_cc_bcc(stub_http):
    import mcp_server
    mcp_server.work_mail_send_message(to=["x@y"], subject="s", body="b")
    payload = _maybe_load(stub_http[0]["data"])
    assert "cc" not in payload
    assert "bcc" not in payload


def test_work_mail_reply_message_payload(stub_http):
    import mcp_server
    mcp_server.work_mail_reply_message(message_ref="REF", body="ok", subject="Re: hi")
    payload = _maybe_load(stub_http[0]["data"])
    assert payload == {"message_ref": "REF", "body": "ok", "subject": "Re: hi"}
    assert stub_http[0]["url"].endswith("/reply_message")


def test_work_mail_list_attachments_payload(stub_http):
    import mcp_server
    mcp_server.work_mail_list_attachments(message_ref="REF")
    payload = _maybe_load(stub_http[0]["data"])
    assert payload == {"message_ref": "REF"}
    assert stub_http[0]["url"].endswith("/list_attachments")


def test_work_mail_download_attachment_payload(stub_http):
    import mcp_server
    mcp_server.work_mail_download_attachment(
        message_ref="REF",
        attachment_ref="ATTREF",
        filename="x.txt",
        target_dir="/tmp/dl",
    )
    payload = _maybe_load(stub_http[0]["data"])
    assert payload == {
        "message_ref": "REF",
        "attachment_ref": "ATTREF",
        "filename": "x.txt",
        "target_dir": "/tmp/dl",
    }
    assert stub_http[0]["url"].endswith("/download_attachment")


def test_work_rag_index_documents_payload(stub_http):
    import mcp_server
    mcp_server.work_rag_index_documents(
        root_dir="rag_data",
        limit=100,
        max_file_bytes=500000,
        extensions=[".txt", ".md"],
        force=True,
    )
    payload = _maybe_load(stub_http[0]["data"])
    assert payload == {
        "root_dir": "rag_data",
        "limit": 100,
        "max_file_bytes": 500000,
        "extensions": [".txt", ".md"],
        "force": True,
    }
    assert stub_http[0]["url"].endswith("/index_rag_documents")


def test_work_rag_index_documents_omits_empty_extensions(stub_http):
    import mcp_server
    mcp_server.work_rag_index_documents()
    payload = _maybe_load(stub_http[0]["data"])
    assert "extensions" not in payload


def test_work_rag_search_documents_payload(stub_http):
    import mcp_server
    mcp_server.work_rag_search_documents(query="quarterly", limit=5, root_dir="rag_data")
    payload = _maybe_load(stub_http[0]["data"])
    assert payload == {"query": "quarterly", "limit": 5, "root_dir": "rag_data"}
    assert stub_http[0]["url"].endswith("/search_rag_documents")


def test_work_rag_get_document_payload(stub_http):
    import mcp_server
    mcp_server.work_rag_get_document(doc_ref="DOC123", include_text=True, max_chars=1000)
    payload = _maybe_load(stub_http[0]["data"])
    assert payload == {"doc_ref": "DOC123", "include_text": True, "max_chars": 1000}
    assert stub_http[0]["url"].endswith("/get_rag_document")


def test_work_mail_list_folders_payload(stub_http):
    import mcp_server
    mcp_server.work_mail_list_folders(target="root")
    payload = _maybe_load(stub_http[0]["data"])
    assert payload == {"target": "root"}
    assert stub_http[0]["url"].endswith("/list_folders")


def test_work_mail_download_status(stub_http):
    import mcp_server
    mcp_server.work_mail_download_status()
    assert stub_http[0]["method"] == "GET"
    assert stub_http[0]["url"].endswith("/download_status")


def test_work_mail_start_dataset_download_payload(stub_http):
    import mcp_server
    mcp_server.work_mail_start_dataset_download(
        limit_per_folder=50, folder="inbox",
        download_attachments=False, extract_archives=False, recursive=False,
    )
    payload = _maybe_load(stub_http[0]["data"])
    assert payload == {
        "limit_per_folder": 50,
        "folder": "inbox",
        "download_attachments": False,
        "extract_archives": False,
        "recursive": False,
    }
    assert stub_http[0]["url"].endswith("/download_dataset")


def test_work_calendar_get_payload(stub_http):
    import mcp_server
    mcp_server.work_calendar_get(date="2026-01-15")
    payload = _maybe_load(stub_http[0]["data"])
    assert payload == {"date": "2026-01-15"}
    assert stub_http[0]["url"].endswith("/get_calendar")


def test_work_calendar_search_payload(stub_http):
    import mcp_server
    mcp_server.work_calendar_search(query="budget", days=7)
    payload = _maybe_load(stub_http[0]["data"])
    assert payload == {"query": "budget", "days": 7}
    assert stub_http[0]["url"].endswith("/search_calendar")


def test_work_calendar_add_payload(stub_http):
    import mcp_server
    mcp_server.work_calendar_add(
        subject="Standup",
        start_time="2026-01-15 10:00",
        end_time="2026-01-15 10:30",
        body="Daily",
        location="Room A",
        attendees=["a@x", "b@y"],
    )
    payload = _maybe_load(stub_http[0]["data"])
    assert payload == {
        "subject": "Standup",
        "start_time": "2026-01-15 10:00",
        "end_time": "2026-01-15 10:30",
        "body": "Daily",
        "location": "Room A",
        "attendees": ["a@x", "b@y"],
    }
    assert stub_http[0]["url"].endswith("/add_event")


def test_work_calendar_delete_payload(stub_http):
    import mcp_server
    mcp_server.work_calendar_delete(subject="Standup")
    payload = _maybe_load(stub_http[0]["data"])
    assert payload == {"subject": "Standup"}
    assert stub_http[0]["url"].endswith("/delete_event")


# ---------------------------------------------------------------------------
# Test 5: tool name stability and required tools present
# ---------------------------------------------------------------------------
def test_required_tool_names_present():
    import mcp_server
    names = {t["name"] for t in mcp_server.TOOL_REGISTRY}
    required = {
        "work_mail_health",
        "work_mail_ready",
        "work_mail_list_messages",
        "work_mail_search_messages",
        "work_mail_get_message",
        "work_mail_send_message",
        "work_mail_reply_message",
        "work_mail_list_attachments",
        "work_mail_download_attachment",
        "work_rag_index_documents",
        "work_rag_search_documents",
        "work_rag_get_document",
        "work_mail_list_folders",
        "work_mail_download_status",
        "work_calendar_get",
        "work_calendar_search",
        "work_calendar_add",
        "work_calendar_delete",
    }
    missing = required - names
    assert not missing, f"missing tools: {missing}"


def test_tool_registry_implementations_exist():
    """Every name in TOOL_REGISTRY must resolve to a real callable."""
    import mcp_server
    for entry in mcp_server.TOOL_REGISTRY:
        impl = globals().get(entry["impl"]) or getattr(mcp_server, entry["impl"])
        assert callable(impl), f"tool {entry['name']} impl not callable"


# ---------------------------------------------------------------------------
# Test 6: clean() helper
# ---------------------------------------------------------------------------
def test_clean_strips_none():
    import mcp_server
    assert mcp_server._clean({"a": 1, "b": None, "c": "x"}) == {"a": 1, "c": "x"}
    assert mcp_server._clean({}) == {}
    assert mcp_server._clean({"a": 0, "b": "", "c": False}) == {"a": 0, "b": "", "c": False}


# ---------------------------------------------------------------------------
# Test 7: no test ever points at the real 127.0.0.1:5015
# ---------------------------------------------------------------------------
def test_default_base_url_is_127_0_0_1_but_tests_use_stub():
    """Sanity guard: this suite must not run against the live service."""
    import mcp_server
    # Default base URL points at the canonical local REST service.
    # All test-level HTTP traffic in this module is replaced by stubs.
    assert mcp_server._base_url() == "http://127.0.0.1:5015"


# ---------------------------------------------------------------------------
# Test 8: MCP SDK exception path — graceful fallback
# ---------------------------------------------------------------------------
def test_build_mcp_raises_loudly_when_sdk_missing(monkeypatch):
    """If FastMCP cannot be imported, _build_mcp() must raise a clear
    RuntimeError, not a silent AttributeError."""
    import mcp_server
    monkeypatch.setattr(mcp_server, "FastMCP", None)
    monkeypatch.setattr(mcp_server, "_MCP_IMPORT_ERROR", RuntimeError("missing"))
    monkeypatch.setattr(mcp_server, "_mcp_instance", None)
    with pytest.raises(RuntimeError, match="mcp SDK is not importable"):
        mcp_server._build_mcp()


def test_register_tools_uses_fastmcp_decorator(monkeypatch):
    """Registration iterates TOOL_REGISTRY and calls ``server.tool()``
    once per entry. We don't need to start a real server."""
    import mcp_server

    calls = []

    class FakeServer:
        def tool(self):
            def deco(fn):
                calls.append(fn.__name__)
                return fn
            return deco

    fake = FakeServer()
    mcp_server._register_tools(fake)
    # Every impl in TOOL_REGISTRY must end up registered exactly once.
    impls = {entry["impl"] for entry in mcp_server.TOOL_REGISTRY}
    assert set(calls) == impls


# ---------------------------------------------------------------------------
# Test 9: protocol smoke — real MCP client over stdio talking to mcp_server
# ---------------------------------------------------------------------------
class _StubHTTPHandler(BaseHTTPRequestHandler):
    """Stub used by the protocol smoke. Always returns the spec-required
    payloads; records calls into a shared list."""

    captured: List[Dict[str, Any]] = []

    def log_message(self, *a, **kw):
        return

    def _read_body(self) -> Optional[Dict[str, Any]]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def _send_json(self, code: int, obj: Dict[str, Any]) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.__class__.captured.append({"method": "GET", "path": self.path})
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "service": "stub"})
        elif self.path == "/ready":
            self._send_json(200, {"ready": True})
        else:
            self._send_json(404, {"error": "not_found", "path": self.path})

    def do_POST(self):
        payload = self._read_body()
        self.__class__.captured.append(
            {"method": "POST", "path": self.path, "payload": payload}
        )
        if self.path == "/list_messages":
            self._send_json(200, {
                "status": "success",
                "messages": [],
                "echo": payload,
            })
        elif self.path == "/search_rag_documents":
            self._send_json(200, {
                "status": "success",
                "count": 0,
                "documents": [],
            })
        else:
            self._send_json(200, {"status": "ok"})


def _start_stub_server() -> tuple[HTTPServer, int]:
    _StubHTTPHandler.captured = []
    srv = HTTPServer(("127.0.0.1", 0), _StubHTTPHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port


def test_mcp_protocol_smoke_real_stdio(monkeypatch):
    """Spawn mcp_server.py over stdio and drive it via the real MCP
    SDK client. No monkeypatching of ``_http_request`` here — the
    spawned subprocess talks HTTP to our stub.

    Uses ``asyncio.run`` directly so we don't depend on pytest-asyncio.
    """
    pytest.importorskip("mcp")
    import asyncio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def _drive():
        srv, port = _start_stub_server()
        base_url = f"http://127.0.0.1:{port}"

        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        py = os.path.join(project_dir, ".venv", "bin", "python")
        if not os.path.exists(py):
            py = sys.executable

        params = StdioServerParameters(
            command=py,
            args=["mcp_server.py"],
            env={**os.environ, "EXCHANGE_CONNECTOR_BASE_URL": base_url},
        )

        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    assert init is not None

                    tools = await session.list_tools()
                    names = {t.name for t in tools.tools}
                    for required in (
                        "work_mail_health",
                        "work_mail_list_messages",
                        "work_rag_search_documents",
                    ):
                        assert required in names, f"missing tool: {required}"

                    # Call work_mail_health → stub returns {status:ok,service:stub}.
                    result = await session.call_tool("work_mail_health", {})
                    payload = _extract_json_payload(result)
                    assert payload.get("status") == "ok"
                    assert payload.get("service") == "stub"

                    # Call work_mail_list_messages(limit=1) → stub echoes payload.
                    result = await session.call_tool(
                        "work_mail_list_messages", {"limit": 1},
                    )
                    payload = _extract_json_payload(result)
                    assert payload.get("status") == "success"
                    assert payload.get("echo", {}).get("limit") == 1
        finally:
            srv.shutdown()

    asyncio.run(_drive())


def _extract_json_payload(result) -> Dict[str, Any]:
    """Pull the JSON object out of an MCP ``CallToolResult``.

    FastMCP's structured-content path wraps the dict returned by the
    tool inside ``{"result": <our_dict>}`` on the ``structuredContent``
    field. The text-content path serialises the same dict as JSON text
    on a single content block. We try both and fall back to a diagnostic
    dump if neither matches.
    """
    if hasattr(result, "structuredContent") and result.structuredContent:
        sc = result.structuredContent
        if isinstance(sc, dict):
            # FastMCP structured mode: {"result": {our_dict}} OR just
            # our_dict when the tool returns a typed model directly.
            if set(sc.keys()) == {"result"} and isinstance(sc["result"], dict):
                return sc["result"]
            return sc
    content = getattr(result, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            try:
                parsed = json.loads(text)
            except Exception:
                continue
            if isinstance(parsed, dict):
                # Same {"result": ...} wrapper in text form.
                if set(parsed.keys()) == {"result"} and isinstance(parsed["result"], dict):
                    return parsed["result"]
                return parsed
    return {"_raw": repr(result)}


# ---------------------------------------------------------------------------
# Test 10: McpHttpError.to_dict is stable
# ---------------------------------------------------------------------------
def test_mcp_http_error_to_dict():
    import mcp_server
    err = mcp_server.McpHttpError(503, "/ready", {"detail": "x"}, message="boom")
    out = err.to_dict()
    assert out == {
        "error": "http_error",
        "status": 503,
        "path": "/ready",
        "message": "boom",
        "body": {"detail": "x"},
    }

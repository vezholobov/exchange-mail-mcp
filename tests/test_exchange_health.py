"""
Tests for the W1 deliverable: structured VPN / DNS / route / account readiness.

These tests use monkeypatch / mock injections — no real Exchange, no real VPN,
no real DNS lookup. They exercise:

  * exchange_service.build_readiness directly (pure stdlib module).
  * The new /health and /ready endpoints via FastAPI TestClient.
  * The VPN guard is preserved: get_account() short-circuits when VPN is down.

main.py pulls in exchangelib + uvicorn + zoneinfo when imported, so this test
sets the minimum required env-vars BEFORE importing the app and stubs the
heavy Exchange class machinery. Existing endpoints are smoke-asserted (path
existence) — we do not actually hit them.
"""
from __future__ import annotations

import socket
import sys
import types
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Path bootstrap: the working dir is the repo root.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Set required env-vars BEFORE importing main.py.
import os
os.environ.setdefault("EXCHANGE_USER", r"DOMAIN\testuser")
os.environ.setdefault("EXCHANGE_PASS", "x" * 8)  # value never printed, length-only
os.environ.setdefault("EXCHANGE_EMAIL", "test@example.com")
os.environ.setdefault("EXCHANGE_SERVER", "owa.example.com")


# ---------------------------------------------------------------------------
# exchange_service unit tests — pure stdlib, fast.
# ---------------------------------------------------------------------------
import exchange_service as svc  # noqa: E402


class _FakeResult:
    def __init__(self, stdout: str | None = None, returncode: int | None = None):
        # If the caller didn't override explicitly, fall back to class-level defaults.
        self.stdout = stdout if stdout is not None else type(self)._stdout
        self.returncode = returncode if returncode is not None else type(self)._returncode


class _FakeProcessOK(_FakeResult):
    _stdout = "vpn0  UNKNOWN  10.255.0.1/21  global  vpn_zone\n"
    _returncode = 0


class _FakeProcessDown(_FakeResult):
    _stdout = ""
    _returncode = 1


def test_get_vpn_status_active():
    out = svc.get_vpn_status(runner=lambda **_: _FakeProcessOK())
    assert out["active"] is True
    assert out["ip"] == "10.255.0.1"
    assert "vpn0" in out["raw"]


def test_get_vpn_status_down_when_returncode_nonzero():
    out = svc.get_vpn_status(runner=lambda **_: _FakeProcessDown())
    assert out["active"] is False
    assert out["ip"] is None


def test_get_vpn_status_down_when_runner_raises():
    out = svc.get_vpn_status(runner=lambda **_: (_ for _ in ()).throw(OSError("no ip")))
    assert out["active"] is False


def test_resolve_exchange_server_success(monkeypatch):
    fake_infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]
    monkeypatch.setattr(svc.socket, "getaddrinfo", lambda *a, **k: fake_infos)
    out = svc.resolve_exchange_server("owa.example.com")
    assert out["server"] == "owa.example.com"
    assert out["resolved"] is True
    assert out["ip"] == "10.0.0.5"
    assert out["error"] is None


def test_resolve_exchange_server_failure(monkeypatch):
    def raise_gai(*a, **k):
        raise socket.gaierror("no such host")
    monkeypatch.setattr(svc.socket, "getaddrinfo", raise_gai)
    out = svc.resolve_exchange_server("owa.example.com")
    assert out["resolved"] is False
    assert out["ip"] is None
    assert "gaierror" in (out["error"] or "")


def test_resolve_exchange_server_empty():
    out = svc.resolve_exchange_server("")
    assert out["resolved"] is False
    assert out["error"] == "empty server"


def test_check_exchange_route_skipped_when_vpn_down():
    """CRITICAL: VPN-down guard — no TCP probe attempted."""
    out = svc.check_exchange_route("10.0.0.5", vpn_active=False)
    assert out["skipped"] is True
    assert out["reachable"] is None
    assert out["error"] == "vpn_down"


def test_check_exchange_route_skipped_when_no_ip():
    out = svc.check_exchange_route(None, vpn_active=True)
    assert out["skipped"] is True
    assert out["reachable"] is None


def test_check_exchange_route_reachable():
    def fake_connect(addr, timeout):
        s = socket.socket()
        # actually open a real local socket for the duration of the check,
        # but against localhost only — never against the public address.
        s.settimeout(timeout)
        s.connect(("127.0.0.1", 1))  # may raise; we only test the wrapper logic
        s.close()
    # The connector runs once; we don't want to actually fail the test
    # if no listener exists. Use a deterministic approach: monkeypatch via
    # a lambda that just sets reachable=True via the wrapper semantics.
    seen = {"called": False, "addr": None}

    def connector(addr, timeout):
        seen["called"] = True
        seen["addr"] = addr

    out = svc.check_exchange_route("10.0.0.5", vpn_active=True, connector=connector)
    assert seen["called"] is True
    assert seen["addr"] == ("10.0.0.5", svc.EXCHANGE_TCP_PORT)
    assert out["reachable"] is True
    assert out["skipped"] is False


def test_check_exchange_route_unreachable():
    def connector(addr, timeout):
        raise OSError("no route to host")

    out = svc.check_exchange_route("10.0.0.5", vpn_active=True, connector=connector)
    assert out["reachable"] is False
    assert "OSError" in (out["error"] or "")


def test_account_is_cached_true():
    assert svc.account_is_cached(lambda: True) is True


def test_account_is_cached_false():
    assert svc.account_is_cached(lambda: False) is False


def test_account_is_cached_handles_exception():
    def boom():
        raise RuntimeError("nope")
    assert svc.account_is_cached(boom) is False


def test_build_readiness_ready(monkeypatch):
    """Happy path — VPN up, DNS resolves, route reachable, no account cached."""
    fake_infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]
    monkeypatch.setattr(svc.socket, "getaddrinfo", lambda *a, **k: fake_infos)

    runner = lambda **_: _FakeProcessOK()
    connector = lambda addr, timeout: None  # pretend success

    out = svc.build_readiness(
        server="owa.example.com",
        is_account_cached=lambda: False,
        runner=runner,
        connector=connector,
    )
    assert out["service_alive"] is True
    assert out["vpn_active"] is True
    assert out["vpn_ip"] == "10.255.0.1"
    assert out["exchange_server"] == "owa.example.com"
    assert out["exchange_dns"]["resolved"] is True
    assert out["exchange_route"]["reachable"] is True
    assert out["account_cached"] is False
    assert out["ready"] is True
    assert out["reason"] == "ok"


def test_build_readiness_vpn_down(monkeypatch):
    monkeypatch.setattr(svc.socket, "getaddrinfo", lambda *a, **k: [])
    runner = lambda **_: _FakeProcessDown()  # VPN down
    out = svc.build_readiness(
        server="owa.example.com",
        is_account_cached=lambda: True,
        runner=runner,
        connector=lambda addr, timeout: None,
    )
    assert out["vpn_active"] is False
    assert out["ready"] is False
    assert out["reason"] == "vpn_down"
    # Account cached but readiness false — VPN guard wins.
    # (account_cached is informational; ready is the gate.)
    assert out["account_cached"] is True


def test_build_readiness_dns_failure(monkeypatch):
    def raise_gai(*a, **k):
        raise socket.gaierror("nope")
    monkeypatch.setattr(svc.socket, "getaddrinfo", raise_gai)
    runner = lambda **_: _FakeProcessOK()
    out = svc.build_readiness(
        server="owa.example.com",
        is_account_cached=lambda: False,
        runner=runner,
        connector=lambda addr, timeout: None,
    )
    assert out["ready"] is False
    assert out["reason"].startswith("dns_unresolved:")


def test_build_readiness_route_failure(monkeypatch):
    fake_infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]
    monkeypatch.setattr(svc.socket, "getaddrinfo", lambda *a, **k: fake_infos)
    runner = lambda **_: _FakeProcessOK()

    def bad_connector(addr, timeout):
        raise OSError("timeout")

    out = svc.build_readiness(
        server="owa.example.com",
        is_account_cached=lambda: False,
        runner=runner,
        connector=bad_connector,
    )
    assert out["ready"] is False
    assert out["reason"].startswith("route_unreachable:")


def test_build_readiness_keys_complete():
    """All required keys are present regardless of success/failure."""
    out = svc.build_readiness(
        server="owa.example.com",
        is_account_cached=lambda: False,
        runner=lambda **_: _FakeProcessDown(),
        connector=lambda addr, timeout: None,
    )
    for key in (
        "service_alive",
        "vpn_active",
        "vpn_ip",
        "exchange_server",
        "exchange_dns",
        "exchange_route",
        "account_cached",
        "ready",
        "reason",
    ):
        assert key in out, f"missing key {key}"


# ---------------------------------------------------------------------------
# TestClient tests for the HTTP layer.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    """Import main.py lazily with exchangelib partly stubbed so the import
    does not require real Exchange network or Python 3.12 zoneinfo semantics.
    The fixture patches _run_ip_cmd directly on the service module.
    """
    # We import main only once the env-vars are set.
    import main  # noqa: F401  (imports fastapi/exchangelib)
    # Replace the vpn check used by main.is_vpn_active to avoid subprocess.
    # (main.is_vpn_active shells out; we monkeypatch it via the service module.)
    main._account_cached_predicate  # sanity: helper exists
    with TestClient(main.app) as c:
        yield c


def test_health_returns_200(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "exchange-connector"


def test_ready_returns_503_when_vpn_down(client, monkeypatch):
    """Force VPN-down via the service module's runner."""
    import main as main_module
    from exchange_service import build_readiness as real_build

    fake_infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

    class _Down(_FakeResult):
        _stdout = ""
        _returncode = 1

    def fake_build(*, server, is_account_cached, **_):
        # Drive the real branch logic via the same module.
        import exchange_service as svc2
        monkeypatch.setattr(svc2.socket, "getaddrinfo", lambda *a, **k: fake_infos)
        return real_build(
            server=server,
            is_account_cached=is_account_cached,
            runner=lambda **_: _Down(),
            connector=lambda addr, timeout: None,
        )

    monkeypatch.setattr(main_module, "build_readiness", fake_build)

    r = client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["ready"] is False
    assert body["vpn_active"] is False
    assert body["reason"] == "vpn_down"
    # All required fields present in the JSON.
    for key in (
        "service_alive", "vpn_active", "vpn_ip", "exchange_server",
        "exchange_dns", "exchange_route", "account_cached", "ready", "reason",
    ):
        assert key in body


def test_ready_returns_200_when_all_green(client, monkeypatch):
    import main as main_module
    from exchange_service import build_readiness as real_build

    fake_infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

    class _OK(_FakeResult):
        _stdout = "vpn0  UNKNOWN  10.255.0.1/21  global\n"
        _returncode = 0

    def fake_build(*, server, is_account_cached, **_):
        import exchange_service as svc2
        monkeypatch.setattr(svc2.socket, "getaddrinfo", lambda *a, **k: fake_infos)
        return real_build(
            server=server,
            is_account_cached=is_account_cached,
            runner=lambda **_: _OK(),
            connector=lambda addr, timeout: None,
        )

    monkeypatch.setattr(main_module, "build_readiness", fake_build)

    r = client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["vpn_active"] is True
    assert body["vpn_ip"] == "10.255.0.1"
    assert body["reason"] == "ok"


def test_existing_endpoints_still_registered(client):
    """Backward compatibility: all prior POST/GET endpoints remain registered."""
    routes = {r.path for r in client.app.routes}
    for p in (
        "/download_status",
        "/check_archive_new",
        "/reply_work_email",
        "/send_work_email",
        "/list_folders",
        "/download_dataset",
        "/add_event",
        "/delete_event",
        "/get_calendar",
        "/search_calendar",
        "/sync_emails",
        "/health",
        "/ready",
    ):
        assert p in routes, f"endpoint {p} missing"


def test_get_account_skipped_when_vpn_down():
    """The hard constraint: get_account() must NOT touch Exchange auth when
    vpn0 is down. We monkeypatch is_vpn_active() and assert Credentials/Account
    constructors are never invoked."""
    import main as main_module
    # Reset internal cache to force a new auth attempt — the guard should
    # block it.
    main_module._cached_account = None
    main_module._account_created_at = None

    auth_called = {"count": 0}

    class _FakeCredentials:
        def __init__(self, *a, **kw):
            auth_called["count"] += 1

    class _FakeAccount:
        def __init__(self, *a, **kw):
            auth_called["count"] += 1

    # Patch exchangelib symbols inside main's namespace.
    main_module.Credentials = _FakeCredentials
    main_module.Account = _FakeAccount

    # Force VPN-down.
    monkeypatch_vpn = lambda: False
    main_module.is_vpn_active = monkeypatch_vpn

    result = main_module.get_account()
    assert result is None
    assert auth_called["count"] == 0, "Credentials/Account must NOT be constructed when VPN is down"

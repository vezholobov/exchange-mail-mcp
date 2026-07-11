"""
exchange_service.py — readiness and health probes for the Exchange connector.

This module is intentionally stdlib-only (no FastAPI, no exchangelib imports)
so it can be loaded safely from tests and from main.py without triggering
network/auth side effects.

Public API:
    get_vpn_status() -> dict              # {"active": bool, "ip": Optional[str], "raw": str}
    resolve_exchange_server(server: str, timeout: float = 2.0) -> dict
    check_exchange_route(ip: str, timeout: float = 2.0) -> dict
    account_is_cached(is_cached_fn) -> bool
    build_readiness(...) -> dict           # the /ready payload
    VpnState, DnsState, RouteState, Reason enums (string constants)

Network probes here are READ-ONLY:
    * VPN probe: parses `ip -br addr show vpn0` (no packets sent).
    * DNS probe: socket.getaddrinfo only (no connection).
    * TCP-route probe: opens a TCP socket to port 443 (TLS handshake start).
      If VPN is down we DO NOT probe (skipped) — same guard as get_account().
      We never send EWS/HTTP auth requests; that would risk account lockout.

No secrets are read or logged from .env in this module.
"""
from __future__ import annotations

import logging
import socket
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("exchange_service")

# Match main.py VPN interface name.
VPN_INTERFACE = "vpn0"
# Cisco AnyConnect / OWA uses HTTPS for EWS.
EXCHANGE_TCP_PORT = 443


# -----------------------------------------------------------------------------
# Result dataclasses — small, JSON-serialisable shapes for tests + main.py.
# -----------------------------------------------------------------------------
@dataclass
class VpnState:
    active: bool = False
    ip: Optional[str] = None
    raw: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"active": self.active, "ip": self.ip, "raw": self.raw}


@dataclass
class DnsState:
    server: str = ""
    resolved: bool = False
    ip: Optional[str] = None
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "server": self.server,
            "resolved": self.resolved,
            "ip": self.ip,
            "error": self.error,
        }


@dataclass
class RouteState:
    reachable: Optional[bool] = None   # True/False when probed; None when skipped
    skipped: bool = False
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "reachable": self.reachable,
            "skipped": self.skipped,
            "error": self.error,
        }


# -----------------------------------------------------------------------------
# VPN probe — wraps what main.is_vpn_active did, but returns structured data.
# -----------------------------------------------------------------------------
def _run_ip_cmd(iface: str = VPN_INTERFACE, timeout: float = 3.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ip", "-br", "addr", "show", iface],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def get_vpn_status(
    runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    iface: str = VPN_INTERFACE,
) -> Dict[str, Any]:
    """Return a dict describing the vpn0 interface state.

    runner is injectable for tests (must mimic subprocess.run signature).
    """
    run = runner or _run_ip_cmd
    try:
        result = run(iface=iface)
    except Exception as e:
        logger.info("vpn probe failed: %s", e)
        return VpnState(active=False, raw="").as_dict()

    if result.returncode != 0 or not (result.stdout or "").strip():
        return VpnState(active=False, raw=(result.stdout or "").strip()).as_dict()

    parts = (result.stdout or "").strip().split()
    # Example line: "vpn0  UNKNOWN  10.255.0.1/21  ..."
    ip_with_cidr = parts[2] if len(parts) >= 3 else None
    if not ip_with_cidr or "/" not in ip_with_cidr:
        return VpnState(active=False, raw=result.stdout.strip()).as_dict()

    ip = ip_with_cidr.split("/", 1)[0]
    return VpnState(active=True, ip=ip, raw=result.stdout.strip()).as_dict()


# -----------------------------------------------------------------------------
# DNS probe — getaddrinfo only. Does NOT open a socket to the server.
# -----------------------------------------------------------------------------
def resolve_exchange_server(server: str, timeout: float = 2.0) -> Dict[str, Any]:
    state = DnsState(server=server)
    if not server:
        state.error = "empty server"
        return state.as_dict()
    try:
        # getaddrinfo with AF_UNSPEC returns first match.
        infos = socket.getaddrinfo(server, None, type=socket.SOCK_STREAM)
        if not infos:
            state.error = "no getaddrinfo result"
            return state.as_dict()
        # Prefer IPv4 if available, else first.
        chosen = next((i for i in infos if i[0] == socket.AF_INET), infos[0])
        ip = chosen[4][0]
        state.resolved = bool(ip)
        state.ip = ip
        return state.as_dict()
    except socket.gaierror as e:
        state.error = f"gaierror: {e}"
        return state.as_dict()
    except Exception as e:
        state.error = f"{type(e).__name__}: {e}"
        return state.as_dict()


# -----------------------------------------------------------------------------
# Route probe — TCP connect to port 443. SAFE: no auth, no EWS request.
# Skipped if VPN is down (preserves the existing guard).
# -----------------------------------------------------------------------------
def check_exchange_route(
    ip: Optional[str],
    vpn_active: bool,
    *,
    timeout: float = 2.0,
    port: int = EXCHANGE_TCP_PORT,
    connector: Optional[Callable[[tuple, float], None]] = None,
) -> Dict[str, Any]:
    """Attempt a TCP connect to (ip, port) to verify the route works.

    connector is injectable for tests. Default is socket.create_connection.
    """
    state = RouteState()
    if not vpn_active:
        state.skipped = True
        state.error = "vpn_down"
        return state.as_dict()
    if not ip:
        state.skipped = True
        state.error = "no_ip"
        return state.as_dict()

    c = connector or (lambda addr, t: socket.create_connection(addr, timeout=t))
    try:
        sock = c((ip, port), timeout)
        try:
            sock.close()
        except Exception:
            pass
        state.reachable = True
        return state.as_dict()
    except Exception as e:
        state.reachable = False
        state.error = f"{type(e).__name__}: {e}"
        return state.as_dict()


# -----------------------------------------------------------------------------
# Account cache check.
# -----------------------------------------------------------------------------
def account_is_cached(is_cached_fn: Callable[[], bool]) -> bool:
    """True if main.py reports an active cached Exchange account."""
    try:
        return bool(is_cached_fn())
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Top-level readiness builder.
# -----------------------------------------------------------------------------
def build_readiness(
    *,
    server: str,
    is_account_cached: Callable[[], bool],
    runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    dns_timeout: float = 2.0,
    route_timeout: float = 2.0,
    connector: Optional[Callable[[tuple, float], None]] = None,
) -> Dict[str, Any]:
    """Build the structured /ready response.

    Returns a dict with keys:
        service_alive, vpn_active, vpn_ip,
        exchange_server, exchange_dns, exchange_route,
        account_cached, ready, reason
    """
    vpn = get_vpn_status(runner=runner)
    dns = resolve_exchange_server(server, timeout=dns_timeout)
    route = check_exchange_route(
        dns.get("ip"),
        vpn["active"],
        timeout=route_timeout,
        connector=connector,
    )
    cached = account_is_cached(is_account_cached)

    # Decide readiness.
    if not vpn["active"]:
        ready, reason = False, "vpn_down"
    elif not dns.get("resolved"):
        ready, reason = False, f"dns_unresolved:{dns.get('error') or 'unknown'}"
    elif not route.get("reachable"):
        ready, reason = False, f"route_unreachable:{route.get('error') or 'unknown'}"
    else:
        ready, reason = True, "ok"

    return {
        "service_alive": True,
        "vpn_active": vpn["active"],
        "vpn_ip": vpn["ip"],
        "exchange_server": server,
        "exchange_dns": dns,
        "exchange_route": route,
        "account_cached": cached,
        "ready": ready,
        "reason": reason,
    }

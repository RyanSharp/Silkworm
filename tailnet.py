"""Reading Tailscale's view of this machine: one implementation, shared by
`silkworm tailscale status` (bin/silkworm) and the mini's `tailscale` health
probe (health_probes.py), so the two cannot disagree about what "the
dashboard is reachable from the phone" means.

Everything here reads; nothing turns serve or Funnel on or off.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

#: The Mac app ships its CLI inside the bundle; Homebrew's puts it on PATH.
TAILSCALE_APP = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")


class TailscaleError(Exception):
    """A `tailscale` call that gave no JSON: its exit code and what it said."""

    def __init__(self, rc: int, text: str):
        super().__init__(text)
        self.rc, self.text = rc, text

    @property
    def daemon_down(self) -> bool:
        """The CLI is here, but no tailscaled is answering it -- the app quit,
        or never started after a reboot."""
        t = self.text.lower()
        return "failed to connect to local tailscale" in t or "is tailscale running" in t


def _subprocess_run(argv, timeout):
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout, r.stderr


def tailscale_bin() -> str:
    """The tailscale CLI, or "" when Tailscale is not installed."""
    found = shutil.which("tailscale")
    if found:
        return found
    return str(TAILSCALE_APP) if TAILSCALE_APP.exists() else ""


def tailscale_call(ts: str, *args, run=None, timeout: float = 15) -> dict:
    """`tailscale <args> --json`, parsed. Raises TailscaleError when it exits
    non-zero or prints no JSON. `run(argv, timeout) -> (rc, out, err)` is
    the caller's way of running it (the health probe's bounded System); its
    own exceptions -- a timeout, a missing binary -- pass through untouched."""
    rc, out, err = (run or _subprocess_run)([ts, *args, "--json"], timeout)
    if rc != 0:
        raise TailscaleError(rc, (err or out or "").strip())
    if not (out or "").strip():
        return {}
    try:
        data = json.loads(out)
    except ValueError:
        raise TailscaleError(rc, "printed something that is not JSON")
    return data if isinstance(data, dict) else {}


def tailscale_json(ts: str, *args) -> dict:
    """`tailscale <args> --json`, parsed; {} when it fails or says nothing."""
    try:
        return tailscale_call(ts, *args)
    except (TailscaleError, OSError, subprocess.TimeoutExpired):
        return {}


def serve_entries(cfg: dict) -> tuple:
    """Everything a `tailscale serve status --json` config exposes: web
    proxy targets, and descriptions of anything else (raw TCP forwards, file
    or text handlers), and whether Funnel is on -- including foreground
    sessions and Services, which nest whole configs of their own. A raw TCP
    forward to the dashboard would skip the identity gate: serve adds no
    headers to it."""
    proxies, other, funnel = [], [], any((cfg.get("AllowFunnel") or {}).values())
    for port, tcp in (cfg.get("TCP") or {}).items():
        if (tcp or {}).get("TCPForward"):
            other.append(f"tcp:{port} -> {tcp['TCPForward']}")
    for host, web in (cfg.get("Web") or {}).items():
        for path, h in ((web or {}).get("Handlers") or {}).items():
            if (h or {}).get("Proxy"):
                proxies.append(h["Proxy"])
            else:
                other.append(f"{host}{path} -> {', '.join(sorted(h or {})) or 'handler'}")
    for nested in [*(cfg.get("Foreground") or {}).values(), *(cfg.get("Services") or {}).values()]:
        p, o, f = serve_entries(nested or {})
        proxies += p; other += o; funnel = funnel or f
    return proxies, other, funnel


def proxy_hosts(cfg: dict) -> dict:
    """Top-level serve web handlers: proxy target -> the hostnames (without
    the port) it is served under. Serve answers by name, so a handler under a
    name this machine no longer has serves nothing."""
    out = {}
    for hostport, web in (cfg.get("Web") or {}).items():
        host = hostport.rsplit(":", 1)[0]
        for h in ((web or {}).get("Handlers") or {}).values():
            if (h or {}).get("Proxy"):
                out.setdefault(h["Proxy"].rstrip("/"), []).append(host)
    return out


def facts_from(st: dict, serve: dict) -> dict:
    """`tailscale status` and `tailscale serve status`, boiled down."""
    me = st.get("Self") or {}
    user = (st.get("User") or {}).get(str(me.get("UserID", "")), {})
    proxies, other, funnel = serve_entries(serve)
    return {"state": st.get("BackendState", ""),
            "login": user.get("LoginName", ""),
            "dns": (me.get("DNSName") or "").rstrip("."),
            "https": bool(st.get("CertDomains")),
            "magicdns": bool((st.get("CurrentTailnet") or {}).get("MagicDNSEnabled")),
            "proxies": proxies, "other": other, "funnel": funnel,
            "hosts": proxy_hosts(serve)}


def tailscale_facts(ts: str) -> dict:
    """What `tailscale status` and `tailscale serve status` say, boiled down."""
    return facts_from(tailscale_json(ts, "status"), tailscale_json(ts, "serve", "status"))


def viz_targets(port: str) -> set:
    return {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}


def serves_viz(proxies: list, port: str) -> bool:
    """Serve hands tailnet requests to the dashboard's loopback port."""
    return any(p.rstrip("/") in viz_targets(port) for p in proxies)


def viz_hosts(facts: dict, port: str) -> list:
    """The names serve hands the dashboard out under."""
    return sorted({h for target in viz_targets(port) for h in facts["hosts"].get(target, [])})

"""Whether a dashboard message is verifiably the owner's -- enough to count
as their word to the operator, exactly as their own Slack message does
(operator_role.Grants).

Reading and chatting on the dashboard asks less (visualizer.tailnet_refusal,
cross_site_refusal); a grant lets the operator accept, dismiss, file past the
cap and run planned ops, so the bar is every one of these, failing closed on
any it cannot establish:

  * it came through `tailscale serve`, carrying a Tailscale-User-Login equal
    to VIZ_TAILSCALE_LOGIN -- a plain loopback request (the CLI, an SSH
    tunnel, an agent's curl) is no one in particular;
  * it passed the dashboard's cross-site checks (JSON, same origin);
  * its peer -- the X-Forwarded-For serve sets from the connection, never
    from what the client sent -- is a tailnet address that is NOT one of this
    machine's own. Agents run on the mini and can reach the tailnet URL; from
    there serve would stamp the owner's login on their requests too;
  * Tailscale itself, asked who that peer is (`tailscale whois`), names the
    owner's login -- and says which device, for the record;
  * and the TCP connection was opened by tailscaled running as root (whose
    own CLI, beside it, is the one asked -- never one found on PATH). Serve
    proxies to the dashboard over loopback, so headers alone could be typed
    by any process on the mini; the process on the other end of the socket
    cannot be: an agent runs as the user, never as root.

The bot takes the dashboard's word for this only from the dashboard itself
(from_visualizer): its /web-message route is loopback too, and any process
on the mini could post a `dashboard_owner` there. So the bot asks, the same
way, who opened the connection carrying one -- and it must be the process
launchd runs as the visualizer's job (VIZ_LABEL), not one an agent started.
A key the two shared would not do: agents run as the same user, and read
whatever file the visualizer could.

The probes (this machine's tailnet addresses, whois, the socket's owner,
the visualizer's job) are a `Probe`, so tests stand in for the machine.
"""

from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import threading
import time


#: Tailscale's address ranges (CGNAT v4 and its ULA v6 prefix).
TAILNET_NETS = (ipaddress.ip_network("100.64.0.0/10"),
                ipaddress.ip_network("fd7a:115c:a1e0::/48"))
#: The process `tailscale serve` proxies from, and the user it must run as.
SERVE_PROCESS = "tailscaled"
SERVE_UID = 0
#: Each probe's limit: a hung tailscale or netstat costs a send this, not more.
PROBE_TIMEOUT_S = 4
#: How long this machine's own tailnet addresses are trusted before re-reading.
SELF_IPS_TTL_S = 60

NETSTAT = "/usr/sbin/netstat"
PS = "/bin/ps"
#: By full path: PATH starts in directories any agent can write.
LAUNCHCTL = "/bin/launchctl"
#: The visualizer's launchd job (bin/silkworm LaunchdManager.SERVICES).
VIZ_LABEL = "com.silkworm.viz"


def peer_of(headers) -> str:
    """The address serve says the request came from, or "".

    Serve sets X-Forwarded-For to the connection's source (not appended to
    one the client sent), so it is one address; anything else is not serve's."""
    raw = (headers.get("X-Forwarded-For") or "").strip()
    if not raw or "," in raw:
        return ""
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return ""


def in_tailnet(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr.version == n.version and addr in n for n in TAILNET_NETS)


def _endpoint(host: str, port) -> str:
    """How macOS netstat writes an address and port: `127.0.0.1.8790`."""
    return f"{host}.{port}"


#: Columns netstat -anv prints after process:pid (state, options, gencnt,
#: flags, flags1, usecnt, rtncnt, fltrs).
AFTER_PID = 8


def socket_owner(netstat_out: str, client, server) -> int:
    """The pid owning the client end of the connection `client` -> `server`
    in `netstat -anv -p tcp` output, or 0. Its line has the client as the
    local address and the server as the foreign one.

    The pid is read counting from the END of the line: the process name
    before it is the executable's file name, which whoever runs it chooses
    -- spaces, colons and digits included ("c:321 d" prints `c:321 d:4242`)
    -- so nothing read left to right past the state can be trusted."""
    local, foreign = _endpoint(*client[:2]), _endpoint(*server[:2])
    for line in netstat_out.splitlines():
        cols = line.split()
        if len(cols) < 11 + AFTER_PID or cols[3] != local or cols[4] != foreign:
            continue
        if cols[5] != "ESTABLISHED":
            continue
        _, sep, pid = cols[-(AFTER_PID + 1)].rpartition(":")
        if sep and pid.isdigit():
            return int(pid)
    return 0


class Probe:
    """The machine, as the check needs it: each read live (the addresses for
    a minute), and any failure an empty answer -- which refuses."""

    def __init__(self, run=None):
        self._run = run or self._subprocess
        self._lock = threading.Lock()
        self._self = (0.0, frozenset())

    @staticmethod
    def _subprocess(argv, timeout=10):
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout, r.stderr

    def _json(self, argv) -> dict:
        try:
            rc, out, _ = self._run(argv, PROBE_TIMEOUT_S)
            got = json.loads(out) if rc == 0 and (out or "").strip() else {}
        except Exception:
            return {}
        return got if isinstance(got, dict) else {}

    def self_ips(self, ts: str) -> frozenset:
        """This machine's own tailnet addresses, asked of the CLI `ts`."""
        with self._lock:
            at, ips = self._self
            if ips and time.time() - at < SELF_IPS_TTL_S:
                return ips
        if not ts:
            return frozenset()
        me = self._json([ts, "status", "--json"]).get("Self")
        got = me.get("TailscaleIPs") if isinstance(me, dict) else None
        ips = frozenset(str(ipaddress.ip_address(i))
                        for i in (got if isinstance(got, list) else ()) if _is_ip(i))
        with self._lock:
            self._self = (time.time(), ips)
        return ips

    def whois(self, ip: str, ts: str) -> dict:
        """{"login", "node"} the CLI `ts` gives for `ip`, or {}. Not through
        tailnet.tailscale_call: it puts --json last, and whois reads a flag
        after the address as a second argument and fails."""
        if not ts or not _is_ip(ip):
            return {}
        w = self._json([ts, "whois", "--json", ip])
        prof = w.get("UserProfile") if isinstance(w.get("UserProfile"), dict) else {}
        login = str(prof.get("LoginName") or "")
        node = w.get("Node") if isinstance(w.get("Node"), dict) else {}
        name = str(node.get("ComputedName") or node.get("Name") or "").rstrip(".")
        return {"login": login, "node": name} if login else {}

    def connector(self, client, server) -> dict:
        """{"pid", "name", "uid", "path"} of the process that opened the
        connection from `client` to `server` (both (host, port)), or {}."""
        try:
            rc, out, _ = self._run([NETSTAT, "-anv", "-p", "tcp"], PROBE_TIMEOUT_S)
            if rc != 0:
                return {}
            pid = socket_owner(out, client, server)
            if not pid:
                return {}
            rc, out, _ = self._run([PS, "-o", "uid=,comm=", "-p", str(pid)], PROBE_TIMEOUT_S)
            if rc != 0 or not out.strip():
                return {}
            uid, comm = out.split(None, 1)
            return {"pid": pid, "uid": int(uid), "name": os.path.basename(comm.strip()),
                    "path": comm.strip()}
        except Exception:
            return {}

    def job_pid(self, label: str) -> int:
        """The pid launchd says its job `label` runs as in this user's GUI
        domain, or 0 (not loaded, not running, launchctl failing, or loaded
        from anywhere but its plist in ~/Library/LaunchAgents)."""
        try:
            rc, out, _ = self._run([LAUNCHCTL, "print", f"gui/{os.getuid()}/{label}"],
                                   PROBE_TIMEOUT_S)
        except Exception:
            return 0
        if rc != 0:
            return 0
        return job_pid_of(out or "", launch_agent(label))

    @staticmethod
    def cli_beside(daemon_path: str) -> str:
        """The tailscale CLI installed beside the running tailscaled, or "".
        Not the one on PATH: PATH starts in directories any agent can write
        (~/.local/bin), and a stand-in there could answer whois as it liked."""
        if not daemon_path or not os.path.isabs(daemon_path):
            return ""
        cli = os.path.join(os.path.dirname(daemon_path), "tailscale")
        return cli if os.path.isfile(cli) and os.access(cli, os.X_OK) else ""


def launch_agent(label: str) -> str:
    """Where `silkworm install` writes the job's plist."""
    return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", f"{label}.plist")


def job_pid_of(launchctl_print: str, plist: str) -> int:
    """The job's own `pid = N` in `launchctl print` output -- the line one
    tab in, not one nested deeper -- or 0, unless the job was loaded from
    `plist`. Any process of the user's can take a free label (`launchctl
    submit -l`, or bootstrap a plist of its own after a bootout); launchd
    says where each job came from, and only the installed plist is the
    visualizer -- writing that is editing the install, not posting to it."""
    lines = launchctl_print.splitlines()
    if f"\tpath = {plist}" not in lines:
        return 0
    for line in lines:
        if line.startswith("\tpid = "):
            raw = line[len("\tpid = "):].strip()
            return int(raw) if raw.isdigit() and int(raw) > 0 else 0
    return 0


def from_visualizer(client, server, probe) -> tuple[bool, str]:
    """(True, "") when the loopback connection from `client` to `server`
    (both (host, port)) was opened by the visualizer's launchd job, else
    (False, why not) -- failing closed on anything it cannot establish. The
    bot asks this before it takes a /web-message's `dashboard_owner` as the
    dashboard's word rather than a claim anyone on the mini could post."""
    try:
        client, server = tuple(client)[:2], tuple(server)[:2]
    except TypeError:
        return False, "no connection to check"
    if len(client) != 2 or len(server) != 2:
        return False, "no connection to check"
    job = probe.job_pid(VIZ_LABEL)
    if not job:
        return False, f"the visualizer's launchd job ({VIZ_LABEL}) is not running"
    pid = (probe.connector(client, server) or {}).get("pid") or 0
    if not pid:
        return False, "could not tell which process opened the connection"
    if pid != job:
        return False, (f"sent by pid {pid}, not the visualizer ({VIZ_LABEL}, pid {job}): "
                       f"only the dashboard vouches for who sent a message")
    return True, ""


def _is_ip(raw) -> bool:
    try:
        ipaddress.ip_address(str(raw))
        return True
    except ValueError:
        return False


def verify(headers, client, server, *, allowed: str, forged: str, proxied: bool,
           login: str, probe) -> tuple[dict | None, str]:
    """({"login", "peer", "node"}, "") when this request is verifiably the
    owner's, else (None, why not). `forged` is what the dashboard's
    cross-site gate said of it ("" for passed); `login` the
    Tailscale-User-Login it carried, decoded; `allowed` VIZ_TAILSCALE_LOGIN
    ("" matches no one). The tailnet gate (visualizer.tailnet_refusal) has
    already refused anything else proxied before a route runs. Cheap checks
    first: the probes run only for a request that is the owner's on its face."""
    if not proxied:
        return None, "not through tailscale serve (a loopback request is no one in particular)"
    if not login or login.lower() != allowed.lower():
        return None, "not the owner's Tailscale login"
    if forged:
        return None, forged
    peer = peer_of(headers)
    if not peer or not in_tailnet(peer):
        return None, "no tailnet peer address from serve"
    who = probe.connector(client, server)
    if who.get("name") != SERVE_PROCESS or who.get("uid") != SERVE_UID:
        return None, "the connection was not opened by tailscaled (root)"
    ts = probe.cli_beside(who.get("path") or "")
    if not ts:
        return None, "no tailscale CLI beside the running tailscaled to ask"
    mine = probe.self_ips(ts)
    if not mine:
        return None, "could not read this machine's own tailnet addresses"
    if peer in mine:
        return None, "sent from this machine itself (an agent can reach the tailnet URL too)"
    w = probe.whois(peer, ts)
    if not w or w.get("login", "").lower() != allowed.lower():
        return None, "Tailscale does not name the owner as that peer's user"
    return {"login": login, "peer": peer, "node": w.get("node") or ""}, ""


def owner(raw, allowed: str) -> dict | None:
    """The bot's reading of what the dashboard vouched for: {login, peer,
    node} if well formed and the login is VIZ_TAILSCALE_LOGIN, else None."""
    if not isinstance(raw, dict) or not allowed:
        return None
    login = str(raw.get("login") or "").strip()
    peer = str(raw.get("peer") or "").strip()
    node = str(raw.get("node") or "").strip()
    if not login or login.lower() != allowed.strip().lower():
        return None
    if not in_tailnet(peer):
        return None
    return {"login": login[:200], "peer": peer, "node": node[:200]}

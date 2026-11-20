"""Rebuild tailscale.json -- the `tailscale` health probe's fixtures -- from raw
captures, with every key, address and identity scrubbed (this repo is public).

Captured on the mini, 2026-10-25, tailscale 1.104.1 (Homebrew):

  status_running.json  `tailscale status --json`, the real daemon
  serve_live.json      `tailscale serve status --json`, the real daemon
  status_fresh.json    `tailscale --socket=S status --json` against a scratch
                       `tailscaled --tun=userspace-networking --socket=S
                       --statedir=D` that has never logged in
  nodaemon.err         the same, after that scratch daemon was killed (rc 1)

So the real tailnet was never touched. Two states could not be captured
without taking the mini off its tailnet or turning Funnel on, so they are
derived from the live captures and say so: "stopped" (status_running with
BackendState Stopped -- what `tailscale down` leaves a logged-in node in) and
"serve_funnel" (serve_live plus the AllowFunnel map `tailscale funnel` adds).

    python3 tests/fixtures/health/record_tailscale.py /tmp/tsfix
"""

import json
import re
import sys
from pathlib import Path

#: The tailnet's name is not a secret, but it is not this repo's either. Read
#: from the capture's MagicDNSSuffix (tailXXXX.ts.net) by main(), never typed here.
TAILNET = ["", "tail0000"]


def tailnet_of(running):
    return (running.get("MagicDNSSuffix") or "").split(".", 1)[0]


def scrub(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ("PublicKey", "PrimaryRoutes"):
                v = "nodekey:[redacted]" if v else v
            elif k in ("Addrs", "AllowedIPs", "PeerAPIURL"):
                v = [] if v else v
            elif k in ("TailscaleIPs",):
                v = ["100.64.0.1"] if v else v
            elif k in ("ID", "StableID", "NodeID", "UserID"):
                v = (0 if isinstance(v, int) else "[id]") if v else v
            elif k == "Name" and v:              # CurrentTailnet: the account
                v = "example.github"
            elif k == "LoginName" and v:
                v = "owner@example.com"
            elif k in ("DisplayName", "ProfilePicURL") and v:
                v = "[redacted]"
            elif k == "Relay":
                v = ""
            elif k == "CapMap":
                v = {}
            out[scrub(k)] = scrub(v)
        return out
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    if isinstance(obj, str):
        obj = obj.replace(*TAILNET) if TAILNET[0] else obj
        for real in sorted(NAMES, key=len, reverse=True):
            obj = obj.replace(real, NAMES[real])
        return obj
    return obj


#: Machine names -> placeholders, filled in by machine_names() from the raw
#: captures themselves, so the real names are never written into this file.
NAMES = {}


def machine_names(running, serve, fresh):
    """The mini's tailnet and OS names (current, and the stale one serve still
    holds), and the kept peer's, each mapped to a stand-in."""
    label = lambda dns: (dns or "").split(".", 1)[0]
    names = {}
    me = running.get("Self") or {}
    names[label(me.get("DNSName"))] = "mac-mini"
    names[me.get("HostName") or ""] = "Mac-mini"
    for i, peer in enumerate((running.get("Peer") or {}).values(), 1):
        names.setdefault(label(peer.get("DNSName")), f"phone{i}")
        if (peer.get("HostName") or "localhost") != "localhost":
            names.setdefault(peer["HostName"], f"Phone{i}")
    for host in serve.get("Web") or {}:
        names.setdefault(label(host), "old-mini")
    names.setdefault(label((fresh.get("Self") or {}).get("HostName")), "Old-Mini")
    names.pop("", None)
    return names


def main(raw: Path):
    read = lambda name: json.loads((raw / name).read_text())
    TAILNET[0] = tailnet_of(read("status_running.json"))
    assert TAILNET[0].startswith("tail"), TAILNET[0]
    NAMES.update(machine_names(read("status_running.json"), read("serve_live.json"),
                               read("status_fresh.json")))
    load = lambda name: scrub(read(name))
    running = load("status_running.json")
    # One peer is enough to show the shape; the rest are other people's machines.
    peers = running.get("Peer") or {}
    running["Peer"] = {"nodekey:[redacted]": next(iter(peers.values()))} if peers else peers
    # The user map is keyed by the user id Self points at, scrubbed to 0 above.
    running["User"] = {"0": next(iter(running["User"].values()))} \
        if running.get("User") else running.get("User")
    serve_live = load("serve_live.json")
    stopped = json.loads(json.dumps(running))
    stopped["BackendState"] = "Stopped"
    funnel = json.loads(json.dumps(serve_live))
    funnel["AllowFunnel"] = {host: True for host in serve_live.get("Web") or {}}
    err = re.sub(r"pid \d+", "pid 0", (raw / "nodaemon.err").read_text().strip())
    fixture = {
        "_recorded": __doc__.split("\n\n")[1].strip(),
        "status_running": running,
        "serve_live": serve_live,
        "status_needs_login": load("status_fresh.json"),
        "serve_empty": {},
        "daemon_down_stderr": err.replace(str(raw), "/tmp/tsfix"),
        "_derived": "status_stopped and serve_funnel are derived; see record_tailscale.py",
        "status_stopped": stopped,
        "serve_funnel": funnel,
    }
    out = Path(__file__).with_name("tailscale.json")
    out.write_text(json.dumps(fixture, indent=1, sort_keys=True) + "\n")
    blob = out.read_text()
    raw_text = "".join((raw / n).read_text() for n in ("status_running.json", "status_fresh.json"))
    ids = set(re.findall(r'"(?:UserID|ID|NodeID)": "?(\w{6,})', raw_text))
    # Keys, public and LAN addresses, the tailnet, the account, every id,
    # and every machine's name.
    for bad in (r"nodekey:[0-9a-f]{8}", r"\b(?:47|192)\.\d+\.\d+\.\d+",
                re.escape(tailnet_of(read("status_running.json"))),
                *(re.escape(u[k].split("@")[0]) for u in
                  (read("status_running.json").get("User") or {}).values()
                  for k in ("LoginName",) if u.get(k)),
                "@github", *ids, *map(re.escape, machine_names(
                    read("status_running.json"), read("serve_live.json"),
                    read("status_fresh.json")))):
        assert not re.search(bad, blob, re.I), bad
    print(f"wrote {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))

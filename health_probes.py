"""Project-specific health probes, plugged into health.py's registry.

health.py's first check, "ci", reads GitHub. These read the things that run
outside it, each a lesson already paid for:

  trader   dispatching  -- the paper daemon wrote a dispatch line for the
                           latest scheduled NYSE slot. On 2026-10-15/16 it
                           wrote none for two days while every existing check
                           (PID, Gateway port, equity snapshot) stayed green.
           gateway      -- IB Gateway's API port listening, and the daemon
                           actually connected to it.
           healthcheck  -- the latest result of trader's own
                           scripts/paper_healthcheck.py, read from its log.
  backends backend-drift -- deployed edge functions and applied migrations
                           against what is on the base branch (Supabase).
  apps     app-store    -- the App Store version and any open review.
           testflight   -- the latest build's processing state.
  mini     credentials  -- claude's credential, Apple certificates/profiles,
                           the App Store Connect key.
           disk         -- free space on the data volume.
           tailscale    -- the phone can reach the dashboard: tailscaled
                           running and logged in, MagicDNS and HTTPS on, serve
                           forwarding this machine's name to the visualizer,
                           Funnel off. On 2026-10-25 the phone showed the
                           offline page three days after a reboot and nothing
                           here had noticed.

Rules every probe keeps, because a monitor that breaks them is worse than
none:

  * Read-only. Nothing here deploys, re-runs a job that pages, or writes
    anywhere but its result. The trader's healthcheck is read from its log,
    never run (running it pages); the trader's own code is imported only for
    pure functions -- the heartbeat parser and verdict, the daemon/Gateway
    checks -- in a subprocess under the trader's interpreter.
  * Not being able to look is `unknown`, never `fail`: offline, a refused or
    missing token, a tool not installed, a timeout. Only the thing looked at
    being wrong fails.
  * No secret in any summary, evidence line, link or log: credentials are
    read to be *used*, and every result passes through `scrub`.
  * Bounded: every external call has a timeout, and each probe a total
    budget (`Budget`), so one slow API costs that probe, not the pass.

Which project is which external thing is health.toml's explicit table, not
inference. All I/O goes through a `System` a probe is built with, so tests
replay recorded responses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import plistlib
import re
import shutil
import socket
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import credentials
import envfile
import health
import releases
import tailnet
from health import FAIL, OK, UNKNOWN, WARN, result, unknown

log = logging.getLogger("silkworm.health")

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "health.toml"

#: The pseudo-project the machine's own checks are filed under.
#: Registered as a real, repo-less project at startup (bot.register_mini), so
#: its items render on the board and a reply to one runs in Silkworm's
#: checkout, where the credential and disk tooling lives; this is the record
#: a pass uses only until then.
MINI = {"slug": "mini", "title": "Mac mini", "scope": {}}

#: Seconds one probe may spend in total, across all its calls.
PROBE_BUDGET_S = 90
#: Expiry thresholds (credentials): warn inside 14 days, fail inside 3.
EXPIRY_WARN_D, EXPIRY_FAIL_D = 14, 3
#: Free space on the data volume: warn under 15%, fail under 5%.
DISK_WARN, DISK_FAIL = 0.15, 0.05
#: An App Store review waiting longer than this warns.
REVIEW_WARN_D = 3
#: A TestFlight build still processing after this long warns.
PROCESSING_WARN_H = 2
#: Backend drift (landed, not released) older than this fails; younger warns.
DRIFT_FAIL_D = 7
#: A deploy within this long before a commit still counts as of that commit
#: (a function deployed from the branch moments before it landed).
DRIFT_SLACK_S = 600
#: Clock skew allowed between Supabase's updated_at and a release tag cut on
#: this machine, when matching a deploy to the release that made it.
RELEASE_SKEW_S = 60


class Unavailable(Exception):
    """Could not look. The message is the reason, for an `unknown` result."""


# --- secrets ---------------------------------------------------------------------

_SECRETS = [
    re.compile(r"sbp_[A-Za-z0-9]{8,}"),                 # Supabase management token
    re.compile(r"sb_(?:secret|publishable)_[A-Za-z0-9_-]{8,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),            # Anthropic / claude token
    re.compile(r"xox[abposr]-[A-Za-z0-9-]{8,}"),         # Slack
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # a JWT
    re.compile(r"(?i)(bearer\s+)[^\s\"']+"),
    re.compile(r"\b(?:nodekey|mkey|discokey|nlpub|privkey):[0-9a-f]{8,}"),  # Tailscale keys
    re.compile(r"tskey-[A-Za-z0-9-]{8,}"),               # a Tailscale auth key
    re.compile(r"(login\.tailscale\.com/a/)[A-Za-z0-9]+"),  # a login link
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
]


def scrub(text, known=()) -> str:
    """`text` with anything shaped like a credential -- and any value in
    `known`, the credentials a probe actually read -- replaced."""
    out = str(text or "")
    for value in known:
        if value and len(value) >= 6:
            out = out.replace(value, "[redacted]")
    for pat in _SECRETS:
        out = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "[redacted]", out)
    return out


def _safe(res: dict, known=()) -> dict:
    return result(res["status"], scrub(res.get("summary"), known),
                  [scrub(e, known) for e in res.get("evidence") or ()],
                  scrub(res.get("link"), known), scrub(res.get("fingerprint"), known))


# --- the system a probe looks at -----------------------------------------------

class System:
    """Every outside thing a probe touches. Tests replace it with recordings."""

    def home(self) -> Path:
        return Path.home()

    def now(self) -> float:
        return time.time()

    def clock(self) -> float:
        return time.monotonic()

    def env(self, name: str) -> str | None:
        return os.environ.get(name)

    def run(self, argv, timeout: float, cwd=None) -> tuple[int, str, str]:
        try:
            r = subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                               timeout=timeout, cwd=cwd, errors="replace",
                               stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise Unavailable(f"{Path(str(argv[0])).name} is not installed here")
        except subprocess.TimeoutExpired:
            raise Unavailable(f"{Path(str(argv[0])).name} timed out after {timeout:g}s")
        except OSError as e:
            raise Unavailable(f"{Path(str(argv[0])).name} could not run: {e.strerror or e}")
        return r.returncode, r.stdout, r.stderr

    def http_get(self, url: str, headers: dict, timeout: float) -> tuple[int, str]:
        req = urllib.request.Request(url, headers={"User-Agent": "silkworm-health", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as f:
                return f.status, f.read(4_000_000).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read(2000).decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise Unavailable(f"{url.split('/')[2]} unreachable: "
                              f"{getattr(e, 'reason', None) or e}")

    def read_text(self, path) -> str:
        return Path(path).read_text(errors="replace")

    def read_tail(self, path, n: int) -> str:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - n))
            return f.read().decode("utf-8", "replace")

    def read_bytes(self, path) -> bytes:
        return Path(path).read_bytes()

    def exists(self, path) -> bool:
        return Path(path).exists()

    def connect(self, host: str, port: int, timeout: float) -> bool:
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True
        except OSError:
            return False

    def disk_usage(self, path):
        return shutil.disk_usage(path)


SYSTEM = System()


class Budget:
    """A probe's total time. `left(cap)` is the timeout for its next call --
    at most `cap`, never more than what remains -- and raises Unavailable
    once it is spent, so a probe stuck behind slow calls reports unknown in
    bounded time rather than holding up every project after it."""

    def __init__(self, sys: System, seconds: float = PROBE_BUDGET_S):
        self.sys, self.deadline = sys, sys.clock() + seconds
        self.seconds = seconds

    def left(self, cap: float) -> float:
        rest = self.deadline - self.sys.clock()
        if rest < 1:
            raise Unavailable(f"ran out of its {self.seconds:g}s budget")
        return min(cap, rest)


def load_config(path=None) -> dict:
    try:
        with open(path or CONFIG, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def _cwd(rec: dict) -> Path | None:
    cwd = str(((rec or {}).get("scope") or {}).get("cwd") or "").strip()
    return Path(cwd).expanduser() if cwd else None


def _days(seconds: float) -> str:
    d = seconds / 86400
    return f"{d:.0f}d" if d >= 2 else f"{seconds / 3600:.0f}h" if seconds >= 3600 \
        else f"{max(0, seconds) / 60:.0f}m"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _parse_time(text) -> float | None:
    """An ISO 8601 time from an API ("2026-10-09T15:23:59.706Z",
    "2027-08-14T18:28:47.000+00:00") as epoch seconds, or None."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def combine(parts) -> str:
    """Several sub-checks' statuses as one: fail, else warn, else unknown
    (what could not be looked at is said, not called fine), else ok."""
    parts = list(parts)
    for s in (FAIL, WARN, UNKNOWN):
        if s in parts:
            return s
    return OK


# --- trader: the paper daemon ----------------------------------------------------

#: Run under the trader's interpreter, in its checkout: its own pure functions,
#: answered as one JSON line. Nothing here pages or writes.
TRADER_SNIPPET = r"""
import json, re, sys
from datetime import datetime, timedelta, timezone
sys.path[:0] = ["scripts", "."]
out = {}
import paper_healthcheck as ph
from trader.runner import heartbeat as hb
grace = timedelta(minutes=float(sys.argv[1]))
out["label"], out["gateway"], out["log"] = ph.LABEL, [ph.GW_HOST, ph.GW_PORT], str(ph.DAEMON_LOG)
out["daemon"] = list(ph.check_daemon())
out["gateway_check"] = list(ph.check_gateway())
try:
    beats = hb.read_heartbeats(ph.DAEMON_LOG)
except OSError as e:
    out["log_error"] = f"{type(e).__name__}: {e}"
else:
    now = datetime.now(timezone.utc)
    ok, msg = hb.health_verdict(beats, now, grace=grace)
    out["verdict"] = [ok, msg]
    out["due"] = hb.last_due_slot(now, grace).astimezone(timezone.utc).isoformat()
    if beats:
        n = max(beats, key=lambda b: b.at)
        out["newest"] = {"at": n.at.isoformat(), "timing": n.timing, "live": n.live,
                         "total": n.total, "describe": n.describe()}
    out["min_live_ratio"] = hb.MIN_LIVE_RATIO
print("HEALTH_JSON " + json.dumps(out))
"""


def trader_state(rec: dict, cfg: dict, sys: System, budget: Budget) -> dict:
    """The trader's own reading of its daemon, Gateway and heartbeat."""
    cwd = _cwd(rec)
    if not cwd or not sys.exists(cwd):
        raise Unavailable("the trader's checkout is not on this machine")
    py = cwd / (cfg.get("python") or ".venv/bin/python3")
    if not sys.exists(py):
        raise Unavailable(f"the trader's interpreter {py.relative_to(cwd)} is missing")
    grace = float(cfg.get("dispatch_grace_minutes") or 60)
    rc, out, err = sys.run([py, "-c", TRADER_SNIPPET, f"{grace:g}"],
                           timeout=budget.left(60), cwd=str(cwd))
    line = next((ln for ln in out.splitlines() if ln.startswith("HEALTH_JSON ")), "")
    if not line:
        tail = (err or out).strip().splitlines()[-1:] or [f"exit {rc}"]
        raise Unavailable(f"the trader's heartbeat code could not run: {tail[0][:160]}")
    try:
        return json.loads(line[len("HEALTH_JSON "):])
    except json.JSONDecodeError:
        raise Unavailable("the trader's heartbeat code answered something that is not JSON")


def make_dispatching(sys: System = SYSTEM, config: dict | None = None):
    def probe(rec, gh):
        cfg = (config if config is not None else load_config()).get("trader") or {}
        try:
            st = trader_state(rec, cfg, sys, Budget(sys))
        except Unavailable as e:
            return unknown(str(e))
        if st.get("log_error"):
            return unknown(f"the daemon log {st.get('log')} could not be read: "
                           f"{st['log_error'][:120]}")
        ok, msg = (st.get("verdict") or [None, ""])[:2]
        newest, due = st.get("newest") or {}, st.get("due") or ""
        evidence = [f"newest dispatch: {newest.get('describe') or 'none in the log'}",
                    f"latest slot due (past its grace): {due[:16].replace('T', ' ')} UTC",
                    f"daemon: {(st.get('daemon') or ['', '?'])[1]}",
                    f"source: {st.get('log')}, trader.runner.heartbeat.health_verdict"]
        if ok is True:
            return result(OK, f"dispatched {newest.get('describe') or ''}".strip(), evidence)
        if ok is False:
            # The fingerprint is the newest heartbeat: the same outage is one
            # failure however long it lasts, and the next dispatch is news.
            return result(FAIL, msg, evidence, fingerprint=f"{newest.get('at') or 'none'}")
        return unknown("the trader's heartbeat verdict was missing")
    return probe


def lsof_connected(sys: System, pid: str, port: int, budget: Budget) -> bool | None:
    """Does process `pid` hold an ESTABLISHED TCP connection to `port`?
    None when lsof cannot say (not installed, failed)."""
    rc, out, _ = sys.run(["/usr/sbin/lsof", "-nP", "-a", "-p", pid, f"-iTCP:{port}",
                          "-sTCP:ESTABLISHED"], timeout=budget.left(15))
    if rc not in (0, 1):
        return None
    return any(f":{port}" in ln and "ESTABLISHED" in ln for ln in out.splitlines()[1:])


def netstat_clients(sys: System, port: int, budget: Budget) -> int | None:
    """ESTABLISHED client connections to `port` on this machine, any process."""
    rc, out, _ = sys.run(["/usr/sbin/netstat", "-anp", "tcp"], timeout=budget.left(15))
    if rc != 0:
        return None
    n = 0
    for ln in out.splitlines():
        cols = ln.split()
        if len(cols) >= 6 and cols[-1] == "ESTABLISHED" and cols[4].endswith(f".{port}"):
            n += 1
    return n


def make_gateway(sys: System = SYSTEM, config: dict | None = None):
    def probe(rec, gh):
        cfg = (config if config is not None else load_config()).get("trader") or {}
        budget = Budget(sys)
        try:
            st = trader_state(rec, cfg, sys, budget)
        except Unavailable as e:
            return unknown(str(e))
        host, port = (st.get("gateway") or ["127.0.0.1", 4002])[:2]
        g_ok, g_msg = (st.get("gateway_check") or [None, "?"])[:2]
        d_ok, d_msg = (st.get("daemon") or [None, "?"])[:2]
        evidence = [f"gateway: {g_msg}", f"daemon: {d_msg}"]
        if g_ok is False:
            return result(FAIL, f"IB Gateway's API port {host}:{port} is not listening",
                          evidence + ["ops/gateway_watchdog.sh relaunches it every 2 min; "
                                      "see data/ibc.log"], fingerprint="port-down")
        if d_ok is False and str(d_msg).startswith("launchctl failed"):
            return unknown(f"Gateway {host}:{port} is listening, but whether the daemon is "
                           f"running could not be read ({str(d_msg)[:100]})")
        if d_ok is False:
            return result(FAIL, f"the paper daemon {st.get('label')} is not running "
                                f"({d_msg}), so nothing is connected to the Gateway",
                          evidence, fingerprint="daemon-down")
        pid = (re.search(r"PID=(\d+)", str(d_msg)) or [None, None])[1]
        try:
            conn = lsof_connected(sys, pid, port, budget) if pid else None
            if conn:
                return result(OK, f"Gateway {host}:{port} listening; daemon (PID {pid}) "
                                  f"connected", evidence)
            clients = netstat_clients(sys, port, budget)
        except Unavailable as e:
            return unknown(f"Gateway {host}:{port} is listening, but whether the daemon is "
                           f"connected could not be read: {e}")
        if conn is None and clients is None:
            return unknown(f"Gateway {host}:{port} is listening, but neither lsof nor netstat "
                           "could say whether the daemon is connected")
        if clients == 0 or (conn is False and clients is None):
            # The daemon reconnects only when it dispatches (runner/base.py), so
            # between Gateway's nightly re-auth and the next slot -- and all
            # weekend -- no connection is the normal state. It is a failure
            # only once a slot has come due since the newest dispatch: then
            # the reconnect that should have happened did not.
            newest = (st.get("newest") or {}).get("at") or ""
            due = st.get("due") or ""
            missed = bool(due) and (not newest or _parse_time(newest) < _parse_time(due))
            line = (f"Gateway {host}:{port} is listening but the daemon (PID {pid}) is not "
                    f"connected to it")
            if missed:
                return result(FAIL, line + " and has missed a dispatch since",
                              evidence + [f"newest dispatch {newest or 'none'}, slot due {due}",
                                          "restart com.traderjoe.papertrade"],
                              fingerprint=f"disconnected:{pid}:{newest}")
            return result(WARN, line + " (it reconnects at its next dispatch)",
                          evidence + [f"newest dispatch {newest}, which covers the latest "
                                      f"due slot {due}"])
        return result(WARN, f"Gateway {host}:{port} has {clients} client connection(s), but "
                            f"none is attributed to the daemon (PID {pid})", evidence)
    return probe


_RUN_HEAD = re.compile(r"^paper healthcheck (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)")
_RUN_LINE = re.compile(r"^\s+\[(OK|FAIL)\] ([\w.-]+): (.*)$")


def healthcheck_runs(text: str) -> list[dict]:
    """paper_healthcheck.py's printed runs, oldest first:
    {at (local wall time, naive), results: [(name, ok, message)]}."""
    runs: list[dict] = []
    for line in (text or "").splitlines():
        m = _RUN_HEAD.match(line)
        if m:
            runs.append({"at": datetime.fromisoformat(m.group(1)), "results": []})
            continue
        m = _RUN_LINE.match(line)
        if m and runs:
            runs[-1]["results"].append((m.group(2), m.group(1) == "OK", m.group(3).strip()))
    return runs


def last_scheduled(slots, now_local: datetime) -> datetime | None:
    """The latest launchd StartCalendarInterval firing at or before `now_local`
    (naive local time), looking back a fortnight. Weekday 0 and 7 are Sunday."""
    best = None
    for back in range(15):
        day = (now_local - timedelta(days=back)).date()
        for s in slots or ():
            wd = s.get("Weekday")
            if wd is not None and int(wd) % 7 != (day.isoweekday() % 7):
                continue
            if "Day" in s and int(s["Day"]) != day.day:
                continue
            at = datetime(day.year, day.month, day.day, int(s.get("Hour", 0)),
                          int(s.get("Minute", 0)))
            if at <= now_local and (best is None or at > best):
                best = at
        if best is not None:
            return best
    return best


def make_healthcheck(sys: System = SYSTEM, config: dict | None = None):
    def probe(rec, gh):
        cfg = (config if config is not None else load_config()).get("trader") or {}
        label = cfg.get("healthcheck_label") or "com.traderjoe.paperhealth"
        plist_path = sys.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        cwd = _cwd(rec)
        if not sys.exists(plist_path) and cwd:
            plist_path = cwd / "ops" / f"{label}.plist"
        try:
            plist = plistlib.loads(sys.read_bytes(plist_path))
        except (OSError, plistlib.InvalidFileException, ValueError) as e:
            return unknown(f"the healthcheck's launchd job {label} could not be read "
                           f"({type(e).__name__})")
        logpath = plist.get("StandardOutPath") or ""
        if not logpath or not sys.exists(logpath):
            return unknown(f"the healthcheck's log {logpath or '(none in its plist)'} "
                           "is not there")
        try:
            runs = healthcheck_runs(sys.read_tail(logpath, 200_000))
        except OSError as e:
            return unknown(f"the healthcheck's log could not be read ({type(e).__name__})")
        complete = [r for r in runs if r["results"]]
        if not complete:
            return unknown("the healthcheck's log holds no completed run")
        last = complete[-1]
        now_local = datetime.fromtimestamp(sys.now())
        evidence, stale = [], ""
        slots = plist.get("StartCalendarInterval")
        if isinstance(slots, dict):
            slots = [slots]
        due = last_scheduled(slots, now_local - timedelta(hours=1)) if slots else None
        if due and last["at"] < due - timedelta(minutes=5):
            stale = (f"its latest run was {last['at']:%Y-%m-%d %H:%M}, but it was "
                     f"scheduled at {due:%Y-%m-%d %H:%M}")
        failing = [(n, m) for n, ok, m in last["results"] if not ok]
        evidence += [f"FAIL {n}: {m}" for n, m in failing]
        evidence.append(f"run {last['at']:%Y-%m-%d %H:%M} (local): "
                        f"{len(last['results']) - len(failing)} of "
                        f"{len(last['results'])} checks OK; {logpath}")
        if runs[-1] is not last:
            evidence.append(f"a later run ({runs[-1]['at']:%Y-%m-%d %H:%M}) printed no results")
        if stale:
            evidence.insert(0, stale)
        if failing:
            names = ", ".join(n for n, _ in failing)
            # Numbers blanked, as the healthcheck's own condition_key does: the
            # same stale universe one day older is the same failure.
            conditions = ";".join(sorted(f"{n}|{re.sub(r'[0-9]+', '#', m)}"
                                         for n, m in failing))
            fp = f"{','.join(sorted(n for n, _ in failing))}:" \
                 f"{hashlib.sha256(conditions.encode()).hexdigest()[:16]}"
            return result(FAIL, f"the trader's healthcheck fails: {names}"
                                + (" (and has stopped running)" if stale else ""),
                          evidence, fingerprint=fp)
        if stale:
            return result(WARN, f"the trader's healthcheck has not run since "
                                f"{last['at']:%Y-%m-%d %H:%M}", evidence)
        return result(OK, f"all {len(last['results'])} of the trader's healthcheck checks OK "
                          f"({last['at']:%Y-%m-%d %H:%M})", evidence)
    return probe


# --- Supabase backends -----------------------------------------------------------

SUPABASE_API = "https://api.supabase.com"
#: Files beside the functions that every function is built with.
SHARED_FILES = ("deno.json", "deno.jsonc", "import_map.json", "package.json")


def supabase_token(sys: System, ref: str) -> tuple[str, str]:
    """(account label, management token) for project `ref`, resolved as
    ~/.supabase/sb does: projects/<ref>.env names the account,
    accounts/<label>.env holds its token. Raises Unavailable, naming the file
    that is missing a setting -- never a value."""
    cred = sys.home() / ".supabase"
    project_env = cred / "projects" / f"{ref}.env"
    if not sys.exists(project_env):
        raise Unavailable(f"no credentials for Supabase project {ref} "
                          f"(~/.supabase/projects/{ref}.env is missing)")
    label = (envfile.value(project_env, "SUPABASE_ACCOUNT") or "").strip()
    if not label:
        raise Unavailable(f"~/.supabase/projects/{ref}.env does not say which account owns "
                          "it (SUPABASE_ACCOUNT=<label>)")
    account_env = cred / "accounts" / f"{label}.env"
    token = (envfile.value(account_env, "SUPABASE_ACCESS_TOKEN") or "").strip()
    if not token:
        raise Unavailable(f"~/.supabase/accounts/{label}.env has no SUPABASE_ACCESS_TOKEN")
    if not token.startswith("sbp_"):
        raise Unavailable(f"the token in ~/.supabase/accounts/{label}.env is not a "
                          "management token (sbp_…)")
    return label, token


def _api_message(body: str) -> str:
    try:
        data = json.loads(body)
        msg = data.get("message") or data.get("error") or data.get("msg") or ""
    except (json.JSONDecodeError, AttributeError):
        msg = body
    return " ".join(str(msg).split())[:120]


def supabase_get(sys: System, label: str, token: str, path: str, budget: Budget):
    status, body = sys.http_get(f"{SUPABASE_API}{path}",
                                {"Authorization": f"Bearer {token}"}, budget.left(20))
    if status in (401, 403):
        raise Unavailable(f"Supabase refused account {label}'s management token "
                          f"(HTTP {status}: {_api_message(body)})")
    if status != 200:
        raise Unavailable(f"Supabase answered HTTP {status} for {path.split('/')[-1]}: "
                          f"{_api_message(body)}")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise Unavailable(f"Supabase's {path.split('/')[-1]} answer is not JSON")


def _git(sys: System, repo, *args, budget: Budget) -> str:
    rc, out, err = sys.run(["git", "-C", str(repo), *args], timeout=budget.left(30))
    if rc != 0:
        raise Unavailable(f"git {args[0]} failed in {Path(repo).name}: "
                          f"{' '.join((err or '').split())[:120]}")
    return out


def base_branch(sys: System, repo, prefer: str, budget: Budget) -> str:
    """The base branch's most advanced copy, unfetched: origin's unless the
    local one is ahead of it (as worktrees.base_ref decides), asked through
    `sys` so tests replay it. Landing moves local main without pushing; a
    merge on the forge moves only origin's."""
    def exists(ref):
        rc, _, _ = sys.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", ref],
                           timeout=budget.left(15))
        return rc == 0
    for name in ([prefer] if prefer else []) + ["main", "master"]:
        local, remote = f"refs/heads/{name}", f"refs/remotes/origin/{name}"
        has_local, has_remote = exists(local), exists(remote)
        if has_local and has_remote:
            rc, _, _ = sys.run(["git", "-C", str(repo), "merge-base", "--is-ancestor",
                                local, remote], timeout=budget.left(15))
            return remote if rc == 0 else local
        if has_local or has_remote:
            return local if has_local else remote
    raise Unavailable(f"{Path(repo).name} has no base branch to read")


def make_backend_drift(sys: System = SYSTEM, config: dict | None = None):
    def probe(rec, gh):
        cfg = ((config if config is not None else load_config()).get("supabase") or {}) \
            .get(rec.get("slug")) or {}
        ref, sub = cfg.get("ref") or "", (cfg.get("dir") or "supabase").strip("/")
        known: list[str] = []
        try:
            return _safe(_drift(sys, rec, ref, sub, known), known)
        except Unavailable as e:
            return _safe(unknown(str(e)), known)
    return probe


def _drift(sys: System, rec, ref, sub, known) -> dict:
    budget = Budget(sys)
    if not ref:
        return unknown("no Supabase project ref for this project in health.toml")
    repo = _cwd(rec)
    if not repo or not sys.exists(repo):
        raise Unavailable("the project's checkout is not on this machine")
    branch = base_branch(sys, repo, ((rec.get("scope") or {}).get("branch") or "").strip(),
                         budget)
    short = branch.split("/", 2)[-1]
    listing = _git(sys, repo, "ls-tree", "--name-only", f"{branch}:{sub}/functions",
                   budget=budget) if _has_path(sys, repo, branch, f"{sub}/functions", budget) \
        else ""
    entries = [ln.strip() for ln in listing.splitlines() if ln.strip()]
    trees = set(_git(sys, repo, "ls-tree", "-d", "--name-only", f"{branch}:{sub}/functions",
                     budget=budget).split()) if entries else set()
    functions = sorted(e for e in entries if e in trees and not e.startswith("_"))
    # What every function is built with: the _shared folders and the config
    # beside them. A README there is not code.
    shared = [f"{sub}/functions/{e}" for e in entries
              if e.startswith("_") or e in SHARED_FILES]
    mig_listing = _git(sys, repo, "ls-tree", "--name-only", f"{branch}:{sub}/migrations",
                       budget=budget) if _has_path(sys, repo, branch, f"{sub}/migrations",
                                                   budget) else ""
    migrations = {}
    for name in mig_listing.split():
        if name.endswith(".sql") and "_" in name:
            migrations[name.split("_", 1)[0]] = name

    label, token = supabase_token(sys, ref)
    known.append(token)
    deployed = supabase_get(sys, label, token, f"/v1/projects/{ref}/functions", budget)
    applied = supabase_get(sys, label, token, f"/v1/projects/{ref}/database/migrations",
                           budget)
    if not isinstance(deployed, list) or not isinstance(applied, list):
        raise Unavailable("Supabase's answers were not lists")
    by_slug = {f.get("slug"): f for f in deployed if isinstance(f, dict)}
    applied_v = {str(m.get("version")) for m in applied if isinstance(m, dict)}

    now = sys.now()
    drift, notes, evidence = [], [], []         # drift: (since, line, fingerprint bit)

    def last_touch(*paths) -> float:
        # Author time: landing rebases, which rewrites committer time to the
        # moment it landed -- after a deploy made from the task's worktree.
        out = _git(sys, repo, "log", "-1", "--format=%at", branch, "--", *paths,
                   budget=budget).strip()
        return float(out) if out else 0.0

    releases = _release_tags(sys, repo, branch, budget)

    def release_for(path, at) -> str | None:
        # The release tag cut with this deploy. releases.py runs every
        # command, then tags, so the tag comes after the functions it pushed
        # -- by up to the rest of the release, each command allowed
        # COMMAND_TIMEOUT_S. A tag from before the deploy did not make it.
        # The first tag after the deploy wins.
        after = [(when, tag) for when, tag, covers, longest in releases
                 if covers(path) and at - RELEASE_SKEW_S <= when <= at + longest]
        return min(after)[1] if after else None

    def landed_since(tag, *paths) -> list[float]:
        # Ancestry, not time: whatever reached the base after the tag was not
        # in the tree the release deployed, however long ago it was authored.
        # First parent, so a merge landing is dated by the merge, not by its
        # branch's commits; committer time, which a rebase landing sets.
        out = _git(sys, repo, "log", "--first-parent", "--format=%ct", f"{tag}..{branch}",
                   "--", *paths, budget=budget).split()
        return sorted(float(t) for t in out)

    def added(path) -> float:
        out = _git(sys, repo, "log", "--diff-filter=A", "--format=%at", branch, "--", path,
                   budget=budget).split()
        return float(out[-1]) if out else 0.0

    for fn in functions:
        path = f"{sub}/functions/{fn}"
        dep = by_slug.get(fn)
        if dep is None:
            since = added(path)
            drift.append((since, f"function {fn}: on {short} since {_iso(since)}, never "
                                 f"deployed", f"new:{fn}"))
            continue
        if str(dep.get("status") or "ACTIVE") != "ACTIVE":
            drift.append((now, f"function {fn}: deployed but {dep.get('status')}",
                          f"status:{fn}:{dep.get('status')}"))
            continue
        at = float(dep.get("updated_at") or 0) / 1000
        tag = release_for(path, at)
        if tag:
            missing = landed_since(tag, path, *shared)
            if missing:
                drift.append((missing[0], f"function {fn}: deployed v{dep.get('version')} "
                                          f"{_iso(at)} as {tag}; {len(missing)} commit(s) "
                                          f"touching it landed on {short} since, first "
                                          f"{_iso(missing[0])}",
                              f"stale:{fn}:{dep.get('version')}"))
            else:
                evidence.append(f"function {fn}: v{dep.get('version')} deployed {_iso(at)} "
                                f"as {tag}, current with {short}")
            continue
        # No release explains the deploy (a manual one, or from a task's
        # worktree before it landed): the newest change's author time is all
        # there is to go on.
        changed = last_touch(path, *shared)
        if changed > at + DRIFT_SLACK_S:
            drift.append((changed, f"function {fn}: deployed v{dep.get('version')} "
                                   f"{_iso(at)}; {short} changed it {_iso(changed)}",
                          f"stale:{fn}:{dep.get('version')}"))
        else:
            evidence.append(f"function {fn}: v{dep.get('version')} deployed {_iso(at)}, "
                            f"current with {short}")
    for slug in sorted(set(by_slug) - set(functions)):
        notes.append(f"function {slug}: deployed (v{by_slug[slug].get('version')}) but not "
                     f"on {short}")

    if migrations and not applied_v:
        return unknown(f"production has no migration history to compare (the project has "
                       f"{len(migrations)} migration(s) on {short}, and "
                       "supabase_migrations.schema_migrations lists none; `sb migration "
                       "repair --status applied` baselines it)")
    for version in sorted(set(migrations) - applied_v):
        since = added(f"{sub}/migrations/{migrations[version]}")
        drift.append((since, f"migration {migrations[version]}: on {short} since "
                             f"{_iso(since)}, not applied in production", f"mig:{version}"))
    for version in sorted(applied_v - set(migrations)):
        notes.append(f"migration {version}: applied in production, not on {short}")

    link = f"https://supabase.com/dashboard/project/{ref}"
    lines = [d[1] for d in sorted(drift)] + notes + evidence
    if drift:
        oldest = min(d[0] for d in drift)
        age = now - oldest
        what = (f"{len(drift)} change(s) on {short} not in production, oldest "
                f"{_days(age)} ({', '.join(d[2].split(':')[1] for d in sorted(drift)[:3])}"
                f"{' …' if len(drift) > 3 else ''})")
        status = FAIL if age >= DRIFT_FAIL_D * 86400 else WARN
        return result(status, what + (f"; {len(notes)} deployed thing(s) not on {short}"
                                      if notes else ""),
                      lines, link, fingerprint=",".join(sorted(d[2] for d in drift)))
    if notes:
        return result(WARN, f"production runs {len(notes)} thing(s) not on {short} "
                            f"({notes[0].split(':')[0]}{' …' if len(notes) > 1 else ''})",
                      lines, link)
    return result(OK, f"{len(functions)} function(s) and {len(migrations)} migration(s) "
                      f"match {short}", lines, link)


def _release_tags(sys: System, repo, branch, budget) -> list:
    """(tagger date, tag, covers(path), longest) for every `<target>/vX.Y.Z`
    release tag of the targets in the base branch's `.silkworm/release.toml`
    (the format releases.py writes). covers(path) says whether a path is one
    of that target's; longest is how long its release can run. Empty when the
    project declares no targets, or declares them in a form releases.py would
    refuse -- drift then falls back to author time."""
    rc, text, _ = sys.run(["git", "-C", str(repo), "show",
                           f"{branch}:.silkworm/release.toml"], timeout=budget.left(15))
    if rc != 0:
        return []
    try:
        targets = tomllib.loads(text).get("targets") or {}
    except tomllib.TOMLDecodeError:
        return []
    if not isinstance(targets, dict):
        return []
    out = []
    for name, t in targets.items():
        if not isinstance(t, dict) or not re.fullmatch(r"[a-z][a-z0-9-]*", name):
            continue
        paths = t.get("paths") if isinstance(t.get("paths"), list) else []
        prefixes = [p.removeprefix("./").strip("/") for p in paths if isinstance(p, str)]
        prefixes = [p for p in prefixes if p]
        if not prefixes:
            continue
        commands = t.get("commands") if isinstance(t.get("commands"), list) else []
        longest = max(len(commands), 1) * releases.COMMAND_TIMEOUT_S

        def covers(path, prefixes=prefixes):
            return any(path == p or path.startswith(p + "/") for p in prefixes)
        listing = _git(sys, repo, "for-each-ref",
                       "--format=%(creatordate:unix) %(refname:short)",
                       f"refs/tags/{name}/", budget=budget)
        for row in listing.splitlines():
            when, _, tag = row.strip().partition(" ")
            if when.isdigit() and re.fullmatch(rf"{re.escape(name)}/v\d+\.\d+\.\d+", tag):
                out.append((float(when), tag, covers, longest))
    return out


def _has_path(sys: System, repo, branch, path, budget) -> bool:
    rc, _, _ = sys.run(["git", "-C", str(repo), "cat-file", "-e", f"{branch}:{path}"],
                       timeout=budget.left(15))
    return rc == 0


# --- App Store Connect -------------------------------------------------------------

def checkout_file(sys: System, rel: str) -> Path | None:
    """`rel` in Silkworm's checkout, else in its main checkout: a task worktree
    has no gitignored files (workspace/, .env); the main checkout does."""
    here = HERE / rel
    if sys.exists(here):
        return here
    try:
        rc, out, _ = sys.run(["git", "-C", str(HERE), "rev-parse", "--git-common-dir"],
                             timeout=10)
    except Unavailable:
        return None
    if rc == 0 and out.strip():
        common = Path(out.strip())
        common = common if common.is_absolute() else HERE / common
        main = common.resolve().parent / rel
        if sys.exists(main):
            return main
    return None


def asc_client(sys: System, cfg: dict) -> Path:
    rel = (cfg.get("asc") or {}).get("client") or "workspace/asc.mjs"
    found = checkout_file(sys, rel)
    if found is None:
        raise Unavailable(f"the App Store Connect client {rel} is not in Silkworm's checkout")
    return found


def asc_get(sys: System, client: Path, path: str, query: str, budget: Budget):
    """GET through asc.mjs, which prints `HTTP <status> …` then the JSON."""
    rc, out, err = sys.run(["node", client, "GET", path, query], timeout=budget.left(30))
    if rc != 0:
        tail = " ".join((err or out or "").split())[:160]
        raise Unavailable(f"the App Store Connect client failed: {tail or f'exit {rc}'}")
    head, _, body = out.partition("\n")
    m = re.match(r"HTTP (\d+)", head.strip())
    if not m:
        raise Unavailable("the App Store Connect client printed no status")
    status = int(m.group(1))
    try:
        data = json.loads(body) if body.strip() else None
    except json.JSONDecodeError:
        data = None
    if status in (401, 403):
        data = data if isinstance(data, dict) else {}
        detail = ((data.get("errors") or [{}])[0] or {}).get("detail") or ""
        raise Unavailable(f"App Store Connect refused the API key (HTTP {status}"
                          f"{': ' + detail[:100] if detail else ''})")
    if status != 200:
        raise Unavailable(f"App Store Connect answered HTTP {status} for {path}")
    if not isinstance(data, dict):
        raise Unavailable(f"App Store Connect's answer for {path} is not JSON")
    return data


#: An App Store version in one of these needs the developer to act.
VERSION_FAILED = ("REJECTED", "METADATA_REJECTED", "INVALID_BINARY",
                  "DEVELOPER_ACTION_NEEDED")
VERSION_IN_REVIEW = ("WAITING_FOR_REVIEW", "IN_REVIEW")
VERSION_LIVE = ("READY_FOR_SALE", "READY_FOR_DISTRIBUTION")
#: A review submission still open, by state.
SUBMISSION_OPEN = ("READY_FOR_REVIEW", "WAITING_FOR_REVIEW", "IN_REVIEW",
                   "UNRESOLVED_ISSUES")


def _app_id(rec: dict, cfg: dict) -> str:
    return str((cfg.get("apps") or {}).get(rec.get("slug") or "") or "")


def _human(state: str) -> str:
    return str(state or "?").lower().replace("_", " ")


def make_app_store(sys: System = SYSTEM, config: dict | None = None):
    def probe(rec, gh):
        cfg = config if config is not None else load_config()
        app = _app_id(rec, cfg)
        if not app:
            return unknown("no App Store Connect app id for this project in health.toml")
        budget = Budget(sys)
        try:
            client = asc_client(sys, cfg)
            versions = asc_get(sys, client, f"/v1/apps/{app}/appStoreVersions",
                               "?limit=10&filter[platform]=IOS&fields[appStoreVersions]=versionString,"
                               "appStoreState,appVersionState,createdDate,platform",
                               budget).get("data") or []
            subs = asc_get(sys, client, "/v1/reviewSubmissions",
                           f"?filter[app]={app}&filter[state]={','.join(SUBMISSION_OPEN)}"
                           "&limit=20", budget).get("data") or []
        except Unavailable as e:
            return _safe(unknown(str(e)))
        return _safe(app_store_verdict(app, versions, subs, sys.now()))
    return probe


def app_store_verdict(app: str, versions, subs, now: float) -> dict:
    link = f"https://appstoreconnect.apple.com/apps/{app}/distribution"
    vs = sorted((v for v in versions if isinstance(v, dict)),
                key=lambda v: _parse_time((v.get("attributes") or {}).get("createdDate")) or 0,
                reverse=True)
    rows = [(v.get("id"), v.get("attributes") or {}) for v in vs]
    ss = sorted((s for s in subs if isinstance(s, dict)),
                key=lambda s: _parse_time((s.get("attributes") or {}).get("submittedDate")) or 0,
                reverse=True)
    open_subs = [(s.get("id"), s.get("attributes") or {}) for s in ss
                 if (s.get("attributes") or {}).get("state") in SUBMISSION_OPEN]
    evidence = [f"{a.get('versionString')}: {a.get('appStoreState')}"
                f" (created {str(a.get('createdDate') or '')[:10]})" for _, a in rows[:4]]
    evidence += [f"review submission {(a.get('state') or '?')}"
                 f" (submitted {str(a.get('submittedDate') or 'not yet')[:10]})"
                 for _, a in open_subs]
    if not rows:
        return result(OK, "no App Store version yet", evidence, link)
    live = next((a for _, a in rows if a.get("appStoreState") in VERSION_LIVE), None)
    newest_id, newest = rows[0]
    state = newest.get("appStoreState") or ""
    tail = f"; {live.get('versionString')} live" if live and live is not newest else ""
    bad = [(i, a) for i, a in open_subs if a.get("state") == "UNRESOLVED_ISSUES"]
    if state in VERSION_FAILED or bad:
        why = _human(state) if state in VERSION_FAILED else "review has unresolved issues"
        return result(FAIL, f"{newest.get('versionString')} {why} — needs you in App Store "
                            f"Connect{tail}", evidence, link,
                      fingerprint=f"{newest_id}:{state}:{','.join(i for i, _ in bad)}")
    reviewing = [a for _, a in open_subs if a.get("state") in VERSION_IN_REVIEW]
    if state in VERSION_IN_REVIEW or reviewing:
        since = _parse_time((reviewing[0] if reviewing else {}).get("submittedDate"))
        waited = now - since if since else None
        what = f"{newest.get('versionString')} {_human(state if state in VERSION_IN_REVIEW else reviewing[0].get('state'))}"
        if waited is not None:
            what += f" for {_days(waited)} (submitted {_iso(since)[:10]})"
        if waited is not None and waited > REVIEW_WARN_D * 86400:
            return result(WARN, what + f" — longer than {REVIEW_WARN_D}d{tail}", evidence,
                          link)
        return result(OK, what + tail, evidence, link)
    if newest is live:
        return result(OK, f"{newest.get('versionString')} live", evidence, link)
    drafts = [a for _, a in open_subs if a.get("state") == "READY_FOR_REVIEW"]
    return result(OK, f"{newest.get('versionString')} {_human(state)}"
                      + (" (a review submission is drafted, not sent)" if drafts else "")
                      + (tail or ("; nothing live yet" if not live else "")),
                  evidence, link)


BUILD_FAILED = ("FAILED", "INVALID")


def make_testflight(sys: System = SYSTEM, config: dict | None = None):
    def probe(rec, gh):
        cfg = config if config is not None else load_config()
        app = _app_id(rec, cfg)
        if not app:
            return unknown("no App Store Connect app id for this project in health.toml")
        budget = Budget(sys)
        try:
            client = asc_client(sys, cfg)
            got = asc_get(sys, client, "/v1/builds",
                          f"?filter[app]={app}&sort=-uploadedDate&limit=1"
                          "&fields[builds]=version,processingState,uploadedDate,expired,"
                          "expirationDate&include=preReleaseVersion"
                          "&fields[preReleaseVersions]=version", budget)
        except Unavailable as e:
            return _safe(unknown(str(e)))
        return _safe(testflight_verdict(app, got, sys.now()))
    return probe


def testflight_verdict(app: str, got: dict, now: float) -> dict:
    link = f"https://appstoreconnect.apple.com/apps/{app}/testflight/ios"
    builds = [b for b in got.get("data") or () if isinstance(b, dict)]
    if not builds:
        return result(OK, "no builds uploaded", (), link)
    b = builds[0]
    a = b.get("attributes") or {}
    pre = next((i.get("attributes", {}).get("version") for i in got.get("included") or ()
                if i.get("type") == "preReleaseVersions"), None)
    name = f"build {a.get('version')}" + (f" ({pre})" if pre else "")
    up = _parse_time(a.get("uploadedDate"))
    state = a.get("processingState") or "?"
    evidence = [f"{name}: {state}, uploaded {str(a.get('uploadedDate') or '?')[:16]}"
                + (", expired" if a.get("expired") else
                   f", expires {str(a.get('expirationDate') or '?')[:10]}")]
    if state in BUILD_FAILED:
        return result(FAIL, f"{name} {_human(state)} in processing", evidence, link,
                      fingerprint=f"{b.get('id')}:{state}")
    if state == "PROCESSING":
        if up and now - up > PROCESSING_WARN_H * 3600:
            return result(WARN, f"{name} still processing after {_days(now - up)}", evidence,
                          link)
        return result(OK, f"{name} processing", evidence, link)
    return result(OK, f"{name} {_human(state)}" + (f", uploaded {_iso(up)[:10]}" if up else "")
                      + (" (expired)" if a.get("expired") else ""), evidence, link)


# --- the mini: credentials and disk ---------------------------------------------------

def _expiry_status(left_s: float) -> str:
    if left_s <= EXPIRY_FAIL_D * 86400:
        return FAIL
    if left_s <= EXPIRY_WARN_D * 86400:
        return WARN
    return OK


def _has_claude_token(sys: System) -> bool:
    if sys.env("CLAUDE_CODE_OAUTH_TOKEN"):
        return True
    env = checkout_file(sys, ".env")
    return bool(env and envfile.value(env, "CLAUDE_CODE_OAUTH_TOKEN"))


def make_credentials(sys: System = SYSTEM, config: dict | None = None,
                     claude_state=None):
    """claude_state(has_token) -> credentials.state(); injectable for tests."""
    def probe(rec, gh):
        cfg = config if config is not None else load_config()
        now = sys.now()
        parts, evidence, fps, summary = [], [], [], []
        # claude: credentials.py already knows where the credential is and when
        # its refresh token dies.
        try:
            st = (claude_state or (lambda t: credentials.state(has_token=t)))(
                _has_claude_token(sys))
        except Exception as e:                       # noqa: BLE001
            st = {"mode": "error", "error": type(e).__name__}
        mode = st.get("mode")
        if mode == "token":
            parts.append(OK)
            evidence.append("claude: long-lived token (CLAUDE_CODE_OAUTH_TOKEN); no refresh "
                            "expiry applies")
        elif mode == "oauth":
            left = float(st.get("hours_left") or 0) * 3600
            s = _expiry_status(left)
            parts.append(s)
            line = (f"claude: refresh token {'expired' if left <= 0 else 'expires in ' + _days(left)}"
                    f" ({st.get('source')})")
            evidence.append(line)
            if s != OK:
                summary.append(line)
                fps.append(f"claude:{int(st.get('expires_at') or 0)}")
            if len(st.get("stores") or []) > 1:
                parts.append(WARN)
                summary.append("claude credentials in two stores (Keychain and file)")
        elif mode == "unreadable" and st.get("source") == "keychain":
            # credentials.py reads None for any `security` failure -- a timeout,
            # rc 36 from a process that cannot reach the Keychain -- so this is
            # not being able to look, not a dead credential.
            parts.append(UNKNOWN)
            evidence.append("claude: the Keychain copy could not be read from here")
        elif mode in ("missing", "unreadable"):
            parts.append(FAIL)
            summary.append(f"claude credentials {mode}")
            evidence.append(f"claude: credentials {mode} ({st.get('source') or 'no store'})")
            fps.append(f"claude:{mode}")
        else:
            parts.append(UNKNOWN)
            evidence.append(f"claude: could not read the credential state ({st.get('error')})")

        # Apple: signing certificates and provisioning profiles. The ASC API
        # key itself has no expiry date; the calls succeeding is its check.
        budget = Budget(sys)
        try:
            client = asc_client(sys, cfg)
            certs = asc_get(sys, client, "/v1/certificates",
                            "?limit=200&fields[certificates]=name,certificateType,"
                            "expirationDate", budget).get("data") or []
            profiles = asc_get(sys, client, "/v1/profiles",
                               "?limit=200&fields[profiles]=name,profileType,profileState,"
                               "expirationDate", budget).get("data") or []
        except Unavailable as e:
            parts.append(UNKNOWN)
            evidence.append(f"apple: {e}")
            certs = profiles = None
        if certs is not None:
            evidence.append("apple: the App Store Connect API key authenticates "
                            "(team keys have no expiry date)")
            soonest = None
            for kind, rows in (("certificate", certs), ("profile", profiles)):
                for r in rows:
                    a = r.get("attributes") or {}
                    exp = _parse_time(a.get("expirationDate"))
                    if exp is None:
                        continue
                    left = exp - now
                    s = _expiry_status(left)
                    if kind == "profile":
                        # Xcode renews a profile itself; an expiring one is worth
                        # knowing, never a page. A certificate is not renewed.
                        s = WARN if s == FAIL else s
                    if kind == "profile" and a.get("profileState") not in (None, "ACTIVE"):
                        s = combine([s, WARN])
                        line = f"profile {a.get('name')}: {a.get('profileState')}"
                        summary.append(line)
                        evidence.append(line)
                        fps.append(f"profile:{r.get('id')}:{a.get('profileState')}")
                    if soonest is None or left < soonest[0]:
                        soonest = (left, kind, a.get("name"))
                    parts.append(s)
                    if s != OK and left <= EXPIRY_WARN_D * 86400:
                        line = (f"{kind} {a.get('name')}: "
                                f"{'expired' if left <= 0 else 'expires in ' + _days(left)}"
                                f" ({str(a.get('expirationDate'))[:10]})")
                        summary.append(line)
                        evidence.append(line)
                        fps.append(f"{kind}:{r.get('id')}")
            evidence.append(f"apple: {len(certs)} certificate(s), {len(profiles)} profile(s)"
                            + (f"; soonest to expire {soonest[1]} {soonest[2]} in "
                               f"{_days(soonest[0])}" if soonest else ""))
        status = combine(parts)
        if status == OK:
            text = "claude credential and Apple certificates/profiles all good for 14d+"
        elif status == UNKNOWN:
            text = "could not read every credential: " + "; ".join(
                e for e in evidence if "could not" in e or "refused" in e or "not " in e)[:200]
        else:
            text = "; ".join(summary) or "a credential needs attention"
        return _safe(result(status, text, evidence, fingerprint=",".join(sorted(fps))))
    return probe


def make_disk(sys: System = SYSTEM, config: dict | None = None):
    def probe(rec, gh):
        cfg = config if config is not None else load_config()
        path = (cfg.get("mini") or {}).get("disk") or "/System/Volumes/Data"
        try:
            du = sys.disk_usage(path)
        except OSError as e:
            return unknown(f"could not read free space on {path} ({type(e).__name__})")
        frac = du.free / du.total if du.total else 0.0
        line = (f"{path}: {du.free / 1e9:.0f} GB free of {du.total / 1e9:.0f} GB "
                f"({frac:.0%})")
        if frac < DISK_FAIL:
            return result(FAIL, line + f" — under {DISK_FAIL:.0%}", [line],
                          fingerprint=f"under{DISK_FAIL}")
        if frac < DISK_WARN:
            return result(WARN, line + f" — under {DISK_WARN:.0%}", [line])
        return result(OK, line, [line])
    return probe


#: Tailscale's states that mean the daemon is between states, not wrong.
TAILSCALE_PASSING = ("Starting", "NoState")


def make_tailscale(sys: System = SYSTEM, config: dict | None = None, find_bin=None):
    """The phone dashboard's path, as `silkworm tailscale status` reads it --
    tailnet.py is both -- minus the .env login, which is configuration this
    machine's reboot cannot change. `find_bin()` -> the CLI or "" (tests)."""
    def probe(rec, gh):
        port = str(sys.env("SILKWORM_VIZ_PORT") or "8790")
        ts = (find_bin or tailnet.tailscale_bin)()
        if not ts:
            return unknown("the tailscale CLI is not installed here")
        budget = Budget(sys)

        def run(argv, timeout):
            return sys.run(argv, budget.left(timeout))

        try:
            st = tailnet.tailscale_call(ts, "status", run=run)
        except Unavailable as e:
            return unknown(f"could not ask tailscale: {e}")
        except tailnet.TailscaleError as e:
            if e.daemon_down:
                return result(FAIL, "tailscaled is not running on the mini — the phone "
                              "cannot reach the dashboard",
                              [f"tailscale status: {e.text[:240]}"], fingerprint="down")
            return unknown(f"tailscale status failed (exit {e.rc}): {e.text[:160]}")
        try:
            serve = tailnet.tailscale_call(ts, "serve", "status", run=run)
        except Unavailable as e:
            return unknown(f"could not ask tailscale serve: {e}")
        except tailnet.TailscaleError as e:
            return unknown(f"tailscale serve status failed (exit {e.rc}): {e.text[:160]}")
        if not st.get("BackendState"):
            return unknown("tailscale status said nothing about its backend state")
        f = tailnet.facts_from(st, serve)
        state, dns = f["state"], f["dns"]
        hosts = tailnet.viz_hosts(f, port)
        evidence = [f"backend {state}" + (f", {dns}" if dns else ""),
                    f"MagicDNS {'on' if f['magicdns'] else 'off'}, HTTPS certificates "
                    f"{'on' if f['https'] else 'off'}",
                    "serve: " + (", ".join(f"{h} -> {t}" for t, hs in sorted(f["hosts"].items())
                                          for h in hs) or "nothing"),
                    f"Funnel {'ON' if f['funnel'] else 'off'}"]
        if state in TAILSCALE_PASSING:
            return result(UNKNOWN, f"tailscale is {state}; look again later", evidence)
        bad, fps = [], []
        # First: summaries are cut at 300 characters, and this one must survive.
        if f["funnel"]:
            bad.append("Funnel is on — the public internet can reach it")
            fps.append("funnel")
        if state == "Stopped":
            bad.append("tailscale is stopped (logged in, not connected)")
        elif state in ("NeedsLogin", "NeedsMachineAuth"):
            bad.append(f"tailscale is logged out ({state})")
        elif state != "Running":
            bad.append(f"tailscale is {state}")
        if state != "Running":
            fps.append(state)
        if state == "Running" and not (f["magicdns"] and f["https"]):
            bad.append("MagicDNS or HTTPS certificates are off")
            fps.append("dns")
        if not hosts:
            bad.append(f"serve does not forward to the dashboard (:{port})")
            fps.append("serve-missing")
        elif dns and dns not in hosts:
            bad.append(f"serve forwards {', '.join(hosts)}, but this machine is now {dns}")
            fps.append("serve-renamed:" + dns)
        others = [p for p in f["proxies"] if not tailnet.serves_viz([p], port)] + f["other"]
        if others:
            bad.append(f"serve also exposes {', '.join(others)[:120]}")
            fps.append("serve-extra")
        if bad:
            return result(FAIL, "; ".join(bad), evidence, fingerprint=",".join(fps))
        return result(OK, f"dashboard served at https://{dns}/ to the tailnet only", evidence)
    return probe


# --- registration -------------------------------------------------------------------

def _in(section: str):
    def applies(rec):
        return (rec.get("slug") or "") in ((load_config().get(section)) or {})
    return applies


def _is_trader(rec) -> bool:
    return (rec.get("slug") or "") == ((load_config().get("trader") or {}).get("project")
                                       or "trader")


def _is_mini(rec) -> bool:
    return (rec.get("slug") or "") == MINI["slug"]


def scrubbed(probe):
    """`probe`, with `scrub` over everything it returns -- every probe, so no
    one of them is the one that forgot."""
    def run(rec, gh):
        try:
            out = probe(rec, gh)
        except Exception as e:                       # noqa: BLE001 - unknown, scrubbed
            log.warning("health: a probe raised %s", type(e).__name__)
            return unknown(scrub(f"the probe raised {type(e).__name__}: {e}")[:200])
        if not isinstance(out, dict) or out.get("status") not in health.STATUSES:
            return out
        return _safe(out)
    run.__wrapped__ = probe
    return run


def register_all(sys: System = SYSTEM, config: dict | None = None,
                 registry: dict | None = None) -> dict:
    """Every probe here, registered into health.REGISTRY (or `registry`)."""
    target = health.REGISTRY if registry is None else registry
    probes = [
        ("dispatching", make_dispatching(sys, config), _is_trader,
         "the paper daemon dispatched for the latest scheduled NYSE slot"),
        ("gateway", make_gateway(sys, config), _is_trader,
         "IB Gateway's API port listening, and the paper daemon connected to it"),
        ("healthcheck", make_healthcheck(sys, config), _is_trader,
         "the latest run of the trader's scripts/paper_healthcheck.py, from its log"),
        ("backend-drift", make_backend_drift(sys, config), _in("supabase"),
         "deployed Supabase edge functions and migrations against the base branch"),
        ("app-store", make_app_store(sys, config), _in("apps"),
         "the App Store version's state and any open review"),
        ("testflight", make_testflight(sys, config), _in("apps"),
         "the latest TestFlight build's processing state"),
        ("credentials", make_credentials(sys, config), _is_mini,
         "claude's credential, Apple certificates/profiles and the ASC key"),
        ("disk", make_disk(sys, config), _is_mini,
         "free space on the mini's data volume"),
        ("tailscale", make_tailscale(sys, config), _is_mini,
         "the phone can reach the dashboard over Tailscale serve, tailnet-only"),
    ]
    for name, probe, applies, about in probes:
        probe = scrubbed(probe)
        decide, short = DECISIONS.get(name), SHORT.get(name)
        if registry is None:
            health.register(name, probe, applies, about, decision=decide, short=short)
        else:
            target[name] = health.Check(name, probe, applies, about, decide, short)
    return target


# --- what the brief says about each --------------------------------------------------
#
# Which troubles are the owner's to settle and which are code work, decided
# here per check rather than by the operator per morning. A release, an Apple
# review, a login or a credential is a decision -- stated, with the exact thing
# to do -- never a task: an implementor cannot release a backend or hurry App
# Review, and a proposal that asks it to only costs a dismissal. What is left
# (CI, the trader's dispatch and its own healthcheck) is code work, and may get
# one fix proposal.

def _decide_drift(rec) -> str:
    slug, summ, st = rec.get("project"), rec.get("summary") or "", rec.get("status")
    if st in (WARN, FAIL):
        return f"release {slug} backend (`!release {slug} backend`), or tell me it waits"
    if "no migration history" in summ:
        return (f"baseline the {slug} migrations (`sb migration repair --status applied …`) "
                "so drift can be read, or tell me to leave it")
    if "no credentials" in summ:
        return f"add the {slug} Supabase credential file, or tell me to drop the check"
    return ""


def _decide_app_store(rec) -> str:
    summ = rec.get("summary") or ""
    if rec.get("status") == WARN and "review" in summ:
        return ("App Review is slow — wait, or send Apple a status inquiry "
                "(developer.apple.com/contact/app-store/)")
    if rec.get("status") in (WARN, FAIL):
        return "act on it in App Store Connect"
    return ""


def _decide_testflight(rec) -> str:
    if rec.get("status") in (WARN, FAIL):
        return "look at the build in App Store Connect; re-upload it if it is stuck"
    return ""


def _decide_credentials(rec) -> str:
    if rec.get("status") in (WARN, FAIL):
        return "renew it (only you can); details above"
    return ""


def _decide_disk(rec) -> str:
    if rec.get("status") in (WARN, FAIL):
        return "free some space on the mini"
    return ""


def _decide_gateway(rec) -> str:
    if rec.get("status") in (WARN, FAIL):
        return ("check IB Gateway: logged in, and its API socket setting toggled off "
                "then on if the port stays unbound")
    return ""


#: What the owner does on the mini, by the trouble a tailscale summary names.
TAILSCALE_ACTIONS = (
    ("not running", "on the mini, open the Tailscale app and connect "
                    "(Homebrew: sudo brew services start tailscale)"),
    ("is stopped", "on the mini, open the Tailscale app and connect (or `tailscale up`)"),
    ("logged out", "on the mini, open the Tailscale app and sign in"),
    ("tailscale is ", "on the mini, open the Tailscale app and connect"),
    ("MagicDNS", "turn on MagicDNS and HTTPS Certificates at "
                 "https://login.tailscale.com/admin/dns"),
    ("this machine is now", "on the mini, run `silkworm tailscale on` (the phone needs the new name), "
             "or rename the machine back in the Tailscale admin console"),
    ("does not forward", "on the mini, run `silkworm tailscale on`"),
    ("also exposes", "on the mini, run `tailscale serve reset`, then `silkworm tailscale on`"),
    ("Funnel is on", "on the mini, run `tailscale funnel reset` now"),
)


def _decide_tailscale(rec) -> str:
    if rec.get("status") not in (WARN, FAIL):
        return ""
    summ, out = rec.get("summary") or "", []
    for marker, action in TAILSCALE_ACTIONS:
        if marker in summ and action not in out and not (
                marker == "tailscale is " and out):
            out.append(action)
    return "; ".join(out) or "on the mini, run `silkworm tailscale status` and fix what it shows"


DECISIONS = {"backend-drift": _decide_drift, "app-store": _decide_app_store,
             "testflight": _decide_testflight, "credentials": _decide_credentials,
             "disk": _decide_disk, "gateway": _decide_gateway,
             "tailscale": _decide_tailscale}


def _short_testflight(rec) -> str:
    return "TF " + (rec.get("summary") or "").split(" valid")[0].split(" (")[0]


def _short_disk(rec) -> str:
    m = re.search(r"\((\d+%)\)", rec.get("summary") or "")
    return f"disk {m.group(1)} free" if m else "disk ok"


SHORT = {"backend-drift": lambda rec: "backend current",
         "app-store": lambda rec: (rec.get("summary") or "").split(";")[-1].strip(),
         "testflight": _short_testflight, "dispatching": lambda rec: "dispatching",
         "gateway": lambda rec: "Gateway up", "healthcheck": lambda rec: "healthcheck passes",
         "credentials": lambda rec: "credentials good", "disk": _short_disk,
         "tailscale": lambda rec: "dashboard reachable"}


def projects_with_mini(recs) -> list[dict]:
    """The project records a health pass runs over: the real ones, plus the
    machine itself -- its registered record when there is one (so its items
    take that record's scope), else the stand-in."""
    recs = list(recs)
    if any((r.get("slug") or "") == MINI["slug"] for r in recs):
        return recs
    return recs + [dict(MINI)]


register_all()

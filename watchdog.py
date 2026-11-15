"""Out-of-band watchdog: notice the bot is down, roll back once, tell the owner.

On 2026-10-26 a deploy restarted the bot onto code that raised at import.
launchd's KeepAlive restarted it, and it raised again, for 18 days; the
deploy's failure note was left in outbox/ because the bot is the Slack
sender. Nothing outside the bot was looking.

This is that something: a launchd job with StartInterval (not KeepAlive, and
not part of bot.py) that runs once, checks, and exits -- so it cannot share
the bot's fate. Each run asks the bot's /status. When the bot has not answered
for DOWN_CHECKS runs in a row, or has answered with a new pid that many runs
in a row (crash-looping), it:

  1. records the outage in outages.json, with the tail of the bot's stderr;
  2. rolls the pinned checkout (live.py) back to the last known good revision
     and kickstarts the services -- once per outage, never to the revision
     already running, never twice within ROLLBACK_GAP_S, and never while a
     `silkworm deploy` holds its lock (a deploy rolls back on its own);
  3. tells the owner directly: chat.postMessage to the home DM with the bot
     token, plus a web push -- then again at most every REALERT_S while it
     stays down, and once when it is back.

Stdlib only (with this checkout's own stdlib-only modules), run by the
install's venv python. `python watchdog.py [--home DIR]`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import alerts    # noqa: E402
import jsonstore  # noqa: E402
import live      # noqa: E402
import outages   # noqa: E402

STATE_FILE = "watchdog.json"
#: Consecutive bad checks before acting. With the job's 120s interval, about
#: six minutes: longer than any restart a deploy does, shorter than anybody
#: would want to find out on their own.
DOWN_CHECKS = 3
#: While still down after an alert, say so again this often -- no more.
REALERT_S = 6 * 3600
#: Never two rollbacks this close together, whatever the outage bookkeeping says.
ROLLBACK_GAP_S = 24 * 3600
LABELS = ("com.silkworm.bot", "com.silkworm.viz")
DEPLOY_LOCK = Path("logs") / "deploy-after-turn.lock"


class World:
    """Every outside thing a check touches. Tests replace it."""

    def __init__(self, home):
        self.home = Path(home)

    def now(self) -> float:
        return time.time()

    def port(self) -> str:
        return alerts.env_value(self.home, "APPROVAL_PORT") or "8787"

    def status(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port()}/status", data=b"{}",
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                ans = json.loads(resp.read() or b"{}")
        except Exception:                     # noqa: BLE001 - down is the answer
            return None
        return ans if isinstance(ans, dict) and ans.get("online") else None

    def launchctl(self, *args) -> tuple:
        try:
            r = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            return 1, str(e)
        return r.returncode, r.stdout + r.stderr

    def domain(self) -> str:
        return f"gui/{os.getuid()}"

    def installed(self) -> bool:
        """The bot's job is loaded: unloaded (silkworm uninstall) is not down."""
        return self.launchctl("print", f"{self.domain()}/{LABELS[0]}")[0] == 0

    def restart(self) -> bool:
        ok = True
        for label in LABELS:
            ok &= self.launchctl("kickstart", "-k", f"{self.domain()}/{label}")[0] == 0
        return ok

    def deploying(self) -> bool:
        """A `silkworm deploy` holds its lock with a live process."""
        try:
            held = json.loads((self.home / DEPLOY_LOCK).read_text() or "{}")
            pid = int(held.get("pid") or 0)
        except (OSError, ValueError, TypeError):
            return False
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    def checkout(self, sha: str) -> tuple:
        return live.checkout(self.home, sha, why="watchdog rollback")

    def stderr_tail(self, n: int = 20) -> str:
        return live.Pin(self.home).stderr_tail(n)

    def alert(self, text: str) -> dict:
        return alerts.owner(self.home, text, title="Silkworm is down")

    def log(self, line: str) -> None:
        print(time.strftime("%Y-%m-%d %H:%M:%S"), line, flush=True)


def _load(home) -> dict:
    try:
        st = jsonstore.load(Path(home) / STATE_FILE, default={}, strict=False)
    except OSError:
        return {}
    return st if isinstance(st, dict) else {}


def _save(home, st: dict) -> None:
    jsonstore.save(Path(home) / STATE_FILE, st, indent=1)


def down_checks(home) -> int:
    try:
        return max(1, int(alerts.env_value(home, "WATCHDOG_DOWN_CHECKS") or DOWN_CHECKS))
    except ValueError:
        return DOWN_CHECKS


def check(home, world: World | None = None, n: int | None = None) -> dict:
    """One run. Returns what it found and did (also kept in watchdog.json)."""
    home = Path(home)
    world = world or World(home)
    n = n or down_checks(home)
    st = _load(home)
    now = world.now()
    st["last_check"] = now
    if not world.installed():
        st.update(down=0, restarts=0, first_fail=None)
        _save(home, st)
        return {"state": "not-installed"}

    ans = world.status()
    if ans:
        pid = ans.get("pid")
        moved = bool(st.get("pid") and pid and pid != st.get("pid"))
        st["restarts"] = (st.get("restarts") or 0) + 1 if moved else 0
        st["pid"] = pid
        st["down"] = 0
        if st["restarts"] < n:
            return _healthy(home, world, st, now, ans)
    else:
        st["down"] = (st.get("down") or 0) + 1
    st["first_fail"] = st.get("first_fail") or now
    bad = st["down"] if not ans else st["restarts"]
    what = "not answering" if not ans else "restarting every check (crash loop)"
    if bad < n:
        _save(home, st)
        world.log(f"bot {what} ({bad}/{n})")
        return {"state": "suspect", "count": bad}
    if world.deploying():
        _save(home, st)
        world.log(f"bot {what}, but a deploy is running -- leaving it to the deploy")
        return {"state": "deploy-running"}

    rec = live.read(home)
    running = rec.get("live") or ""
    cause = world.stderr_tail()
    out = outages.open_(home, start=st["first_fail"], cause=cause, seen_by="watchdog",
                        revision=running)
    outages.update_open(home, cause=cause)
    result = {"state": "down", "what": what, "rolled_back": None, "alerted": False}

    # The last known good, unless that is what is failing now -- a revision
    # can pass a deploy's confirm and crash later -- then the one before it.
    target = rec.get("last_good") or ""
    if target == running and rec.get("previous_good"):
        target = rec["previous_good"]
    since_rb = now - (st.get("rollback_at") or 0)
    if not out.get("rolled_back") and not st.get("rolled_back"):
        why_not = ""
        if not live.is_checkout(live.live_dir(home)):
            why_not = "there is no pinned checkout to roll back (never promoted)"
        elif not target:
            why_not = "no revision is recorded as known good"
        elif target == running:
            why_not = f"{running[:8]} is the last known good revision already"
        elif since_rb < ROLLBACK_GAP_S:
            why_not = "a rollback already happened within the last day"
        if why_not:
            st["rolled_back"] = {"to": "", "why_not": why_not, "at": now}
            world.log(f"bot {what}; not rolling back: {why_not}")
        else:
            ok, detail = world.checkout(target)
            restarted = ok and world.restart()
            rb = {"from": running, "to": target, "at": now, "ok": bool(restarted),
                  "detail": detail if not ok else ""}
            st.update(rolled_back=rb, rollback_at=now, down=0, restarts=0, pid=None)
            outages.update_open(home, rolled_back=rb)
            world.log(f"bot {what}; rolled back {running[:8]} -> {target[:8]}: "
                      + ("restarted" if restarted else f"FAILED ({detail})"))
        result["rolled_back"] = st["rolled_back"]

    if not st.get("alerted_at") or now - st["alerted_at"] >= REALERT_S:
        res = world.alert(_down_text(home, st, what, cause, now))
        st["alerted_at"] = now
        st["alerted"] = True
        result["alerted"] = res
        world.log(f"alerted: slack={res.get('slack')} push={res.get('push')}")
    _save(home, st)
    return result


def _healthy(home, world, st, now, ans) -> dict:
    closed = outages.close(home, end=now, how="watchdog saw it answer")
    note = None
    if st.get("alerted"):
        rb = st.get("rolled_back") or {}
        rev = ((ans.get("revision") or {}).get("started") or "")[:8]
        text = (f":white_check_mark: Silkworm is answering again (running {rev or '?'})"
                + (f", after rolling back {rb.get('from', '')[:8]} → {rb['to'][:8]}"
                   if rb.get("to") else "")
                + (f". {outages.describe(closed, now)}" if closed else "."))
        note = world.alert(text)
        world.log("recovered; told the owner")
    st["down"] = 0          # restarts is check()'s: it counts across healthy answers
    for k in ("first_fail", "rolled_back", "alerted", "alerted_at"):
        st.pop(k, None)
    _save(home, st)
    return {"state": "up", "closed": closed, "recovery_alert": note}


def _down_text(home, st, what, cause, now) -> str:
    first = st.get("first_fail") or now
    mins = int((now - first) // 60)
    rb = st.get("rolled_back") or {}
    if rb.get("to"):
        did = (f"Rolled back {rb.get('from', '')[:8] or '?'} → {rb['to'][:8]} and restarted"
               + ("." if rb.get("ok") else f" — but that failed: {rb.get('detail') or 'see logs'}."))
    elif rb.get("why_not"):
        did = f"Not rolled back: {rb['why_not']}."
    else:
        did = "Already rolled back once this outage; not again."
    tail = "\n".join((outages.scrub(cause) or "(nothing in logs/bot.err.log)").splitlines()[-12:])
    return (f":rotating_light: *Silkworm is down* — {what} for {mins}m "
            f"(seen by the watchdog). {did}\n"
            f"Last of logs/bot.err.log:\n```\n{tail[-1500:]}\n```\n"
            f"On the mini: `silkworm status`, `silkworm logs`.")


def main(argv) -> int:
    home = Path(__file__).resolve().parent
    if argv[:1] == ["--home"] and len(argv) > 1:
        home = Path(argv[1]).expanduser().resolve()
    elif argv:
        print("usage: watchdog.py [--home DIR]", file=sys.stderr)
        return 2
    try:
        res = check(home)
    except Exception as e:                    # noqa: BLE001 - a watchdog must not die quietly
        print(time.strftime("%Y-%m-%d %H:%M:%S"), f"check failed: {type(e).__name__}: {e}",
              flush=True)
        return 1
    if res.get("state") not in ("up", "not-installed"):
        print(time.strftime("%Y-%m-%d %H:%M:%S"), json.dumps(res, default=str)[:500], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

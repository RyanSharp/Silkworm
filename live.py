"""The pinned release checkout the bot and the visualizer run from.

Until 2026-11 they ran from the main checkout -- the one landings fast-forward
and turns edit -- so "what is running" was whatever that tree held at the last
restart, and a deploy could only restart onto it: there was nothing else to
go back to. When the deploy of 793f119 booted code that raised at import,
nothing rolled it back, and the fix was a hand edit in the live tree.

Now the code runs from a separate git worktree (`live_dir`), detached at one
commit. `silkworm deploy` promotes a commit by checking it out there and
restarting; when the new revision answers and reconnects it becomes
`last_good`, and when it does not the checkout goes back to `last_good`. The
watchdog (watchdog.py) uses the same record to roll back once when the bot is
down. The record is `live.json` in the install (the main checkout), beside
the other state files.

Stdlib only (and jsonstore, which is): the watchdog imports it.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import jsonstore

STATE_FILE = "live.json"
#: Promotions remembered, newest last.
HISTORY_MAX = 30


def live_dir(home) -> Path:
    """Where the pinned checkout lives: SILKWORM_LIVE_DIR, else a hidden
    sibling of the install (never under .worktrees/, whose sweeper is for
    task checkouts)."""
    home = Path(home)
    env = (os.environ.get("SILKWORM_LIVE_DIR") or "").strip()
    return Path(env).expanduser() if env else home.parent / f".{home.name}-live"


def is_checkout(path) -> bool:
    return (Path(path) / ".git").exists() and (Path(path) / "bot.py").exists()


def code_dir(home) -> Path:
    """What launchd should run: the pinned checkout once there is one, else
    the install itself (an install that has never been promoted)."""
    d = live_dir(home)
    return d if is_checkout(d) else Path(home)


def _git(cwd, *args, timeout: float = 120):
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def read(home) -> dict:
    """Without repairing: the bot reads this while a deploy or the watchdog
    writes it."""
    try:
        rec = jsonstore.load(Path(home) / STATE_FILE, default={}, strict=False, repair=False)
    except OSError:
        return {}
    return rec if isinstance(rec, dict) else {}


def write(home, rec: dict) -> None:
    jsonstore.save(Path(home) / STATE_FILE, rec, indent=1)


def head(path) -> str:
    code, out, _ = _git(path, "rev-parse", "HEAD", timeout=20)
    return out if code == 0 else ""


def checkout(home, sha: str, *, why: str = "", git=_git) -> tuple:
    """Put the pinned checkout at `sha`. (ok, detail).

    Creates it the first time (a detached worktree of the install's
    repository); after that, a forced detached checkout plus a clean, so
    nothing left in it -- a stray edit, a .pyc of a deleted module -- is part
    of what runs. Verified by reading HEAD back.
    """
    home, d = Path(home), live_dir(home)
    if not sha:
        return False, "no revision to check out"
    if not is_checkout(d):
        if d.exists() and any(d.iterdir()):
            return False, f"{d} exists and is not a checkout -- move it aside"
        git(home, "worktree", "prune")
        code, _, err = git(home, "worktree", "add", "--detach", "--force", str(d), sha)
        if code != 0:
            return False, f"could not create {d}: {err[-300:]}"
    else:
        code, _, err = git(d, "checkout", "--detach", "--force", sha)
        if code != 0:
            return False, f"could not check out {sha[:8]} in {d}: {err[-300:]}"
        git(d, "clean", "-ffdx", "--quiet")
    code, now, _ = git(d, "rev-parse", "HEAD")
    if now != sha:
        return False, f"{d} is at {now[:8] or 'nothing'}, not {sha[:8]}"
    rec = read(home)
    rec.update(live=sha, live_at=time.time(), live_why=why)
    write(home, rec)
    return True, str(d)


def mark_good(home, sha: str, by: str = "") -> None:
    """`sha` answered and reconnected: it is what a rollback goes back to."""
    rec = read(home)
    if rec.get("last_good") and rec["last_good"] != sha:
        rec["previous_good"] = rec["last_good"]
    rec.update(last_good=sha, good_at=time.time())
    hist = list(rec.get("history") or [])
    hist.append({"sha": sha, "at": time.time(), "by": by})
    rec["history"] = hist[-HISTORY_MAX:]
    write(home, rec)


def last_good(home) -> str:
    return read(home).get("last_good") or ""


def describe(home) -> str:
    rec = read(home)
    if not rec.get("live"):
        return f"no pinned checkout yet (running from {home})"
    good = rec.get("last_good") or ""
    same = good == rec["live"]
    return (f"pinned at {rec['live'][:8]} in {live_dir(home)}"
            + ("" if same else f"; last known good {good[:8] or 'none'}"))


class Pin:
    """The pinned checkout of one install, as `silkworm deploy` drives it.
    Tests drive deploy with a fake that has the same four methods."""

    def __init__(self, home):
        self.home = Path(home)

    def checkout(self, sha: str, why: str = "") -> tuple:
        return checkout(self.home, sha, why=why)

    def last_good(self) -> str:
        return last_good(self.home)

    def mark_good(self, sha: str, by: str = "deploy") -> None:
        mark_good(self.home, sha, by)

    def boot(self, sha: str, timeout: float = 90) -> tuple:
        """Boot `sha` exactly as committed, in a throwaway worktree, with
        smoke.py. (ok, detail: why and the stderr tail)."""
        import shutil
        import smoke
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="silkworm-boot-")) / "tree"
        code, _, err = _git(self.home, "worktree", "add", "--detach", "--force", str(tmp), sha)
        if code != 0:
            return False, f"could not check {sha[:8]} out to boot it: {err[-300:]}"
        try:
            py = self.home / ".venv" / "bin" / "python"
            res = smoke.boot(tmp, python=str(py) if py.exists() else None,
                             timeout=timeout, expect_sha=sha)
        finally:
            _git(self.home, "worktree", "remove", "--force", str(tmp))
            shutil.rmtree(tmp.parent, ignore_errors=True)
            _git(self.home, "worktree", "prune")
        return res["ok"], smoke.describe(res)

    def record_outage(self, entry: dict) -> None:
        import outages
        outages.record(self.home, entry)

    def stderr_tail(self, n: int = 20) -> str:
        try:
            lines = (self.home / "logs" / "bot.err.log").read_text(errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-n:])

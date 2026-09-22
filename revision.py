"""Which revision of this code is actually running.

A landed fix is not a running fix. Silkworm merges reviewed, verified work onto
its own main without a person (`merge.land`), but nothing restarts the bot and
the bot is not under launchd by default -- so the commit sits in the working
tree while the live process keeps serving the revision it booted with. The
reply says "Landed on main", which reads as *this is now true*, and it is not.

That gap was invisible from outside. Startup logged the workspace, the approval
mode and the turn limits, but never the revision; neither `silkworm status` nor
the dashboard reported it. The only way to answer "is the running bot the code
I am reading" was to probe for a behaviour difference and infer backwards, which
is how it was found: on 21 September the live log was still writing a `filed
task` line in the format the commit of the 20th had replaced, and the first
thing the new check said, asked of the live bot, was that the bot was too old
to answer.

So: resolve the revision at startup, keep it, and compare it against the
checkout's HEAD on demand. The comparison is deliberately separate from the git
calls, because the interesting part is the verdict and it should be testable
without a repository.

Not knowing is its own answer. `unknown` is never folded into "current": a
missing git, a checkout that is not a repo, or a bot too old to report at all
would otherwise all read as all-clear -- which is the same silence this exists
to break.
"""

import subprocess
import time
from pathlib import Path

#: The checkout this code was loaded from -- the code that is running, not the
#: workspace it operates on.
HOME = Path(__file__).resolve().parent

#: Long enough that polling the dashboard every five seconds does not spawn git
#: twice a second; short enough that a landing shows up while you are looking.
CACHE_S = 30

_cache: dict = {}


def _git(cwd, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def of(path=HOME) -> dict:
    """The revision a checkout is at right now.

    `sha` is empty when the answer is not knowable -- no git, or not a
    repository. Callers must read that as unknown rather than as unchanged.

    `dirty` matters because a process that booted from a modified tree is
    running code that was never any commit, so a matching sha is weaker
    evidence than it looks.
    """
    sha = _git(path, "rev-parse", "HEAD")
    if not sha:
        return {"sha": "", "branch": "", "dirty": False}
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    return {"sha": sha,
            # Detached HEAD reports the literal string rather than a name.
            "branch": "" if branch == "HEAD" else branch,
            "dirty": bool(_git(path, "status", "--porcelain"))}


def behind(path=HOME, sha: str = "") -> int:
    """How many commits HEAD has that `sha` does not. -1 when unanswerable.

    Unanswerable is the normal case for a rewritten history: once a branch is
    rebased or the startup commit is gone, git cannot count from it, and a
    guess of 0 would say "up to date" about a revision it could not even find.
    """
    if not sha:
        return -1
    count = _git(path, "rev-list", "--count", f"{sha}..HEAD")
    try:
        return int(count)
    except ValueError:
        return -1


def drift(started: dict, head: dict, ahead: int = -1) -> dict:
    """Whether the process's revision is still the checkout's revision.

    Pure, so the verdict can be tested without a repository: `started` is what
    the running process recorded at boot, `head` is what the checkout says now,
    and `ahead` is `behind()`'s count of what has landed since.

    Three states, and the third is not a failure of the first two. `unknown`
    means the question could not be answered -- and answering "current" in that
    case would be a lie told confidently, which is worse than no answer.
    """
    started, head = started or {}, head or {}
    a, b = started.get("sha") or "", head.get("sha") or ""
    dirty = bool(started.get("dirty"))
    out = {"behind": 0, "started": a, "head": b, "dirty": dirty,
           "branch": head.get("branch") or started.get("branch") or "", "why": ""}
    if not a or not b:
        return {**out, "state": "unknown", "why": "no-revision"}
    if a != b:
        # Only a count when git actually counted one, and only when there is a
        # gap to count: the two git calls are separate processes, so a commit
        # landing between them can pair an unchanged sha with a nonzero count.
        return {**out, "state": "stale", "behind": ahead if ahead > 0 else 0}
    if dirty:
        # A matching sha is the weakest evidence here, not the strongest: the
        # process booted from a tree that was no commit, and the edits it
        # booted with may since have been reverted, so the code it is running
        # exists nowhere on disk. That is not "current" -- it is unknowable,
        # which is the one thing this module refuses to round off.
        return {**out, "state": "unknown", "why": "dirty-boot"}
    return {**out, "state": "current"}


def describe(d: dict) -> str:
    """One line, for a log, a check or an alert."""
    d = d or {}
    short = (d.get("started") or "")[:8]
    branch = f" on {d['branch']}" if d.get("branch") else ""
    dirty = " (+uncommitted changes)" if d.get("dirty") else ""
    if d.get("state") == "stale":
        n = d.get("behind") or 0
        gap = f"{n} commit{'s' if n != 1 else ''} behind" if n else "behind"
        return (f"running {short}{branch}{dirty} — {gap} "
                f"the checkout's {(d.get('head') or '')[:8]}")
    if d.get("state") == "current":
        return f"running {short}{branch} — matches the checkout"
    if d.get("why") == "dirty-boot":
        return (f"running {short}{branch} plus uncommitted changes — what it "
                "booted with was never a commit, so this cannot be checked")
    return "running revision unknown"


def state(path=HOME, started: dict | None = None, ttl: float = CACHE_S) -> dict:
    """The full answer for a status route: what booted, what is on disk, drift.

    Memoised whole rather than per-git-call. The dashboard polls /status every
    five seconds and this is two subprocesses, so caching only the cheaper half
    would have left the other one running twelve times a minute -- and caching
    them separately lets the two expire apart, pairing a fresh HEAD with a
    count taken against the previous one.
    """
    started = started or {}
    key = (str(path), started.get("sha") or "")
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    d = drift(started, of(path), behind(path, started.get("sha", "")))
    answer = {**d, "message": describe(d)}
    _cache[key] = (time.time(), answer)
    return answer

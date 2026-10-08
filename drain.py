"""Draining: let what is running finish, start nothing new, then restart.

A restart kills whatever turn is running. Over 2026-09-18..10-01 the bot was
restarted thirteen times in ten days to load new code, and nine runs ended
killed (exit 143) part-way through -- each interrupted queue task then started
again from scratch and spent its whole session a second time. Deploys were
done by hand with a script that waited for "nothing running" and restarted,
which worked only as long as nothing new was claimed in between.

So `silkworm deploy` asks the running bot to drain: the queue runner stops
claiming, turns already underway carry on, and the deploy waits for the board
to go quiet before it restarts.

Draining lives only in the running process. The restart it exists for clears
it, and nothing persisted can outlive a deploy that crashed half-way. It also
has a deadline: a deploy that was killed, or forgot, must not be able to hold
the runner indefinitely, so the drain lapses on its own when its time is up.
"""

import logging
import threading
import time

log = logging.getLogger("silkworm.drain")

#: Longest any one drain may hold the runner, whatever the caller asked for.
CAP_S = 6 * 3600
#: What a drain asked for without a length gets.
DEFAULT_S = 2 * 3600


class Drain:
    """Tells the queue runner not to claim, until stopped or its deadline."""

    def __init__(self):
        self._lock = threading.Lock()
        self._until = 0.0
        self._why = ""

    def start(self, seconds: float, why: str = "", now: float | None = None) -> float:
        """Drain for `seconds` (at most CAP_S) from now. Returns the deadline.

        Unlike the outage hold this may shorten as well as extend: it is one
        caller's explicit request, and the latest one is what it wants now.
        """
        now = time.time() if now is None else now
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            seconds = DEFAULT_S
        seconds = max(0.0, min(seconds, CAP_S))
        with self._lock:
            self._until, self._why = now + seconds, why
        log.warning("draining for %ds%s: the runner claims nothing new",
                    seconds, f" ({why})" if why else "")
        return now + seconds

    def stop(self) -> bool:
        """End the drain. Returns whether one was in force."""
        with self._lock:
            was = self._until > time.time()
            self._until, self._why = 0.0, ""
        if was:
            log.info("drain lifted: the runner may claim again")
        return was

    def remaining(self, now: float | None = None) -> float:
        """Seconds the drain still has to run; 0 once lifted or lapsed."""
        now = time.time() if now is None else now
        with self._lock:
            return max(0.0, self._until - now)

    def reason(self) -> str:
        with self._lock:
            return self._why if self._until > time.time() else ""


def busy(records, landing=(), live=(), releasing=()) -> list[dict]:
    """What a restart right now would kill, as rows for a person to read.

    Every task in `running` counts, whatever drives it: a Slack conversation is
    killed by a restart exactly as a queued task is, and is less recoverable.
    A landing in flight counts too -- it runs on a daemon thread and a restart
    stops it part-way through a merge -- but only one this process is running
    (`landing`), not a durable marker, which an earlier crash may have left.

    A release in flight counts for the same reason, and worse: a restart can
    stop it with a target's migrations pushed but its functions not deployed,
    or a tag pushed and the targets after it never run. `releasing` is the set
    of project slugs this process is releasing.

    `live` is the set of task ids this process holds a child for. A `running`
    record that is not among them is still reported -- the board says it is
    running, and guessing otherwise is how work gets killed -- but marked, so
    whoever is waiting on it can see why it never finishes.
    """
    rows = []
    for r in records:
        if r.get("state") != "running":
            continue
        rows.append({"id": r.get("id", ""), "kind": "task",
                     "title": (r.get("title") or "")[:80],
                     "driver": r.get("driver") or "",
                     "since": r.get("updated") or 0,
                     "live": r.get("id") in live})
    seen = {row["id"] for row in rows}
    by_id = {r.get("id"): r for r in records}
    for tid in sorted(landing):
        if tid in seen:
            continue
        rows.append({"id": tid, "kind": "landing",
                     "title": ((by_id.get(tid) or {}).get("title") or "")[:80],
                     "driver": "", "since": 0, "live": True})
    for slug in sorted(releasing):
        rows.append({"id": slug, "kind": "release", "title": f"release of {slug}"[:80],
                     "driver": "", "since": 0, "live": True})
    return rows

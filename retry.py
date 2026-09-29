"""Telling a transient failure from a real one.

A turn that died because the quota was exhausted or the API was overloaded has
not failed in any sense the user can act on -- there is nothing to fix, only a
time to wait. Filing those under "needs you" is what turns a board people trust
into one they learn to ignore.

So they are classified, parked in `blocked` with a time to come back, and
requeued automatically. Real failures -- a bad command, a tool error, a broken
session -- still stop and ask, because those genuinely need a person.
"""

import logging
import re
import threading
import time
from datetime import datetime, timedelta

log = logging.getLogger("silkworm.retry")

QUOTA = "quota"
OVERLOADED = "overloaded"
NETWORK = "network"

#: Matched against the error text a turn died with. Order matters only in that
#: the first match wins; the kinds differ mainly in how long to wait.
PATTERNS = (
    # The separator varies: "session limit" in prose, "rate_limit_error" from
    # the API. Matching only the spaced form would miss half of them.
    (re.compile(r"(session|usage|rate)[ _-]?limit|quota|resets\s+\d", re.I), QUOTA),
    (re.compile(r"\b529\b|overloaded|service unavailable|\b503\b", re.I), OVERLOADED),
    (re.compile(r"connection (closed|reset|error)|temporarily unavailable"
                r"|\b502\b|\b504\b", re.I), NETWORK),
)

#: Give up eventually. A task that has been requeued this many times is not
#: hitting a blip, and silently retrying forever would hide a real problem.
MAX_AUTO_RETRIES = 5

#: Runs that died before doing any work do not count against MAX_AUTO_RETRIES
#: -- a quota outage says nothing about the task -- but they are still bounded,
#: so a task that can never get started does eventually ask. Generous, because
#: each one is a whole outage window (hours, for a quota) and the runner holds
#: off between them rather than burning them back to back.
MAX_FALSE_STARTS = 24

QUOTA_FALLBACK_S = 30 * 60          # when the message says nothing useful
BACKOFF_BASE_S = 60
BACKOFF_CAP_S = 30 * 60

#: The longest the runner holds off on one failure's say-so. A reset time is
#: parsed from prose, and one read a minute late ("resets 6pm" seen at 6:01)
#: rolls to tomorrow: the whole queue would idle a day on a typo. Past this it
#: sends one probe, which costs a refunded false start if the wait was real.
HOLD_CAP_S = 5 * 60 * 60


def classify(text: str) -> str | None:
    """The kind of transient failure this is, or None if it's a real one."""
    if not text:
        return None
    for pattern, kind in PATTERNS:
        if pattern.search(text):
            return kind
    return None


def reset_at(text: str, now: float | None = None) -> float | None:
    """Parse the reset time out of a quota message, e.g. 'resets 7pm'.

    Returns the next time that clock reading occurs, so a message at 11pm
    saying 'resets 7am' waits until the morning rather than 20 hours ago.
    """
    m = re.search(r"resets?\s*(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text or "", re.I)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    base = datetime.fromtimestamp(now if now is not None else time.time())
    target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= base:                   # already passed today -> tomorrow
        target += timedelta(days=1)
    return target.timestamp()


def retry_at(text: str, attempts: int, now: float | None = None,
             false_starts: int = 0) -> tuple[str, float] | None:
    """When to try this again, or None if it should not be retried at all.

    `attempts` counts runs that reached real work and is what the retry budget
    is spent from. `false_starts` -- runs that died before doing anything --
    does not spend it, but does lengthen the backoff: otherwise a task whose
    false starts are refunded would retry an overloaded API every minute.
    """
    kind = classify(text)
    if kind is None:
        return None
    if attempts >= MAX_AUTO_RETRIES:
        log.warning("not auto-retrying after %d attempts: %s", attempts, (text or "")[:80])
        return None
    if false_starts >= MAX_FALSE_STARTS:
        log.warning("not auto-retrying after %d runs that never started: %s",
                    false_starts, (text or "")[:80])
        return None
    return kind, wait_until(kind, text, attempts + false_starts, now)


def wait_until(kind: str, text: str, tries: int = 1, now: float | None = None) -> float:
    """When a transient condition of this kind is plausibly over."""
    now = now if now is not None else time.time()
    if kind == QUOTA:
        when = reset_at(text, now)
        # A quota message usually says when it resets; if not, wait a while
        # rather than hammering it.
        return when if when else now + QUOTA_FALLBACK_S
    # Overload and network blips clear on their own; back off progressively.
    return now + min(BACKOFF_BASE_S * (2 ** max(0, tries - 1)), BACKOFF_CAP_S)


class Hold:
    """Tells the queue runner to stop claiming while a global condition lasts.

    Quota, overload and a dead network are not properties of a task: when one
    kills a turn it will kill the next one too, in seconds. Without this the
    runner claimed the next task regardless, and walked the whole queue in one
    burst -- 15 tasks in 39 seconds on 2026-09-26, some 260 on 2026-09-22 --
    each given a worktree and parked unrun. Parked retries then share one reset
    time, so the next window repeated the burst exactly.

    With it, the first casualty closes the hold until the condition is
    plausibly over; everything behind it stays queued, untouched, and the
    reconverged retries cost one probe rather than one pass each. Any turn that
    succeeds opens it again early -- it is evidence the condition has passed,
    and a guessed reset time is not worth idling on once it is contradicted.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._until = 0.0
        self._why = ""

    def close(self, until: float, why: str) -> bool:
        """Hold off until `until`, at most HOLD_CAP_S. Only ever extends."""
        until = min(until, time.time() + HOLD_CAP_S)
        with self._lock:
            if until <= self._until:
                return False
            self._until, self._why = until, why
        log.warning("queue runner holding until %s: %s",
                    datetime.fromtimestamp(until).strftime("%H:%M:%S"), why[:120])
        return True

    def open(self) -> None:
        with self._lock:
            was = self._until > time.time()
            self._until, self._why = 0.0, ""
        if was:
            log.info("queue runner hold lifted: a turn got through")

    def remaining(self, now: float | None = None) -> float:
        """Seconds left to hold off; 0 when the runner may claim."""
        with self._lock:
            return max(0.0, self._until - (now if now is not None else time.time()))

    def reason(self) -> str:
        with self._lock:
            return self._why if self._until > time.time() else ""


def describe(kind: str, when: float) -> str:
    return (f"{kind}: retrying at "
            f"{datetime.fromtimestamp(when).strftime('%H:%M')}")

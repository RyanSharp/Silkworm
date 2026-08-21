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

QUOTA_FALLBACK_S = 30 * 60          # when the message says nothing useful
BACKOFF_BASE_S = 60
BACKOFF_CAP_S = 30 * 60


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


def retry_at(text: str, attempts: int, now: float | None = None) -> tuple[str, float] | None:
    """When to try this again, or None if it should not be retried at all."""
    kind = classify(text)
    if kind is None:
        return None
    if attempts >= MAX_AUTO_RETRIES:
        log.warning("not auto-retrying after %d attempts: %s", attempts, (text or "")[:80])
        return None
    now = now if now is not None else time.time()
    if kind == QUOTA:
        when = reset_at(text, now)
        # A quota message usually says when it resets; if not, wait a while
        # rather than hammering it.
        return kind, (when if when else now + QUOTA_FALLBACK_S)
    # Overload and network blips clear on their own; back off progressively.
    delay = min(BACKOFF_BASE_S * (2 ** max(0, attempts - 1)), BACKOFF_CAP_S)
    return kind, now + delay


def describe(kind: str, when: float) -> str:
    return (f"{kind}: retrying at "
            f"{datetime.fromtimestamp(when).strftime('%H:%M')}")

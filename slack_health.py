"""Is the bot actually connected to Slack, as distinct from merely running?

A socket-mode connection can fail in a way the process never notices. The
websocket breaks, slack_sdk reconnects, the fresh session breaks immediately,
and the loop spins forever. The process stays up, launchd's KeepAlive sees a
healthy service, the local HTTP server still answers, and the only symptom is
silence. That is exactly what happened on 2026-08-24: the socket broke at
22:26 and nothing noticed for seventeen hours, until a person restarted it by
hand.

Sampling is_connected() once is not enough to catch it. In that loop a session
really is established every few seconds, so a single well-timed sample honestly
reports "connected". Health is therefore a *fraction over a window*: a link
that is up two percent of the time is down, however often it says hello.

The remedy is deliberately blunt. Repairing slack_sdk's internal state from
outside is guesswork; restarting the process is what actually worked when a
person did it, and interrupted turns are already rescued by recovery.py.
"""

import logging
from collections import deque

log = logging.getLogger("silkworm.slack")

#: Seconds between samples, and how many are kept. 60 x 5s = a 5 minute window.
INTERVAL_S = 5.0
WINDOW = 60

#: Below this fraction of connected samples the link is considered down. Half
#: is generous: healthy operation sits at ~1.0 and the failure mode near 0.
MIN_FRACTION = 0.5

REPAIR, RESTART = "repair", "restart"


class Health:
    """Rolling verdict on the socket-mode link.

    Deliberately pure -- it is fed samples and returns what to do, so the
    escalation can be tested without a socket, a thread or a clock.
    """

    def __init__(self, window: int = WINDOW, min_fraction: float = MIN_FRACTION):
        self._samples: deque[bool] = deque(maxlen=window)
        self._window = window
        self._min = min_fraction
        self._repaired = False
        self.down_since: float | None = None

    def fraction(self) -> float:
        if not self._samples:
            return 1.0
        return sum(self._samples) / len(self._samples)

    @property
    def ready(self) -> bool:
        """A partial window cannot condemn a link -- startup is not an outage."""
        return len(self._samples) >= self._window

    def healthy(self) -> bool:
        return not self.ready or self.fraction() >= self._min

    def sample(self, connected: bool, now: float) -> str | None:
        """Record one observation. Returns REPAIR, RESTART, or None.

        Escalates rather than repeating: the first bad window asks for a
        reconnect, and only a second bad window *after* that reconnect asks for
        a restart. The window is cleared on repair so the verdict that follows
        is about the new connection, not the one already replaced.
        """
        self._samples.append(bool(connected))
        if connected and self.down_since is not None:
            self.down_since = None
        elif not connected and self.down_since is None:
            self.down_since = now
        if self.healthy():
            # Only a *full* healthy window forgives the repair. Forgiving during
            # the warm-up that follows one would rearm the repair every time and
            # never escalate -- the same unbounded loop this exists to break.
            if self.ready:
                self._repaired = False
            return None
        pct = round(self.fraction() * 100)
        self._samples.clear()             # judge what happens next, not what just did
        if self._repaired:
            log.error("slack link still down (%d%% of the last window connected) "
                      "after a reconnect; exiting so launchd starts us clean", pct)
            return RESTART
        self._repaired = True
        log.warning("slack link down (%d%% of the last window connected); reconnecting", pct)
        return REPAIR

    def status(self, now: float) -> dict:
        return {"connected": bool(self._samples and self._samples[-1]),
                "fraction": round(self.fraction(), 3),
                "ready": self.ready,
                "down_for": round(now - self.down_since, 1) if self.down_since else 0.0}

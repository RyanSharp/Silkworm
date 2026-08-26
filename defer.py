"""Wake a thread later, instead of holding a turn open until then.

"Monitor this and get back to me" is not a request/response, but a turn is:
one prompt in, one reply out, and the session is dormant either side of it. So
the instruction has two ways to be lost, and both were reachable. Hold the turn
open and it fights everything -- the 15 minute cap kills it, the runaway reaper
kills what survives that, the thread lock means you cannot talk to that thread
meanwhile, and a restart orphans it. End the turn and background the work, and
the answer arrives somewhere nobody is listening, because nothing outside a
turn was ever wired to speak.

The fix is not a longer timeout. It is to stop holding the turn open: finish
now, and schedule a short turn for later on the same session. A scheduled
wake-up is an ordinary task in `blocked` with a `retry_at`, which the queue
runner already requeues when its time arrives -- so it costs nothing while it
waits, survives restarts because it is a durable record, and resumes the thread
with its context intact.

Chains are capped. A model that keeps misjudging "is it finished yet" would
otherwise poll at your expense forever.
"""

import re

#: Below this, a wake-up is really a busy-loop; above it, use a real schedule.
MIN_DELAY_S = 30
MAX_DELAY_S = 24 * 3600

#: How many times one chain may re-defer before it must report and stop.
MAX_DEFERS = 24

#: What a wake-up says when there is still nothing worth interrupting for. The
#: turn happened, but the thread stays silent -- a check every ten minutes that
#: narrates itself is worse than no check at all.
QUIET = "NOTHING YET"

_DELAY = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])?\s*$", re.I)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_delay(text: str) -> float:
    """"10m" -> 600.0. Bare numbers are seconds. Raises ValueError."""
    m = _DELAY.match(str(text or ""))
    if not m:
        raise ValueError(f"could not read a delay from {text!r} (try 30s, 10m, 2h, 1d)")
    seconds = float(m.group(1)) * _UNITS[(m.group(2) or "s").lower()]
    if seconds < MIN_DELAY_S:
        raise ValueError(f"delay must be at least {MIN_DELAY_S}s")
    if seconds > MAX_DELAY_S:
        raise ValueError(f"delay must be at most {int(MAX_DELAY_S / 3600)}h")
    return seconds


def next_depth(depth) -> int:
    """Depth of the chain a new wake-up would start. Raises at the cap."""
    try:
        d = int(depth or 0)
    except (TypeError, ValueError):
        d = 0
    if d >= MAX_DEFERS:
        raise ValueError(
            f"this chain has already woken {d} times; report what you know "
            "instead of scheduling another check")
    return d + 1


WAKE_NOTE = (
    "This is a wake-up you scheduled earlier, on the same conversation -- your "
    "earlier context is intact.\n\n"
    "Check whether the thing you were watching has finished.\n"
    f"- If it has, or if something happened worth interrupting for, say so.\n"
    f"- If nothing has changed, schedule another check and reply with exactly "
    f"{QUIET} and nothing else. That reply is swallowed, so the thread stays "
    "quiet until there is news."
)

HOW_TO = (
    "If you are asked to watch something and report back later, do not hold "
    "this turn open waiting -- it will be killed, and the thread stays locked "
    "meanwhile. Finish your reply now, and schedule a wake-up:\n"
    "    {bin} defer <delay> \"<what to check when you wake>\"\n"
    "for example: {bin} defer 10m \"check whether the deploy finished\"\n"
    "It resumes this same conversation later, so write the note to yourself. "
    f"Delays run from {MIN_DELAY_S}s to {int(MAX_DELAY_S / 3600)}h."
)


#: Shown when a wake-up reports "nothing yet" but schedules no successor. The
#: watch has stopped without saying so, which is the silence this exists to
#: prevent, so it is surfaced rather than swallowed.
STOPPED = (":warning: _I was watching something for you and just checked "
           "again — nothing had changed, but I failed to schedule the next "
           "check, so this watch has stopped. Tell me to resume it if you "
           "still want it._")

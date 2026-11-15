"""When the bot was down, why, and what was done about it.

From 2026-10-26 to 2026-11-14 Silkworm was down and nothing anywhere said so:
the deploy's own failure note sat in outbox/ because the bot is what posts,
and the digest and status that would have shown it are the bot. So outages
are written down by whoever sees them -- the watchdog (watchdog.py), a deploy
that rolled back -- in `outages.json` in the install, and read back by the
bot's /status and daily digest once it is up again.

Each entry: start, end (None while still down), cause (the tail of the bot's
stderr, scrubbed of anything token-shaped), what was rolled back, who saw it.

Stdlib only (and jsonstore, which is).
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import jsonstore

STATE_FILE = "outages.json"
KEEP = 50

_TOKENS = re.compile(r"(xox[abposr]-|xapp-|sk-ant-|sbp_|ghp_|github_pat_)[A-Za-z0-9_-]+")


def scrub(text: str) -> str:
    return _TOKENS.sub(lambda m: m.group(1) + "[redacted]", str(text or ""))


def load(home) -> list:
    """Read without repairing: the bot, the CLI and the watchdog all read
    this, and none of them may move a file another is writing."""
    try:
        rows = jsonstore.load(Path(home) / STATE_FILE, default=[], strict=False, repair=False)
    except OSError:
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _save(home, rows: list) -> None:
    jsonstore.save(Path(home) / STATE_FILE, rows[-KEEP:], indent=1)


def current(home) -> dict | None:
    """The open outage, if one is."""
    for r in reversed(load(home)):
        if not r.get("end"):
            return r
    return None


def open_(home, *, start: float, cause: str, seen_by: str, revision: str = "") -> dict:
    """Record an outage as begun at `start`, unless one is already open
    (which is returned instead: one outage, however many saw it)."""
    rows = load(home)
    for r in reversed(rows):
        if not r.get("end"):
            return r
    rec = {"start": start, "end": None, "cause": scrub(cause)[-2000:], "seen_by": seen_by,
           "revision": revision, "rolled_back": None}
    rows.append(rec)
    _save(home, rows)
    return rec


def update_open(home, **fields) -> dict | None:
    rows = load(home)
    for r in reversed(rows):
        if not r.get("end"):
            r.update({k: (scrub(v)[-2000:] if k == "cause" else v) for k, v in fields.items()})
            _save(home, rows)
            return r
    return None


def close(home, *, end: float, how: str) -> dict | None:
    """End the open outage, if any. Returns it."""
    rows = load(home)
    for r in reversed(rows):
        if not r.get("end"):
            r.update(end=end, ended_by=how)
            _save(home, rows)
            return r
    return None


def record(home, entry: dict) -> dict:
    """A whole outage at once -- a deploy that failed and rolled back
    within its own run. Closes any open one first: they are the same event."""
    rows = [dict(r, end=r.get("end") or entry.get("start"), ended_by=r.get("ended_by") or
                 "superseded") if not r.get("end") else r for r in load(home)]
    entry = dict(entry, cause=scrub(entry.get("cause"))[-2000:])
    rows.append(entry)
    _save(home, rows)
    return entry


def since(home, t: float) -> list:
    """Outages that were going on at any point after `t`."""
    return [r for r in load(home) if not r.get("end") or (r.get("end") or 0) >= t]


def _dur(s: float) -> str:
    s = max(0, int(s))
    if s < 120:
        return f"{s}s"
    if s < 7200:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s / 3600:.1f}h"
    return f"{s / 86400:.1f}d"


def describe(r: dict, now: float | None = None) -> str:
    """One line: when, how long, why, what was rolled back."""
    now = time.time() if now is None else now
    start = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("start") or 0))
    if r.get("end"):
        span = f"down {_dur(r['end'] - (r.get('start') or r['end']))} from {start}"
    else:
        span = f"DOWN since {start} ({_dur(now - (r.get('start') or now))})"
    rb = r.get("rolled_back") or {}
    back = ""
    if rb.get("to"):
        back = (f"; rolled back {(rb.get('from') or '?')[:8]} → {rb['to'][:8]}"
                + ("" if rb.get("ok", True) else " (which did not come up either)"))
    cause = " ".join((r.get("cause") or "").split())
    if len(cause) > 160:
        cause = "…" + cause[-160:]
    return span + (f" — {cause}" if cause else "") + back

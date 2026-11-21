"""Which threads have replies the owner has not read yet, for the dashboard.

A reply is unread when it is not the owner's -- the assistant answering, the
operator, a task's run, a scheduled wake-up reporting back: anything written
as the assistant in the thread's transcript -- and it arrived after the later
of two moments:

  * the owner last saw the thread at its bottom on the dashboard, on any
    device: the watermark the page posts (`Seen`, kept in seen.json beside
    sessions.json, on the mini, so reading on the phone clears the laptop);
  * the owner last wrote in it, from Slack or the dashboard: replying means
    you have read what came before. The bot records that moment on the
    session (`owner_said`, stamped by bot.handle_prompt).

The owner's own messages never count. Slack's own read state is not something
a bot can see (Slack exposes no read receipts to apps), so a thread read only
in Slack stays unread here until it is opened on the dashboard or replied to.

Counted from what the dashboard already reads: the transcript, through an
index that keeps, per transcript, the times of its assistant messages that
carry text (what the chat draws as reply bubbles; a turn's tool calls are one
folded line, not replies) and reads only what was appended since the last
look. A transcript not written since a thread's watermark is not read at all.

Threads that existed before unread tracking began are not all unread at once:
seen.json records when it began (`since`), and nothing older counts.
"""

from __future__ import annotations

import bisect
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import jsonstore

#: Its own file, not a field on sessions.json: the bot owns that file and the
#: dashboard only ever reads it; this one only the dashboard writes.
SEEN_NAME = "seen.json"

#: How far ahead of this machine's clock a posted watermark may be (the
#: page's clock is not trusted to mark tomorrow's replies read).
FUTURE_SLACK_S = 5.0


def parse_ts(value) -> float | None:
    """A transcript timestamp ("2026-11-21T10:00:00.123Z") or unix seconds, as
    unix seconds; None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    try:
        return float(v) if v.replace(".", "", 1).isdigit() else (
            datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())
    except (ValueError, OverflowError):
        return None


class Seen:
    """Per-thread last_seen watermarks, the owner's, written atomically.

    The dashboard has one owner (VIZ_TAILSCALE_LOGIN, or whoever is at the
    mini's loopback), so this is theirs. A watermark only moves forward.
    """

    def __init__(self, path: Path, clock=time.time):
        self._path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._data: dict | None = None
        self.error = ""

    def _loaded(self) -> dict:
        if self._data is None:
            try:
                raw = jsonstore.load(self._path, default=None)
            except Exception as e:
                # Both copies unreadable: say so, count nothing, write nothing
                # (a save would put an empty store over the evidence).
                self.error = f"{self._path.name} is unreadable: {e}"
                return {}
            if raw is not None and not isinstance(raw, dict):
                self.error = f"{self._path.name} holds {type(raw).__name__}, not its watermarks"
                return {}
            if raw is None:
                # First run: tracking starts now, so every existing reply is
                # read rather than all of them unread at once.
                raw = {"since": self._clock(), "threads": {}}
                jsonstore.save(self._path, raw)
            raw.setdefault("threads", {})
            self._data = raw
            self.error = ""
        return self._data

    def since(self) -> float:
        with self._lock:
            return float(self._loaded().get("since") or 0.0)

    def get(self, key: str) -> float:
        with self._lock:
            d = self._loaded()
            return max(float(d.get("since") or 0.0), float(d.get("threads", {}).get(key) or 0.0))

    def all(self) -> dict:
        with self._lock:
            return dict(self._loaded().get("threads") or {})

    def mark(self, key: str, at: float) -> float | None:
        """Move `key`'s watermark to `at` (never back, never past now).
        The watermark now held, or None when it could not be stored."""
        with self._lock:
            d = self._loaded()
            if self._data is None:
                return None
            at = min(float(at), self._clock() + FUTURE_SLACK_S)
            threads = d["threads"]
            if at > float(threads.get(key) or 0.0):
                threads[key] = at
                jsonstore.save(self._path, d)
            return max(float(d.get("since") or 0.0), float(threads.get(key) or 0.0))


def reply_time(entry: dict) -> float | None:
    """When this transcript line is a reply -- an assistant message with
    words in it -- its time; else None."""
    if not isinstance(entry, dict) or entry.get("type") != "assistant" or entry.get("isMeta"):
        return None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        has_text = bool(content.strip())
    else:
        has_text = any(isinstance(b, dict) and b.get("type") == "text"
                       and str(b.get("text") or "").strip() for b in content or [])
    return parse_ts(entry.get("timestamp")) if has_text else None


class ReplyIndex:
    """Per transcript, the sorted times of its replies, read incrementally."""

    def __init__(self):
        self._lock = threading.Lock()
        self._files: dict = {}           # path -> {"ino", "offset", "times"}

    def times(self, path: Path, floor: float = 0.0) -> list:
        """The reply times in `path` later than `floor` (which may be dropped
        from memory: nothing older is ever asked about again)."""
        with self._lock:
            key = str(path)
            rec = self._files.get(key)
            try:
                st = os.stat(path)
            except OSError:
                self._files.pop(key, None)
                return []
            if rec is None or rec["ino"] != st.st_ino or st.st_size < rec["offset"]:
                rec = {"ino": st.st_ino, "offset": 0, "times": []}     # new, or replaced
                self._files[key] = rec
            if st.st_size > rec["offset"]:
                with open(path, "rb") as f:
                    f.seek(rec["offset"])
                    chunk = f.read(st.st_size - rec["offset"])
                end = chunk.rfind(b"\n") + 1           # a line still being written waits
                for raw in chunk[:end].splitlines():
                    if b'"assistant"' not in raw:     # most lines: no need to parse
                        continue
                    try:
                        t = reply_time(json.loads(raw))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if t is not None and t > floor:
                        bisect.insort(rec["times"], t)
                rec["offset"] += end
            if floor and rec["times"] and rec["times"][0] <= floor:
                del rec["times"][:bisect.bisect_right(rec["times"], floor)]
            return list(rec["times"])


def count_after(times, watermark: float) -> int:
    """How many of the sorted `times` are later than `watermark`."""
    return len(times) - bisect.bisect_right(times, watermark)


def watermark(seen_at: float, owner_said) -> float:
    """The moment before which everything in a thread is read: the later of
    the dashboard's watermark (already no earlier than `since`) and the
    owner's last message there."""
    return max(seen_at, parse_ts(owner_said) or 0.0)


def unread(path: Path | None, mark: float, index: ReplyIndex, since: float = 0.0) -> int:
    """Unread replies in the transcript at `path` for watermark `mark`.
    A transcript not written since the watermark is not opened."""
    if path is None:
        return 0
    try:
        if os.stat(path).st_mtime <= mark:
            return 0
    except OSError:
        return 0
    return count_after(index.times(path, since), mark)

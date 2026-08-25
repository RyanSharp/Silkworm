"""Catch up on messages that arrived while the bot could not hear them.

Socket Mode does not queue. Anything sent while the websocket is down is not
redelivered when it comes back -- it is simply gone, and nothing anywhere
records that it existed. During the outage on 2026-08-24 five messages were
lost that way; four were noticed and re-sent by hand, and one was never
answered at all.

So on the way back up, each known thread is re-read from the Slack Web API,
which does have the history the socket does not, and anything past the
thread's watermark is handed to the ordinary prompt path. Replay is safe
because that path already guards against redelivery: `last_msg_ts` moves only
forward, and a message at or behind it is skipped. This adds no new
idempotency -- it reuses the guard restarts already depend on.

Bounded on purpose. A thread with no watermark is skipped, because "everything
ever said" is not a backlog. Old messages are skipped too: an unanswered
question from last week has either been re-asked or stopped mattering.
"""

import logging
import time

log = logging.getLogger("silkworm.backfill")

#: Anything older than this is water under the bridge.
MAX_AGE_S = 3 * 24 * 3600

#: Per thread, so one chatty gap cannot spend a day's quota on replay. What is
#: dropped is logged rather than silently forgotten.
MAX_PER_THREAD = 5


def missed(entries: dict, *, replies, bot_user_id: str, handled_subtypes,
           now: float | None = None, max_age_s: float = MAX_AGE_S,
           max_per_thread: int = MAX_PER_THREAD) -> list[dict]:
    """Message events past each thread's watermark, oldest first.

    `replies(channel, thread_ts)` returns that thread's messages; it is injected
    so this is testable without Slack.
    """
    now = time.time() if now is None else now
    out = []
    for key, entry in entries.items():
        last = entry.get("last_msg_ts")
        channel, _, thread_ts = key.partition(":")
        if not last or not thread_ts:
            continue              # no watermark: everything would look new
        try:
            floor = float(last)
        except (TypeError, ValueError):
            continue
        try:
            msgs = replies(channel, thread_ts)
        except Exception:
            log.exception("could not re-read %s", key)
            continue
        fresh = []
        for m in msgs or []:
            try:
                ts = float(m.get("ts", 0))
            except (TypeError, ValueError):
                continue
            if ts <= floor or now - ts > max_age_s:
                continue
            if m.get("bot_id") or m.get("user") == bot_user_id or not m.get("user"):
                continue
            subtype = m.get("subtype")
            if subtype and subtype not in handled_subtypes:
                continue
            fresh.append({"channel": channel, "thread_ts": thread_ts,
                          "ts": m["ts"], "user": m.get("user", ""),
                          "text": m.get("text", ""), "files": m.get("files") or [],
                          "channel_type": "im",
                          **({"subtype": subtype} if subtype else {})})
        fresh.sort(key=lambda e: float(e["ts"]))
        if len(fresh) > max_per_thread:
            dropped = len(fresh) - max_per_thread
            # Loudly: a silent cap reads as "nothing was missed".
            log.warning("%s: %d missed message(s), replaying the newest %d and "
                        "skipping %d older", key, len(fresh), max_per_thread, dropped)
            fresh = fresh[-max_per_thread:]
        out.extend(fresh)
    out.sort(key=lambda e: float(e["ts"]))
    return out

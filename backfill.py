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

import collections
import logging
import time

log = logging.getLogger("silkworm.backfill")

#: Anything older than this is water under the bridge.
MAX_AGE_S = 3 * 24 * 3600

#: Per thread, so one chatty gap cannot spend a day's quota on replay. What is
#: dropped is logged rather than silently forgotten.
MAX_PER_THREAD = 5

#: Tries per page of thread history. A long thread's page can be cut off
#: mid-download (2026-10-02: IncompleteRead, 74664 of 415061 bytes), and one
#: such hiccup used to cost the whole thread its replay.
ATTEMPTS = 3

#: Slack error codes worth another try. Any other code (missing_scope,
#: channel_not_found, invalid_auth...) will say the same thing again.
TRANSIENT_ERRORS = {"ratelimited", "internal_error", "fatal_error",
                    "service_unavailable", "request_timeout"}


def _permanent(e: Exception) -> bool:
    """A Slack API refusal that a retry cannot change; transport errors are not."""
    resp = getattr(e, "response", None)
    code = resp.get("error") if hasattr(resp, "get") else None
    return bool(code) and code not in TRANSIENT_ERRORS


#: Messages asked for per page. Smaller pages are less to lose to a cut-off.
PAGE = 100


def read_thread(client, channel: str, thread_ts: str, *, oldest: str | None = None,
                keep_last: int | None = None, max_pages: int = 50,
                page: int = PAGE, attempts: int = ATTEMPTS,
                sleep=time.sleep) -> list[dict]:
    """Every message of a thread (past `oldest`, if given), oldest first.

    conversations.replies returns a thread oldest-first, one page at a time, so
    a single unpaginated call on a long thread sees only its beginning -- never
    the recent end that a watermark or a fresh session actually needs. This
    follows the cursor to the end, retrying each page before giving up.
    `keep_last` holds only the newest n, for callers that want the tail.
    """
    out = collections.deque(maxlen=keep_last) if keep_last else []
    seen = set()       # Slack repeats the parent at the top of every page
    cursor = None
    for n in range(max_pages):
        args = {"channel": channel, "ts": thread_ts, "limit": page}
        if oldest:
            args["oldest"] = oldest
            args["inclusive"] = False
        if cursor:
            args["cursor"] = cursor
        for attempt in range(1, attempts + 1):
            try:
                resp = client.conversations_replies(**args)
                break
            except Exception as e:
                if attempt == attempts or _permanent(e):
                    raise
                log.warning("reading %s:%s page %d failed (%s); retrying",
                            channel, thread_ts, n + 1, e)
                sleep(2 * attempt)
        for m in resp.get("messages") or []:
            if m.get("ts") not in seen:
                seen.add(m.get("ts"))
                out.append(m)
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return list(out)
    log.warning("%s:%s: stopped after %d pages; the thread goes on",
                channel, thread_ts, max_pages)
    return list(out)


def missed(entries: dict, *, replies, bot_user_id: str, handled_subtypes,
           now: float | None = None, max_age_s: float = MAX_AGE_S,
           max_per_thread: int = MAX_PER_THREAD) -> list[dict]:
    """Message events past each thread's watermark, oldest first.

    `replies(channel, thread_ts, oldest)` returns that thread's messages after
    `oldest` (the watermark) -- in practice `read_thread`, which pages and
    retries. It is injected so this is testable without Slack.
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
            msgs = replies(channel, thread_ts, last)
        except Exception:
            # After read_thread's own retries: loudly, since a thread skipped
            # here is a thread whose missed messages are never answered.
            log.exception("could not re-read %s after retries; its missed "
                          "messages, if any, will not be replayed", key)
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

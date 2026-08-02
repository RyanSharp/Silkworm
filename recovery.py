"""Recovery for turns interrupted by a bot restart.

A turn runs inside the bot process, but the `claude` child is started in its own
process group — so when the bot is restarted mid-turn (a deploy, a crash), the
child keeps working and writes its answer to the session transcript. Nobody is
left reading its output, so the reply is lost, the placeholder message stays
frozen on "…", and the ⏳ reaction never clears.

So each turn records a `pending` marker on its session before running. On the
next start the bot walks those markers and, for each one, waits briefly for the
orphaned child to finish, then recovers the reply straight out of the transcript
and posts it. If nothing was produced, the turn is reported as interrupted
instead. Either way the thread ends in a truthful state rather than a stuck one.
"""

import json
import logging
import subprocess
import time
from datetime import datetime, timezone

from harvester import find_transcript

log = logging.getLogger("silkworm.recovery")

WAIT_S = 600      # how long to keep waiting on a still-running orphan
POLL_S = 5


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def mark_pending(store, key: str, *, msg_ts: str | None, progress_ts: str | None,
                 session_id: str | None, prompt: str = "") -> None:
    """Record that a turn is in flight, so a restart can pick it up."""
    store.update(key, pending={
        "started": now_iso(),
        "msg_ts": msg_ts,
        "progress_ts": progress_ts,
        "session_id": session_id,
        "prompt": prompt[:200],
    })


def note_session(store, key: str, session_id: str) -> None:
    """Attach the session id once Claude reports it (new sessions don't have
    one until init, and without it we can't find the transcript later)."""
    entry = store.get(key) or {}
    pending = entry.get("pending")
    if pending and not pending.get("session_id"):
        pending["session_id"] = session_id
        store.update(key, pending=pending)


def clear_pending(store, key: str) -> None:
    store.update(key, pending=None)


def _claude_alive(session_id: str) -> bool:
    if not session_id:
        return False
    out = subprocess.run(["pgrep", "-f", f"claude.*{session_id}"],
                         capture_output=True, text=True)
    return bool(out.stdout.split())


def final_reply(path, since_iso: str) -> str:
    """Text of the last assistant message written after `since_iso`."""
    latest = ""
    for line in path.read_text().splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "assistant":
            continue
        ts = d.get("timestamp", "")
        if since_iso and ts and ts <= since_iso:
            continue
        content = (d.get("message") or {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text")
        else:
            text = ""
        if text.strip():
            latest = text.strip()  # keep overwriting; the last one is the reply
    return latest


def _await_reply(session_id: str, since_iso: str, wait_s: int) -> str:
    """Poll the transcript for a reply, while the orphaned child still lives."""
    deadline = time.time() + wait_s
    while True:
        path = find_transcript(session_id) if session_id else None
        if path:
            text = final_reply(path, since_iso)
            if text and not _claude_alive(session_id):
                return text          # finished and written
        if not _claude_alive(session_id):
            return final_reply(path, since_iso) if path else ""
        if time.time() >= deadline:
            return final_reply(path, since_iso) if path else ""
        time.sleep(POLL_S)


def recover(store, *, finalize, reactions_for, say, wait_s: int = WAIT_S) -> dict:
    """Resolve every pending turn left behind by the previous process.

    `finalize(channel, thread_ts, ts, text)` edits the frozen placeholder
    message, `reactions_for(channel, thread_ts, msg_ts)` builds a
    ThreadReactions, and `say(channel, thread_ts, text)` posts a fresh message.
    """
    stats = {"recovered": 0, "interrupted": 0}
    for key, entry in store.all().items():
        pending = entry.get("pending")
        if not pending:
            continue
        channel, _, thread_ts = key.partition(":")
        sid = pending.get("session_id") or entry.get("session_id") or ""
        started = pending.get("started", "")
        log.info("recovering interrupted turn on %s (session=%s)", key, sid[:8] or "?")

        try:
            text = _await_reply(sid, started, wait_s)
        except Exception:
            log.exception("recovery read failed for %s", key)
            text = ""

        rx = reactions_for(channel, thread_ts, pending.get("msg_ts"))
        note = ("_:leftwards_arrow_with_hook: Recovered after a restart — "
                "this reply was produced but never delivered._")
        try:
            if text:
                body = f"{note}\n\n{text}"
                if pending.get("progress_ts"):
                    finalize(channel, thread_ts, pending["progress_ts"], body)
                else:
                    say(channel, thread_ts, body)
                rx.done()
                stats["recovered"] += 1
            else:
                msg = (":warning: _This turn was interrupted by a bot restart and "
                       "produced no reply. Send the message again to retry._")
                if pending.get("progress_ts"):
                    finalize(channel, thread_ts, pending["progress_ts"], msg)
                else:
                    say(channel, thread_ts, msg)
                rx.failed()
                stats["interrupted"] += 1
        except Exception:
            log.exception("recovery delivery failed for %s", key)
        finally:
            clear_pending(store, key)

    if stats["recovered"] or stats["interrupted"]:
        log.info("recovery: %d reply/replies recovered, %d interrupted",
                 stats["recovered"], stats["interrupted"])
    return stats

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
import threading
import time
import uuid
from datetime import datetime, timezone

import procs
from harvester import find_transcript

log = logging.getLogger("silkworm.recovery")

WAIT_S = 3600     # cap on waiting for a still-running orphan
POLL_S = 5
STILL_RUNNING = object()  # sentinel: gave up waiting, but the turn is genuinely live


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def mark_pending(store, key: str, *, msg_ts: str | None, progress_ts: str | None,
                 session_id: str | None, prompt: str = "", turn: str = "") -> str:
    """Record that a turn is in flight, so a restart can pick it up, and
    return the session it runs on.

    A turn starting a fresh session has no id to resume, so one is chosen here
    and the caller names the session with it (run_turn's new_session_id). The
    marker then says which session is running from the moment it is written,
    not once Claude gets round to reporting it -- or never, for a run whose
    id was only ever kept on its task: without it the dashboard has no
    transcript to follow and no process to find, and calls the run stalled.

    `turn` names the turn that wrote it, so that turn can later clear its own
    marker and no other (see clear_pending).
    """
    sid = session_id or str(uuid.uuid4())
    store.update(key, pending={
        "started": now_iso(),
        "msg_ts": msg_ts,
        "progress_ts": progress_ts,
        "session_id": sid,
        "prompt": prompt[:200],
        "turn": turn,
    })
    return sid


def note_session(store, key: str, session_id: str, *, turn: str = "") -> None:
    """Correct the marker's session id to the one Claude reports, should it
    differ from the one mark_pending recorded. Given `turn`, only that turn's
    marker: a review sharing its implementor's thread writes its own."""
    entry = store.get(key) or {}
    pending = entry.get("pending")
    if not pending or (turn and pending.get("turn") != turn):
        return
    if pending.get("session_id") != session_id:
        pending["session_id"] = session_id
        store.update(key, pending=pending)


def clear_pending(store, key: str, *, turn: str = "") -> None:
    """Forget a thread's in-flight marker.

    Given `turn`, only the marker that turn wrote: a turn ending after its
    thread lock is released can otherwise clear the marker of the next turn on
    the same thread -- a review starting on its implementor's thread, say --
    and a restart then finds nothing to recover.
    """
    if turn:
        pending = (store.get(key) or {}).get("pending")
        if not pending or pending.get("turn") != turn:
            return
    store.update(key, pending=None)


def _claude_alive(session_id: str) -> bool:
    return procs.session_alive(session_id)


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


#: Keys a recovery pass is currently resolving. The startup pass can wait up to
#: an hour on a live child while the periodic sweep runs alongside it, so two
#: passes really can meet on one thread -- and both would post the same reply.
_inflight: set[str] = set()
_inflight_lock = threading.Lock()


def _claim(key: str) -> bool:
    with _inflight_lock:
        if key in _inflight:
            return False
        _inflight.add(key)
        return True


def _release(key: str) -> None:
    with _inflight_lock:
        _inflight.discard(key)


def _await_reply(session_id: str, since_iso: str, wait_s: int):
    """Wait for the orphaned child to finish, then read its reply.

    Only a *finished* child gives a trustworthy answer: while it is still
    running, the newest assistant message is mid-turn narration, not the reply.
    So if the wait cap is hit with the child still alive we return
    STILL_RUNNING and leave the turn pending rather than posting a fragment.
    """
    deadline = time.time() + wait_s
    while True:
        path = find_transcript(session_id) if session_id else None
        if not _claude_alive(session_id):
            return final_reply(path, since_iso) if path else ""
        if time.time() >= deadline:
            return STILL_RUNNING
        time.sleep(POLL_S)


def recover(store, *, finalize, reactions_for, say, wait_s: int = WAIT_S,
            on_outcome=None, skip=None) -> dict:
    """Resolve every pending turn left behind by the previous process.

    `finalize(channel, thread_ts, ts, text)` edits the frozen placeholder
    message, `reactions_for(channel, thread_ts, msg_ts)` builds a
    ThreadReactions, and `say(channel, thread_ts, text)` posts a fresh message.
    `on_outcome(key, recovered)` is told how each thread ended, so a caller can
    resolve whatever else it tracks about that turn. `skip` is the set of keys
    this process is itself running: their pending markers belong to a live
    handler that will clear them, and touching one would post the reply twice.
    """
    stats = {"recovered": 0, "interrupted": 0, "still_running": 0}
    for key, entry in store.all().items():
        pending = entry.get("pending")
        if not pending or key in (skip or ()):
            continue
        if not _claim(key):
            continue          # another pass is already resolving this thread
        try:
            channel, _, thread_ts = key.partition(":")
            sid = pending.get("session_id") or entry.get("session_id") or ""
            started = pending.get("started", "")
            # Logged only once something is actually resolved: the periodic
            # sweep visits every pending marker, and announcing each visit
            # would bury the passes that did something.
            try:
                text = _await_reply(sid, started, wait_s)
            except Exception:
                log.exception("recovery read failed for %s", key)
                text = ""
            if text is STILL_RUNNING:
                # Genuinely mid-turn after a long wait: the ⏳ is accurate, so leave
                # the marker in place for the next start rather than guessing. No
                # event -- nothing happened to it, and it would repeat every restart.
                # Only a wait that actually elapsed is worth a warning; a sweep
                # (wait_s=0) merely observed a healthy turn still in progress.
                if wait_s:
                    log.warning("gave up waiting on %s — turn still running, left pending", key)
                else:
                    log.debug("%s still running, left pending", key)
                stats["still_running"] += 1
                continue

            log.info("recovering interrupted turn on %s (session=%s)", key, sid[:8] or "?")
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
                    store.add_event(key, "recovered", "reply rescued from the transcript")
                    stats["recovered"] += 1
                    if on_outcome:
                        on_outcome(key, True)
                else:
                    msg = (":warning: _This turn was interrupted by a bot restart and "
                           "produced no reply. Send the message again to retry._")
                    if pending.get("progress_ts"):
                        finalize(channel, thread_ts, pending["progress_ts"], msg)
                    else:
                        say(channel, thread_ts, msg)
                    rx.failed()
                    store.add_event(key, "lost", "interrupted turn produced no reply")
                    stats["interrupted"] += 1
                    if on_outcome:
                        on_outcome(key, False)
            except Exception:
                log.exception("recovery delivery failed for %s", key)
            finally:
                clear_pending(store, key)
        finally:
            # Always, including the still-running path: a leaked claim
            # would lock that thread out of every future pass.
            _release(key)

    if any(stats.values()):
        log.info("recovery: %d recovered, %d interrupted, %d still running",
                 stats["recovered"], stats["interrupted"], stats["still_running"])
    return stats

"""Slack bot that gives every thread its own Claude Code session.

DMs and @-mentions get replies in a thread; each thread maps to one headless
Claude session (resumed on every message). Features: live progress updates,
cost footers, per-thread model switching, !stop, file exchange, thread-context
bootstrap, Slack approval buttons, per-channel working dirs, session hygiene,
and a user allowlist.
"""

import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
import urllib.request
import uuid
from collections import OrderedDict
from pathlib import Path

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import backfill
import branches
import credentials
import daemons
import defer
import email_ingest
import harvester
import home
import holding
import jsonstore
import learnings_git
import repos
import revision
import roles
import scoping
import slack_health
import slacklinks
import tasks
import procs
import projects
import recovery
import retry
import merge
import summaries
import verify
import worktrees
from approvals import ApprovalManager, describe_tool
from claude_runner import ClaudeError, ClaudeStopped, ClaudeTimeout, run_turn
from learnings import TYPES as LEARNING_TYPES, LearningStore, render_block
from localserver import LocalServer
from store import SessionStore

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("silkworm")

BASE_DIR = Path(__file__).resolve().parent

#: Resolved once, at import, because it is a fact about *this process* and the
#: checkout keeps moving underneath it -- Silkworm auto-merges onto its own
#: main, so asking git later answers a different question than "what am I
#: running". Everything that reports drift compares against this.
REVISION = revision.of(BASE_DIR)

# --- Claude config ---------------------------------------------------------
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_CWD = Path(os.environ.get("CLAUDE_CWD", BASE_DIR / "workspace"))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL")
CLAUDE_EXTRA_ARGS = shlex.split(os.environ.get("CLAUDE_EXTRA_ARGS", ""))
# A turn may run as long as it is still working. The old 900s wall clock cut
# off real work -- a strategy backtest, a long refactor -- because elapsed
# time cannot tell progress from a wedge. 0 means no absolute cap.
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "0"))
# What actually ends a turn: silence. Generous, because a single tool call
# is legitimately quiet while it runs (Claude Code caps Bash at 10 minutes),
# so this only fires on a process that has genuinely stopped doing anything.
CLAUDE_IDLE_TIMEOUT = int(os.environ.get("CLAUDE_IDLE_TIMEOUT", "1800"))
# How many queued tasks run at once. One by default: an agent can work
# indefinitely, so a single worker burns the queue down over time, and nothing
# has to be merged against anything else -- the parallelism was buying speed
# nobody was waiting on, at the cost of conflicts somebody would be. Worktrees
# make raising it safe; each worker is a live Claude session, so it is a quota
# decision as much as a concurrency one.
TASK_WORKERS = int(os.environ.get("TASK_WORKERS", "1"))
#: Absolute, because a turn's PATH is not ours to assume.
SILKWORM_BIN = str(Path(__file__).resolve().parent / "bin" / "silkworm")
NAMING_MODEL = os.environ.get("NAMING_MODEL", "haiku")  # empty string disables
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "haiku")  # empty string disables
HARVEST_MODEL = os.environ.get("HARVEST_MODEL", "sonnet")
HARVEST_INTERVAL_H = float(os.environ.get("HARVEST_INTERVAL_H", "6"))  # 0 disables auto-harvest

# --- Gmail watching (off unless credentials are present) ---
GMAIL_USER = os.environ.get("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
GMAIL_HOST = os.environ.get("GMAIL_HOST", "imap.gmail.com").strip()
GMAIL_MAILBOX = os.environ.get("GMAIL_MAILBOX", "INBOX").strip()
GMAIL_POLL_MIN = float(os.environ.get("GMAIL_POLL_MIN", "15"))
GMAIL_MAX_PER_RUN = int(os.environ.get("GMAIL_MAX_PER_RUN", "25"))
# Inbox triage proposes tasks from mail that looks like it wants a person. Off
# by default: you already read your inbox, so re-surfacing it mostly duplicates
# work you do anyway. Label-driven fact filing is the half that earns its keep.
GMAIL_TRIAGE = os.environ.get("GMAIL_TRIAGE", "").strip().lower() in ("1", "true", "yes")
EMAIL_STATE_FILE = BASE_DIR / "email_state.json"

# skip  = --dangerously-skip-permissions (full autonomy)
# slack = gated: every non-trivial tool call posts Approve/Deny buttons
# gated = plain --permission-mode (prompted actions are denied in headless)
CLAUDE_APPROVAL_MODE = os.environ.get("CLAUDE_APPROVAL_MODE", "skip").lower()
CLAUDE_PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits")
APPROVAL_PORT = int(os.environ.get("APPROVAL_PORT", "8787"))
APPROVAL_TIMEOUT = int(os.environ.get("APPROVAL_TIMEOUT", "300"))
APPROVAL_AUTO_ALLOW = {
    t.strip() for t in os.environ.get(
        "APPROVAL_AUTO_ALLOW",
        "Read,Glob,Grep,Task,TodoWrite,WebFetch,WebSearch,NotebookRead",
    ).split(",") if t.strip()
}

# --- Slack config ----------------------------------------------------------
ALLOWED_USERS = {
    u.strip() for u in os.environ.get("SLACK_ALLOWED_USERS", "").split(",") if u.strip()
}

# {"C0123ABC": "/path/to/repo", "channel-name": "/other/path"}  (JSON or a=b,c=d)
def _parse_channel_dirs(raw: str) -> dict[str, str]:
    raw = raw.strip()
    if not raw:
        return {}
    try:
        return {str(k): str(v) for k, v in json.loads(raw).items()}
    except json.JSONDecodeError:
        pairs = [p.split("=", 1) for p in raw.split(",") if "=" in p]
        return {k.strip().lstrip("#"): v.strip() for k, v in pairs}

CHANNEL_DIRS = _parse_channel_dirs(os.environ.get("CLAUDE_CHANNEL_DIRS", ""))

# How old an *empty* session record has to be before it is forgotten. Records
# with anything in them -- a title, a summary, cost history, files -- are kept
# for ever and retired by hiding instead; see SessionStore.forget_empty.
SESSION_MAX_AGE_DAYS = float(os.environ.get("SESSION_MAX_AGE_DAYS", "30"))
# Finished tasks are kept forever -- they are the project's history -- but
# past this the reply text and event log are dropped from them.
TASK_COMPACT_AFTER_DAYS = float(os.environ.get("TASK_COMPACT_AFTER_DAYS", "14"))
SLACK_MSG_LIMIT = 3800

# When a thread is checked out to the terminal (!terminal), a Slack message
# normally gets held with a warning. With this on, Slack wins: the terminal
# session is force-closed and the message runs.
HANDOFF_SLACK_WINS = os.environ.get("HANDOFF_SLACK_WINS", "0") not in ("0", "false", "no", "")

# Channel (or DM channel) where terminal-initiated sessions (bin/silkworm)
# get their Slack anchor thread. Falls back to the bot's most recent DM.
SILKWORM_HOME_CHANNEL = os.environ.get("SILKWORM_HOME_CHANNEL", "").strip()

OUTBOX_ROOT = BASE_DIR / "outbox"
ARTIFACTS_ROOT = BASE_DIR / "artifacts"
# Uploads live here, not in the thread's working directory. A thread's cwd is
# usually a git repo, and writing screenshots into it leaves untracked noise in
# `git status` that is one careless `git add -A` from being committed.
UPLOADS_ROOT = BASE_DIR / "uploads"
HOOK_PATH = BASE_DIR / "approval_hook.py"

CLAUDE_CWD.mkdir(parents=True, exist_ok=True)
OUTBOX_ROOT.mkdir(exist_ok=True)
ARTIFACTS_ROOT.mkdir(exist_ok=True)
UPLOADS_ROOT.mkdir(exist_ok=True)

# --- Shared state -----------------------------------------------------------
store = SessionStore(BASE_DIR / "sessions.json")
task_store = tasks.TaskStore(BASE_DIR / "tasks.json")
project_store = projects.ProjectStore(BASE_DIR / "projects.json")
# Created here, not beside its watchdog: /status reads it and the local server
# starts long before the watchdog thread does.
slack = slack_health.Health()
# LEARNINGS_FILE can point at a file inside a git repo to share across machines.
LEARNINGS_FILE = Path(os.environ.get("LEARNINGS_FILE", BASE_DIR / "learnings.json")).expanduser()
LEARNINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
LEARNINGS_AUTOSYNC = os.environ.get("LEARNINGS_AUTOSYNC", "0") not in ("0", "false", "no", "")
learnings = LearningStore(LEARNINGS_FILE)
_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()
RUNNING: dict[str, object] = {}          # thread key -> RunHandle
#: task id -> RunHandle, for the same children keyed the way the board sees
#: them. RUNNING alone cannot answer "is *this task* still going", and that is
#: the question both cancelling and the worktree sweep need answered: a cancel
#: that only rewrites the record leaves an agent working in a checkout nobody
#: is protecting any more, which has already cost a task its first pass.
RUNNING_TASKS: dict[str, object] = {}
#: Closed when a turn dies of something global (quota, overload, network), so
#: the queue runner stops claiming until it is plausibly over instead of
#: walking the whole queue into the same wall. See retry.Hold.
RUNNER_HOLD = retry.Hold()
ACTIVE_SESSIONS: dict[str, tuple[str, str]] = {}  # session_id -> (channel, thread_ts)
_seen_events: OrderedDict[str, None] = OrderedDict()
_users_cache: dict[str, str] = {}
_channels_cache: dict[str, str] = {}


def _thread_lock(key: str) -> threading.Lock:
    with _thread_locks_guard:
        return _thread_locks.setdefault(key, threading.Lock())


#: Serialises turns that share a git working tree, keyed by resolved path.
_repo_locks: dict[str, threading.Lock] = {}
_repo_locks_guard = threading.Lock()


def _repo_lock(cwd):          # -> threading.Lock | None
    # Unannotated on purpose: threading.Lock is a factory function, not a
    # class, so `threading.Lock | None` raises TypeError at import on 3.12.
    """The lock for a checkout, or None if this directory is not one.

    Only actual repo roots are serialised. The shared scratch directory holds
    a dozen unrelated projects, and locking that would queue every thread
    behind every other for no benefit.
    """
    try:
        path = Path(cwd).resolve()
    except Exception:
        return None
    if not (path / ".git").exists():
        return None
    with _repo_locks_guard:
        return _repo_locks.setdefault(str(path), threading.Lock())


@contextlib.contextmanager
def repo_guard(cwd, progress=None):
    """Hold a checkout for the duration of a turn.

    Thread locks are keyed by thread, so nothing stopped two sessions editing
    one working tree at once -- and filing a task under a project opens a
    *new* thread in that project's directory, which makes the collision the
    normal case rather than a corner. Two agents in one checkout do not merely
    race on files: one running `git checkout` moves the ground under the other.

    Taken inside the thread lock by the turns that use it, so the ordering is
    consistent and cannot deadlock. A landing takes it with no thread lock at
    all and holds it for two full suite runs, which is the point -- a live turn
    in the same checkout waits rather than editing under a rebase -- and it
    never goes on to want a thread lock, so there is still no cycle.
    """
    lock = _repo_lock(cwd)
    if lock is None:
        yield
        return
    if not lock.acquire(blocking=False):
        log.info("waiting on another turn already working in %s", cwd)
        if progress:
            progress.update(":hourglass_flowing_sand: _Waiting for another thread "
                            "working in the same checkout…_")
        lock.acquire()
    try:
        yield
    finally:
        lock.release()


def _dedup(channel: str, ts: str) -> bool:
    """True if this event was already handled (mention + DM double-delivery)."""
    key = f"{channel}:{ts}"
    if key in _seen_events:
        return True
    _seen_events[key] = None
    while len(_seen_events) > 500:
        _seen_events.popitem(last=False)
    return False


def _already_handled(key: str, msg_ts: str) -> bool:
    """True if this thread has already started a turn for this message.

    _seen_events only lives as long as the process, so an event Slack
    redelivers after a restart (its ack died with the old process) looks new
    and runs a second time. Message timestamps within a thread only move
    forward, so anything at or behind the last one we picked up is a
    redelivery. The watermark is written when a turn *starts*, which is safe
    because recovery.py delivers the reply if that turn is cut short.
    """
    last = (store.get(key) or {}).get("last_msg_ts")
    if not last:
        return False
    try:
        return float(msg_ts) <= float(last)
    except (TypeError, ValueError):
        return False


# --- Slack formatting -------------------------------------------------------
MENTION_RE = re.compile(r"<@[UW][A-Z0-9]+>")


def to_mrkdwn(text: str) -> str:
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<\2|\1>", text)
    return text


def chunk(text: str, limit: int = SLACK_MSG_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit and current:
            chunks.append(current)
            current = ""
        while len(line) > limit:  # single monster line
            chunks.append(line[:limit])
            line = line[limit:]
        current += line
    if current:
        chunks.append(current)
    return chunks


def fmt_duration(ms: int) -> str:
    secs = int(ms / 1000)
    return f"{secs // 60}m{secs % 60:02d}s" if secs >= 60 else f"{secs}s"


def fmt_age(ts: float) -> str:
    days = (time.time() - ts) / 86400
    if days < 1:
        return f"{int(days * 24)}h ago"
    return f"{int(days)}d ago"


class ProgressMessage:
    """Single placeholder message updated with live tool activity."""

    def __init__(self, client, channel: str, thread_ts: str):
        self.client = client
        self.channel = channel
        self.ts = None
        self._last_update = 0.0
        try:
            resp = client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=":hourglass_flowing_sand: _Starting Claude…_")
            self.ts = resp["ts"]
        except Exception:
            log.exception("failed to post progress message")

    def update(self, text: str) -> None:
        if not self.ts:
            return
        now = time.monotonic()
        if now - self._last_update < 1.5:
            return
        self._last_update = now
        try:
            self.client.chat_update(channel=self.channel, ts=self.ts, text=text)
        except Exception:
            pass

    def finalize(self, text: str) -> None:
        if not self.ts:
            return
        try:
            self.client.chat_update(channel=self.channel, ts=self.ts, text=text)
        except Exception:
            log.exception("failed to finalize progress message")
            self.ts = None

    def delete(self) -> None:
        """Remove the placeholder entirely, for a turn with nothing to say."""
        if not self.ts:
            return
        try:
            self.client.chat_delete(channel=self.channel, ts=self.ts)
        except Exception:
            log.exception("failed to delete progress message")
        self.ts = None


WORKING, DONE, FAILED = "hourglass", "white_check_mark", "bangbang"


class ThreadReactions:
    """Status reactions on the thread parent and the message being answered.

    The parent carries the thread's *current* state, so a new turn clears the
    last outcome before working; each answered message keeps its own result
    permanently, as a record of how that turn went.
    """

    def __init__(self, client, channel: str, thread_ts: str, msg_ts: str | None):
        self.client = client
        self.channel = channel
        self.parent = thread_ts
        # In a DM the first message *is* the thread parent — reacting to both
        # would double up on one message. Web-relayed prompts have no real ts.
        self.msg = msg_ts if msg_ts and msg_ts != thread_ts else None

    def _apply(self, ts: str | None, add: str | None = None, remove: tuple = ()) -> None:
        if not ts:
            return
        for name in remove:
            self._call(self.client.reactions_remove, ts, name)
        if add:
            self._call(self.client.reactions_add, ts, name=add)

    def _call(self, fn, ts: str, name: str) -> None:
        try:
            fn(channel=self.channel, timestamp=ts, name=name)
        except Exception as e:
            # already_reacted / no_reaction just mean we're already in the
            # desired state; message_not_found means the message is gone.
            err = getattr(getattr(e, "response", None), "data", {}) or {}
            if err.get("error") not in ("already_reacted", "no_reaction",
                                        "message_not_found"):
                log.debug("reaction %s on %s failed: %s", name, ts, e)

    def working(self) -> None:
        self._apply(self.parent, add=WORKING, remove=(DONE,))
        self._apply(self.msg, add=WORKING)

    def done(self) -> None:
        self._apply(self.parent, add=DONE, remove=(WORKING, FAILED))
        self._apply(self.msg, add=DONE, remove=(WORKING,))

    def failed(self) -> None:
        self._apply(self.parent, add=FAILED, remove=(WORKING,))
        self._apply(self.msg, add=FAILED, remove=(WORKING,))

    def cleared(self) -> None:
        """Neither outcome — e.g. the user stopped the turn."""
        self._apply(self.parent, remove=(WORKING,))
        self._apply(self.msg, remove=(WORKING,))


# --- Permission / approval plumbing ----------------------------------------

def permission_args() -> list[str]:
    if CLAUDE_APPROVAL_MODE == "skip":
        return ["--dangerously-skip-permissions"]
    if CLAUDE_APPROVAL_MODE == "slack":
        settings = {"hooks": {"PreToolUse": [{
            "matcher": "*",
            "hooks": [{"type": "command",
                       "command": f'"{sys.executable}" "{HOOK_PATH}"',
                       "timeout": APPROVAL_TIMEOUT + 30}],
        }]}}
        return ["--permission-mode", "dontAsk", "--settings", json.dumps(settings)]
    return ["--permission-mode", CLAUDE_PERMISSION_MODE]


def claude_env(key: str = "", defers: int = 0, role: str = "") -> dict:
    env = dict(os.environ)
    env["SILKWORM_BOT"] = "1"  # lets the global session_hook ignore our own runs
    # So a turn can schedule a wake-up against its own thread rather than
    # holding itself open until whatever it is watching finishes.
    env["SILKWORM_PORT"] = str(APPROVAL_PORT)
    if key:
        env["SILKWORM_THREAD"] = key
    env["SILKWORM_DEFERS"] = str(defers or 0)
    # Which role this run *is*, so the filing path can decide where its work
    # lands rather than believing what it claims. Set here and nowhere else: a
    # restricted role's allowlist is prefix-matched against the command as
    # typed, so it has no way to put an assignment in front of the CLI and
    # describe itself as something less supervised.
    env["SILKWORM_ROLE"] = role or "assistant"
    if CLAUDE_APPROVAL_MODE == "slack":
        env["SLACK_BOT_APPROVAL_PORT"] = str(APPROVAL_PORT)
        env["SLACK_BOT_APPROVAL_TIMEOUT"] = str(APPROVAL_TIMEOUT)
    return env


def name_thread(key: str, prompt: str, reply: str) -> None:
    """Title a new thread with a cheap model; runs in the background."""
    if not NAMING_MODEL:
        return
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", "--model", NAMING_MODEL, "--output-format", "text"],
            input=("Write a terse 3-6 word title for this conversation. "
                   "Reply with the title only — no quotes, no punctuation at the end.\n\n"
                   f"User: {prompt[:500]}\n\nAssistant: {reply[:500]}"),
            capture_output=True, text=True, timeout=60, cwd=CLAUDE_CWD, env=claude_env())
        title = proc.stdout.strip().strip("\"'").splitlines()
        title = title[0].strip()[:60] if title else ""
        if title:
            store.update(key, title=title)
            log.info("named thread %s: %r", key, title)
    except Exception:
        log.exception("naming failed for %s", key)


def fail_or_retry(task_id: str | None, error: str, started: bool = True) -> bool:
    """Park a transient failure for a later retry instead of asking for help.

    Returns True if it was parked. Quota exhaustion and API overload are not
    failures a person can do anything about, so surfacing them would just
    train you to ignore the list.

    A transient failure is not about this task, so it also holds the queue
    runner off until the condition is plausibly over -- whether or not this
    task is parked. And `started=False` -- the turn died before its first tool
    call -- refunds the attempt the run cost: an outage that never let the
    work begin must not spend the budget that decides when a person looks.
    """
    kind = retry.classify(error)
    if kind:
        RUNNER_HOLD.close(retry.wait_until(kind, error), error)
    if not task_id:
        return False
    task = task_store.get(task_id) or {}
    if kind and not started:
        task = task_store.refund_attempt(task_id) or task
    plan = retry.retry_at(error, task.get("attempts") or 0,
                          false_starts=task.get("false_starts") or 0)
    if not plan:
        return False
    kind, when = plan
    try:
        task_store.update(task_id, retry_at=when,
                          # Hand it to the runner: whatever was driving it (a
                          # Slack handler) is gone by the time it retries.
                          driver="queue")
        task_store.transition(task_id, tasks.BLOCKED, retry.describe(kind, when))
    except Exception:
        log.exception("could not park %s for retry", task_id)
        return False
    log.info("task %s parked: %s", task_id, retry.describe(kind, when))
    return True


def stop_task(task_id: str) -> bool:
    """Kill the child a task is running, if this process is running one.

    What `!stop` does, reachable by task id rather than by thread. False means
    there was nothing of ours to stop -- the task finished, or it was orphaned
    by a restart and its child (if any) belongs to reap_runaways now.
    """
    handle = RUNNING_TASKS.get(task_id)
    if not handle:
        return False
    try:
        handle.stop()
    except Exception:
        log.exception("could not stop the child running task %s", task_id)
        return False
    log.info("stopped the child running task %s", task_id)
    return True


def task_state(task_id: str | None, state: str, detail: str = "") -> None:
    """Move a task's state without ever breaking the turn it describes.

    The task record is bookkeeping around the reply; a lifecycle complaint must
    never cost the user their answer, so a refused transition is logged loudly
    and swallowed rather than raised.
    """
    if not task_id:
        return
    try:
        task = task_store.transition(task_id, state, detail)
        if state == tasks.DONE:
            # This thread just produced an answer, so any earlier failed turn
            # on it has been overtaken by events.
            task_store.supersede_failed(task.get("thread", ""),
                                        task.get("created") or time.time())
    except tasks.InvalidTransition:
        log.warning("task %s could not move to %s (bug in the executor's state handling)",
                    task_id, state)
    except Exception:
        log.exception("task %s state update to %s failed", task_id, state)


def refresh_brief(slug: str, event: str) -> None:
    """Rewrite a project's brief in the background after something happened.

    Rewritten rather than appended: a brief rides on every prompt for the
    project, so it has to stay a short living document rather than a log.
    """
    if not slug or not SUMMARY_MODEL or not event.strip():
        return

    def run():
        try:
            # Read from the file, not the record: the brief moved to
            # CLAUDE.md, and reading a field that no longer exists would make
            # every rewrite start from scratch and discard what was learned.
            current = project_store.brief_for(slug)
            prompt = projects.BRIEF_PROMPT.format(
                brief=current or "(nothing yet)", event=event[:3000])
            proc = subprocess.run(
                [CLAUDE_BIN, "-p", "--model", SUMMARY_MODEL, "--output-format", "text"],
                input=prompt, capture_output=True, text=True, timeout=120,
                cwd=str(CLAUDE_CWD), env=claude_env())
            text = " ".join(proc.stdout.split())
            if text:
                project_store.set_brief(slug, text)
                log.info("brief updated for project %s (%d chars)", slug, len(text))
        except Exception:
            log.exception("brief update failed for %s", slug)

    threading.Thread(target=run, daemon=True, name="brief").start()


def refresh_summary(key: str) -> None:
    """Re-summarize a thread in the background after a turn completes."""
    if not SUMMARY_MODEL:
        return

    def run():
        try:
            summaries.summarize_one(store, key, binary=CLAUDE_BIN,
                                    model=SUMMARY_MODEL, env=claude_env())
        except Exception:
            log.exception("summary refresh failed for %s", key)

    threading.Thread(target=run, daemon=True, name="summarizer").start()


# --- Terminal handoff ---------------------------------------------------------

def kill_terminal(session_id: str) -> bool:
    """Force-close an interactive `claude --resume <session_id>` process.

    Completed turns are already persisted in the session log, so this only
    loses an in-flight generation. Returns True if a process was killed.
    """
    pids = procs.session_pids(session_id)
    if not pids:
        return False
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + 3
    while time.time() < deadline:
        if not any(_alive(p) for p in pids):
            return True
        time.sleep(0.2)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return True


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def handle_session_event(payload: dict) -> dict:
    """Route for /session-event, POSTed by the global session_hook."""
    sid = payload.get("session_id")
    event = payload.get("hook_event_name")
    if not sid or event not in ("SessionStart", "SessionEnd"):
        return {}
    key = store.find_by_session(sid)
    if not key:
        return {}
    entry = store.get(key) or {}
    if not entry.get("checked_out"):
        return {}
    channel, thread_ts = key.split(":", 1)
    try:
        if event == "SessionStart":
            already_live = entry.get("terminal_live")
            store.update(key, terminal_live=True)
            if not already_live:
                app.client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text=":desktop_computer: Terminal session opened — this thread is live in the terminal.")
        else:  # SessionEnd
            store.update(key, checked_out=False, terminal_live=False)
            app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=":leftwards_arrow_with_hook: Terminal session ended — thread reclaimed; "
                     "Slack messages run here again.")
        log.info("session event %s for %s", event, key)
    except Exception:
        log.exception("failed to handle session event for %s", key)
    return {}


# --- Files in / out ----------------------------------------------------------

def download_attachments(files: list[dict], dest_dir: Path, key: str) -> list[Path]:
    saved = []
    dest_dir.mkdir(parents=True, exist_ok=True)
    token = os.environ["SLACK_BOT_TOKEN"]
    for f in files or []:
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        name = Path(f.get("name") or f"file-{uuid.uuid4().hex[:6]}").name
        path = dest_dir / f"{int(time.time())}-{name}"
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(path, "wb") as out:
                shutil.copyfileobj(resp, out)
            saved.append(path)
            store.add_file(key, {"name": name, "path": str(path),
                                 "direction": "in", "ts": time.time()})
        except Exception:
            log.exception("failed to download attachment %s", name)
    return saved


def upload_outbox(client, outbox: Path, channel: str, thread_ts: str, key: str) -> int:
    """Upload outbox files to the thread, then archive them for the visualizer."""
    count = 0
    archive_dir = ARTIFACTS_ROOT / key.replace(":", "__")
    for path in sorted(p for p in outbox.rglob("*") if p.is_file()):
        uploaded = False
        try:
            client.files_upload_v2(channel=channel, thread_ts=thread_ts,
                                   file=str(path), title=path.name)
            uploaded = True
            count += 1
        except Exception:
            log.exception("failed to upload %s", path)
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
            dest = archive_dir / f"{int(time.time())}-{path.name}"
            shutil.move(str(path), dest)
            store.add_file(key, {"name": path.name, "path": str(dest),
                                 "direction": "out", "ts": time.time(),
                                 "uploaded": uploaded})
        except Exception:
            log.exception("failed to archive %s", path)
    shutil.rmtree(outbox, ignore_errors=True)
    return count


# --- Thread-context bootstrap -------------------------------------------------

def user_name(client, user_id: str) -> str:
    if user_id not in _users_cache:
        try:
            info = client.users_info(user=user_id)["user"]
            _users_cache[user_id] = info.get("profile", {}).get("display_name") or info.get("real_name") or user_id
        except Exception:
            _users_cache[user_id] = user_id
    return _users_cache[user_id]


def thread_context(client, channel: str, thread_ts: str, exclude_ts: str) -> str | None:
    """Transcript of an existing thread, for 'summarize this thread' mentions."""
    try:
        resp = client.conversations_replies(channel=channel, ts=thread_ts, limit=50)
    except Exception as e:
        log.warning("could not fetch thread history (%s) — missing history scope?", e)
        return None
    lines = []
    for m in resp.get("messages", []):
        if m.get("ts") == exclude_ts or m.get("subtype"):
            continue
        who = "claude (this bot)" if m.get("user") == BOT_USER_ID or m.get("bot_id") else user_name(client, m.get("user", "?"))
        text = MENTION_RE.sub("", m.get("text", "")).strip()
        if text:
            lines.append(f"{who}: {text}")
    if not lines:
        return None
    transcript = "\n".join(lines)[-6000:]
    return f"Context — earlier messages in this Slack thread:\n{transcript}\n---\n"


# --- Per-channel working dirs -------------------------------------------------

def channel_name(client, channel: str) -> str | None:
    if channel not in _channels_cache:
        try:
            _channels_cache[channel] = client.conversations_info(channel=channel)["channel"].get("name", "")
        except Exception:
            _channels_cache[channel] = ""
    return _channels_cache[channel] or None


def resolve_cwd(client, channel: str) -> Path:
    if CHANNEL_DIRS:
        mapped = CHANNEL_DIRS.get(channel)
        if not mapped:
            name = channel_name(client, channel)
            if name:
                mapped = CHANNEL_DIRS.get(name)
        if mapped:
            p = Path(mapped).expanduser()
            if p.is_dir():
                return p
            log.warning("channel dir %s does not exist; using default", p)
    return CLAUDE_CWD


# --- Commands -----------------------------------------------------------------

HELP = """*Commands* (send inside a thread):
• `!reset` / `!new` — start this thread's session over
• `!model <alias>` — switch this thread's model (`opus`, `sonnet`, `haiku`, …); `!model reset` for default
• `!stop` — kill the currently running turn in this thread
• `!learn do|avoid|note <text>` — add a learning for threads in this dir; prefix `global` for everywhere
• `!learnings` — show learnings that apply to this thread
• `!unlearn <id>` — remove a learning
• `!terminal` — check this thread out to your terminal (Slack messages held until you're done)
• `!back` — reclaim a checked-out thread for Slack
• `!takeover` — force-close the live terminal session and reclaim the thread
• `!stats` — this thread's session info (model, turns, cost)
• `!sessions` — list all active thread sessions
• `!help` — this message
Attach files to a message and Claude can read them; files Claude produces get uploaded back here.`!project <name>` — file this thread's tasks under a project
"""


def handle_command(cmd: str, key: str, say, thread_ts: str) -> bool:
    """Returns True if the message was a command (already handled)."""
    lower = cmd.lower()
    if lower in ("!help", "!commands"):
        say(text=HELP, thread_ts=thread_ts)
    elif lower.startswith("!brief"):
        arg = cmd[len("!brief"):].strip()
        slug = (store.get(key) or {}).get("project", "")
        if not slug:
            say(text="This thread isn't filed under a project — `!project <name>` first.",
                thread_ts=thread_ts)
        elif not arg:
            brief = project_store.brief_for(slug)
            say(text=(f":card_index_dividers: *{slug}* brief:\n{brief}" if brief
                      else f"No brief for *{slug}* yet. It writes itself as tasks "
                           "complete, or set one with `!brief <text>`."),
                thread_ts=thread_ts)
        elif arg.lower() in ("clear", "none"):
            project_store.set_brief(slug, "")
            say(text=f"Brief cleared for *{slug}*.", thread_ts=thread_ts)
        else:
            project_store.set_brief(slug, arg)
            say(text=f":card_index_dividers: Brief set for *{slug}*. "
                     "Tasks filed here will start with it.", thread_ts=thread_ts)
    elif lower.startswith("!project"):
        arg = cmd[len("!project"):].strip()
        entry = store.get(key) or {}
        if not arg:
            current = entry.get("project") or ""
            listing = ", ".join(f"`{p['slug']}`" for p in project_store.all(False)) or "none yet"
            say(text=(f"This thread files tasks under `{current}`." if current
                      else "This thread isn't filed under a project.")
                     + f"\nProjects: {listing}\n"
                       "`!project <name>` to file it here, `!project none` to unfile.",
                thread_ts=thread_ts)
        elif arg.lower() in ("none", "off", "clear"):
            store.update(key, project="")
            say(text="Unfiled — new tasks from this thread won't belong to a project.",
                thread_ts=thread_ts)
        elif arg.lower().startswith("ideate"):
            slug = entry.get("project") or ""
            when = arg[6:].strip()
            if not slug:
                say(text="File this thread under a project first: `!project <name>`.",
                    thread_ts=thread_ts)
            elif not when:
                cur = (project_store.get(slug) or {}).get("ideate_at") or ""
                say(text=(f"*{slug}* is reviewed nightly at *{cur}*." if cur
                          else f"*{slug}* has no nightly review. "
                               "`!project ideate 02:00` to add one."),
                    thread_ts=thread_ts)
            elif when.lower() in ("off", "none", "stop", "clear"):
                project_store.ensure(slug, ideate_at="")
                say(text=f":crescent_moon: Nightly review off for *{slug}*.",
                    thread_ts=thread_ts)
            else:
                try:
                    at = projects.parse_at(when)
                except ValueError as e:
                    say(text=f":warning: {e}", thread_ts=thread_ts)
                else:
                    project_store.ensure(slug, ideate_at=at)
                    say(text=f":crescent_moon: *{slug}* will be reviewed nightly at "
                             f"*{at}*, and anything it finds lands in "
                             "*proposed* for you to accept or dismiss.",
                        thread_ts=thread_ts)
        elif arg.lower() == "test" or arg.lower().startswith("test "):
            # How this project proves its own work, for the verifier to run.
            slug = entry.get("project") or ""
            want = arg[4:].strip()
            if not slug:
                say(text="File this thread under a project first: `!project <name>`.",
                    thread_ts=thread_ts)
            elif not want:
                cur = (project_store.get(slug) or {}).get("test_cmd") or ""
                say(text=(f"*{slug}* verifies with `{cur}`." if cur
                          else f"*{slug}* has no test command, so its work is never "
                               "verified and never auto-merged."),
                    thread_ts=thread_ts)
            elif want.lower() in ("none", "off", "clear"):
                project_store.ensure(slug, test_cmd="")
                say(text=f":grey_question: *{slug}* will no longer be verified.",
                    thread_ts=thread_ts)
            else:
                project_store.ensure(slug, test_cmd=want)
                say(text=f":test_tube: *{slug}* verifies with `{want}`. Work that "
                         "fails it goes back to be fixed rather than to you.",
                    thread_ts=thread_ts)
        elif arg.lower() == "base" or arg.lower().startswith("base "):
            # Which branch this project's tasks build on. Fresh main unless a
            # project says otherwise -- the trader's research branch is
            # eighteen commits ahead of main, and building on main there would
            # silently discard all of it.
            slug = entry.get("project") or ""
            want = arg[4:].strip()
            if not slug:
                say(text="File this thread under a project first: `!project <name>`.",
                    thread_ts=thread_ts)
            elif not want:
                cur = (project_store.scope_for(slug) or {}).get("branch") or ""
                say(text=(f"*{slug}* branches from *{cur}*." if cur
                          else f"*{slug}* branches from the default branch."),
                    thread_ts=thread_ts)
            else:
                sc = project_store.scope_for(slug)
                sc["branch"] = "" if want.lower() in ("none", "default", "clear") else want
                project_store.ensure(slug, scope=sc)
                say(text=(f":herb: *{slug}* tasks now branch from *{sc['branch']}*."
                          if sc["branch"] else
                          f":herb: *{slug}* tasks branch from the default branch again."),
                    thread_ts=thread_ts)
        elif arg.lower() == "mail" or arg.lower().startswith("mail "):
            # The Gmail label whose mail belongs here. Defaults to the project's
            # title, so this is only needed when the label is named differently.
            slug = entry.get("project") or ""
            label = arg[4:].strip()
            if not slug:
                say(text="File this thread under a project first: `!project <name>`.",
                    thread_ts=thread_ts)
            elif label:
                project_store.ensure(slug, mail_label="" if label.lower() in
                                     ("none", "off", "clear") else label)
                say(text=f":inbox_tray: Mail labelled *{project_store.label_for(slug)}* "
                         "files its bookings and confirmations here.",
                    thread_ts=thread_ts)
            else:
                say(text=f"Drawing facts from the Gmail label "
                         f"*{project_store.label_for(slug) or '(none)'}*.",
                    thread_ts=thread_ts)
        else:
            # Created on first use: needing to define a project before filing
            # anything is how a task system stops getting used.
            proj = project_store.ensure(
                arg, scope={"cwd": entry.get("cwd")} if entry.get("cwd") else {})
            # A repo-less project keeps its context in its own CLAUDE.md, which
            # only loads if the thread actually runs there.
            home = project_store.home(proj["slug"], create=True)
            store.update(key, project=proj["slug"],
                         **({"cwd": str(home)} if home else {}))
            say(text=f":card_index_dividers: Filed under *{proj['title']}* "
                     f"(`{proj['slug']}`). Tasks from this thread inherit it.",
                thread_ts=thread_ts)
    elif lower in ("!reset", "!new"):
        store.drop(key)
        say(text="Session cleared — the next message in this thread starts fresh.", thread_ts=thread_ts)
    elif lower == "!stop":
        handle = RUNNING.get(key)
        if handle:
            handle.stop()
            say(text=":octagonal_sign: Stopping…", thread_ts=thread_ts)
        else:
            say(text="Nothing is running in this thread.", thread_ts=thread_ts)
    elif lower.startswith("!model"):
        parts = cmd.split(None, 1)
        if len(parts) == 1:
            entry = store.get(key) or {}
            say(text=f"Current model: `{entry.get('model') or CLAUDE_MODEL or 'CLI default'}`. "
                     "Use `!model <alias>` or `!model reset`.", thread_ts=thread_ts)
        else:
            choice = parts[1].strip()
            if choice.lower() in ("reset", "default"):
                store.update(key, model=None)
                say(text="Model reset to default for this thread.", thread_ts=thread_ts)
            else:
                store.update(key, model=choice)
                say(text=f"This thread now uses `{choice}`.", thread_ts=thread_ts)
    elif lower.startswith("!learn") and not lower.startswith("!learnings"):
        entry = store.get(key) or {}
        cwd = entry.get("cwd") or str(CLAUDE_CWD)
        rest = cmd[len("!learn"):].strip()
        scope = cwd
        if rest.lower().startswith("global"):
            rest = rest[len("global"):].strip()
            scope = ""
        parts = rest.split(None, 1)
        ltype = parts[0].lower() if parts else ""
        body = parts[1].strip() if len(parts) > 1 else ""
        if ltype not in LEARNING_TYPES or not body:
            say(text="Usage: `!learn do|avoid|note <text>` (add `global` before the type "
                     "to apply everywhere). Example: `!learn avoid force-push to main`.",
                thread_ts=thread_ts)
        else:
            rec = learnings.add(ltype, body, scope, source=key)
            where = "everywhere" if not scope else f"threads under `{scope}`"
            say(text=f":brain: Learned ({ltype}, {where}): {body}\n_`{rec['id']}` — remove with "
                     f"`!unlearn {rec['id']}`. Applies to new turns in matching threads._",
                thread_ts=thread_ts)
    elif lower == "!learnings":
        entry = store.get(key) or {}
        cwd = entry.get("cwd") or str(CLAUDE_CWD)
        applic = learnings.applicable(cwd)
        if not applic:
            say(text="No learnings apply to this thread yet. Add one with "
                     "`!learn do|avoid|note <text>`.", thread_ts=thread_ts)
        else:
            lines = []
            for x in applic:
                tag = "🌍" if not x["scope"] else "📁"
                lines.append(f"{tag} *{x['type']}* — {x['text']}  `{x['id']}`")
            say(text=f"*Learnings for this thread ({len(applic)}):*\n" + "\n".join(lines),
                thread_ts=thread_ts)
    elif lower.startswith("!unlearn"):
        parts = cmd.split(None, 1)
        if len(parts) < 2:
            say(text="Usage: `!unlearn <id>` (get the id from `!learnings`).", thread_ts=thread_ts)
        elif learnings.delete(parts[1].strip()):
            say(text=":wastebasket: Removed.", thread_ts=thread_ts)
        else:
            say(text="No learning with that id.", thread_ts=thread_ts)
    elif lower in ("!terminal", "!handoff"):
        entry = store.get(key)
        if not entry or not entry.get("session_id"):
            say(text="No session in this thread yet — send a prompt first.", thread_ts=thread_ts)
        else:
            store.update(key, checked_out=True, terminal_live=False)
            cwd = entry.get("cwd", str(CLAUDE_CWD))
            say(text=(f":outbox_tray: Thread checked out to the terminal. Continue it with:\n"
                      f"```cd {cwd} && claude --resume {entry['session_id']}```\n"
                      "_Slack messages are held while it's checked out. When you exit the "
                      "terminal session the thread reclaims itself; `!back` reclaims without "
                      "the terminal, `!takeover` force-closes a live terminal session._"),
                thread_ts=thread_ts)
    elif lower == "!back":
        entry = store.get(key) or {}
        if not entry.get("checked_out"):
            say(text="This thread isn't checked out.", thread_ts=thread_ts)
        else:
            store.update(key, checked_out=False, terminal_live=False)
            say(text=":leftwards_arrow_with_hook: Thread reclaimed — Slack messages run here again.",
                thread_ts=thread_ts)
    elif lower == "!takeover":
        entry = store.get(key) or {}
        if not entry.get("checked_out"):
            say(text="This thread isn't checked out — nothing to take over.", thread_ts=thread_ts)
        else:
            killed = kill_terminal(entry.get("session_id", ""))
            store.update(key, checked_out=False, terminal_live=False)
            note = "closed the live terminal session and " if killed else "no terminal process found; "
            say(text=f":leftwards_arrow_with_hook: Took over — {note}the thread is back on Slack.",
                thread_ts=thread_ts)
    elif lower == "!stats":
        entry = store.get(key)
        if not entry:
            say(text="No session yet in this thread.", thread_ts=thread_ts)
        else:
            say(text=(f"*Session* `{entry.get('session_id', '?')[:8]}…`\n"
                      f"model: `{entry.get('model') or CLAUDE_MODEL or 'default'}` · "
                      f"cwd: `{entry.get('cwd', CLAUDE_CWD)}`\n"
                      f"turns: {entry.get('turns', 0)} · total cost: ${entry.get('cost', 0):.4f} · "
                      f"last used {fmt_age(entry.get('updated', time.time()))}"),
                thread_ts=thread_ts)
    elif lower == "!sessions":
        entries = store.all()
        if not entries:
            say(text="No active sessions.", thread_ts=thread_ts)
        else:
            lines = []
            for k, v in sorted(entries.items(), key=lambda kv: -kv[1].get("updated", 0))[:20]:
                ch, ts = k.split(":", 1)
                name = v.get("title") or "thread"
                link = f"<{slacklinks.thread_link(ch, ts)}|{name}>"
                lines.append(f"• {link} — {v.get('turns', 0)} turns, ${v.get('cost', 0):.2f}, {fmt_age(v.get('updated', 0))}")
            say(text=f"*Active sessions ({len(entries)}):*\n" + "\n".join(lines), thread_ts=thread_ts)
    else:
        return False
    return True


# --- Main handler ---------------------------------------------------------------

app = App(token=os.environ["SLACK_BOT_TOKEN"])
_AUTH = app.client.auth_test()
BOT_USER_ID = _AUTH["user_id"]
# The workspace URL, for linking a task to its thread from the Home tab.
TEAM_URL = _AUTH.get("url", "")

def handle_status(payload: dict) -> dict:
    """Route for /status — thread runtime state for the visualizer."""
    threads = {}
    for key, entry in store.all().items():
        threads[key] = {
            "running": key in RUNNING,
            "checked_out": bool(entry.get("checked_out")),
            "terminal_live": bool(entry.get("terminal_live")),
        }
    # "online" only ever meant "this HTTP server answered", which stayed true
    # through a seventeen-hour Slack outage. Report the link separately.
    # Reports what *this process* resolved, not what .env says now. Editing
    # .env without restarting is the normal case, and a check that reads the
    # file would call that fixed while the running bot still had the old value.
    auth = credentials.state(has_token=bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")))
    auth.pop("expires_at", None)
    # Landed is not running: nothing restarts the bot when it merges onto its
    # own main, so the checkout moves on without the process. Reported as what
    # booted versus what is on disk, rather than one "version", because the
    # useful fact is the gap between them.
    # The Slack link is not the only thing that can stop while the process
    # stays up: every background loop ends for good on one escaped exception,
    # and a dead thread logs nothing further. Say which ones are gone.
    # A held runner looks exactly like an idle one from the board -- queued
    # work, not moving -- so say that it is holding, and why.
    return {"online": True, "threads": threads,
            "slack": slack.status(time.time()), "auth": auth,
            "revision": revision.state(BASE_DIR, REVISION),
            "daemons": daemons.status(),
            "runner_hold": {"seconds": round(RUNNER_HOLD.remaining()),
                            "reason": RUNNER_HOLD.reason()[:160]}}


def handle_web_message(payload: dict) -> dict:
    """Route for /web-message — a prompt or !command sent from the visualizer.

    Localhost-only by construction (the LocalServer binds 127.0.0.1), so it is
    trusted like the machine owner: it bypasses the Slack allowlist.
    """
    key = payload.get("key", "")
    text = (payload.get("text") or "").strip()
    # Key must look like channel:ts. Store membership isn't required — !reset
    # drops the entry, but the thread remains valid and messages recreate it.
    if not text or ":" not in key or not re.fullmatch(r"[A-Z0-9]+:[0-9.]+", key):
        return {"ok": False, "error": "unknown thread or empty message"}
    channel, thread_ts = key.split(":", 1)

    def say(text, thread_ts=thread_ts, **kwargs):
        app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text, **kwargs)

    def run():
        try:
            if not text.startswith("!"):
                say(f":globe_with_meridians: _via visualizer:_ {text}")
            event = {"channel": channel, "ts": f"{time.time():.6f}",
                     "thread_ts": thread_ts, "user": "", "text": text, "_web": True}
            handle_prompt(event, say, app.client)
        except Exception:
            log.exception("web message failed for %s", key)

    threading.Thread(target=run, daemon=True, name="web-message").start()
    return {"ok": True}


def handle_register_terminal(payload: dict) -> dict:
    """Route for /register-terminal — bin/silkworm starting a tracked session.

    Posts an anchor message in Slack so the session has a thread from birth;
    the checkout/handoff machinery then treats it like any handed-off thread.
    """
    sid = payload.get("session_id", "").strip()
    if not sid:
        return {"ok": False, "error": "missing session_id"}
    cwd = payload.get("cwd") or str(CLAUDE_CWD)
    title = payload.get("title") or Path(cwd).name

    channel = SILKWORM_HOME_CHANNEL
    if not channel:  # fall back to the bot's most recently used DM
        dms = [k for k, v in sorted(store.all().items(),
                                    key=lambda kv: -kv[1].get("updated", 0))
               if k.startswith("D")]
        channel = dms[0].split(":", 1)[0] if dms else ""
    if not channel:
        return {"ok": False, "error": "set SILKWORM_HOME_CHANNEL in .env (no DM history to fall back to)"}

    try:
        resp = app.client.chat_postMessage(
            channel=channel,
            text=(f":thread: *{title}* — terminal session started via the silkworm CLI "
                  f"in `{cwd}`.\n_This thread follows it: when the terminal exits, "
                  "reply here to continue the session from Slack._"))
    except Exception as e:
        log.exception("failed to post terminal anchor")
        return {"ok": False, "error": f"could not post to {channel}: {e}"}

    ts = resp["ts"]
    key = f"{channel}:{ts}"
    store.update(key, session_id=sid, cwd=cwd, checked_out=True,
                 terminal_live=bool(payload.get("live")), title=title[:60])
    ACTIVE_SESSIONS[sid] = (channel, ts)
    log.info("registered terminal session %s -> %s", sid[:8], key)
    return {"ok": True, "key": key,
            "link": slacklinks.thread_link(channel, ts)}


HARVEST_STATE = BASE_DIR / "harvest_state.json"
# Where the task board is posted, by name or id; see home.Board.
BOARD_CHANNEL = os.environ.get("SILKWORM_BOARD_CHANNEL", "silkworm-board")
BOARD_STATE = BASE_DIR / "board.json"
BOARD_POLL_S = 60
_harvest_lock = threading.Lock()


def run_harvest() -> dict:
    with _harvest_lock:
        result = harvester.harvest(store, learnings, binary=CLAUDE_BIN,
                                   model=HARVEST_MODEL, env=claude_env(),
                                   state_path=HARVEST_STATE)
    if result.get("added") and LEARNINGS_AUTOSYNC and learnings_git.is_git_backed(LEARNINGS_FILE):
        try:
            learnings_git.sync(LEARNINGS_FILE)
        except Exception:
            log.exception("auto-sync after harvest failed")
    return result


def handle_learnings(payload: dict) -> dict:
    """Route for /learnings — CRUD + harvest from the visualizer (localhost-trusted)."""
    action = payload.get("action")
    if action == "add":
        try:
            rec = learnings.add(payload.get("type", ""), payload.get("text", ""),
                                payload.get("scope", ""), source="web")
            return {"ok": True, "learning": rec}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
    if action == "delete":
        return {"ok": learnings.delete(payload.get("id", ""))}
    if action == "toggle":
        return {"ok": learnings.set_enabled(payload.get("id", ""), bool(payload.get("enabled")))}
    if action == "harvest":
        try:
            return {"ok": True, **run_harvest()}
        except Exception as e:
            log.exception("manual harvest failed")
            return {"ok": False, "error": str(e)}
    if action == "sync":
        try:
            return learnings_git.sync(LEARNINGS_FILE)
        except Exception as e:
            log.exception("learnings sync failed")
            return {"ok": False, "error": str(e)}
    # list (optionally filtered to a thread's cwd)
    cwd = payload.get("cwd")
    return {"ok": True, "learnings": learnings.applicable(cwd) if cwd else learnings.all()}


def handle_titles(payload: dict) -> dict:
    """Route for /titles — name threads from their summaries (localhost-trusted)."""
    key, manual = payload.get("key"), (payload.get("title") or "").strip()
    if key and manual:                      # user typed one; no model needed
        store.update(key, title=manual[:60])
        return {"ok": True, "title": manual[:60]}
    if not NAMING_MODEL:
        return {"ok": False, "error": "NAMING_MODEL is empty (naming disabled)"}
    try:
        if key:
            title = summaries.title_one(store, key, binary=CLAUDE_BIN,
                                        model=NAMING_MODEL, env=claude_env(), force=True)
            return {"ok": bool(title), "title": title,
                    "error": "" if title else "no summary to name it from"}
        return {"ok": True, **summaries.backfill_titles(
            store, binary=CLAUDE_BIN, model=NAMING_MODEL, env=claude_env(),
            force=bool(payload.get("force")))}
    except Exception as e:
        log.exception("titling failed")
        return {"ok": False, "error": str(e)}


def approve_task(payload: dict) -> dict:
    """Approve flagged work — and land it, where a passing review would have.

    Approving is a person reading the reviewer's findings and deciding the work
    is fine, which is at least as strong as a review that found nothing to say.
    Mapping it to a bare transition made the one route through flagged work
    also the only route that never tried to merge: tsk_5fb51199eb reached
    `done` carrying four reviewed, test-passing commits that main did not have.

    Only from `awaiting_approval`. The same button is legal from `blocked`,
    where the task is still waiting on its review, and approving there must not
    merge work nobody has read yet.

    The task ends `done` whether or not the landing succeeds — approving is the
    user closing the item, and refusing to close it would only put the same
    task in front of them again. What happened to the branch is written to the
    record instead, which is what the board reads.
    """
    tid = payload.get("id", "")
    task = task_store.get(tid)
    if not task:
        return {"ok": False, "error": "unknown task"}
    reviewed = task.get("state") == tasks.AWAITING_APPROVAL
    # Approving is one of the two moments a held checkout becomes unreachable;
    # see the note on dismiss in handle_tasks, which this mirrors.
    held = holding.for_task(task)
    note = holding.short(held) if held else ""
    try:
        done = task_store.transition(
            tid, tasks.DONE, f"approve via {payload.get('by', 'ui')}"
            + (f" ({note})" if note else ""))
    except tasks.InvalidTransition as e:
        return {"ok": False, "error": f"not allowed: {e}"}
    if held:
        tell_thread(task.get("thread", ""), holding.note(held))
    if reviewed:
        # The task is already `done`; a failure to even begin the landing must
        # not be reported as a refused approval. It used to propagate, and the
        # dashboard renders any raised error as "Not allowed" -- so the user
        # re-clicked, the state was no longer awaiting_approval, and no landing
        # was ever attempted. That is the original silence, through new code.
        try:
            start_landing(tid)
        except Exception:
            log.exception("could not start the landing for %s", tid)
    return {"ok": True, "note": note, "task": task_store.get(tid) or done}


def handle_tasks(payload: dict) -> dict:
    """Route for /tasks — read and triage tasks (localhost-trusted)."""
    action = payload.get("action", "list")
    project = payload.get("project") or ""
    def _filter(items):
        return [t for t in items if not project or (t.get("project") or "") == project]
    if action == "list":
        state = payload.get("state")
        items = (task_store.by_state(state) if state
                 else sorted(task_store.all().values(),
                             key=lambda t: -(t.get("created") or 0)))
        return {"ok": True, "tasks": _filter(items)[:200], "counts": task_store.counts()}
    if action == "attention":
        return {"ok": True, "tasks": _filter(task_store.needs_attention()),
                "counts": task_store.counts()}
    if action == "watching":
        # Scheduled wake-ups specifically, not everything parked in `blocked`:
        # a quota-blocked retry is the system waiting on itself, while a watch
        # is a promise made to you, and only one of those is worth checking on.
        now = time.time()
        out = []
        for tid, r in task_store.all().items():
            if r.get("state") != tasks.BLOCKED or r.get("source") != "defer":
                continue
            out.append({"id": tid, "goal": r.get("goal", ""),
                        "thread": r.get("thread", ""),
                        "in_s": round((r.get("retry_at") or now) - now),
                        "depth": r.get("defers") or 0,
                        "remaining": defer.MAX_DEFERS - (r.get("defers") or 0)})
        return {"ok": True, "watching": sorted(out, key=lambda w: w["in_s"])}
    if action == "unmerged":
        # Finished work whose commits never reached the base. Its own action
        # rather than part of `list`: the badge polls `list` every five
        # seconds, and this asks git, so folding it in would run a survey
        # twice a minute to answer a question that only changes when you
        # merge something.
        rows = branches.survey(_filter(list(task_store.all().values())))
        return {"ok": True, "unmerged": rows, "summary": branches.line(rows)}
    if action == "roles":
        # What the form may offer, fetched rather than written into the page,
        # so the choices and the rule that enforces them cannot drift apart.
        return {"ok": True, "default": roles.DEFAULT_FILED,
                "roles": [{"name": n, "hint": h, "default": n == roles.DEFAULT_FILED}
                          for n, h in roles.FILEABLE.items()]}
    if action == "holding":
        # Checkouts still holding uncommitted work. Its own action for the same
        # reason as `unmerged`: this walks every worktree and asks git about
        # each, and folding it into `list` would do that twice a minute.
        # Every record, not the filtered ones: attribution is what makes a row
        # readable, and a task filtered out of the view would turn its checkout
        # into an unattributable one rather than hiding it. The filter is
        # applied to the rows afterwards instead.
        rows = holding.survey(list(task_store.all().values()))
        if project:
            rows = [r for r in rows if r["project"] == project]
        return {"ok": True, "holding": rows, "summary": holding.line(rows)}
    if action == "ingest-email":
        try:
            return run_email_ingest()
        except Exception as e:
            log.exception("manual email ingest failed")
            return {"ok": False, "error": str(e)}
    if action == "create":
        try:
            # The same default and the same choices as filing from a
            # conversation (handle_file_task, via roles.FILEABLE). This route
            # used to default to `assistant`, whose review flag is False, so
            # the same sentence typed into the dashboard got no reviewer, no
            # verification and nothing that could ever land -- on a branch, in
            # its own worktree, with full permissions, and nothing saying so.
            #
            # Checked before the project is ensured, so a refused filing leaves
            # nothing behind.
            role = (payload.get("role") or roles.DEFAULT_FILED).strip()
            err = roles.validate_filed(role)
            if err:
                return {"ok": False, "error": err}
            # `make` checks the state is a real one; it cannot know that only
            # two of them are somewhere a *filing* may start. Queued means the
            # runner will take it, proposed means it waits for you to accept
            # it. Filed as `running`, it would be running with nothing running
            # it, and nothing would ever pick it up.
            state = payload.get("state") or tasks.QUEUED
            if state not in (tasks.QUEUED, tasks.PROPOSED):
                return {"ok": False, "error": f"cannot file a task as {state!r}"}
            proj = (payload.get("project") or "").strip()
            if proj:
                proj = project_store.ensure(proj)["slug"]
                # Give a repo-less project a home; running there is what makes
                # its CLAUDE.md load, with no injection on our part.
                project_store.home(proj, create=True)
            # A project's default scope saves repeating the directory on
            # every task filed under it.
            scope = payload.get("scope") or project_store.scope_for(proj) \
                or {"cwd": str(CLAUDE_CWD)}
            t = task_store.create(payload.get("goal", ""),
                                  role=role,
                                  project=proj,
                                  source=payload.get("source", "ui"),
                                  state=state,
                                  # Nobody is holding a live message for these,
                                  # so the runner is what will execute them.
                                  # An unrecognised one is refused by `make`,
                                  # which is what this route's ValueError
                                  # handler turns into an answer.
                                  driver=payload.get("driver") or "queue",
                                  # Filed work stands on its own, so it runs in
                                  # its own checkout rather than the tree you
                                  # are editing.
                                  isolate=True,
                                  thread=payload.get("thread", ""),
                                  scope=scope)
            return {"ok": True, "task": t}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
    if action == "rework":
        # Send flagged work back with the reviewer's findings attached, so the
        # rerun addresses them rather than repeating the same thing.
        tid = payload.get("id", "")
        task = task_store.get(tid)
        if not task:
            return {"ok": False, "error": "unknown task"}
        review = (task.get("result") or {}).get("review") or {}
        findings = review.get("findings") or []
        notes = (payload.get("notes") or "").strip()
        parts = []
        if findings:
            parts.append("A review flagged this. Address the findings, then say "
                         "what changed.")
            parts.append("\n".join(f"- {f}" for f in findings))
        if notes:
            # The user's own words carry more weight than the reviewer's, so
            # they go last and are labelled as coming from a person.
            parts.append(f"From {payload.get('by', 'the user')}:\n{notes}")
        if not parts:
            parts.append("Sent back for another pass.")
        addendum = "\n\n".join(parts)
        try:
            # Clear the previous cycle's review and verdict. Both the review
            # gate and verification are guarded by `not blocked_on`, so leaving
            # the old reviewer's id there made a reworked task skip both and go
            # straight to done -- unproven and unreviewed, which is the exact
            # opposite of what sending it back is for.
            task_store.update(tid, goal=f"{task.get('goal', '')}\n\n{addendum}",
                              driver="queue", blocked_on=[], verified=None)
            return {"ok": True, "task": task_store.transition(
                tid, tasks.QUEUED, "sent back for rework")}
        except tasks.InvalidTransition as e:
            return {"ok": False, "error": f"not allowed: {e}"}
    if action == "approve":
        return approve_task(payload)
    if action in ("accept", "dismiss", "retry", "cancel"):
        target = {"accept": tasks.QUEUED, "retry": tasks.QUEUED,
                  "dismiss": tasks.CANCELLED, "cancel": tasks.CANCELLED}[action]
        tid = payload.get("id", "")
        # Cancelling a running task has to stop it, not just relabel it. It
        # used to do only the latter: the record said cancelled while the agent
        # carried on working and spending, in an isolated checkout that -- no
        # longer being owned by anything running -- the sweeper was then free
        # to delete underneath it. That happened, and took a task's first pass
        # of uncommitted work with it.
        note = ""
        rec = task_store.get(tid) or {}
        current = rec.get("state")
        if target == tasks.CANCELLED and current == tasks.RUNNING:
            note = ("stopped" if stop_task(tid) else
                    "no live child here — it was orphaned by a restart")
        # Dismissing (and approving, in approve_task) is the moment a task stops
        # being anybody's business, and if its isolated checkout still holds uncommitted files
        # it is also the moment that work becomes unreachable -- nothing after
        # this will ever mention it again.
        #
        # So the checkout is handed back rather than reclaimed: named, with
        # what is in it, in the reply and durably in the task's own thread, and
        # left untouched on disk. Reclaiming it automatically was considered
        # and rejected on the evidence. Deleting is out by the standing rule.
        # The only non-destructive automatic reclaim is to commit the leftovers
        # onto the task's branch and release the tree -- and of the twenty-one
        # held checkouts this was written against, twenty were held by an
        # untracked .venv and a data directory. Committing those to the
        # project's own branch is a worse outcome than the problem. Whether
        # what is in there is worth keeping is a judgement about the files, so
        # it goes to the person who can make it, at the moment they are already
        # looking.
        held = holding.for_task(rec) if action == "dismiss" else None
        if held:
            note = f"{note}; {holding.short(held)}" if note else holding.short(held)
        try:
            moved = task_store.transition(
                tid, target, f"{action} via {payload.get('by', 'ui')}"
                + (f" ({note})" if note else ""))
            if held:
                tell_thread(rec.get("thread", ""), holding.note(held))
            return {"ok": True, "note": note, "task": moved}
        except tasks.InvalidTransition as e:
            return {"ok": False, "error": f"not allowed: {e}"}
        except KeyError:
            return {"ok": False, "error": "unknown task"}
    return {"ok": False, "error": f"unknown action {action!r}"}


def handle_projects(payload: dict) -> dict:
    """Route for /projects — list, create and archive (localhost-trusted)."""
    action = payload.get("action", "list")
    if action == "list":
        return {"ok": True,
                # So the nightly panel can show a project as paused rather
                # than merely quiet: the limit lives in one place and the page
                # compares against it instead of hard-coding a number.
                "proposal_limit": scoping.max_open_proposals(),
                "projects": projects.summarise(
                    project_store.all(include_archived=bool(payload.get("archived"))),
                    list(task_store.all().values()), tasks.NEEDS_ATTENTION)}
    if action == "ensure":
        name = (payload.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "a project needs a name"}
        scope = payload.get("scope")
        return {"ok": True, "project": project_store.ensure(
            name, **({"scope": scope} if scope else {}))}
    if action == "test-cmd":
        # How a project proves its own work. Set here as well as from Slack so
        # the dashboard can configure it; empty means unverified, which blocks
        # auto-merge rather than being treated as a pass.
        slug = (payload.get("slug") or "").strip()
        if not project_store.get(slug):
            return {"ok": False, "error": f"unknown project {slug!r}"}
        cmd = (payload.get("cmd") or "").strip()
        if cmd.lower() in ("off", "none", "clear"):
            cmd = ""
        return {"ok": True, "project": project_store.ensure(slug, test_cmd=cmd)}
    if action == "auto-merge":
        slug = (payload.get("slug") or "").strip()
        rec = project_store.get(slug)
        if not rec:
            return {"ok": False, "error": f"unknown project {slug!r}"}
        on = bool(payload.get("on"))
        if on and not (rec.get("test_cmd") or "").strip():
            return {"ok": False,
                    "error": "set a test command first — nothing may land unproven"}
        return {"ok": True, "project": project_store.ensure(slug, auto_merge=on)}
    if action == "publish":
        # Whether a landing is pushed. Separate from auto-merge on purpose:
        # landing is a decision about this checkout, publishing is one about
        # the remote, and a project can reasonably want the first without the
        # second. Refused without auto-merge, which is the only thing that
        # lands anything for this to publish.
        slug = (payload.get("slug") or "").strip()
        rec = project_store.get(slug)
        if not rec:
            return {"ok": False, "error": f"unknown project {slug!r}"}
        on = bool(payload.get("on"))
        if on and not rec.get("auto_merge"):
            return {"ok": False,
                    "error": "turn auto-merge on first — there is nothing to publish"}
        return {"ok": True, "project": project_store.ensure(slug, publish=on)}
    if action == "ideate":
        # Nightly review, set from the dashboard rather than only from Slack.
        # Validated here rather than in the page: "2am" should work, and a
        # typo should be refused with a reason rather than silently stored.
        slug = (payload.get("slug") or "").strip()
        if not project_store.get(slug):
            return {"ok": False, "error": f"unknown project {slug!r}"}
        want = (payload.get("at") or "").strip()
        if want.lower() in ("", "off", "none", "clear"):
            return {"ok": True, "project": project_store.ensure(slug, ideate_at="")}
        try:
            at = projects.parse_at(want)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "project": project_store.ensure(slug, ideate_at=at)}
    if action == "brief":
        slug = payload.get("slug", "")
        if "brief" in payload:
            rec = project_store.set_brief(slug, payload.get("brief") or "")
            return {"ok": bool(rec), "project": rec,
                    "error": "" if rec else "unknown project"}
        return {"ok": True, "brief": project_store.brief_for(slug)}
    if action in ("archive", "unarchive"):
        ok = project_store.set_archived(payload.get("slug", ""), action == "archive")
        return {"ok": ok, "error": "" if ok else "unknown project"}
    return {"ok": False, "error": f"unknown action {action!r}"}


def handle_release(payload: dict) -> dict:
    """Route for /release — force-free a wedged thread (localhost-trusted).

    Automates the cleanup a stuck turn needs: kill the child (whether or not
    this process still owns it), clear the pending marker, finalize the frozen
    placeholder, and drop the stale hourglass. !stop can't do this, because a
    turn orphaned by a restart isn't in RUNNING any more.
    """
    key = payload.get("key", "")
    entry = store.get(key)
    if not entry:
        return {"ok": False, "error": "unknown thread"}

    handle = RUNNING.get(key)
    if handle:
        try:
            handle.stop()
        except Exception:
            log.exception("stopping owned turn failed for %s", key)

    pending = entry.get("pending") or {}
    sid = pending.get("session_id") or entry.get("session_id") or ""
    pids = procs.session_pids(sid)
    for pid in pids:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            pass
    if pids:
        time.sleep(4)
        for pid in pids:
            if _alive(pid):
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except OSError:
                    pass

    channel, _, thread_ts = key.partition(":")
    note = (":warning: _Turn released — it was killed after getting stuck. "
            "The thread's history is intact; send your message again to continue._")
    if pending.get("progress_ts"):
        try:
            app.client.chat_update(channel=channel, ts=pending["progress_ts"], text=note)
        except Exception:
            log.exception("finalizing placeholder failed for %s", key)
    else:
        try:
            app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=note)
        except Exception:
            log.exception("posting release note failed for %s", key)
    try:
        ThreadReactions(app.client, channel, thread_ts, pending.get("msg_ts")).failed()
    except Exception:
        log.exception("clearing reactions failed for %s", key)

    recovery.clear_pending(store, key)
    RUNNING.pop(key, None)
    store.add_event(key, "released", f"stuck turn killed ({len(pids)} process(es))")
    log.warning("released thread %s (killed %d process(es))", key, len(pids))
    return {"ok": True, "killed": len(pids)}


def handle_summaries(payload: dict) -> dict:
    """Route for /summaries — regenerate thread summaries (localhost-trusted)."""
    if not SUMMARY_MODEL:
        return {"ok": False, "error": "SUMMARY_MODEL is empty (summaries disabled)"}
    force = bool(payload.get("force"))
    key = payload.get("key")
    try:
        if key:
            ok = summaries.summarize_one(store, key, binary=CLAUDE_BIN,
                                         model=SUMMARY_MODEL, env=claude_env(),
                                         force=True)
            return {"ok": ok, "summary": (store.get(key) or {}).get("summary", "")}
        return {"ok": True, **summaries.backfill(store, binary=CLAUDE_BIN,
                                                 model=SUMMARY_MODEL,
                                                 env=claude_env(), force=force)}
    except Exception as e:
        log.exception("summarize failed")
        return {"ok": False, "error": str(e)}


def handle_defer(payload: dict) -> dict:
    """Route for /defer — schedule a later turn on a thread.

    Deliberately a plain `blocked` task with a `retry_at`: the queue runner
    already requeues those when their time comes, so a wake-up costs nothing
    while it waits and survives a restart because it is a durable record
    rather than a sleeping process.
    """
    key = (payload.get("key") or "").strip()
    goal = (payload.get("goal") or "").strip()
    if not re.fullmatch(r"[A-Z0-9]+:[0-9.]+", key):
        return {"ok": False, "error": "no thread to schedule against"}
    if not goal:
        return {"ok": False, "error": "say what to check when it wakes"}
    try:
        delay = defer.parse_delay(payload.get("delay", ""))
        depth = defer.next_depth(payload.get("depth"))
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    entry = store.get(key) or {}
    when = time.time() + delay
    task = task_store.create(
        goal, title=goal[:70], state=tasks.BLOCKED, driver="queue",
        source="defer", thread=key, project=entry.get("project") or "",
        # The runner executes it, but it is the same conversation resuming the
        # same session: it belongs in the thread's checkout, not a worktree.
        isolate=False,
        scope={"cwd": entry.get("cwd") or str(CLAUDE_CWD)},
        retry_at=when, defers=depth,
    )
    log.info("scheduled wake-up %s on %s in %.0fs (depth %d)",
             task["id"], key, delay, depth)
    return {"ok": True, "id": task["id"], "at": when, "in_s": round(delay),
            "depth": depth, "remaining": defer.MAX_DEFERS - depth}


#: Filed per turn, so a plan that loses count cannot become fifty sessions.
_filed_this_turn: dict = {}


def filed_by_restricted_role(caller_role: str, key: str) -> bool:
    """Whether the run doing the filing is one that may not write.

    Not "unattended": an implementor runs unattended too, and files onto the
    queue, because it already has full autonomy directly. The question here is
    only whether this run is on an allowlist.

    Two independent reads, because either alone is a single point of failure.
    The role the bot put in the run's own environment is the direct answer,
    but it reaches this route back through the CLI; the role of whatever task
    is running on the thread is the bot's own record, which nothing outside
    the process touches. Either saying "restricted" is enough.

    An unrecognised name counts as restricted too: roles.get() reads a name it
    does not know as `assistant`, so a typo would otherwise be a way of buying
    full autonomy. No name at all is a person at a terminal, who files
    normally — being the person the proposed gate exists to ask.
    """
    if caller_role and (caller_role not in roles.ROLES
                        or roles.is_restricted(caller_role)):
        return True
    return any(roles.is_restricted(t.get("role") or "")
               for t in task_store.by_state(tasks.RUNNING)
               if t.get("thread") == key)


def begin_turn(key: str) -> None:
    """Give the turn about to run on this thread a fresh filing budget.

    Every path that starts a turn has to call this, not just the live Slack
    one. A task keeps the thread key it was first given for the rest of its
    life, so when the reset only happened on the Slack path, any task run a
    second time on the same key -- retried after a quota error, requeued after
    a restart, reworked, sent back -- began with a budget the previous run had
    already spent, and was refused without having filed anything. An ideator
    hit that way files nothing at all, which is its entire purpose. (Each
    night's pass is a new task on a new key, so it is a rerun that suffers,
    not the following night.)

    Called under the thread's lock, which is what makes it safe: several tasks
    can share one key -- a reviewer child, a wake-up, orphans handed back to
    the runner -- and resetting outside the lock would wipe the budget of a
    turn already running on it.

    A retry therefore gets a whole fresh budget rather than the remains of the
    attempt that failed. That is the intent: a retried pass re-derives its work
    from scratch, so what the last one spent says nothing about this one.
    """
    _filed_this_turn.pop(key, None)


def handle_file_task(payload: dict) -> dict:
    """Route for /file-task — a turn filing work it just scoped with the user.

    Separate from the dashboard's create: this one resolves the project and
    scope from the thread it was called in, so a conversation already bound to
    a project does not have to restate where its work belongs.
    """
    key = (payload.get("key") or "").strip()
    goal = (payload.get("goal") or "").strip()
    if not re.fullmatch(r"[A-Z0-9]+:[0-9.]+", key):
        return {"ok": False, "error": "no thread to file against"}

    # A proposal is not work yet. Nothing ran it past you, so it waits in
    # `proposed` until you accept it -- which is the whole point of a job that
    # thinks about your projects while you are asleep. It also files against
    # the smaller budget, because each one costs you a decision.
    #
    # Which of the two it is comes from the role that is filing, not only from
    # the flag it passed, because a flag is something a caller can leave off.
    # The ideator's allowlist is `Bash({bin} task:*)` -- a prefix, so it
    # permits an invocation with no --propose at all, and that filed straight
    # to `queued` as an implementor for the runner to execute overnight with
    # full write permissions. Read-only stops the ideator editing the tree; it
    # does not stop it commissioning an agent that will. Acceptance is a
    # person, every time, so a restricted role's work waits whatever it asked.
    restricted = filed_by_restricted_role(
        (payload.get("caller_role") or "").strip(), key)
    propose = bool(payload.get("propose")) or restricted
    state = tasks.PROPOSED if propose else tasks.QUEUED

    # Named, not resolved. `ensure` creates the project's record and its home
    # directory on disk, which has to stay below the checks so a refused
    # filing leaves nothing behind; `slugify` is the same key `by_project`
    # stores under, arrived at without writing anything.
    entry = store.get(key) or {}
    proj = (payload.get("project") or entry.get("project") or "").strip()
    slug = projects.slugify(proj) if proj else ""

    # Proposals are also bounded across passes, not just within one: the
    # per-turn count starts at zero every run, so without this a nightly pass
    # keeps adding to a pile nobody has got to. Counted live from the board so
    # a pass that began while there was room still stops when the room runs
    # out. Only for a named project: `by_project("")` is every task filed
    # under no project at all, which is where mail triage puts its proposals,
    # and pooling those would refuse an unrelated filing over a count no
    # project panel can show. The nightly pass always names its project.
    open_now = (scoping.open_proposals(task_store.by_project(slug))
                if propose and slug else 0)
    err = scoping.validate(goal, _filed_this_turn.get(key, 0), propose=propose,
                           open_now=open_now, slug=slug)
    if err:
        return {"ok": False, "error": err}

    role = (payload.get("role") or roles.DEFAULT_FILED).strip()
    err = roles.validate_filed(role)
    if err:
        return {"ok": False, "error": err}

    # Created only once the filing is going to happen. Naming a project makes
    # its record and its home directory on disk, and doing that above the
    # checks meant a refused filing still left one behind -- a write, done on
    # behalf of a role whose whole point is that it cannot write.
    if proj:
        proj = project_store.ensure(proj)["slug"]
        project_store.home(proj, create=True)

    scope = project_store.scope_for(proj) or {"cwd": entry.get("cwd") or str(CLAUDE_CWD)}
    task = task_store.create(
        goal, title=goal[:70], role=role, project=proj,
        # Work scoped with you is queued: it was already reviewed by the person
        # the proposed gate exists to ask. A proposal nobody has seen is not.
        state=state, driver="queue",
        source="ideation" if propose else "scoped",
        # Filed work, not the conversation that scoped it: whoever runs it
        # starts fresh, so it gets its own checkout.
        isolate=True,
        scope=scope,
    )
    _filed_this_turn[key] = _filed_this_turn.get(key, 0) + 1
    log.info("filed task %s from %s (project=%s role=%s state=%s%s)",
             task["id"], key, proj or "-", role, state,
             " restricted-caller" if restricted else "")
    return {"ok": True, "id": task["id"], "project": proj, "state": state,
            "role": role, "cwd": scope.get("cwd", ""),
            "remaining": scoping.limit_for(propose) - _filed_this_turn[key]}


def handle_hide(payload: dict) -> dict:
    """Route for /hide — take a thread out of the dashboard's default list.

    Hidden, never deleted: dropping a record destroys its title, summary, cost
    history and file list along with the session, and "I am done looking at
    this" is not "erase what it cost me".
    """
    action = payload.get("action", "hide")
    if action == "bulk":
        days = float(payload.get("days") or 30)
        kinds = tuple(payload.get("kinds") or ())
        keys = store.hide_older_than(days, kinds=kinds)
        log.info("hid %d thread(s) untouched for %sd%s", len(keys), days,
                 f" (kinds={','.join(kinds)})" if kinds else "")
        return {"ok": True, "hidden": len(keys), "keys": keys}
    key = payload.get("key", "")
    if not store.get(key):
        return {"ok": False, "error": "unknown thread"}
    ok = store.set_hidden(key, action != "unhide")
    return {"ok": ok, "key": key, "hidden": action != "unhide"}


server = LocalServer(APPROVAL_PORT)
server.route("/session-event", handle_session_event)
server.route("/status", handle_status)
server.route("/web-message", handle_web_message)
server.route("/register-terminal", handle_register_terminal)
server.route("/learnings", handle_learnings)
server.route("/summaries", handle_summaries)
server.route("/release", handle_release)
server.route("/titles", handle_titles)
server.route("/tasks", handle_tasks)
server.route("/projects", handle_projects)
server.route("/defer", handle_defer)
server.route("/file-task", handle_file_task)
server.route("/hide", handle_hide)

approvals: ApprovalManager | None = None
if CLAUDE_APPROVAL_MODE == "slack":
    def _resolve_thread(session_id: str):
        if session_id in ACTIVE_SESSIONS:
            return ACTIVE_SESSIONS[session_id]
        key = store.find_by_session(session_id)
        if key:
            ch, ts = key.split(":", 1)
            return ch, ts
        return None

    approvals = ApprovalManager(
        app.client, timeout=APPROVAL_TIMEOUT,
        auto_allow=APPROVAL_AUTO_ALLOW, allowed_users=ALLOWED_USERS,
        resolve_thread=_resolve_thread)
    approvals.register(app)
    server.route("/approve", approvals.handle_request)

# The task board, for when the dashboard is out of reach: one message in a
# private channel, kept current. (It was the app's Home tab until that turned
# out to send Reply in Slack's Threads view to Home instead of the thread.)
# Its buttons call handle_tasks, the dashboard's own route, so the two cannot
# disagree about what a click is allowed to do.
BOARD = home.Board(
    state_path=BOARD_STATE, channel=BOARD_CHANNEL, bot_user=BOT_USER_ID,
    store=task_store, call=handle_tasks, allowed_users=ALLOWED_USERS,
    base_url=TEAM_URL,
    watching=lambda: handle_tasks({"action": "watching"}).get("watching", []),
    unmerged=lambda: branches.line(branches.survey(list(task_store.all().values()))))
home.register(app, BOARD)

server.start()


def handle_prompt(event: dict, say, client) -> None:
    channel = event["channel"]
    msg_ts = event["ts"]
    user = event.get("user", "")
    thread_ts = event.get("thread_ts", msg_ts)
    key = f"{channel}:{thread_ts}"

    if _dedup(channel, msg_ts):
        return
    if not event.get("_web") and _already_handled(key, msg_ts):
        log.info("ignoring redelivered message %s on %s", msg_ts, key)
        return
    if ALLOWED_USERS and not event.get("_web") and user not in ALLOWED_USERS:
        say(text="Sorry, you're not on this bot's allowlist.", thread_ts=thread_ts)
        return

    text = MENTION_RE.sub("", event.get("text", "")).strip()
    files = event.get("files") or []

    if text.startswith("!") and handle_command(text, key, say, thread_ts):
        # Commands get the same redelivery guard as prompts: a restart-replayed
        # !reset or !takeover would otherwise run a second time. Only for
        # threads that already exist -- a command on an unknown thread has
        # nothing to protect, and recording one would create an empty entry.
        if not event.get("_web") and store.get(key):
            store.update(key, last_msg_ts=msg_ts)
        return
    if not text and not files:
        say(text="Send me a prompt (or `!help`) and I'll spin up a Claude session for this thread.",
            thread_ts=thread_ts)
        return

    entry = store.get(key) or {}

    if entry.get("checked_out"):
        if HANDOFF_SLACK_WINS:
            killed = kill_terminal(entry.get("session_id", ""))
            store.update(key, checked_out=False, terminal_live=False)
            if killed:
                say(text=":leftwards_arrow_with_hook: _Closed the live terminal session — Slack takes over._",
                    thread_ts=thread_ts)
            entry = store.get(key) or {}
        else:
            live = " (a terminal session is live right now)" if entry.get("terminal_live") else ""
            say(text=f":no_entry_sign: This thread is checked out to the terminal{live}. "
                     "Send `!takeover` to force-close it and run your message here, or `!back` "
                     "if the terminal is already done.",
                thread_ts=thread_ts)
            return

    session_id = entry.get("session_id")
    model = entry.get("model") or CLAUDE_MODEL
    cwd = Path(entry.get("cwd")) if entry.get("cwd") else resolve_cwd(client, channel)

    # Assemble the prompt: [thread context] + text + [attachment notes]
    prompt = text or "(see the attached files)"
    if session_id is None and thread_ts != msg_ts:
        ctx = thread_context(client, channel, thread_ts, msg_ts)
        if ctx:
            prompt = ctx + "The user now says: " + prompt
    if files:
        saved = download_attachments(files, UPLOADS_ROOT / key.replace(":", "__"), key)
        if saved:
            by_name = {Path(f.get("name") or "").name: f for f in files}
            lines, any_image = [], False
            for path in saved:
                meta = by_name.get(path.name.split("-", 1)[-1], {})
                mime = meta.get("mimetype") or ""
                size = meta.get("size")
                note = f" ({mime}{f', {size // 1024} KB' if size else ''})" if mime else ""
                if mime.startswith("image/"):
                    any_image = True
                    note += " — an image; use Read to look at it"
                lines.append(f"- {path}{note}")
            listing = "\n".join(lines)
            prompt += (f"\n\n[The user attached {len(saved)} file(s), saved locally:\n"
                       f"{listing}\n"
                       + ("Read the image before answering — the user is showing you "
                          "something, not describing it." if any_image else
                          "Read them if they are relevant to the request.") + "]")

    # Stable per-thread outbox path: this string ends up in --append-system-prompt,
    # and prompt caching is a byte-exact prefix match — a path that changes every
    # message invalidates the cache and re-bills the whole history at full price.
    outbox = OUTBOX_ROOT / key.replace(":", "__")
    system_note = (
        "You are replying inside Slack; keep responses conversational. "
        f"If you create a file the user should receive, copy it into {outbox} "
        "and it will be uploaded to the Slack thread automatically."
    )
    system_note += "\n\n" + defer.HOW_TO.format(bin=SILKWORM_BIN)
    system_note += "\n\n" + scoping.HOW_TO.format(bin=SILKWORM_BIN,
                                                   max=scoping.MAX_PER_TURN)
    learn_block = render_block(learnings.applicable(str(cwd)))
    if learn_block:
        system_note += "\n\n" + learn_block

    # Every prompt is a task now. Created before the lock, so a message
    # waiting its turn is visibly queued rather than invisible.
    task = task_store.create(
        text or "(attached files)",
        role="assistant",
        source="ui" if event.get("_web") else "slack",
        source_ref=msg_ts,
        thread=key,
        # Bound once with !project, inherited by every turn after.
        project=(store.get(key) or {}).get("project", ""),
        # A conversation is never isolated -- it runs where your uncommitted
        # edits are. Recorded rather than inferred, because a restart may hand
        # this very task to the queue runner (close_out_orphans) so the message
        # is not lost, and being run by the runner must not move where it runs.
        isolate=False,
        scope={"cwd": str(cwd), "repo": repos.identity(str(cwd))},
    )
    task_id = task["id"]

    progress = ProgressMessage(client, channel, thread_ts)
    reactions = ThreadReactions(client, channel, thread_ts,
                                None if event.get("_web") else msg_ts)
    reactions.working()
    worked = [False]                  # reached a tool call; see execute_task
    lock = _thread_lock(key)
    if lock.locked():
        progress.update(":hourglass_flowing_sand: _Queued behind an earlier message in this thread…_")

    try:
        with lock, repo_guard(cwd, progress):
            entry = store.get(key) or {}
            session_id = entry.get("session_id")
            log.info("thread=%s session=%s cwd=%s prompt=%r", key, session_id or "NEW", cwd, text[:120])
            # Recorded before the run so a restart mid-turn can find and finish
            # it. Inside the lock, so it describes the turn actually running.
            recovery.mark_pending(store, key, msg_ts=reactions.msg,
                                  progress_ts=progress.ts,
                                  session_id=session_id, prompt=text)
            begin_turn(key)
            task_state(task_id, tasks.RUNNING)
            if not event.get("_web"):  # web prompts have a synthetic ts
                store.update(key, last_msg_ts=msg_ts)

            def on_init(sid: str) -> None:
                ACTIVE_SESSIONS[sid] = (channel, thread_ts)
                # A new session has no id until now; recovery needs it to find
                # the transcript if this process dies mid-turn.
                recovery.note_session(store, key, sid)

            def on_activity(name: str, tool_input: dict) -> None:
                worked[0] = True
                progress.update(f":hourglass_flowing_sand: `{name}` {describe_tool(name, tool_input)[:120]}")

            def on_start(handle) -> None:
                RUNNING[key] = handle
                RUNNING_TASKS[task_id] = handle

            kwargs = dict(
                binary=CLAUDE_BIN, cwd=cwd, permission_args=permission_args(),
                model=model, append_system_prompt=system_note,
                extra_args=CLAUDE_EXTRA_ARGS, env=claude_env(key),
                timeout=CLAUDE_TIMEOUT, idle_timeout=CLAUDE_IDLE_TIMEOUT,
                on_init=on_init, on_activity=on_activity, on_start=on_start,
            )
            # The outbox dir is shared by every turn in this thread, so its
            # whole lifecycle (create -> upload -> remove) stays inside the lock.
            outbox.mkdir(parents=True, exist_ok=True)
            try:
                try:
                    result = run_turn(prompt, session_id=session_id, **kwargs)
                except (ClaudeStopped, ClaudeTimeout):
                    # A stop or a timeout says nothing about whether the session
                    # is still good — discarding it would throw away the whole
                    # thread's history over one slow turn.
                    raise
                except ClaudeError as e:
                    # Only a session that genuinely no longer exists justifies
                    # starting over. Every other error -- a mid-turn API hiccup,
                    # a tool failure, "Claude reported an error" -- says nothing
                    # about whether the session is resumable, and starting fresh
                    # silently discards the thread's entire history. If the
                    # transcript is on disk, the session is fine: surface the
                    # error and let the next message resume it.
                    if session_id is None or harvester.find_transcript(session_id):
                        raise
                    log.warning("session %s for %s has no transcript on disk; "
                                "starting a fresh session (%s)", session_id[:8], key, e)
                    store.add_event(key, "session-lost",
                                    f"transcript for {session_id[:8]} missing; started fresh")
                    # Keep the entry (title, cost, summary, files) and remember
                    # the old id rather than dropping everything on the floor.
                    prev = (entry.get("previous_sessions") or []) + [session_id]
                    store.update(key, session_id=None, previous_sessions=prev[-10:])
                    progress.update(":hourglass_flowing_sand: _Old session was gone — starting fresh…_")
                    result = run_turn(prompt, session_id=None, **kwargs)

                RUNNER_HOLD.open()
                store.update(key, session_id=result.session_id, model=entry.get("model"), cwd=str(cwd))
                store.add_cost(key, result.cost_usd)
                if session_id is None and not entry.get("title"):
                    threading.Thread(target=name_thread, args=(key, text, result.text),
                                     daemon=True, name="namer").start()
                if result.session_id:
                    ACTIVE_SESSIONS[result.session_id] = (channel, thread_ts)
                uploaded = upload_outbox(client, outbox, channel, thread_ts, key)
            finally:
                shutil.rmtree(outbox, ignore_errors=True)

        total = (store.get(key) or {}).get("cost", 0.0)
        footer = (f"\n\n_:stopwatch: {fmt_duration(result.duration_ms)} · "
                  f"${result.cost_usd:.4f} · thread total ${total:.2f}_")
        parts = chunk(to_mrkdwn(result.text))
        parts[-1] += footer
        progress.finalize(parts[0])
        for part in parts[1:]:
            say(text=part, thread_ts=thread_ts)

        if uploaded:
            log.info("uploaded %d file(s) from outbox for %s", uploaded, key)
        reactions.done()
        # A conversation in a project-bound thread is where most decisions get
        # made; without this the brief only ever learns from queued tasks.
        refresh_brief((store.get(key) or {}).get("project", ""),
                      f"Asked: {text[:500]}\n\nAnswered: {result.text[:1500]}")
        task_store.update(task_id, session_id=result.session_id,
                          result={"text": result.text[:4000],
                                  "cost": result.cost_usd,
                                  "files_uploaded": uploaded})
        task_state(task_id, tasks.DONE)
        refresh_summary(key)

    except ClaudeStopped:
        progress.finalize(":octagonal_sign: Stopped.")
        reactions.cleared()
        task_state(task_id, tasks.CANCELLED, "stopped by the user")
    except ClaudeTimeout as e:
        progress.finalize(f":warning: {e}")
        reactions.failed()
        store.add_event(key, "timeout", str(e))
        if not fail_or_retry(task_id, str(e), started=worked[0]):
            task_state(task_id, tasks.FAILED, str(e)[:160])
    except ClaudeError as e:
        progress.finalize(f":warning: {e}")
        reactions.failed()
        store.add_event(key, "error", str(e)[:160])
        if not fail_or_retry(task_id, str(e), started=worked[0]):
            task_state(task_id, tasks.FAILED, str(e)[:160])
    except Exception:
        log.exception("unhandled error in thread %s", key)
        progress.finalize(":warning: Something went wrong — check the bot logs.")
        reactions.failed()
        task_state(task_id, tasks.FAILED, "unhandled error")
    finally:
        RUNNING.pop(key, None)
        RUNNING_TASKS.pop(task_id, None)
        recovery.clear_pending(store, key)


@app.event("app_mention")
def on_mention(event, say, client):
    handle_prompt(event, say, client)


#: Subtypes that are still a person talking to us. A file upload arrives as a
#: message with subtype "file_share" -- dropping every subtype silently threw
#: away every screenshot before it reached the attachment handling.
HANDLED_SUBTYPES = {"file_share"}


def should_handle(event: dict) -> bool:
    """Whether an incoming message event is a DM from a human we should answer."""
    if event.get("channel_type") != "im":
        return False
    if event.get("bot_id") or event.get("user") == BOT_USER_ID:
        return False
    subtype = event.get("subtype")
    if subtype and subtype not in HANDLED_SUBTYPES:
        return False          # joins, edits, deletions, channel noise
    return True


@app.event("message")
def on_message(event, say, client):
    if should_handle(event):
        handle_prompt(event, say, client)


# --- Housekeeping -----------------------------------------------------------------

def system_boot_time() -> float:
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "kern.boottime"],
                                 capture_output=True, text=True).stdout
            m = re.search(r"sec = (\d+)", out)
            return float(m.group(1)) if m else 0.0
        with open("/proc/uptime") as f:  # linux
            return time.time() - float(f.read().split()[0])
    except Exception:
        return 0.0


def reconcile_checkouts() -> None:
    """Release checkouts that predate the current boot — their terminal
    sessions cannot have survived the reboot."""
    boot = system_boot_time()
    if not boot:
        return
    for key, entry in store.all().items():
        if entry.get("checked_out") and entry.get("updated", 0) < boot:
            store.update(key, checked_out=False, terminal_live=False)
            log.info("released stale pre-boot checkout %s", key)


TASK_POLL_S = 10


def home_channel() -> str:
    """Where a task with no thread of its own should report."""
    if SILKWORM_HOME_CHANNEL:
        return SILKWORM_HOME_CHANNEL
    dms = [k for k, v in sorted(store.all().items(), key=lambda kv: -kv[1].get("updated", 0))
           if k.startswith("D")]
    return dms[0].split(":", 1)[0] if dms else ""


def task_thread(task: dict) -> tuple[str, str]:
    """The Slack thread a task narrates into, creating one if it has none.

    A task created in the dashboard has nowhere to talk, so it gets an anchor
    message — the same trick terminal-started sessions use — and from then on
    it behaves like any other thread.
    """
    if task.get("thread") and ":" in task["thread"]:
        channel, _, thread_ts = task["thread"].partition(":")
        return channel, thread_ts
    channel = home_channel()
    if not channel:
        return "", ""
    resp = app.client.chat_postMessage(
        channel=channel,
        text=f":clipboard: *Task* — {task.get('title') or task['id']}\n"
             f"_{task['id']} · queued from {task.get('source', 'ui')}_")
    thread_ts = resp["ts"]
    key = f"{channel}:{thread_ts}"
    task_store.update(task["id"], thread=key)
    # Label it so the dashboard doesn't list a task run as an untitled
    # conversation sitting alongside real ones.
    store.update(key, kind="task",
                 title=f"Task: {(task.get('title') or task['id'])[:52]}")
    return channel, thread_ts


def tell_thread(key: str, text: str) -> None:
    """Put a line in a task's own thread, where it survives the toast.

    Never allowed to cost the action it is attached to: a task that could not
    be told about its checkout is a worse outcome than one that could not be
    approved. So a missing thread, a Slack outage or a malformed key logs and
    is swallowed.
    """
    if not key or ":" not in key or not text:
        return
    try:
        channel, thread_ts = key.split(":", 1)
        app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
    except Exception:
        log.exception("could not post to %s", key)


def record_branch(tid: str, worktree, scope: dict) -> None:
    """Write down what this task left in git, before its checkout goes away.

    The worktree is about to be released and it is the only thing that knows
    which branch the work is on and what it was cut from. Afterwards the branch
    is still there but nothing points at it: eight finished tasks each left a
    commit nobody merged, and the board recorded eight plain `done`.

    Never allowed to cost a reply. A failure here loses the note, not the work,
    so it is logged and swallowed rather than raised into the turn.
    """
    try:
        repo = worktrees.main_repo(worktree)
        base = worktrees.base_ref(repo, fetch=False,
                                  prefer=(scope or {}).get("branch") or "") if repo else ""
        made = worktrees.commits_on(worktree, base)
        branch = worktrees.branch_of(worktree)
        if not made or not branch:
            return                               # nothing to point anyone at
        task_store.update(tid, branch=branch, commits=made,
                          base=branches.base_name(repo, base) if repo else base)
    except Exception:
        log.exception("could not record the branch for %s", tid)


def review_branch(task: dict) -> str:
    """The branch a review has to stand on, or "" if there is none.

    An isolated implementor commits inside its own worktree, on
    `silkworm/<task-id>`, and nowhere else — and that checkout is released when
    its turn ends. So a review handed the scope the implementor *started* from
    arrives in the main checkout, on the base branch, and audits a tree the
    change never reached. It can then do nothing but believe the summary, which
    is the self-certification the gate exists to prevent — and since the
    landing chain runs off a passing review, that verdict is now one of the two
    things that merge work with nobody watching.

    Read off what the implementor actually left rather than assumed from the
    task id: a task that ran in the shared checkout has no branch of its own,
    and there is nothing to attach. record_branch writes it before the
    implementor's checkout goes away, which is the last moment anything knows.
    """
    if task.get("role") != "reviewer":
        return ""
    parent = task_store.get(task.get("parent") or "") or {}
    return parent.get("branch") or ""


def refuse_review(task: dict, branch: str, progress, channel: str,
                  thread_ts: str) -> None:
    """End a review that cannot reach the work, without letting it certify.

    The gate exists so that nothing completes on its own say-so, and the
    landing chain now merges off a passing verdict. A review that cannot get
    at the branch therefore has exactly one safe outcome: no verdict, and the
    work it was gating goes in front of a person. Failing the review alone
    would strand its parent in `blocked` with nothing left to unblock it.
    """
    tid, parent_id = task["id"], task.get("parent") or ""
    why = f"could not check out `{branch}`, so nothing was reviewed"
    log.warning("review %s of %s: %s", tid, parent_id, why)
    progress.finalize(f":hand: _Review not run — {why}._")
    parent = task_store.get(parent_id)
    # The no-verdict goes on the parent *before* the review is failed. Failing
    # a blocker releases whatever waits on it, and the release sends a waiter
    # with a result to awaiting_approval and one without to failed -- so
    # writing it afterwards lost the race to a rule that would otherwise agree.
    if parent:
        task_store.update(parent_id, result={
            **(parent.get("result") or {}),
            "review": roles.no_verdict(why)})
    task_state(tid, tasks.FAILED, why[:160])
    if not parent:
        return
    if (task_store.get(parent_id) or {}).get("state") == tasks.BLOCKED:
        task_state(parent_id, tasks.AWAITING_APPROVAL, why[:160])
    try:
        app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=(f":hand: *Review could not run* — {why}. `{parent_id}` is "
                  f"waiting for you rather than completing unreviewed."))
    except Exception:
        log.exception("posting the refused review failed")


def execute_task(task: dict) -> None:
    """Run one claimed task. Already in `running` — the claim did that."""
    tid = task["id"]
    role_name = task.get("role") or "assistant"
    # A role we cannot recognise is not run. roles.get() now falls back to
    # read-only rather than to the unrestricted assistant template, so this is
    # belt and braces -- but a task whose permissions are a guess should stop
    # and say so rather than quietly run under permissions nobody chose.
    if not roles.known(role_name):
        log.error("task %s names unknown role %r; refusing to run it", tid, role_name)
        task_state(tid, tasks.FAILED, f"unknown role {role_name!r}")
        return
    fresh = roles.is_fresh(role_name)
    scope = task.get("scope") or {}
    cwd = Path(scope.get("cwd") or CLAUDE_CWD)
    channel, thread_ts = task_thread(task)
    if not channel:
        log.error("task %s has nowhere to report (set SILKWORM_HOME_CHANNEL)", tid)
        task_state(tid, tasks.FAILED, "no Slack channel to report into")
        return

    key = f"{channel}:{thread_ts}"
    progress = ProgressMessage(app.client, channel, thread_ts)

    # Self-contained work in a repository gets its own checkout. It cannot then
    # leave your working tree dirty or on another branch, and it no longer
    # queues behind a conversation about the same repo. Only self-contained
    # work: a worktree cannot see uncommitted changes in your main tree, so a
    # conversation about what you are editing right now must stay where you are
    # editing it.
    #
    # That decision is on the record (`isolate`), taken when the task was made.
    # It used to be read off `driver`, which close_out_orphans rewrites at
    # startup so an orphaned Slack message is re-run rather than dropped -- and
    # that quietly moved the conversation into a worktree the user's edits were
    # not in, then committed to silkworm/<task-id> instead of their branch.
    worktree = None
    # Where the *thread* lives, as distinct from where this turn runs. A turn
    # in a worktree must never leave that path behind as the thread's home: the
    # worktree is released when the turn ends, so every later message in that
    # thread fails with "working directory no longer exists" -- permanently,
    # and with no obvious connection to the task that caused it.
    home_cwd = cwd
    # A read-only role has nothing to isolate: it cannot write, so a worktree
    # buys nothing and leaves an empty branch behind every run -- one per
    # project per night once ideation is scheduled.
    #
    # A review does not branch, it borrows. The work it has to read is already
    # committed on the implementor's branch, so it gets a checkout of *that*
    # branch rather than a fresh one off the base — a fresh one would hold none
    # of it. `borrowed` then marks the branch under this turn as somebody
    # else's: not this task's work to record, and not this task's branch to
    # tidy away. The checkout is keyed by the implementor's id, not the
    # review's, so that it lives exactly as long as the work in it is
    # unresolved: the sweep keeps what a non-terminal task owns, and the
    # implementor stays blocked until the verdict lands.
    borrowed = review_branch(task)
    if borrowed:
        progress.update(":deciduous_tree: _Checking out the work to review…_")
        worktree = (worktrees.attach(cwd, task["parent"], borrowed, label="review")
                    if worktrees.is_repo(cwd) else None)
        if not worktree:
            # No checkout, no review. Carrying on in the main tree is worse
            # than not reviewing at all: the prompt has already promised this
            # reviewer a checkout of `borrowed`, so it runs `git log
            # <fork point>..HEAD` there and reads whatever has landed on the
            # base branch since — other people's commits, which it will find
            # nothing wrong with. A passing verdict on those then lands work
            # nobody looked at. So the gate refuses instead.
            refuse_review(task, borrowed, progress, channel, thread_ts)
            return
        cwd = worktree
        task_store.update(tid, scope={**scope, "worktree": str(worktree)})
    elif (tasks.isolated(task) and worktrees.is_repo(cwd)
            and not roles.get(role_name).get("restricted")):
        progress.update(":deciduous_tree: _Setting up an isolated checkout…_")
        worktree = worktrees.create(cwd, tid, base=scope.get("branch") or "")
        if worktree:
            cwd = worktree
            task_store.update(tid, scope={**scope, "worktree": str(worktree)})

    outbox = OUTBOX_ROOT / key.replace(":", "__")
    system_note = (
        "You are completing a task; report the outcome concisely. "
        f"If you create a file the user should receive, copy it into {outbox}."
    )
    if task.get("source") == "defer":
        system_note += "\n\n" + defer.WAKE_NOTE
    else:
        system_note += "\n\n" + defer.HOW_TO.format(bin=SILKWORM_BIN)
    role_system = roles.system_prompt(role_name)
    if role_system:
        system_note += "\n\n" + role_system
    learn_block = render_block(learnings.applicable(str(cwd)))
    if learn_block:
        system_note += "\n\n" + learn_block

    # Whether the turn got as far as a tool call. One that died before that --
    # a quota message, typically, three seconds in -- never started the work,
    # and must not be charged an attempt for it.
    worked = [False]
    lock = _thread_lock(key)
    try:
        with lock, repo_guard(cwd, progress):
            entry = store.get(key) or {}
            # A fresh role starts its own session; resuming the thread's would
            # hand the reviewer the very conversation it is meant to audit.
            session_id = None if fresh else (task.get("session_id") or entry.get("session_id"))
            recovery.mark_pending(store, key, msg_ts=None, progress_ts=progress.ts,
                                  session_id=session_id, prompt=task.get("goal", ""))
            begin_turn(key)
            outbox.mkdir(parents=True, exist_ok=True)

            def on_start(handle) -> None:
                RUNNING[key] = handle
                RUNNING_TASKS[tid] = handle

            def on_activity(name: str, tool_input: dict) -> None:
                worked[0] = True
                progress.update(f":hourglass_flowing_sand: `{name}` "
                                f"{describe_tool(name, tool_input)[:120]}")

            try:
                result = run_turn(
                    task.get("goal", ""), session_id=session_id,
                    binary=CLAUDE_BIN, cwd=cwd,
                    permission_args=roles.permission_args(role_name, permission_args(),
                                                        bin=SILKWORM_BIN),
                    model=entry.get("model") or CLAUDE_MODEL,
                    append_system_prompt=system_note, extra_args=CLAUDE_EXTRA_ARGS,
                    env=claude_env(key, task.get("defers") or 0, role=role_name),
                    timeout=CLAUDE_TIMEOUT, idle_timeout=CLAUDE_IDLE_TIMEOUT,
                    on_init=lambda sid: task_store.update(tid, session_id=sid),
                    on_activity=on_activity,
                    on_start=on_start,
                )
                RUNNER_HOLD.open()
                # A fresh run must not repoint the thread at its throwaway
                # session, or the next Slack message resumes the review.
                if not fresh:
                    store.update(key, session_id=result.session_id,
                                 cwd=str(home_cwd))
                store.add_cost(key, result.cost_usd)
                uploaded = upload_outbox(app.client, outbox, channel, thread_ts, key)
            finally:
                shutil.rmtree(outbox, ignore_errors=True)

        if task.get("source") == "defer" and result.text.strip() == defer.QUIET:
            # The check ran and found nothing worth interrupting for. Say
            # nothing: a watch that narrates every poll is worse than no watch.
            # But only if it actually scheduled the next one -- "nothing yet"
            # with no successor is a watch that quietly stopped watching, which
            # is the exact silence this whole mechanism exists to prevent.
            task_store.update(tid, session_id=result.session_id,
                              result={"text": defer.QUIET, "cost": result.cost_usd})
            if task_store.has_pending_wakeup(key):
                progress.delete()
                task_state(tid, tasks.DONE, "nothing to report yet")
            else:
                progress.finalize(defer.STOPPED)
                task_state(tid, tasks.NEEDS_INPUT, "watch ended without scheduling")
            return

        # Verify before the checkout is released: it is the only place the work
        # exists. Running afterwards pointed at a deleted directory, which
        # verify.run reported as "could not run" -- correctly -- and the caller
        # read as "nothing to verify". Every task passed unverified, silently.
        checked = None
        if roles.needs_review(role_name) and not task.get("blocked_on"):
            checked = verify_work(task, cwd)
            if checked["ran"]:
                task_store.update(tid, verified=bool(checked["ok"]))

        wt_note = ""
        if worktree and (checked is None or checked.get("ok") or not checked["ran"]):
            # A failure keeps its checkout: the next attempt reattaches the
            # branch anyway, but leaving it makes the failure inspectable.
            #
            # A borrowed branch is left strictly alone. Recording it would file
            # the implementor's commits against the review, and letting go of
            # it as an empty branch would discard the very work the review just
            # read — release() only deletes a branch it believes has nothing on
            # it, and "nothing on it" is a count against a base that has read
            # as zero before now. It must also be gone before resolve_review
            # below: a passing verdict lands, and the landing reattaches this
            # same branch, which git refuses while another worktree holds it.
            if not borrowed:
                record_branch(tid, worktree, scope)
            removed, note = worktrees.release(worktree,
                                              delete_empty_branch=not borrowed)
            worktree = None                      # released; finally need not repeat it
            if borrowed:
                # The ordinary note would claim the implementor's branch as
                # this turn's work. A *failed* release is the opposite: it is
                # the only record that the branch is still checked out, and
                # the landing a few lines below will refuse to reattach it
                # for a reason nothing else anywhere states.
                if not removed:
                    log.warning("the review's checkout was not released: %s", note)
                    wt_note = (f"\n\n_:deciduous_tree: The checkout this review "
                               f"read {note} — the branch is still held._")
            elif note:
                wt_note = (f"\n\n_:deciduous_tree: Worked in an isolated checkout — {note}._"
                           if removed else
                           f"\n\n_:deciduous_tree: Isolated checkout {note}._")

        total = (store.get(key) or {}).get("cost", 0.0)
        verdict = ("\n\n" + verify.summary(checked)) if checked else ""
        parts = chunk(to_mrkdwn(result.text + wt_note + verdict))
        parts[-1] += (f"\n\n_:stopwatch: {fmt_duration(result.duration_ms)} · "
                      f"${result.cost_usd:.4f} · thread total ${total:.2f}_")
        progress.finalize(parts[0])
        for part in parts[1:]:
            app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=part)
        task_store.update(tid, session_id=result.session_id,
                          result={"text": result.text[:4000], "cost": result.cost_usd,
                                  "files_uploaded": uploaded})
        if checked and not checked["ok"] and checked["ran"]:
            if send_back_for_tests(task, checked, channel, thread_ts):
                return
        if not resolve_review(task, role_name, result.text, channel, thread_ts):
            task_state(tid, tasks.DONE)
        refresh_summary(key)
        refresh_brief(task.get("project", ""),
                      f"Task: {task.get('goal', '')[:500]}\n\n"
                      f"Outcome: {result.text[:1500]}")
    except ClaudeStopped:
        progress.finalize(":octagonal_sign: Stopped.")
        task_state(tid, tasks.CANCELLED, "stopped by the user")
    except ClaudeError as e:
        progress.finalize(f":warning: {e}")
        if not fail_or_retry(tid, str(e), started=worked[0]):
            task_state(tid, tasks.FAILED, str(e)[:160])
    except Exception as e:
        log.exception("task %s failed", tid)
        progress.finalize(":warning: Task failed — check the bot logs.")
        task_state(tid, tasks.FAILED, str(e)[:160])
    finally:
        if worktree:
            # Any path out of the turn that did not release it. release() keeps
            # a dirty tree, so a failed task's partial work survives.
            try:
                if not borrowed:
                    record_branch(tid, worktree, scope)
                worktrees.release(worktree, delete_empty_branch=not borrowed)
            except Exception:
                log.exception("could not release worktree %s", worktree)
        RUNNING.pop(key, None)
        RUNNING_TASKS.pop(tid, None)
        recovery.clear_pending(store, key)


#: How many times work may be sent back for failing its own tests before it
#: stops and asks. A change that cannot pass after this many goes is not one
#: more attempt away from passing.
MAX_VERIFY_ATTEMPTS = int(os.environ.get("MAX_VERIFY_ATTEMPTS", "2"))


def verify_work(task: dict, cwd) -> dict:
    """Run the project's own tests against this task's checkout.

    Evidence, not judgement: a subprocess and an exit code, with no model in
    between that could report a suite as green when it was not. Returns the
    raw result; `ran=False` means there was nothing to run, which callers must
    not confuse with failing.
    """
    proj = project_store.get(task.get("project") or "") or {}
    cmd = (proj.get("test_cmd") or "").strip()
    if not cmd:
        return {"ran": False, "ok": False, "code": None,
                "output": "no test command is configured for this project"}
    log.info("verifying %s with %r in %s", task["id"], cmd, cwd)
    return verify.run(cmd, cwd)


def send_back_for_tests(task: dict, result: dict, channel: str, thread_ts: str) -> bool:
    """Requeue failing work with the failure attached. True if it was sent back.

    The implementor gets the actual output rather than "it failed", and the
    attempt is counted -- a change that cannot pass after a couple of goes is
    not one more away from passing, and should cost a person's attention
    instead of another session.
    """
    tid = task["id"]
    tried = int(task.get("verify_attempts") or 0) + 1
    task_store.update(tid, verify_attempts=tried)
    if tried > MAX_VERIFY_ATTEMPTS:
        app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f":x: *Tests still fail after {tried} attempts.* Leaving it for you.\n"
                 f"```\n{result.get('output', '')[-1200:]}\n```")
        task_state(tid, tasks.AWAITING_APPROVAL,
                   f"tests still failing after {tried} attempts")
        return True
    app.client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=f":arrows_counterclockwise: *Tests fail — sending it back* "
             f"(attempt {tried} of {MAX_VERIFY_ATTEMPTS}).")
    task_store.update(tid, goal=(task.get("goal", "") + "\n\n"
                                 + verify.rework_note(result)))
    task_state(tid, tasks.QUEUED, f"tests failed; sent back (attempt {tried})")
    return True


def file_followups(task: dict, followups: list[str]) -> list[str]:
    """File a passing review's non-blocking findings as proposals.

    A review that passes the work completes it silently, and that rule is what
    lets the board reach empty: the most common thing a reviewer says is that
    it could not run the test suite, and stopping to ask about each of those
    would make "what needs me?" permanently full and therefore ignored.

    But silence must not also be how a real bug is lost. A reviewer that
    noticed something on the way past had nowhere to put it: on a completed
    task the finding is never read again. `proposed` is already the answer to
    "worth a decision, not worth interrupting for" -- it reaches the same view,
    and a dismiss is one click. So each followup becomes one.

    Capped like any other unattended pass. A review that finds ten things has
    not prioritised either, and every proposal costs a decision.
    """
    proj = task.get("project") or ""
    # Not the parent's scope verbatim: it carries the worktree that turn ran
    # in, which was released when the turn ended.
    scope = project_store.scope_for(proj) or {
        k: v for k, v in (task.get("scope") or {}).items() if k != "worktree"}
    filed = []
    for finding in followups[:scoping.limit_for(propose=True)]:
        goal = (f"A review of {task['id']} ({task.get('title') or 'untitled'}) "
                f"found this alongside that task, rather than in it:\n\n"
                f"{finding}\n\n"
                "Check it is still true before changing anything -- the review "
                "read the tree as it was then, and main has moved since. If it "
                "is not, say so and stop.")
        # Counted per finding rather than once for the batch: a review is the
        # other unattended producer of proposals, so it is held to the same
        # standing limit as the nightly pass -- otherwise a board too deep for
        # the ideator to touch keeps filling through this door instead, and
        # the panel reports it paused while it grows. Live, so the batch stops
        # at the limit rather than filing all five past it.
        err = scoping.validate(
            goal, propose=True, slug=proj,
            open_now=(scoping.open_proposals(task_store.by_project(proj))
                      if proj else 0))
        if err:
            log.warning("not filing a review followup on %s: %s", task["id"], err)
            continue
        try:
            child = task_store.create(
                goal, title=f"From review: {finding[:52]}", role="implementor",
                project=proj, state=tasks.PROPOSED, driver="queue",
                # Filed work: whoever picks it up starts fresh, so it gets its
                # own checkout rather than whatever tree you are mid-edit in.
                isolate=True,
                source="review", source_ref=task["id"],
                root=task.get("root") or task["id"], scope=scope)
        except ValueError:
            log.exception("could not file a review followup on %s", task["id"])
            continue
        filed.append(child["id"])
    return filed


def resolve_review(task: dict, role_name: str, text: str,
                   channel: str, thread_ts: str) -> bool:
    """Apply the review gate. Returns True if the task's fate is already settled.

    An implementor does not finish on its own say-so: its output goes to a
    reviewer with fresh context, and the task waits. The reviewer's verdict
    then either completes it silently or puts it in front of the user with
    specific findings — which is the whole point, spending tokens so that only
    flagged work costs attention.

    That leaves a third thing a reviewer says, and it used to have nowhere to
    go: a real problem that is not this task's fault. Routing on `ok` alone
    meant it was written to a completed task and never read. It now gets its
    own destination — `findings` stop the task, `followups` are filed as
    proposals, and `unverified` is recorded and nothing else — so no finding
    depends on the user opening a task that is already done.
    """
    tid = task["id"]
    if roles.needs_review(role_name) and not task.get("blocked_on"):
        # Not the implementor's scope verbatim: it carries the worktree that
        # turn ran in, which was released a few lines ago. What the review
        # needs is the branch the work was left on — which it checks out for
        # itself when its turn starts, since that may be hours from now and
        # holding a directory open across a queue is how the last attempt at
        # this ended up special-casing quota failures.
        scope = {k: v for k, v in (task.get("scope") or {}).items()
                 if k != "worktree"}
        # From the store, not the dict in hand. record_branch wrote the branch
        # moments ago, on the way out of the checkout — the only moment
        # anything knew it — and the record this turn has been carrying since
        # it was claimed predates that. Read the stale one and the prompt names
        # no branch, which is the whole bug wearing a smaller hat.
        branch = (task_store.get(tid) or task).get("branch") or ""
        # Resolved the way execute_task resolves it, including the fallback.
        # Read straight off the scope, a task filed against a project with a
        # base branch but no directory (`!base` on a repo-less project writes
        # exactly that) left this empty while the review still ran in
        # CLAUDE_CWD — so the prompt named the branch and then told it to look
        # in "?", and dropped the two commands worth having.
        here = str(scope.get("cwd") or CLAUDE_CWD)
        where = str(worktrees.path_for(here, tid, "review")) if branch else here
        child = task_store.create(
            roles.review_goal(task, text, cwd=where, branch=branch,
                              base=worktrees.fork_point(
                                  here, branch, scope.get("branch") or "")
                              if branch else ""),
            role="reviewer", driver="queue",
            # Not isolated in the ordinary sense: a fresh worktree off the base
            # branch would hold none of the work. It gets a checkout of the
            # implementor's own branch instead — see review_branch.
            isolate=False,
            # Without an explicit title it would be the review prompt's first
            # line ("Goal that was given:"), which reads as nonsense in a list.
            title=f"Review: {(task.get('title') or tid)[:46]}",
            source="review", source_ref=tid, parent=tid,
            root=task.get("root") or tid, thread=f"{channel}:{thread_ts}",
            scope=scope)
        task_store.update(tid, blocked_on=[child["id"]])
        task_state(tid, tasks.BLOCKED, f"awaiting review {child['id']}")
        return True

    parent_id = task.get("parent")
    if role_name != "reviewer" or not parent_id:
        return False
    verdict = roles.parse_verdict(text)
    parent = task_store.get(parent_id)

    # A passing verdict completes the task silently. What it must not do is
    # swallow whatever the reviewer found that it did not consider blocking:
    # those go out as proposals, so they reach the same view by the route
    # built for things worth a decision but not an interruption.
    filed: list[str] = []
    if parent and verdict["ok"] and verdict["followups"]:
        try:
            filed = file_followups(parent, verdict["followups"])
        except Exception:
            log.exception("filing review followups for %s failed", parent_id)
    verdict = {**verdict, "filed": filed}

    note = (":white_check_mark: *Review passed* — " if verdict["ok"]
            else ":mag: *Review flagged this* — ") + (verdict["summary"] or "")
    if verdict["findings"]:
        note += "\n" + "\n".join(f"• {f}" for f in verdict["findings"])
    if verdict["followups"]:
        note += ("\n_Filed for you to accept or dismiss:_" if filed
                 else "\n_Also noted, alongside the task:_")
        note += "\n" + "\n".join(f"• {f}" for f in verdict["followups"])
        if filed:
            note += "\n" + "  ".join(f"`{i}`" for i in filed)
    if verdict["unverified"]:
        note += "\n_The review could not check:_ " + "; ".join(verdict["unverified"])
    try:
        app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=note)
    except Exception:
        log.exception("posting the review verdict failed")
    if parent:
        task_store.update(parent_id, result={**(parent.get("result") or {}),
                                             "review": verdict})
        stranded = ""
        if verdict["ok"]:
            # Land before completing: if the landing refuses, that is worth
            # knowing in the same breath as the verdict rather than later.
            outcome = land_and_record(parent_id, channel, thread_ts)
            if merge.needs_a_person(outcome):
                # Reviewed, proven, and still not on the base. Nobody has
                # looked at why, and `done` would say there is nothing to
                # look at -- which is how the same gap got implemented twice
                # on two different nights, each time at the price of a
                # session and a reviewer.
                #
                # A parent the user closed while its review was still running
                # cannot be parked -- `done` is terminal, and task_state
                # swallows the refusal rather than costing the reviewer its
                # reply. The record still carries the refusal, which is what
                # the board reads, so it does not become silent again.
                stranded = f"review passed but the landing refused " \
                           f"({outcome.get('stage')})"
        if stranded:
            task_state(parent_id, tasks.AWAITING_APPROVAL, stranded[:160])
        else:
            task_state(parent_id, tasks.DONE if verdict["ok"] else tasks.AWAITING_APPROVAL,
                       verdict["summary"][:160])
    return False


def landing_enabled(task: dict) -> bool:
    """Has this task's project asked Silkworm to merge on its behalf?

    Kept apart from the rest of the gate because it is the one refusal that is
    not a problem: a project that never opted in was always going to end with a
    branch, and saying so on every task would make the marker meaningless.
    """
    return bool((project_store.get(task.get("project") or "") or {}).get("auto_merge"))


def land_if_ready(task: dict) -> dict:
    """Land a task's branch if the project allows it and the work earned it.

    Everything here is a refusal by default. A project must opt in, must be
    able to prove itself, and the work must already have passed both its own
    tests and an independent review. Anything short of that leaves the branch
    where it is and says why.

    What comes back is a record rather than a sentence. It used to return the
    Slack line directly, which meant the caller could print it and nothing
    else: "not enabled", "never verified" and "the rebase conflicts" were one
    string, so every one of them ended the task as plainly `done`. `eligible`
    separates the two kinds -- False means it was never a candidate, True means
    git was asked and said no, which is the case that needs a person.
    """
    proj = project_store.get(task.get("project") or "") or {}
    scope = task.get("scope") or {}
    cwd = scope.get("cwd")
    # The same answer the unmerged survey uses, which honours the branch the
    # task actually recorded as its checkout closed rather than assuming the
    # convention. Two ways of naming it would be two things to keep in step.
    branch = branches.name_for(task)

    def never(stage: str, detail: str = "") -> dict:
        return {"eligible": False, "landed": False, "stage": stage,
                "detail": detail, "branch": branch}

    if not landing_enabled(task):
        return never("not-enabled", "this project does not land its own work")
    if not (proj.get("test_cmd") or "").strip():
        return never("no-test-command",
                     "this project has no test command, so nothing proved it")
    # This is also what keeps `send_back_for_tests` honest. Work parked in
    # awaiting_approval because its tests failed MAX_VERIFY_ATTEMPTS times is
    # not verified, so approving it -- which means "stop trying", not "merge
    # it" -- cannot reach a merge through here.
    if not task.get("verified"):
        return never("unverified", "the change was never verified")
    if not cwd:
        return never("no-checkout", "it has no checkout to land from")

    # The task's own worktree was released when its turn ended, so reattach the
    # branch. Rebasing and retesting need somewhere to happen that is not the
    # checkout being merged into.
    here = worktrees.attach(cwd, task["id"], branch)
    if not here:
        return {"eligible": True, "landed": False, "stage": "attach",
                "branch": branch, "detail": f"could not check out {branch}"}

    def run_tests(where):
        return verify.run(proj["test_cmd"], where)

    def on_merge(base_name, before):
        # What clear_interrupted_landings needs if a restart lands between the
        # fast-forward and the outcome being recorded. Raising here refuses the
        # merge, which is the point: unrecorded, that window reads as "nothing
        # was merged".
        record_landing(task, {"eligible": True, "landed": False,
                              "stage": LANDING_UNDERWAY, "branch": branch,
                              "detail": "merging", "checkpointed": True,
                              "base": base_name, "base_before": before})

    try:
        # Landing touches the shared checkout, so take the guard a turn takes.
        with repo_guard(cwd):
            result = merge.land(here, cwd, branch, scope.get("branch") or "",
                                run_tests, publish=bool(proj.get("publish")),
                                on_merge=on_merge)
    finally:
        worktrees.release(here)
    return {**result, "eligible": True, "branch": branch}


def record_landing(task: dict, outcome: dict) -> None:
    """Write what the landing did onto the record, so it outlives the message.

    A Slack line scrolls away within the hour. Until this existed the only
    durable trace was `result.landed`, written on success alone, so a refusal
    left the record identical to a clean merge -- which is how five branches
    carrying eleven commits sat behind tasks that read as finished.
    """
    if not outcome.get("eligible") and outcome.get("stage") == "not-enabled":
        return                        # landing was never this project's model
    # Re-read rather than trust the caller's copy: a landing runs for minutes,
    # and merging its outcome into a dict fetched before it started would drop
    # anything written in between -- the review verdict, most of all.
    current = task_store.get(task["id"]) or task
    keep = ("eligible", "landed", "stage", "detail", "branch", "head", "base",
            "checkpointed", "base_before")
    result = {**(current.get("result") or {}),
              "landing": {k: outcome[k] for k in keep if k in outcome}}
    if outcome.get("landed"):
        result["landed"] = outcome.get("head")
    task_store.update(task["id"], result=result)


def land_and_record(task_id: str, channel: str = "", thread_ts: str = "") -> dict:
    """Attempt a landing, record the outcome, and narrate it. Never raises."""
    task = task_store.get(task_id) or {"id": task_id}
    branch = branches.name_for(task)
    try:
        outcome = land_if_ready(task)
    except Exception:
        log.exception("landing %s failed", task_id)
        outcome = {"eligible": True, "landed": False, "stage": "errored",
                   "branch": branch, "detail": "the landing itself errored; "
                                               "see the bot log"}
    try:
        record_landing(task, outcome)
    except Exception:
        log.exception("recording the landing of %s failed", task_id)
    if channel and outcome.get("stage") != "not-enabled":
        text = merge.summary(outcome, branch)
        # "Landed on main" reads as *this is now true*. When the repo being landed
        # into is the one this process is running from, it is not: nothing restarts
        # the bot, so the commit is on disk and the old code is still serving. Say
        # so here, where the claim is made, rather than leaving it to be discovered.
        if outcome.get("landed") and _is_own_checkout((task.get("scope") or {}).get("cwd")):
            text += "\n:warning: _This is Silkworm's own checkout — the running bot is still on `" \
                    f"{REVISION['sha'][:8] or 'unknown'}`. Run `silkworm restart` to pick it up._"
        try:
            app.client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                        text=text)
        except Exception:
            log.exception("posting the landing result failed")
    return outcome



def _is_own_checkout(cwd) -> bool:
    """Whether a path is the tree this process was loaded from.

    An empty path is not it. `Path("").resolve()` is the *current* directory,
    which is this checkout whenever the bot runs from its own repo -- so the
    obvious one-liner answers "yes" to having been given nothing.
    """
    if not cwd:
        return False
    try:
        return Path(cwd).resolve() == BASE_DIR
    except (OSError, ValueError):
        return False

#: Landings under way in this process, so a second request for the same task
#: cannot start one beside it. A landing is not idempotent: both attempts would
#: be handed the same `land/<task-id>` checkout by worktrees.attach, and one
#: releasing it while the other rebases inside it destroys the work in flight.
_landing_now: set[str] = set()
_landing_guard = threading.Lock()

#: What a landing interrupted by a restart looks like on the record.
LANDING_UNDERWAY = "in-progress"


def start_landing(task_id: str) -> bool:
    """Run a landing off the calling thread, marking it as under way first.

    A landing rebases and runs the project's suite twice; the dashboard gives
    its call fifteen seconds. So the request cannot wait for it -- but nor can
    the record go quiet in the meantime, which is the whole failure being
    fixed, so the in-progress marker goes down before the thread starts.
    """
    task = task_store.get(task_id)
    if not task or not landing_enabled(task):
        return False
    with _landing_guard:
        # Two Approve clicks land in two server threads, and both read the
        # task's state before either writes it, so both believe they are the
        # one approving it. Only one may go on to touch git.
        if task_id in _landing_now:
            log.info("a landing for %s is already under way", task_id)
            return False
        _landing_now.add(task_id)
    record_landing(task, {"eligible": True, "landed": False,
                          "stage": LANDING_UNDERWAY,
                          "branch": branches.name_for(task),
                          "detail": "landing under way", "checkpointed": True})

    def run():
        try:
            channel, thread_ts = "", ""
            # Deliberately not task_thread(): a task with nowhere to talk should
            # not get an anchor message posted for it hours after it finished.
            if task.get("thread") and ":" in task["thread"]:
                channel, _, thread_ts = task["thread"].partition(":")
            land_and_record(task_id, channel, thread_ts)
        except Exception:
            # land_and_record guards itself, so this is the thread dying before
            # it gets there. Leaving the marker would say "landing…" for ever.
            log.exception("the landing thread for %s died", task_id)
            try:
                record_landing(task_store.get(task_id) or task,
                               {"eligible": True, "landed": False,
                                "stage": "errored", "branch": branches.name_for(task),
                                "detail": "the landing thread died; see the bot log"})
            except Exception:
                log.exception("could not record the death of %s's landing", task_id)
        finally:
            with _landing_guard:
                _landing_now.discard(task_id)

    threading.Thread(target=run, daemon=True, name=f"land-{task_id[:12]}").start()
    return True


def _interrupted_landing_detail(task: dict, landing: dict) -> str:
    """Say what a killed landing left behind, as far as can be known.

    "Nothing was merged" is only true up to the fast-forward. `merge.land`
    moves the base *before* its post-merge suite, and resets it only if that
    suite finishes and fails -- so a restart during that run leaves the base
    carrying commits nothing finished proving, with no reset ever coming.

    `merge.land` checkpoints the base's name and commit just before it
    fast-forwards, so the marker says which side of that line the restart
    fell on. Asking git alone cannot: an empty branch is already an ancestor
    of the base, and the checkout may no longer be parked on it.
    """
    prefix = "the bot restarted while it was landing; "
    unsure = (prefix + "the base may already have moved without the tests "
              "after the merge finishing -- check it before trusting it")
    if not landing.get("checkpointed"):
        return unsure                 # written by a bot that did not checkpoint
    before, base = landing.get("base_before"), landing.get("base")
    if not before:
        return prefix + "nothing was merged"   # killed before the fast-forward
    cwd = (task.get("scope") or {}).get("cwd")
    branch = landing.get("branch") or ""
    try:
        now = subprocess.run(["git", "rev-parse", "--verify", "-q",
                              f"refs/heads/{base}"], cwd=cwd, capture_output=True,
                             text=True, timeout=30).stdout.strip()
        def is_ancestor(a, b):
            return subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                                  cwd=cwd, capture_output=True, text=True,
                                  timeout=30).returncode == 0
        # The branch brought something `before` lacked, and the base has it
        # now. Either half alone is not enough: an empty branch is on any base
        # that moved for some other reason.
        has = (not is_ancestor(branch, before)
               and is_ancestor(branch, f"refs/heads/{base}"))
    except Exception:
        # Startup must not stall on this: a checkout that cannot be asked gets
        # the answer that names both possibilities.
        return unsure
    if now == before:
        # Either the fast-forward never ran, or the post-merge suite failed and
        # put the base back -- both leave nothing merged.
        return prefix + "nothing was merged"
    if now and has:
        return (prefix + f"{branch} was fast-forwarded onto {base} "
                f"(from {before[:8]}), but the tests after the merge were never "
                f"seen to pass -- check {base} before trusting it")
    return unsure


def clear_interrupted_landings() -> list[str]:
    """Rewrite landings a restart killed, so no row says "landing…" for ever.

    The landing runs on a daemon thread, which a restart ends without
    unwinding. The marker it left is durable and nothing else would ever
    revisit it, so the board would animate a merge that stopped happening days
    ago -- the same lie as silence, only moving. This is the task equivalent of
    `requeue_interrupted`, and like it, it runs before anything else can.
    """
    stale = []
    for tid, rec in task_store.all().items():
        landing = ((rec.get("result") or {}).get("landing")) or {}
        if landing.get("stage") != LANDING_UNDERWAY:
            continue
        try:
            record_landing(rec, {**landing, "stage": "interrupted",
                                 "detail": _interrupted_landing_detail(rec, landing)})
            stale.append(tid)
        except Exception:
            log.exception("could not clear the stale landing on %s", tid)
    if stale:
        log.info("cleared %d landing(s) a restart interrupted: %s",
                 len(stale), ", ".join(stale))
    return stale


_email_lock = threading.Lock()


def run_email_ingest() -> dict:
    """One Gmail pass. Off unless both credentials are set.

    Two halves, and they produce different things. Labelled mail is filed into
    the matching project as *facts* -- a booking is not an action item, and
    putting it on the board would mean clicking to dismiss something true.
    Inbox triage, which proposes tasks, is opt-in.
    """
    if not (GMAIL_USER and GMAIL_APP_PASSWORD):
        return {"ok": False, "error": "GMAIL_USER / GMAIL_APP_PASSWORD not set"}
    # Serialised like its harvest twin. The poller and the dashboard's
    # ingest-email action both land here, and two passes sharing one watermark
    # each read it, advance their own copy and write it back -- so the later
    # save drops whatever the other one marked seen, and that mail is proposed
    # a second time. The save itself is atomic; the read-modify-write is not.
    with _email_lock:
        # Non-strict: a watermark, so the worst case is re-reading some mail.
        state = jsonstore.load(EMAIL_STATE_FILE, default={}, strict=False) or {}
        common = dict(host=GMAIL_HOST, user=GMAIL_USER, password=GMAIL_APP_PASSWORD,
                      limit=GMAIL_MAX_PER_RUN, binary=CLAUDE_BIN,
                      model=NAMING_MODEL or "haiku", env=claude_env(),
                      cwd=str(CLAUDE_CWD))
        facts = email_ingest.ingest_facts(
            project_store, state.setdefault("labels", {}), **common)
        triaged = {}
        if GMAIL_TRIAGE:
            triaged = email_ingest.ingest(
                task_store, state, mailbox=GMAIL_MAILBOX, **common)
        jsonstore.save(EMAIL_STATE_FILE, state)
    return {"ok": True, **facts, "triaged": triaged}


def _email_watcher() -> None:
    if not (GMAIL_USER and GMAIL_APP_PASSWORD):
        log.info("gmail watching disabled (no GMAIL_USER / GMAIL_APP_PASSWORD)")
        return
    time.sleep(90)      # let the bot settle before reaching outside
    while True:
        try:
            run_email_ingest()
        except Exception:
            log.exception("gmail ingest failed")
        time.sleep(max(GMAIL_POLL_MIN, 5) * 60)


# --- is Slack actually connected? ---------------------------------------------

def _slack_repair(client) -> None:
    try:
        client.disconnect()
        client.connect()
    except Exception:
        log.exception("slack reconnect failed")
        return
    # Whatever arrived during the gap was never delivered; the socket does not
    # queue. Reconnecting is only half of coming back.
    try:
        run_backfill()
    except Exception:
        log.exception("backfill after reconnect failed")


def _slack_watchdog(handler) -> None:
    """Restart the process when the socket-mode link stays down.

    The process staying alive is not evidence that Slack can reach it: a
    websocket that breaks and re-breaks leaves a running bot nobody can talk
    to, which launchd's KeepAlive cannot see and the local HTTP server does not
    reflect. Interrupted turns are rescued by recovery.py on the way back up,
    so exiting is cheaper than the silence it replaces.
    """
    client = handler.client
    while True:
        time.sleep(slack_health.INTERVAL_S)
        try:
            action = slack.sample(bool(client.is_connected()), time.time())
        except Exception:
            log.exception("slack health check failed")
            continue
        if action == slack_health.REPAIR:
            # In its own thread: connect() can block, and a blocked repair must
            # not stall the sampler whose job is to escalate when it does not
            # take. That would reproduce the unbounded wait being fixed here.
            threading.Thread(target=_slack_repair, args=(client,), daemon=True,
                             name="slack-repair").start()
        elif action == slack_health.RESTART:
            os._exit(1)          # KeepAlive brings us back with a clean client


def run_backfill() -> dict:
    """Replay messages Slack could not deliver while we were disconnected.

    Socket Mode does not queue: a message sent while the link is down is gone,
    and nothing records that it existed. The Web API still has the thread, so
    read it back and hand anything past the watermark to the ordinary path,
    which already refuses redeliveries.
    """
    def replies(channel, thread_ts):
        r = app.client.conversations_replies(channel=channel, ts=thread_ts, limit=100)
        return r.get("messages", [])

    events = backfill.missed(store.all(), replies=replies,
                             bot_user_id=BOT_USER_ID,
                             handled_subtypes=HANDLED_SUBTYPES)
    for event in events:
        key = f"{event['channel']}:{event['thread_ts']}"
        log.info("backfilling missed message %s on %s", event["ts"], key)

        def say(text, thread_ts=event["thread_ts"], **kwargs):
            app.client.chat_postMessage(channel=event["channel"],
                                        thread_ts=thread_ts, text=text, **kwargs)
        try:
            handle_prompt(event, say, app.client)
        except Exception:
            log.exception("backfill failed for %s", key)
    if events:
        log.info("backfill: replayed %d missed message(s)", len(events))
    return {"ok": True, "replayed": len(events)}


def _backfiller() -> None:
    # After recovery, so a turn that was already in flight finishes delivering
    # before anything new is replayed into the same thread.
    time.sleep(20)
    try:
        run_backfill()
    except Exception:
        log.exception("backfill pass failed")


#: Remembers which expiry we have already warned about, so a new credential
#: re-arms the warning and an old one does not nag hourly.
_warned_expiry = [0.0]


def _credential_watcher() -> None:
    """Say something before the credentials die, not after.

    A restart cannot fix an expired token -- it is on disk, and restarting
    re-reads the same file -- so the only useful moment is beforehand. The
    refresh token's expiry does not roll forward when the access token is
    refreshed, which makes it a scheduled outage rather than a risk.
    """
    time.sleep(120)          # let the bot settle before it talks
    while True:
        try:
            st = credentials.state(has_token=bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")))
            msg = credentials.warning(st)
            expiry = st.get("expires_at", 0.0)
            if msg and expiry != _warned_expiry[0]:
                channel = home_channel()
                if channel:
                    app.client.chat_postMessage(channel=channel, text=msg)
                    _warned_expiry[0] = expiry
                    log.warning("credential warning posted: %s", st.get("mode"))
            elif not msg:
                _warned_expiry[0] = 0.0      # healthy again; re-arm
        except Exception:
            log.exception("credential check failed")
        time.sleep(3600)


def run_ideation(slug: str) -> dict:
    """File the nightly look at one project as a task the runner will pick up.

    A task rather than a direct run, so it queues behind whatever else is
    happening, survives a restart, and shows up in the record like any other
    work. The ideator role is read-only: it can propose, and nothing else.
    """
    rec = project_store.get(slug) or {}

    # A night that would only be refused at filing time is a session spent to
    # learn what the board already knew, so the pass does not start at all
    # while the project is at its standing limit. `ideate_on` is stamped
    # either way: the night has been dealt with, and re-deciding it every five
    # minutes until midnight would fill the log with the same answer.
    open_now = scoping.open_proposals(task_store.by_project(slug))
    if scoping.backlog_full(open_now):
        project_store.ensure(slug, ideate_on=datetime.now().strftime("%Y-%m-%d"))
        log.info("skipped nightly ideation for %s: %d proposals already waiting",
                 slug, open_now)
        return {"ok": True, "skipped": "backlog", "open": open_now,
                "limit": scoping.max_open_proposals()}

    scope = project_store.scope_for(slug) or {"cwd": str(CLAUDE_CWD)}
    goal = (
        f"Look over the {rec.get('title') or slug} project and propose work "
        "worth doing.\n\n"
        "Read what is actually there before suggesting anything: the code, "
        "recent commits, the tests, CLAUDE.md. Then file each proposal with\n"
        f"    {SILKWORM_BIN} task --propose --project {slug} \"<goal>\"\n"
        f"at most {scoping.MAX_PROPOSALS} of them, fewer if fewer are "
        "warranted, and none at all if the project is in good shape. Each goal "
        "must stand alone: whoever picks it up will not have read this.\n\n"
        "Then say what you looked at and what you filed."
    )
    board = task_store.by_project(slug)

    # Told nothing, a fresh session re-derives last night's ideas and files them
    # again -- correctly, since a real gap is still there tomorrow. So the goal
    # carries the project's own board: what is still open, and what was proposed
    # and turned down. Otherwise every night costs the same dismissals over.
    note = scoping.board_note(*scoping.already_filed(board, time.time()))
    if note:
        goal += "\n\n" + note

    # Told nothing about them, a fresh session reads the base, finds a gap that
    # is already fixed on an unmerged branch, and files it again -- which is how
    # one fix came to be implemented twice, at the cost of two sessions and two
    # reviewers each time. So the goal carries the list.
    note = scoping.unmerged_note(branches.survey(board))
    if note:
        goal += "\n\n" + note

    task = task_store.create(goal, title=f"Nightly review: {rec.get('title') or slug}",
                             role="ideator", project=slug, state=tasks.QUEUED,
                             # Unattended work of its own, though the ideator
                             # being read-only means it is denied a checkout
                             # anyway -- one empty branch a night otherwise.
                             driver="queue", source="ideation", isolate=True,
                             scope=scope)
    project_store.ensure(slug, ideate_on=datetime.now().strftime("%Y-%m-%d"))
    log.info("filed nightly ideation %s for %s", task["id"], slug)
    return {"ok": True, "id": task["id"]}


def _ideation_scheduler() -> None:
    """Fire each project's nightly look when its time comes round.

    Checked against the wall clock rather than slept precisely to, so a bot
    restarted at 02:00 still runs the pass when it comes back, and one that
    already ran today does not run twice.
    """
    time.sleep(180)
    while True:
        try:
            for slug in projects.due_for_ideation(
                    project_store.all(include_archived=False), datetime.now()):
                run_ideation(slug)
        except Exception:
            log.exception("ideation scheduler failed")
        time.sleep(300)


def _board_loop() -> None:
    """Keep the board message current. It redraws only when what it shows has
    changed (or its relative times have gone stale), so this is cheap."""
    while True:
        try:
            BOARD.sync(app.client)
        except Exception:
            log.exception("board sync failed")
        time.sleep(BOARD_POLL_S)


def _task_scheduler() -> None:
    """Requeue what a restart interrupted, then keep the board honest.

    Two jobs on one beat: retries whose time has come, and tasks waiting on a
    blocker that has already ended -- which are waiting for nothing, in a state
    that shows up nowhere.

    Deliberately one thread rather than one per worker: `blocked -> queued` is
    a state transition, and several workers racing to make the same one would
    have all but the first refused, filling the log with noise about nothing.
    """
    try:
        moved = task_store.requeue_interrupted()
        if moved:
            log.info("requeued %d task(s) interrupted by a restart", moved)
    except Exception:
        log.exception("closing out interrupted tasks failed")
    try:
        # A landing runs on a daemon thread, which a restart ends mid-merge. Its
        # marker is durable, so without this the board keeps saying "landing…"
        # about something that stopped days ago.
        clear_interrupted_landings()
    except Exception:
        log.exception("clearing interrupted landings failed")
    # After the requeue, not before: a reviewer the restart is about to put
    # back in the queue has not stranded its parent, and auditing first would
    # say it had.
    while True:
        try:
            for tid in task_store.due_retries(time.time()):
                task_state(tid, tasks.QUEUED, "retry time reached")
            # Anything waiting on a task that ended is waiting for nothing.
            # Released here as well as when the blocker ends, so a task
            # stranded by a crash in between still reaches the board.
            task_store.release_stranded()
        except Exception:
            log.exception("task scheduler iteration failed")
        time.sleep(TASK_POLL_S)


def _task_worker(n: int) -> None:
    """Claim and run queued work, alongside the other workers.

    Safe to run several of these because the pieces underneath were built for
    it: `claim` selects and transitions under one lock, so no two workers get
    the same task, and every queued task works in its own git worktree, so no
    two of them share a checkout.

    A task waiting on its reviewer does not hold a worker -- the review is
    enqueued as its own task and the implementor parks in `blocked` -- so
    workers cannot all end up waiting on each other.
    """
    while True:
        try:
            # Held while something global (quota, overload) is killing turns.
            # Claiming anyway fails the next task the same way in seconds --
            # a fresh worktree each, parked unrun -- until the queue is empty.
            held = RUNNER_HOLD.remaining()
            if held:
                time.sleep(min(held, TASK_POLL_S))
                continue
            task = task_store.claim()
            if task:
                execute_task(task)
                continue           # drain without waiting, unless that closed the hold
        except Exception:
            log.exception("task worker %d failed", n)
        time.sleep(TASK_POLL_S)


#: How often to re-check for orphaned turns after the startup pass.
RECOVERY_SWEEP_S = 120


def run_recovery(wait_s: int | None = None, skip=None) -> dict:
    """Finish turns no live handler is driving any more.

    Runs at startup and then periodically. The startup pass alone is not
    enough: a turn still running when it fires is left pending, and nothing
    later would ever deliver its reply -- so restarting during a long turn
    silently swallowed the answer.
    """
    def finalize(channel: str, thread_ts: str, ts: str, text: str) -> None:
        parts = chunk(to_mrkdwn(text))
        app.client.chat_update(channel=channel, ts=ts, text=parts[0])
        for part in parts[1:]:
            app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=part)

    def say(channel: str, thread_ts: str, text: str) -> None:
        for part in chunk(to_mrkdwn(text)):
            app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=part)

    def reactions_for(channel: str, thread_ts: str, msg_ts):
        return ThreadReactions(app.client, channel, thread_ts, msg_ts)

    def on_outcome(key: str, recovered: bool) -> None:
        # A rescued reply means the turn actually succeeded, so its task should
        # say so. Without this every restart would file a false failure into
        # the "needs you" list, which is the noise that kills the board.
        tid = inline_task_for(key)
        if tid:
            task_state(tid, tasks.DONE if recovered else tasks.FAILED,
                       "recovered after a restart" if recovered
                       else "interrupted, produced no reply")

    kwargs = {} if wait_s is None else {"wait_s": wait_s}
    return recovery.recover(store, finalize=finalize, reactions_for=reactions_for,
                            say=say, on_outcome=on_outcome, skip=skip, **kwargs)


def inline_task_for(key: str) -> str | None:
    """The in-flight Slack-driven task for a thread, if there is one."""
    for tid, rec in task_store.all().items():
        if (rec.get("thread") == key and rec.get("state") == tasks.RUNNING
                and rec.get("driver") == "inline"):
            return tid
    return None


def repair_thread_cwds() -> int:
    """Repoint threads whose working directory has gone away.

    A turn that ran in a worktree used to leave that path as the thread's home,
    and a released worktree then broke every later message in it -- permanently,
    with nothing connecting the failure to the task that caused it. That bug is
    fixed, but 24 threads were already carrying dead paths, and a directory can
    always be moved or deleted by hand.

    Prefers the project's own directory, then the repository the worktree came
    from, then the default. Only ever runs when the recorded path is gone, so a
    thread deliberately pointed somewhere unusual is left alone.
    """
    fixed = 0
    for key, entry in store.all().items():
        cwd = entry.get("cwd")
        if not cwd or Path(cwd).is_dir():
            continue
        target = ""
        scoped = project_store.scope_for(entry.get("project") or "")
        if scoped.get("cwd") and Path(scoped["cwd"]).is_dir():
            target = scoped["cwd"]
        elif Path(cwd).parent == worktrees.ROOT:
            # ".../<repo>--<task>", ".../<repo>--land--<task>", "--review--"
            repo = Path(cwd).name.split(worktrees.SEP, 1)[0]
            guess = Path.home() / "workspace" / repo
            if guess.is_dir():
                target = str(guess)
        if not target:
            target = str(CLAUDE_CWD)
        store.update(key, cwd=target)
        log.info("thread %s pointed at a missing %s; now %s", key, cwd, target)
        fixed += 1
    return fixed


def close_out_orphans() -> None:
    """Settle inline tasks no handler in this process will ever drive.

    An inline task is driven by the Slack handler that created it, and that
    handler died with the previous process. Runs *before* recovery rather than
    after: the startup pass can wait an hour on a live child, and these records
    would claim work was in flight the whole time.

    Anything recovery is about to resolve is left alone -- but only the one
    turn it is about to resolve. Skipping every task on a thread that has a
    pending marker left a task stuck in `running` for sixteen hours: the
    conversation carried on, so the thread always had a marker, and the marker
    belonged to a newer turn each time. The marker names the live turn by its
    message, so match on that instead of on the thread.
    """
    live = {k: (e.get("pending") or {}).get("msg_ts")
            for k, e in store.all().items() if e.get("pending")}
    for tid, rec in task_store.all().items():
        if rec.get("driver") != "inline":
            continue
        thread = rec.get("thread")
        if thread in live and rec.get("source_ref") == live[thread]:
            continue                    # this is the turn recovery will finish
        if rec.get("state") == tasks.RUNNING:
            task_state(tid, tasks.FAILED, "interrupted by a restart")
        elif rec.get("state") == tasks.QUEUED:
            # Never started, and its handler is gone -- but the work is still
            # wanted, so hand it to the queue runner rather than dropping it.
            #
            # Cancelling it and trusting the backfill was wrong. The backfill
            # keys on a single high-water mark, and messages are not processed
            # in arrival order: one that queued behind another, then died in a
            # restart, is invisible the moment any later message has run. That
            # silently lost real messages -- exactly the "3 deep and some go
            # missing" symptom. The record already holds the goal, thread and
            # project, so nothing needs to be recovered from Slack at all.
            #
            # Only *who* runs it changes. Where it runs is on the record
            # (`isolate`), so this cannot move a conversation about your
            # uncommitted edits into a worktree that does not have them.
            task_store.update(tid, driver="queue")
            log.info("task %s handed to the runner (its Slack handler is gone)", tid)


def _recoverer() -> None:
    try:
        n = repair_thread_cwds()
        if n:
            log.info("repointed %d thread(s) whose directory had gone", n)
    except Exception:
        log.exception("repairing thread working directories failed")
    try:
        close_out_orphans()
    except Exception:
        log.exception("closing out orphaned inline tasks failed")
    try:
        run_recovery()
    except Exception:
        log.exception("startup recovery failed")


def live_worktree_tasks() -> set:
    """Task ids whose isolated checkout must survive this sweep.

    Not just `running`. A task leaves that state the moment anything happens to
    it -- a cancel, a failure, a review gate -- while its checkout may still be
    the only place its work exists, and the child may still be in it. Keeping
    only `running` meant a cancel handed the very next sweep a live worktree to
    tidy away; a task that had just committed and was running its tests looked
    clean for minutes at a time, so the dirty check saved nothing.

    So: anything with a child of ours in it, plus anything that has not reached
    a terminal state. A finished or cancelled task has had its checkout
    released the ordinary way, and one still on disk for it really is litter.
    """
    live = ({tid for tid, r in task_store.all().items()
             if r.get("state") not in tasks.TERMINAL}
            | set(RUNNING_TASKS))
    # And a review's checkout is keyed by the task it is reviewing, not by
    # itself, because the work in it is that task's. Cancelling a parent
    # mid-review is legal and drops it from the set above -- which would hand
    # the sweep the directory its reviewer is currently standing in.
    live |= {(task_store.get(t) or {}).get("parent") or "" for t in RUNNING_TASKS}
    return live - {""}


def _worktree_sweeper() -> None:
    """Remove isolated checkouts no live task owns.

    A restart orphans whatever was running, and an orphaned worktree is
    invisible: it costs disk and clutters `git worktree list` while looking
    like nothing at all. Dirty ones are always kept -- unfinished work is
    still work, and it is reported rather than tidied away.

    What is left to sweep, once live_worktree_tasks() has had its say, is
    checkouts belonging to tasks that are finished or that no longer exist at
    all. A restart-orphaned one is requeued rather than swept, and the next
    attempt picks the same checkout back up -- which is the better outcome
    anyway, since the work is already in it.
    """
    while True:
        try:
            n = worktrees.sweep(keep=live_worktree_tasks())
            if n:
                log.info("swept %d orphaned worktree(s)", n)
        except Exception:
            log.exception("worktree sweep failed")
        # Its own try: a repository git chokes on must not stop the worktree
        # sweep above from running next time, or the other way round.
        #
        # Under the landing guard, so no landing can start part-way through,
        # and skipping every one already running: Approve lands a task that is
        # already `done`, and its branch is not litter until that finishes.
        try:
            with _landing_guard:
                records = task_store.all().values()
                busy = set(_landing_now) | {
                    r.get("id") for r in records
                    if ((r.get("result") or {}).get("landing") or {}).get("stage")
                    == LANDING_UNDERWAY}
                branches.prune_merged(records, skip=busy)
        except Exception:
            log.exception("merged-branch prune failed")
        time.sleep(1800)


def _recovery_sweeper() -> None:
    """Collect orphaned turns as their children finish, for as long as we run.

    Its own thread, not a tail on the startup pass: that pass waits up to an
    hour on a live child, and a sweep sitting behind it would not engage until
    the very outage it exists to shorten was over. Running alongside is safe
    because recovery claims a thread before resolving it, so the two passes
    cannot both deliver the same reply.

    wait_s=0 collects only children that have already exited. Turns this
    process is running are skipped outright: their marker belongs to a live
    handler that will clear it.
    """
    while True:
        time.sleep(RECOVERY_SWEEP_S)
        try:
            stats = run_recovery(wait_s=0, skip=set(RUNNING))
            if stats.get("recovered") or stats.get("interrupted"):
                log.info("recovery sweep: %s", stats)
        except Exception:
            log.exception("recovery sweep failed")


def reap_runaways(max_age_s: float) -> int:
    """Kill claude children that have outlived any plausible turn.

    A turn's deadline lives in a threading.Timer inside this process, so a
    child orphaned by a restart has no deadline at all and can run forever --
    holding its thread's pending marker open and its lock unreleasable. Turns
    this process is actually running are left alone; they have their timer.
    """
    killed = 0
    for key, entry in store.all().items():
        pending = entry.get("pending")
        if not pending or key in RUNNING:
            continue
        started = pending.get("started", "")
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds()
        except (TypeError, ValueError):
            continue
        if age < max_age_s:
            continue
        sid = pending.get("session_id") or entry.get("session_id") or ""
        pids = procs.session_pids(sid)
        if not pids:
            continue
        log.warning("reaping runaway turn on %s (session=%s, running %.1fh)",
                    key, sid[:8], age / 3600)
        store.add_event(key, "reaped", f"runaway turn killed after {age / 3600:.1f}h")
        for pid in pids:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except OSError:
                pass
        time.sleep(5)
        for pid in pids:
            if _alive(pid):
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except OSError:
                    pass
        killed += 1
    return killed


def _watchdog() -> None:
    # Generous: a legitimate long turn must never be mistaken for a runaway.
    # Keyed off the idle limit rather than the absolute cap, which is now
    # normally 0 -- deriving a bound from that would have made it a flat hour
    # and started reaping orphaned children in the middle of real work.
    bound = max(CLAUDE_IDLE_TIMEOUT * 2, CLAUDE_TIMEOUT * 2, 3600)
    while True:
        time.sleep(300)
        try:
            reap_runaways(bound)
        except Exception:
            log.exception("watchdog sweep failed")


def _sweep_pass() -> None:
    """Six-hourly tidy: empty session husks, finished tasks, yesterday's outboxes.

    Threads are retired by hiding them, not by being deleted out from under
    you -- so the first step only removes records with nothing in them and no
    task pointing at them. See SessionStore.forget_empty.

    Every step is guarded, and separately. Only the compaction used to be, and
    the other two raise for real: the session step ends in a jsonstore save
    that a full or read-only disk fails, and `iterdir()` then `stat()` races
    the outbox directories turns create while this walks them. Either one
    ended the thread -- the only caller of forget_empty() and
    compact_older_than() -- so tasks.json would have grown forever with nothing
    in the log to say it had stopped being tidied. Separately rather than as
    one block because these are unrelated jobs: a sessions.json that cannot be
    written must not be the reason tasks.json never compacts again.
    """
    try:
        referenced = {r.get("thread") for r in task_store.all().values() if r.get("thread")}
        gone = store.forget_empty(SESSION_MAX_AGE_DAYS, keep=referenced)
        if gone:
            log.info("forgot %d empty session record(s) older than %sd: %s",
                     len(gone), SESSION_MAX_AGE_DAYS, ", ".join(gone))
    except Exception:
        log.exception("forgetting empty sessions failed")
    try:
        task_store.compact_older_than(TASK_COMPACT_AFTER_DAYS)
    except Exception:
        log.exception("compacting finished tasks failed")
    try:
        for orphan in OUTBOX_ROOT.iterdir():
            try:
                stale = orphan.is_dir() and orphan.stat().st_mtime < time.time() - 86400
            except OSError:
                continue          # created or removed under us mid-walk; not ours
            if stale:
                shutil.rmtree(orphan, ignore_errors=True)
    except Exception:
        log.exception("sweeping old outboxes failed")


def _sweeper() -> None:
    while True:
        try:
            _sweep_pass()
        except Exception:
            # Nothing above should reach here; this is so that whatever is
            # added to the pass next still costs one round rather than the
            # thread, the way every sibling loop in this file is written.
            log.exception("sweep pass failed")
        time.sleep(6 * 3600)


def _harvester() -> None:
    if HARVEST_INTERVAL_H <= 0:
        return
    time.sleep(120)  # let the bot settle after boot before the first pass
    while True:
        try:
            run_harvest()
        except Exception:
            log.exception("scheduled harvest failed")
        time.sleep(HARVEST_INTERVAL_H * 3600)


if __name__ == "__main__":
    reconcile_checkouts()
    # Every long-lived loop is started through daemons.start, so /status can
    # tell when one has stopped. forever=False marks a startup pass that is
    # meant to finish; a feature switched off in .env returns at once on
    # purpose, so its loop is only expected to persist when it is switched on.
    # Background: an orphaned turn may still be writing, so this waits on it.
    daemons.start(_recoverer, "recoverer", forever=False)
    daemons.start(_recovery_sweeper, "rsweep")
    daemons.start(_worktree_sweeper, "wtsweep")
    daemons.start(_sweeper, "sweeper")
    daemons.start(_watchdog, "watchdog")
    daemons.start(_backfiller, "backfill", forever=False)
    daemons.start(_task_scheduler, "tsched")
    daemons.start(_board_loop, "board")
    for _i in range(max(1, TASK_WORKERS)):
        daemons.start(_task_worker, f"task{_i}", args=(_i,))
    daemons.start(_email_watcher, "email", forever=bool(GMAIL_USER and GMAIL_APP_PASSWORD))
    daemons.start(_credential_watcher, "creds")
    daemons.start(_ideation_scheduler, "ideate")
    daemons.start(_harvester, "harvester", forever=HARVEST_INTERVAL_H > 0)
    log.info("workspace=%s approval_mode=%s allowlist=%s channel_dirs=%d",
             CLAUDE_CWD, CLAUDE_APPROVAL_MODE,
             ",".join(ALLOWED_USERS) or "(everyone)", len(CHANNEL_DIRS))
    # Logged because "is the new limit actually in effect" is otherwise only
    # answerable by reading .env and trusting that the service was restarted.
    log.info("turn limits: cap=%s idle=%ds · task workers: %d",
             f"{CLAUDE_TIMEOUT}s" if CLAUDE_TIMEOUT else "none",
             CLAUDE_IDLE_TIMEOUT, TASK_WORKERS)
    # Same reason as the line above, one level down: a landed fix is not a
    # running fix, and this is the only place the answer is written down at the
    # moment it is still true. Everything else asks git, which by then is
    # describing the checkout rather than the process.
    log.info("revision: %s%s on %s%s", REVISION["sha"][:8] or "unknown",
             " (dirty)" if REVISION["dirty"] else "",
             REVISION["branch"] or "detached HEAD", f" · {BASE_DIR}")
    slack_handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    daemons.start(_slack_watchdog, "slack-health", args=(slack_handler,))
    slack_handler.start()

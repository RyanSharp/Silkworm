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
import credentials
import defer
import email_ingest
import harvester
import learnings_git
import repos
import roles
import scoping
import slack_health
import tasks
import procs
import projects
import recovery
import retry
import summaries
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

SESSION_MAX_AGE_DAYS = float(os.environ.get("SESSION_MAX_AGE_DAYS", "30"))
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

    Taken inside the thread lock at both call sites, so the ordering is
    consistent and cannot deadlock.
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


def claude_env(key: str = "", defers: int = 0) -> dict:
    env = dict(os.environ)
    env["SILKWORM_BOT"] = "1"  # lets the global session_hook ignore our own runs
    # So a turn can schedule a wake-up against its own thread rather than
    # holding itself open until whatever it is watching finishes.
    env["SILKWORM_PORT"] = str(APPROVAL_PORT)
    if key:
        env["SILKWORM_THREAD"] = key
    env["SILKWORM_DEFERS"] = str(defers or 0)
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


def fail_or_retry(task_id: str | None, error: str) -> bool:
    """Park a transient failure for a later retry instead of asking for help.

    Returns True if it was parked. Quota exhaustion and API overload are not
    failures a person can do anything about, so surfacing them would just
    train you to ignore the list.
    """
    if not task_id:
        return False
    task = task_store.get(task_id) or {}
    plan = retry.retry_at(error, task.get("attempts") or 0)
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
                link = f"<https://slack.com/archives/{ch}/p{ts.replace('.', '')}|{name}>"
                lines.append(f"• {link} — {v.get('turns', 0)} turns, ${v.get('cost', 0):.2f}, {fmt_age(v.get('updated', 0))}")
            say(text=f"*Active sessions ({len(entries)}):*\n" + "\n".join(lines), thread_ts=thread_ts)
    else:
        return False
    return True


# --- Main handler ---------------------------------------------------------------

app = App(token=os.environ["SLACK_BOT_TOKEN"])
BOT_USER_ID = app.client.auth_test()["user_id"]

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
    return {"online": True, "threads": threads,
            "slack": slack.status(time.time()), "auth": auth}


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
            "link": f"https://slack.com/archives/{channel}/p{ts.replace('.', '')}"}


HARVEST_STATE = BASE_DIR / "harvest_state.json"
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
    if action == "ingest-email":
        try:
            return run_email_ingest()
        except Exception as e:
            log.exception("manual email ingest failed")
            return {"ok": False, "error": str(e)}
    if action == "create":
        try:
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
                                  role=payload.get("role", "assistant"),
                                  project=proj,
                                  source=payload.get("source", "ui"),
                                  state=payload.get("state", tasks.QUEUED),
                                  # Nobody is holding a live message for these,
                                  # so the runner is what will execute them.
                                  driver=payload.get("driver", "queue"),
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
            task_store.update(tid, goal=f"{task.get('goal', '')}\n\n{addendum}",
                              driver="queue")
            return {"ok": True, "task": task_store.transition(
                tid, tasks.QUEUED, "sent back for rework")}
        except tasks.InvalidTransition as e:
            return {"ok": False, "error": f"not allowed: {e}"}
    if action in ("accept", "approve", "dismiss", "retry", "cancel"):
        target = {"accept": tasks.QUEUED, "retry": tasks.QUEUED,
                  "approve": tasks.DONE,
                  "dismiss": tasks.CANCELLED, "cancel": tasks.CANCELLED}[action]
        try:
            return {"ok": True, "task": task_store.transition(
                payload.get("id", ""), target, f"{action} via {payload.get('by', 'ui')}")}
        except tasks.InvalidTransition as e:
            return {"ok": False, "error": f"not allowed: {e}"}
        except KeyError:
            return {"ok": False, "error": "unknown task"}
    return {"ok": False, "error": f"unknown action {action!r}"}


def handle_projects(payload: dict) -> dict:
    """Route for /projects — list, create and archive (localhost-trusted)."""
    action = payload.get("action", "list")
    if action == "list":
        return {"ok": True, "projects": projects.summarise(
            project_store.all(include_archived=bool(payload.get("archived"))),
            list(task_store.all().values()), tasks.NEEDS_ATTENTION)}
    if action == "ensure":
        name = (payload.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "a project needs a name"}
        scope = payload.get("scope")
        return {"ok": True, "project": project_store.ensure(
            name, **({"scope": scope} if scope else {}))}
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
        scope={"cwd": entry.get("cwd") or str(CLAUDE_CWD)},
        retry_at=when, defers=depth,
    )
    log.info("scheduled wake-up %s on %s in %.0fs (depth %d)",
             task["id"], key, delay, depth)
    return {"ok": True, "id": task["id"], "at": when, "in_s": round(delay),
            "depth": depth, "remaining": defer.MAX_DEFERS - depth}


#: Filed per turn, so a plan that loses count cannot become fifty sessions.
_filed_this_turn: dict = {}


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

    entry = store.get(key) or {}
    proj = (payload.get("project") or entry.get("project") or "").strip()
    if proj:
        proj = project_store.ensure(proj)["slug"]
        project_store.home(proj, create=True)

    err = scoping.validate(goal, _filed_this_turn.get(key, 0))
    if err:
        return {"ok": False, "error": err}

    role = payload.get("role") or "implementor"
    if role not in roles.ROLES or role in ("reviewer", "ideator"):
        return {"ok": False, "error": f"unknown role {role!r}"}

    # A proposal is not work yet. Nothing ran it past you, so it waits in
    # `proposed` until you accept it -- which is the whole point of a job that
    # thinks about your projects while you are asleep.
    propose = bool(payload.get("propose"))
    state = tasks.PROPOSED if propose else tasks.QUEUED

    scope = project_store.scope_for(proj) or {"cwd": entry.get("cwd") or str(CLAUDE_CWD)}
    task = task_store.create(
        goal, title=goal[:70], role=role, project=proj,
        # Work scoped with you is queued: it was already reviewed by the person
        # the proposed gate exists to ask. A proposal nobody has seen is not.
        state=state, driver="queue",
        source="ideation" if propose else "scoped",
        scope=scope,
    )
    _filed_this_turn[key] = _filed_this_turn.get(key, 0) + 1
    log.info("filed task %s from %s (project=%s role=%s)", task["id"], key, proj or "-", role)
    return {"ok": True, "id": task["id"], "project": proj, "state": state,
            "role": role, "cwd": scope.get("cwd", ""),
            "remaining": scoping.MAX_PER_TURN - _filed_this_turn[key]}


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
        scope={"cwd": str(cwd), "repo": repos.identity(str(cwd))},
    )
    task_id = task["id"]

    progress = ProgressMessage(client, channel, thread_ts)
    reactions = ThreadReactions(client, channel, thread_ts,
                                None if event.get("_web") else msg_ts)
    reactions.working()
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
            _filed_this_turn.pop(key, None)   # a fresh budget for this turn
            task_state(task_id, tasks.RUNNING)
            if not event.get("_web"):  # web prompts have a synthetic ts
                store.update(key, last_msg_ts=msg_ts)

            def on_init(sid: str) -> None:
                ACTIVE_SESSIONS[sid] = (channel, thread_ts)
                # A new session has no id until now; recovery needs it to find
                # the transcript if this process dies mid-turn.
                recovery.note_session(store, key, sid)

            def on_activity(name: str, tool_input: dict) -> None:
                progress.update(f":hourglass_flowing_sand: `{name}` {describe_tool(name, tool_input)[:120]}")

            def on_start(handle) -> None:
                RUNNING[key] = handle

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
        if not fail_or_retry(task_id, str(e)):
            task_state(task_id, tasks.FAILED, str(e)[:160])
    except ClaudeError as e:
        progress.finalize(f":warning: {e}")
        reactions.failed()
        store.add_event(key, "error", str(e)[:160])
        if not fail_or_retry(task_id, str(e)):
            task_state(task_id, tasks.FAILED, str(e)[:160])
    except Exception:
        log.exception("unhandled error in thread %s", key)
        progress.finalize(":warning: Something went wrong — check the bot logs.")
        reactions.failed()
        task_state(task_id, tasks.FAILED, "unhandled error")
    finally:
        RUNNING.pop(key, None)
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


def execute_task(task: dict) -> None:
    """Run one claimed task. Already in `running` — the claim did that."""
    tid = task["id"]
    role_name = task.get("role") or "assistant"
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

    # A queued task in a repository gets its own checkout. It cannot then leave
    # your working tree dirty or on another branch, and it no longer queues
    # behind a conversation about the same repo. Only queued work: a worktree
    # cannot see uncommitted changes in your main tree, so a conversation about
    # what you are editing right now must stay where you are editing it.
    worktree = None
    # A read-only role has nothing to isolate: it cannot write, so a worktree
    # buys nothing and leaves an empty branch behind every run -- one per
    # project per night once ideation is scheduled.
    if (task.get("driver") == "queue" and worktrees.is_repo(cwd)
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

    lock = _thread_lock(key)
    try:
        with lock, repo_guard(cwd, progress):
            entry = store.get(key) or {}
            # A fresh role starts its own session; resuming the thread's would
            # hand the reviewer the very conversation it is meant to audit.
            session_id = None if fresh else (task.get("session_id") or entry.get("session_id"))
            recovery.mark_pending(store, key, msg_ts=None, progress_ts=progress.ts,
                                  session_id=session_id, prompt=task.get("goal", ""))
            outbox.mkdir(parents=True, exist_ok=True)
            try:
                result = run_turn(
                    task.get("goal", ""), session_id=session_id,
                    binary=CLAUDE_BIN, cwd=cwd,
                    permission_args=roles.permission_args(role_name, permission_args(),
                                                        bin=SILKWORM_BIN),
                    model=entry.get("model") or CLAUDE_MODEL,
                    append_system_prompt=system_note, extra_args=CLAUDE_EXTRA_ARGS,
                    env=claude_env(key, task.get("defers") or 0),
                    timeout=CLAUDE_TIMEOUT, idle_timeout=CLAUDE_IDLE_TIMEOUT,
                    on_init=lambda sid: task_store.update(tid, session_id=sid),
                    on_activity=lambda n, i: progress.update(
                        f":hourglass_flowing_sand: `{n}` {describe_tool(n, i)[:120]}"),
                    on_start=lambda h: RUNNING.__setitem__(key, h),
                )
                # A fresh run must not repoint the thread at its throwaway
                # session, or the next Slack message resumes the review.
                if not fresh:
                    store.update(key, session_id=result.session_id, cwd=str(cwd))
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

        wt_note = ""
        if worktree:
            removed, note = worktrees.release(worktree)
            worktree = None                      # released; finally need not repeat it
            if note:
                wt_note = (f"\n\n_:deciduous_tree: Worked in an isolated checkout — {note}._"
                           if removed else
                           f"\n\n_:deciduous_tree: Isolated checkout {note}._")

        total = (store.get(key) or {}).get("cost", 0.0)
        parts = chunk(to_mrkdwn(result.text + wt_note))
        parts[-1] += (f"\n\n_:stopwatch: {fmt_duration(result.duration_ms)} · "
                      f"${result.cost_usd:.4f} · thread total ${total:.2f}_")
        progress.finalize(parts[0])
        for part in parts[1:]:
            app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=part)
        task_store.update(tid, session_id=result.session_id,
                          result={"text": result.text[:4000], "cost": result.cost_usd,
                                  "files_uploaded": uploaded})
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
        if not fail_or_retry(tid, str(e)):
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
                worktrees.release(worktree)
            except Exception:
                log.exception("could not release worktree %s", worktree)
        RUNNING.pop(key, None)
        recovery.clear_pending(store, key)


def resolve_review(task: dict, role_name: str, text: str,
                   channel: str, thread_ts: str) -> bool:
    """Apply the review gate. Returns True if the task's fate is already settled.

    An implementor does not finish on its own say-so: its output goes to a
    reviewer with fresh context, and the task waits. The reviewer's verdict
    then either completes it silently or puts it in front of the user with
    specific findings — which is the whole point, spending tokens so that only
    flagged work costs attention.
    """
    tid = task["id"]
    if roles.needs_review(role_name) and not task.get("blocked_on"):
        child = task_store.create(
            roles.review_goal(task, text), role="reviewer", driver="queue",
            # Without an explicit title it would be the review prompt's first
            # line ("Goal that was given:"), which reads as nonsense in a list.
            title=f"Review: {(task.get('title') or tid)[:46]}",
            source="review", source_ref=tid, parent=tid,
            root=task.get("root") or tid, thread=f"{channel}:{thread_ts}",
            scope=task.get("scope") or {})
        task_store.update(tid, blocked_on=[child["id"]])
        task_state(tid, tasks.BLOCKED, f"awaiting review {child['id']}")
        return True

    parent_id = task.get("parent")
    if role_name != "reviewer" or not parent_id:
        return False
    verdict = roles.parse_verdict(text)
    note = (":white_check_mark: *Review passed* — " if verdict["ok"]
            else ":mag: *Review flagged this* — ") + (verdict["summary"] or "")
    if verdict["findings"]:
        note += "\n" + "\n".join(f"• {f}" for f in verdict["findings"])
    try:
        app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=note)
    except Exception:
        log.exception("posting the review verdict failed")
    parent = task_store.get(parent_id)
    if parent:
        task_store.update(parent_id, result={**(parent.get("result") or {}),
                                             "review": verdict})
        task_state(parent_id, tasks.DONE if verdict["ok"] else tasks.AWAITING_APPROVAL,
                   verdict["summary"][:160])
    return False


def run_email_ingest() -> dict:
    """One Gmail pass. Off unless both credentials are set.

    Two halves, and they produce different things. Labelled mail is filed into
    the matching project as *facts* -- a booking is not an action item, and
    putting it on the board would mean clicking to dismiss something true.
    Inbox triage, which proposes tasks, is opt-in.
    """
    if not (GMAIL_USER and GMAIL_APP_PASSWORD):
        return {"ok": False, "error": "GMAIL_USER / GMAIL_APP_PASSWORD not set"}
    try:
        state = json.loads(EMAIL_STATE_FILE.read_text()) if EMAIL_STATE_FILE.exists() else {}
    except (OSError, json.JSONDecodeError):
        state = {}
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
    EMAIL_STATE_FILE.write_text(json.dumps(state, indent=2))
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
    scope = project_store.scope_for(slug) or {"cwd": str(CLAUDE_CWD)}
    # Told nothing, a fresh session re-derives last night's ideas and files them
    # again -- correctly, since a real gap is still there tomorrow. So it is
    # told: here is what is already open, and here is what you already said no
    # to. Otherwise every night costs the user the same dismissals.
    open_names, dismissed = scoping.already_filed(
        task_store.by_project(slug), time.time())
    goal = scoping.ideation_goal(slug, rec.get("title") or slug, SILKWORM_BIN,
                                 open_names, dismissed)
    task = task_store.create(goal, title=f"Nightly review: {rec.get('title') or slug}",
                             role="ideator", project=slug, state=tasks.QUEUED,
                             driver="queue", source="ideation", scope=scope)
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


def _task_scheduler() -> None:
    """Requeue what a restart interrupted, then keep due retries moving.

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
    while True:
        try:
            for tid in task_store.due_retries(time.time()):
                task_state(tid, tasks.QUEUED, "retry time reached")
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
            task = task_store.claim()
            if task:
                execute_task(task)
                continue           # drain without waiting
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
            task_store.update(tid, driver="queue")
            log.info("task %s handed to the runner (its Slack handler is gone)", tid)


def _recoverer() -> None:
    try:
        close_out_orphans()
    except Exception:
        log.exception("closing out orphaned inline tasks failed")
    try:
        run_recovery()
    except Exception:
        log.exception("startup recovery failed")


def _worktree_sweeper() -> None:
    """Remove isolated checkouts no live task owns.

    A restart orphans whatever was running, and an orphaned worktree is
    invisible: it costs disk and clutters `git worktree list` while looking
    like nothing at all. Dirty ones are always kept -- unfinished work is
    still work, and it is reported rather than tidied away.
    """
    while True:
        try:
            live = {tid for tid, r in task_store.all().items()
                    if r.get("state") == tasks.RUNNING}
            n = worktrees.sweep(keep=live)
            if n:
                log.info("swept %d orphaned worktree(s)", n)
        except Exception:
            log.exception("worktree sweep failed")
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


def _sweeper() -> None:
    while True:
        removed = store.sweep(SESSION_MAX_AGE_DAYS)
        if removed:
            log.info("swept %d stale session(s) older than %sd", removed, SESSION_MAX_AGE_DAYS)
        for orphan in OUTBOX_ROOT.iterdir():
            if orphan.is_dir() and orphan.stat().st_mtime < time.time() - 86400:
                shutil.rmtree(orphan, ignore_errors=True)
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
    # Background: an orphaned turn may still be writing, so this waits on it.
    threading.Thread(target=_recoverer, daemon=True, name="recoverer").start()
    threading.Thread(target=_recovery_sweeper, daemon=True, name="rsweep").start()
    threading.Thread(target=_worktree_sweeper, daemon=True, name="wtsweep").start()
    threading.Thread(target=_sweeper, daemon=True, name="sweeper").start()
    threading.Thread(target=_watchdog, daemon=True, name="watchdog").start()
    threading.Thread(target=_backfiller, daemon=True, name="backfill").start()
    threading.Thread(target=_task_scheduler, daemon=True, name="tsched").start()
    for _i in range(max(1, TASK_WORKERS)):
        threading.Thread(target=_task_worker, args=(_i,), daemon=True,
                         name=f"task{_i}").start()
    threading.Thread(target=_email_watcher, daemon=True, name="email").start()
    threading.Thread(target=_credential_watcher, daemon=True, name="creds").start()
    threading.Thread(target=_ideation_scheduler, daemon=True, name="ideate").start()
    threading.Thread(target=_harvester, daemon=True, name="harvester").start()
    log.info("workspace=%s approval_mode=%s allowlist=%s channel_dirs=%d",
             CLAUDE_CWD, CLAUDE_APPROVAL_MODE,
             ",".join(ALLOWED_USERS) or "(everyone)", len(CHANNEL_DIRS))
    # Logged because "is the new limit actually in effect" is otherwise only
    # answerable by reading .env and trusting that the service was restarted.
    log.info("turn limits: cap=%s idle=%ds · task workers: %d",
             f"{CLAUDE_TIMEOUT}s" if CLAUDE_TIMEOUT else "none",
             CLAUDE_IDLE_TIMEOUT, TASK_WORKERS)
    slack_handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    threading.Thread(target=_slack_watchdog, args=(slack_handler,),
                     daemon=True, name="slack-health").start()
    slack_handler.start()

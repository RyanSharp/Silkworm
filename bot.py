"""Slack bot that gives every thread its own Claude Code session.

DMs and @-mentions get replies in a thread; each thread maps to one headless
Claude session (resumed on every message). Features: live progress updates,
cost footers, per-thread model switching, !stop, file exchange, thread-context
bootstrap, Slack approval buttons, per-channel working dirs, session hygiene,
and a user allowlist.
"""

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
import urllib.request
import uuid
from collections import OrderedDict
from pathlib import Path

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from approvals import ApprovalManager, describe_tool
from claude_runner import ClaudeError, ClaudeStopped, run_turn
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
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "900"))
NAMING_MODEL = os.environ.get("NAMING_MODEL", "haiku")  # empty string disables

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
HOOK_PATH = BASE_DIR / "approval_hook.py"

CLAUDE_CWD.mkdir(parents=True, exist_ok=True)
OUTBOX_ROOT.mkdir(exist_ok=True)
ARTIFACTS_ROOT.mkdir(exist_ok=True)

# --- Shared state -----------------------------------------------------------
store = SessionStore(BASE_DIR / "sessions.json")
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


def _dedup(channel: str, ts: str) -> bool:
    """True if this event was already handled (mention + DM double-delivery)."""
    key = f"{channel}:{ts}"
    if key in _seen_events:
        return True
    _seen_events[key] = None
    while len(_seen_events) > 500:
        _seen_events.popitem(last=False)
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


def claude_env() -> dict:
    env = dict(os.environ)
    env["SILKWORM_BOT"] = "1"  # lets the global session_hook ignore our own runs
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


# --- Terminal handoff ---------------------------------------------------------

def kill_terminal(session_id: str) -> bool:
    """Force-close an interactive `claude --resume <session_id>` process.

    Completed turns are already persisted in the session log, so this only
    loses an in-flight generation. Returns True if a process was killed.
    """
    if not session_id:
        return False
    out = subprocess.run(["pgrep", "-f", f"claude.*{session_id}"],
                         capture_output=True, text=True)
    pids = [int(p) for p in out.stdout.split() if p.strip().isdigit()]
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
• `!terminal` — check this thread out to your terminal (Slack messages held until you're done)
• `!back` — reclaim a checked-out thread for Slack
• `!takeover` — force-close the live terminal session and reclaim the thread
• `!stats` — this thread's session info (model, turns, cost)
• `!sessions` — list all active thread sessions
• `!help` — this message
Attach files to a message and Claude can read them; files Claude produces get uploaded back here."""


def handle_command(cmd: str, key: str, say, thread_ts: str) -> bool:
    """Returns True if the message was a command (already handled)."""
    lower = cmd.lower()
    if lower in ("!help", "!commands"):
        say(text=HELP, thread_ts=thread_ts)
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
    return {"online": True, "threads": threads}


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


server = LocalServer(APPROVAL_PORT)
server.route("/session-event", handle_session_event)
server.route("/status", handle_status)
server.route("/web-message", handle_web_message)
server.route("/register-terminal", handle_register_terminal)

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
    if ALLOWED_USERS and not event.get("_web") and user not in ALLOWED_USERS:
        say(text="Sorry, you're not on this bot's allowlist.", thread_ts=thread_ts)
        return

    text = MENTION_RE.sub("", event.get("text", "")).strip()
    files = event.get("files") or []

    if text.startswith("!") and handle_command(text, key, say, thread_ts):
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
        saved = download_attachments(files, cwd / "slack-uploads", key)
        if saved:
            listing = "\n".join(f"- {p}" for p in saved)
            prompt += f"\n\n[The user attached file(s), saved locally at:\n{listing}]"

    # Stable per-thread outbox path: this string ends up in --append-system-prompt,
    # and prompt caching is a byte-exact prefix match — a path that changes every
    # message invalidates the cache and re-bills the whole history at full price.
    outbox = OUTBOX_ROOT / key.replace(":", "__")
    system_note = (
        "You are replying inside Slack; keep responses conversational. "
        f"If you create a file the user should receive, copy it into {outbox} "
        "and it will be uploaded to the Slack thread automatically."
    )

    progress = ProgressMessage(client, channel, thread_ts)
    lock = _thread_lock(key)
    if lock.locked():
        progress.update(":hourglass_flowing_sand: _Queued behind an earlier message in this thread…_")

    try:
        with lock:
            entry = store.get(key) or {}
            session_id = entry.get("session_id")
            log.info("thread=%s session=%s cwd=%s prompt=%r", key, session_id or "NEW", cwd, text[:120])

            def on_init(sid: str) -> None:
                ACTIVE_SESSIONS[sid] = (channel, thread_ts)

            def on_activity(name: str, tool_input: dict) -> None:
                progress.update(f":hourglass_flowing_sand: `{name}` {describe_tool(name, tool_input)[:120]}")

            def on_start(handle) -> None:
                RUNNING[key] = handle

            kwargs = dict(
                binary=CLAUDE_BIN, cwd=cwd, permission_args=permission_args(),
                model=model, append_system_prompt=system_note,
                extra_args=CLAUDE_EXTRA_ARGS, env=claude_env(), timeout=CLAUDE_TIMEOUT,
                on_init=on_init, on_activity=on_activity, on_start=on_start,
            )
            # The outbox dir is shared by every turn in this thread, so its
            # whole lifecycle (create -> upload -> remove) stays inside the lock.
            outbox.mkdir(parents=True, exist_ok=True)
            try:
                try:
                    result = run_turn(prompt, session_id=session_id, **kwargs)
                except ClaudeStopped:
                    raise
                except ClaudeError as e:
                    if session_id is None:
                        raise
                    log.warning("resume failed for %s (%s); retrying with a fresh session", key, e)
                    store.drop(key)
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

    except ClaudeStopped:
        progress.finalize(":octagonal_sign: Stopped.")
    except ClaudeError as e:
        progress.finalize(f":warning: {e}")
    except Exception:
        log.exception("unhandled error in thread %s", key)
        progress.finalize(":warning: Something went wrong — check the bot logs.")
    finally:
        RUNNING.pop(key, None)


@app.event("app_mention")
def on_mention(event, say, client):
    handle_prompt(event, say, client)


@app.event("message")
def on_message(event, say, client):
    if event.get("channel_type") != "im":
        return
    if event.get("subtype") or event.get("bot_id") or event.get("user") == BOT_USER_ID:
        return
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


def _sweeper() -> None:
    while True:
        removed = store.sweep(SESSION_MAX_AGE_DAYS)
        if removed:
            log.info("swept %d stale session(s) older than %sd", removed, SESSION_MAX_AGE_DAYS)
        for orphan in OUTBOX_ROOT.iterdir():
            if orphan.is_dir() and orphan.stat().st_mtime < time.time() - 86400:
                shutil.rmtree(orphan, ignore_errors=True)
        time.sleep(6 * 3600)


if __name__ == "__main__":
    reconcile_checkouts()
    threading.Thread(target=_sweeper, daemon=True, name="sweeper").start()
    log.info("workspace=%s approval_mode=%s allowlist=%s channel_dirs=%d",
             CLAUDE_CWD, CLAUDE_APPROVAL_MODE,
             ",".join(ALLOWED_USERS) or "(everyone)", len(CHANNEL_DIRS))
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()

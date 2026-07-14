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

OUTBOX_ROOT = BASE_DIR / "outbox"
HOOK_PATH = BASE_DIR / "approval_hook.py"

CLAUDE_CWD.mkdir(parents=True, exist_ok=True)
OUTBOX_ROOT.mkdir(exist_ok=True)

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
    if CLAUDE_APPROVAL_MODE == "slack":
        env["SLACK_BOT_APPROVAL_PORT"] = str(APPROVAL_PORT)
        env["SLACK_BOT_APPROVAL_TIMEOUT"] = str(APPROVAL_TIMEOUT)
    return env


# --- Files in / out ----------------------------------------------------------

def download_attachments(files: list[dict], dest_dir: Path) -> list[Path]:
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
        except Exception:
            log.exception("failed to download attachment %s", name)
    return saved


def upload_outbox(client, outbox: Path, channel: str, thread_ts: str) -> int:
    count = 0
    for path in sorted(p for p in outbox.rglob("*") if p.is_file()):
        try:
            client.files_upload_v2(channel=channel, thread_ts=thread_ts,
                                   file=str(path), title=path.name)
            count += 1
        except Exception:
            log.exception("failed to upload %s", path)
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
• `!terminal` — get the command to continue this thread in your terminal
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
            cwd = entry.get("cwd", str(CLAUDE_CWD))
            say(text=(f"Continue this thread in your terminal:\n"
                      f"```cd {cwd} && claude --resume {entry['session_id']}```\n"
                      "_Same session both ways: terminal turns become part of this thread's "
                      "history, and the next Slack message here picks up where the terminal "
                      "left off. Just don't run both at the same moment._"),
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
                link = f"<https://slack.com/archives/{ch}/p{ts.replace('.', '')}|thread>"
                lines.append(f"• {link} — {v.get('turns', 0)} turns, ${v.get('cost', 0):.2f}, {fmt_age(v.get('updated', 0))}")
            say(text=f"*Active sessions ({len(entries)}):*\n" + "\n".join(lines), thread_ts=thread_ts)
    else:
        return False
    return True


# --- Main handler ---------------------------------------------------------------

app = App(token=os.environ["SLACK_BOT_TOKEN"])
BOT_USER_ID = app.client.auth_test()["user_id"]

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
        app.client, port=APPROVAL_PORT, timeout=APPROVAL_TIMEOUT,
        auto_allow=APPROVAL_AUTO_ALLOW, allowed_users=ALLOWED_USERS,
        resolve_thread=_resolve_thread)
    approvals.register(app)
    approvals.start()


def handle_prompt(event: dict, say, client) -> None:
    channel = event["channel"]
    msg_ts = event["ts"]
    user = event.get("user", "")
    thread_ts = event.get("thread_ts", msg_ts)
    key = f"{channel}:{thread_ts}"

    if _dedup(channel, msg_ts):
        return
    if ALLOWED_USERS and user not in ALLOWED_USERS:
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
        saved = download_attachments(files, cwd / "slack-uploads")
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
                if result.session_id:
                    ACTIVE_SESSIONS[result.session_id] = (channel, thread_ts)
                uploaded = upload_outbox(client, outbox, channel, thread_ts)
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
    threading.Thread(target=_sweeper, daemon=True, name="sweeper").start()
    log.info("workspace=%s approval_mode=%s allowlist=%s channel_dirs=%d",
             CLAUDE_CWD, CLAUDE_APPROVAL_MODE,
             ",".join(ALLOWED_USERS) or "(everyone)", len(CHANNEL_DIRS))
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()

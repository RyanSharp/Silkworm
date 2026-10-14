"""Conversation threads from the dashboard: the parts that need no Slack.

The dashboard talks into the same threads Slack does -- a thread is still its
Slack `channel:ts`, its session and its sessions.json record -- so most of
what it needs is bot.py's own handle_prompt and handle_command. What is here
is the pure part, importable without a token and so tested directly:

  * files sent from the phone, which arrive inside the request as base64 and
    are written where Slack's file_share downloads go (uploads/<thread>/);
  * the note a turn's prompt gets about attached files, shared with Slack's
    path so both say the same thing (and both tell Claude to Read an image);
  * what a thread is doing now -- running, queued behind the running turn,
    how its recent turns ended -- read off the task records every prompt
    already creates.
"""

import base64
import binascii
import mimetypes
import time
import uuid
from pathlib import Path

import tasks

#: One file's limit. A phone photo is a few MB; this is a screen recording.
MAX_FILE_BYTES = 25 * 1024 * 1024
#: Files on one message, as Slack allows ten.
MAX_FILES = 10
#: How many finished turns the open thread is told about.
RECENT_TURNS = 5
#: Sources whose task is one message in a conversation (handle_prompt makes
#: "slack" and "ui" ones; a deferred wake-up is "defer").
CONVERSATION_SOURCES = ("slack", "ui", "defer")


class Refused(ValueError):
    """A dashboard message that cannot be taken, and why, in a sentence."""


def thread_dir(root: Path, key: str) -> Path:
    """Where a thread's uploads live -- the directory Slack's go to."""
    return Path(root) / key.replace(":", "__")


def _safe_name(raw) -> str:
    name = Path(str(raw or "").replace("\\", "/")).name.strip()
    # Path("..").name is "..": a name that is only dots names no file.
    if not name or set(name) <= {"."}:
        name = f"file-{uuid.uuid4().hex[:6]}"
    return name[:120]


def decode_files(files) -> list[dict]:
    """Check and decode the files a dashboard message carries, all before any
    is written: one bad file refuses the message rather than half of it.

    Each is {name, data (base64, or a data: URL), type?}. Returns
    [{name, data (bytes), mimetype}]; raises Refused.
    """
    if not files:
        return []
    if not isinstance(files, list):
        raise Refused("files must be a list")
    if len(files) > MAX_FILES:
        raise Refused(f"at most {MAX_FILES} files on one message")
    out = []
    for f in files:
        if not isinstance(f, dict):
            raise Refused("each file must have a name and its data")
        name = _safe_name(f.get("name"))
        raw = f.get("data") or ""
        if not isinstance(raw, str):
            raise Refused(f"{name}: its data is not text")
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise Refused(f"{name}: not base64") from None
        if not data:
            raise Refused(f"{name} is empty")
        if len(data) > MAX_FILE_BYTES:
            raise Refused(f"{name} is over {MAX_FILE_BYTES // (1024 * 1024)} MB")
        mime = str(f.get("type") or "").strip() or (mimetypes.guess_type(name)[0] or "")
        out.append({"name": name, "data": data, "mimetype": mime[:100]})
    return out


def write_files(root: Path, key: str, decoded: list[dict], now=None) -> list[dict]:
    """Write decoded files into the thread's uploads directory, named as
    Slack's are (`<epoch>-<name>`). Returns [{name, path, mimetype, size}]."""
    if not decoded:
        return []
    dest = thread_dir(root, key)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = int(now if now is not None else time.time())
    saved = []
    for f in decoded:
        path = dest / f"{stamp}-{f['name']}"
        if path.exists():                    # two of the same name this second
            path = dest / f"{stamp}-{uuid.uuid4().hex[:4]}-{f['name']}"
        # Only ever inside the thread's own directory. decode_files reduces a
        # name to its last component; this holds for a caller that did not.
        if path.resolve().parent != dest.resolve():
            raise Refused(f"{f['name']}: not a file name")
        path.write_bytes(f["data"])
        saved.append({"name": f["name"], "path": str(path),
                      "mimetype": f["mimetype"], "size": len(f["data"])})
    return saved


def attachment_note(saved: list[dict]) -> str:
    """What the turn's prompt says about the files sent with it.

    `saved` is [{path, mimetype, size}], whichever way they came (Slack or the
    dashboard). An image is named as one, with the instruction to look at it:
    a model handed a path alone tends to describe it rather than read it.
    """
    if not saved:
        return ""
    lines, any_image = [], False
    for f in saved:
        mime, size = f.get("mimetype") or "", f.get("size")
        note = f" ({mime}{f', {size // 1024} KB' if size else ''})" if mime else ""
        if mime.startswith("image/"):
            any_image = True
            note += " — an image; use Read to look at it"
        lines.append(f"- {f['path']}{note}")
    return (f"\n\n[The user attached {len(saved)} file(s), saved locally:\n"
            + "\n".join(lines) + "\n"
            + ("Read the image before answering — the user is showing you "
               "something, not describing it." if any_image else
               "Read them if they are relevant to the request.") + "]")


def echo(text: str, names=()) -> str:
    """How a dashboard message appears in its Slack thread: Slack never saw
    it, so the bot posts it, marked as from the dashboard."""
    line = f":globe_with_meridians: _via visualizer:_ {text}".rstrip()
    if names:
        line += f"\n:paperclip: _{len(names)} file(s): {', '.join(names)}_"
    return line


def _why(t: dict) -> str:
    for ev in reversed(t.get("events") or []):
        if ev.get("detail"):
            return str(ev["detail"])[:240]
    return ""


def thread_state(turns: list[dict], running: bool) -> dict:
    """What a thread is doing, from its tasks (TaskStore.by_thread, oldest
    first) and whether a turn holds its lock (`running`).

    A message sent while a turn runs has its task created before it waits on
    the thread's lock (handle_prompt), so it is here as queued from the
    moment it arrives -- whichever side sent it. `ref` is the task's
    source_ref: the message ts, which /web-message hands back to the page.
    """
    convo = [t for t in turns if t.get("driver") == "inline"
             and t.get("source") in CONVERSATION_SOURCES]

    def row(t):
        r = {"ref": t.get("source_ref") or "", "id": t.get("id"), "state": t.get("state"),
             "title": t.get("title") or "", "source": t.get("source") or "",
             "at": t.get("updated") or t.get("created") or 0}
        if t.get("state") in (tasks.FAILED, tasks.CANCELLED, tasks.BLOCKED):
            r["why"] = _why(t)
        return r
    queued = [row(t) for t in convo if t.get("state") == tasks.QUEUED]
    now = [row(t) for t in convo if t.get("state") == tasks.RUNNING]
    ended = [row(t) for t in convo
             if t.get("state") not in (tasks.QUEUED, tasks.RUNNING)][-RECENT_TURNS:]
    return {"ok": True, "running": bool(running) or bool(now), "queued": queued,
            "turns": ended + now + queued}

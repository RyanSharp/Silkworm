#!/usr/bin/env python3
"""Silkworm session visualizer — local web dashboard for thread sessions.

    python3 visualizer.py            # http://127.0.0.1:8790
    SILKWORM_VIZ_PORT=9000 python3 visualizer.py

Reads sessions.json and the Claude Code transcripts in ~/.claude/projects/.
Talks to the running bot (localhost) for live thread status and to relay
messages/commands into threads. Stdlib only; binds 127.0.0.1 only.
"""

import gzip
import hashlib
import json
import os
import socket
import socketserver
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import email.header
import secrets

import envfile
import jsonstore
import procs
import slacklinks

BASE_DIR = Path(__file__).resolve().parent


#: Read once: it is a kilobyte and never changes while the server runs.
try:
    FAVICON = (BASE_DIR / "assets" / "favicon.svg").read_bytes()
except OSError:
    FAVICON = b""

# --- installable web app ---------------------------------------------------
# The dashboard as a home-screen app: a manifest, its icons, and a service
# worker. All of it is served through do_GET like every other route, so the
# tailnet identity gate and the token apply to it too.

#: The page's background, used as the app's theme and splash colour.
THEME = "#2C0E2E"
#: Served path -> (file in assets/icons, size). Drawn from logo.svg by
#: assets/make_icons.py; 180 is the flattened apple-touch-icon. iOS asks for
#: /apple-touch-icon*.png at the root on its own, so those answer too.
ICONS = {
    "/icons/icon-180.png": ("icon-180.png", 180),
    "/icons/icon-192.png": ("icon-192.png", 192),
    "/icons/icon-512.png": ("icon-512.png", 512),
    "/apple-touch-icon.png": ("icon-180.png", 180),
    "/apple-touch-icon-precomposed.png": ("icon-180.png", 180),
}
ICON_BYTES = {}
for _path, (_name, _size) in ICONS.items():
    try:
        ICON_BYTES[_path] = (BASE_DIR / "assets" / "icons" / _name).read_bytes()
    except OSError:
        pass

MANIFEST = json.dumps({
    "name": "Silkworm",
    "short_name": "Silkworm",
    "id": "/",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": THEME,
    "theme_color": THEME,
    "icons": [{"src": p, "sizes": f"{s}x{s}", "type": "image/png", "purpose": "any"}
              for p, (_, s) in ICONS.items() if p.startswith("/icons/")],
}, indent=1).encode()

#: The service worker, at /sw.js so its scope is the whole dashboard. It caches
#: nothing: task state a minute stale is worse than an honest error, and the
#: page itself is one request. What it adds is a page that says why, when the
#: dashboard cannot be reached at all -- usually Tailscale being off. It only
#: answers navigations; every other request (/api/*, actions, icons) goes
#: straight to the network as if there were no worker. Below the fetch
#: handler: web push (push.py) -- show the notification, and a tap opens or
#: focuses the app on its thread or task.
SERVICE_WORKER = r"""// Silkworm dashboard service worker. Served by visualizer.py; caches nothing.
const VERSION = "1";

const OFFLINE = `<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="__THEME__"><title>Silkworm — offline</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:__THEME__;color:#F2E8F1;font:16px/1.5 -apple-system,"Segoe UI",sans-serif;
padding:max(24px,env(safe-area-inset-top)) max(24px,env(safe-area-inset-right))
max(24px,env(safe-area-inset-bottom)) max(24px,env(safe-area-inset-left));box-sizing:border-box}
main{max-width:420px;text-align:center}h1{font-size:20px;margin:0 0 8px}
p{color:#B49BB6;margin:0 0 18px}button{background:#ECB22E;color:#3D1140;border:0;
border-radius:10px;padding:12px 22px;font:inherit;font-weight:600}</style></head>
<body><main><h1>Can't reach Silkworm — is Tailscale connected?</h1>
<p>The dashboard runs on your Mac and is only reachable over your tailnet.
Turn Tailscale on, then try again.</p>
<button onclick="location.reload()">Try again</button></main></body></html>`;

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", event => {
  // Nothing is cached on purpose; clear anything an older worker left behind.
  event.waitUntil((async () => {
    for (const key of await caches.keys()) await caches.delete(key);
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", event => {
  const req = event.request;
  // Only page loads. /api/* and every action go to the network untouched,
  // and fail the way they would without a worker.
  if (req.mode !== "navigate") return;
  event.respondWith(fetch(req).catch(() => new Response(OFFLINE, {
    status: 503, headers: {"Content-Type": "text/html; charset=utf-8",
                           "Cache-Control": "no-store"}})));
});

// --- push ---
// A notification from the bot (push.py): {title, body, url, tag}, where url
// is a path on this dashboard -- "/?thread=<key>" or "/?task=<id>".
function safeUrl(u) {
  // A path on this dashboard and nothing else: no other origin, no scheme.
  return typeof u === "string" && u.startsWith("/") && !u.startsWith("//")
    && !u.startsWith("/\\") ? u : "/";
}

self.addEventListener("push", event => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; }
  catch (e) { d = {body: event.data ? event.data.text() : ""}; }
  // iOS ends a subscription that receives a push and shows nothing, so
  // every push shows something.
  event.waitUntil(self.registration.showNotification(d.title || "Silkworm", {
    body: d.body || "", tag: d.tag || undefined, icon: "/icons/icon-192.png",
    data: {url: safeUrl(d.url)}}));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const url = safeUrl(event.notification.data && event.notification.data.url);
  event.waitUntil((async () => {
    // The app already open: bring it forward and have the page open the
    // thread or task itself (it keeps its state; a reload would not).
    const wins = await self.clients.matchAll({type: "window", includeUncontrolled: true});
    for (const c of wins) {
      if (typeof c.focus !== "function") continue;
      c.postMessage({type: "open", url});
      try { await c.focus(); } catch (e) { /* focused or not, it was told */ }
      return;
    }
    await self.clients.openWindow(url);
  })());
});
""".replace("__THEME__", THEME).encode()

SESSIONS_FILE = BASE_DIR / "sessions.json"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
PORT = int(os.environ.get("SILKWORM_VIZ_PORT", "8790"))
# Loopback by default. The dashboard is not read-only -- it relays prompts into
# threads, and the bot trusts any localhost caller as the machine owner -- so
# binding it anywhere else requires a token (enforced at startup, below).
BIND = os.environ.get("VIZ_BIND", "127.0.0.1").strip()
TOKEN = os.environ.get("VIZ_TOKEN", "").strip()
LOOPBACK = BIND in ("127.0.0.1", "::1", "localhost")
#: Where VIZ_TAILSCALE_LOGIN is read from when the environment lacks it: the
#: launchd plist passes only PATH, so the installed service sees .env or
#: nothing. Read per request, so setting it needs no restart.
ENV_FILE = BASE_DIR / ".env"

#: What `tailscale serve` puts on every request it proxies (ipn/ipnlocal/
#: serve.go: addProxyForwardedHeaders always sets X-Forwarded-Host;
#: addTailscaleIdentityHeaders deletes then sets the Tailscale-* ones). A
#: direct loopback caller -- the CLI, an SSH tunnel -- sends none of them.
PROXY_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto",
                 "Forwarded", "Tailscale-User-Login", "Tailscale-User-Name",
                 "Tailscale-Funnel-Request", "Tailscale-Headers-Info")


def tailscale_login() -> str:
    """The one Tailscale login allowed through `tailscale serve`, or ""."""
    return (os.environ.get("VIZ_TAILSCALE_LOGIN", "").strip()
            or (envfile.value(ENV_FILE, "VIZ_TAILSCALE_LOGIN") or "").strip())


def proxied(headers) -> bool:
    """The request came through a proxy (tailscale serve), not straight from
    a loopback client. A Host on a tailnet name counts too, in case a serve
    version ever stops forwarding the X-Forwarded-* headers."""
    if any(headers.get(h) is not None for h in PROXY_HEADERS):
        return True
    host = (headers.get("Host") or "").split(":")[0].rstrip(".").lower()
    return host.endswith(".ts.net")


def _decode_login(raw: str) -> str:
    """Serve encodes a non-ASCII login as an RFC 2047 word; decode it."""
    try:
        return str(email.header.make_header(email.header.decode_header(raw))).strip()
    except Exception:
        return raw.strip()


def tailnet_refusal(headers) -> str:
    """Why a proxied request may not come in, or "" if it may (or is not proxied).

    Serve strips any Tailscale-User-* header a client sent and sets its own
    from WhoIs, so the login is the tailnet's word for who is asking. It
    leaves them off for tagged devices and for Funnel, so no header is no
    one in particular -- refused. Without VIZ_TAILSCALE_LOGIN nothing
    proxied gets in: turning serve on first exposes nothing."""
    if not proxied(headers):
        return ""
    allowed = tailscale_login()
    if not allowed:
        return ("Refused: this request came through a proxy (tailscale serve), and "
                "VIZ_TAILSCALE_LOGIN is not set, so the dashboard admits no one that "
                "way. Set VIZ_TAILSCALE_LOGIN in .env to your Tailscale login; "
                "`silkworm tailscale status` shows it.")
    if headers.get("Tailscale-Funnel-Request") is not None:
        return "Refused: the dashboard is never served over Tailscale Funnel."
    raw = headers.get("Tailscale-User-Login")
    if not raw or not raw.strip():
        return ("Refused: this request came through a proxy with no Tailscale "
                "identity (a tagged device, or not tailscale serve). Only "
                "VIZ_TAILSCALE_LOGIN may use the dashboard remotely.")
    login = _decode_login(raw)
    if login.lower() != allowed.lower():
        return (f"Refused: {login} is not the Tailscale login this dashboard "
                "belongs to (VIZ_TAILSCALE_LOGIN).")
    return ""



def cross_site_refusal(headers) -> str:
    """Why a proxied POST may not come in as a forgery, or "".

    Serve stamps the owner's login on every request from the owner's device,
    whichever web page sent it -- so a hostile page open in their phone's
    browser could POST to /api/tasks as them. A cross-site POST with no
    preflight can only be a "simple" one (text/plain, a form), and browsers
    say where it came from; the dashboard's own page always sends JSON from
    its own origin. Only proxied requests are held to this: direct loopback
    callers behave as they always have."""
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        return "Refused: a remote POST must be application/json."
    site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if site and site not in ("same-origin", "none"):
        return f"Refused: a {site} request; only the dashboard's own page may post."
    origin = (headers.get("Origin") or "").strip()
    if origin:
        host = (headers.get("X-Forwarded-Host") or headers.get("Host") or "").strip()
        if urlparse(origin).netloc.lower() != host.lower():
            return f"Refused: a POST from {origin}, not this dashboard."
    return ""

# Silent past twice the bot's idle limit -- which should have stopped it at
# once -- a turn is not slow, it is stuck. Silence, not age: a run writing to
# its transcript is working however long it has been at it.
STALL_AFTER_S = max(int(os.environ.get("CLAUDE_IDLE_TIMEOUT", "1800") or 0) * 2, 3600)


def _bot_port() -> str:
    return envfile.value(BASE_DIR / ".env", "APPROVAL_PORT") or "8787"


BOT_PORT = _bot_port()


# --- bot bridge ---------------------------------------------------------------

#: Why the last bot_call on this thread came back empty: "offline" (the bot
#: could not be reached, so it never saw the request) or "unknown" (it was
#: reached and gave no usable answer -- a timeout, a dropped connection, an
#: empty reply -- so it may have acted on it). bot_failure() reads it.
_CALL = threading.local()


def bot_call(path: str, payload: dict, timeout: float = 1.0) -> dict:
    _CALL.failure = ""
    req = urllib.request.Request(
        f"http://127.0.0.1:{BOT_PORT}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            answer = json.loads(resp.read())
    except urllib.error.HTTPError:
        _CALL.failure = "unknown"
        return {}
    except urllib.error.URLError:
        # urlopen wraps what goes wrong while connecting -- refused, no route,
        # a connect that timed out: the request never reached the bot.
        _CALL.failure = "offline"
        return {}
    except Exception:
        # Connected, then no answer: a timeout, a reset, a dropped connection.
        _CALL.failure = "unknown"
        return {}
    if not answer:
        _CALL.failure = "unknown"      # the bot's handler raised (LocalServer answers {})
    return answer


def bot_failure() -> str:
    """"unknown" or "offline" for this thread's last empty bot_call;
    "offline" if it said nothing (a stand-in bot_call)."""
    return getattr(_CALL, "failure", "") or "offline"


#: What /api/send says when the bot took a message and did not answer in time.
#: Not "offline": the thread may well have been made, and the page reconciles
#: by sending it again under the same client_id, which the bot answers from
#: the first send (bot.handle_web_message) rather than repeating it.
SEND_UNKNOWN = {"ok": False, "unknown": True,
                "error": "no answer from the bot in time — it may still have gone through"}


#: How long /api/send waits on the bot (which waits WEB_COMMAND_WAIT_S on a
#: command, and WEB_REPEAT_WAIT_S on a repeat still under way).
SEND_WAIT_S = 20


def send_answer(answer: dict) -> dict:
    """/api/send's reply for what bot_call returned."""
    if answer:
        return answer
    if bot_failure() == "unknown":
        return dict(SEND_UNKNOWN)
    return {"ok": False, "error": "bot is offline"}


# --- data loading ---------------------------------------------------------------

def raw_sessions() -> dict[str, dict]:
    # repair=False: the bot owns this file and is the one writing it. Falling
    # back to the backup in memory is fine for drawing a page; setting the
    # primary aside and rewriting it from a second process is not.
    raw = jsonstore.load(SESSIONS_FILE, default={}, repair=False) or {}
    return {k: ({"session_id": v} if isinstance(v, str) else v) for k, v in raw.items()}


def last_write(session_id: str) -> float | None:
    """When this session last wrote anything, or None: its transcript, or a
    subagent's -- each writes its own file beside it (<session>/subagents/),
    and a turn waiting on one leaves its own transcript quiet meanwhile."""
    path = find_transcript(session_id)
    if not path:
        return None
    times = []
    for p in (path, *(path.with_suffix("") / "subagents").glob("*.jsonl")):
        try:
            times.append(p.stat().st_mtime)
        except OSError:
            pass
    return max(times, default=None)


def turn_health(entry: dict, live: set, written=last_write) -> dict | None:
    """How long this thread's in-flight turn has been going, whether the
    child is actually there, and how long it has been silent.

    A turn whose child has died, or that has written nothing to its
    transcript for far longer than any tool call takes, is stalled -- it will
    never finish on its own. Surfacing that is the difference between
    noticing in a minute and noticing in a day. A long run still writing is
    not: age alone says nothing.
    """
    pending = entry.get("pending")
    if not pending:
        return None
    started = pending.get("started") or ""
    try:
        began = datetime.strptime(started, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - began).total_seconds()
    except (TypeError, ValueError):
        return None
    sid = pending.get("session_id") or entry.get("session_id") or ""
    alive = sid in live
    # Silent since the later of its start and its last transcript write; a
    # run that has not written yet is as silent as it is old.
    wrote = (written(sid) if sid else None) or 0
    silent = min(age, time.time() - wrote) if wrote else age
    return {
        "age_s": int(age),
        "silent_s": int(max(0, silent)),
        "child_alive": alive,
        # No child means it can never finish; silent past the bound, it never will.
        "stalled": (not alive) or silent > STALL_AFTER_S,
        "prompt": pending.get("prompt", ""),
    }


def cost_anomaly(entry: dict) -> dict | None:
    """Flag a last turn that cost far more than this thread's own norm.

    Compared against the thread's median rather than a fixed number, since a
    cheap chat thread and a heavy refactor thread have very different baselines.
    A cache regression shows up exactly like this.
    """
    costs = [c for c in (entry.get("costs") or []) if c is not None]
    if len(costs) < 5:
        return None                      # not enough history to have a norm
    last, prev = costs[-1], sorted(costs[:-1])
    median = prev[len(prev) // 2]
    if median <= 0 or last < 0.25:        # ignore noise on trivially cheap turns
        return None
    ratio = last / median
    if ratio < 5:
        return None
    return {"last": round(last, 4), "median": round(median, 4), "ratio": round(ratio, 1)}


def load_sessions() -> dict:
    status = bot_call("/status", {}, timeout=0.6)
    threads = status.get("threads", {})
    entries = raw_sessions()
    # Only a pending turn reads `live`; with none in flight, no ps at all.
    live = procs.alive_sessions(
        [(e.get("pending") or {}).get("session_id") or e.get("session_id")
         for e in entries.values() if e.get("pending")])
    out = []
    for key, entry in entries.items():
        channel, _, thread_ts = key.partition(":")
        sid = entry.get("session_id", "")
        cwd = entry.get("cwd", "")
        st = threads.get(key, {})
        out.append({
            "key": key,
            "hidden": bool(entry.get("hidden")),
            "title": entry.get("title") or "",
            "kind": entry.get("kind") or "thread",
            # So the projects board can list threads filed under none.
            "project": entry.get("project") or "",
            "summary": entry.get("summary") or "",
            "session_id": sid,
            "model": entry.get("model"),
            "cwd": cwd,
            "turns": entry.get("turns", 0),
            "cost": entry.get("cost", 0.0),
            "updated": entry.get("updated", 0),
            "files": len(entry.get("files", [])),
            "turn": turn_health(entry, live),
            # A summary written before later turns no longer describes the
            # thread. Entries predating summary_turns are unknown, not stale --
            # assuming 0 would flag every existing thread at once.
            "summary_stale": bool(entry.get("summary") and "summary_turns" in entry
                                  and entry.get("turns", 0) > entry["summary_turns"]),
            "cost_flag": cost_anomaly(entry),
            "events": (entry.get("events") or [])[-8:],
            "running": st.get("running", False),
            "checked_out": st.get("checked_out", False),
            "terminal_live": st.get("terminal_live", False),
            "slack_link": slacklinks.thread_link(channel, thread_ts),
            "resume_cmd": f"cd {cwd or '~'} && claude --resume {sid}",
            "has_transcript": find_transcript(sid) is not None,
        })
    out.sort(key=lambda s: -s["updated"])
    # bot_online only says the local server answered; it stayed true through a
    # seventeen-hour Slack outage. The link is a separate fact.
    return {"bot_online": bool(status.get("online")), "sessions": out,
            "slack": status.get("slack") or {},
            # A bot answering here is not a bot running the code on disk: it
            # merges onto its own main and nothing restarts it. A bot that
            # answers but reports nothing is old enough to predate the field,
            # which is itself the answer; one that does not answer at all is
            # offline, and the status dot already says so.
            "revision": (status.get("revision") or
                         ({"state": "unknown",
                           "message": "this bot is too old to report its revision"}
                          if status.get("online") else {}))}


#: session id -> transcript path, rebuilt only when a project directory
#: changes. find_transcript used to glob all ~400 of them per call, and
#: /api/sessions calls it once per thread: 0.95s of the 1s that request took.
_index_lock = threading.Lock()
_index: dict = {"root": None, "key": None, "checked": 0.0, "map": {}}

#: How long a checked index is trusted without looking at the directories
#: again. Long enough that one request's four hundred lookups share one look;
#: short enough that a new session is found on the next poll.
INDEX_RECHECK_S = 1.0


def _scan_dirs(root: Path) -> list:
    try:
        return sorted((e for e in os.scandir(root) if e.is_dir()), key=lambda e: e.name)
    except OSError:
        return []


def transcript_index() -> dict:
    """{session id: transcript path}. A file appearing in, or leaving, a
    project directory changes that directory's mtime, and a new project
    directory changes the root's; anything else needs no rescan."""
    root = PROJECTS_DIR
    now = time.monotonic()
    with _index_lock:
        if _index["root"] == root and now - _index["checked"] < INDEX_RECHECK_S:
            return _index["map"]
        dirs = _scan_dirs(root)
        try:
            key = (root.stat().st_mtime_ns,) + tuple(
                (e.name, e.stat().st_mtime_ns) for e in dirs)
        except OSError:
            key = None
        if _index["root"] != root or key is None or key != _index["key"]:
            found: dict = {}
            for e in dirs:                    # first match wins, as the glob did
                try:
                    for f in os.scandir(e.path):
                        if f.name.endswith(".jsonl"):
                            found.setdefault(f.name[:-6], Path(f.path))
                except OSError:
                    # Not remembered as current: a directory that could not
                    # be read is read again next time, not hidden until it
                    # happens to change.
                    key = None
                    continue
            _index.update(root=root, key=key, map=found)
        _index["checked"] = now
        return _index["map"]


def find_transcript(session_id: str) -> Path | None:
    if not session_id or not PROJECTS_DIR.exists():
        return None
    p = transcript_index().get(session_id)
    # Deleted since the index was built: not there, rather than a stale path.
    return p if p is not None and p.exists() else None


def _block_list(content) -> list[dict]:
    blocks = []
    if isinstance(content, str):
        if content.strip():
            blocks.append({"type": "text", "text": content})
        return blocks
    for b in content or []:
        btype = b.get("type")
        if btype == "text" and b.get("text", "").strip():
            blocks.append({"type": "text", "text": b["text"]})
        elif btype == "thinking" and b.get("thinking", "").strip():
            blocks.append({"type": "thinking", "text": b["thinking"]})
        elif btype == "tool_use":
            blocks.append({"type": "tool", "name": b.get("name", "tool"),
                           "input": json.dumps(b.get("input") or {}, indent=2)[:2000]})
        elif btype == "tool_result":
            inner = b.get("content")
            if isinstance(inner, list):
                inner = "\n".join(x.get("text", "") for x in inner if isinstance(x, dict))
            blocks.append({"type": "tool_result", "text": str(inner or "")[:2000]})
    return blocks


def iter_entries(path: Path):
    for line in path.read_text().splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") in ("user", "assistant"):
            yield d


def _complete_lines(path: Path, start: int) -> tuple[list, int, bool]:
    """The whole lines written after byte `start`, and the offset just past the
    last of them. A line still being written is left for the next read. A file
    now shorter than `start` was replaced: read from the top, and say so."""
    reset = False
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        if start > size:
            start, reset = 0, True
        f.seek(start)
        chunk = f.read(size - start)
    end = chunk.rfind(b"\n") + 1
    return chunk[:end].splitlines(), start + end, reset


def _entries_of(lines):
    for raw in lines:
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(d, dict) and d.get("type") in ("user", "assistant"):
            yield d


def load_transcript(session_id: str, cursor: int | None = None) -> dict:
    """The transcript's messages. With `cursor` (a byte offset a previous
    answer returned), only those written since: the page polls the open
    thread every four seconds and used to take the whole file each time."""
    path = find_transcript(session_id)
    if not path:
        return {"error": "no transcript found for this session"}
    reset = False
    if cursor is None:
        entries = iter_entries(path)
    else:
        try:
            lines, cursor, reset = _complete_lines(path, max(0, cursor))
        except OSError:
            return {"error": "no transcript found for this session"}
        entries = _entries_of(lines)
    messages = []
    for d in entries:
        msg = d.get("message") or {}
        blocks = _block_list(msg.get("content"))
        if not blocks:
            continue
        item = {"role": d["type"], "ts": d.get("timestamp", ""), "blocks": blocks}
        usage = msg.get("usage")
        if d["type"] == "assistant" and isinstance(usage, dict):
            item["usage"] = {
                "in": usage.get("input_tokens", 0),
                "out": usage.get("output_tokens", 0),
                "cache_read": usage.get("cache_read_input_tokens", 0),
                "cache_create": usage.get("cache_creation_input_tokens", 0),
            }
            item["model"] = msg.get("model")
        messages.append(item)
    out = {"session_id": session_id, "path": str(path), "messages": messages}
    if cursor is not None:
        out.update(cursor=cursor, reset=reset)
    return out


def live_session(entry: dict) -> str:
    """The session a thread is on now. A turn starting a new session (a new
    thread, or after !reset) only writes session_id when it finishes; its
    pending marker has the id from the moment Claude reports it, which is
    what lets its first turn be followed live."""
    return ((entry.get("pending") or {}).get("session_id")
            or entry.get("session_id") or "")


def load_thread(key: str, held: str, cursor: int) -> dict:
    """The open thread, by its key: the transcript of the session it is on now
    from `cursor`, and what it is doing (the bot's /thread-state).

    `held` is the session the page has drawn so far. When the thread has moved
    on to another -- reset, a new thread's first session appearing, a move
    into a project -- the read starts again from the top and says `reset`,
    so the page never appends one session's messages to another's. A thread
    with no session yet, or whose new session has written nothing yet, is an
    empty transcript rather than an error: it is the moment just after
    sending, and the page shows the message as on its way.
    """
    entry = raw_sessions().get(key) or {}
    sid = live_session(entry)
    moved = sid != (held or "")
    if not sid:
        data = {"session_id": "", "messages": [], "cursor": 0, "reset": moved}
    else:
        data = load_transcript(sid, 0 if moved else max(0, cursor))
        if data.get("error") and entry.get("pending"):
            data = {"session_id": sid, "messages": [], "cursor": 0}
        data.update(session_id=sid, reset=moved)
    data["key"] = key
    data["live"] = bot_call("/thread-state", {"key": key}, timeout=1.0) or {"offline": True}
    return data


#: Per transcript, its token usage so far, read incrementally: a transcript
#: only grows, so each poll reads what was appended since the last one rather
#: than every transcript in full (2.4s every thirty seconds).
_usage_lock = threading.Lock()
_usage: dict = {}


def _usage_of(path: Path) -> dict:
    """{offset, ino, cache, fresh, models: {model: out}, daily: {day: {...}}}."""
    key = str(path)
    st = path.stat()
    with _usage_lock:
        rec = _usage.get(key)
        if rec is None or rec["ino"] != st.st_ino or st.st_size < rec["offset"]:
            rec = {"offset": 0, "ino": st.st_ino, "cache": 0, "fresh": 0,
                   "models": {}, "daily": {}}
        if st.st_size > rec["offset"]:
            lines, rec["offset"], reset = _complete_lines(path, rec["offset"])
            if reset:                         # replaced underneath us: start over
                rec.update(cache=0, fresh=0, models={}, daily={})
            _add_usage(rec, _entries_of(lines))
        _usage[key] = rec
        # A copy, taken under the lock: a second request reading the same
        # transcript may be adding to the record while this one sums it.
        return {"cache": rec["cache"], "fresh": rec["fresh"], "models": dict(rec["models"]),
                "daily": {d: dict(v) for d, v in rec["daily"].items()}}


def _add_usage(rec: dict, entries) -> None:
    for d in entries:
        msg = d.get("message") or {}
        usage = msg.get("usage")
        if d.get("type") != "assistant" or not isinstance(usage, dict):
            continue
        ts = d.get("timestamp", "")
        cache = usage.get("cache_read_input_tokens", 0) or 0
        fresh = (usage.get("input_tokens", 0) or 0) + (usage.get("cache_creation_input_tokens", 0) or 0)
        out = usage.get("output_tokens", 0) or 0
        rec["cache"] += cache
        rec["fresh"] += fresh
        model = msg.get("model") or "unknown"
        if not model.startswith("<"):
            rec["models"][model] = rec["models"].get(model, 0) + out
        if ts:
            try:
                t = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
                day = time.strftime("%Y-%m-%d", time.localtime(t))
            except ValueError:
                continue
            bucket = rec["daily"].setdefault(day, {"cache_read": 0, "fresh_in": 0, "out": 0})
            bucket["cache_read"] += cache
            bucket["fresh_in"] += fresh
            bucket["out"] += out


def load_stats(days: int = 14) -> dict:
    sessions = raw_sessions()
    today = time.time()
    day_keys = [time.strftime("%Y-%m-%d", time.localtime(today - i * 86400))
                for i in range(days - 1, -1, -1)]
    daily = {d: {"cache_read": 0, "fresh_in": 0, "out": 0} for d in day_keys}
    models: dict[str, int] = {}
    tot_cache = tot_in = 0
    for entry in sessions.values():
        path = find_transcript(entry.get("session_id", ""))
        if not path:
            continue
        try:
            rec = _usage_of(path)
        except OSError:
            continue
        tot_cache += rec["cache"]
        tot_in += rec["fresh"]
        for model, n in rec["models"].items():
            models[model] = models.get(model, 0) + n
        for day, row in daily.items():
            got = rec["daily"].get(day)
            if got:
                for k in row:
                    row[k] += got[k]
    denom = tot_cache + tot_in
    return {
        "total_cost": round(sum(e.get("cost", 0.0) for e in sessions.values()), 2),
        "threads": len(sessions),
        "cache_rate": round(100 * tot_cache / denom, 1) if denom else None,
        "days": [{"date": d, **daily[d]} for d in day_keys],
        "models": sorted(models.items(), key=lambda kv: -kv[1]),
    }


#: load_stats' answer, reused for this long. The page polls it every thirty
#: seconds and it changes only as fast as tokens are spent; the transcripts
#: are read incrementally underneath anyway (_usage_of).
STATS_TTL_S = 20
_stats_cache: dict = {"at": 0.0, "data": None}


def cached_stats() -> dict:
    now = time.monotonic()
    if _stats_cache["data"] is None or now - _stats_cache["at"] >= STATS_TTL_S:
        _stats_cache.update(data=load_stats(), at=now)
    return _stats_cache["data"]


def search(query: str) -> list[dict]:
    q = query.lower()
    results = []
    if not q:
        return results
    for key, entry in raw_sessions().items():
        sid = entry.get("session_id", "")
        path = find_transcript(sid)
        if not path:
            continue
        for d in iter_entries(path):
            for b in _block_list((d.get("message") or {}).get("content")):
                if b["type"] != "text":
                    continue
                low = b["text"].lower()
                i = low.find(q)
                if i < 0:
                    continue
                start = max(0, i - 60)
                snippet = b["text"][start:i + len(query) + 60].replace("\n", " ")
                results.append({"key": key, "session_id": sid, "role": d["type"],
                                "ts": d.get("timestamp", ""), "snippet": snippet})
                if len(results) >= 50:
                    return results
                break  # one hit per message is enough
    return results


def artifacts(key: str) -> list[dict]:
    entry = raw_sessions().get(key) or {}
    out = []
    for rec in entry.get("files", []):
        p = Path(rec.get("path", ""))
        out.append({**rec, "exists": p.is_file(),
                    "size": p.stat().st_size if p.is_file() else 0})
    return out


def allowed_download(path_str: str) -> Path | None:
    """Only serve files the bot recorded as thread artifacts."""
    for entry in raw_sessions().values():
        for rec in entry.get("files", []):
            if rec.get("path") == path_str:
                p = Path(path_str)
                return p if p.is_file() else None
    return None


# --- HTTP -----------------------------------------------------------------------

#: /api/tasks actions that only read. Only these answer 304: an action that
#: does something must always do it, whatever the page last saw.
READ_TASK_ACTIONS = frozenset({"list", "attention", "counts", "board", "overview",
                               "task", "unmerged", "holding", "roles", "watching"})

#: The largest POST taken: a message's attached files travel in it as base64
#: (webthread.MAX_FILE_BYTES each, about 4/3 larger encoded).
MAX_POST_BYTES = 60 * 1024 * 1024

#: Below this a body goes as it is; gzip would only add its header.
GZIP_MIN = 1024


def etag_for(data) -> str:
    """A strong validator for a JSON answer: the hash of how it serialises."""
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return '"' + hashlib.sha1(raw).hexdigest()[:24] + '"'


def sessions_tag(data: dict) -> str:
    """/api/sessions' validator, leaving out each running turn's age and
    silence. They move every second, so with them in no poll during a turn
    would ever match; the page adds the time since its last full answer instead. Crossing into
    `stalled` changes the answer on its own."""
    steady = dict(data, sessions=[
        dict(x, turn=dict(x["turn"], age_s=None, silent_s=None)) if x.get("turn") else x
        for x in data.get("sessions") or []])
    return etag_for(steady)


def make_server(bind: str, port: int, handler=None) -> ThreadingHTTPServer:
    """A server on VIZ_BIND. ThreadingHTTPServer is IPv4-only, so an IPv6
    address (``::1``, ``::``, a LAN v6 address) would fail at startup with
    gaierror; any bind containing ':' gets an AF_INET6 server instead. For
    the ``::`` wildcard, IPV6_V6ONLY is cleared so it also answers IPv4 --
    ``silkworm`` probes a wildcard bind on 127.0.0.1, and on Linux the
    default would leave that refused.

    HTTPServer.server_bind also reverse-resolves the bind address with
    getfqdn, only to fill server_name (read by CGI, which this is not); on
    a machine whose resolver stalls that took 35s, so it is skipped."""
    v6 = ":" in bind

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if v6 else socket.AF_INET

        def server_bind(self):
            if bind == "::":
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = bind, self.server_address[1]

    return Server((bind, port), handler or Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        # The page is usually read through an SSH tunnel: JSON shrinks four or
        # five times, which is most of what a poll costs there.
        if (len(body) >= GZIP_MIN and ctype.startswith(("application/json", "text/"))
                and "gzip" in self.headers.get("Accept-Encoding", "")):
            body = gzip.compress(body, 5)
            extra = {**(extra or {}), "Content-Encoding": "gzip", "Vary": "Accept-Encoding"}
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, tag: str | None = None) -> None:
        """Send `data`; with `tag`, as a conditional answer -- 304 and no body
        when the page already holds this exact answer (If-None-Match)."""
        if tag is None:
            self._send(200, json.dumps(data).encode(), "application/json")
            return
        held = {t.strip() for t in self.headers.get("If-None-Match", "").split(",")}
        if tag in held:
            self.send_response(304)
            self.send_header("ETag", tag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        self._send(200, json.dumps(data).encode(), "application/json",
                   {"ETag": tag, "Cache-Control": "no-cache"})

    def _cached(self, data) -> None:
        self._json(data, etag_for(data))

    def _authed(self, url) -> bool:
        """Proxied (tailscale serve) requests must be VIZ_TAILSCALE_LOGIN;
        then loopback needs no token, and anything else must present one.

        Accepts ?token= once and hands back a cookie, so a bookmarked URL
        works without the secret sitting in the address bar afterwards.
        """
        refusal = tailnet_refusal(self.headers)
        if refusal:
            self._send(403, (refusal + "\n").encode(), "text/plain; charset=utf-8")
            return False
        if LOOPBACK:
            return True
        supplied = (self.headers.get("X-Silkworm-Token", "")
                    or parse_qs(url.query).get("token", [""])[0]
                    or self._cookie_token())
        if secrets.compare_digest(supplied, TOKEN):
            return True
        self._send(401, b"unauthorized", "text/plain")
        return False

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "silkworm_token":
                return value
        return ""

    def do_GET(self):
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        if not self._authed(url):
            return
        if url.path == "/":
            cookie = {}
            if not LOOPBACK and qs.get("token", [""])[0] == TOKEN:
                cookie["Set-Cookie"] = (f"silkworm_token={TOKEN}; Path=/; "
                                        "HttpOnly; SameSite=Strict; Max-Age=31536000")
            self._send(200, PAGE.encode(), "text/html; charset=utf-8", cookie)
        elif url.path in ("/favicon.svg", "/favicon.ico"):
            # Both paths: the link tag asks for the svg, but browsers request
            # /favicon.ico on their own and a 404 for it is noise in the log.
            if FAVICON:
                self._send(200, FAVICON, "image/svg+xml",
                           {"Cache-Control": "public, max-age=86400"})
            else:
                self._send(404, b"no icon", "text/plain")
        elif url.path in ICONS:
            if url.path in ICON_BYTES:
                self._send(200, ICON_BYTES[url.path], "image/png",
                           {"Cache-Control": "public, max-age=86400"})
            else:
                self._send(404, b"no icon", "text/plain")
        elif url.path == "/manifest.webmanifest":
            self._send(200, MANIFEST, "application/manifest+json",
                       {"Cache-Control": "no-cache"})
        elif url.path == "/sw.js":
            # no-cache: an updated worker must reach the phone on its next load.
            self._send(200, SERVICE_WORKER, "text/javascript; charset=utf-8",
                       {"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})
        elif url.path == "/api/sessions":
            data = load_sessions()
            self._json(data, sessions_tag(data))
        elif url.path == "/api/session":
            frm = qs.get("from", [""])[0]
            cursor = int(frm) if frm.isdigit() else None
            key = qs.get("key", [""])[0]
            if key:
                self._cached(load_thread(key, qs.get("id", [""])[0], cursor or 0))
            else:
                self._cached(load_transcript(qs.get("id", [""])[0], cursor))
        elif url.path == "/api/stats":
            self._cached(cached_stats())
        elif url.path == "/api/search":
            self._json(search(qs.get("q", [""])[0]))
        elif url.path == "/api/artifacts":
            self._cached(artifacts(qs.get("key", [""])[0]))
        elif url.path == "/download":
            p = allowed_download(qs.get("path", [""])[0])
            if not p:
                self._send(404, b"not an artifact", "text/plain")
                return
            self._send(200, p.read_bytes(), "application/octet-stream",
                       {"Content-Disposition": f'attachment; filename="{p.name}"'})
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        url = urlparse(self.path)
        if not self._authed(url):
            return
        forged = cross_site_refusal(self.headers) if proxied(self.headers) else ""
        if forged:
            self._send(403, (forged + "\n").encode(), "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length > MAX_POST_BYTES:
            # Said before reading it: a photo too big is refused, not buffered.
            self._send(413, json.dumps({"ok": False, "error": (
                f"too big: at most {MAX_POST_BYTES // (1024 * 1024)} MB in one message")}).encode(),
                "application/json")
            return
        try:
            payload = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            payload = {}
        if url.path == "/api/send":
            # A !command is waited on for its replies (bot.WEB_COMMAND_WAIT_S),
            # and attached files travel inside the request.
            answer = bot_call("/web-message", payload, timeout=SEND_WAIT_S)
            self._json(send_answer(answer))
        elif url.path == "/api/push":
            # A test sends to every subscription before answering.
            answer = bot_call("/push", payload,
                              timeout=60 if payload.get("action") == "test" else 10)
            self._json(answer or {"ok": False, "error": "bot is offline"})
        elif url.path == "/api/learnings":
            answer = bot_call("/learnings", payload, timeout=3)
            self._json(answer or {"ok": False, "error": "bot is offline"})
        elif url.path == "/api/projects":
            # Making a project with a GitHub repository waits on a push.
            answer = bot_call("/projects", payload,
                              timeout=200 if payload.get("action") == "new" else 10)
            self._json(answer or {"ok": False, "error": "bot is offline"})
        elif url.path == "/api/tasks":
            answer = bot_call("/tasks", payload, timeout=15)
            if answer and payload.get("action", "list") in READ_TASK_ACTIONS:
                self._cached(answer)
            else:
                self._json(answer or {"ok": False, "error": "bot is offline"})
        elif url.path == "/api/titles":
            answer = bot_call("/titles", payload, timeout=180)
            self._json(answer or {"ok": False, "error": "bot is offline"})
        elif url.path == "/api/hide":
            answer = bot_call("/hide", payload, timeout=10)
            self._json(answer or {"ok": False, "error": "bot is offline"})
        elif url.path == "/api/releases":
            # A preview runs the target's dry run (a migration diff); a plan
            # fetches origin. Neither ships anything.
            answer = bot_call("/releases", payload,
                              timeout=900 if payload.get("action") == "preview" else 90)
            self._json(answer or {"ok": False, "error": "bot is offline"})
        elif url.path == "/api/release":
            answer = bot_call("/release", payload, timeout=30)
            self._json(answer or {"ok": False, "error": "bot is offline"})
        elif url.path == "/api/summaries":
            # Generating a summary runs a model, so allow a generous timeout.
            answer = bot_call("/summaries", payload, timeout=180)
            self._json(answer or {"ok": False, "error": "bot is offline"})
        else:
            self._send(404, b"not found", "text/plain")


# --- page -------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Silkworm — Sessions</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials">
<link rel="apple-touch-icon" href="/icons/icon-180.png">
<meta name="theme-color" content="#2C0E2E">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Silkworm">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<style>
  :root {
    --bg: #2C0E2E; --panel: #3A1440; --line: #57265B; --line2: #4A1F50;
    --ink: #F2E8F1; --muted: #B49BB6; --gold: #ECB22E; --silk: #F6EEDF;
    --c-cache: #C08A1C; --c-fresh: #2D9FD0; --c-out: #D8517F;
    --mono: ui-monospace, "SF Mono", Menlo, monospace;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
         font: 15px/1.5 -apple-system, "Segoe UI", sans-serif; height: 100vh;
         display: flex; flex-direction: column; }
  header { display: flex; align-items: center; gap: 12px; padding: 10px 20px;
           border-bottom: 1px solid var(--line); flex: none; }
  header svg { width: 40px; height: 26px; flex: none; }
  header h1 { font-size: 17px; margin: 0; font-weight: 600; white-space: nowrap; }
  #botdot { width: 9px; height: 9px; border-radius: 50%; background: #777; flex: none; }
  #botdot.on { background: #2EB67D; }
  #search { margin-left: auto; background: var(--panel); color: var(--ink);
            border: 1px solid var(--line); border-radius: 8px; padding: 6px 12px;
            width: 300px; font-size: 13px; }
  #search:focus { outline: none; border-color: var(--gold); }

  .layout { display: flex; flex: 1; min-height: 0; }
  nav { width: 350px; flex: none; overflow-y: auto; border-right: 1px solid var(--line);
        padding: 10px; display: flex; flex-direction: column; gap: 8px; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
          padding: 10px 12px; cursor: pointer; }
  .card:hover { border-color: var(--gold); }
  .card.active { border-color: var(--gold); box-shadow: 0 0 0 1px var(--gold); }
  .card .key { font-family: var(--mono); font-size: 12px; color: var(--silk);
               word-break: break-all; display: flex; gap: 6px; align-items: center; }
  .card .key .title { font-family: inherit; font-size: 13.5px; font-weight: 600;
                      color: var(--ink); }
  .card .subkey { font-family: var(--mono); font-size: 10px; color: var(--muted);
                  word-break: break-all; margin-top: 1px; }
  /* Sidebar gets a 3-line clamp; the full text lives in the detail header. */
  .card .summary { font-size: 11.5px; line-height: 1.45; color: var(--muted);
                   margin-top: 5px; word-break: normal; display: -webkit-box;
                   -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
  .ctx { background: var(--panel2, #00000014); border-left: 2px solid var(--gold);
         border-radius: 0 6px 6px 0; padding: 8px 11px; margin: 0 0 10px;
         font-size: 12.5px; line-height: 1.5; color: var(--ink); }
  .ctx .lbl { font-family: var(--mono); font-size: 10px; color: var(--muted);
              text-transform: uppercase; letter-spacing: .06em; margin-right: 6px; }
  .badge { font-size: 10px; border-radius: 6px; padding: 1px 7px; font-family: var(--mono);
           flex: none; }
  .badge.run { background: #2EB67D22; color: #2EB67D; border: 1px solid #2EB67D66; }
  .badge.term { background: #C08A1C22; color: var(--gold); border: 1px solid #C08A1C66; }
  .badge.stall { background: #D8517F26; color: #D8517F; border: 1px solid #D8517F99;
                 font-weight: 650; }
  .turnbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
             font-size: 12.5px; padding: 8px 11px; margin: 0 0 10px;
             border-radius: 6px; background: #2EB67D18;
             border-left: 2px solid #2EB67D; }
  .turnbar.bad { background: #D8517F1C; border-left-color: #D8517F; }
  .badge.kind { background: #8881; color: var(--muted); border: 1px solid var(--line); }
  .listcol { display: flex; flex-direction: column; min-width: 0; }
  #kindfilter { display: flex; gap: 4px; padding: 6px 8px 2px; flex-wrap: wrap; }
  #kindfilter button { font-size: 11px; font-family: var(--mono); cursor: pointer;
    background: transparent; color: var(--muted); border: 1px solid var(--line);
    border-radius: 6px; padding: 2px 8px; }
  #kindfilter button:hover { color: var(--fg); }
  #kindfilter button.on { background: #8882; color: var(--fg); border-color: #888a; }
  #nightly { display: flex; gap: 6px; flex-wrap: wrap; align-items: center;
    padding: 4px 0 10px; }
  #nightly .nlabel { font-size: 11px; color: var(--muted); font-family: var(--mono); }
  #nightly button { font-size: 11px; font-family: var(--mono); cursor: pointer;
    background: transparent; color: var(--muted); border: 1px solid var(--line);
    border-radius: 6px; padding: 2px 8px; }
  #nightly button:hover { color: var(--fg); }
  #nightly button.on { background: #C08A1C22; color: var(--gold);
    border-color: #C08A1C99; }
  #unmerged { display: flex; gap: 6px; flex-wrap: wrap; align-items: center;
    padding: 0 0 10px; }
  #unmerged .nlabel { font-size: 11px; color: var(--c-fresh); font-family: var(--mono); }
  #unmerged .b { font-size: 11px; font-family: var(--mono); cursor: default;
    background: #2D9FD01A; color: var(--ink); border: 1px solid #2D9FD077;
    border-radius: 6px; padding: 2px 8px; }
  #unmerged button.b { cursor: pointer; }
  #unmerged button.b:hover { border-color: var(--c-fresh); }
  #unmerged .b b { color: var(--c-fresh); }
  #holding { display: flex; gap: 6px; flex-wrap: wrap; align-items: center;
    padding: 0 0 10px; }
  #holding .nlabel { font-size: 11px; color: var(--gold); font-family: var(--mono); }
  #holding .b { font-size: 11px; font-family: var(--mono); cursor: default;
    background: #C08A1C1A; color: var(--ink); border: 1px solid #C08A1C77;
    border-radius: 6px; padding: 2px 8px; }
  #holding button.b { cursor: pointer; }
  #holding button.b:hover { border-color: var(--gold); }
  #holding .b b { color: var(--gold); }
  .task .held { color: var(--gold); }
  .hidebtn { float: right; background: transparent; border: 0; cursor: pointer;
    color: var(--muted); font-size: 13px; line-height: 1; padding: 0 2px; }
  .hidebtn:hover { color: var(--fg); }
  .badge.cost { background: #C08A1C22; color: var(--gold); border: 1px solid #C08A1C99; }
  .card .untitled { color: var(--muted); font-style: italic; font-size: 12.5px; }
  .pill { font-size: 10.5px; font-family: var(--mono); color: var(--gold);
          border: 1px solid #C08A1C66; border-radius: 6px; padding: 1px 6px;
          white-space: nowrap; }
  #alertbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
              padding: 0 18px 10px; }
  #alertbar:empty { display: none; }
  .alert { font-size: 12px; font-weight: 650; padding: 3px 9px; border-radius: 6px; }
  .alert.bad  { background: #D8517F26; color: #D8517F; border: 1px solid #D8517F99; }
  .alert.warn { background: #C08A1C26; color: var(--gold); border: 1px solid #C08A1C99; }
  .events { margin: 0 0 10px; }
  .evsum { font-family: var(--mono); font-size: 11px; color: var(--muted); cursor: pointer; }
  .ev { display: flex; gap: 8px; align-items: baseline; font-size: 11.5px;
        padding: 3px 0 3px 12px; }
  .ev .k { font-family: var(--mono); font-size: 10px; border-radius: 5px;
           padding: 1px 6px; flex: none; }
  .ev .k-recovered { background: #2EB67D22; color: #2EB67D; }
  .ev .k-reaped, .ev .k-released, .ev .k-timeout, .ev .k-lost, .ev .k-error {
           background: #D8517F22; color: #D8517F; }
  .ev .k-interrupted { background: #C08A1C22; color: var(--gold); }
  .ev .t { color: var(--muted); font-size: 10.5px; flex: none; }
  .ev .d { color: var(--ink); opacity: .8; }
  .turnbar .what { color: var(--muted); font-family: var(--mono); font-size: 11px;
                   overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
                   max-width: 40%; }
  .card .meta { color: var(--muted); font-size: 12px; margin-top: 4px; display: flex;
                gap: 10px; flex-wrap: wrap; }
  .card .meta b { color: var(--gold); font-weight: 600; }

  main { flex: 1; overflow-y: auto; padding: 18px 22px; min-width: 0;
         display: flex; flex-direction: column; }
  #content { flex: 1; }

  /* dashboard */
  #dash { border: 1px solid var(--line); border-radius: 12px; background: var(--panel);
          padding: 14px 16px; margin-bottom: 16px; }
  .tiles { display: flex; gap: 26px; flex-wrap: wrap; margin-bottom: 12px; }
  .tile .n { font-size: 24px; font-weight: 650; font-variant-numeric: tabular-nums; }
  .tile .l { font-size: 11px; text-transform: uppercase; letter-spacing: .8px;
             color: var(--muted); }
  .legend { display: flex; gap: 16px; font-size: 12px; color: var(--muted);
            margin-bottom: 6px; align-items: center; }
  .legend .sw { display: inline-block; width: 10px; height: 10px; border-radius: 3px;
                margin-right: 5px; vertical-align: -1px; }
  .legend button { margin-left: auto; background: none; border: 1px solid var(--line);
                   color: var(--muted); border-radius: 6px; padding: 2px 10px;
                   font-size: 11px; cursor: pointer; }
  .chart { display: flex; align-items: flex-end; gap: 6px; height: 120px;
           border-bottom: 1px solid var(--line2); position: relative; }
  .col { flex: 1; display: flex; flex-direction: column-reverse; gap: 2px;
         height: 100%; justify-content: flex-start; min-width: 8px; }
  .seg { border-radius: 3px 3px 0 0; min-height: 0; }
  .col:hover { filter: brightness(1.25); }
  .xlabels { display: flex; gap: 6px; font-size: 10px; color: var(--muted);
             font-family: var(--mono); margin-top: 4px; }
  .xlabels span { flex: 1; text-align: center; min-width: 8px; }
  #tip { position: fixed; background: #1E0620; border: 1px solid var(--line);
         border-radius: 8px; padding: 8px 10px; font-size: 12px; pointer-events: none;
         display: none; z-index: 10; font-family: var(--mono); }
  .dashtable { width: 100%; border-collapse: collapse; font-size: 12px;
               font-family: var(--mono); margin-top: 8px; }
  .dashtable td, .dashtable th { padding: 3px 10px 3px 0; text-align: right;
               border-bottom: 1px solid var(--line2); font-variant-numeric: tabular-nums; }
  .dashtable th { color: var(--muted); font-weight: 500; }
  .dashtable td:first-child, .dashtable th:first-child { text-align: left; }
  .modelsplit { font-size: 12px; color: var(--muted); margin-top: 10px;
                font-family: var(--mono); }

  /* transcript */
  .toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 14px;
             flex-wrap: wrap; }
  .toolbar code { font-family: var(--mono); font-size: 12px; background: var(--panel);
                  border: 1px solid var(--line); border-radius: 8px; padding: 6px 10px;
                  overflow-x: auto; white-space: nowrap; max-width: 100%; }
  button.act { background: var(--gold); color: #3D1140; border: 0; border-radius: 8px;
               padding: 6px 12px; font-weight: 600; cursor: pointer; font-size: 13px; }
  button.ghost { background: transparent; color: var(--muted);
                 border: 1px solid var(--line); border-radius: 8px; padding: 6px 12px;
                 font-size: 13px; cursor: pointer; }
  button.ghost:hover { color: var(--ink); border-color: var(--gold); }
  a { color: var(--gold); }

  .msg { max-width: 800px; margin: 0 0 14px; }
  .msg .who { font-size: 11px; text-transform: uppercase; letter-spacing: .8px;
              color: var(--muted); margin-bottom: 4px; }
  .bubble { border-radius: 12px; padding: 10px 14px; word-break: break-word; }
  .bubble p { margin: 0 0 8px; } .bubble p:last-child { margin: 0; }
  .bubble pre { background: #1E0620; border-radius: 8px; padding: 10px;
                overflow-x: auto; font-size: 12.5px; font-family: var(--mono); }
  .user .bubble pre { background: #E3D5BE; color: #33203B; }
  .bubble code { font-family: var(--mono); font-size: .92em; background: #00000030;
                 border-radius: 4px; padding: 1px 5px; }
  .bubble pre code { background: none; padding: 0; }
  .bubble ul { margin: 4px 0; padding-left: 22px; }
  .bubble h4 { margin: 10px 0 4px; font-size: 1.02em; }
  .user .bubble { background: var(--silk); color: #33203B; }
  .assistant .bubble { background: var(--panel); border: 1px solid var(--line); }
  .usage { font-family: var(--mono); font-size: 11px; color: var(--muted); margin-top: 4px; }
  details { margin: 6px 0; }
  summary { cursor: pointer; font-family: var(--mono); font-size: 12px; color: var(--gold); }
  summary.result { color: #2EB67D; }
  summary.think { color: var(--muted); }
  details pre { background: #1E0620; border-radius: 8px; padding: 10px;
                overflow-x: auto; font-size: 12px; margin: 6px 0 0;
                font-family: var(--mono); white-space: pre-wrap; }
  .empty { color: var(--muted); margin-top: 60px; text-align: center; }

  .artifacts { border-top: 1px solid var(--line); margin-top: 18px; padding-top: 10px;
               font-size: 13px; }
  .artifacts .f { display: flex; gap: 10px; font-family: var(--mono); font-size: 12px;
                  padding: 3px 0; color: var(--muted); }

  /* composer */
  #composer { flex: none; display: none; flex-direction: column; gap: 6px; padding-top: 12px;
              border-top: 1px solid var(--line); margin-top: 8px; }
  #composer .crow { display: flex; gap: 8px; align-items: flex-end; }
  label.attach { display: inline-flex; align-items: center; justify-content: center;
                 cursor: pointer; font-size: 18px; padding: 6px 10px; min-width: 44px;
                 min-height: 44px; }
  label.attach input { display: none; }
  #chips, .tform .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip { display: inline-flex; gap: 6px; align-items: center; background: var(--panel);
          border: 1px solid var(--line); border-radius: 14px; padding: 2px 4px 2px 10px;
          font-size: 12px; font-family: var(--mono); max-width: 100%; }
  .chip span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 220px; }
  .chip button { background: none; border: 0; color: var(--muted); cursor: pointer;
                 font-size: 15px; padding: 0 6px; }
  /* the live end of the open thread: what was just sent, and what it is doing */
  #tlive .msg.out .bubble { opacity: .82; }
  #tlive .state { font-size: 12px; color: var(--muted); margin-top: 4px; }
  #tlive .state.bad { color: #D8517F; }
  #tlive .note .bubble { background: transparent; border: 1px dashed var(--line);
                         font-size: 13.5px; }
  .working { display: flex; gap: 10px; align-items: center; color: var(--gold);
             font-size: 13px; margin: 0 0 14px; }
  .working .dots::after { content: "…"; animation: blink 1.2s steps(3) infinite; }
  @keyframes blink { 50% { opacity: .2; } }
  .listtop { display: flex; gap: 8px; padding: 10px 10px 0; }
  .listtop button { flex: 1; }
  #dashm { display: none; margin: 8px 10px 0; }
  #dashm summary { font-family: inherit; font-size: 13px; color: var(--ink);
                   background: var(--panel); border: 1px solid var(--line);
                   border-radius: 10px; padding: 8px 12px; }
  #dashphone { border: 1px solid var(--line); border-radius: 12px; background: var(--panel);
               padding: 12px; margin-top: 6px; overflow-x: auto; }
  #composer textarea { flex: 1; background: var(--panel); color: var(--ink);
                       border: 1px solid var(--line); border-radius: 10px;
                       padding: 9px 12px; font: inherit; font-size: 14px; resize: none;
                       height: 44px; }
  #composer textarea:focus { outline: none; border-color: var(--gold); }

  #searchresults { position: fixed; top: 52px; right: 20px; width: 480px;
                   max-height: 60vh; overflow-y: auto; background: #1E0620;
                   border: 1px solid var(--line); border-radius: 10px; z-index: 20;
                   display: none; }
  #searchresults .hit { padding: 9px 12px; border-bottom: 1px solid var(--line2);
                        cursor: pointer; font-size: 12.5px; }
  #searchresults .hit:hover { background: var(--panel); }
  #searchresults .hit .k { font-family: var(--mono); font-size: 11px; color: var(--gold); }
  #toast { position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%);
           background: #1E0620; border: 1px solid var(--gold); color: var(--ink);
           border-radius: 8px; padding: 8px 16px; font-size: 13px; display: none; z-index: 30; }

  #taskmodal { position: fixed; inset: 0; background: #14041699; z-index: 40;
               display: none; align-items: flex-start; justify-content: center; padding: 60px 20px; }
  #taskmodal .box { background: var(--bg); border: 1px solid var(--line); border-radius: 14px;
                    width: min(860px, 100%); max-height: 80vh; overflow: auto; padding: 22px 24px; }
  #taskmodal h2 { margin: 0 0 4px; font-size: 18px; }
  #taskmodal .hint { color: var(--muted); font-size: 13px; margin-bottom: 14px; }
  .seg { display: inline-flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
  .seg button { background: none; border: 0; color: var(--muted); font: inherit; font-size: 12px;
                padding: 3px 12px; cursor: pointer; }
  .seg button.on { background: #C08A1C22; color: var(--gold); }
  #taskbadge { margin-left: 6px; font-size: 10px; font-family: var(--mono);
               background: #D8517F; color: #fff; border-radius: 8px; padding: 1px 6px; }
  #taskbadge:empty { display: none; }
  .task { display: flex; align-items: baseline; gap: 10px; padding: 9px 2px;
          border-bottom: 1px solid var(--line); font-size: 13px; }
  .task .st { font-family: var(--mono); font-size: 10px; border-radius: 5px;
              padding: 1px 7px; flex: none; }
  .st-proposed { background: #C08A1C22; color: var(--gold); }
  .st-awaiting_approval, .st-needs_input { background: #2D9FD022; color: #2D9FD0; }
  .st-failed { background: #D8517F22; color: #D8517F; }
  .st-running { background: #2EB67D22; color: #2EB67D; }
  .st-queued, .st-done, .st-cancelled, .st-blocked { background: #8881; color: var(--muted); }
  .task .tt { flex: 1; }
  .task .sub { color: var(--muted); font-size: 11px; font-family: var(--mono); }
  .task .proj { color: var(--gold); }
  /* The disclosure holding the goal. Shut by default -- fifty proposals each
     showing three thousand characters is only a different way of being
     unreadable -- but one click away, because Accept and Dismiss both cost
     real money and the title is a sentence fragment. */
  .task .more > summary { cursor: pointer; list-style-position: outside; }
  .task .more > summary::marker { color: var(--muted); }
  .task .goal { margin: 7px 0 0; padding: 8px 10px; max-height: 360px; overflow-y: auto;
                white-space: pre-wrap; word-break: break-word;
                font-family: var(--mono); font-size: 11.5px; line-height: 1.5;
                color: var(--ink); background: #8881; border-radius: 6px; }
  /* Why it stopped wears the same treatment as a flagged review: both answer
     "what happened here" for a row that is asking you to decide something. */
  .task .rev, .task .why { margin-top: 6px; font-size: 11.5px; line-height: 1.45; color: var(--ink);
               background: #D8517F14; border-left: 2px solid #D8517F;
               border-radius: 0 5px 5px 0; padding: 6px 9px; }
  /* A passed review is not a warning, so it does not wear the flagged colour --
     but it still shows, because it can carry what the reviewer found anyway. */
  .task .rev.ok { background: #2EB67D14; border-left-color: #2EB67D; }
  .task .rev ul { margin: 4px 0 0; padding-left: 16px; }
  .task .rev .lbl { display: block; margin-top: 5px; color: var(--muted);
                    font-size: 11px; }
  .task .rev .filed { font-family: var(--mono); color: var(--muted); font-size: 11px; }
  .task .land { margin-top: 6px; font-size: 11.5px; line-height: 1.45;
                font-family: var(--mono); color: var(--muted); }
  .task .land.bad { color: #D8517F; }
  .task .land.ok { color: #2EB67D; }
  .task .land .d { color: var(--muted); font-family: var(--mono); font-size: 10.5px;
                   white-space: pre-wrap; margin-top: 3px; }
  #tproj { background: var(--bg); color: var(--ink); border: 1px solid var(--line);
           border-radius: 8px; font: inherit; font-size: 12px; padding: 3px 8px; }
  /* projects overview and per-project boards */
  #boardmodal { position: fixed; inset: 0; background: #14041699; z-index: 40;
                display: none; align-items: flex-start; justify-content: center; padding: 30px 16px; }
  #boardmodal .box { background: var(--bg); border: 1px solid var(--line); border-radius: 14px;
                     width: min(1560px, 100%); max-height: 90vh; overflow: auto; padding: 18px 20px; }
  #boardmodal h2 { margin: 0 0 4px; font-size: 18px; display: flex; align-items: center;
                   gap: 10px; flex-wrap: wrap; }
  #boardmodal .hint { color: var(--muted); font-size: 13px; margin-bottom: 12px; }
  .bfilters { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }
  .bfilters select, .bfilters input { background: var(--panel); color: var(--ink);
    border: 1px solid var(--line); border-radius: 8px; font: inherit; font-size: 12.5px;
    padding: 4px 8px; }
  .bfilters input { width: 240px; }
  .pcards { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(320px, 100%), 1fr));
            gap: 12px; }
  .pcard { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
           padding: 11px 13px; font-size: 12.5px; cursor: pointer; }
  .pcard:hover { border-color: var(--gold); }
  .pcard h3 { margin: 0 0 6px; font-size: 15px; display: flex; gap: 8px; align-items: baseline; }
  .pcard h3 .slug { font-family: var(--mono); font-size: 10.5px; color: var(--muted); font-weight: 400; }
  .pcard .row { margin: 4px 0; color: var(--ink); }
  .pcard .lbl { font-family: var(--mono); font-size: 10px; color: var(--muted);
                text-transform: uppercase; letter-spacing: .06em; margin-right: 6px; }
  .pcard .chips { display: flex; gap: 4px; flex-wrap: wrap; }
  .pcard .on { color: #2EB67D; } .pcard .off { color: var(--muted); }
  .pcard .warn { color: var(--gold); }
  .bcols { display: grid; grid-template-columns: repeat(5, minmax(210px, 1fr)); gap: 10px;
           align-items: start; overflow-x: auto; }
  .bcol { background: #00000018; border: 1px solid var(--line2); border-radius: 10px;
          padding: 8px; min-height: 80px; }
  .bcol > h4 { margin: 0 0 8px; font-size: 12px; font-family: var(--mono); color: var(--muted);
               text-transform: uppercase; letter-spacing: .06em; }
  .bcard { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
           padding: 8px 9px; margin-bottom: 7px; font-size: 12.5px; cursor: pointer; }
  .bcard:hover { border-color: var(--gold); }
  .bcard .tt { font-weight: 600; word-break: break-word; }
  .bcard .sub { color: var(--muted); font-size: 10.5px; font-family: var(--mono); margin-top: 3px; }
  .bcard .proj { color: var(--gold); }
  .bcard .rv { margin-top: 4px; font-size: 11px; }
  .bcard .rv.ok { color: #2EB67D; } .bcard .rv.bad { color: #D8517F; }
  .bcard .acts { margin-top: 6px; display: flex; gap: 4px; flex-wrap: wrap; cursor: default; }
  .bcard .acts button { font-size: 11px; padding: 2px 8px; }
  .bcard .land, #bdetail .land { margin-top: 4px; font-size: 11px; font-family: var(--mono);
                                 color: var(--muted); }
  .bcard .land.ok, #bdetail .land.ok { color: #2EB67D; }
  .bcard .land.bad, #bdetail .land.bad { color: #D8517F; }
  .bcard .land .d { display: none; }
  .bcard .why { margin-top: 4px; font-size: 11px; color: #D8517F; }
  /* A step a run left for you: the colour of the state that holds it. */
  .todo { margin-top: 5px; font-size: 11.5px; line-height: 1.45; color: var(--ink);
          background: #2D9FD014; border-left: 2px solid #2D9FD0;
          border-radius: 0 5px 5px 0; padding: 5px 9px; word-break: break-word; }
  .todo code { font-family: var(--mono); }
  .bthreads { margin-top: 14px; }
  .bthreads .b { display: inline-block; font-size: 12px; margin: 0 6px 6px 0; }
  #bdetail { position: fixed; top: 0; right: 0; bottom: 0; width: min(680px, 96vw);
             background: var(--bg); border-left: 1px solid var(--gold); z-index: 45;
             overflow-y: auto; padding: 18px 20px; display: none; font-size: 13px; }
  #bdetail h3 { margin: 0 0 6px; font-size: 16px; }
  #bdetail .sub { color: var(--muted); font-size: 11px; font-family: var(--mono); }
  #bdetail .goal { margin: 8px 0; padding: 8px 10px; white-space: pre-wrap; word-break: break-word;
                   font-family: var(--mono); font-size: 11.5px; background: #8881; border-radius: 6px;
                   max-height: 360px; overflow-y: auto; }
  #bdetail .rev { margin-top: 6px; font-size: 12px; line-height: 1.45; background: #D8517F14;
                  border-left: 2px solid #D8517F; border-radius: 0 5px 5px 0; padding: 6px 9px; }
  #bdetail .rev.ok { background: #2EB67D14; border-left-color: #2EB67D; }
  #bdetail .rev ul { margin: 4px 0 0; padding-left: 16px; }
  #bdetail .rev .lbl { display: block; margin-top: 5px; color: var(--muted); font-size: 11px; }
  #bdetail .land .d { white-space: pre-wrap; font-size: 10.5px; margin-top: 3px; }
  .bhead { display: flex; gap: 10px; align-items: center; margin-bottom: 10px; }
  .bhead h3 { margin: 0; font-size: 15px; }
  .bhead .warn, .tform .warn { color: var(--gold); font-size: 12px; }
  .bcol > h4 button { float: right; font-size: 11px; padding: 0 6px; }
  .bcard .qpos { color: var(--gold); }
  .pcard h3 .gear { margin-left: auto; font-size: 12px; padding: 0 6px; }
  .parch { margin-top: 12px; color: var(--muted); font-size: 13px; }
  .parch summary { cursor: pointer; }
  .parch .arow { display: flex; gap: 8px; align-items: center; margin: 4px 0 0 14px; }
  #fmodal { position: fixed; inset: 0; background: #14041699; z-index: 50; display: none;
            align-items: flex-start; justify-content: center; padding: 60px 20px; }
  #fmodal .fbox { background: var(--bg); border: 1px solid var(--gold); border-radius: 14px;
                  width: 600px; max-width: 100%; max-height: 84vh; overflow-y: auto; padding: 18px 22px; }
  #fmodal h3 { margin: 0 0 4px; font-size: 17px; }
  .tform .choice { display: flex; gap: 8px; align-items: flex-start; margin-top: 6px;
                   color: var(--ink); font-size: 13px; }
  .tform .choice input { margin-top: 3px; }
  .tform .fixed { margin-top: 4px; font-weight: 600; }
  .tform label { display: block; margin-top: 10px; font-size: 12px; color: var(--muted); }
  .tform input[type=text], .tform select, .tform textarea {
        width: 100%; background: var(--panel); color: var(--ink); border: 1px solid var(--line);
        border-radius: 8px; padding: 7px 9px; font: inherit; font-size: 13px; }
  .tform textarea { min-height: 150px; font-family: var(--mono); font-size: 12px; }
  .tform .line { display: flex; gap: 8px; align-items: center; margin-top: 6px; }
  .tform .line input[type=text] { flex: 1; }
  .tform .note { color: var(--muted); font-size: 11px; margin-top: 3px; }
  .tform .err { color: #D8517F; font-size: 12px; margin-top: 8px; white-space: pre-wrap; }
  .tform .acts { margin-top: 14px; display: flex; gap: 8px; }
  .tform .rtarget { border-top: 1px solid var(--line2); margin-top: 10px; padding-top: 6px; }
  .tform .reffects { margin: 8px 0; padding-left: 18px; font-size: 13px; }
  .tform .on { color: #2EB67D; }
  .spin { display: inline-block; animation: spin 1.2s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  #bdetail .sect { margin-top: 14px; font-family: var(--mono); font-size: 10.5px; color: var(--muted);
                   text-transform: uppercase; letter-spacing: .06em; }
  #learnmodal { position: fixed; inset: 0; background: #14041699; z-index: 40;
                display: none; align-items: flex-start; justify-content: center; padding: 60px 20px; }
  #learnmodal .box { background: var(--bg); border: 1px solid var(--line); border-radius: 14px;
                     width: 720px; max-width: 100%; max-height: 80vh; overflow-y: auto; padding: 20px 22px; }
  #learnmodal h2 { margin: 0 0 4px; font-size: 18px; }
  #learnmodal .hint { color: var(--muted); font-size: 13px; margin-bottom: 14px; }
  .lform { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; align-items: center; }
  .lform select, .lform input { background: var(--panel); color: var(--ink);
        border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; font: inherit; font-size: 14px; }
  .lform input.text { flex: 1; min-width: 200px; }
  .lform input.scope { width: 230px; font-family: var(--mono); font-size: 12px; }
  .lrow { display: flex; gap: 10px; align-items: flex-start; padding: 8px 0;
          border-bottom: 1px solid var(--line2); font-size: 14px; }
  .lrow .t { font-family: var(--mono); font-size: 11px; padding: 2px 8px; border-radius: 6px; flex: none; }
  .lrow .t.do { background: #2EB67D22; color: #2EB67D; }
  .lrow .t.avoid { background: #D8517F22; color: #D8517F; }
  .lrow .t.note { background: #2D9FD022; color: #2D9FD0; }
  .lrow .body { flex: 1; }
  .lrow .scope { font-family: var(--mono); font-size: 11px; color: var(--muted); }
  .lrow.off { opacity: .45; }
  .lrow .del { cursor: pointer; color: var(--muted); border: 0; background: none; font-size: 15px; }
  .lrow .del:hover { color: #D8517F; }
  .lrow .tog { cursor: pointer; border: 0; background: none; font-size: 15px; flex: none; }
  .lrow .origin { font-family: var(--mono); font-size: 10px; padding: 1px 6px; border-radius: 5px;
                  flex: none; border: 1px solid var(--line); color: var(--muted); }
  .lrow .origin.harvest { color: var(--gold); border-color: #C08A1C66; }

  /* --- phone and home-screen app --------------------------------------
     The page's own width rules above are for a laptop. Below 700px the list
     and the open thread take turns (body.reading, set by showReading), the
     boards stack their columns, and every panel is the whole screen with its
     own close button. env(safe-area-inset-*) is 0 in a browser tab and the
     notch / home indicator in the standalone app (viewport-fit=cover plus
     the black-translucent status bar put the page under both). */
  body { height: 100dvh; }
  #backbtn { display: none; }
  .xclose { float: right; margin-left: 8px; }
  header { padding-top: max(10px, env(safe-area-inset-top));
           padding-left: max(20px, env(safe-area-inset-left));
           padding-right: max(20px, env(safe-area-inset-right)); }
  #composer { padding-bottom: env(safe-area-inset-bottom); }
  #toast { bottom: calc(18px + env(safe-area-inset-bottom)); }
  .task.flash { outline: 2px solid var(--gold); outline-offset: 2px; }
  #notifybtn.on { color: var(--gold); }
  /* Phone-only controls: the header's search and overflow buttons and the
     board's column picker. Hidden here, so the laptop page is unchanged. */
  .phoneonly, .bpick { display: none; }
  .hmore { display: contents; }
  /* The phone's chat: its thread header, alert count and sheet, jump pill. */
  #alertslot, #chathead, #jumppill, #alertsheet { display: none; }

  /* --- phone (max-width: 700px) ----------------------------------------
     One breakpoint, one set of measures. On a phone:
     - the header is one row: brand, Tasks, Projects, 🔍 (search drops in
       as a second row only while open) and ⋯ (Learnings, Name untitled,
       Notify, as a menu);
     - titles and prose are the proportional face and wrap on spaces; ids,
       keys and hashes stay monospace and ellipsize rather than break;
     - chip rows scroll sideways inside themselves instead of wrapping;
     - the board shows one column at a time, picked from a row of tabs;
     - panels and forms are full-screen sheets, their actions pinned to
       the bottom;
     - anything tapped is at least --tap tall, and every field is --field-fs
       so iOS does not zoom the page on focus. */
  @media (max-width: 700px) {
    :root {
      --tap: 44px;                     /* Apple's minimum hit target */
      --field-fs: 16px;                /* below this, iOS zooms in on focus */
      --gutter: 12px;
      --sans: -apple-system, "Segoe UI", sans-serif;
      --safe-l: max(var(--gutter), env(safe-area-inset-left));
      --safe-r: max(var(--gutter), env(safe-area-inset-right));
      --safe-t: max(var(--gutter), env(safe-area-inset-top));
      --safe-b: env(safe-area-inset-bottom);
    }
    body { font-size: 15px; }
    html, body { overflow-x: hidden; }

    /* header: one row */
    header { position: relative; flex-wrap: nowrap; gap: 4px; padding: 6px var(--safe-r) 6px var(--safe-l);
             padding-top: max(6px, env(safe-area-inset-top)); }
    header svg { width: 30px; height: 20px; }
    /* the logo stands for the name: the row holds the alerts' count and ✏️ */
    header h1 { display: none; }
    #botdot { margin: 0 4px; }
    header .lbl, header .wide { display: none; }
    header button.hbtn { margin-left: 0 !important; min-width: var(--tap); min-height: var(--tap);
                         padding: 0 9px; font-size: 18px; border-color: transparent;
                         display: inline-flex; align-items: center; justify-content: center; }
    header .hbtn:first-of-type { margin-left: auto !important; }
    header .phoneonly { display: inline-flex; }
    #taskbadge { font-size: 11px; }
    .hmore { display: none; }
    header.menu .hmore { display: flex; flex-direction: column; position: absolute; z-index: 35;
                         top: calc(100% - 4px); right: var(--safe-r); min-width: 220px;
                         background: #1E0620; border: 1px solid var(--line); border-radius: 12px;
                         padding: 6px; box-shadow: 0 10px 30px #0008; }
    header.menu .hmore button.hbtn { justify-content: flex-start; gap: 10px; font-size: 16px;
                                     padding: 0 12px; color: var(--ink); width: 100%; }
    header.menu .hmore .lbl, header.menu .hmore .wide { display: inline; }
    #search { display: none; order: 9; flex: 1 1 100%; width: auto; margin: 4px 0 2px;
              min-height: var(--tap); }
    body.searching header { flex-wrap: wrap; }
    body.searching #search { display: block; }
    #searchresults { left: 8px; right: 8px; width: auto; }
    #searchresults .hit { overflow-wrap: anywhere; }
    /* alerts: a count in the header (⚠ 3), opened as a sheet; never a bar */
    #alertbar { display: none; }
    header #alertslot { display: contents; }
    header button.hbtn.alertbtn { color: #D8517F; font-size: 14px; font-weight: 650; padding: 0 6px;
                                  min-width: 40px; white-space: nowrap; border-color: #D8517F55; }
    #alertsheet.open { display: flex; position: fixed; inset: 0; z-index: 45; background: var(--bg); }
    #alertsheet .box { width: 100%; overflow-y: auto; overscroll-behavior: contain;
      padding: var(--safe-t) var(--safe-r) calc(18px + var(--safe-b)) var(--safe-l); }
    #alertsheet h2 { font-size: 20px; margin: 6px 0 14px; }
    #alertsheet .aitem { display: flex; flex-direction: column; align-items: stretch; gap: 8px;
                         margin-bottom: 18px; }
    #alertsheet .alert { font-size: 14px; padding: 8px 12px; border-radius: 10px; }
    #alertsheet .aitem button.ghost { text-align: left; color: var(--ink); font-size: 14.5px;
                                      padding: 8px 12px; overflow-wrap: anywhere; }

    /* the list and the open thread take turns */
    .layout { display: block; flex: 1; overflow: hidden; }
    .listcol { height: 100%; }
    .listtop { padding: 10px var(--safe-r) 0 var(--safe-l); }
    #dashm { margin: 8px var(--safe-r) 0 var(--safe-l); }
    #dashm summary { min-height: var(--tap); display: flex; align-items: center;
                     white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    nav { width: auto; border-right: 0; flex: 1; min-height: 0;
          padding: 8px var(--safe-r) calc(10px + var(--safe-b)) var(--safe-l); }
    main { display: none; height: 100%; padding: 8px var(--safe-r) 0 var(--safe-l); }
    body.reading .listcol { display: none; }
    body.reading main { display: flex; }
    #dash { display: none; }
    #dashm { display: block; }
    #dashphone .tiles { gap: 14px 22px; }
    #dashphone .tile .n { font-size: 20px; }
    #dashphone .xlabels span { white-space: nowrap; overflow: visible; }

    /* chip rows: one line, scrolled sideways inside themselves */
    #kindfilter {
      flex-wrap: nowrap; overflow-x: auto; scrollbar-width: none;
      -webkit-overflow-scrolling: touch; overscroll-behavior-x: contain; }
    #kindfilter::-webkit-scrollbar, .bpick::-webkit-scrollbar { display: none; }
    #kindfilter { padding: 8px var(--safe-r) 2px var(--safe-l); gap: 6px; }
    #kindfilter button, #nightly button, #unmerged .b, #holding .b {
      flex: none; white-space: nowrap; font-family: var(--sans); font-size: 13px;
      min-height: var(--tap); padding: 0 12px; border-radius: 10px; }
    /* the task panel's strips are actions, not filters: they wrap, under
       their label */
    #nightly .nlabel, #unmerged .nlabel, #holding .nlabel {
      flex: 1 1 100%; font-family: var(--sans); font-size: 13px; }
    #nightly, #unmerged, #holding { padding-bottom: 8px; }

    /* session cards: title on the first line beside its ×, badges below */
    .card { padding: 10px 6px 10px 12px; }
    .card .key { flex-wrap: wrap; row-gap: 4px; font-family: var(--sans); word-break: normal; }
    .card .key .title { flex: 1 1 calc(100% - var(--tap) - 6px); min-width: 0; font-size: 15px;
                        line-height: 1.3; overflow-wrap: break-word; }
    .card .key .untitled { flex: 1 1 calc(100% - var(--tap) - 6px); }
    .card .key .hidebtn { order: 1; flex: none; width: var(--tap); height: var(--tap);
                          margin: -10px -2px -6px 0; font-size: 20px; padding: 0; }
    .card .key .badge { order: 2; }
    .card .subkey { white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
                    word-break: normal; margin-right: 6px; }
    .card .summary { font-size: 13px; margin-right: 6px; }
    .card .meta { font-size: 13px; gap: 4px 12px; }
    .badge { font-family: var(--sans); font-size: 11.5px; padding: 2px 7px; }

    /* the open thread */
    .toolbar { gap: 6px; }
    .toolbar code { flex: 1 1 100%; min-width: 0; white-space: nowrap; overflow: hidden;
                    text-overflow: ellipsis; word-break: normal; }
    .msg { max-width: 100%; }
    .bubble { overflow-wrap: break-word; word-break: normal; }
    .bubble pre, details pre { white-space: pre-wrap; overflow-wrap: anywhere; }
    .turnbar .what { max-width: 100%; }
    .ctx { overflow-wrap: break-word; }
    .ctx button.ghost { min-height: 36px; padding: 0 10px; }
    .ev { flex-wrap: wrap; row-gap: 0; }
    .ev .d { flex: 1 1 100%; min-width: 0; overflow-wrap: anywhere; padding-left: 2px; }
    .artifacts .f { flex-wrap: wrap; overflow-wrap: anywhere; }
    #transcript { scroll-margin-bottom: 130px; }   /* clear of the composer */
    #composer { position: sticky; bottom: 0; background: var(--bg);
                padding-bottom: max(10px, var(--safe-b)); }
    body.kb #composer { padding-bottom: 8px; }      /* the keyboard covers the indicator */
    #composer textarea { min-height: var(--tap); }
    #composer .crow button.act { min-width: 64px; }

    /* the open thread is a chat: it has the screen, under its own header */
    body { height: var(--vvh, 100dvh); }
    body.reading header { display: none; }
    body.reading main { padding-top: 0; }
    #backbtn, body.reading #backbtn { display: none; }
    body.reading #thead { display: none; }
    body.reading.threadinfo #thead { display: block; padding-top: 10px; }
    body.reading #chathead { display: flex; align-items: center; gap: 4px; position: sticky;
      top: 0; z-index: 6; background: var(--bg); border-bottom: 1px solid var(--line);
      margin: 0 calc(-1 * var(--safe-r)) 0 calc(-1 * var(--safe-l));
      padding: max(6px, env(safe-area-inset-top)) var(--safe-r) 6px max(2px, env(safe-area-inset-left)); }
    #chathead button { background: none; border: 0; color: var(--ink); cursor: pointer;
                       min-width: var(--tap); min-height: var(--tap); padding: 0 8px; flex: none; }
    #chathead .cback { font-size: 34px; line-height: 1; color: var(--gold); padding: 0 6px 4px; }
    #chathead .ctitle { flex: 1; min-width: 0; display: flex; flex-direction: column; }
    #chathead .ctitle b { font-size: 16px; font-weight: 650; white-space: nowrap; overflow: hidden;
                          text-overflow: ellipsis; }
    #chathead .ctitle b.untitled { color: var(--muted); font-style: italic; font-weight: 500; }
    #chathead .csub { display: flex; gap: 6px; align-items: center; font-size: 12px;
                      color: var(--muted); white-space: nowrap; overflow: hidden; }
    #chathead .cproj { border: 1px solid var(--line); border-radius: 6px; padding: 0 6px;
                       color: var(--silk); overflow: hidden; text-overflow: ellipsis; flex: 0 1 auto;
                       min-width: 0; }
    #chathead .cst { flex: none; }
    #chathead .cst.run { color: #2EB67D; }
    #chathead .cst.bad { color: #D8517F; }
    #chathead .calert { color: #D8517F; font-weight: 650; font-size: 14px; padding: 0 4px;
                        min-width: 40px; }
    #chathead .cstop { border: 1px solid var(--line); border-radius: 10px; font-size: 14px;
                       min-height: 36px; padding: 0 10px; }
    #chathead .cinfo { font-size: 18px; color: var(--muted); padding: 0 6px; min-width: 38px; }
    body.reading #transcript { padding-top: 10px; }
    .cmsg { display: flex; flex-direction: column; align-items: flex-start; max-width: 92%;
            margin: 0 0 12px; }
    .cmsg.user { margin-left: auto; align-items: flex-end; max-width: 85%; }
    .cmsg .bubble { font-size: 15.5px; line-height: 1.45; border-radius: 18px; padding: 9px 13px;
                    max-width: 100%; }
    .cmsg.user .bubble { border-bottom-right-radius: 6px; }
    .cmsg.assistant .bubble { border-bottom-left-radius: 6px; }
    .cmsg .bubble pre { font-size: 12.5px; }
    .cmeta { font-size: 11.5px; color: var(--muted); margin: 3px 8px 0; }
    .cmeta details { margin: 0; }
    .cmeta summary { font-family: var(--sans); font-size: 11.5px; color: var(--muted);
                     list-style: none; }
    .cmeta summary::-webkit-details-marker { display: none; }
    .ctools { margin: 0 0 12px; }
    .ctl { background: none; border: 1px solid var(--line2); border-radius: 16px;
           color: var(--muted); font-size: 13px; min-height: 34px; padding: 0 12px;
           max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
           cursor: pointer; }
    .ctools.open .ctl { color: var(--gold); border-color: #C08A1C99; }
    .cdetail { border-left: 2px solid var(--line); padding: 8px 0 0 10px; margin: 6px 0 0 6px; }
    .cdetail .msg { margin-bottom: 10px; }
    body.reading #tlive .msg.user { margin-left: auto; max-width: 85%; display: flex;
                                    flex-direction: column; align-items: flex-end; }
    body.reading #tlive .msg .who { font-size: 10.5px; }
    body.reading #tlive .msg.user .bubble { border-radius: 18px; border-bottom-right-radius: 6px; }
    body.reading .working button { display: none; }   /* Stop is in the header */
    .cfiles { margin: 6px 0 4px; }
    .cfiles summary { font-family: var(--sans); font-size: 13px; color: var(--muted);
                      min-height: 32px; display: flex; align-items: center; }
    .cfiles .artifacts { border-top: 0; margin-top: 0; padding-top: 2px; }
    #jumppill { position: absolute; bottom: calc(100% + 8px); left: 50%;
                transform: translateX(-50%); z-index: 7; background: var(--panel);
                color: var(--ink); border: 1px solid var(--gold); border-radius: 18px;
                min-height: 36px; padding: 0 14px; font-size: 13.5px; box-shadow: 0 4px 14px #0008;
                white-space: nowrap; }
    #jumppill.show { display: block; }
    #composer .crow { align-items: flex-end; gap: 6px; }
    #composer textarea { height: auto; min-height: var(--tap); line-height: 22px;
                         padding: 10px 12px; border-radius: 20px; resize: none;
                         overflow-y: hidden; font-family: var(--sans); }
    #composer .crow button.act { min-height: var(--tap); border-radius: 20px; }
    #composer label.attach { padding: 0; }

    /* panels: whole-screen sheets, scrolling inside, closed by their ✕ */
    #taskmodal, #boardmodal, #learnmodal, #fmodal { padding: 0; }
    #taskmodal .box, #boardmodal .box, #learnmodal .box, #fmodal .fbox {
      width: 100%; max-width: 100%; height: 100%; max-height: none; border-radius: 0;
      border: 0; -webkit-overflow-scrolling: touch; overscroll-behavior: contain;
      padding: var(--safe-t) var(--safe-r) calc(18px + var(--safe-b)) var(--safe-l); }
    #taskmodal h2, #boardmodal h2, #learnmodal h2 { font-size: 20px; gap: 8px; }
    .xclose { position: sticky; top: 0; z-index: 2; background: var(--bg); }
    .box .hint { font-size: 13px; }
    #bdetail { width: 100vw; border-left: 0;
      padding: var(--safe-t) var(--safe-r) calc(18px + var(--safe-b)) var(--safe-l); }
    #bdetail h3 { overflow-wrap: break-word; padding-right: 4px; }
    #bdetail .sub, .task .sub, .bcard .sub { overflow-wrap: anywhere; }
    #bdetail .goal, .task .goal { font-family: var(--sans); font-size: 13.5px; }
    #bdetail .acts { display: flex; flex-wrap: wrap; gap: 6px; }

    /* the task panel */
    .task { flex-wrap: wrap; gap: 6px 8px; }
    .task .tt { flex: 1 1 calc(100% - 90px); min-width: 0; overflow-wrap: break-word; }
    .task .more > summary { list-style-position: inside;
                            font-family: var(--sans); font-size: 14.5px; color: var(--ink);
                            font-weight: 600; overflow-wrap: break-word; }
    .task .sub { overflow-wrap: anywhere; }
    #tproj { height: var(--tap); flex: 1 1 100%; padding: 0 10px; font-weight: 400; }

    /* projects: overview cards and one board column at a time */
    .bfilters select, .bfilters input { flex: 1 1 140px; width: auto; min-width: 0;
                                        min-height: var(--tap); }
    .pcard { font-size: 13.5px; }
    .pcard h3 { flex-wrap: wrap; overflow-wrap: anywhere; }
    .pcard h3 .gear { min-width: var(--tap); min-height: var(--tap); }
    .bhead { flex-wrap: wrap; }
    .bpick { display: flex; gap: 6px; overflow-x: auto; scrollbar-width: none;
             margin: 0 0 10px; padding-bottom: 2px; }
    .bpick button { flex: none; white-space: nowrap; min-height: var(--tap); padding: 0 12px;
                    font-size: 13.5px; background: transparent; color: var(--muted);
                    border: 1px solid var(--line); border-radius: 10px; }
    .bpick button.on { background: #C08A1C22; color: var(--gold); border-color: #C08A1C99; }
    .bpick button b { color: var(--ink); margin-left: 4px; }
    .bcols { grid-template-columns: 1fr; display: block; overflow: visible; }
    .bcols .bcol { display: none; background: none; border: 0; padding: 0; }
    .bcols[data-show="backlog"] .bcol[data-col="backlog"],
    .bcols[data-show="running"] .bcol[data-col="running"],
    .bcols[data-show="review"] .bcol[data-col="review"],
    .bcols[data-show="needs"] .bcol[data-col="needs"],
    .bcols[data-show="done"] .bcol[data-col="done"] { display: block; }
    .bcol > h4 { display: none; }
    .bcard { font-size: 14px; padding: 10px 11px; }
    .bcard .tt { overflow-wrap: break-word; word-break: normal; }
    .bthreads .b { min-height: var(--tap); }

    /* forms: labelled fields, the actions pinned to the bottom of the sheet */
    #fmodal .fbox { padding-bottom: 0; }
    .tform h3 { font-size: 19px; overflow-wrap: break-word; }
    .tform label { font-size: 13px; }
    .tform .choice { font-size: 14.5px; }
    .tform .choice input { width: 20px; height: 20px; flex: none; margin-top: 1px; }
    .tform .line { flex-wrap: wrap; }
    .tform .line input[type=text] { flex: 1 1 180px; min-width: 0; }
    .tform input[type=text], .tform select, .tform textarea { min-height: var(--tap); }
    .tform textarea { font-family: var(--sans); }
    .tform .note code, .tform .sub { overflow-wrap: anywhere; }
    .tform .acts { position: sticky; bottom: 0; z-index: 2; background: var(--bg);
                   margin: 14px calc(-1 * var(--safe-r)) 0 calc(-1 * var(--safe-l));
                   padding: 10px var(--safe-r) max(12px, var(--safe-b)) var(--safe-l);
                   border-top: 1px solid var(--line); flex-wrap: wrap; }
    .tform .acts button { flex: 1 1 0; }
    .tform .rtarget .note code { overflow-wrap: anywhere; }
    .tform pre.goal { white-space: pre-wrap; overflow-wrap: anywhere; }

    /* learnings */
    .lform input.text, .lform input.scope { flex: 1 1 100%; width: auto; min-width: 0; }
    .lform select { min-height: var(--tap); }
    .lrow { flex-wrap: wrap; align-items: center; gap: 4px 8px; }
    .lrow .del { order: 1; margin-left: auto; }
    .lrow .body { order: 2; flex: 1 1 100%; min-width: 0; overflow-wrap: break-word;
                  padding-left: 4px; }
    .lrow .scope { overflow-wrap: anywhere; }
    .lrow .tog, .lrow .del { min-width: var(--tap); min-height: var(--tap); }

    /* fingers, not a pointer */
    button.act, button.ghost, a.ghost { min-height: var(--tap); padding: 0 14px; font-size: 15px; }
    a.ghost { display: inline-flex; align-items: center; text-decoration: none; }
    .bcard .acts button, .seg button { min-height: var(--tap); padding: 0 12px; font-size: 14px; }
    .seg { flex: none; }
    label.attach { min-width: var(--tap); min-height: var(--tap); }
    .chip button { min-width: 36px; min-height: 36px; }
    .bcol > h4 button { min-height: 0; padding: 2px 8px; }
    input, select, textarea, #search, #composer textarea, .tform input[type=text],
      .tform select, .tform textarea, .lform select, .lform input,
      .bfilters select, .bfilters input { font-size: var(--field-fs); }
  }
  /* the narrowest phones (SE, mini): a little less room per button */
  @media (max-width: 380px) {
    header button.hbtn { min-width: 40px; padding: 0 6px; }
  }
</style>
</head>
<body>
<header>
  <svg viewBox="40 100 440 280">
    <path d="M414,254 C 442,238 452,210 438,186 C 420,155 350,128 268,140 C 186,152 108,196 74,244 C 52,276 62,310 104,314"
          fill="none" stroke="#ECB22E" stroke-width="14" stroke-linecap="round" stroke-dasharray="24 18"/>
    <circle cx="118" cy="318" r="30" fill="#EFE2CC"/><circle cx="170" cy="296" r="38" fill="#F6EEDF"/>
    <circle cx="228" cy="282" r="45" fill="#EFE2CC"/><circle cx="290" cy="276" r="51" fill="#F6EEDF"/>
    <circle cx="360" cy="286" r="57" fill="#F6EEDF"/>
    <circle cx="346" cy="270" r="7.5" fill="#331233"/><circle cx="382" cy="272" r="7.5" fill="#331233"/>
    <path d="M352,296 q12,10 26,2" fill="none" stroke="#331233" stroke-width="5" stroke-linecap="round"/>
  </svg>
  <h1>Silkworm<span class="wide"> sessions</span></h1>
  <span id="botdot" title="bot status"></span>
  <span id="alertslot"></span>
  <button class="ghost hbtn" style="margin-left:16px" onclick="toggleTasks()">📋<span class="lbl"> Tasks</span><span id="taskbadge"></span></button>
  <button class="ghost hbtn" onclick="toggleBoard()" title="per-project boards and backlogs">🗂<span class="lbl"> Projects</span></button>
  <button class="ghost hbtn phoneonly" onclick="newThreadForm()" title="new thread" aria-label="new thread">✏️</button>
  <button class="ghost hbtn phoneonly" onclick="toggleSearch()" title="search transcripts">🔍</button>
  <button class="ghost hbtn phoneonly" id="menubtn" onclick="toggleMenu(event)" title="more" aria-haspopup="true">⋯</button>
  <div class="hmore" onclick="closeMenu()">
  <button class="ghost hbtn" onclick="toggleLearn()" title="learnings">🧠<span class="lbl"> Learnings</span></button>
  <button class="ghost hbtn wide" onclick="nameAllThreads()" title="name every untitled thread">✎ Name untitled</button>
  <button class="ghost hbtn" id="notifybtn" onclick="enableNotifications()" title="Enable notifications">🔔<span class="lbl" id="notifylbl"> Notify</span></button>
  </div>
  <input id="search" placeholder="Search transcripts…" autocomplete="off">
</header>
<div id="alertbar"></div>
<div id="searchresults"></div>
<div class="layout">
  <div class="listcol">
    <div class="listtop"><button class="act" onclick="newThreadForm()">+ New thread</button></div>
    <details id="dashm"><summary id="dashsum">📊 Stats</summary><div id="dashphone"></div></details>
    <div id="kindfilter"></div>
    <nav id="list"></nav>
  </div>
  <main>
    <button id="backbtn" class="ghost" onclick="closeSession()">‹ Sessions</button>
    <div id="chathead"></div>
    <div id="content">
      <div id="dash"></div>
      <div id="transcript"><div id="thead"><div class="empty">Pick a session on the left.</div></div><div id="tmsgs"></div><div id="tlive"></div><div id="tfiles"></div></div>
    </div>
    <div id="composer">
      <button id="jumppill" onclick="toEnd()">↓ Jump to latest</button>
      <div id="chips"></div>
      <div class="crow">
        <label class="ghost attach" title="attach files or photos">📎<input type="file" id="attach" multiple onchange="addFiles(this.files, 'reply'); this.value = ''"></label>
        <textarea id="reply" placeholder="Message this thread (also posts to Slack) — !project, !stop, !reset, !brief… work too"></textarea>
        <button class="act" onclick="sendReply()">Send</button>
      </div>
    </div>
  </main>
</div>
<div id="tip"></div>
<div id="toast"></div>
<div id="alertsheet" onclick="if(event.target.id==='alertsheet'||(event.target.closest&&event.target.closest('#alertlist button')))closeAlerts()">
  <div class="box">
    <button class="ghost xclose" onclick="closeAlerts()" title="close">✕</button>
    <h2>⚠ Alerts</h2>
    <div id="alertlist"></div>
  </div>
</div>
<div id="taskmodal" onclick="if(event.target.id==='taskmodal')toggleTasks()">
  <div class="box">
    <button class="ghost xclose" onclick="toggleTasks()" title="close">✕</button>
    <h2 style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">📋 Tasks
      <span class="seg"><button id="tabneed" class="on" onclick="setTaskView('attention')">Needs you</button><button id="taball" onclick="setTaskView('all')">All</button></span>
      <select id="tproj" onchange="renderTasks()" title="filter by project"></select></h2>
    <div class="hint">Work Silkworm is managing. This opens on what needs you —
      tasks proposed for triage, waiting on approval, asking a question, or failed.
      Everything else is the system's business and stays out of the way.
      A finished task's commits stay on its own branch, so any that never reached
      the base are listed below — nothing here merges them. A checkout still
      holding uncommitted files is listed too, because approving or dismissing
      that task is the last time anything will mention it.</div>
    <div id="nightly"></div>
    <div id="spend" class="hint"></div>
    <div id="unmerged"></div>
    <div id="holding"></div>
    <div class="lform" id="taskadd">
      <button class="act" onclick="newProjectForm()">+ New project</button>
      <span class="hint" style="margin:0">New tasks are filed from a project's board — 🗂 Projects, open one, + New task.</span>
    </div>
    <div id="unregistered" class="hint"></div>
    <div id="tlist"></div>
  </div>
</div>
<div id="boardmodal" onclick="if(event.target.id==='boardmodal')toggleBoard()">
  <div class="box">
    <button class="ghost xclose" onclick="toggleBoard()" title="close">✕</button>
    <h2>🗂 Projects
      <span class="seg"><button id="bvover" class="on" onclick="setBoardProject(null)">Overview</button><button id="bvall" onclick="setBoardProject('')">All work</button><button id="bvunfiled" onclick="setBoardProject(UNFILED)">Unfiled</button></span></h2>
    <div class="hint">One card per project: what is open, running, landed and unmerged,
      whether it can take unsupervised work, what it spent this week and what is ready
      to release. Open one for its board. Every button here is the task panel's own
      action; "Needs you" stays in 📋 Tasks.</div>
    <div class="bfilters">
      <select id="bproj" onchange="setBoardProject(this.value === '*' ? null : this.value)" title="project"></select>
      <select id="brole" onchange="renderBoard()" title="role"></select>
      <select id="bstate" onchange="renderBoard()" title="state"></select>
      <input id="bq" placeholder="search title or goal…" autocomplete="off" oninput="boardSearch()">
    </div>
    <div id="bover"></div>
    <div id="bboard"></div>
  </div>
</div>
<div id="bdetail"></div>
<div id="fmodal" onclick="if(event.target.id==='fmodal')closeModal()"><div class="fbox" id="fbox"></div></div>
<div id="learnmodal" onclick="if(event.target.id==='learnmodal')toggleLearn()">
  <div class="box">
    <button class="ghost xclose" onclick="toggleLearn()" title="close">✕</button>
    <h2 style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">🧠 Learnings
      <button class="ghost" style="font-size:12px" onclick="harvestNow(this)">✨ Harvest now</button>
      <button class="ghost" style="font-size:12px" onclick="syncLearnings(this)">⇅ Sync</button></h2>
    <div class="hint">Auto-distilled from session activity by the harvester, plus any you add
      here. Toggle one off to stop injecting it without losing the record. Global learnings apply
      to every thread; a scope path limits one to threads working under it.</div>
    <div class="lform">
      <select id="ltype"><option value="do">always</option><option value="avoid">never</option><option value="note">context</option></select>
      <input class="text" id="ltext" placeholder="add one manually…">
      <input class="scope" id="lscope" placeholder="scope path (blank = global)">
      <button class="act" onclick="addLearning()">Add</button>
    </div>
    <div id="llist"></div>
  </div>
</div>
<script>
const esc = s => s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtTok = n => n >= 1e6 ? (n/1e6).toFixed(1)+"M" : n >= 1e3 ? (n/1e3).toFixed(1)+"k" : String(n);
let active = null, showTable = false, statsCache = null;

function dur(s) {
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s/60) + "m";
  if (s < 86400) return Math.floor(s/3600) + "h" + (Math.floor(s%3600/60) || "") ;
  return Math.floor(s/86400) + "d" + (Math.floor(s%86400/3600) || "");
}
function age(ts) {
  const d = (Date.now()/1000 - ts) / 86400;
  if (d < 1/24) return Math.max(1, Math.round(d*24*60)) + "m ago";
  if (d < 1) return Math.round(d*24) + "h ago";
  return Math.round(d) + "d ago";
}

// --- polling cheaply ---
// Each read remembers its last answer and the server's ETag for it, and asks
// again with If-None-Match: an unchanged answer is a 304 with no body, and the
// page draws from what it already holds. Through an SSH tunnel that is most of
// what polling cost. `key` names the read when the URL alone does not (POSTs).
const readCache = new Map();
async function readJSON(url, opts, key) {
  key = key || url;
  const prev = readCache.get(key);
  const headers = Object.assign({}, (opts || {}).headers);
  if (prev) headers["If-None-Match"] = prev.etag;
  const r = await fetch(url, Object.assign({}, opts || {}, {headers, cache: "no-store"}));
  if (r.status === 304 && prev) return prev.data;
  const data = await r.json();
  const etag = r.headers && r.headers.get ? r.headers.get("ETag") : null;
  if (etag) readCache.set(key, {etag, data, at: Date.now()});
  else readCache.delete(key);
  return data;
}
// Write markup only when it differs from what this element was last given, so
// a poll that changed nothing leaves hover, focus, scroll, open <details> and
// selection exactly where they were. True when it wrote.
function setHTML(el, html) {
  if (el._html === html) return false;
  el.innerHTML = html;
  el._html = html;
  return true;
}
// A tab nobody is looking at polls nothing; coming back catches up at once.
function visible() { return !document.hidden; }

// --- minimal markdown -> html (escaped first) ---
function md(src) {
  let s = esc(src);
  const fences = [];
  s = s.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    fences.push(code); return "\x00F" + (fences.length-1) + "\x00";
  });
  s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
  const lines = s.split("\n"); const out = []; let inList = false;
  for (const line of lines) {
    const h = line.match(/^#{1,6}\s+(.*)/);
    const li = line.match(/^\s*[-*]\s+(.*)/);
    if (li) { if (!inList) { out.push("<ul>"); inList = true; } out.push("<li>"+li[1]+"</li>"); continue; }
    if (inList) { out.push("</ul>"); inList = false; }
    if (h) out.push("<h4>"+h[1]+"</h4>");
    else if (line.trim() === "") out.push("</p><p>");
    else out.push(line + "<br>");
  }
  if (inList) out.push("</ul>");
  let html = "<p>" + out.join("") + "</p>";
  html = html.replace(/\x00F(\d+)\x00/g, (_, i) => "<pre><code>" + fences[+i] + "</code></pre>");
  return html.replace(/<p><\/p>/g, "").replace(/(<br>)+<\/p>/g, "</p>");
}

function toast(msg, ms) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.style.display = "block";
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.style.display = "none", ms || 2500);
}

// --- dashboard ---
async function loadStats() {
  statsCache = await readJSON("/api/stats");
  renderDash();
}
function renderDash() {
  const s = statsCache; if (!s) return;
  const tiles = `
    <div class="tiles">
      <div class="tile"><div class="n">$${s.total_cost.toFixed(2)}</div><div class="l" title="what the API would charge at list price; this bot runs on a subscription token">total spend (API list price)</div></div>
      <div class="tile"><div class="n">${fmtTok(s.days.reduce((a,d)=>a+d.cache_read+d.fresh_in+d.out,0))}</div><div class="l">tokens · 14d</div></div>
      <div class="tile"><div class="n">${s.cache_rate == null ? "—" : s.cache_rate + "%"}</div><div class="l">cache hit rate</div></div>
      <div class="tile"><div class="n">${s.threads}</div><div class="l">threads</div></div>
    </div>`;
  const legend = `
    <div class="legend">
      <span><span class="sw" style="background:var(--c-cache)"></span>cache read</span>
      <span><span class="sw" style="background:var(--c-fresh)"></span>fresh input</span>
      <span><span class="sw" style="background:var(--c-out)"></span>output</span>
      <button onclick="showTable=!showTable;renderDash()">${showTable ? "chart" : "table"}</button>
    </div>`;
  let body;
  if (showTable) {
    body = `<table class="dashtable"><tr><th>date</th><th>cache read</th><th>fresh in</th><th>output</th></tr>` +
      s.days.map(d => `<tr><td>${d.date.slice(5)}</td><td>${fmtTok(d.cache_read)}</td><td>${fmtTok(d.fresh_in)}</td><td>${fmtTok(d.out)}</td></tr>`).join("") + "</table>";
  } else {
    const max = Math.max(1, ...s.days.map(d => d.cache_read + d.fresh_in + d.out));
    body = `<div class="chart">` + s.days.map(d => {
      const segs = [["cache_read","var(--c-cache)"],["fresh_in","var(--c-fresh)"],["out","var(--c-out)"]]
        .map(([k, c]) => `<div class="seg" style="background:${c};height:${100*d[k]/max}%"></div>`).join("");
      return `<div class="col" data-tip="${d.date} — cache ${fmtTok(d.cache_read)} · fresh ${fmtTok(d.fresh_in)} · out ${fmtTok(d.out)}">${segs}</div>`;
    }).join("") + `</div><div class="xlabels">` +
      s.days.map((d, i) => `<span>${i % 2 ? "" : d.date.slice(5)}</span>`).join("") + "</div>";
  }
  const models = s.models.length
    ? `<div class="modelsplit">output by model: ` +
      s.models.map(([m, t]) => `${esc(m)} ${fmtTok(t)}`).join(" · ") + "</div>" : "";
  const html = tiles + legend + body + models;
  // Twice: above the open thread, and at the top of the phone's session list,
  // where it can be reached -- on a phone the thread column is a screen of its
  // own and the panel scrolled away above it (#dashm).
  const wrote = setHTML(document.getElementById("dash"), html);
  const sum = document.getElementById("dashsum");
  if (sum) sum.textContent = `📊 $${s.total_cost.toFixed(2)} · ${s.threads} threads · cache ${
    s.cache_rate == null ? "—" : s.cache_rate + "%"}`;
  const phoneDash = document.getElementById("dashphone");
  if (!(phoneDash && setHTML(phoneDash, html)) && !wrote) return;
  document.querySelectorAll(".chart .col").forEach(col => {
    col.onmousemove = e => {
      const tip = document.getElementById("tip");
      tip.textContent = col.dataset.tip; tip.style.display = "block";
      tip.style.left = Math.min(e.clientX + 12, innerWidth - 320) + "px";
      tip.style.top = (e.clientY - 40) + "px";
    };
    col.onmouseleave = () => document.getElementById("tip").style.display = "none";
  });
}

// --- session list ---
// Threads are two quite different things wearing one list: conversations you
// return to, and the one-off threads a task narrates into. Ten of the latter
// buries the former. Only kinds actually present get a button, so this stays a
// way to narrow what is there rather than a menu of empty categories.
let threadKind = "all";

const KIND_LABEL = {thread: "Conversations", task: "Task runs"};

// Hidden threads are kept, just not listed. "I am done looking at this" is a
// different thing from "erase what it cost me", and the record holds the
// title, summary, cost history and file list.
let showHidden = false;

function matchesKind(s) {
  if (!showHidden && s.hidden) return false;
  return threadKind === "all" || (s.kind || "thread") === threadKind;
}

async function hideThread(key, hidden, ev) {
  if (ev) ev.stopPropagation();          // the card itself opens the thread
  await fetch("/api/hide", {method: "POST", headers: {"Content-Type": "application/json"},
                            body: JSON.stringify({action: hidden ? "hide" : "unhide", key})});
  loadList();
}

function toggleHidden() { showHidden = !showHidden; loadList(); }

async function hideOldTaskRuns() {
  // Task runs are one-offs and are what actually piles up; a quiet
  // conversation may still be one you come back to, so it is left alone.
  const r = await (await fetch("/api/hide", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({action: "bulk", days: 14, kinds: ["task"]})})).json();
  if (r.ok) loadList();
}

function setKind(k) {
  threadKind = k;
  kindChosen = true;
  loadList();
}
// A phone opens the list on Conversations: those are the threads its owner
// writes in. Task runs outnumber them fifteen to one (407 to 27 in
// sessions.json on 2026-10-15; 77 to 11 touched that week) and are reached
// from 📋 Tasks when one needs them. A laptop keeps All, and a choice made
// on either sticks.
let kindChosen = false;
function defaultKind(sessions) {
  if (kindChosen) return;
  if (!phone()) { threadKind = "all"; return; }   // widened again: the laptop's All
  threadKind = sessions.some(s => !s.hidden && (s.kind || "thread") === "thread") ? "thread" : "all";
}

function renderKindFilter(sessions) {
  const bar = document.getElementById("kindfilter");
  const counts = {};
  for (const s of sessions) {
    const k = s.kind || "thread";
    counts[k] = (counts[k] || 0) + 1;
  }
  const kinds = Object.keys(counts).sort();
  const btn = (k, label, n) =>
    `<button class="${threadKind === k ? "on" : ""}" onclick="setKind('${k}')">` +
    `${label} <b>${n}</b></button>`;
  // One kind is not a choice, but the hidden controls still belong here.
  const kindBtns = kinds.length < 2 ? "" :
    btn("all", "All", sessions.length) +
    kinds.map(k => btn(k, KIND_LABEL[k] || k, counts[k])).join("");
  const nHidden = sessions.filter(s => s.hidden).length;
  const oldRuns = sessions.filter(
    s => !s.hidden && (s.kind || "thread") === "task"
         && (Date.now() / 1000 - s.updated) > 14 * 86400).length;
  setHTML(bar, kindBtns +
    (nHidden ? `<button class="${showHidden ? "on" : ""}" onclick="toggleHidden()"
                  title="hidden threads are kept, not deleted"
                >hidden <b>${nHidden}</b></button>` : "") +
    (oldRuns ? `<button onclick="hideOldTaskRuns()"
                  title="hide task runs untouched for 14 days"
                >tidy <b>${oldRuns}</b></button>` : ""));
}

// The time a running turn has been going is the one thing in this answer that
// moves every second, so the server leaves it out of the ETag; the page adds
// the time since the answer it is drawing from arrived.
function freshAges(data) {
  const c = readCache.get("/api/sessions");
  const gone = c ? (Date.now() - c.at) / 1000 : 0;
  for (const s of data.sessions || []) if (s.turn) {
    if (s.turn.age0 === undefined) s.turn.age0 = s.turn.age_s;
    s.turn.age_s = Math.round(s.turn.age0 + gone);
  }
}
async function loadList() {
  const data = await readJSON("/api/sessions");
  freshAges(data);
  document.getElementById("botdot").className = data.bot_online ? "on" : "";
  document.getElementById("botdot").title = data.bot_online ? "bot online" : "bot offline";
  renderAlerts(data.sessions, data.slack, data.revision);
  refreshTaskBadge();
  lastSessions = data.sessions;
  defaultKind(data.sessions);
  renderKindFilter(data.sessions);
  renderList(data.sessions.filter(matchesKind));
  if (active) {
    const cur = data.sessions.find(x => x.key === active.key);
    if (cur) active = cur;
    drawChatHead();
  }
}
function cardHtml(s) {
  const t = s.turn;
  const badges =
    (t && t.stalled ? `<span class="badge stall" title="${t.child_alive
          ? "silent far past the idle limit" : "no claude process — it cannot finish"}"
       >stalled ${dur(t.age_s)}</span>`
     : t ? `<span class="badge run">running ${dur(t.age_s)}</span>`
     : s.running ? '<span class="badge run">running</span>' : "") +
    (s.checked_out ? `<span class="badge term">${s.terminal_live ? "in terminal" : "checked out"}</span>` : "");
  const label = s.title ? `<span class="title">${esc(s.title)}</span>`
                        : `<span class="untitled">untitled</span>`;
  const kindTag = s.kind === "task" ? `<span class="badge kind">task run</span>` : "";
  const hideBtn = `<button class="hidebtn" title="${s.hidden ? "show again" : "hide"}"
      onclick="hideThread('${s.key}', ${s.hidden ? "false" : "true"}, event)"
    >${s.hidden ? "↺" : "×"}</button>`;
  return `<div class="key">${label}${kindTag}${badges}${hideBtn}
      ${s.cost_flag ? `<span class="badge cost" title="last turn $${s.cost_flag.last} vs median $${s.cost_flag.median}">${s.cost_flag.ratio}× cost</span>` : ""}</div>
    <div class="subkey">${esc(s.key)}</div>
    ${s.summary ? `<div class="summary">${esc(s.summary)}</div>` : ""}
    <div class="meta"><span><b>${s.turns}</b> turns</span>
    <span title="API list-price equivalent, not billed"><b>$${(s.cost||0).toFixed(2)}</b></span>
    <span>${esc(s.model || "default")}</span>
    <span>${age(s.updated)}</span>
    ${s.files ? `<span>📎 ${s.files}</span>` : ""}</div>`;
}
// One element per thread, kept across polls: a card is rewritten only when its
// own markup changed, and the list is reordered only when the order did. The
// whole nav used to be rebuilt every five seconds, dropping hover and focus.
const cardEls = new Map();
function openSession(key) {
  active = lastSessions.find(x => x.key === key) || active;
  loadList(); loadTranscript(true);
}
function renderList(shown) {
  const nav = document.getElementById("list");
  const want = new Set(shown.map(s => s.key));
  for (const [k, div] of cardEls) if (!want.has(k)) { div.remove(); cardEls.delete(k); }
  const divs = shown.map(s => {
    let div = cardEls.get(s.key);
    if (!div) {
      div = document.createElement("div");
      div.onclick = () => openSession(s.key);
      cardEls.set(s.key, div);
    }
    const cls = "card" + (active && active.key === s.key ? " active" : "");
    if (div.className !== cls) div.className = cls;
    setHTML(div, cardHtml(s));
    return div;
  });
  const now = [...(nav.children || [])];
  if (now.length !== divs.length || divs.some((d, i) => now[i] !== d))
    for (const d of divs) nav.appendChild(d);
}

// --- transcript ---
// The open thread is read from where the last read stopped (a byte cursor the
// server hands back), and new messages are appended: the whole transcript used
// to be fetched and redrawn every four seconds, closing every open <details>.
let tview = {key: null, sid: "", cursor: 0, busy: false, live: null};
function messageHtml(m) {
  let inner = "";
  for (const b of m.blocks) {
    if (b.type === "text") inner += `<div class="bubble">${md(b.text)}</div>`;
    else if (b.type === "thinking")
      inner += `<details><summary class="think">thinking</summary><pre>${esc(b.text)}</pre></details>`;
    else if (b.type === "tool")
      inner += `<details><summary>⚙ ${esc(b.name)}</summary><pre>${esc(b.input)}</pre></details>`;
    else if (b.type === "tool_result")
      inner += `<details><summary class="result">↳ result</summary><pre>${esc(b.text)}</pre></details>`;
  }
  const when = m.ts ? new Date(m.ts).toLocaleTimeString() : "";
  return `<div class="msg ${m.role}"><div class="who">${m.role} · ${when}</div>${inner}${usageLine(m)}</div>`;
}
function usageLine(m) {
  if (!m.usage) return "";
  const u = m.usage, denom = u.cache_read + u.in + u.cache_create;
  const rate = denom ? Math.round(100 * u.cache_read / denom) : null;
  return `<div class="usage">in ${fmtTok(u.in + u.cache_create)} · cache ${fmtTok(u.cache_read)}` +
    (rate == null ? "" : ` (${rate}%)`) + ` · out ${fmtTok(u.out)}` +
    (m.model ? ` · ${esc(m.model)}` : "") + `</div>`;
}

// --- the phone's chat ---
// On a phone the open thread reads as a chat: what the owner said and what
// Claude answered, as bubbles; every run of tool calls, their results
// and thinking between two of those folded into one line ("⚙ ran 6 tools")
// that opens onto the same detail a laptop shows. Token, cache and model
// figures sit behind a bubble's time, or inside the opened line. A laptop
// keeps the full log (messageHtml above).
//
// chatItems turns messages into
//   {kind: "bubble", id, role, text, ts, usage, model}
//   {kind: "tools", id, parts: [message-shaped pieces], tools, names}
// An id is where the item starts ("<message>.<block>"); messages are only
// ever appended, so an item keeps its id as the thread grows, and a tools
// line still collecting keeps its id (and whether it is open) as it grows.
function chatItems(messages) {
  const items = [];
  let group = null;
  messages.forEach((m, i) => {
    const blocks = m.blocks || [];
    let bubble = null, part = null, said = null;
    blocks.forEach((b, j) => {
      if (b.type === "text") {
        part = null; group = null;
        if (!bubble) {
          bubble = said = {kind: "bubble", id: `${i}.${j}`, role: m.role, text: b.text, ts: m.ts};
          items.push(bubble);
        } else bubble.text += "\n\n" + b.text;
        return;
      }
      bubble = null;
      if (!group) {
        group = {kind: "tools", id: `${i}.${j}`, parts: [], tools: 0, names: []};
        items.push(group);
      }
      if (!part) { part = {role: m.role, ts: m.ts, blocks: []}; group.parts.push(part); }
      part.blocks.push(b);
      if (b.type === "tool") {
        group.tools++;
        if (!group.names.includes(b.name)) group.names.push(b.name);
      }
    });
    // A message's figures go with its words when it has any, else its tools.
    const owner = said || part;
    if (owner && m.usage) { owner.usage = m.usage; owner.model = m.model; }
  });
  return items;
}
function toolsLabel(g) {
  const n = g.tools;
  const what = n ? `⚙ ran ${n} tool${n === 1 ? "" : "s"}` : "💭 thinking";
  const names = g.names.slice(0, 3).join(", ") + (g.names.length > 3 ? ", …" : "");
  return what + (names ? " · " + names : "");
}
function chatTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (isNaN(d)) return "";
  const t = d.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
  return d.toDateString() === new Date().toDateString() ? t
    : d.toLocaleDateString([], {month: "short", day: "numeric"}) + ", " + t;
}
const chatOpen = new Set();
function chatHtml(it) {
  if (it.kind === "bubble") {
    const when = esc(chatTime(it.ts));
    const meta = it.usage
      ? `<details class="cstat"><summary>${when} · ⓘ</summary>${usageLine(it)}</details>`
      : `<span>${when}</span>`;
    return `<div class="cmsg ${it.role}"><div class="bubble">${md(it.text)}</div>` +
           `<div class="cmeta">${meta}</div></div>`;
  }
  const open = chatOpen.has(it.id);
  return `<div class="ctools${open ? " open" : ""}"><button class="ctl" onclick="toggleTools('${it.id}')"
    >${esc(toolsLabel(it))} <span class="caret">${open ? "▾" : "▸"}</span></button>${
    open ? `<div class="cdetail">${it.parts.map(messageHtml).join("")}</div>` : ""}</div>`;
}
// One element per item, as the session list does: an item is rewritten only
// when its own markup changed, so an open detail stays open as the thread
// grows and a long thread is not redrawn every few seconds.
const chatEls = new Map();
function drawChat(msgs, messages, sessionId) {
  const items = chatItems(messages);
  if (!items.length) {
    chatEls.clear();
    msgs.innerHTML = sessionId ? "" : `<div class="empty">No messages yet — say something below.</div>`;
    return;
  }
  const want = new Set(items.map(x => x.id));
  for (const [k, div] of chatEls) if (!want.has(k)) { div.remove(); chatEls.delete(k); }
  if (msgs.querySelector && msgs.querySelector(".empty")) msgs.innerHTML = "";
  const divs = items.map(it => {
    let div = chatEls.get(it.id);
    if (!div) { div = document.createElement("div"); chatEls.set(it.id, div); }
    setHTML(div, chatHtml(it));
    return div;
  });
  const now = [...(msgs.children || [])];
  if (now.length !== divs.length || divs.some((d, i) => now[i] !== d))
    for (const d of divs) msgs.appendChild(d);
}
function toggleTools(id) {
  if (chatOpen.has(id)) chatOpen.delete(id); else chatOpen.add(id);
  if (active && tview.key === active.key)
    drawChat(document.getElementById("tmsgs"), tview.msgs || [], tview.sid);
}
// The chat follows the newest message while it is read at the bottom; read
// further up, it stays put and a pill offers the way down.
function scroller() {
  return typeof document.querySelector === "function" ? document.querySelector("main") : null;
}
function atEnd() {
  const m = scroller();
  return !m || m.scrollHeight - m.scrollTop - m.clientHeight < 80;
}
function toEnd() {
  const m = scroller();
  if (m) m.scrollTop = m.scrollHeight;
  showJump(false);
}
function showJump(on) {
  const p = document.getElementById("jumppill");
  if (p && p.classList && p.classList.toggle) p.classList.toggle("show", !!on);
}
// Draws with `fn`, then keeps the reader where they were: at the end if they
// were there, else where they are, with the pill if something new arrived.
function keepEnd(fn, force) {
  if (!chatMode()) { fn(); return; }
  const was = atEnd(), m = scroller(), before = m ? m.scrollHeight : 0;
  fn();
  if (force || was) toEnd();
  else if (m && m.scrollHeight > before) showJump(true);
}
function chatMode() { return phone(); }
// The open thread's own header on a phone, stuck to the top: back to the
// list, its name and project, what it is doing (with Stop while a turn runs),
// the alerts' count, and ⓘ for what a laptop shows above the transcript.
function chatHead(s, live, nAlerts) {
  const turns = (live && live.turns) || [];
  const queued = turns.filter(t => t.state === "queued").length;
  const t = s.turn;
  const running = !!(t || (live && live.running) || s.running);
  const state = t && t.stalled ? `<span class="cst bad">⚠ stalled ${dur(t.age_s)}</span>`
    : running ? `<span class="cst run">● running${t ? " " + dur(t.age_s) : ""}</span>`
    : s.updated ? `<span class="cst">${age(s.updated)}</span>` : "";
  return `<button class="cback" onclick="closeSession()" title="back to the list" aria-label="back to the list">‹</button>
    <div class="ctitle"><b class="${s.title ? "" : "untitled"}">${esc(s.title || "untitled")}</b>
      <span class="csub">${s.project ? `<span class="cproj">${esc(s.project)}</span>` : ""}${state}${
        queued ? `<span class="cst">· ${queued} queued</span>` : ""}</span></div>
    ${nAlerts ? `<button class="calert" onclick="openAlerts(event)" title="alerts">⚠ ${nAlerts}</button>` : ""}
    ${running ? `<button class="cstop" onclick="cmdSend('!stop')">■ Stop</button>` : ""}
    <button class="cinfo" onclick="document.body.classList.toggle('threadinfo')" title="thread details">ⓘ</button>`;
}
function drawChatHead() {
  const el = document.getElementById("chathead");
  if (!el || !active) return;
  setHTML(el, chatHead(active, tview.key === active.key ? tview.live : null, alertList.length));
}
function transcriptHead(s) {
  let h = `<div class="toolbar">
    <code id="cmd">${esc(s.resume_cmd)}</code>
    <button class="act" onclick="navigator.clipboard.writeText(document.getElementById('cmd').textContent);toast('Copied')">Copy resume cmd</button>
    <button class="ghost" onclick="window.open('${esc(s.slack_link)}')">Open in Slack</button>
    ${s.running ? `<button class="ghost" onclick="cmdSend('!stop')">■ Stop</button>` : ""}
    ${s.checked_out ? `<button class="ghost" onclick="cmdSend('!takeover')">Take over</button>
                       <button class="ghost" onclick="cmdSend('!back')">Reclaim</button>` : ""}
    <button class="ghost" onclick="const m=prompt('Model alias (opus / sonnet / haiku / fable, or reset):'); if(m) cmdSend('!model '+m)">Model…</button>
    <button class="ghost" onclick="if(confirm('Reset this thread\\'s session?')) cmdSend('!reset')">Reset</button>
  </div>`;
  if (s.turn) {
    const t = s.turn;
    h += `<div class="turnbar${t.stalled ? " bad" : ""}">
      <b>${t.stalled ? "⚠ Turn stalled" : "⏳ Turn running"} · ${dur(t.age_s)}</b>
      <span>${t.child_alive ? "claude process alive" : "no claude process — cannot finish"}</span>
      ${t.prompt ? `<span class="what">${esc(t.prompt)}</span>` : ""}
      <button class="ghost" onclick="releaseThread()">Release thread</button></div>`;
  }
  h += `<div class="ctx"><span class="lbl">context</span>${
    s.summary ? esc(s.summary) : `<i>No summary yet.</i>`
  }${s.summary_stale ? ` <span class="pill">stale — newer turns since</span>` : ""
  } <button class="ghost" style="margin-left:6px" title="regenerate summary"
      onclick="resummarize()">↻</button>
     <button class="ghost" title="rename this thread" onclick="retitle()">✎ name</button></div>`;
  if (s.events && s.events.length) {
    h += `<details class="events"><summary class="evsum">${s.events.length} thread event${
      s.events.length === 1 ? "" : "s"}</summary>` +
      s.events.slice().reverse().map(e =>
        `<div class="ev"><span class="k k-${esc(e.kind)}">${esc(e.kind)}</span>
         <span class="t">${age(e.at)}</span>
         <span class="d">${esc(e.detail || "")}</span></div>`).join("") + `</details>`;
  }
  return h;
}
function filesHtml(files, compact) {
  if (!files.length) return "";
  // In the phone's chat, one line that opens onto the list.
  if (compact) return `<details class="cfiles"><summary>📎 ${files.length} file${
    files.length === 1 ? "" : "s"}</summary>${filesHtml(files).replace("<b>Files</b>", "")}</details>`;
  return `<div class="artifacts"><b>Files</b>` + files.map(f =>
    `<div class="f"><span>${f.direction === "in" ? "⬇" : "⬆"}</span>
     ${f.exists ? `<a href="/download?path=${encodeURIComponent(f.path)}">${esc(f.name)}</a>` : esc(f.name) + (f.pruned ? " (pruned " + new Date(f.pruned * 1000).toLocaleDateString() + "; the copy is in Slack)" : " (gone)")}
     <span>${fmtTok(f.size)}B</span></div>`).join("") + "</div>";
}
async function loadTranscript(scroll) {
  if (!active) return;
  const s = active;
  // A new thread or a click starts again from the top. Otherwise one read at
  // a time, each from where the last stopped. The read names the thread, not
  // a session: the server answers for whichever session the thread is on now
  // (a reset, or a new thread's first session, included) and says `reset`
  // when that is not the one drawn so far.
  if (scroll) showReading();
  // Crossing the phone width redraws from the top in the other form.
  if (scroll || tview.key !== s.key || tview.chat !== chatMode()) {
    tview = {key: s.key, sid: "", cursor: 0, busy: false, error: false, msgs: [], chat: chatMode()};
    chatOpen.clear(); chatEls.clear();
  }
  const want = tview, from = want.cursor;
  // One read at a time; a call made meanwhile (a rename, a resummarize) is
  // remembered and run once this one is drawn, so it still shows promptly.
  if (want.busy) { want.again = true; return; }
  want.busy = true;
  let data, files;
  try {
    [data, files] = await Promise.all([
      (await fetch("/api/session?key=" + encodeURIComponent(s.key) + "&id=" +
                   encodeURIComponent(want.sid || "") + "&from=" + from)).json(),
      readJSON("/api/artifacts?key=" + encodeURIComponent(s.key)),
    ]);
  } finally { want.busy = false; }
  if (tview !== want || want.cursor !== from) return;   // superseded meanwhile
  const msgs = document.getElementById("tmsgs");
  keepEnd(() => {
    setHTML(document.getElementById("thead"), transcriptHead(s));
    if (data.error) {
      msgs.innerHTML = `<div class="empty">${esc(data.error)}</div>`;
      chatEls.clear();
      // The messages above were just replaced, so the next read starts over.
      want.error = true;
      want.cursor = 0;
    } else {
      // The first read, another session (reset), or recovering from an error
      // starts the messages over; anything else is only what is new.
      if (from === 0 || data.reset || want.error) {
        msgs.innerHTML = ""; want.msgs = []; chatEls.clear(); chatOpen.clear();
      }
      want.error = false;
      if (want.chat) {
        want.msgs = want.msgs.concat(data.messages);
        if (data.messages.length || !msgs.innerHTML) drawChat(msgs, want.msgs, data.session_id);
      } else if (data.messages.length)
        msgs.insertAdjacentHTML("beforeend", data.messages.map(messageHtml).join(""));
      else if (!msgs.innerHTML && !data.session_id)
        msgs.innerHTML = `<div class="empty">No messages yet.</div>`;
      want.cursor = data.cursor || 0;
      want.live = data.live || null;
      arrived(s.key, data.messages, (want.live && want.live.turns) || []);
    }
    if (data.session_id !== undefined) want.sid = data.session_id || "";
    want.live = data.live || null;
    drawLive();
    setHTML(document.getElementById("tfiles"), filesHtml(files, want.chat));
    document.getElementById("composer").style.display = "flex";
  }, scroll);
  if (scroll && !want.chat) document.getElementById("transcript").scrollIntoView(false);
  if (want.again && tview === want) { want.again = false; loadTranscript(false); }
}

// --- the live end of the open thread ---
// What was sent from this page and has not yet become transcript, per thread:
// {ref, text, files, kind ("prompt" | "command"), at, replies, finished, seen}.
// `ref` is the message's ts, which /api/send hands back and the bot records as
// the source_ref of the task the message's turn becomes; the thread's live
// state (/thread-state, carried on every transcript read) says what that
// task is doing -- sending, queued behind the turn that holds the thread,
// running, or how it ended. One mechanism: the transcript read above.
const outgoing = new Map();
function sentFor(key) {
  if (!outgoing.has(key)) outgoing.set(key, []);
  return outgoing.get(key);
}
// A message the transcript now holds is no longer drawn as on its way. The
// turn's prompt is the message, sometimes with Slack context before it or a
// note about attachments after, so it is found by what was typed.
function arrived(key, messages, turns) {
  const items = sentFor(key);
  // Only a message whose turn has not ended can be arriving now: the same
  // words sent again after a failed turn are the new one.
  const ended = new Set((turns || []).filter(t => !["queued", "running"].includes(t.state))
                                     .map(t => t.ref));
  const live = new Set((turns || []).filter(t => ["queued", "running"].includes(t.state))
                                    .map(t => t.ref));
  if (!items.length || !messages || !messages.length) return;
  for (const m of messages) {
    if (m.role !== "user") continue;
    const said = m.blocks.filter(b => b.type === "text").map(b => b.text).join("\n");
    if (!said) continue;
    // Written after it was sent (two minutes' grace for the phone's clock),
    // so a reread from the top cannot match an older message saying the same.
    const at = m.ts ? Date.parse(m.ts) : NaN;
    const fits = items.filter(i => i.kind === "prompt" && !i.seen && i.ref && !ended.has(i.ref) &&
      !(at < i.at - 120000) &&
      (i.text ? said.includes(i.text.slice(0, 80)) : said.includes("attached")));
    const it = fits.find(i => live.has(i.ref)) || fits[0];
    if (it) it.seen = true;
  }
}
// Drop what is done with: a prompt whose turn finished, or which the
// transcript now shows. Whatever was sent before it goes too -- a command's
// replies, a turn that failed -- so the thread reads in the order it
// happened; only what is still on its way stays.
function prune(items, turns) {
  const state = new Map(turns.map(t => [t.ref, t.state]));
  const over = it => it.kind === "prompt" && !!it.ref && (state.get(it.ref) === "done" ||
    (it.seen && (state.get(it.ref) === "running" || !state.has(it.ref))));
  const waiting = it => it.unconfirmed || (it.kind === "prompt" && !over(it) &&
    (!it.ref || ["queued", "running"].includes(state.get(it.ref))));
  let cut = -1;
  items.forEach((it, i) => { if (over(it)) cut = i; });
  return items.filter((it, i) => i > cut || waiting(it));
}
const QUEUED_NOTE = "queued — runs when the turn in progress finishes";
function outBubble(it, note, bad, who) {
  const files = it.files && it.files.length
    ? `<div class="state">📎 ${it.files.map(esc).join(", ")}</div>` : "";
  return `<div class="msg user out"><div class="who">${esc(who || (it.kind === "command" ? "you · command" : "you · dashboard"))}</div>
    ${it.text ? `<div class="bubble">${md(it.text)}</div>` : ""}${files}
    ${note ? `<div class="state${bad ? " bad" : ""}">${esc(note)}</div>` : ""}</div>`;
}
function noteBubble(text) {
  return `<div class="msg bot note"><div class="who">silkworm</div><div class="bubble">${md(text)}</div></div>`;
}
function promptNote(it, t) {
  if (!it.ref) return ["sending…", false];
  if (!t) return ["sent", false];
  if (t.state === "queued") return [QUEUED_NOTE, false];
  if (t.state === "running") return ["starting…", false];
  if (t.state === "failed") return ["⚠ failed" + (t.why ? ": " + t.why : ""), true];
  if (t.state === "cancelled") return ["stopped" + (t.why ? " — " + t.why : ""), true];
  if (t.state === "blocked") return ["held — " + (t.why || "it will be retried"), true];
  return [t.state || "", false];
}
function liveHtml(s, live, items) {
  live = live || {};
  const turns = live.turns || [];
  const byRef = new Map(turns.map(t => [t.ref, t]));
  let h = "";
  items.forEach((it, i) => {
    if (it.unconfirmed) {
      h += outBubble(it, "⚠ not confirmed — it may have gone through" + (it.why ? ` (${it.why})` : ""), true)
        + `<div class="state">Checking sends it again under the same id, so it cannot arrive twice.
           <button class="ghost" onclick="recheckSend(${i})">Check again</button></div>`;
      return;
    }
    if (it.kind === "command")
      h += outBubble(it, it.finished === false ? "still running — the rest of what it says is in Slack" : "");
    else h += outBubble(it, ...promptNote(it, byRef.get(it.ref)));
    for (const r of it.replies || []) h += noteBubble(r);
  });
  // Messages waiting their turn that this page did not send: from Slack, or
  // from the dashboard open elsewhere. The thread runs one turn at a time.
  for (const t of turns)
    if (t.state === "queued" && !items.some(i => i.ref === t.ref))
      h += outBubble({kind: "prompt", text: t.title, files: []}, QUEUED_NOTE, false,
                     t.source === "slack" ? "you · slack" : "you · queued");
  if (live.running)
    h += `<div class="working">⏳ <b>Working<span class="dots"></span></b>
      <span>${s.turn ? dur(s.turn.age_s) : ""}</span>
      <button class="ghost" onclick="cmdSend('!stop')">■ Stop</button></div>`;
  const ended = turns.filter(t => t.state !== "queued" && t.state !== "running");
  const last = ended[ended.length - 1];
  if (last && !live.running && ["failed", "cancelled"].includes(last.state)
      && !items.some(i => i.ref === last.ref))
    h += `<div class="state bad">⚠ The last turn ${last.state === "failed" ? "failed" : "was stopped"}${
      last.why ? ": " + esc(last.why) : ""}</div>`;
  if (live.checked_out)
    h += `<div class="state bad">This thread is checked out to the terminal, so messages are held.
      <button class="ghost" onclick="cmdSend('!back')">Reclaim</button>
      <button class="ghost" onclick="cmdSend('!takeover')">Take over</button></div>`;
  if (live.offline)
    h += `<div class="state bad">The bot is offline: a message sent now waits until it is back.</div>`;
  return h;
}
function drawLive(force) {
  if (!active || tview.key !== active.key) return;
  const turns = (tview.live && tview.live.turns) || [];
  const items = prune(sentFor(active.key), turns);
  outgoing.set(active.key, items);
  keepEnd(() => setHTML(document.getElementById("tlive"), liveHtml(active, tview.live, items)), force);
  drawChatHead();
}
// Something is under way in the open thread: it is read every 1.5s rather
// than every 4s, so a reply streams in as the transcript is written. A
// message still on its way counts; one whose turn ended badly does not, nor
// one the bot never made a turn of (a checked-out thread) after a minute.
function threadBusy() {
  if (!active || tview.key !== active.key) return false;
  const live = tview.live || {};
  const state = new Map((live.turns || []).map(t => [t.ref, t.state]));
  return !!(live.running || (live.queued && live.queued.length) ||
            sentFor(active.key).some(i => i.kind === "prompt" && !i.unconfirmed && (!i.ref ||
              ["queued", "running"].includes(state.get(i.ref)) ||
              (!state.has(i.ref) && Date.now() - i.at < 60000))));
}

// --- composer / commands ---
// Files waiting to go with the next message: the composer's, and the new
// thread form's. Read into base64 only when sent; the request carries them
// (the dashboard's POSTs are JSON, which is also what the proxy gate wants).
const MAX_FILE_MB = 25;
const staged = {reply: [], nt: []};
function addFiles(list, where) {
  for (const f of Array.from(list || [])) {
    if (f.size > MAX_FILE_MB * 1024 * 1024) { toast(`${f.name} is over ${MAX_FILE_MB} MB`); continue; }
    staged[where].push(f);
  }
  renderChips(where);
}
function dropFile(where, i) { staged[where].splice(i, 1); renderChips(where); }
function renderChips(where) {
  const box = document.getElementById(where === "nt" ? "ntchips" : "chips");
  if (!box) return;
  setHTML(box, staged[where].map((f, i) =>
    `<span class="chip"><span>${esc(f.name)}</span><button title="remove" onclick="dropFile('${where}', ${i})">✕</button></span>`).join(""));
}
function readB64(file) {
  return new Promise((ok, fail) => {
    const r = new FileReader();
    r.onload = () => ok(String(r.result).split(",", 2)[1] || "");
    r.onerror = () => fail(r.error || new Error("could not read " + file.name));
    r.readAsDataURL(file);
  });
}
async function encodeFiles(files) {
  return Promise.all(files.map(async f =>
    ({name: f.name, type: f.type || "", data: await readB64(f)})));
}
async function postSend(payload) {
  try {
    const r = await fetch("/api/send", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    return await r.json();
  } catch (e) {
    // The request may have arrived before the connection dropped.
    return {ok: false, unknown: true, error: "could not reach the dashboard"};
  }
}
// Every message carries an id of its own. When a send's outcome is unknown --
// the bot took it and did not answer in time, or a repeat found the first
// still under way -- it is sent again under the same id, and the bot answers
// a repeat from the first send: one thread, one turn, however many tries.
function newClientId() {
  try { if (crypto && crypto.randomUUID) return crypto.randomUUID(); } catch (e) { /* below */ }
  let id = "";
  while (id.length < 32) id += Math.random().toString(36).slice(2);
  return id.slice(0, 32);
}
const RECONCILE_TRIES = 4, RECONCILE_GAP_MS = 1500;
function unsure(r) { return !r.ok && !!(r.unknown || r.pending); }
async function sendOnce(payload) {
  let r = await postSend(payload);
  for (let i = 0; i < RECONCILE_TRIES && unsure(r); i++) {
    await new Promise(ok => setTimeout(ok, RECONCILE_GAP_MS));
    r = await postSend(payload);
  }
  return r;
}
// Shown at once as on its way, then followed by its ref (see `outgoing`).
// A command waits for its replies (the bot runs it through handle_command,
// as from Slack) and they are drawn under it.
async function sendText(text, files) {
  if (!active) return false;
  text = (text || "").trim(); files = files || [];
  if (!text && !files.length) return false;
  const key = active.key;
  const it = {ref: "", text, files: files.map(f => f.name),
              kind: text.startsWith("!") ? "command" : "prompt", at: Date.now()};
  sentFor(key).push(it);
  drawLive(true);                // what was just sent is in view
  let r;
  try {
    it.picked = files;
    it.payload = {key, text, files: await encodeFiles(files), client_id: newClientId()};
    r = await sendOnce(it.payload);
  } catch (e) { r = {ok: false, error: String(e && e.message || e)}; }
  return settleSend(key, it, r);
}
// What a send came to. Sent: it follows its ref. Refused: it is dropped, and
// the caller puts the text back. Unknown even after reconciling: it stays,
// marked unconfirmed with its payload, and the text is NOT put back -- it may
// well have gone through, and sending it again by hand would be a second one.
// "Check again" (recheckSend) resends it under the same id instead.
function settleSend(key, it, r) {
  if (unsure(r)) {
    it.unconfirmed = true; it.why = r.error || "";
    drawLive();
    return "unknown";
  }
  it.unconfirmed = false;
  if (!r.ok) {
    outgoing.set(key, sentFor(key).filter(x => x !== it));
    toast("Not sent: " + (r.error || "unknown"));
  } else {
    it.ref = r.ref || "";
    it.replies = r.replies || [];
    it.finished = r.finished;
    it.payload = null; it.picked = null;
  }
  drawLive();
  loadTranscript(false);
  if (it.kind === "command") loadList();
  return !!r.ok;
}
async function recheckSend(i) {
  if (!active) return;
  const key = active.key, it = sentFor(key)[i];
  if (!it || !it.unconfirmed || !it.payload) return;
  it.why = "checking…"; drawLive();
  const r = await sendOnce(it.payload);
  if (settleSend(key, it, r) === false && it.kind === "prompt") {
    // Definitely not sent: now it is safe to give the words back, files too.
    const box = document.getElementById("reply");
    if (box && !box.value && !staged.reply.length) {
      box.value = it.text; staged.reply = it.picked || []; renderChips("reply");
    }
  }
}
function cmdSend(cmd) { sendText(cmd); }
async function sendReply() {
  const box = document.getElementById("reply");
  const text = box.value, files = staged.reply.slice();
  if (!text.trim() && !files.length) return;
  box.value = ""; staged.reply = []; renderChips("reply"); growReply();
  // Refused: put it back, so nothing typed or picked is lost. Not when the
  // outcome is unknown: it may have gone, and is shown as unconfirmed.
  if ((await sendText(text, files)) === false && !box.value) {
    box.value = text; staged.reply = files; renderChips("reply"); growReply();
  }
}
// On a phone the box grows with what is typed, one line to six, then
// scrolls inside itself; its placeholder is short enough to show whole.
const REPLY_MAX_LINES = 6;
function growReply() {
  const box = document.getElementById("reply");
  if (!box) return;
  if (!phone()) { box.style.height = ""; box.style.overflowY = ""; return; }
  const cs = typeof getComputedStyle === "function" ? getComputedStyle(box) : null;
  const px = k => cs ? parseFloat(cs[k]) || 0 : 0;
  const edges = px("paddingTop") + px("paddingBottom") + px("borderTopWidth") + px("borderBottomWidth");
  const most = (px("lineHeight") || 22) * REPLY_MAX_LINES + edges;
  box.style.height = "auto";                          // so it can shrink, too
  const want = box.scrollHeight + px("borderTopWidth") + px("borderBottomWidth");
  box.style.height = Math.min(want, most) + "px";
  box.style.overflowY = want > most ? "auto" : "hidden";
}
(function () {
  const box = document.getElementById("reply");
  if (!box) return;
  if (phone()) { box.placeholder = "Message (also in Slack)"; box.rows = 1; }
  if (box.addEventListener) box.addEventListener("input", growReply);
})();
// The phone's keyboard: the page is kept to what is visible above it, so the
// composer sits on the keyboard rather than under it. In the home-screen app
// the home indicator's inset is dropped while the keyboard covers it.
function fitViewport() {
  const vv = typeof window !== "undefined" && window.visualViewport;
  const root = typeof document !== "undefined" && document.documentElement;
  // Pinch-zoomed, the visible area is not the keyboard's doing: leave it be.
  if (!vv || !root || !root.style || !phone() || vv.scale > 1) return;
  root.style.setProperty("--vvh", Math.round(vv.height) + "px");
  if (document.body) document.body.classList.toggle("kb", window.innerHeight - vv.height > 120);
  if (vv.offsetTop && window.scrollTo) window.scrollTo(0, 0);
}
if (typeof window !== "undefined" && window.visualViewport && window.visualViewport.addEventListener) {
  window.visualViewport.addEventListener("resize", fitViewport);
  window.visualViewport.addEventListener("scroll", fitViewport);
  fitViewport();
}
(function () {
  const m = scroller();
  if (m && m.addEventListener) m.addEventListener("scroll", () => { if (atEnd()) showJump(false); },
                                                  {passive: true});
})();
document.addEventListener("keydown", e => {
  // On a phone Return is a new line; the Send button sends.
  if (e.target.id === "reply" && e.key === "Enter" && !e.shiftKey && !phone()) {
    e.preventDefault(); sendReply();
  }
});

// --- a new thread ---
// Its first message is posted in Slack's home channel as the thread's parent,
// and the thread is then the same as one started there. A project is filed by
// the bot with `!project <slug>` before the first turn, so it is inherited
// exactly as from Slack.
let ntClientId = "";
async function newThreadForm() {
  try { await loadFormChoices(); } catch (e) { formProjects = formProjects || []; }
  staged.nt = [];
  // One id for whatever this form sends: a second tap after an unconfirmed
  // start asks after the first rather than starting another thread.
  ntClientId = newClientId();
  openModal(`<div class="tform"><h3>New thread</h3>
    <div class="sub">It starts in Slack too, under the bot's home channel — reply from either side.</div>
    <label>project <span class="note">(optional — the thread is filed under it, as <code>!project</code> does)</span></label>
    <select id="ntproj"><option value="">(no project)</option>${(formProjects || []).map(p =>
      `<option value="${esc(p.slug)}">${esc(p.title || p.slug)}</option>`).join("")}</select>
    <label>message</label>
    <textarea id="nttext" placeholder="What is it about?"></textarea>
    <div class="line"><label class="ghost attach" title="attach files or photos">📎<input type="file" multiple onchange="addFiles(this.files, 'nt'); this.value = ''"></label>
      <div class="chips" id="ntchips"></div></div>
    ${modalButtons("submitNewThread", "Start thread")}</div>`, {kind: "thread"});
}
async function submitNewThread() {
  const text = document.getElementById("nttext").value.trim();
  const project = document.getElementById("ntproj").value;
  const files = staged.nt.slice();
  if (!text && !files.length) { formError("a thread starts with a message"); return; }
  formError("");
  let r;
  try { r = await sendOnce({new: true, text, project, files: await encodeFiles(files),
                            client_id: ntClientId}); }
  catch (e) { r = {ok: false, error: String(e && e.message || e)}; }
  if (unsure(r)) {
    formError("Not confirmed — it may have started. Tap Start thread again to check: "
              + "it asks after the same message, so it cannot start a second thread.");
    return;
  }
  if (!r.ok) { formError(r.error || "refused"); return; }
  closeModal();
  staged.nt = [];
  sentFor(r.key).push({ref: r.ref, text, files: files.map(f => f.name), kind: "prompt",
                       at: Date.now(), replies: r.replies || []});
  // Until the list has it: enough of a session for the header to draw.
  active = {key: r.key, title: "", kind: "thread", project, session_id: "", summary: "",
            events: [], turns: 0, cost: 0, updated: Date.now() / 1000, files: 0,
            resume_cmd: "", slack_link: r.link || "", running: true};
  loadTranscript(true);
  loadList();
}

// --- search ---
let searchTimer = null;
document.getElementById("search").addEventListener("input", e => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  const box = document.getElementById("searchresults");
  if (!q) { box.style.display = "none"; return; }
  searchTimer = setTimeout(async () => {
    const hits = await (await fetch("/api/search?q=" + encodeURIComponent(q))).json();
    box.innerHTML = hits.length ? hits.map(h =>
      `<div class="hit" data-key="${esc(h.key)}" data-sid="${esc(h.session_id)}">
         <div class="k">${esc(h.key)} · ${h.role}</div>${esc(h.snippet)}</div>`).join("")
      : `<div class="hit">No matches.</div>`;
    // On a phone the header wraps (and sits below the notch in the app), so
    // the results go under wherever it ends rather than a fixed 52px.
    const head = phone() && document.querySelector && document.querySelector("header");
    box.style.top = head ? Math.ceil(head.getBoundingClientRect().bottom + 4) + "px" : "";
    box.style.display = "block";
    box.querySelectorAll(".hit[data-key]").forEach(hit => hit.onclick = async () => {
      box.style.display = "none";
      document.getElementById("search").value = "";
      const data = await readJSON("/api/sessions");
      active = data.sessions.find(s => s.key === hit.dataset.key) || null;
      loadList(); if (active) loadTranscript(true);
    });
  }, 250);
});
document.addEventListener("click", e => {
  if (!e.target.closest("#searchresults") && e.target.id !== "search")
    document.getElementById("searchresults").style.display = "none";
});
// On a phone the header is one row: search is a 🔍 that opens a row under it,
// and Learnings, Name untitled and Notify live behind ⋯. On a laptop both
// buttons are hidden and everything sits in the header as before.
function toggleSearch() {
  const on = document.body.classList.toggle("searching");
  const box = document.getElementById("search");
  if (on) box.focus();
  else { box.value = ""; document.getElementById("searchresults").style.display = "none"; }
}
function toggleMenu(e) {
  if (e) e.stopPropagation();
  document.querySelector("header").classList.toggle("menu");
}
function closeMenu() { document.querySelector("header").classList.remove("menu"); }
document.addEventListener("click", e => {
  if (!e.target.closest || !e.target.closest(".hmore")) closeMenu();
});

// --- learnings ---
async function learnCall(payload) {
  return (await (await fetch("/api/learnings", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)})).json());
}
// The alerts, one string of markup each: the laptop's bar shows them in a
// row; a phone shows only how many there are (⚠ 3), in the header, and opens
// them as a sheet -- they never sit above the conversation.
let alertList = [];
function renderAlerts(sessions, slack, revision) {
  alertList = alertParts(sessions, slack, revision);
  setHTML(document.getElementById("alertbar"), alertList.join(" "));
  drawAlertCount();
}
function alertButton() {
  return alertList.length ? `<button class="ghost hbtn alertbtn" onclick="openAlerts(event)"
    title="${alertList.length} alert${alertList.length === 1 ? "" : "s"}">⚠ ${alertList.length}</button>` : "";
}
function drawAlertCount() {
  const slot = document.getElementById("alertslot");
  if (slot) setHTML(slot, alertButton());
  const list = document.getElementById("alertlist");
  if (list) setHTML(list, alertList.length
    ? alertList.map(a => `<div class="aitem">${a}</div>`).join("")
    : `<div class="empty">Nothing to look at.</div>`);
  drawChatHead();
}
function openAlerts(e) {
  if (e && e.stopPropagation) e.stopPropagation();
  const sheet = document.getElementById("alertsheet");
  if (sheet) sheet.classList.add("open");
}
function closeAlerts() {
  const sheet = document.getElementById("alertsheet");
  if (sheet) sheet.classList.remove("open");
}
function alertParts(sessions, slack, revision) {
  const stalled = sessions.filter(s => s.turn && s.turn.stalled);
  const pricey = sessions.filter(s => s.cost_flag);
  // Only trust a full sampling window; a bot that just started is not down.
  const down = slack && slack.ready && !slack.connected;
  // Landed is not running. Nothing restarts the bot when it merges onto its
  // own main, so the fix sits in the tree while the old process keeps serving.
  // "unknown" is not "fine" either — a bot too old to report the field gives
  // us nothing, which is the silence this is here to break.
  const stale = revision && revision.state && revision.state !== "current";
  const parts = [];
  if (stale) parts.push(`<span class="alert ${revision.state === "stale" ? "bad" : "warn"}">⚠ ${
    esc(revision.message || "running revision unknown")} — silkworm restart</span>`);
  if (down) parts.push(`<span class="alert bad">⚠ not connected to Slack${
    slack.down_for ? " · " + dur(slack.down_for) : ""} — restarting itself</span>`);
  if (stalled.length) parts.push(
    `<span class="alert bad">⚠ ${stalled.length} thread${stalled.length>1?"s":""} stalled</span>` +
    stalled.map(s => `<button class="ghost" onclick="jumpTo('${s.key}')">${
      esc(s.title || s.key)} · ${dur(s.turn.age_s)}</button>`).join(""));
  if (pricey.length) parts.push(
    `<span class="alert warn">$ ${pricey.length} cost spike${pricey.length>1?"s":""}</span>` +
    pricey.map(s => `<button class="ghost" onclick="jumpTo('${s.key}')">${
      esc(s.title || s.key)} · ${s.cost_flag.ratio}×</button>`).join(""));
  return parts;
}
function jumpTo(key) {
  const card = [...document.querySelectorAll("#list .card")].find(
    c => c.querySelector(".subkey") && c.querySelector(".subkey").textContent === key);
  if (card) card.click();
  // On a phone, filtered out of the list (a task run while it shows
  // conversations): open it anyway. A laptop's list is as it always was.
  else if (phone() && lastSessions.some(x => x.key === key)) openSession(key);
}
async function retitle() {
  if (!active) return;
  const typed = prompt("Thread name (leave blank to generate one from its summary):",
                       active.title || "");
  if (typed === null) return;
  toast(typed.trim() ? "Renaming…" : "Generating a name…");
  const r = await (await fetch("/api/titles", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({key: active.key, title: typed.trim()})})).json();
  toast(r.ok ? `Named “${r.title}”` : (r.error || "Could not name it"));
  await loadList(); loadTranscript(false);
}
async function nameAllThreads() {
  if (!confirm("Generate names for every untitled thread?")) return;
  toast("Naming untitled threads…");
  const r = await (await fetch("/api/titles", {method: "POST",
    headers: {"Content-Type": "application/json"}, body: "{}"})).json();
  toast(r.ok ? `Named ${r.titled}, skipped ${r.skipped}` : (r.error || "Failed"));
  await loadList();
}
async function releaseThread() {
  if (!active) return;
  if (!confirm("Kill this thread's stuck turn and release it?\n\n"
             + "Its session history is kept — you can just send the message again.")) return;
  toast("Releasing…");
  const r = await (await fetch("/api/release", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({key: active.key})})).json();
  if (r.ok) { toast(`Released (killed ${r.killed} process${r.killed === 1 ? "" : "es"})`); }
  else toast(r.error || "Could not release");
  await loadList(); loadTranscript(false);
}
async function resummarize() {
  if (!active) return;
  toast("Summarizing…");
  const r = await (await fetch("/api/summaries", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({key: active.key})})).json();
  if (r.ok) { toast("Summary updated"); await loadList(); loadTranscript(false); }
  else toast(r.error || "Could not summarize");
}
let taskView = "attention";

async function taskCall(payload) {
  return (await (await fetch("/api/tasks", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)})).json());
}
// The same, for a read the page polls: answered 304 when nothing changed. Only
// ever for actions that read -- the server refuses a 304 to anything else.
async function taskRead(payload) {
  const body = JSON.stringify(payload);
  return readJSON("/api/tasks", {method: "POST",
    headers: {"Content-Type": "application/json"}, body}, "tasks " + body);
}
function toggleTasks() {
  const m = document.getElementById("taskmodal");
  const open = m.style.display !== "flex";
  m.style.display = open ? "flex" : "none";
  if (open) { renderProjects().then(renderTasks); renderUnregistered(); }
}
function setTaskView(v) {
  taskView = v;
  document.getElementById("tabneed").className = v === "attention" ? "on" : "";
  document.getElementById("taball").className = v === "all" ? "on" : "";
  renderTasks();
}
// Kept so the schedule prompt can show what a project is currently set to
// rather than making you remember it.
let projectRows = [];

async function setIdeate(slug) {
  const cur = (projectRows.find(p => p.slug === slug) || {}).ideate_at || "";
  const at = prompt(
    `Nightly review for ${slug}\n\nTime of day (02:00, or 2am), or "off" to stop:`,
    cur || "02:00");
  if (at === null) return;                       // cancelled is not "off"
  const r = await (await fetch("/api/projects", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({action: "ideate", slug, at})})).json();
  if (!r.ok) { toast(r.error || "could not set that"); return; }
  const now = (r.project || {}).ideate_at;
  toast(now ? `${slug}: nightly review at ${now}` : `${slug}: nightly review off`);
  renderProjects();
}

function renderNightly(rows, limit) {
  const el = document.getElementById("nightly");
  if (!el) return;
  if (!rows.length) { el.innerHTML = ""; return; }
  const on = rows.filter(p => p.ideate_at).length;
  el.innerHTML =
    `<span class="nlabel">🌙 nightly review${on ? "" : " — none scheduled"}</span>` +
    rows.map(p => {
      // A project at its standing limit is skipped until some of what is
      // already proposed is triaged. Said here, because "it stopped running"
      // and "it ran and found nothing" look identical from outside.
      const paused = p.ideate_at && limit && (p.proposed || 0) >= limit;
      return `<button class="${p.ideate_at ? "on" : ""}" onclick="setIdeate('${esc(p.slug)}')"
         title="${paused
           ? `paused — ${p.proposed} proposals waiting, the limit is ${limit}; `
             + `accept or dismiss some and it runs again at ${p.ideate_at}`
           : p.ideate_at
           ? `reads the project at ${p.ideate_at} and files proposals for you to accept`
           : "off — click to schedule a nightly look"}"
       >${esc(p.title)}${p.ideate_at ? ` <b>${paused ? "paused" : p.ideate_at}</b>` : ""}</button>`;
    }).join("");
}

// Repositories in the workspace no project covers. Clicking one opens the
// new-project form to adopt it, rather than registering anything by itself.
async function renderUnregistered() {
  const el = document.getElementById("unregistered");
  if (!el) return;
  let r;
  try {
    r = await (await fetch("/api/projects", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action: "unregistered"})})).json();
  } catch (e) { el.textContent = ""; return; }
  const rows = (r && r.ok && r.repos) || [];
  el.innerHTML = rows.length
    ? "📂 unregistered repos: " + rows.map(p =>
        `<a href="#" data-path="${esc(p)}" onclick="adoptRepo(this.dataset.path);return false"
          >${esc(p.split("/").pop())}</a>`).join(", ")
    : "";
}

function adoptRepo(path) {
  newProjectForm({name: path.split("/").pop(), path});
}

async function renderProjects() {
  const sel = document.getElementById("tproj");
  const keep = sel.value;
  const r = await (await fetch("/api/projects", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({action: "list"})})).json();
  const rows = (r.projects || []).filter(p => !p.archived);
  projectRows = rows;
  renderNightly(rows, r.proposal_limit || 0);
  sel.innerHTML = `<option value="">all projects</option>` + rows.map(p =>
    `<option value="${esc(p.slug)}">${esc(p.title)}${
      p.needs ? ` (${p.needs})` : p.open ? ` · ${p.open}` : ""}</option>`).join("");
  sel.value = keep;
}
async function sendBack(id, needsAnswer) {
  // Ask before returning work, so the rerun knows why. Cancelling the prompt
  // abandons the whole action -- "never mind" is not the same as "no notes".
  const notes = prompt(needsAnswer
    ? "Your answer (it resumes with this):"
    : "Anything to add? The reviewer's findings are included automatically.\n"
      + "Leave blank to send back with just those.", "");
  if (notes === null) return;
  await taskAction(id, "rework", notes);
}
async function digDeeper(id, round) {
  // The question is the whole point of digging deeper: cancelling the prompt
  // abandons the action, and a blank answer is asked for again rather than
  // rerunning the research with nothing new to go on.
  let q = "";
  while (!q.trim()) {
    q = prompt("What should it dig into? The research resumes with your question "
               + "and parks again with new findings.", q);
    if (q === null) return;
  }
  await taskAction(id, "dig", q, round);
}
async function closeFindings(id, round) {
  if (!confirm("Close these findings?\n\nThe research is done; nothing further runs. "
             + "Any next steps it filed stay on the board for you to accept or dismiss.")) return;
  await taskAction(id, "close", "", round);
}
function hasFindings(t) {
  // Asked of the server (research.has_findings), so the page names no role.
  return !!t.findings;
}
function findings(t) {
  // A research task's next steps: the proposals it filed under itself, and
  // the ones the proposal cap or dedup refused -- said, never dropped.
  const r = t.research;
  if (!r) return "";
  const kids = (r.children || []).map(k =>
    `<li><code>${esc(k.id)}</code> <span class="st st-${esc(k.state)}">${esc(k.state)}</span> ${esc(k.title)}</li>`).join("");
  const held = (r.held || []).map(h =>
    `<li>${esc(h.goal)} — <i>${esc(h.why)}</i></li>`).join("");
  if (!kids && !held) return "";
  return `<div class="rev ok"><b>Next steps</b>${kids ? `<ul>${kids}</ul>` : ""}${
    held ? `<span class="lbl">not filed:</span><ul>${held}</ul>` : ""}</div>`;
}
async function stopTask(id) {
  // Cancelling a running task is not the same act as cancelling a queue entry,
  // so it does not share the button. The backend kills the child mid-turn; its
  // checkout is released the way any other turn's is, and release() keeps a
  // tree with uncommitted changes on disk rather than discarding it. Worth one
  // question anyway: `cancelled` is terminal, so the run cannot be resumed.
  if (!confirm("Stop this agent mid-run?\n\n"
             + "Its child process is killed and the task is cancelled; it cannot "
             + "be resumed. Uncommitted work in its checkout is kept on disk, "
             + "not discarded.")) return;
  await taskAction(id, "cancel");
}
async function taskAction(id, action, notes, round) {
  // `round` is which findings a button on research was drawn for; a dig parks
  // research in needs_input again, so the state alone cannot tell them apart.
  const p = notes ? {action, id, notes, by: "you"} : {action, id};
  if (round) p.round = round;
  const r = await taskCall(p);
  // Cancelling something that was running says whether the work was actually
  // stopped, which is the only part of it you cannot see from the board.
  // Land only starts a landing; "Task landed" would claim a merge that has
  // not happened. Those two say what they did in their own words.
  // Release likewise only starts one; Done is not a verb to add -ed to.
  toast(r.ok ? ((action === "land" || action === "drop" || action === "release") && r.note
                 ? r.note
               : action === "resolve" ? "Marked done"
               : action === "close" ? "Findings closed"
               : action === "dig" ? "Sent to dig deeper"
               : `Task ${action}ed${r.note ? ` — ${r.note}` : ""}`)
             : (r.error || "Not allowed"));
  renderTasks();
  refreshTaskBadge();
  if (boardIsOpen()) refreshBoard();
}
function review(t) {
  // Show why it is waiting, so approving is an informed click rather than a leap.
  // A passed review is shown too: its findings used to be written to a done
  // task and never read again, which is the whole reason followups exist.
  const rv = (t.result || {}).review;
  if (!rv) return "";
  const list = (xs, label) => (xs || []).length
    ? `${label ? `<span class="lbl">${label}</span>` : ""}`
      + `<ul>${xs.map(f => `<li>${esc(f)}</li>`).join("")}</ul>`
    : "";
  const filed = (rv.filed || []).length
    ? `<div class="filed">filed as ${(rv.filed || []).map(esc).join("  ")}</div>` : "";
  const unver = (rv.unverified || []).length
    ? `<span class="lbl">not checked by the review: ${
        esc((rv.unverified || []).join("; "))}</span>` : "";
  return `<div class="rev ${rv.ok ? "ok" : ""}">
    <b>${rv.ok ? "Review passed" : "Review flagged"}</b> ${esc(rv.summary || "")}
    ${list(rv.findings, "")}${list(rv.earlier, "flagged on the earlier pass, and sent back:")}${list(rv.followups, "found alongside it:")}${filed}${list(rv.held, "held at the proposal cap, not filed:")}${unver}
  </div>`;
}
function yoursToDo(t) {
  // The step a run left for you (needs_user). Without it on the card, a task
  // that stopped short because the release was yours read as finished.
  const n = t.needs_user;
  // Only while it can still be acted on: waiting on you, or on approval first.
  if (!n || !n.action || (t.state !== "needs_input" && t.state !== "awaiting_approval")) return "";
  return `<div class="todo">🙋 <b>yours to do:</b> <code>${esc(n.action)}</code>${
    n.why ? ` — ${esc(n.why)}` : ""}</div>`;
}
async function releaseStep(id, round) {
  // Runs only the !release the task recorded; the request names nothing else.
  if (!confirm("Run the release this task left for you?\n\n"
             + "It is the same as typing the !release shown on the card. The "
             + "result is posted to the task's thread, and the task is done "
             + "only if the release goes out.")) return;
  await taskAction(id, "release", "", round);
}
function lastEvent(t) {
  // Why a task stopped is already on the record -- transition() writes the
  // reason into events -- the row just never showed it. Prefer the event that
  // put it in the state it is in; fall back to whatever happened last.
  const ev = t.events || [];
  const e = ev.filter(x => x.kind === t.state).slice(-1)[0] || ev.slice(-1)[0];
  return (e && e.detail) || "";
}
function taskBody(t) {
  // A title is the goal's first line cut at sixty characters. For a nightly
  // proposal that is the opening clause of two thousand words of evidence --
  // file names, line numbers, what done looks like -- and the row asks you to
  // spend a session on it or throw it away, so opening the row shows it all.
  const goal = (t.goal || "").trim();
  // A short goal is its own title; there is nothing behind it to open.
  // Research's findings are what you are deciding on, so they are behind it too.
  const said = hasFindings(t) && (t.result || {}).text
    ? `<div class="sect">findings</div><pre class="goal">${esc(t.result.text)}</pre>` : "";
  return (goal && goal !== (t.title || "").trim()
    ? `<pre class="goal">${esc(goal)}</pre>` : "") + said;
}
function taskDetail(t) {
  const why = t.state === "failed" ? lastEvent(t) : "";
  const body = taskBody(t);
  if (!body && !why) return "";
  return (why ? `<div class="why">${esc(why)}</div>` : "") + body;
}
// A row carries whether anything is behind its title (`more`) and why a failure
// stopped (`why`) -- board.panel_row -- rather than the goal and reply, which
// were 6 MB polled every five seconds to fill disclosures nobody had opened. A
// whole record, from a bot older than `slim`, is asked the same questions.
function rowMore(t) { return t.more !== undefined ? !!t.more : !!taskBody(t); }
function rowWhy(t) {
  return t.why !== undefined ? t.why : (t.state === "failed" ? lastEvent(t) : "");
}
function moreToggled(el, id) { if (el.open) openMore(id); }
async function openMore(id) {
  const box = document.getElementById("more-" + id);
  if (!box || box.dataset.loaded) return;
  const r = await taskCall({action: "task", id, full: true});
  if (!r.ok) { box.innerHTML = `<div class="hint">${esc(r.error || "bot offline")}</div>`; return; }
  box.innerHTML = taskBody(r.task) || `<div class="hint">nothing more to show</div>`;
  box.dataset.loaded = "1";
}
function landing(t) {
  // A task whose commits never reached the base must not read as plainly done.
  // The record is only written for projects that land their own work -- the
  // branch is the deliverable everywhere else, and a warning on every task
  // would hide the one that means something -- so anything present here is
  // worth showing, including the refusals that never reached git.
  const l = (t.result || {}).landing;
  if (!l) return "";
  if (l.landed)
    return `<div class="land ok">landed ${esc((l.head || "").slice(0, 8))}</div>`;
  if (l.stage === "in-progress") return `<div class="land">landing…</div>`;
  const d = l.detail ? `<div class="d">${esc(String(l.detail).slice(-300))}</div>` : "";
  // Never a candidate (unverified, no suite) versus git asked and refused.
  // Only the second leaves a branch for anyone to do something about.
  // A task that found its job already done: not a failure to land anything.
  if (l.stage === "nothing-to-land")
    return `<div class="land ok">nothing to land — ${esc(l.detail || "")}</div>`;
  if (!l.eligible)
    return `<div class="land">not landed — ${esc(l.detail || l.stage || "?")}</div>`;
  // Its branch moved to a task catching it up with the base; that one has it.
  if (l.reworked_by)
    return `<div class="land">not landed (${esc(l.stage || "?")}) — handed to ${
      esc(l.reworked_by)} to catch up with the base</div>`;
  return `<div class="land bad">not landed (${esc(l.stage || "?")}) — ${
    esc(l.branch || "the branch")} is waiting for you${d}</div>`;
}
// Mirrors slacklinks.thread_link: without thread_ts and cid, Slack opens the
// app (its Home tab) rather than the thread.
function threadLink(key) {
  const [ch, ts] = key.split(":");
  return `https://slack.com/archives/${ch}/p${ts.replace(".", "")}?thread_ts=${ts}&cid=${ch}`;
}
function held(t) {
  // Said on the row itself, because approving or dismissing is the moment the
  // files in that checkout stop being anybody's business.
  const h = heldById[t.id];
  if (!h) return "";
  return ` · <span class="held" title="${esc(h.path)}\n\n${
    esc((h.files || []).join("\n"))}">🧰 checkout holds ${h.changes} uncommitted</span>`;
}
// Mirrors costs.fmt: what the task cost, its reviews included. A missing
// cost is unknown, never zero, so a total with a part missing is a floor
// and says so with "+"; one with nothing known at all is "$?".
function costText(t) {
  const c = t.cost_total;
  if (!c) return "";
  const usd = c.usd || 0;
  if (!usd && !c.complete) return ` · <span title="cost not recorded">$?</span>`;
  const s = usd >= 100 ? `$${Math.round(usd).toLocaleString("en-US")}` : `$${usd.toFixed(2)}`;
  return ` · <span title="${c.complete ? "this task and its reviews"
    : "a lower bound: some runs recorded no cost"} (API list-price equivalent, not billed)">${s}${c.complete ? "" : "+"}</span>`;
}
function taskButtons(t) {
  const b = [];
  if (t.state === "proposed") {
    b.push(`<button class="ghost" onclick="taskAction('${t.id}','accept')">Accept</button>`);
    b.push(`<button class="ghost" onclick="taskAction('${t.id}','dismiss')">Dismiss</button>`);
  } else if (t.state === "awaiting_approval") {
    b.push(`<button class="act" onclick="taskAction('${t.id}','approve')">Approve</button>`);
    b.push(`<button class="ghost" onclick="sendBack('${t.id}',false)">Send back…</button>`);
    b.push(`<button class="ghost" onclick="taskAction('${t.id}','dismiss')">Dismiss</button>`);
  } else if (t.findings) {         // research waiting on you: hasFindings(t)
    // Research waiting on you with its findings. Close ends it; Dig deeper
    // sends it back with your question; Discuss opens its thread in Slack,
    // linked the one way slacklinks builds every thread link.
    const n = t.needs_user || {};
    if (n.release)
      b.push(`<button class="act" title="!release ${esc(n.release)}" onclick="releaseStep('${t.id}','${esc(t.round || "")}')">Release</button>`);
    b.push(`<button class="act" onclick="closeFindings('${t.id}','${esc(t.round || "")}')">Close</button>`);
    b.push(`<button class="ghost" onclick="digDeeper('${t.id}','${esc(t.round || "")}')">Dig deeper…</button>`);
    if (t.thread)
      b.push(`<a class="ghost" target="_blank" href="${esc(t.thread_link || threadLink(t.thread))}">Discuss ↗</a>`);
    b.push(`<button class="ghost" onclick="taskAction('${t.id}','dismiss','','${esc(t.round || "")}')">Dismiss</button>`);
  } else if (t.state === "needs_input") {
    // A step the run left for you: run it from here when it is a release, or
    // say you took it. Answer still sends it back with something to go on.
    const n = t.needs_user || {};
    if (n.release)
      b.push(`<button class="act" title="!release ${esc(n.release)}" onclick="releaseStep('${t.id}')">Release</button>`);
    if (n.action)
      b.push(`<button class="ghost" title="you took the step; close it" onclick="taskAction('${t.id}','resolve')">Done</button>`);
    b.push(`<button class="ghost" onclick="sendBack('${t.id}',true)">Answer…</button>`);
    b.push(`<button class="ghost" onclick="taskAction('${t.id}','dismiss')">Dismiss</button>`);
  } else if (t.state === "failed") {
    b.push(`<button class="ghost" onclick="taskAction('${t.id}','retry')">Retry</button>`);
    b.push(`<button class="ghost" onclick="taskAction('${t.id}','dismiss')">Dismiss</button>`);
  } else if (t.state === "running") {
    // A running row used to offer nothing but Thread, which left the one
    // surface that lists running tasks unable to stop one: the only way in was
    // to open the anchor thread and type !stop. Said as "Stop" rather than
    // "Cancel" because it ends a live agent mid-sentence rather than dropping a
    // queue entry, and the toast reports which of those actually happened.
    b.push(`<button class="ghost" onclick="stopTask('${t.id}')">Stop agent…</button>`);
  } else if (t.state === "queued" || t.state === "blocked") {
    b.push(`<button class="ghost" onclick="taskAction('${t.id}','cancel')">Cancel</button>`);
  }
  // close the panel on the way, or the thread opens behind it
  if (t.thread) b.push(`<button class="ghost" onclick="toggleTasks();jumpTo('${t.thread}')">Thread</button>`);
  return b.join("");
}
async function renderTasks() {
  const list = document.getElementById("tlist");
  const project = document.getElementById("tproj").value;
  renderUnmerged();
  // Awaited, not fired off: a row has to say whether its checkout is still
  // holding work before it offers a button that makes that work unreachable.
  await renderHolding();
  const r = await taskRead({action: taskView === "attention" ? "attention" : "list",
                            project: project || undefined, slim: true});
  if (!r.ok) { setHTML(list, `<div class="hint">${esc(r.error || "bot offline")}</div>`); return; }
  updateTaskBadge(r.counts);
  const spend = document.getElementById("spend");
  if (spend) spend.textContent = r.spend_line ? `💰 ${r.spend_line}` : "";
  if (!r.tasks.length) {
    setHTML(list, taskView === "attention"
      ? `<div class="hint">Nothing needs you. 🎉</div>`
      : `<div class="hint">No tasks yet — queue one above.</div>`);
    return;
  }
  setHTML(list, r.tasks.map(t => {
    const why = rowWhy(t), more = rowMore(t);
    // What is behind the title is fetched when the row is opened (openMore).
    const title = why || more
      ? `<details class="more" ontoggle="moreToggled(this,'${esc(t.id)}')"><summary>${esc(t.title)}</summary>${
          why ? `<div class="why">${esc(why)}</div>` : ""}${
          more ? `<div class="lazy" id="more-${esc(t.id)}"><div class="hint">loading…</div></div>` : ""}</details>`
      : esc(t.title);
    return `<div class="task" data-task="${esc(t.id)}">
      <span class="st st-${esc(t.state)}">${esc(t.state)}</span>
      <span class="tt">${title}
        <div class="sub">${t.project ? `<span class="proj">${esc(t.project)}</span> · ` : ""}${
          esc(t.id)} · ${esc(t.source)}${t.attempts > 1 ? ` · attempt ${t.attempts}` : ""} · ${
          age(t.created)}${costText(t)}${held(t)}</div>${yoursToDo(t)}${findings(t)}${review(t)}${landing(t)}</span>
      ${taskButtons(t)}</div>`;
  }).join(""));
}
// Work that finished and never reached the base. Nothing in the dashboard
// mentioned branches at all, so eight completed tasks left eight unmerged
// commits and the board showed eight plain "done". Its own request rather than
// part of the task list, because the badge polls that every five seconds and
// this one asks git.
async function renderUnmerged() {
  const el = document.getElementById("unmerged");
  if (!el) return;
  const project = document.getElementById("tproj").value;
  const r = await taskCall({action: "unmerged", project: project || undefined});
  const rows = (r && r.unmerged) || [];
  if (!rows.length) { el.innerHTML = ""; return; }
  el.innerHTML = `<span class="nlabel">🌿 ${esc(r.summary || "")}</span>` +
    rows.map(b => {
      // null means git was asked for the count and would not answer. Shown
      // as "?" rather than dropped or zeroed: a branch nobody can measure is
      // still a branch nobody merged, and this panel is the only place it
      // gets named.
      const n = (b.commits === null || b.commits === undefined) ? "?" : b.commits;
      // A branch that exists only on a remote is real unmerged work, but
      // `git switch` will not find it under the bare name, so the name says
      // where it is.
      const where = (b.local === false && b.remote) ? esc(b.remote) + "/" : "";
      const tip = `${esc(b.title)}\n${esc(b.head)} · ${esc(b.id)} · ${
        esc(b.state)} · off ${esc(b.base)}${
        n === "?" ? "\ngit could not count its commits" : ""}${
        where ? "\nno local branch — only on " + esc(b.remote) : ""}\n${esc(b.repo)}`;
      const label = `${where}${esc(b.branch.replace(/^silkworm\//, ""))} <b>${n}</b>`;
      const name = b.thread
        ? `<button class="b" title="${tip}" onclick="toggleTasks();jumpTo('${esc(b.thread)}')">${label}</button>`
        : `<span class="b" title="${tip}">${label}</span>`;
      // Land: the same landing Approve starts (rebase, suite, merge, suite).
      // Drop: delete the branch, tagging what no other ref holds first.
      // Both buttons act on the local branch. With none, Land would report
      // "its branch is gone: nothing to land" over commits sitting on the
      // remote, and Drop would delete nothing -- so neither is offered, and
      // the row stays a name to go and fetch.
      if (where) return `<span class="ub">${name}</span>`;
      return `<span class="ub">${name}`
        + `<button class="ghost" title="rebase onto the base, test, merge, test" `
        + `onclick="taskAction('${esc(b.id)}','land')">Land</button>`
        + `<button class="ghost" title="delete the branch; its commits are kept under a tag" `
        + `onclick="if(confirm('Drop ${esc(b.branch)}? Its commits are kept under a discarded/ tag.'))taskAction('${esc(b.id)}','drop')">Drop</button></span>`;
    }).join("");
}
// Checkouts that still hold uncommitted files. worktrees.py refuses to delete
// one, which is right, and said so only in a log line every half hour -- 508 of
// them over six days, for two trader checkouts nobody could see from here. Its
// own request for the same reason as the branch survey: it walks every worktree
// and asks git about each.
let heldById = {};
async function renderHolding() {
  const el = document.getElementById("holding");
  heldById = {};
  if (!el) return;
  const project = document.getElementById("tproj").value;
  // Swallowed, because renderTasks awaits this before drawing anything: a
  // blip on the survey must cost the marker on a row, never the list of rows.
  // Driving it against a rejecting fetch is how that was found.
  let r = null;
  try { r = await taskCall({action: "holding", project: project || undefined}); }
  catch (e) { r = null; }
  if (!r || r.ok === false) {
    // "Could not ask" is not "nothing held" -- `silkworm status` already draws
    // that line and the panel did not. A bot running older code answers
    // {ok: false, unknown action}, which read as an all-clear and quietly took
    // the marker off every row while leaving the buttons that act on them.
    el.innerHTML = `<span class="nlabel">🧰 could not ask whether any checkout `
      + `is holding uncommitted work${r && r.error ? ` — ${esc(r.error)}` : ""}</span>`;
    return;
  }
  const rows = r.holding || [];
  rows.forEach(h => { if (h.id) heldById[h.id] = h; });
  if (!rows.length) { el.innerHTML = ""; return; }
  el.innerHTML = `<span class="nlabel">🧰 ${esc(r.summary || "")}</span>` +
    rows.map(h => {
      // The file list is the point: a checkout held by a virtualenv and one
      // held by a script somebody wrote look identical as a count.
      const who = h.title || (h.known ? h.id : "no task on the board owns this");
      const tip = `${esc(who)}\n${esc(h.state || "unknown state")}${
        h.terminal ? " · finished, so nothing will ask about it again" : ""}\n${
        esc(h.path)}\non ${esc(h.branch || "?")}\n\n${esc((h.files || []).join("\n"))}`;
      const label = `${esc(h.name)} <b>${h.changes}</b>`;
      return h.thread
        ? `<button class="b" title="${tip}" onclick="toggleTasks();jumpTo('${esc(h.thread)}')">${label}</button>`
        : `<span class="b" title="${tip}">${label}</span>`;
    }).join("");
}
function updateTaskBadge(counts) {
  const need = ["proposed", "awaiting_approval", "needs_input", "failed"]
    .reduce((n, k) => n + ((counts || {})[k] || 0), 0);
  document.getElementById("taskbadge").textContent = need ? String(need) : "";
}
async function refreshTaskBadge() {
  // Polled with the session list: the counts alone, not two hundred tasks.
  let r = await taskRead({action: "counts"});
  // A bot older than `counts` refuses it; the whole list still has them.
  if (!r.ok) r = await taskCall({action: "list"});
  if (r.ok) updateTaskBadge(r.counts);
}
function toggleLearn() {
  const m = document.getElementById("learnmodal");
  const open = m.style.display !== "flex";
  m.style.display = open ? "flex" : "none";
  if (open) { document.getElementById("lscope").value = active ? active.cwd : ""; renderLearnings(); }
}
async function renderLearnings() {
  const r = await learnCall({action: "list"});
  const list = document.getElementById("llist");
  const items = r.learnings || [];
  if (!items.length) { list.innerHTML = `<div class="hint">No learnings yet — run a few sessions, then Harvest.</div>`; return; }
  list.innerHTML = items.map(x => {
    const on = x.enabled !== false;
    const src = x.source && x.source.includes(":")
      ? `<a href="${threadLink(x.source)}" target="_blank">source</a>` : "";
    return `<div class="lrow ${on ? "" : "off"}">
      <button class="tog" title="${on ? "disable" : "enable"}" onclick="toggleLearning('${x.id}', ${!on})">${on ? "🟢" : "⚪️"}</button>
      <span class="t ${x.type}">${x.type}</span>
      <span class="origin ${x.origin === "harvest" ? "harvest" : ""}">${x.origin === "harvest" ? "auto" : "manual"}</span>
      <span class="body">${esc(x.text)}<div class="scope">${x.scope ? "📁 " + esc(x.scope) : "🌍 global"} · ${x.id} ${src}</div></span>
      <button class="del" title="delete" onclick="delLearning('${x.id}')">✕</button></div>`;
  }).join("");
}
async function toggleLearning(id, enabled) {
  await learnCall({action: "toggle", id, enabled}); renderLearnings();
}
async function harvestNow(btn) {
  btn.disabled = true; btn.textContent = "✨ Harvesting…";
  const r = await learnCall({action: "harvest"});
  btn.disabled = false; btn.textContent = "✨ Harvest now";
  toast(r.ok ? `Harvested ${r.added} from ${r.scanned} sessions` : "Failed: " + (r.error || "unknown"));
  renderLearnings();
}
async function syncLearnings(btn) {
  btn.disabled = true; btn.textContent = "⇅ Syncing…";
  const r = await learnCall({action: "sync"});
  btn.disabled = false; btn.textContent = "⇅ Sync";
  toast(r.ok ? (r.note || "Synced") : "Sync: " + (r.error || "unknown"));
  renderLearnings();
}
async function addLearning() {
  const text = document.getElementById("ltext").value.trim();
  if (!text) return;
  const r = await learnCall({action: "add", type: document.getElementById("ltype").value,
    text, scope: document.getElementById("lscope").value.trim()});
  if (r.ok) { document.getElementById("ltext").value = ""; renderLearnings(); toast("Learning added"); }
  else toast("Failed: " + (r.error || "unknown"));
}
async function delLearning(id) {
  const r = await learnCall({action: "delete", id});
  if (r.ok) renderLearnings();
}

// --- projects overview and per-project boards ---
// The task panel answers "what needs me"; this answers "where is each project".
// Read through the bot's /tasks route (overview, board, task), which reuses a
// cached branch survey and release plan rather than asking git per poll. Every
// button is one the task panel already offers, through taskAction.
const UNFILED = "__unfiled__";            // board.UNFILED: work under no project
const BOARD_COLUMNS = [["backlog", "Backlog"], ["running", "Running"],
  ["review", "In review"], ["needs", "Needs you"], ["done", "Done · 14d"]];
let boardProject = null;                  // null: overview; "": all; UNFILED; a slug
let boardDetailId = null;
let boardRows = [];                       // the overview's projects, for the picker
let lastSessions = [];                    // from loadList, for the unfiled threads
let boardTimer = null;
let boardAsk = 0;                         // newest board request; older answers are dropped
let boardForm = null;                     // the open modal form: {kind, ...}; null when closed
let boardInfo = null;                     // the open project's name and readiness, from `board`
let formProjects = [];                    // /projects list, with each one's `unready`
let formRoles = [];                       // /tasks roles: name, hint, default, held_if_unready
let goalBounds = {min: 0, max: 0};        // scoping.validate's, from the roles route

function boardIsOpen() {
  return document.getElementById("boardmodal").style.display === "flex";
}
function toggleBoard() {
  const m = document.getElementById("boardmodal");
  const open = !boardIsOpen();
  m.style.display = open ? "flex" : "none";
  if (open) refreshBoard(); else closeDetail();
}
function closeBoard() {
  document.getElementById("boardmodal").style.display = "none";
  closeDetail();
}
function setBoardCol(k) {
  boardCol = k;
  const c = document.querySelector(".bcols");
  if (c) c.dataset.show = k;
  document.querySelectorAll(".bpick button").forEach(b =>
    b.className = b.dataset.col === k ? "on" : "");
}
function setBoardProject(p) {
  boardProject = p;
  boardCol = "";
  // Back to the overview is back to all of it: a filter left set would only
  // turn it straight back into a board.
  if (p === null) for (const id of ["brole", "bstate", "bq"]) document.getElementById(id).value = "";
  closeDetail();
  refreshBoard();
}
function boardSearch() {
  clearTimeout(boardTimer);
  boardTimer = setTimeout(renderBoard, 250);
}
function refreshBoard() {
  renderBoard();
  if (boardDetailId) openCard(boardDetailId);
}
// Mirrors costs.fmt: unknown is "$?", a floor is "+", never a silent zero.
function fmtCost(c) {
  if (!c) return "";
  const usd = c.usd || 0;
  if (!usd && !c.complete) return "$?";
  return (usd >= 100 ? `$${Math.round(usd).toLocaleString("en-US")}` : `$${usd.toFixed(2)}`)
    + (c.complete ? "" : "+");
}
function boardFilters() {
  const proj = document.getElementById("bproj");
  const keep = boardProject === null ? "*" : boardProject;
  const listed = ["*", "", UNFILED, ...boardRows.map(p => p.slug)].includes(keep);
  // Rewritten only when the options change, so a poll does not shut the
  // picker while it is open.
  setHTML(proj, `<option value="*">overview</option><option value="">all projects</option>`
    + boardRows.map(p => `<option value="${esc(p.slug)}">${esc(p.title)}</option>`).join("")
    + `<option value="${UNFILED}">unfiled</option>`
    // a slug the overview has not listed yet
    + (listed ? "" : `<option value="${esc(keep)}">${esc(keep)}</option>`));
  if (proj.value !== keep) proj.value = keep;
  document.getElementById("bvover").className = boardProject === null ? "on" : "";
  document.getElementById("bvall").className = boardProject === "" ? "on" : "";
  document.getElementById("bvunfiled").className = boardProject === UNFILED ? "on" : "";
}
function fillSelect(id, values, label) {
  const sel = document.getElementById(id);
  const keep = sel.value;
  setHTML(sel, `<option value="">${label}</option>` + values.map(v =>
    `<option value="${esc(v)}">${esc(v)}</option>`).join(""));
  const want = values.includes(keep) ? keep : "";
  if (sel.value !== want) sel.value = want;
}
async function renderBoard() {
  const role = document.getElementById("brole").value;
  const state = document.getElementById("bstate").value;
  const q = document.getElementById("bq").value.trim();
  // A filter typed on the overview means "find it", which is a board.
  if (boardProject === null && (role || state || q)) boardProject = "";
  if (boardProject === null) {
    setHTML(document.getElementById("bboard"), "");
    return loadOverview();
  }
  setHTML(document.getElementById("bover"), "");
  boardFilters();
  // A new filter is a new set of columns: the phone's tab goes back to the
  // first that has something in it.
  const fkey = [boardProject, role, state, q].join("\u0000");
  if (fkey !== boardFilterKey) { boardFilterKey = fkey; boardCol = ""; }
  // A poll in flight when a filter changes must not land on top of the
  // filtered answer: only the newest request draws.
  const ask = ++boardAsk;
  const r = await taskRead({action: "board", project: boardProject, role, state, q});
  if (ask !== boardAsk) return;
  const el = document.getElementById("bboard");
  if (!r.ok) { setHTML(el, `<div class="hint">${esc(r.error || "bot offline")}</div>`); return; }
  fillSelect("brole", r.roles || [], "any role");
  fillSelect("bstate", ["proposed", "queued", "running", "blocked", "awaiting_approval",
                        "needs_input", "failed", "done"], "any state");
  boardInfo = r.info || null;
  setHTML(el, boardHead(boardInfo) + renderColumns(r)
    + (boardProject === UNFILED ? unfiledThreads() : ""));
}
// The open project's name, whether it can take unsupervised work, and its
// settings. Only on a project's own board: "all" and "unfiled" have none.
function boardHead(info) {
  if (!info) return "";
  return `<div class="bhead"><h3>${esc(info.title)}</h3>${
    info.unready ? `<span class="warn" title="work the runner would take on its own is held in Needs you instead">⚠ ${
      esc(info.unready)}</span>` : ""}<button class="act" onclick="newTaskForm()">+ New task</button>${
    info.release ? `<button class="ghost" onclick="releaseForm('${esc(info.slug)}')">Release…</button>` : ""}<button class="ghost" onclick="projectSettings('${
    esc(info.slug)}')">⚙ Settings</button></div>`;
}
// On a phone the board is one column at a time (CSS shows the one named by
// data-show), picked from a row of tabs; on a laptop the tabs are hidden and
// all five columns stand side by side. The pick lasts until the project changes.
let boardCol = "", boardFilterKey = "";
function renderColumns(r) {
  const cols = r.columns || {};
  const n = k => (cols[k] || []).length;
  if (!BOARD_COLUMNS.some(([k]) => k === boardCol))
    boardCol = n("needs") ? "needs" : (BOARD_COLUMNS.find(([k]) => n(k)) || BOARD_COLUMNS[0])[0];
  return `<div class="bpick">` + BOARD_COLUMNS.map(([k, label]) =>
    `<button data-col="${k}" class="${k === boardCol ? "on" : ""}" onclick="setBoardCol('${k}')">${
      label}<b>${n(k)}</b></button>`).join("") + `</div>`
    + `<div class="bcols" data-show="${boardCol}">` + BOARD_COLUMNS.map(([k, label]) => {
    const items = (r.columns || {})[k] || [];
    return `<div class="bcol" data-col="${k}"><h4>${label} · ${items.length}</h4>${
      items.map(boardCard).join("") || `<div class="hint">—</div>`}</div>`;
  }).join("") + `</div>`;
}
function cardReview(t) {
  const rv = t.review;
  if (!rv) return "";
  const n = rv.findings + rv.followups;
  return `<div class="rv ${rv.ok ? "ok" : "bad"}" title="${esc(rv.summary || "")}">${
    rv.ok ? "✓ review passed" : "⚑ review flagged"}${n ? ` · ${rv.findings} finding${
    rv.findings === 1 ? "" : "s"}${rv.followups ? `, ${rv.followups} follow-up${
    rv.followups === 1 ? "" : "s"}` : ""}` : ""}</div>`;
}
function cardLanding(t) {
  // The same words as the task panel's landing(); a card only drops the detail.
  if (t.landing) return landing({result: {landing: t.landing}});
  if (t.state === "done" && t.unmerged)
    return `<div class="land bad">not landed — ${esc(t.unmerged.branch || "its branch")} holds ${
      t.unmerged.commits === null ? "?" : t.unmerged.commits} commit(s)</div>`;
  return "";
}
function cardButtons(t) {
  // taskButtons() is the task panel's; its Thread button closes that panel,
  // so here it closes this one instead.
  let b = taskButtons(t).split("toggleTasks();jumpTo(").join("closeBoard();jumpTo(");
  // Only before it has run: after that, Send back with notes is the edit.
  if ((t.state === "proposed" || t.state === "queued") && t.role !== "reviewer")
    b += `<button class="ghost" onclick="editTaskForm('${esc(t.id)}')">Edit…</button>`;
  if (t.state === "queued" && t.role !== "reviewer")
    b += t.priority
      ? `<button class="ghost" title="back to its place, oldest first" onclick="runNext('${esc(t.id)}',false)">Unpin</button>`
      : `<button class="ghost" title="the runner claims this before older queued work" onclick="runNext('${esc(t.id)}',true)">Run next</button>`;
  // Land and Drop, for finished work the survey says never reached the base --
  // the unmerged strip's buttons, offered only on a local branch for the same
  // reason as there.
  if (t.state === "done" && t.unmerged && t.unmerged.local !== false
      && !(t.landing && t.landing.reworked_by)) {
    b += `<button class="ghost" title="rebase onto the base, test, merge, test" `
      + `onclick="taskAction('${esc(t.id)}','land')">Land</button>`
      + `<button class="ghost" title="delete the branch; its commits are kept under a tag" `
      + `onclick="if(confirm('Drop ${esc(t.unmerged.branch || t.id)}? Its commits are kept under a discarded/ tag.'))taskAction('${esc(t.id)}','drop')">Drop</button>`;
  }
  return b;
}
function boardCard(t) {
  const cost = fmtCost(t.cost_total);
  return `<div class="bcard" data-id="${esc(t.id)}" onclick="openCard('${esc(t.id)}')">
    <div class="tt">${esc(t.title || t.id)}</div>
    <div class="sub">${t.project && boardProject === "" ? `<span class="proj">${esc(t.project)}</span> · ` : ""}${
      esc(t.role)}${t.state === "blocked" || t.state === "proposed" || t.state === "failed"
        || t.state === "needs_input" || t.state === "awaiting_approval"
        ? ` · <span class="st st-${esc(t.state)}">${esc(t.state)}</span>` : ""} · ${
      age(t.created)}${cost ? ` · <span title="API list-price equivalent, not billed">${cost}</span>` : ""}${
      t.attempts > 1 ? ` · attempt ${t.attempts}` : ""}${
      t.queue_pos ? ` · <span class="qpos" title="its place in the runner's queue, all projects">${
        t.priority ? "📌 " : ""}#${t.queue_pos} in queue</span>` : ""}</div>
    ${t.why ? `<div class="why">${esc(t.why)}</div>` : ""}${yoursToDo(t)}${
      t.research && t.research.line ? `<div class="rv ok">🔎 ${esc(t.research.line)}</div>` : ""}${cardReview(t)}${cardLanding(t)}
    <div class="acts" onclick="event.stopPropagation()">${cardButtons(t)}</div></div>`;
}
function unfiledThreads() {
  // Conversations filed under no project. Task-run threads are left out: the
  // tasks they narrate are on the board above.
  const rows = lastSessions.filter(s => !s.project && !s.hidden && (s.kind || "thread") !== "task");
  return `<div class="bthreads"><div class="hint">💬 ${rows.length} thread${
    rows.length === 1 ? "" : "s"} with no project</div>` + rows.map(s =>
    `<button class="ghost b" title="${esc(s.key)}" onclick="closeBoard();jumpTo('${esc(s.key)}')">${
      esc(s.title || s.key)} · ${age(s.updated)}</button>`).join("") + `</div>`;
}
async function loadOverview() {
  const ask = ++boardAsk;
  const r = await taskRead({action: "overview"});
  if (ask !== boardAsk || boardProject !== null) return;   // superseded meanwhile
  const el = document.getElementById("bover");
  if (!r.ok) { setHTML(el, `<div class="hint">${esc(r.error || "bot offline")}</div>`); return; }
  boardRows = r.projects || [];
  boardFilters();
  setHTML(el, renderOverview(r));
}
function renderOverview(r) {
  const cards = (r.projects || []).map(projectCard);
  const u = r.unfiled;
  const threads = lastSessions.filter(s => !s.project && !s.hidden && (s.kind || "thread") !== "task").length;
  if (u) cards.push(projectCard(u, threads));
  return `<div class="bhead"><h3>Projects</h3><button class="act" onclick="newProjectForm()">+ New project</button></div>`
    + `<div class="pcards">${cards.join("") || `<div class="hint">No projects yet.</div>`}</div>`
    + archivedSection(r.archived || [])
    + (r.note ? `<div class="hint" style="margin-top:8px">Costs are ${esc(r.note)}.</div>` : "");
}
// Archived projects, collapsed, so one can be brought back from where it went.
function archivedSection(rows) {
  if (!rows.length) return "";
  return `<details class="parch"><summary>Archived · ${rows.length}</summary>${rows.map(p =>
    `<div class="arow" data-slug="${esc(p.slug)}">${esc(p.title)} <span class="slug">${esc(p.slug)}</span>
      · ${p.tasks} task${p.tasks === 1 ? "" : "s"}
      <button class="ghost" onclick="archiveProject('${esc(p.slug)}',false)">Unarchive</button></div>`).join("")}</details>`;
}
function releaseText(rel) {
  if (!rel) return "";
  if (rel.error) return `<div class="row warn"><span class="lbl">release</span>release.toml: ${esc(rel.error)}</div>`;
  const parts = (rel.targets || []).map(t => t.commits
    ? `<b>${esc(t.target)}</b> ${t.commits} commit${t.commits === 1 ? "" : "s"} → ${esc(t.version || "?")}`
    : `<span class="off">${esc(t.target)} nothing pending</span>`);
  return `<div class="row"><span class="lbl">release</span>${parts.join(" · ") || "no targets"}</div>`;
}
function projectCard(p, threads) {
  const c = p.counts || {};
  const order = ["proposed", "queued", "running", "blocked", "awaiting_approval",
                 "needs_input", "failed", "done", "cancelled"];
  const chips = order.filter(k => c[k]).map(k =>
    `<span class="st st-${k}">${k} ${c[k]}</span>`).join("");
  const running = (p.running || []).map(t => esc(t.title || t.id)).join(" · ");
  const last = p.last_landed;
  const um = p.unmerged || {};
  const rd = p.readiness;
  const yes = (on, label, tip) => `<span class="${on ? "on" : "off"}" title="${esc(tip || "")}">${on ? "✓" : "✗"} ${label}</span>`;
  const slug = p.slug === UNFILED ? UNFILED : p.slug;
  return `<div class="pcard" data-slug="${esc(slug)}" onclick="setBoardProject('${esc(slug)}')">
    <h3>${esc(p.title)}${p.slug !== UNFILED ? ` <span class="slug">${esc(p.slug)}</span>` : ""}${
      p.needs ? ` <span class="st st-failed">${p.needs} need you</span>` : ""}${
      p.slug !== UNFILED ? `<button class="ghost gear" title="settings" onclick="event.stopPropagation();projectSettings('${esc(p.slug)}')">⚙</button>` : ""}</h3>
    ${p.main_failing ? `<div class="row"><span class="warn" title="the base fails its own suite: new work is paused, and tasks failing only these tests are held">🔴 ${esc(p.main_failing.line)}</span></div>` : ""}
    <div class="row chips">${chips || `<span class="off">no tasks</span>`}</div>
    <div class="row"><span class="lbl">running</span>${running || `<span class="off">nothing</span>`}</div>
    <div class="row"><span class="lbl">last landed</span>${last
      ? `<code>${esc(last.head)}</code> ${esc(last.title)} · ${age(last.at)}` : `<span class="off">nothing recorded</span>`}</div>
    <div class="row"><span class="lbl">unmerged</span>${um.branches
      ? `<span class="warn">${um.branches} branch${um.branches === 1 ? "" : "es"} · ${um.commits}${um.unknown ? "+?" : ""} commit${um.commits === 1 ? "" : "s"}</span>`
      : `<span class="off">none</span>`}</div>
    ${rd ? `<div class="row"><span class="lbl">ready</span>${
      yes(rd.test_cmd, "tests", rd.test_cmd || "no test command")} · ${
      yes(rd.auto_merge, "auto-merge")} · ${yes(rd.publish, "publish")}${
      rd.unready ? `<div class="off" style="font-size:11px">${esc(rd.unready)}</div>` : ""}</div>` : ""}
    <div class="row"><span class="lbl">this week</span>${fmtCost(p.cost_week) || `<span class="off">$0</span>`}</div>
    ${releaseText(p.release)}${p.slug !== UNFILED && releaseButton(p.slug, p.release)
      ? `<div class="row">${releaseButton(p.slug, p.release)}</div>` : ""}
    ${threads !== undefined ? `<div class="row"><span class="lbl">threads</span>${threads} with no project</div>` : ""}
  </div>`;
}
function closeDetail() {
  boardDetailId = null;
  const el = document.getElementById("bdetail");
  el.style.display = "none";
  setHTML(el, "");
}
async function openCard(id) {
  boardDetailId = id;
  const r = await taskRead({action: "task", id});
  if (boardDetailId !== id) return;      // another card was opened meanwhile
  const el = document.getElementById("bdetail");
  if (!r.ok) { setHTML(el, `<div class="hint">${esc(r.error || "bot offline")}</div>`); el.style.display = "block"; return; }
  setHTML(el, renderDetail(r.task));
  el.style.display = "block";
}
function renderDetail(t) {
  const events = (t.events || []).slice().reverse().map(e =>
    `<div class="ev"><span class="k k-${esc(e.kind || "")}">${esc(e.kind || "")}</span>
     <span class="t">${e.at ? age(e.at) : ""}</span>
     <span class="d">${esc(e.detail || "")}</span></div>`).join("");
  const reviews = (t.reviews || []).map(x => `${esc(x.id)} (${esc(x.state || "")})`).join(", ");
  const l = (t.result || {}).landing;
  return `<button class="ghost" style="float:right" onclick="closeDetail()">✕</button>
    <h3>${esc(t.title || t.id)}</h3>
    <div class="sub">${esc(t.id)} · ${esc(t.role || "")} · <span class="st st-${esc(t.state)}">${esc(t.state)}</span>${
      t.project ? ` · ${esc(t.project)}` : " · unfiled"} · ${esc(t.source || "")} · ${age(t.created)}${costText(t)}${
      t.branch ? ` · ${esc(t.branch)}${t.commits ? ` (${t.commits})` : ""}` : ""}</div>
    <div class="acts" style="margin-top:8px">${cardButtons(t)}${t.thread_link
      ? `<a class="ghost" href="${esc(t.thread_link)}" target="_blank">Slack thread ↗</a>` : ""}</div>
    ${yoursToDo(t)}
    <div class="sect">goal</div><pre class="goal">${esc(t.goal || "")}</pre>
    ${t.research && (t.result || {}).text
      ? `<div class="sect">findings</div><pre class="goal">${esc(t.result.text)}</pre>` : ""}
    ${findings(t)}
    ${t.state === "failed" ? `<div class="sect">why it stopped</div><div class="rev">${esc(lastEvent(t))}</div>` : ""}
    ${(t.result || {}).review ? `<div class="sect">review</div>${review(t)}` : ""}
    ${reviews ? `<div class="sub">reviewed by ${reviews}</div>` : ""}
    ${l || (t.result || {}).landed ? `<div class="sect">landing</div>${landing(t)}${l ? `<div class="sub">${
      esc(l.stage || "")}${l.base ? ` · onto ${esc(l.base)}` : ""}${l.at ? ` · ${age(l.at)}` : ""}${
      l.detail && l.landed ? `<div>${esc(String(l.detail))}</div>` : ""}</div>` : ""}` : ""}
    <div class="sect">events · ${(t.events || []).length}</div>${events || `<div class="hint">none</div>`}`;
}

// --- releasing a project, from its card or its board -----------------------------
// One modal, three stages: the plan (what is pending per target, a level for
// each, the dependency order, and each target's preview run as it opens), the
// confirmation (exactly what will reach production), and the release's own
// progress, polled. Every step asks the bot's /releases route; the page holds
// no rule of its own. A start carries the fingerprint of the plan the
// confirmation showed, and the route refuses one that no longer matches.
async function releaseCall(payload) {
  return (await (await fetch("/api/releases", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)})).json());
}
function releaseButton(slug, rel) {
  if (!rel || rel.error || !(rel.targets || []).length) return "";
  return `<button class="act" title="what is pending, its previews, then a confirmation" onclick="event.stopPropagation();releaseForm('${esc(slug)}')">Release…</button>`;
}
// The targets ticked, and the level chosen for each.
function releaseSelection() {
  const f = boardForm || {};
  const offer = f.offer || {targets: []};
  const names = offer.targets.filter(t => {
    const box = document.getElementById(`rsel-${t.target}`);
    return box ? box.checked : t.pending > 0;
  }).map(t => t.target);
  const levels = {};
  for (const n of names) {
    const sel = document.getElementById(`rlvl-${n}`);
    let lvl = sel ? sel.value : (f.levels || {})[n] || "patch";
    if (lvl === "version") {
      // Chosen, not yet typed: kept as the choice, so the box appears.
      const v = (document.getElementById(`rver-${n}`) || {}).value || "";
      lvl = v.trim() || "version";
    }
    levels[n] = lvl;
  }
  return {targets: names, levels};
}
async function releaseForm(slug) {
  openModal(`<div class="tform"><h3>Release ${esc(slug)}</h3>
    <div class="hint">⏳ working out what is pending…</div></div>`,
    {kind: "release", slug, stage: "plan", previews: {}, levels: {}});
  const r = await releaseCall({action: "plan", slug});
  const f = boardForm;
  if (!f || f.kind !== "release" || f.slug !== slug) return;   // closed meanwhile
  if (!r.ok) { document.getElementById("fbox").innerHTML = `<div class="tform"><h3>Release ${
    esc(slug)}</h3><div class="err">${esc(r.error || "bot offline")}</div>${modalButtons("closeModal", "Close")}</div>`; return; }
  f.offer = r;
  // Releasing everything pending is one plan; the levels start where the
  // route put them, a patch each.
  if (r.running) {
    // Its progress only if it is the dashboard's or the board's own, still
    // going; one started by `!release` is told in its thread, and the plan
    // below says a release is running.
    const st = await releaseCall({action: "status", slug});
    if (boardForm !== f) return;
    if (st.run && !st.run.finished) return releaseProgress(slug);
  }
  drawReleasePlan();
  if (r.blocked) return;                 // nothing to confirm; no previews either
  for (const t of r.targets) if (t.pending && (t.preview || []).length) runPreview(slug, t.target);
}
async function runPreview(slug, target) {
  const f = boardForm;
  f.previews[target] = {running: true};
  drawPreview(target);
  const r = await releaseCall({action: "preview", slug, target});
  if (boardForm !== f) return;
  f.previews[target] = r;
  drawPreview(target);
}
function previewHtml(target) {
  const p = ((boardForm || {}).previews || {})[target];
  if (!p) return "";
  if (p.running) return `<div class="note"><span class="spin">⏳</span> running ${esc(target)}'s preview…</div>`;
  if (!p.ok) return `<div class="err">preview: ${esc(p.error || "failed")}</div>`;
  return (p.steps || []).map(s => `<div class="note">preview <code>${esc(s.command)}</code>${
    s.ok ? "" : ` <span class="err">exit ${esc(String(s.code))}</span>`}</div><pre class="goal">${
    esc((s.output || "").trim() || "(no output)")}</pre>`).join("");
}
function drawPreview(target) {
  const el = document.getElementById(`rpv-${target}`);
  if (el) el.innerHTML = previewHtml(target);
}
function releaseLevels(t, single) {
  const v = t.versions || {};
  const opts = [["patch", `patch → ${v.patch || "?"}`], ["minor", `minor → ${v.minor || "?"}`]];
  // Major and an explicit version are for one target on its own, as !release has it.
  if (single) opts.push(["major", `major → ${v.major || "?"}`], ["version", "exact version…"]);
  const want = (boardForm.levels || {})[t.target] || "patch";
  const cur = opts.find(o => o[0] === want) ? want : (single && /^\d/.test(want) ? "version" : "patch");
  return `<select id="rlvl-${esc(t.target)}" onchange="releaseChanged()">${opts.map(o =>
    `<option value="${o[0]}"${o[0] === cur ? " selected" : ""}>${esc(o[1])}</option>`).join("")}</select>${
    cur === "version" ? `<input type="text" id="rver-${esc(t.target)}" placeholder="x.y.z" value="${
      esc(/^\d/.test(want) ? want : "")}" onchange="releaseChanged()">` : ""}`;
}
function drawReleasePlan() {
  const f = boardForm, r = f.offer;
  const chosen = f.chosen || r.targets.filter(t => t.pending).map(t => t.target);
  const single = chosen.length === 1;
  const rows = r.targets.map(t => `<div class="rtarget">
    <label class="choice"><input type="checkbox" id="rsel-${esc(t.target)}"${
      chosen.includes(t.target) ? " checked" : ""}${t.pending ? "" : " disabled"} onchange="releaseChanged()">
      <span><b>${esc(t.target)}</b> · ${t.pending ? `${t.pending} change${t.pending === 1 ? "" : "s"}` : "nothing pending"} since ${
        esc(t.last || "the start")} · ${t.ship === "tag" ? "the tag is the release" : "deploys from here"}${
        (t.after || []).length ? ` · after ${esc(t.after.join(", "))}` : ""}</span></label>
    ${t.pending ? `<div class="line">${releaseLevels(t, single)}</div>` : ""}
    ${(t.commits || []).map(c => `<div class="note"><code>${esc(c)}</code></div>`).join("")}${
      t.pending > (t.commits || []).length ? `<div class="note">…and ${t.pending - t.commits.length} more</div>` : ""}
    <div id="rpv-${esc(t.target)}">${previewHtml(t.target)}</div></div>`).join("");
  document.getElementById("fbox").innerHTML = `<div class="tform"><h3>Release ${esc(f.slug)}</h3>
    <div class="sub">from ${esc(r.base)} — nothing ships until you confirm</div>${rows}
    <label>order</label><div id="rorder">${releaseOrder(r)}</div>
    <div class="err" id="tferr">${esc(r.blocked ? `Can't release: ${r.blocked}` : "")}</div>
    <div class="acts"><button class="act" id="rgo" onclick="confirmRelease()"${
      r.blocked || !r.confirm ? " disabled" : ""}>Release…</button>
      <button class="ghost" onclick="closeModal()">Cancel</button></div></div>`;
}
function releaseOrder(r) {
  const steps = (r.steps || []).filter(s => s.commits);
  return steps.length ? steps.map((s, i) => `${i + 1}. <b>${esc(s.target)}</b> → <code>${
    esc(s.tag || "")}</code> (${esc(s.level)})`).join("<br>") : `<span class="off">nothing</span>`;
}
// A changed tick or level is a new plan, from the route: its order, its
// versions, its refusals and a new fingerprint.
async function releaseChanged() {
  const f = boardForm;
  if (!f || f.kind !== "release") return;
  const sel = releaseSelection();
  if (sel.targets.length !== 1)          // major/exact only for one target alone
    for (const n of sel.targets)
      if (["major", "version"].includes(sel.levels[n]) || /^\d/.test(sel.levels[n])) sel.levels[n] = "patch";
  f.chosen = sel.targets; f.levels = sel.levels;
  f.offer = Object.assign({}, f.offer, {confirm: ""});
  drawReleasePlan();
  if (!sel.targets.length) { formError("Tick a target to release."); return; }
  if (Object.values(sel.levels).includes("version")) {
    formError("Type the exact version, x.y.z."); return;   // nothing to confirm until then
  }
  const r = await releaseCall({action: "plan", slug: f.slug, ...sel});
  if (boardForm !== f) return;
  if (!r.ok) { formError(r.error || "bot offline"); document.getElementById("rgo").disabled = true; return; }
  f.offer = r;
  drawReleasePlan();
}
// The last look: exactly what reaches production, from the route's own words.
function confirmRelease() {
  const f = boardForm;
  if (!f || !f.offer || !f.offer.confirm || f.offer.blocked) return;
  f.stage = "confirm";
  document.getElementById("fbox").innerHTML = `<div class="tform"><h3>Release ${esc(f.slug)}?</h3>
    <div class="warn">This reaches production and cannot be undone from here:</div>
    <ul class="reffects">${(f.offer.effects || []).map(e => `<li>${esc(e)}</li>`).join("")}</ul>
    <div class="err" id="tferr"></div>
    <div class="acts"><button class="act" onclick="startRelease()">Yes, release</button>
      <button class="ghost" onclick="drawReleasePlan()">Back</button>
      <button class="ghost" onclick="closeModal()">Cancel</button></div></div>`;
}
async function startRelease() {
  const f = boardForm;
  if (!f || f.stage !== "confirm") return;
  // Exactly what the confirmed plan was asked with: no targets is "everything
  // pending", which the route orders differently from the same names listed.
  const sel = f.chosen ? {targets: f.chosen, levels: f.levels || {}} : {};
  const r = await releaseCall({action: "start", slug: f.slug, confirm: f.offer.confirm,
                               by: "dashboard", ...sel});
  if (boardForm !== f) return;
  if (!r.ok) {
    // The plan moved under the confirmation: show the new one to read again.
    if (r.offer) { f.offer = r.offer; drawReleasePlan(); }
    formError(r.error || "bot offline");
    return;
  }
  releaseProgress(f.slug);
}
function releaseProgress(slug) {
  const f = boardForm;
  f.stage = "running";
  document.getElementById("fbox").innerHTML = `<div class="tform"><h3>Releasing ${esc(slug)}</h3>
    <div id="rprog"><span class="spin">⏳</span> starting…</div>
    <div class="note">Also posted to its Slack thread.</div>
    <div class="acts"><button class="ghost" onclick="closeModal()">Close</button></div></div>`;
  pollRelease(slug);
}
async function pollRelease(slug) {
  const f = boardForm;
  if (!f || f.kind !== "release" || f.stage !== "running") return;
  const r = await releaseCall({action: "status", slug});
  if (boardForm !== f) return;
  const run = r.run;
  const el = document.getElementById("rprog");
  if (!run) { el.textContent = "no release recorded"; return; }
  const done = !!run.finished;
  el.innerHTML = (run.lines || []).map(l => `<pre class="goal">${esc(l)}</pre>`).join("")
    + (done ? `<div class="${run.ok ? "on" : "err"}">${run.ok ? "✓ released " + esc((run.released || []).join(", "))
      : "✗ stopped" + ((run.released || []).length ? " after " + esc(run.released.join(", ")) : "")}</div>`
      : `<div><span class="spin">⏳</span> running…</div>`);
  f.finished = done;
  if (!done) setTimeout(() => pollRelease(slug), 2000);
  else loadOverview();
}

// --- creating and editing tasks and projects, in one modal ---
// New task, edit, project settings and new project all open the same modal:
// labelled fields, Cancel and Save, the route's error shown inside it.
// Cancel, Esc and a click outside close it and send nothing. Everything goes
// through the bot's own routes -- /tasks create, edit, run-next and /projects.
async function projectCall(payload) {
  return (await (await fetch("/api/projects", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)})).json());
}
function modalIsOpen() {
  return document.getElementById("fmodal").style.display === "flex";
}
function openModal(html, form) {
  boardForm = form;
  document.getElementById("fbox").innerHTML = html;
  document.getElementById("fmodal").style.display = "flex";
}
function closeModal() {
  boardForm = null;
  document.getElementById("fmodal").style.display = "none";
  document.getElementById("fbox").innerHTML = "";
}
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && modalIsOpen()) closeModal();
});
function formError(msg) {
  document.getElementById("tferr").textContent = msg || "";
}
function modalButtons(save, label) {
  return `<div class="err" id="tferr"></div>
    <div class="acts"><button class="act" onclick="${save}()">${label}</button>
      <button class="ghost" onclick="closeModal()">Cancel</button></div>`;
}
async function loadFormChoices() {
  const [pr, rr] = await Promise.all([projectCall({action: "list"}), taskCall({action: "roles"})]);
  formProjects = (pr.projects || []).filter(p => !p.archived);
  formRoles = rr.roles || [];
  goalBounds = {min: rr.goal_min || 0, max: rr.goal_max || 0};
  return formRoles.map(x => x.name);
}
const titleCase = s => s ? s[0].toUpperCase() + s.slice(1) : s;
// The two roles as described choices. Names and descriptions both come from
// the roles route (roles.FILEABLE), so the page holds no copy of the list.
function roleChoices(current) {
  const extra = current && !formRoles.find(x => x.name === current)
    ? [{name: current, hint: "internal"}] : [];
  return formRoles.concat(extra).map(x =>
    `<label class="choice"><input type="radio" name="tfrole" value="${esc(x.name)}"${
      x.name === current ? " checked" : ""} onchange="taskFormWarn()">
      <span><b>${esc(titleCase(x.name))}</b>: ${esc(x.hint || "")}</span></label>`).join("");
}
function pickedRole() {
  const box = [...document.querySelectorAll('input[name="tfrole"]')].find(x => x.checked);
  return box ? box.value : (boardForm && boardForm.role) || "";
}
function pickRole(name) {
  if (boardForm) boardForm.role = name;
  for (const x of document.querySelectorAll('input[name="tfrole"]')) x.checked = x.value === name;
}
function projectTitle(slug) {
  const p = formProjects.find(x => x.slug === slug);
  return p ? p.title : slug;
}
// The goal's length, said as it is typed: the same bounds as the route.
function goalCheck() {
  const n = document.getElementById("tfgoal").value.trim().length;
  const why = !goalBounds.min ? ""
    : n < goalBounds.min ? `${goalBounds.min - n} more character${goalBounds.min - n === 1 ? "" : "s"} needed `
      + `— a task needs at least ${goalBounds.min}; whoever picks it up has only this to go on`
    : goalBounds.max && n > goalBounds.max ? `${n - goalBounds.max} characters over the ${goalBounds.max} limit — split it`
    : "";
  document.getElementById("tfgoalwhy").textContent = why;
  return why;
}
function taskFormHtml(t, fixedProject) {
  const projField = fixedProject
    ? `<label>project</label><div class="fixed">${esc(projectTitle(t.project))}</div>`
    : `<label>project</label><select id="tfproj" onchange="taskFormWarn()"><option value="">(no project)</option>${
        formProjects.map(p => `<option value="${esc(p.slug)}"${p.slug === (t.project || "") ? " selected" : ""}>${
          esc(p.title)}</option>`).join("")}</select>`;
  return `<div class="tform">
    <h3>${t.id ? `Edit ${esc(t.title || t.id)}` : `New task in ${esc(projectTitle(t.project))}`}</h3>
    ${t.id ? `<div class="sub">${esc(t.id)} · ${esc(t.state)} — editable until it runs; the change is recorded on the task.</div>` : ""}
    ${projField}
    <label>title <span class="note">(optional — the goal's first line otherwise)</span></label>
    <input type="text" id="tftitle" value="${esc(t.title || "")}">
    <label>What should it do?</label>
    <textarea id="tfgoal" oninput="goalCheck()" placeholder="whoever picks it up has only this to go on">${esc(t.goal || "")}</textarea>
    <div class="err" id="tfgoalwhy"></div>
    <label>role</label>${roleChoices(t.role)}
    <div class="warn" id="tfwarn"></div>
    ${t.id ? "" : `<label>when</label>
      <label class="choice"><input type="radio" name="tfstate" id="tfqueue" checked> <span><b>Queue now</b>: the runner picks it up in turn</span></label>
      <label class="choice"><input type="radio" name="tfstate" id="tfpropose"> <span><b>Propose for later</b>: waits on the board until you accept it</span></label>`}
    ${modalButtons("submitTaskForm", t.id ? "Save" : "Create task")}</div>`;
}
// Said before filing, from the rule the runner applies (projects.unready via
// /projects list), so work for a project that cannot take unsupervised work
// does not look queued and then turn up in Needs you.
function taskFormWarn() {
  const role = pickedRole();
  if (boardForm) boardForm.role = role;
  const sel = document.getElementById("tfproj");
  const slug = boardForm && boardForm.kind === "new" ? boardForm.project : (sel ? sel.value : "");
  const p = formProjects.find(x => x.slug === slug);
  const held = (formRoles.find(x => x.name === role) || {}).held_if_unready;
  const why = !held ? ""
    : p ? (p.unready || "") : "is not a registered project and cannot take unsupervised work";
  document.getElementById("tfwarn").textContent = why
    ? `⚠ ${slug || "a task with no project"} ${why}. The runner would hold this task in Needs you `
      + "instead of running it — propose it, pick a role it does not hold, or set the project up first."
    : "";
}
async function newTaskForm() {
  // Only from a project's own board: the project decides where it runs.
  const slug = boardProject && boardProject !== UNFILED ? boardProject : "";
  if (!slug) { toast("Open a project's board to add a task to it"); return; }
  await loadFormChoices();
  const role = (formRoles.find(x => x.default) || formRoles[0] || {}).name || "";
  openModal(taskFormHtml({project: slug, role}, true), {kind: "new", project: slug, role});
  taskFormWarn();
}
async function editTaskForm(id) {
  const [, r] = await Promise.all([loadFormChoices(), taskCall({action: "task", id})]);
  if (!r.ok) { toast(r.error || "bot offline"); return; }
  const t = r.task;
  // A task under an archived or unregistered project still shows where it is.
  if (t.project && !formProjects.find(p => p.slug === t.project))
    formProjects.push({slug: t.project, title: t.project, unready: ""});
  openModal(taskFormHtml(t, false), {kind: "edit", id, was: t, role: t.role});
  taskFormWarn();
}
async function submitTaskForm() {
  const f = boardForm || {};
  const fields = {role: pickedRole(),
                  title: document.getElementById("tftitle").value.trim(),
                  goal: document.getElementById("tfgoal").value.trim()};
  if (f.kind === "new") fields.project = f.project;
  else fields.project = document.getElementById("tfproj").value;
  if (goalCheck()) return;              // said inline already; the route would agree
  let r;
  if (f.kind === "edit") {
    // Only what changed, compared the way the route stores them, so an
    // unchanged field is not re-validated and stray spaces are no edit.
    const was = f.was || {};
    const norm = (k, v) => k === "title" ? (v || "").split(/\s+/).filter(Boolean).join(" ")
                                         : (v || "").trim();
    const payload = {action: "edit", id: f.id, by: "you"};
    for (const k of ["project", "role", "title", "goal"])
      if (norm(k, fields[k]) !== norm(k, was[k])) payload[k] = fields[k];
    r = await taskCall(payload);
  } else {
    r = await taskCall({action: "create", ...fields, by: "you",
      title: fields.title || undefined,
      state: document.getElementById("tfpropose").checked ? "proposed" : "queued"});
  }
  if (!r.ok) { formError(r.error || "refused"); return; }
  toast((f.kind === "edit" ? "Saved" : (r.task || {}).state === "proposed" ? "Proposed" : "Queued")
        + (r.held ? ` — ${r.held}` : ""));
  closeModal();
  if (boardIsOpen()) refreshBoard();
  refreshTaskBadge();
}
async function runNext(id, on) {
  const r = await taskCall({action: "run-next", id, on, by: "you"});
  toast(r.ok ? (on ? "Runs next" : "Back in its turn") : (r.error || "Not allowed"));
  if (boardIsOpen()) refreshBoard();
}

// --- a new project: directory, git, CLAUDE.md and registration in one step.
// The GitHub box is the only way a repository gets published from here.
// Mirrors projects.slugify, for the live location line only; the route makes
// the real slug.
function slugify(name) {
  const s = (name || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  return s.slice(0, 40) || "untitled";
}
function newProjectForm(pre) {
  pre = pre || {};
  const existing = !!pre.path;
  openModal(`<div class="tform"><h3>New project</h3>
    <label>name</label><input type="text" id="npname" value="${esc(pre.name || "")}" oninput="projectFormWhere()">
    <label>What is it for? <span class="note">(one line; it goes into the project's CLAUDE.md)</span></label>
    <input type="text" id="nppurpose" value="">
    <label>location</label><div class="fixed" id="npwhere"></div>
    <label class="choice"><input type="checkbox" id="npexisting"${existing ? " checked" : ""} onchange="projectFormWhere()">
      <span>Use an existing folder instead</span></label>
    <div id="nppathrow" style="display:${existing ? "block" : "none"}"><input type="text" id="nppath" value="${esc(pre.path || "")}" placeholder="~/workspace/… or a full path"></div>
    <label class="choice"><input type="checkbox" id="npgithub">
      <span><b>Create a private GitHub repo</b>: makes a private repository and pushes this folder to it</span></label>
    ${modalButtons("submitProjectForm", "Create project")}</div>`, {kind: "project"});
  projectFormWhere();
}
function projectFormWhere() {
  const existing = document.getElementById("npexisting").checked;
  document.getElementById("nppathrow").style.display = existing ? "block" : "none";
  document.getElementById("npwhere").textContent = existing
    ? "the folder below, adopted as it is"
    : `~/workspace/${slugify(document.getElementById("npname").value)}`;
}
async function submitProjectForm() {
  const name = document.getElementById("npname").value.trim();
  if (!name) { formError("a project needs a name"); return; }
  const existing = document.getElementById("npexisting").checked;
  const path = existing ? document.getElementById("nppath").value.trim() : "";
  if (existing && !path) { formError("say which folder to use, or untick the box"); return; }
  const r = await projectCall({action: "new", name,
    purpose: document.getElementById("nppurpose").value.trim(),
    path, adopt: existing, github: document.getElementById("npgithub").checked});
  if (!r.ok) { formError(r.error || "Could not create it"); return; }
  const done = r.created || {};
  closeModal();
  toast(`${done.title || name} registered at ${done.cwd || "?"}`
        + (done.github_url ? ` · ${done.github_url}` : "")
        + ((done.notes || []).length ? ` · ${done.notes.join("; ")}` : ""));
  renderUnregistered();
  if (document.getElementById("taskmodal").style.display === "flex") toggleTasks();
  if (!boardIsOpen()) toggleBoard();
  setBoardProject(done.slug || slugify(name));
}

// --- project settings
async function projectSettings(slug) {
  const r = await projectCall({action: "get", slug});
  if (!r.ok) { toast(r.error || "bot offline"); return; }
  const p = r.project, sc = p.scope || {};
  const save = fn => `<button class="ghost" onclick="${fn}('${esc(slug)}')">Save</button></div>`;
  openModal(`<div class="tform" data-slug="${esc(slug)}"><h3>⚙ ${esc(p.title)} <span class="sub">${esc(slug)}</span></h3>
    <div class="sub">${esc(sc.cwd || "no directory")}${r.repo ? "" : " · not a git repository"}</div>
    ${r.unready ? `<div class="warn">⚠ ${esc(slug)} ${esc(r.unready)}</div>` : `<div class="note">ready: queued work and nightly review run unattended</div>`}
    <label>name</label><div class="line"><input type="text" id="pstitle" value="${esc(p.title || "")}">${save("saveProjectTitle")}
    <label>test command</label><div class="line"><input type="text" id="pstest" value="${esc(p.test_cmd || "")}" placeholder="e.g. ./bin/test">${save("saveTestCmd")}
    <div class="note">How work here is proven. Readiness needs it: without one nothing is verified, so nothing auto-merges and no unattended work runs.</div>
    <label>landing</label>
    <div class="line"><label style="margin:0"><input type="checkbox" id="psauto"${p.auto_merge ? " checked" : ""}
      onchange="setAutoMerge('${esc(slug)}',this.checked)"> auto-merge</label><span class="note">land reviewed, verified work without asking</span></div>
    <div class="line"><label style="margin:0"><input type="checkbox" id="pspublish"${p.publish ? " checked" : ""}
      onchange="setPublish('${esc(slug)}',this.checked)"> publish</label><span class="note">push the base to origin after each landing</span></div>
    <label>nightly review</label><div class="line"><input type="text" id="psideate" value="${esc(p.ideate_at || "")}" placeholder="off — or 02:00, 2am">${save("saveIdeate")}
    <label>base branch</label><div class="line"><input type="text" id="psbase" value="${esc(r.base || "")}" placeholder="the default branch">${save("saveBase")}
    <div class="note">What its tasks build on and land onto. Blank is the repository's default branch.</div>
    <label>generated paths</label><div class="line"><input type="text" id="psgenerated" value="${esc((p.generated || []).join(", "))}" placeholder="none — e.g. ios/App/Info.plist">${save("saveGenerated")}
    <div class="note">Files the build rewrites. Never counted as uncommitted work: a task checkout holding only these is released, and they never make the base checkout dirty.</div>
    <div class="err" id="tferr"></div>
    <div class="acts">${p.archived
      ? `<button class="ghost" onclick="archiveProject('${esc(slug)}',false)">Unarchive</button>`
      : `<button class="ghost" onclick="archiveProject('${esc(slug)}',true)">Archive…</button>`}
      <button class="ghost" onclick="closeModal()">Close</button></div></div>`,
    {kind: "settings", slug});
}
// One settings write: the route's answer is shown, and the panel and board
// redrawn from what was stored rather than from what was typed.
async function settingsCall(payload, done) {
  const r = await projectCall(payload);
  if (!r.ok) {
    // Redrawn from what is stored (a refused toggle unticks itself), and the
    // reason written after that, or the redraw would wipe it.
    if (boardForm && boardForm.slug === payload.slug) await projectSettings(payload.slug);
    formError(r.error || "refused");
    return r;
  }
  toast(done);
  if (boardIsOpen()) renderBoard();
  if (boardForm && boardForm.kind === "settings") await projectSettings(payload.slug);
  return r;
}
async function saveProjectTitle(slug) {
  await settingsCall({action: "title", slug, title: document.getElementById("pstitle").value}, "Renamed");
}
async function saveTestCmd(slug) {
  const cmd = document.getElementById("pstest").value.trim();
  if (!cmd && !confirm(`Clear ${slug}'s test command?\n\nIts work is then never verified, so nothing `
                       + "auto-merges and it stops taking unattended work.")) return;
  await settingsCall({action: "test-cmd", slug, cmd: cmd || "off"}, cmd ? "Test command saved" : "Test command cleared");
}
async function saveGenerated(slug) {
  const paths = document.getElementById("psgenerated").value.trim();
  await settingsCall({action: "generated", slug, paths: paths || "off"},
                     paths ? "Generated paths saved" : "Generated paths cleared");
}
async function setAutoMerge(slug, on) {
  if (on && !confirm(`Turn auto-merge on for ${slug}?\n\nReviewed work that passes its tests then lands `
                     + "on the base branch with nobody approving it, and queued work there runs unattended.")) {
    document.getElementById("psauto").checked = false; return;
  }
  await settingsCall({action: "auto-merge", slug, on}, on ? "Auto-merge on" : "Auto-merge off");
}
async function setPublish(slug, on) {
  if (on && !confirm(`Turn publishing on for ${slug}?\n\nEvery landing is then pushed to origin — `
                     + "an outward change to the shared remote that is not quietly undone.")) {
    document.getElementById("pspublish").checked = false; return;
  }
  await settingsCall({action: "publish", slug, on}, on ? "Publishing on" : "Publishing off");
}
async function saveIdeate(slug) {
  const at = document.getElementById("psideate").value.trim();
  const off = !at || ["off", "none", "clear"].includes(at.toLowerCase());
  if (!off && !confirm(`Review ${slug} every night at ${at}?\n\nAn agent reads the project unattended `
                       + "and files proposals for you to accept.")) return;
  await settingsCall({action: "ideate", slug, at: off ? "off" : at}, off ? "Nightly review off" : "Nightly review set");
}
async function saveBase(slug) {
  const branch = document.getElementById("psbase").value.trim();
  // Asked first without writing, so the confirmation can carry the warning.
  const c = await projectCall({action: "base", slug, branch, check: true});
  if (!c.ok) { formError(c.error || "refused"); return; }
  if (!confirm((branch ? `Build ${slug}'s tasks on ${branch}?` : `Build ${slug}'s tasks on the default branch?`)
               + "\n\nNew tasks are cut from it and land onto it."
               + (c.warning ? `\n\n⚠ ${c.warning}` : ""))) return;
  await settingsCall({action: "base", slug, branch, force: !!c.warning},
                     branch ? `Base: ${branch}` : "Base: the default branch");
}
async function archiveProject(slug, on) {
  if (on && !confirm(`Archive ${slug}?\n\nIt is hidden from the overview and from project pickers. `
                     + "Nothing is deleted: its tasks keep their label and its settings stay as they are, "
                     + "and it can be brought back from Archived."))
    return;
  const r = await projectCall({action: on ? "archive" : "unarchive", slug});
  if (!r.ok) { if (modalIsOpen()) formError(r.error || "refused"); else toast(r.error || "refused"); return; }
  toast(on ? `${slug} archived` : `${slug} is back`);
  if (on && boardProject === slug) { closeModal(); setBoardProject(null); return; }
  if (boardForm && boardForm.kind === "settings") await projectSettings(slug);
  if (boardIsOpen()) renderBoard();
}

// --- phone ---
// Under 700px wide the session list and the open thread take turns on the
// screen: body.reading shows the thread, and ‹ Sessions goes back. Opening one
// pushes a history entry, so the system back gesture does the same.
function phone() {
  return typeof matchMedia === "function" && matchMedia("(max-width: 700px)").matches;
}
function showReading() {
  const body = document.body;
  if (!body || body.classList.contains("reading")) return;
  body.classList.add("reading");
  if (phone() && window.history) history.pushState({reading: true}, "");
}
function leaveReading() {
  if (document.body) { document.body.classList.remove("reading"); document.body.classList.remove("threadinfo"); }
  showJump(false); closeAlerts();
  active = null;                 // stops the transcript poll; the list unhighlights
  loadList();
}
function closeSession() {
  if (window.history && history.state && history.state.reading) history.back();
  else leaveReading();
}
// A reload while a thread was open comes back on the list, but history still
// holds that thread's entry: one back gesture would then do nothing.
if (window.history && history.state && history.state.reading && history.replaceState)
  history.replaceState(null, "");
if (window.addEventListener)
  window.addEventListener("popstate", () => {
    if (document.body && document.body.classList.contains("reading")) leaveReading();
  });
// --- deep links ---
// Every view opens from the address: a bookmark, a notification, and
// bin/dashshots (which photographs each view in a simulator with no way to
// tap) all land on it the same way. Read once, then taken off the address
// bar, so a reload opens the list rather than this again.
//   ?thread=<key> | ?task=<id>            a thread, or a task in the task panel
//   ?view=stats | tasks[&tab=all] | projects | project&slug=<slug> | learnings
//   ?card=<task id>                       a task's detail, over the board
//   ?modal=new-thread | new-project | new-task | settings&slug= | release&slug= | alerts
//   ?q=<words>                            transcript search
async function openLink(q) {
  const view = q.get("view") || "", slug = q.get("slug") || "", modal = q.get("modal") || "";
  const thread = q.get("thread"), card = q.get("card"), words = q.get("q");
  // A thread or a task: the notification's own way in (openTarget, below).
  if (thread || q.get("task")) await openTarget("/?" + q.toString());
  if (view === "stats") {
    const d = document.getElementById("dashm");
    if (d) d.open = true;
  } else if (view === "tasks") {
    toggleTasks();
    if (q.get("tab") === "all") setTaskView("all");
  } else if (view === "projects" || view === "project" || card) {
    boardProject = view === "project" ? (slug === "all" ? "" : slug === "unfiled" ? UNFILED : slug) : null;
    if (!boardIsOpen()) toggleBoard(); else refreshBoard();
  } else if (view === "learnings") {
    toggleLearn();
  }
  if (card) openCard(card);
  if (modal === "new-thread") newThreadForm();
  else if (modal === "new-project") newProjectForm();
  else if (modal === "new-task") newTaskForm();
  else if (modal === "settings" && slug) projectSettings(slug);
  else if (modal === "release" && slug) releaseForm(slug);
  else if (modal === "alerts") openAlerts();
  if (words) {
    document.body.classList.add("searching");     // the phone's search row
    const box = document.getElementById("search");
    box.value = words;
    box.dispatchEvent(new Event("input"));
  }
}
// The service worker only adds an offline page (it caches nothing); without
// one -- plain http, an old browser -- the dashboard works the same.
if (typeof navigator !== "undefined" && navigator.serviceWorker)
  navigator.serviceWorker.register("/sw.js", {scope: "/"}).catch(() => {});

// --- notifications ---
// Web push: the bot (push.py) notifies this device when a task needs you or a
// thread has a reply. iPhone allows it only in the installed home-screen app
// (iOS 16.4+), and only when asked from a tap.
function standalone() {
  return (typeof matchMedia === "function" && matchMedia("(display-mode: standalone)").matches)
    || (typeof navigator !== "undefined" && navigator.standalone === true);
}
function iosDevice() {
  const n = typeof navigator !== "undefined" ? navigator : {};
  return /iPhone|iPad|iPod/.test(n.userAgent || "")
    || (n.platform === "MacIntel" && n.maxTouchPoints > 1);     // iPadOS reads as a Mac
}
// What this device can do, before asking it anything.
function pushSupport() {
  if (iosDevice() && !standalone()) return "install";
  const has = typeof navigator !== "undefined" && !!navigator.serviceWorker
    && typeof window !== "undefined" && "PushManager" in window
    && typeof Notification !== "undefined";
  if (!has) return "unsupported";
  if (Notification.permission === "denied") return "denied";
  return Notification.permission === "granted" ? "granted" : "ask";
}
const PUSH_HELP = {
  install: "Add to Home Screen first: tap Share, then Add to Home Screen, and open "
         + "Silkworm from there. iPhone only allows notifications in the installed app.",
  unsupported: "This browser can't receive notifications.",
  denied: "Notifications are off for Silkworm. Turn them on in Settings › Notifications "
        + "› Silkworm, then tap 🔔 again.",
};
function b64uBytes(text) {
  const b = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((text.length + 3) % 4));
  return Uint8Array.from(b, c => c.charCodeAt(0));
}
function b64uOf(buf) {
  return btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
async function pushCall(payload) {
  try {
    return await (await fetch("/api/push", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)})).json();
  } catch (e) { return {ok: false, error: "could not reach the dashboard"}; }
}
// Subscribe (or refresh the subscription) and hand it to the bot. A
// subscription made with another key is useless and is replaced.
async function subscribePush() {
  const reg = await navigator.serviceWorker.ready;
  const k = await pushCall({action: "key"});
  if (!k.ok) throw new Error(k.error || "the bot has no push key");
  let sub = await reg.pushManager.getSubscription();
  const held = sub && sub.options && sub.options.applicationServerKey;
  if (sub && held && b64uOf(held) !== k.key) { await sub.unsubscribe(); sub = null; }
  if (!sub) sub = await reg.pushManager.subscribe({userVisibleOnly: true,
                                                  applicationServerKey: b64uBytes(k.key)});
  const r = await pushCall({action: "subscribe", subscription: sub.toJSON(),
                            agent: navigator.userAgent || ""});
  if (!r.ok) throw new Error(r.error || "refused");
  return r;
}
async function enableNotifications() {
  const support = pushSupport();
  if (PUSH_HELP[support] && support !== "denied") {
    toast(PUSH_HELP[support], 8000); renderNotify(); return;
  }
  // Straight from the tap, before any other await: iOS refuses a permission
  // request that is not.
  let perm = Notification.permission;
  if (perm !== "granted") perm = await Notification.requestPermission();
  if (perm !== "granted") { toast(PUSH_HELP.denied, 8000); renderNotify(); return; }
  try {
    await subscribePush();
    toast("Notifications are on for this device");
  } catch (e) {
    toast("Could not turn notifications on: " + (e && e.message || e), 8000);
  }
  renderNotify();
}
function renderNotify() {
  const btn = document.getElementById("notifybtn"), lbl = document.getElementById("notifylbl");
  if (!btn) return;
  const support = pushSupport();
  const words = {install: "Add to Home Screen first to get notifications",
                 unsupported: "This browser can't receive notifications",
                 denied: "Notifications are off in Settings", granted: "Notifications are on",
                 ask: "Enable notifications"}[support];
  btn.title = words;
  btn.className = "ghost hbtn" + (support === "granted" ? " on" : "");
  if (lbl) lbl.textContent = support === "granted" ? " On" : " Notify";
}
// Already allowed: keep the bot's copy of this device's subscription current
// (a push service can rotate it). Quietly; nothing here is asked of anyone.
if (pushSupport() === "granted") subscribePush().catch(() => {});
renderNotify();

// --- opening a thread or task from a notification ---
// The app opens on "/?thread=<key>" or "/?task=<id>" (a cold start), or the
// service worker posts {type: "open", url} to the page already open.
async function openTarget(href) {
  let q;
  try { q = new URL(href, location.href).searchParams; } catch (e) { return; }
  const thread = q.get("thread"), task = q.get("task");
  if (thread) {
    if (!lastSessions.length) { try { await loadList(); } catch (e) { /* drawn below anyway */ } }
    active = lastSessions.find(x => x.key === thread) ||
      {key: thread, title: "", kind: "thread", project: "", session_id: "", summary: "",
       events: [], turns: 0, cost: 0, updated: Date.now() / 1000, files: 0,
       resume_cmd: "", slack_link: "", running: false};
    loadList(); loadTranscript(true);
  } else if (task) {
    await openTask(task);
  }
}
async function openTask(id) {
  const m = document.getElementById("taskmodal");
  // Whatever project the list was narrowed to, the task must be in it.
  const sel = document.getElementById("tproj");
  if (sel) sel.value = "";
  taskView = "attention";
  document.getElementById("tabneed").className = "on";
  document.getElementById("taball").className = "";
  if (m.style.display !== "flex") {
    m.style.display = "flex";
    renderUnregistered();
    try { await renderProjects(); } catch (e) { /* the list below still draws */ }
  }
  try { await renderTasks(); } catch (e) { /* looked for below all the same */ }
  for (let i = 0; i < 50; i++) {             // a list drawn meanwhile, if not this one
    const row = document.querySelector(`[data-task="${CSS.escape(id)}"]`);
    if (row) {
      const more = row.querySelector("details");
      if (more) more.open = true;
      row.classList.add("flash");
      if (row.scrollIntoView) row.scrollIntoView({block: "center"});
      return true;
    }
    await new Promise(ok => setTimeout(ok, 100));
  }
  toast("That task no longer needs you");
  return false;
}
if (typeof navigator !== "undefined" && navigator.serviceWorker && navigator.serviceWorker.addEventListener)
  navigator.serviceWorker.addEventListener("message", e => {
    if (e.data && e.data.type === "open") openTarget(e.data.url);
  });
if (typeof location !== "undefined"
    && /[?&](thread|task|view|card|modal|q)=/.test(location.search || "")) {
  const target = new URLSearchParams(location.search);
  // Off the address bar, so a reload opens the list rather than this again.
  if (window.history && history.replaceState) history.replaceState(null, "", "/");
  openLink(target);
}

// --- polling ---
// Each poll waits for its last one, so a slow tunnel queues nothing; a hidden
// tab skips them all, and showing it again refreshes at once.
function poller(fn) {
  let busy = false;
  return async () => {
    if (busy || !visible()) return;
    busy = true;
    try { await fn(); } catch (e) { /* the next tick tries again */ } finally { busy = false; }
  };
}
const pollList = poller(loadList), pollStats = poller(loadStats),
      pollTranscript = poller(() => active ? loadTranscript(false) : null),
      pollLive = poller(() => threadBusy() ? loadTranscript(false) : null),
      pollBoard = poller(() => boardIsOpen() ? refreshBoard() : null);
loadList(); loadStats();
setInterval(pollList, 5000);
setInterval(pollStats, 30000);
setInterval(pollTranscript, 4000);
setInterval(pollLive, 1500);
setInterval(pollBoard, 5000);
document.addEventListener("visibilitychange", () => {
  if (visible()) { pollList(); pollStats(); pollTranscript(); pollBoard(); }
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    if not LOOPBACK and not TOKEN:
        # Failing closed: an unauthenticated bind beyond loopback would hand
        # arbitrary command execution to anything that can reach the port.
        raise SystemExit(
            f"VIZ_BIND={BIND} exposes the dashboard beyond loopback, which needs\n"
            "authentication. Set VIZ_TOKEN to a secret, e.g.\n\n"
            "    VIZ_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')\n\n"
            "then open http://<host>:%d/?token=$VIZ_TOKEN once." % PORT)
    server = make_server(BIND, PORT)
    # An IPv6 bind answers only on that address (::1 refuses 127.0.0.1).
    where = f"[{BIND}]" if ":" in BIND else ("127.0.0.1" if LOOPBACK else BIND)
    print(f"Silkworm visualizer: http://{where}:{PORT} (bot bridge on :{BOT_PORT})"
          + ("" if LOOPBACK else "  [token required]"))
    server.serve_forever()

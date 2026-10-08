#!/usr/bin/env python3
"""Silkworm session visualizer — local web dashboard for thread sessions.

    python3 visualizer.py            # http://127.0.0.1:8790
    SILKWORM_VIZ_PORT=9000 python3 visualizer.py

Reads sessions.json and the Claude Code transcripts in ~/.claude/projects/.
Talks to the running bot (localhost) for live thread status and to relay
messages/commands into threads. Stdlib only; binds 127.0.0.1 only.
"""

import json
import os
import socket
import socketserver
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

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
SESSIONS_FILE = BASE_DIR / "sessions.json"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
PORT = int(os.environ.get("SILKWORM_VIZ_PORT", "8790"))
# Loopback by default. The dashboard is not read-only -- it relays prompts into
# threads, and the bot trusts any localhost caller as the machine owner -- so
# binding it anywhere else requires a token (enforced at startup, below).
BIND = os.environ.get("VIZ_BIND", "127.0.0.1").strip()
TOKEN = os.environ.get("VIZ_TOKEN", "").strip()
LOOPBACK = BIND in ("127.0.0.1", "::1", "localhost")
# Past twice the per-turn timeout, a turn is not slow -- it is stuck.
STALL_AFTER_S = max(int(os.environ.get("CLAUDE_TIMEOUT", "900")) * 2, 3600)


def _bot_port() -> str:
    return envfile.value(BASE_DIR / ".env", "APPROVAL_PORT") or "8787"


BOT_PORT = _bot_port()


# --- bot bridge ---------------------------------------------------------------

def bot_call(path: str, payload: dict, timeout: float = 1.0) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{BOT_PORT}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return {}


# --- data loading ---------------------------------------------------------------

def raw_sessions() -> dict[str, dict]:
    # repair=False: the bot owns this file and is the one writing it. Falling
    # back to the backup in memory is fine for drawing a page; setting the
    # primary aside and rewriting it from a second process is not.
    raw = jsonstore.load(SESSIONS_FILE, default={}, repair=False) or {}
    return {k: ({"session_id": v} if isinstance(v, str) else v) for k, v in raw.items()}


def turn_health(entry: dict, live: set) -> dict | None:
    """How long this thread's in-flight turn has been going, and whether the
    child is actually there.

    A turn whose child has died, or that has run far past any plausible
    duration, is stalled -- it will never finish on its own. Surfacing that is
    the difference between noticing in a minute and noticing in a day.
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
    return {
        "age_s": int(age),
        "child_alive": alive,
        # No child means it can never finish; past the bound it never will.
        "stalled": (not alive) or age > STALL_AFTER_S,
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
    live = procs.alive_sessions(
        [(e.get("pending") or {}).get("session_id") or e.get("session_id")
         for e in entries.values()])
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


def find_transcript(session_id: str) -> Path | None:
    if not session_id or not PROJECTS_DIR.exists():
        return None
    for p in PROJECTS_DIR.glob(f"*/{session_id}.jsonl"):
        return p
    return None


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


def load_transcript(session_id: str) -> dict:
    path = find_transcript(session_id)
    if not path:
        return {"error": "no transcript found for this session"}
    messages = []
    for d in iter_entries(path):
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
    return {"session_id": session_id, "path": str(path), "messages": messages}


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
        for d in iter_entries(path):
            msg = d.get("message") or {}
            usage = msg.get("usage")
            if d.get("type") != "assistant" or not isinstance(usage, dict):
                continue
            ts = d.get("timestamp", "")
            cache = usage.get("cache_read_input_tokens", 0) or 0
            fresh = (usage.get("input_tokens", 0) or 0) + (usage.get("cache_creation_input_tokens", 0) or 0)
            out = usage.get("output_tokens", 0) or 0
            tot_cache += cache
            tot_in += fresh
            model = msg.get("model") or "unknown"
            if not model.startswith("<"):
                models[model] = models.get(model, 0) + out
            if ts:
                try:
                    t = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
                    day = time.strftime("%Y-%m-%d", time.localtime(t))
                except ValueError:
                    continue
                if day in daily:
                    daily[day]["cache_read"] += cache
                    daily[day]["fresh_in"] += fresh
                    daily[day]["out"] += out
    denom = tot_cache + tot_in
    return {
        "total_cost": round(sum(e.get("cost", 0.0) for e in sessions.values()), 2),
        "threads": len(sessions),
        "cache_rate": round(100 * tot_cache / denom, 1) if denom else None,
        "days": [{"date": d, **daily[d]} for d in day_keys],
        "models": sorted(models.items(), key=lambda kv: -kv[1]),
    }


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
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data) -> None:
        self._send(200, json.dumps(data).encode(), "application/json")

    def _authed(self, url) -> bool:
        """Loopback needs no token; anything else must present one.

        Accepts ?token= once and hands back a cookie, so a bookmarked URL
        works without the secret sitting in the address bar afterwards.
        """
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
        elif url.path == "/api/sessions":
            self._json(load_sessions())
        elif url.path == "/api/session":
            self._json(load_transcript(qs.get("id", [""])[0]))
        elif url.path == "/api/stats":
            self._json(load_stats())
        elif url.path == "/api/search":
            self._json(search(qs.get("q", [""])[0]))
        elif url.path == "/api/artifacts":
            self._json(artifacts(qs.get("key", [""])[0]))
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
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            payload = {}
        if url.path == "/api/send":
            answer = bot_call("/web-message", payload, timeout=3)
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
            self._json(answer or {"ok": False, "error": "bot is offline"})
        elif url.path == "/api/titles":
            answer = bot_call("/titles", payload, timeout=180)
            self._json(answer or {"ok": False, "error": "bot is offline"})
        elif url.path == "/api/hide":
            answer = bot_call("/hide", payload, timeout=10)
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
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Silkworm — Sessions</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
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
  #chart { display: flex; align-items: flex-end; gap: 6px; height: 120px;
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
  #dashtable { width: 100%; border-collapse: collapse; font-size: 12px;
               font-family: var(--mono); margin-top: 8px; }
  #dashtable td, #dashtable th { padding: 3px 10px 3px 0; text-align: right;
               border-bottom: 1px solid var(--line2); font-variant-numeric: tabular-nums; }
  #dashtable th { color: var(--muted); font-weight: 500; }
  #dashtable td:first-child, #dashtable th:first-child { text-align: left; }
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
  #composer { flex: none; display: none; gap: 8px; padding-top: 12px;
              border-top: 1px solid var(--line); margin-top: 8px; }
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
  .pcards { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 12px; }
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
  <h1>Silkworm sessions</h1>
  <span id="botdot" title="bot status"></span>
  <button class="ghost" style="margin-left:16px" onclick="toggleTasks()">📋 Tasks<span id="taskbadge"></span></button>
  <button class="ghost" onclick="toggleBoard()" title="per-project boards and backlogs">🗂 Projects</button>
  <button class="ghost" onclick="toggleLearn()">🧠 Learnings</button>
  <button class="ghost" onclick="nameAllThreads()" title="name every untitled thread">✎ Name untitled</button>
  <input id="search" placeholder="Search transcripts…" autocomplete="off">
</header>
<div id="alertbar"></div>
<div id="searchresults"></div>
<div class="layout">
  <div class="listcol">
    <div id="kindfilter"></div>
    <nav id="list"></nav>
  </div>
  <main>
    <div id="content">
      <div id="dash"></div>
      <div id="transcript"><div class="empty">Pick a session on the left.</div></div>
    </div>
    <div id="composer">
      <textarea id="reply" placeholder="Message this thread (also posts to Slack) — !commands work too"></textarea>
      <button class="act" onclick="sendReply()">Send</button>
    </div>
  </main>
</div>
<div id="tip"></div>
<div id="toast"></div>
<div id="taskmodal" onclick="if(event.target.id==='taskmodal')toggleTasks()">
  <div class="box">
    <h2 style="display:flex;align-items:center;gap:12px">📋 Tasks
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
    <h2 style="display:flex;align-items:center;gap:12px">🧠 Learnings
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

function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.style.display = "block";
  setTimeout(() => t.style.display = "none", 2500);
}

// --- dashboard ---
async function loadStats() {
  statsCache = await (await fetch("/api/stats")).json();
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
    body = `<table id="dashtable"><tr><th>date</th><th>cache read</th><th>fresh in</th><th>output</th></tr>` +
      s.days.map(d => `<tr><td>${d.date.slice(5)}</td><td>${fmtTok(d.cache_read)}</td><td>${fmtTok(d.fresh_in)}</td><td>${fmtTok(d.out)}</td></tr>`).join("") + "</table>";
  } else {
    const max = Math.max(1, ...s.days.map(d => d.cache_read + d.fresh_in + d.out));
    body = `<div id="chart">` + s.days.map(d => {
      const segs = [["cache_read","var(--c-cache)"],["fresh_in","var(--c-fresh)"],["out","var(--c-out)"]]
        .map(([k, c]) => `<div class="seg" style="background:${c};height:${100*d[k]/max}%"></div>`).join("");
      return `<div class="col" data-tip="${d.date} — cache ${fmtTok(d.cache_read)} · fresh ${fmtTok(d.fresh_in)} · out ${fmtTok(d.out)}">${segs}</div>`;
    }).join("") + `</div><div class="xlabels">` +
      s.days.map((d, i) => `<span>${i % 2 ? "" : d.date.slice(5)}</span>`).join("") + "</div>";
  }
  const models = s.models.length
    ? `<div class="modelsplit">output by model: ` +
      s.models.map(([m, t]) => `${esc(m)} ${fmtTok(t)}`).join(" · ") + "</div>" : "";
  document.getElementById("dash").innerHTML = tiles + legend + body + models;
  document.querySelectorAll("#chart .col").forEach(col => {
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
  loadList();
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
  bar.innerHTML = kindBtns +
    (nHidden ? `<button class="${showHidden ? "on" : ""}" onclick="toggleHidden()"
                  title="hidden threads are kept, not deleted"
                >hidden <b>${nHidden}</b></button>` : "") +
    (oldRuns ? `<button onclick="hideOldTaskRuns()"
                  title="hide task runs untouched for 14 days"
                >tidy <b>${oldRuns}</b></button>` : "");
}

async function loadList() {
  const data = await (await fetch("/api/sessions")).json();
  document.getElementById("botdot").className = data.bot_online ? "on" : "";
  document.getElementById("botdot").title = data.bot_online ? "bot online" : "bot offline";
  renderAlerts(data.sessions, data.slack, data.revision);
  refreshTaskBadge();
  lastSessions = data.sessions;
  renderKindFilter(data.sessions);
  const nav = document.getElementById("list");
  nav.innerHTML = "";
  for (const s of data.sessions.filter(matchesKind)) {
    const div = document.createElement("div");
    div.className = "card" + (active && active.key === s.key ? " active" : "");
    const t = s.turn;
    const badges =
      (t && t.stalled ? `<span class="badge stall" title="${t.child_alive
            ? "running far past the timeout" : "no claude process — it cannot finish"}"
         >stalled ${dur(t.age_s)}</span>`
       : t ? `<span class="badge run">running ${dur(t.age_s)}</span>`
       : s.running ? '<span class="badge run">running</span>' : "") +
      (s.checked_out ? `<span class="badge term">${s.terminal_live ? "in terminal" : "checked out"}</span>` : "");
    const label = s.title ? `<span class="title">${esc(s.title)}</span>`
                          : `<span class="untitled">untitled</span>`;
    const kindTag = s.kind === "task" ? `<span class="badge kind">task run</span>` : "";
    const hideBtn = `<button class="hidebtn" title="${s.hidden ? "show again" : "hide"}"
        onclick="hideThread('${s.key}', ${s.hidden ? "false" : "true"}, event)"
      >${s.hidden ? "\u21ba" : "\u00d7"}</button>`;
    div.innerHTML = `<div class="key">${label}${kindTag}${badges}${hideBtn}
        ${s.cost_flag ? `<span class="badge cost" title="last turn $${s.cost_flag.last} vs median $${s.cost_flag.median}">${s.cost_flag.ratio}× cost</span>` : ""}</div>
      <div class="subkey">${esc(s.key)}</div>
      ${s.summary ? `<div class="summary">${esc(s.summary)}</div>` : ""}
      <div class="meta"><span><b>${s.turns}</b> turns</span>
      <span title="API list-price equivalent, not billed"><b>$${(s.cost||0).toFixed(2)}</b></span>
      <span>${esc(s.model || "default")}</span>
      <span>${age(s.updated)}</span>
      ${s.files ? `<span>📎 ${s.files}</span>` : ""}</div>`;
    div.onclick = () => { active = s; loadList(); loadTranscript(true); };
    nav.appendChild(div);
  }
  if (active) {
    const cur = data.sessions.find(x => x.key === active.key);
    if (cur) active = cur;
  }
}

// --- transcript ---
async function loadTranscript(scroll) {
  if (!active) return;
  const s = active;
  const [data, files] = await Promise.all([
    (await fetch("/api/session?id=" + encodeURIComponent(s.session_id))).json(),
    (await fetch("/api/artifacts?key=" + encodeURIComponent(s.key))).json(),
  ]);
  const el = document.getElementById("transcript");
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
  if (data.error) {
    h += `<div class="empty">${esc(data.error)}</div>`;
  } else {
    for (const m of data.messages) {
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
      let usage = "";
      if (m.usage) {
        const u = m.usage, denom = u.cache_read + u.in + u.cache_create;
        const rate = denom ? Math.round(100 * u.cache_read / denom) : null;
        usage = `<div class="usage">in ${fmtTok(u.in + u.cache_create)} · cache ${fmtTok(u.cache_read)}` +
          (rate == null ? "" : ` (${rate}%)`) + ` · out ${fmtTok(u.out)}` +
          (m.model ? ` · ${esc(m.model)}` : "") + `</div>`;
      }
      const when = m.ts ? new Date(m.ts).toLocaleTimeString() : "";
      h += `<div class="msg ${m.role}"><div class="who">${m.role} · ${when}</div>${inner}${usage}</div>`;
    }
  }
  if (files.length) {
    h += `<div class="artifacts"><b>Files</b>` + files.map(f =>
      `<div class="f"><span>${f.direction === "in" ? "⬇" : "⬆"}</span>
       ${f.exists ? `<a href="/download?path=${encodeURIComponent(f.path)}">${esc(f.name)}</a>` : esc(f.name) + (f.pruned ? " (pruned " + new Date(f.pruned * 1000).toLocaleDateString() + "; the copy is in Slack)" : " (gone)")}
       <span>${fmtTok(f.size)}B</span></div>`).join("") + "</div>";
  }
  el.innerHTML = h;
  document.getElementById("composer").style.display = "flex";
  if (scroll) el.scrollIntoView(false);
}

// --- composer / commands ---
async function sendText(text) {
  if (!active || !text.trim()) return;
  const r = await (await fetch("/api/send", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({key: active.key, text: text.trim()})})).json();
  toast(r.ok ? "Sent — reply lands here and in Slack" : "Failed: " + (r.error || "unknown"));
}
function cmdSend(cmd) { sendText(cmd); setTimeout(loadList, 800); }
function sendReply() {
  const box = document.getElementById("reply");
  sendText(box.value); box.value = "";
}
document.addEventListener("keydown", e => {
  if (e.target.id === "reply" && e.key === "Enter" && !e.shiftKey) {
    e.preventDefault(); sendReply();
  }
});

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
    box.style.display = "block";
    box.querySelectorAll(".hit[data-key]").forEach(hit => hit.onclick = async () => {
      box.style.display = "none";
      document.getElementById("search").value = "";
      const data = await (await fetch("/api/sessions")).json();
      active = data.sessions.find(s => s.key === hit.dataset.key) || null;
      loadList(); if (active) loadTranscript(true);
    });
  }, 250);
});
document.addEventListener("click", e => {
  if (!e.target.closest("#searchresults") && e.target.id !== "search")
    document.getElementById("searchresults").style.display = "none";
});

// --- learnings ---
async function learnCall(payload) {
  return (await (await fetch("/api/learnings", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)})).json());
}
function renderAlerts(sessions, slack, revision) {
  const bar = document.getElementById("alertbar");
  const stalled = sessions.filter(s => s.turn && s.turn.stalled);
  const pricey = sessions.filter(s => s.cost_flag);
  // Only trust a full sampling window; a bot that just started is not down.
  const down = slack && slack.ready && !slack.connected;
  // Landed is not running. Nothing restarts the bot when it merges onto its
  // own main, so the fix sits in the tree while the old process keeps serving.
  // "unknown" is not "fine" either — a bot too old to report the field gives
  // us nothing, which is the silence this is here to break.
  const stale = revision && revision.state && revision.state !== "current";
  if (!stalled.length && !pricey.length && !down && !stale) { bar.innerHTML = ""; return; }
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
  bar.innerHTML = parts.join(" ");
}
function jumpTo(key) {
  const card = [...document.querySelectorAll("#list .card")].find(
    c => c.querySelector(".subkey") && c.querySelector(".subkey").textContent === key);
  if (card) card.click();
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
async function taskAction(id, action, notes) {
  const r = await taskCall(notes ? {action, id, notes, by: "you"} : {action, id});
  // Cancelling something that was running says whether the work was actually
  // stopped, which is the only part of it you cannot see from the board.
  // Land only starts a landing; "Task landed" would claim a merge that has
  // not happened. Those two say what they did in their own words.
  // Release likewise only starts one; Done is not a verb to add -ed to.
  toast(r.ok ? ((action === "land" || action === "drop" || action === "release") && r.note
                 ? r.note
               : action === "resolve" ? "Marked done"
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
async function releaseStep(id) {
  // Runs only the !release the task recorded; the request names nothing else.
  if (!confirm("Run the release this task left for you?\n\n"
             + "It is the same as typing the !release shown on the card. The "
             + "result is posted to the task's thread, and the task is done "
             + "only if the release goes out.")) return;
  await taskAction(id, "release");
}
function lastEvent(t) {
  // Why a task stopped is already on the record -- transition() writes the
  // reason into events -- the row just never showed it. Prefer the event that
  // put it in the state it is in; fall back to whatever happened last.
  const ev = t.events || [];
  const e = ev.filter(x => x.kind === t.state).slice(-1)[0] || ev.slice(-1)[0];
  return (e && e.detail) || "";
}
function taskDetail(t) {
  // A title is the goal's first line cut at sixty characters. For a nightly
  // proposal that is the opening clause of two thousand words of evidence --
  // file names, line numbers, what done looks like -- and the row asks you to
  // spend a session on it or throw it away. The payload already carries the
  // whole thing, so show it rather than fetching it and dropping it.
  const goal = (t.goal || "").trim();
  const why = t.state === "failed" ? lastEvent(t) : "";
  // A short goal is its own title; there is nothing behind it to open.
  const body = goal && goal !== (t.title || "").trim()
    ? `<pre class="goal">${esc(goal)}</pre>` : "";
  if (!body && !why) return "";
  return (why ? `<div class="why">${esc(why)}</div>` : "") + body;
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
  const r = await taskCall({action: taskView === "attention" ? "attention" : "list",
                            project: project || undefined});
  if (!r.ok) { list.innerHTML = `<div class="hint">${esc(r.error || "bot offline")}</div>`; return; }
  updateTaskBadge(r.counts);
  const spend = document.getElementById("spend");
  if (spend) spend.textContent = r.spend_line ? `💰 ${r.spend_line}` : "";
  if (!r.tasks.length) {
    list.innerHTML = taskView === "attention"
      ? `<div class="hint">Nothing needs you. 🎉</div>`
      : `<div class="hint">No tasks yet — queue one above.</div>`;
    return;
  }
  list.innerHTML = r.tasks.map(t => {
    const more = taskDetail(t);
    const title = more
      ? `<details class="more"><summary>${esc(t.title)}</summary>${more}</details>`
      : esc(t.title);
    return `<div class="task">
      <span class="st st-${esc(t.state)}">${esc(t.state)}</span>
      <span class="tt">${title}
        <div class="sub">${t.project ? `<span class="proj">${esc(t.project)}</span> · ` : ""}${
          esc(t.id)} · ${esc(t.source)}${t.attempts > 1 ? ` · attempt ${t.attempts}` : ""} · ${
          age(t.created)}${costText(t)}${held(t)}</div>${yoursToDo(t)}${review(t)}${landing(t)}</span>
      ${taskButtons(t)}</div>`;
  }).join("");
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
  const r = await taskCall({action: "list"});
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
function setBoardProject(p) {
  boardProject = p;
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
  proj.innerHTML = `<option value="*">overview</option><option value="">all projects</option>`
    + boardRows.map(p => `<option value="${esc(p.slug)}">${esc(p.title)}</option>`).join("")
    + `<option value="${UNFILED}">unfiled</option>`;
  proj.value = keep;
  if (proj.value !== keep) {             // a slug the overview has not listed yet
    proj.insertAdjacentHTML("beforeend", `<option value="${esc(keep)}">${esc(keep)}</option>`);
    proj.value = keep;
  }
  document.getElementById("bvover").className = boardProject === null ? "on" : "";
  document.getElementById("bvall").className = boardProject === "" ? "on" : "";
  document.getElementById("bvunfiled").className = boardProject === UNFILED ? "on" : "";
}
function fillSelect(id, values, label) {
  const sel = document.getElementById(id);
  const keep = sel.value;
  sel.innerHTML = `<option value="">${label}</option>` + values.map(v =>
    `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  sel.value = values.includes(keep) ? keep : "";
}
async function renderBoard() {
  const role = document.getElementById("brole").value;
  const state = document.getElementById("bstate").value;
  const q = document.getElementById("bq").value.trim();
  // A filter typed on the overview means "find it", which is a board.
  if (boardProject === null && (role || state || q)) boardProject = "";
  if (boardProject === null) {
    document.getElementById("bboard").innerHTML = "";
    return loadOverview();
  }
  document.getElementById("bover").innerHTML = "";
  boardFilters();
  // A poll in flight when a filter changes must not land on top of the
  // filtered answer: only the newest request draws.
  const ask = ++boardAsk;
  const r = await taskCall({action: "board", project: boardProject, role, state, q});
  if (ask !== boardAsk) return;
  const el = document.getElementById("bboard");
  if (!r.ok) { el.innerHTML = `<div class="hint">${esc(r.error || "bot offline")}</div>`; return; }
  fillSelect("brole", r.roles || [], "any role");
  fillSelect("bstate", ["proposed", "queued", "running", "blocked", "awaiting_approval",
                        "needs_input", "failed", "done"], "any state");
  boardInfo = r.info || null;
  el.innerHTML = boardHead(boardInfo) + renderColumns(r)
    + (boardProject === UNFILED ? unfiledThreads() : "");
}
// The open project's name, whether it can take unsupervised work, and its
// settings. Only on a project's own board: "all" and "unfiled" have none.
function boardHead(info) {
  if (!info) return "";
  return `<div class="bhead"><h3>${esc(info.title)}</h3>${
    info.unready ? `<span class="warn" title="work the runner would take on its own is held in Needs you instead">⚠ ${
      esc(info.unready)}</span>` : ""}<button class="act" onclick="newTaskForm()">+ New task</button><button class="ghost" onclick="projectSettings('${
    esc(info.slug)}')">⚙ Settings</button></div>`;
}
function renderColumns(r) {
  return `<div class="bcols">` + BOARD_COLUMNS.map(([k, label]) => {
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
    ${t.why ? `<div class="why">${esc(t.why)}</div>` : ""}${yoursToDo(t)}${cardReview(t)}${cardLanding(t)}
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
  const r = await taskCall({action: "overview"});
  if (ask !== boardAsk || boardProject !== null) return;   // superseded meanwhile
  const el = document.getElementById("bover");
  if (!r.ok) { el.innerHTML = `<div class="hint">${esc(r.error || "bot offline")}</div>`; return; }
  boardRows = r.projects || [];
  boardFilters();
  el.innerHTML = renderOverview(r);
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
    ${releaseText(p.release)}
    ${threads !== undefined ? `<div class="row"><span class="lbl">threads</span>${threads} with no project</div>` : ""}
  </div>`;
}
function closeDetail() {
  boardDetailId = null;
  const el = document.getElementById("bdetail");
  el.style.display = "none";
  el.innerHTML = "";
}
async function openCard(id) {
  boardDetailId = id;
  const r = await taskCall({action: "task", id});
  if (boardDetailId !== id) return;      // another card was opened meanwhile
  const el = document.getElementById("bdetail");
  if (!r.ok) { el.innerHTML = `<div class="hint">${esc(r.error || "bot offline")}</div>`; el.style.display = "block"; return; }
  el.innerHTML = renderDetail(r.task);
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
    ${t.state === "failed" ? `<div class="sect">why it stopped</div><div class="rev">${esc(lastEvent(t))}</div>` : ""}
    ${(t.result || {}).review ? `<div class="sect">review</div>${review(t)}` : ""}
    ${reviews ? `<div class="sub">reviewed by ${reviews}</div>` : ""}
    ${l || (t.result || {}).landed ? `<div class="sect">landing</div>${landing(t)}${l ? `<div class="sub">${
      esc(l.stage || "")}${l.base ? ` · onto ${esc(l.base)}` : ""}${l.at ? ` · ${age(l.at)}` : ""}${
      l.detail && l.landed ? `<div>${esc(String(l.detail))}</div>` : ""}</div>` : ""}` : ""}
    <div class="sect">events · ${(t.events || []).length}</div>${events || `<div class="hint">none</div>`}`;
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

// --- polling ---
loadList(); loadStats();
setInterval(loadList, 5000);
setInterval(loadStats, 30000);
setInterval(() => { if (active) loadTranscript(false); }, 4000);
setInterval(() => { if (boardIsOpen()) refreshBoard(); }, 5000);
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

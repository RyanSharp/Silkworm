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
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import secrets

import procs

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
    port = "8787"
    try:
        for line in (BASE_DIR / ".env").read_text().splitlines():
            if line.strip().startswith("APPROVAL_PORT="):
                port = line.split("=", 1)[1].strip()
    except OSError:
        pass
    return port


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
    if not SESSIONS_FILE.exists():
        return {}
    raw = json.loads(SESSIONS_FILE.read_text())
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
            "slack_link": f"https://slack.com/archives/{channel}/p{thread_ts.replace('.', '')}",
            "resume_cmd": f"cd {cwd or '~'} && claude --resume {sid}",
            "has_transcript": find_transcript(sid) is not None,
        })
    out.sort(key=lambda s: -s["updated"])
    # bot_online only says the local server answered; it stayed true through a
    # seventeen-hour Slack outage. The link is a separate fact.
    return {"bot_online": bool(status.get("online")), "sessions": out,
            "slack": status.get("slack") or {}}


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
            answer = bot_call("/projects", payload, timeout=10)
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
  .task .rev { margin-top: 6px; font-size: 11.5px; line-height: 1.45; color: var(--ink);
               background: #D8517F14; border-left: 2px solid #D8517F;
               border-radius: 0 5px 5px 0; padding: 6px 9px; }
  .task .rev ul { margin: 4px 0 0; padding-left: 16px; }
  #tproj { background: var(--bg); color: var(--ink); border: 1px solid var(--line);
           border-radius: 8px; font: inherit; font-size: 12px; padding: 3px 8px; }
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
      Everything else is the system's business and stays out of the way.</div>
    <div class="lform">
      <input class="text" id="tgoal" placeholder="what should it do?"
             onkeydown="if(event.key==='Enter')addTask()">
      <input class="scope" id="tnewproj" placeholder="project (optional)">
      <input class="scope" id="tcwd" placeholder="working dir (blank = default)">
      <button class="act" onclick="addTask()">Queue it</button>
    </div>
    <div id="tlist"></div>
  </div>
</div>
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
      <div class="tile"><div class="n">$${s.total_cost.toFixed(2)}</div><div class="l">total spend</div></div>
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
  renderAlerts(data.sessions, data.slack);
  refreshTaskBadge();
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
      <span><b>$${(s.cost||0).toFixed(2)}</b></span>
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
       ${f.exists ? `<a href="/download?path=${encodeURIComponent(f.path)}">${esc(f.name)}</a>` : esc(f.name) + " (gone)"}
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
function renderAlerts(sessions, slack) {
  const bar = document.getElementById("alertbar");
  const stalled = sessions.filter(s => s.turn && s.turn.stalled);
  const pricey = sessions.filter(s => s.cost_flag);
  // Only trust a full sampling window; a bot that just started is not down.
  const down = slack && slack.ready && !slack.connected;
  if (!stalled.length && !pricey.length && !down) { bar.innerHTML = ""; return; }
  const parts = [];
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
  if (open) renderProjects().then(renderTasks);
}
function setTaskView(v) {
  taskView = v;
  document.getElementById("tabneed").className = v === "attention" ? "on" : "";
  document.getElementById("taball").className = v === "all" ? "on" : "";
  renderTasks();
}
async function renderProjects() {
  const sel = document.getElementById("tproj");
  const keep = sel.value;
  const r = await (await fetch("/api/projects", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({action: "list"})})).json();
  const rows = (r.projects || []).filter(p => !p.archived);
  sel.innerHTML = `<option value="">all projects</option>` + rows.map(p =>
    `<option value="${esc(p.slug)}">${esc(p.title)}${
      p.needs ? ` (${p.needs})` : p.open ? ` · ${p.open}` : ""}</option>`).join("");
  sel.value = keep;
}
async function addTask() {
  const goal = document.getElementById("tgoal").value.trim();
  if (!goal) return;
  const cwd = document.getElementById("tcwd").value.trim();
  // an explicit project wins; otherwise inherit whatever is being filtered on
  const proj = document.getElementById("tnewproj").value.trim()
            || document.getElementById("tproj").value;
  const r = await taskCall({action: "create", goal, project: proj || undefined,
                            scope: cwd ? {cwd} : undefined});
  if (r.ok) {
    document.getElementById("tgoal").value = "";
    document.getElementById("tnewproj").value = "";
    toast("Task queued");
    await renderProjects();
    renderTasks();
  } else toast(r.error || "Could not create it");
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
async function taskAction(id, action, notes) {
  const r = await taskCall(notes ? {action, id, notes, by: "you"} : {action, id});
  toast(r.ok ? `Task ${action}ed` : (r.error || "Not allowed"));
  renderTasks();
  refreshTaskBadge();
}
function review(t) {
  // Show why it is waiting, so approving is an informed click rather than a leap.
  const rv = (t.result || {}).review;
  if (!rv) return "";
  const items = (rv.findings || []).map(f => `<li>${esc(f)}</li>`).join("");
  return `<div class="rev"><b>${rv.ok ? "Review passed" : "Review flagged"}</b>
    ${esc(rv.summary || "")}${items ? `<ul>${items}</ul>` : ""}</div>`;
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
    b.push(`<button class="ghost" onclick="sendBack('${t.id}',true)">Answer…</button>`);
    b.push(`<button class="ghost" onclick="taskAction('${t.id}','dismiss')">Dismiss</button>`);
  } else if (t.state === "failed") {
    b.push(`<button class="ghost" onclick="taskAction('${t.id}','retry')">Retry</button>`);
    b.push(`<button class="ghost" onclick="taskAction('${t.id}','dismiss')">Dismiss</button>`);
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
  const r = await taskCall({action: taskView === "attention" ? "attention" : "list",
                            project: project || undefined});
  if (!r.ok) { list.innerHTML = `<div class="hint">${esc(r.error || "bot offline")}</div>`; return; }
  updateTaskBadge(r.counts);
  if (!r.tasks.length) {
    list.innerHTML = taskView === "attention"
      ? `<div class="hint">Nothing needs you. 🎉</div>`
      : `<div class="hint">No tasks yet — queue one above.</div>`;
    return;
  }
  list.innerHTML = r.tasks.map(t => `<div class="task">
      <span class="st st-${esc(t.state)}">${esc(t.state)}</span>
      <span class="tt">${esc(t.title)}
        <div class="sub">${t.project ? `<span class="proj">${esc(t.project)}</span> · ` : ""}${
          esc(t.id)} · ${esc(t.source)}${t.attempts > 1 ? ` · attempt ${t.attempts}` : ""} · ${
          age(t.created)}</div>${review(t)}</span>
      ${taskButtons(t)}</div>`).join("");
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
      ? `<a href="https://slack.com/archives/${x.source.replace(":", "/p").replace(".", "")}" target="_blank">source</a>` : "";
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

// --- polling ---
loadList(); loadStats();
setInterval(loadList, 5000);
setInterval(loadStats, 30000);
setInterval(() => { if (active) loadTranscript(false); }, 4000);
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
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    where = "127.0.0.1" if LOOPBACK else BIND
    print(f"Silkworm visualizer: http://{where}:{PORT} (bot bridge on :{BOT_PORT})"
          + ("" if LOOPBACK else "  [token required]"))
    server.serve_forever()

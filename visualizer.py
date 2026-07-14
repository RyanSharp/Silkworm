#!/usr/bin/env python3
"""Silkworm session visualizer — a local web page for browsing thread sessions.

Reads sessions.json (thread -> session mapping) and the Claude Code transcript
files in ~/.claude/projects/, and serves a single-page viewer on localhost:

    python3 visualizer.py            # http://127.0.0.1:8790
    SILKWORM_VIZ_PORT=9000 python3 visualizer.py

Stdlib only; read-only (never writes to sessions or transcripts).
"""

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_FILE = BASE_DIR / "sessions.json"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
PORT = int(os.environ.get("SILKWORM_VIZ_PORT", "8790"))


# --- data loading -----------------------------------------------------------

def load_sessions() -> list[dict]:
    if not SESSIONS_FILE.exists():
        return []
    raw = json.loads(SESSIONS_FILE.read_text())
    out = []
    for key, entry in raw.items():
        if isinstance(entry, str):
            entry = {"session_id": entry}
        channel, _, thread_ts = key.partition(":")
        sid = entry.get("session_id", "")
        cwd = entry.get("cwd", "")
        out.append({
            "key": key,
            "channel": channel,
            "thread_ts": thread_ts,
            "session_id": sid,
            "model": entry.get("model"),
            "cwd": cwd,
            "turns": entry.get("turns", 0),
            "cost": entry.get("cost", 0.0),
            "updated": entry.get("updated", 0),
            "slack_link": f"https://slack.com/archives/{channel}/p{thread_ts.replace('.', '')}",
            "resume_cmd": f"cd {cwd or '~'} && claude --resume {sid}",
            "has_transcript": find_transcript(sid) is not None,
        })
    out.sort(key=lambda s: -s["updated"])
    return out


def find_transcript(session_id: str) -> Path | None:
    if not session_id or not PROJECTS_DIR.exists():
        return None
    for p in PROJECTS_DIR.glob(f"*/{session_id}.jsonl"):
        return p
    return None


def _block_list(content) -> list[dict]:
    """Normalize a message's content into renderable blocks."""
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


def load_transcript(session_id: str) -> dict:
    path = find_transcript(session_id)
    if not path:
        return {"error": "no transcript found for this session"}
    messages = []
    for line in path.read_text().splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") not in ("user", "assistant"):
            continue
        blocks = _block_list((d.get("message") or {}).get("content"))
        if not blocks:
            continue
        messages.append({
            "role": d["type"],
            "ts": d.get("timestamp", ""),
            "blocks": blocks,
        })
    return {"session_id": session_id, "path": str(path), "messages": messages}


# --- HTTP -------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data) -> None:
        self._send(200, json.dumps(data).encode(), "application/json")

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif url.path == "/api/sessions":
            self._json(load_sessions())
        elif url.path == "/api/session":
            sid = parse_qs(url.query).get("id", [""])[0]
            self._json(load_transcript(sid))
        else:
            self._send(404, b"not found", "text/plain")


# --- page -------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Silkworm — Sessions</title>
<style>
  :root {
    --bg: #2C0E2E; --panel: #3A1440; --panel2: #45195023; --line: #57265B;
    --ink: #F2E8F1; --muted: #B49BB6; --gold: #ECB22E; --silk: #F6EEDF;
    --green: #2EB67D; --mono: ui-monospace, "SF Mono", Menlo, monospace;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
         font: 15px/1.5 -apple-system, "Segoe UI", sans-serif; height: 100vh;
         display: flex; flex-direction: column; }
  header { display: flex; align-items: center; gap: 12px; padding: 12px 20px;
           border-bottom: 1px solid var(--line); flex: none; }
  header svg { width: 40px; height: 26px; }
  header h1 { font-size: 17px; margin: 0; font-weight: 600; letter-spacing: .3px; }
  header .sub { color: var(--muted); font-size: 13px; margin-left: auto; }

  .layout { display: flex; flex: 1; min-height: 0; }
  nav { width: 340px; flex: none; overflow-y: auto; border-right: 1px solid var(--line);
        padding: 10px; display: flex; flex-direction: column; gap: 8px; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
          padding: 10px 12px; cursor: pointer; }
  .card:hover { border-color: var(--gold); }
  .card.active { border-color: var(--gold); box-shadow: 0 0 0 1px var(--gold); }
  .card .key { font-family: var(--mono); font-size: 12px; color: var(--silk); word-break: break-all; }
  .card .meta { color: var(--muted); font-size: 12px; margin-top: 4px; display: flex; gap: 10px; flex-wrap: wrap; }
  .card .meta b { color: var(--gold); font-weight: 600; }

  main { flex: 1; overflow-y: auto; padding: 18px 22px; min-width: 0; }
  .toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 14px; flex-wrap: wrap; }
  .toolbar code { font-family: var(--mono); font-size: 12px; background: var(--panel);
                  border: 1px solid var(--line); border-radius: 8px; padding: 6px 10px;
                  overflow-x: auto; white-space: nowrap; max-width: 100%; }
  button { background: var(--gold); color: #3D1140; border: 0; border-radius: 8px;
           padding: 6px 12px; font-weight: 600; cursor: pointer; font-size: 13px; }
  button.ghost { background: transparent; color: var(--muted); border: 1px solid var(--line); }
  a { color: var(--gold); }

  .msg { max-width: 780px; margin: 0 0 14px; }
  .msg .who { font-size: 11px; text-transform: uppercase; letter-spacing: .8px;
              color: var(--muted); margin-bottom: 4px; }
  .bubble { border-radius: 12px; padding: 10px 14px; white-space: pre-wrap; word-break: break-word; }
  .user .bubble { background: var(--silk); color: #33203B; }
  .assistant .bubble { background: var(--panel); border: 1px solid var(--line); }
  details { margin: 6px 0; }
  summary { cursor: pointer; font-family: var(--mono); font-size: 12px; color: var(--gold); }
  summary.result { color: var(--green); }
  summary.think { color: var(--muted); }
  details pre { background: #240826; border-radius: 8px; padding: 10px; overflow-x: auto;
                font-size: 12px; margin: 6px 0 0; }
  .empty { color: var(--muted); margin-top: 60px; text-align: center; }
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
  <span class="sub" id="status"></span>
</header>
<div class="layout">
  <nav id="list"></nav>
  <main id="main"><div class="empty">Pick a session on the left.</div></main>
</div>
<script>
const esc = s => s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let active = null;

function age(ts) {
  const d = (Date.now()/1000 - ts) / 86400;
  if (d < 1/24) return Math.round(d*24*60) + "m ago";
  if (d < 1) return Math.round(d*24) + "h ago";
  return Math.round(d) + "d ago";
}

async function loadList() {
  const sessions = await (await fetch("/api/sessions")).json();
  document.getElementById("status").textContent =
    sessions.length + " session" + (sessions.length === 1 ? "" : "s");
  const nav = document.getElementById("list");
  nav.innerHTML = "";
  for (const s of sessions) {
    const div = document.createElement("div");
    div.className = "card" + (active && active.key === s.key ? " active" : "");
    div.innerHTML = `<div class="key">${esc(s.key)}</div>
      <div class="meta"><span><b>${s.turns}</b> turns</span>
      <span><b>$${(s.cost||0).toFixed(2)}</b></span>
      <span>${esc(s.model || "default")}</span>
      <span>${age(s.updated)}</span>
      ${s.has_transcript ? "" : "<span>⚠︎ no transcript</span>"}</div>`;
    div.onclick = () => { active = s; loadList(); loadTranscript(s); };
    nav.appendChild(div);
  }
}

async function loadTranscript(s) {
  const data = await (await fetch("/api/session?id=" + encodeURIComponent(s.session_id))).json();
  const main = document.getElementById("main");
  const cmd = esc(s.resume_cmd);
  let h = `<div class="toolbar">
    <code id="cmd">${cmd}</code>
    <button onclick="navigator.clipboard.writeText(document.getElementById('cmd').textContent)">Copy resume command</button>
    <button class="ghost" onclick="window.open('${esc(s.slack_link)}')">Open in Slack</button>
  </div>`;
  if (data.error) {
    h += `<div class="empty">${esc(data.error)}</div>`;
  } else {
    for (const m of data.messages) {
      let inner = "";
      for (const b of m.blocks) {
        if (b.type === "text") inner += `<div class="bubble">${esc(b.text)}</div>`;
        else if (b.type === "thinking")
          inner += `<details><summary class="think">thinking</summary><pre>${esc(b.text)}</pre></details>`;
        else if (b.type === "tool")
          inner += `<details><summary>⚙ ${esc(b.name)}</summary><pre>${esc(b.input)}</pre></details>`;
        else if (b.type === "tool_result")
          inner += `<details><summary class="result">↳ result</summary><pre>${esc(b.text)}</pre></details>`;
      }
      const when = m.ts ? new Date(m.ts).toLocaleTimeString() : "";
      h += `<div class="msg ${m.role}"><div class="who">${m.role} · ${when}</div>${inner}</div>`;
    }
  }
  main.innerHTML = h;
}

loadList();
setInterval(() => { loadList(); if (active) loadTranscript(active); }, 10000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Silkworm visualizer: http://127.0.0.1:{PORT}")
    server.serve_forever()

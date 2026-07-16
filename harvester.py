"""Periodic learning harvester.

Reads each Silkworm-tracked session's transcript, feeds the turns that are new
since the last run to a model, and distills durable do/avoid/note learnings into
the LearningStore — deduped against what's already there. A per-session
watermark (harvest_state.json) means each turn is only ever evaluated once.
"""

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

import repos

log = logging.getLogger("silkworm.harvester")

PROJECTS_DIR = Path.home() / ".claude" / "projects"
MAX_CHARS = 30000  # cap conversation text sent to the model per session

PROMPT = """You are auditing a completed AI coding/assistant session to extract \
durable LEARNINGS that would help future sessions do better work.

A good learning is generalizable and actionable — a preference the user \
expressed, a mistake the assistant made and corrected, a project convention, a \
tool that worked or failed, a constraint to respect. Ignore one-off task \
details, chit-chat, and anything already covered by the existing learnings.

Existing learnings (do NOT restate these):
{existing}

Conversation (new turns since last audit):
{convo}

Return ONLY a JSON array (possibly empty). Each item:
  {{"type": "do"|"avoid"|"note", "text": "<= 18 words, imperative", "scope": "global"|"project"}}
"do" = always do this; "avoid" = never do this; "note" = useful context.
"scope": "project" if it is specific to THIS codebase/directory, else "global".
Output nothing but the JSON array."""


def find_transcript(session_id: str) -> Path | None:
    if not session_id or not PROJECTS_DIR.exists():
        return None
    for p in PROJECTS_DIR.glob(f"*/{session_id}.jsonl"):
        return p
    return None


def _new_turns(path: Path, since_ts: str) -> tuple[str, str]:
    """Return (conversation_text, max_timestamp) for entries newer than since_ts."""
    parts, max_ts = [], since_ts
    for line in path.read_text().splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") not in ("user", "assistant"):
            continue
        ts = d.get("timestamp", "")
        if ts and since_ts and ts <= since_ts:
            continue
        if ts > max_ts:
            max_ts = ts
        content = (d.get("message") or {}).get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            chunks = []
            for b in content:
                t = b.get("type")
                if t == "text":
                    chunks.append(b.get("text", ""))
                elif t == "tool_use":
                    chunks.append(f"[calls {b.get('name', 'tool')}]")
                elif t == "tool_result" and b.get("is_error"):
                    inner = b.get("content")
                    if isinstance(inner, list):
                        inner = " ".join(x.get("text", "") for x in inner if isinstance(x, dict))
                    chunks.append(f"[tool ERROR: {str(inner)[:300]}]")
            text = "\n".join(c for c in chunks if c.strip())
        if text.strip():
            parts.append(f"{d['type'].upper()}: {text.strip()}")
    convo = "\n\n".join(parts)
    if len(convo) > MAX_CHARS:
        convo = convo[-MAX_CHARS:]  # keep the most recent
    return convo, max_ts


def _extract(binary: str, model: str, env: dict, existing: list[str], convo: str) -> list[dict]:
    existing_txt = "\n".join(f"- {t}" for t in existing) or "(none)"
    prompt = PROMPT.format(existing=existing_txt, convo=convo)
    try:
        proc = subprocess.run(
            [binary, "-p", "--model", model, "--output-format", "text"],
            input=prompt, capture_output=True, text=True, timeout=180, env=env)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("harvest model call failed: %s", e)
        return []
    out = proc.stdout.strip()
    m = re.search(r"\[.*\]", out, re.DOTALL)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return items if isinstance(items, list) else []


def harvest(store, learnings, *, binary: str, model: str, env: dict,
            state_path: Path) -> dict:
    """One harvest pass over all tracked sessions. Returns a summary dict."""
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except (json.JSONDecodeError, OSError):
            state = {}
    sessions_state = state.setdefault("sessions", {})

    added, scanned = 0, 0
    for key, entry in store.all().items():
        sid = entry.get("session_id")
        if not sid:
            continue
        path = find_transcript(sid)
        if not path:
            continue
        since = sessions_state.get(sid, "")
        # Skip if the file hasn't grown past what we've seen (cheap mtime guard).
        if since and path.stat().st_mtime <= sessions_state.get(sid + "@mtime", 0):
            continue
        convo, max_ts = _new_turns(path, since)
        if not convo.strip():
            sessions_state[sid + "@mtime"] = path.stat().st_mtime
            continue
        scanned += 1
        items = _extract(binary, model, env, learnings.texts(), convo)
        cwd = entry.get("cwd", "")
        # Prefer the repo identity so worktrees/clones of the same repo share
        # the learning; fall back to the cwd path for non-repo directories.
        project_scope = repos.identity(cwd) or cwd
        for it in items:
            ltype = str(it.get("type", "")).lower()
            text = str(it.get("text", "")).strip()
            if ltype not in ("do", "avoid", "note") or not text:
                continue
            scope = project_scope if str(it.get("scope", "")).lower() == "project" else ""
            rec = learnings.add_deduped(ltype, text, scope,
                                        origin="harvest", source=key)
            if rec:
                added += 1
        sessions_state[sid] = max_ts
        sessions_state[sid + "@mtime"] = path.stat().st_mtime

    state["last_run"] = time.time()
    state_path.write_text(json.dumps(state, indent=2))
    log.info("harvest: %d sessions scanned, %d learnings added", scanned, added)
    return {"scanned": scanned, "added": added, "last_run": state["last_run"]}

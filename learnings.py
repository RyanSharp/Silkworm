"""Persistent, ever-growing store of learnings injected into session prompts.

A learning is a short instruction distilled from experience:
  type    — "do" (always), "avoid" (never), or "note" (context)
  text    — the instruction
  scope   — "" = global (every session), or an absolute path prefix so the
            learning only applies to threads working under that directory
  origin  — "harvest" (auto-distilled from a session) or "manual"
  source  — the thread key / session it came from (for auditing)
  enabled — audited off learnings stay in the record but aren't injected

Stored as a JSON array in learnings.json; thread-safe; read by the bot, the
harvester, and the visualizer.
"""

import json
import threading
import time
import uuid
from pathlib import Path

import repos

TYPES = ("do", "avoid", "note")
_LABELS = {"do": "ALWAYS", "avoid": "NEVER", "note": "CONTEXT"}


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


class LearningStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._data: list[dict] = []
        if path.exists():
            try:
                self._data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = []

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._data, indent=2))

    def add(self, ltype: str, text: str, scope: str = "", *,
            origin: str = "manual", source: str = "", enabled: bool = True) -> dict:
        if ltype not in TYPES:
            raise ValueError(f"type must be one of {TYPES}")
        rec = {"id": "lrn_" + uuid.uuid4().hex[:8], "type": ltype,
               "text": text.strip(), "scope": scope.strip(),
               "origin": origin, "source": source, "enabled": enabled,
               "created": time.time()}
        with self._lock:
            self._data.append(rec)
            self._save()
        return rec

    def add_deduped(self, ltype: str, text: str, scope: str = "", **kw) -> dict | None:
        """Add unless a same-scope learning with near-identical text exists."""
        key = _norm(text)
        with self._lock:
            for x in self._data:
                if x["scope"] == scope.strip() and _norm(x["text"]) == key:
                    return None
        return self.add(ltype, text, scope, **kw)

    def delete(self, learning_id: str) -> bool:
        with self._lock:
            before = len(self._data)
            self._data = [x for x in self._data if x["id"] != learning_id]
            if len(self._data) != before:
                self._save()
                return True
            return False

    def set_enabled(self, learning_id: str, enabled: bool) -> bool:
        with self._lock:
            for x in self._data:
                if x["id"] == learning_id:
                    x["enabled"] = enabled
                    self._save()
                    return True
            return False

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(x) for x in self._data]

    def texts(self) -> list[str]:
        with self._lock:
            return [x["text"] for x in self._data]

    def applicable(self, cwd: str) -> list[dict]:
        """Enabled learnings that apply to a session working in cwd.

        A learning applies if it is global, if its scope equals the session's
        repo identity (shared across all worktrees/clones of that repo), or —
        for non-repo dirs and legacy path scopes — if cwd is under a path scope.
        """
        cwd = str(cwd)
        repo = repos.identity(cwd)
        with self._lock:
            out = [dict(x) for x in self._data
                   if x.get("enabled", True) and _matches(x["scope"], cwd, repo)]
        out.sort(key=lambda x: (TYPES.index(x["type"]), x["created"]))
        return out


def _matches(scope: str, cwd: str, repo: str) -> bool:
    if not scope:
        return True                       # global
    if repo and scope == repo:
        return True                       # same repo identity
    if scope.startswith("/"):             # path scope (non-repo dirs, legacy)
        return cwd == scope or cwd.startswith(scope.rstrip("/") + "/")
    return False


def render_block(learnings: list[dict]) -> str:
    """Format applicable learnings as a system-prompt section (stable text)."""
    if not learnings:
        return ""
    lines = ["Operator learnings for this session — follow them:"]
    for ltype in TYPES:
        items = [x for x in learnings if x["type"] == ltype]
        if not items:
            continue
        lines.append(f"{_LABELS[ltype]}:")
        lines += [f"- {x['text']}" for x in items]
    return "\n".join(lines)

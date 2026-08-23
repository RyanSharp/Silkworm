"""Projects: what a task belongs to, as distinct from where it may act.

`scope` answers "which directory and repo may this touch" -- an isolation
question. `project` answers "what body of work is this part of" -- an
organisational one. They are deliberately separate: two projects can share a
repo, one project can span several, and plenty of projects ("plan the Asia
trip") have no repo at all. Grouping tasks by cwd would quietly work for the
first case and fall apart on the last.

Projects are created on first use rather than set up in advance. A task system
that requires you to define a project before you can file anything is a task
system you stop using. A project may carry a default scope, which tasks filed
under it inherit when they don't specify their own -- so "work on the trader"
lands in the right directory without repeating it every time.
"""

import json
import logging
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("silkworm.projects")

VERSION = 1

#: A brief is context on every prompt, so it has to stay small. Past this it
#: costs more than it saves.
BRIEF_CHARS = 1500

FIELDS: dict[str, tuple] = {
    "slug":     (None,  "stable identifier, e.g. asia-trip"),
    "v":        (0,     "schema version of this record"),
    "title":    ("",    "human name, e.g. Asia Trip"),
    "scope":    (dict,  "default {cwd, repo} tasks inherit when unset"),
    # What has been decided about this project, injected into every task filed
    # under it. Rewritten rather than appended, so it stays a short living
    # brief instead of an ever-growing log that taxes every prompt.
    "brief":    ("",    "durable facts and decisions; injected as context"),
    "brief_at": (0.0,   "unix time the brief was last rewritten"),
    "archived": (False, "hidden from pickers; existing tasks keep their label"),
    "created":  (0.0,   "unix time"),
    "updated":  (0.0,   "unix time"),
}


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s[:40] or "untitled"


def default(name: str):
    if name not in FIELDS:
        return None
    d = FIELDS[name][0]
    return d() if callable(d) else d


class ProjectStore:
    """projects.json: slug -> record."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if path.exists():
            for slug, rec in json.loads(path.read_text()).items():
                for f in FIELDS:
                    rec.setdefault(f, default(f))
                self._data[slug] = rec

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._data, indent=2))

    def get(self, slug: str) -> dict | None:
        with self._lock:
            rec = self._data.get(slug)
            return dict(rec) if rec else None

    def ensure(self, name: str, **fields) -> dict:
        """Look up a project by name or slug, creating it if it's new."""
        slug = slugify(name)
        with self._lock:
            rec = self._data.get(slug)
            if rec is None:
                rec = {f: default(f) for f in FIELDS}
                rec.update(slug=slug, v=VERSION, title=(name or slug).strip(),
                           created=time.time(), updated=time.time())
                self._data[slug] = rec
                log.info("project %s created (%s)", slug, rec["title"])
            rec.update({k: v for k, v in fields.items()
                        if k in FIELDS and k not in ("slug", "created")})
            rec["updated"] = time.time()
            self._save()
            return dict(rec)

    def set_archived(self, slug: str, archived: bool) -> bool:
        with self._lock:
            rec = self._data.get(slug)
            if not rec:
                return False
            rec["archived"] = bool(archived)
            rec["updated"] = time.time()
            self._save()
            return True

    def all(self, include_archived: bool = True) -> list[dict]:
        with self._lock:
            out = [dict(r) for r in self._data.values()
                   if include_archived or not r.get("archived")]
        return sorted(out, key=lambda r: r.get("title", "").lower())

    def set_brief(self, slug: str, text: str) -> dict | None:
        with self._lock:
            rec = self._data.get(slug)
            if not rec:
                return None
            rec["brief"] = (text or "").strip()[:BRIEF_CHARS]
            rec["brief_at"] = time.time()
            rec["updated"] = time.time()
            self._save()
            return dict(rec)

    def brief_for(self, slug: str) -> str:
        return (self.get(slug or "") or {}).get("brief", "")

    def scope_for(self, slug: str) -> dict:
        """The default scope a task filed under this project inherits."""
        rec = self.get(slug or "")
        return dict((rec or {}).get("scope") or {})


def context_block(brief: str, title: str = "") -> str:
    """The brief as it appears in a task's system prompt."""
    if not (brief or "").strip():
        return ""          # never spend tokens, or churn the cache, on nothing
    head = f"What you already know about {title}:" if title else "Project context:"
    return f"{head}\n{brief.strip()}"


BRIEF_PROMPT = """Keep a short standing brief for a project, for whoever picks \
up the next piece of work on it.

Current brief (rewrite it; do not append):
{brief}

What just happened:
{event}

Return the updated brief and nothing else. Keep only durable things: decisions \
made, constraints, preferences, names and facts that will still matter next \
week. Drop anything transient, anything already obvious from the task itself, \
and anything superseded by what just happened. Aim for under 150 words. If \
nothing durable changed, return the current brief unchanged."""


def summarise(projects: list[dict], tasks: list[dict],
              needs_attention: tuple) -> list[dict]:
    """Projects with their task counts, so a picker shows where the work is."""
    by_slug: dict[str, dict] = {}
    for t in tasks:
        slug = t.get("project") or ""
        if not slug:
            continue
        row = by_slug.setdefault(slug, {"open": 0, "needs": 0, "total": 0})
        row["total"] += 1
        if t.get("state") in needs_attention:
            row["needs"] += 1
        if t.get("state") in ("proposed", "queued", "running", "blocked",
                              "awaiting_approval", "needs_input"):
            row["open"] += 1
    out = []
    for p in projects:
        counts = by_slug.get(p["slug"], {"open": 0, "needs": 0, "total": 0})
        out.append({**p, **counts})
    # Anything filed under a project that was never registered still shows up,
    # rather than vanishing because its record is missing.
    known = {p["slug"] for p in projects}
    for slug, counts in by_slug.items():
        if slug not in known:
            out.append({"slug": slug, "title": slug, "scope": {},
                        "archived": False, **counts})
    return sorted(out, key=lambda r: (-r["needs"], -r["open"], r["title"].lower()))

"""Task records: the unit of managed work.

Silkworm is the source of truth for the state of a task, even when the item it
came from lives elsewhere (a GitHub issue); see DESIGN.md. This module owns the
record, the legal state transitions and the store. It deliberately knows nothing
about executing anything -- wiring that up is a separate step, so this can land
without touching a running bot.

Fields are declared here the way session fields are declared in schema.py: with
a default and a meaning, so a reader never has to guess whether a missing value
means "old record" or "nothing happened".
"""

import json
import logging
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger("silkworm.tasks")

VERSION = 1

# --- lifecycle ----------------------------------------------------------------

PROPOSED = "proposed"                    # auto-ingested; needs triage
QUEUED = "queued"
RUNNING = "running"
AWAITING_APPROVAL = "awaiting_approval"
NEEDS_INPUT = "needs_input"
BLOCKED = "blocked"                      # waiting on another task
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

STATES = (PROPOSED, QUEUED, RUNNING, AWAITING_APPROVAL, NEEDS_INPUT,
          BLOCKED, DONE, FAILED, CANCELLED)

#: The only states that put a task in front of the user. Everything else is the
#: system's business; see the "what needs me?" rule in DESIGN.md.
NEEDS_ATTENTION = (PROPOSED, AWAITING_APPROVAL, NEEDS_INPUT, FAILED)

TERMINAL = (DONE, CANCELLED)

#: state -> states it may move to. Anything absent is rejected, so an executor
#: bug shows up as a refused transition rather than a task in a nonsense state.
TRANSITIONS: dict[str, tuple] = {
    PROPOSED:          (QUEUED, CANCELLED),
    QUEUED:            (RUNNING, BLOCKED, CANCELLED),
    RUNNING:           (DONE, FAILED, AWAITING_APPROVAL, NEEDS_INPUT, BLOCKED, CANCELLED),
    AWAITING_APPROVAL: (RUNNING, CANCELLED, FAILED),
    NEEDS_INPUT:       (RUNNING, CANCELLED, FAILED),
    BLOCKED:           (QUEUED, CANCELLED, FAILED),
    FAILED:            (QUEUED, CANCELLED),        # retry
    DONE:              (),
    CANCELLED:         (),
}


class InvalidTransition(Exception):
    pass


# --- record -------------------------------------------------------------------

FIELDS: dict[str, tuple] = {
    "id":          (None,  "tsk_… identifier"),
    # Who is responsible for running this. "inline" means a live handler (a
    # Slack turn) already owns it and the queue runner must keep its hands off;
    # "queue" means nobody is driving it and the runner may claim it. Defaults
    # to inline so a task never starts executing merely by existing.
    "driver":      ("inline", "inline | queue — who executes this"),
    "v":           (0,     "schema version of this record"),
    "title":       ("",    "short human label"),
    "goal":        ("",    "what the task should achieve; the prompt"),
    "state":       (QUEUED, "lifecycle state; see STATES"),
    "role":        ("assistant", "role template this runs as"),
    "source":      ("ui",  "where it came from: ui | slack | github | email | …"),
    "source_ref":  ("",    "identifier in the originating system, if any"),
    "scope":       (dict,  "{repo, cwd, branch, worktree, paths} — where it may act"),
    "thread":      ("",    "Slack thread key for narration, if any"),
    "session_id":  (None,  "Claude Code session executing it"),
    "checkpoint":  (None,  "in-flight marker; recovery.py's pending, generalised"),
    "parent":      (None,  "task that spawned this one"),
    "root":        (None,  "top-level task this belongs to"),
    "blocked_on":  (list,  "task ids that must finish first"),
    "result":      (None,  "{text, artifacts, cost} once finished"),
    "events":      (list,  "state changes and notable occurrences, capped at 50"),
    "attempts":    (0,     "how many times execution has been tried"),
    "created":     (0.0,   "unix time"),
    "updated":     (0.0,   "unix time of the last write"),
}


def default(name: str):
    if name not in FIELDS:
        return None
    d = FIELDS[name][0]
    return d() if callable(d) else d


def new_id() -> str:
    return "tsk_" + uuid.uuid4().hex[:10]


def make(goal: str, **fields) -> dict:
    """Build a record with every field present, so readers never guess."""
    task = {name: default(name) for name in FIELDS}
    task.update(id=new_id(), v=VERSION, goal=goal,
                created=time.time(), updated=time.time())
    task.update({k: v for k, v in fields.items() if k in FIELDS})
    if not task.get("title"):
        task["title"] = (goal or "").strip().splitlines()[0][:60] if goal else "(untitled)"
    stray = [k for k in fields if k not in FIELDS]
    if stray:
        log.warning("ignoring undeclared task field(s): %s", ", ".join(stray))
    if task["state"] not in STATES:
        raise ValueError(f"unknown state {task['state']!r}")
    return task


def can(from_state: str, to_state: str) -> bool:
    return to_state in TRANSITIONS.get(from_state, ())


# --- store --------------------------------------------------------------------

class TaskStore:
    """tasks.json: id -> record. Same shape as SessionStore, on purpose."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if path.exists():
            for tid, rec in json.loads(path.read_text()).items():
                # Fill in anything a newer field list added, without clobbering.
                for name in FIELDS:
                    rec.setdefault(name, default(name))
                self._data[tid] = rec

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._data, indent=2))

    def create(self, goal: str, **fields) -> dict:
        task = make(goal, **fields)
        with self._lock:
            self._data[task["id"]] = task
            self._save()
        log.info("task %s created in %s: %s", task["id"], task["state"], task["title"])
        return dict(task)

    def get(self, tid: str) -> dict | None:
        with self._lock:
            rec = self._data.get(tid)
            return dict(rec) if rec else None

    def update(self, tid: str, **fields) -> dict | None:
        stray = [f for f in fields if f not in FIELDS]
        if stray:
            log.warning("writing undeclared field(s) %s on %s", ", ".join(stray), tid)
        if "state" in fields:
            raise ValueError("use transition() to change state")
        with self._lock:
            rec = self._data.get(tid)
            if rec is None:
                return None
            rec.update(fields)
            rec["updated"] = time.time()
            self._save()
            return dict(rec)

    def transition(self, tid: str, to_state: str, detail: str = "") -> dict:
        """Move a task's state, refusing anything the lifecycle disallows."""
        if to_state not in STATES:
            raise ValueError(f"unknown state {to_state!r}")
        with self._lock:
            rec = self._data.get(tid)
            if rec is None:
                raise KeyError(tid)
            current = rec["state"]
            if current == to_state:
                return dict(rec)                     # idempotent
            if not can(current, to_state):
                raise InvalidTransition(f"{tid}: {current} -> {to_state}")
            rec["state"] = to_state
            rec["updated"] = time.time()
            events = rec.setdefault("events", [])
            events.append({"at": time.time(), "kind": to_state, "detail": detail[:200]})
            del events[:-50]
            if to_state == RUNNING:
                rec["attempts"] = (rec.get("attempts") or 0) + 1
            self._save()
            log.info("task %s %s -> %s%s", tid, current, to_state,
                     f" ({detail})" if detail else "")
            return dict(rec)

    def all(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}

    def by_state(self, *states: str) -> list[dict]:
        with self._lock:
            out = [dict(v) for v in self._data.values() if v.get("state") in states]
        return sorted(out, key=lambda t: t.get("created", 0))

    def needs_attention(self) -> list[dict]:
        """The default view: only what the user has to act on."""
        return self.by_state(*NEEDS_ATTENTION)

    def next_queued(self) -> dict | None:
        pending = self.by_state(QUEUED)
        return pending[0] if pending else None

    def claim(self) -> dict | None:
        """Take the oldest queued task the runner is allowed to execute.

        The claim (queued -> running) happens under the same lock that selects
        it, so two runners -- or a runner and a restart -- can never both pick
        up the same task. Tasks with driver="inline" are owned by a live Slack
        turn and are never claimed here.
        """
        with self._lock:
            candidates = [r for r in self._data.values()
                          if r.get("state") == QUEUED and r.get("driver") == "queue"]
            if not candidates:
                return None
            rec = min(candidates, key=lambda r: r.get("created", 0))
            rec["state"] = RUNNING
            rec["attempts"] = (rec.get("attempts") or 0) + 1
            rec["updated"] = time.time()
            events = rec.setdefault("events", [])
            events.append({"at": time.time(), "kind": RUNNING, "detail": "claimed by the runner"})
            del events[:-50]
            self._save()
            log.info("task %s claimed by the runner", rec["id"])
            return dict(rec)

    def requeue_interrupted(self) -> int:
        """Return tasks left mid-run by a restart to the queue.

        A queue task in `running` with nobody running it cannot make progress,
        so it is recorded as interrupted and put back. Inline tasks are left
        alone: recovery.py already rescues their reply from the transcript.
        """
        moved = 0
        for tid, rec in list(self._data.items()):
            if rec.get("state") != RUNNING or rec.get("driver") != "queue":
                continue
            self.transition(tid, FAILED, "interrupted by a restart")
            self.transition(tid, QUEUED, "requeued after a restart")
            moved += 1
        return moved

    def counts(self) -> dict[str, int]:
        with self._lock:
            out: dict[str, int] = {}
            for rec in self._data.values():
                out[rec.get("state", "?")] = out.get(rec.get("state", "?"), 0) + 1
        return out

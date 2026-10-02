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

import logging
import threading
import time
import uuid
from pathlib import Path

import jsonstore
import roles

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

#: Who may be executing a task. `inline` means a live handler already owns it;
#: `queue` means the runner may claim it. Checked like STATES, and for the same
#: reason: this field decides whether anything ever picks the task up. A value
#: nothing recognises is claimed by the runner (which wants "queue") and owned
#: by no live handler either, so the task simply sits in `queued` for ever,
#: looking filed and never running.
DRIVERS = ("inline", "queue")

#: The keys of `result` a compacted record keeps. Cost is the number the history
#: is read for ("what has this project cost me"), and `review` is the record
#: that the work was checked -- the dashboard shows it on every row, finished
#: ones included, and it is bounded at a 300-char summary and 20 findings by
#: roles.parse_verdict. `landed` is the commit the work reached the base branch
#: as (db2a533); it is forty bytes, it is written nowhere else, and without it a
#: task that landed is indistinguishable from one whose branch was discarded --
#: the same mistake as losing the verdict. `landing` is the same argument for
#: the case that did *not* land -- the stage the merge refused at and why. It is
#: what stops a finished task with unmerged commits reading as plainly done, so
#: throwing it away at fourteen days would only defer that silence rather than
#: end it, and a refusal is worth keeping more than a success is. The reply text
#: is the bulk, and once the work has landed nobody opens that again.
RESULT_KEEPS = ("cost", "review", "landed", "landing")

#: States a blocker never comes back from on its own. A task waiting on one of
#: these will never be released by it, so the store releases it instead; see
#: TaskStore._release_waiters. `done` belongs here: a reviewer that finishes
#: normally moves its parent on before finishing, so a parent still `blocked`
#: on a finished task was missed, not waiting.
ENDED = TERMINAL + (FAILED,)

#: state -> states it may move to. Anything absent is rejected, so an executor
#: bug shows up as a refused transition rather than a task in a nonsense state.
TRANSITIONS: dict[str, tuple] = {
    PROPOSED:          (QUEUED, CANCELLED),
    QUEUED:            (RUNNING, BLOCKED, CANCELLED),
    # QUEUED because work can be sent back to be redone: it ran, failed the
    # project's own tests, and goes round again with the failure attached.
    # Without it the send-back is refused and swallowed, and the task sits in
    #  for ever -- which is exactly what happened the first time.
    RUNNING:           (DONE, FAILED, AWAITING_APPROVAL, NEEDS_INPUT, BLOCKED,
                        CANCELLED, QUEUED),
    # DONE: you looked and it's fine. QUEUED: send it back to be reworked.
    # Without those two, a task could enter this state and have no way out.
    AWAITING_APPROVAL: (RUNNING, QUEUED, DONE, CANCELLED, FAILED),
    NEEDS_INPUT:       (RUNNING, QUEUED, CANCELLED, FAILED),
    # DONE/AWAITING_APPROVAL: a task blocked on its review is resolved by the
    # reviewer's verdict, not by going round the queue again.
    BLOCKED:           (QUEUED, DONE, AWAITING_APPROVAL, CANCELLED, FAILED),
    # DONE because recovery can arrive after the failure was recorded: a
    # restart marks an in-flight turn failed, and a later sweep finds the
    # child finished and delivers its reply. Refusing that leaves a false
    # failure on the board, which is the noise that stops it being trusted.
    FAILED:            (QUEUED, CANCELLED, DONE),   # retry, or a late rescue
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
    # Whether this work belongs in its own checkout. Decided when the record is
    # made, and deliberately *not* derived from `driver`: a restart hands an
    # orphaned conversational turn to the runner (close_out_orphans), and if
    # isolation followed the driver, "fix what I'm working on" would come back
    # in a worktree where your uncommitted edits do not exist.
    "isolate":     (False, "run in its own checkout, away from your working tree"),
    "v":           (0,     "schema version of this record"),
    "title":       ("",    "short human label"),
    "goal":        ("",    "what the task should achieve; the prompt"),
    "state":       (QUEUED, "lifecycle state; see STATES"),
    "role":        ("assistant", "role template this runs as"),
    "source":      ("ui",  "where it came from: ui | dashboard | slack | github | email | …"),
    # What body of work this belongs to. Distinct from scope: scope is where it
    # may act, project is what it is part of. Plenty of projects have no repo.
    "project":     ("",    "project slug, or empty for unfiled"),
    "source_ref":  ("",    "identifier in the originating system, if any"),
    "scope":       (dict,  "{repo, cwd, branch, worktree, paths} — where it may act"),
    "thread":      ("",    "Slack thread key for narration, if any"),
    "session_id":  (None,  "Claude Code session executing it"),
    # Written the moment a queued turn's session exists (on_init) and cleared
    # when the turn ends, so one still here after a restart is a turn that was
    # killed part-way -- and names the session to resume rather than redo.
    "checkpoint":  (None,  "{session_id, at} of the turn in flight, if any"),
    "parent":      (None,  "task that spawned this one"),
    "root":        (None,  "top-level task this belongs to"),
    "blocked_on":  (list,  "task ids that must finish first"),
    "result":      (None,  "{text, artifacts, cost} once finished"),
    # What the task left behind in git. The worktree is released when the turn
    # ends and took the only trace of it with it -- the branch outlives the
    # checkout, but nothing except one line in a Slack reply ever said so, and
    # eight finished tasks went unmerged without the board knowing. Note this
    # `branch` is the task's own; `scope["branch"]` is the base it builds on.
    # Merge state is deliberately not stored: you can land a branch by hand,
    # and a stale flag is worse than asking git. See branches.py.
    "branch":      ("",    "branch this task's commits are on, if it made any"),
    "base":        ("",    "the base it was cut from, as resolved at the time"),
    "commits":     (0,     "commits it made, counted as its checkout closed"),
    "events":      (list,  "state changes and notable occurrences, capped at 50"),
    "attempts":    (0,     "how many times execution has been tried"),
    # Runs that died of something global (quota, overload) before doing any
    # work. Their `attempts` increment is refunded -- that counter decides
    # between "retry later" and "a person must look", and an outage that never
    # let the task start says nothing about the task -- and counted here
    # instead, so they are still bounded and still visible.
    "false_starts": (0,    "runs refunded because they never reached real work"),
    # Set once the record has been through compact_older_than: the work landed
    # long enough ago that the reply text and event log were dropped. The
    # record itself stays, so a missing result never has to be guessed at.
    "compacted":   (False, "bulky detail dropped; summary fields kept"),
    # Set when a turn died on something transient (quota, overload). The task
    # waits in `blocked` until this passes, rather than asking for help.
    "retry_at":    (None,  "unix time to requeue this automatically"),
    # How many times this chain of scheduled wake-ups has already fired.
    # Capped, so a model that keeps misjudging "is it done yet" cannot
    # poll at your expense forever.
    "defers":      (0,     "depth of the scheduled wake-up chain"),
    # Evidence from the project's own tests, kept so the record says whether
    # work was proven rather than merely believed.
    "verified":        (None,  "True/False from the test command, None = not run"),
    "verify_attempts": (0,     "times it was sent back for failing tests"),
    "commit_attempts": (0,     "times it was sent back for leaving its work uncommitted"),
    # Each once at most, and only on projects that take unsupervised work:
    # see bot.MAX_REVIEW_REWORKS. A person's Send back with findings counts too.
    "review_reworks":   (0,    "times it was sent back for a review's findings"),
    "conflict_reworks": (0,    "times it was sent back to catch up with a moved base"),
    # Kept apart from `result`, which each run's turn rewrites whole, so a
    # second flagged review can still show what the first one asked for.
    "reworked_findings": (list, "review findings it was already sent back for"),
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
    if task["driver"] not in DRIVERS:
        raise ValueError(f"unknown driver {task['driver']!r}")
    # The role decides what the task is allowed to do when it runs, so an
    # unrecognised one is refused here rather than persisted and discovered at
    # execution time. Validated in the same place as the state, and for the
    # same reason: a field that changes behaviour should never reach the store
    # holding a value nothing understands.
    if not task["role"]:
        task["role"] = default("role")
    if not roles.known(task["role"]):
        raise ValueError(f"unknown role {task['role']!r}")
    return task


#: What a turn killed by a restart is resumed with, in place of its goal. The
#: session already holds the goal and everything done towards it; restating the
#: goal reads as "start again", which is the cost this exists to avoid.
RESUME_PROMPT = (
    "Your previous turn on this task was interrupted (by a Silkworm restart "
    "or an outage) before it finished. Carry on from where you were. Check the actual state "
    "first (git status, git log, files you were editing) -- some of the work "
    "may already be done or committed -- then finish the task and report the "
    "outcome as you would have.")


#: Sources whose work is a conversation with you, or a continuation of one.
#: These run where you are working, never in a checkout of their own.
CONVERSATIONAL = ("slack", "defer")


def isolated(rec: dict) -> bool:
    """Whether this task runs in its own checkout rather than yours.

    Ask the record, never the driver. Who executes a task changes -- a Slack
    turn orphaned by a restart is handed to the queue runner so its message is
    not lost -- but what kind of work it is does not, and only that decides
    where it may run.
    """
    return bool(rec.get("isolate"))


def can(from_state: str, to_state: str) -> bool:
    return to_state in TRANSITIONS.get(from_state, ())


# --- store --------------------------------------------------------------------

class TaskStore:
    """tasks.json: id -> record. Same shape as SessionStore, on purpose."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        for tid, rec in (jsonstore.load(path, default={}) or {}).items():
            # Isolation used to be inferred from `driver`. Records written
            # before it became a field of its own keep the answer that rule
            # gave them -- read here, at load, before restart recovery
            # rewrites any driver it would have been inferred from.
            #
            # Except where that rule was the bug: a conversation, or a
            # conversation's scheduled wake-up, already handed to the
            # runner by an earlier restart reads as driver="queue" and
            # must not inherit an isolation it was never meant to have.
            # `source` says what the work is, and nothing rewrites it.
            rec.setdefault("isolate", rec.get("driver") == "queue"
                           and rec.get("source") not in CONVERSATIONAL)
            # Fill in anything a newer field list added, without clobbering.
            for name in FIELDS:
                rec.setdefault(name, default(name))
            self._data[tid] = rec

    def _save(self) -> None:
        jsonstore.save(self._path, self._data)

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

    def transition(self, tid: str, to_state: str, detail: str = "",
                   _release: bool = True, _seen: set | None = None,
                   _unblock: tuple = ()) -> dict:
        """Move a task's state, refusing anything the lifecycle disallows.

        Ending a task also releases whatever was waiting on it. That happens
        here, not in the executor, because a blocker can end in half a dozen
        places -- an error, a stop, a dismissal from the dashboard -- and only
        one of them ever remembered to look. `_release=False` is for the one
        case where the ending is not real: a task marked failed by a restart
        and requeued in the same breath has not lost anybody their blocker.
        """
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
            if _unblock:
                # Forgetting an ended blocker happens in the same write as the
                # state change, never before it. Persisting the strip first and
                # then dying would leave a task `blocked` with an empty
                # `blocked_on` -- invisible to the board and to the audit that
                # exists to find it, which is the exact strand this prevents.
                rec["blocked_on"] = [b for b in (rec.get("blocked_on") or [])
                                     if b not in _unblock]
            rec["updated"] = time.time()
            # A retry time is a reason to be blocked, and leaving `blocked`
            # spends it. Kept, it outlived the wait it was set for: a task that
            # once hit a quota limit and later parked for its reviewer was
            # requeued within the minute, ran again, and closed with its review
            # and landing skipped.
            if current == BLOCKED:
                rec["retry_at"] = None
            events = rec.setdefault("events", [])
            events.append({"at": time.time(), "kind": to_state, "detail": detail[:200]})
            del events[:-50]
            if to_state == RUNNING:
                rec["attempts"] = (rec.get("attempts") or 0) + 1
            self._save()
            log.info("task %s %s -> %s%s", tid, current, to_state,
                     f" ({detail})" if detail else "")
            moved = dict(rec)
        if _release and to_state in ENDED:
            self._release_waiters(tid, to_state, detail, _seen or {tid})
        return moved

    # --- nothing waits on a task that has stopped ----------------------------
    #
    # `blocked` is the one state that is neither running, finished, nor asking
    # for anything: it is not in NEEDS_ATTENTION, so a task sitting in it is
    # invisible. That is correct while its blocker is still going, and a trap
    # the moment the blocker stops. A reviewer that finishes normally moves its
    # parent on itself; one that errored, was stopped, or was dismissed used to
    # move nothing, and the implementor's finished work sat on a branch that
    # nobody would ever be told about again. DESIGN.md says the board is the
    # source of truth for the state of work, and it cannot be if work can fall
    # off it.

    def _ended(self, tid: str) -> bool:
        """Whether a blocker (held under the lock) will never resolve anything."""
        rec = self._data.get(tid)
        return rec is None or rec.get("state") in ENDED

    def _plan_release(self, blocker_id: str, ended_state: str | None,
                      detail: str) -> list[tuple[str, str | None, str, tuple]]:
        """Say what should become of each task waiting on an ended blocker.

        Reads under the lock and writes nothing: the strip is handed to the
        caller so it can be made in the same write as the state change. An
        earlier version saved the strip here, which meant a crash in the gap
        left a task `blocked` with an empty `blocked_on` -- the one shape the
        audit below cannot find.
        """
        what = {DONE: "finished", FAILED: "failed",
                CANCELLED: "was cancelled"}.get(ended_state, "no longer exists")
        plan = []
        with self._lock:
            blocker = self._data.get(blocker_id) or {}
            label = "review" if blocker.get("role") == "reviewer" else "blocker"
            for wid, rec in self._data.items():
                waiting = rec.get("blocked_on") or []
                if blocker_id not in waiting or rec.get("state") != BLOCKED:
                    # A waiter that has already moved on keeps its blocked_on:
                    # that is what stops a reviewed task being reviewed twice.
                    continue
                # Every blocker that has ended goes, not only the one that just
                # did. A leftover id makes a later rerun skip both verification
                # and the review gate, which is the hazard blocked_on guards.
                dead = tuple(b for b in waiting if self._ended(b))
                if len(dead) < len(waiting):
                    plan.append((wid, None, "", dead))   # still waiting on something live
                    continue
                why = f"{label} {blocker_id} {what}"
                if detail:
                    why += f": {detail}"
                # Work that exists is work someone should look at. A task with
                # nothing to show has not earned an approval prompt, and saying
                # so on the board beats silence.
                plan.append((wid, AWAITING_APPROVAL if rec.get("result") else FAILED,
                             why, dead))
        return plan

    def _drop_blockers(self, tid: str, dead: tuple) -> None:
        """Forget ended blockers for a task that is still waiting on others."""
        if not dead:
            return
        with self._lock:
            rec = self._data.get(tid)
            if rec is None:
                return
            rest = [b for b in (rec.get("blocked_on") or []) if b not in dead]
            if rest != (rec.get("blocked_on") or []):
                rec["blocked_on"] = rest
                self._save()

    def _release_waiters(self, blocker_id: str, ended_state: str | None,
                         detail: str, seen: set) -> list[str]:
        """Move every task stranded by `blocker_id` ending. Returns their ids.

        Nothing raised here may reach the caller: this is bookkeeping around
        somebody else's transition, which has already happened, and turning it
        into an error would report a state change that did occur as a failure.
        A release that does not happen leaves the waiter exactly as it was, so
        the audit finds it on the next beat.
        """
        freed = []
        for wid, to_state, why, dead in self._plan_release(blocker_id, ended_state, detail):
            if to_state is None:
                self._drop_blockers(wid, dead)
                continue
            if wid in seen:
                continue                        # a cycle, or already handled
            seen.add(wid)
            try:
                self.transition(wid, to_state, why[:200], _seen=seen, _unblock=dead)
                freed.append(wid)
                log.warning("task %s released from %s: %s", wid, blocker_id, why)
            except Exception:
                log.exception("could not release %s waiting on %s", wid, blocker_id)
        return freed

    def release_stranded(self) -> list[str]:
        """Free tasks already waiting on a blocker that has ended.

        The release above only fires as a blocker ends, which does nothing for
        a task stranded before the rule existed, or by a crash in between. Run
        at startup and on the scheduler's beat so `blocked` always means
        "waiting on something that is still going".
        """
        with self._lock:
            ended: dict[str, str | None] = {}
            for rec in self._data.values():
                if rec.get("state") != BLOCKED:
                    continue
                for b in rec.get("blocked_on") or []:
                    if self._ended(b):
                        ended[b] = (self._data.get(b) or {}).get("state")
        freed = []
        for bid, state in ended.items():
            freed += self._release_waiters(
                bid, state, "found by the stranded-task audit", set())
        if freed:
            log.warning("released %d task(s) stranded on an ended blocker: %s",
                        len(freed), ", ".join(freed))
        return freed

    def all(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}

    def by_state(self, *states: str) -> list[dict]:
        with self._lock:
            out = [dict(v) for v in self._data.values() if v.get("state") in states]
        return sorted(out, key=lambda t: t.get("created", 0))

    def by_project(self, slug: str) -> list[dict]:
        with self._lock:
            out = [dict(v) for v in self._data.values() if (v.get("project") or "") == slug]
        return sorted(out, key=lambda t: -t.get("created", 0))

    def needs_attention(self) -> list[dict]:
        """The default view: only what the user has to act on."""
        return self.by_state(*NEEDS_ATTENTION)

    def next_queued(self) -> dict | None:
        pending = self.by_state(QUEUED)
        return pending[0] if pending else None

    def claim(self, *, only_roles=None, except_roles=None) -> dict | None:
        """Take the oldest queued task the runner is allowed to execute.

        The claim (queued -> running) happens under the same lock that selects
        it, so two runners -- or a runner and a restart -- can never both pick
        up the same task. Tasks with driver="inline" are owned by a live Slack
        turn and are never claimed here.

        `only_roles` / `except_roles` split the queue into lanes: reviews have one
        of their own, so a read-only review is not stuck behind an implementor
        that may run for hours, while implementors stay strictly one at a time.
        The filter is applied in the same select, under the same lock -- a
        lane picking a task and then checking its role would be a second step
        another lane could slip between.

        Nor is a task claimed while another queue task is running on its thread
        or is its parent or child. Their turns share the thread's lock, so the
        claim would only park a worker waiting on it -- and a review claimed
        while its reworked implementor runs would check out the branch before
        that implementor finished moving it. Queue tasks only: those are put
        back on every restart, so a stale `running` cannot wedge a thread.
        """
        with self._lock:
            busy = [r for r in self._data.values()
                    if r.get("state") == RUNNING and r.get("driver") == "queue"]
            threads = {r.get("thread") for r in busy if r.get("thread")}
            ids = {r.get("id") for r in busy}
            parents = {r.get("parent") for r in busy if r.get("parent")}

            def fits(r) -> bool:
                role = r.get("role") or "assistant"
                if only_roles is not None and role not in only_roles:
                    return False
                if except_roles is not None and role in except_roles:
                    return False
                if r.get("thread") and r.get("thread") in threads:
                    return False
                return r.get("parent") not in ids and r.get("id") not in parents

            candidates = [r for r in self._data.values()
                          if r.get("state") == QUEUED and r.get("driver") == "queue"
                          and fits(r)]
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

    def refund_attempt(self, tid: str) -> dict | None:
        """Take back the `attempts` a run cost when it never reached real work.

        Every claim spends an attempt, and the retry budget is counted in
        them. A run killed by a quota outage before its first tool call spent
        one each for nothing, so a few outages failed tasks that had never
        started. Counted in `false_starts` instead, in the same write.
        """
        with self._lock:
            rec = self._data.get(tid)
            if rec is None:
                return None
            rec["attempts"] = max(0, (rec.get("attempts") or 0) - 1)
            rec["false_starts"] = (rec.get("false_starts") or 0) + 1
            rec["updated"] = time.time()
            self._save()
            return dict(rec)

    def requeue_interrupted(self) -> int:
        """Return tasks left mid-run by a restart to the queue.

        A queue task in `running` with nobody running it cannot make progress,
        so it is recorded as interrupted and put back. Inline tasks are left
        alone: recovery.py already rescues their reply from the transcript.

        Put back to be *resumed*, not redone. Its checkpoint -- the session the
        killed turn was running, recorded when that session began -- and its
        worktree are both left exactly where they are: the runner resumes the
        session in the same checkout when it next claims the task. Clearing
        either here would start the work again from nothing, which is what
        every restart used to cost.
        """
        moved = 0
        for tid, rec in list(self._data.items()):
            if rec.get("state") != RUNNING or rec.get("driver") != "queue":
                continue
            sid = (rec.get("checkpoint") or {}).get("session_id") or ""
            # _release=False: it is going straight back in the queue, so
            # anything blocked on it has not actually lost its blocker.
            self.transition(tid, FAILED, "interrupted by a restart", _release=False)
            self.transition(tid, QUEUED,
                            f"requeued after a restart; will resume session {sid[:8]}"
                            if sid else "requeued after a restart")
            moved += 1
        return moved

    def supersede_failed(self, thread: str, before: float) -> list[str]:
        """Cancel failed conversational turns on `thread` older than `before`.

        A Slack thread is a conversation. If it carried on and produced
        answers, an earlier failed turn is history -- you either re-asked or
        moved on -- so it is not an open action item, and leaving it on the
        board is how the board stops being read.

        Restricted to conversational (inline) tasks on purpose. A queued task
        that failed is real work that did not happen, and an unrelated success
        somewhere else says nothing about whether it still needs doing.
        """
        if not thread:
            return []
        superseded = []
        with self._lock:
            for tid, rec in self._data.items():
                if (rec.get("state") == FAILED and rec.get("thread") == thread
                        and rec.get("driver") == "inline"
                        and (rec.get("created") or 0) < before):
                    superseded.append(tid)
        for tid in superseded:
            try:
                self.transition(tid, CANCELLED,
                                "superseded by a later successful turn on this thread")
            except InvalidTransition:
                superseded.remove(tid)
        if superseded:
            log.info("superseded %d stale failure(s) on %s", len(superseded), thread)
        return superseded

    def compact_older_than(self, days: float) -> list[str]:
        """Strip the freight from finished records untouched for `days`.

        tasks.json is rewritten whole on every create, update, transition and
        claim, and every Slack turn adds a record, so it only ever grows. What
        grows it is not the records themselves -- it is the reply text and the
        event log hanging off work that landed weeks ago and is never opened
        again.

        Same decision as hiding threads rather than deleting them (cdbe005):
        keep the record, lose the weight. Title, state, timestamps and cost
        survive, so "what has this project had done to it" still reads back;
        `result.text` and the events go.

        Nothing in NEEDS_ATTENTION is compacted, however old. That is the list
        the user actually works from, and a task that has been waiting on them
        for a month is the last one whose detail should be thrown away.
        """
        cutoff = time.time() - days * 86400
        with self._lock:
            ids = []
            for tid, rec in self._data.items():
                if rec.get("state") in NEEDS_ATTENTION or rec.get("state") not in TERMINAL:
                    continue
                if rec.get("compacted") or (rec.get("updated") or 0) >= cutoff:
                    continue
                result = rec.get("result")
                result = result if isinstance(result, dict) else {}
                if not rec.get("events") and not result.get("text"):
                    continue                      # nothing to drop; leave it be
                ids.append(tid)
            for tid in ids:
                rec = self._data[tid]
                result = rec.get("result")
                if isinstance(result, dict):
                    kept = {k: v for k, v in result.items() if k in RESULT_KEEPS}
                    rec["result"] = kept or None
                rec["events"] = []
                rec["compacted"] = True
                # `updated` is deliberately left alone: compacting is tidying,
                # not activity, and a compacted task must not look freshly
                # worked on -- nor drift back inside the retention window.
            if ids:
                self._save()
        if ids:
            log.info("compacted %d finished task(s) older than %sd", len(ids), days)
        return ids

    def due_retries(self, now: float) -> list[str]:
        """Ids of blocked tasks whose retry time has arrived.

        Not a task blocked on another task: that one is waiting for the other
        to finish, not for a clock, and requeueing it runs it again while its
        reviewer is still reading the first attempt. Checked here as well as
        cleared on the way out of `blocked`, because records written before
        that carry a spent time into their next wait.
        """
        with self._lock:
            return [tid for tid, r in self._data.items()
                    if r.get("state") == BLOCKED and r.get("retry_at")
                    and r["retry_at"] <= now and not self._waiting_on_open(r)]

    def _waiting_on_open(self, rec: dict) -> bool:
        """Whether a task is waiting on another that has not finished.

        `blocked_on` alone is not enough: it outlives the review it named --
        retrying a failed task does not clear it -- so treating any entry as a
        live wait would strand that task's next quota retry for good. Caller
        holds the lock."""
        return any((self._data.get(t) or {}).get("state") not in TERMINAL
                   and t in self._data for t in rec.get("blocked_on") or ())

    def has_pending_wakeup(self, thread: str) -> bool:
        """Whether a scheduled wake-up is still waiting on this thread."""
        with self._lock:
            return any(r.get("state") == BLOCKED and r.get("source") == "defer"
                       and r.get("thread") == thread and r.get("retry_at")
                       for r in self._data.values())

    def counts(self) -> dict[str, int]:
        with self._lock:
            out: dict[str, int] = {}
            for rec in self._data.values():
                out[rec.get("state", "?")] = out.get(rec.get("state", "?"), 0) + 1
        return out

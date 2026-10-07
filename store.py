"""Persistent thread -> Claude session state.

sessions.json maps "channel:thread_ts" to one session record; every field of
that record is declared in schema.py, which is also where its default and
meaning live. Records are migrated forward on load.
"""

import logging
import threading
import time
from pathlib import Path

import jsonstore
import schema

log = logging.getLogger("silkworm.store")


class SessionStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        raw = jsonstore.load(path, default={})
        if raw:
            migrated = False
            for key, val in raw.items():
                # Read the old version first: migrate() edits in place, so
                # afterwards `val` is the migrated record, not the original.
                had = val.get("v") if isinstance(val, dict) else None
                entry = schema.migrate(val)
                entry.setdefault("updated", time.time())
                if had != entry.get("v"):
                    migrated = True
                unknown = schema.unknown_fields(entry)
                if unknown:
                    # Kept, not dropped: a newer Silkworm may have written them.
                    log.warning("%s has fields not in schema v%d: %s",
                                key, schema.VERSION, ", ".join(unknown))
                self._data[key] = entry
            if migrated:
                # Write the migration through, so what's on disk matches what
                # we're holding rather than waiting for an unrelated update.
                self._save()
                log.info("migrated %d session record(s) to schema v%d",
                         len(raw), schema.VERSION)

    def _save(self) -> None:
        jsonstore.save(self._path, self._data)

    def get(self, key: str) -> dict | None:
        with self._lock:
            entry = self._data.get(key)
            return dict(entry) if entry else None

    def update(self, key: str, **fields) -> dict:
        stray = [f for f in fields if f not in schema.FIELDS]
        if stray:  # a typo'd field name would otherwise persist silently
            log.warning("writing undeclared field(s) %s on %s — add them to schema.py",
                        ", ".join(stray), key)
        with self._lock:
            entry = self._data.setdefault(key, {"v": schema.VERSION})
            entry.update(fields)
            entry["updated"] = time.time()
            self._save()
            return dict(entry)

    def add_file(self, key: str, record: dict) -> None:
        with self._lock:
            entry = self._data.setdefault(key, {})
            files = entry.setdefault("files", [])
            files.append(record)
            del files[:-200]  # cap per thread
            self._save()

    def mark_pruned(self, key: str, paths, at: float) -> int:
        """Say on a thread's file records that their bytes were removed.

        The record is the fact the file existed; only the weight goes. See
        artifacts.py. Not activity, so `updated` is left alone.
        """
        paths = set(paths)
        with self._lock:
            n = 0
            for rec in (self._data.get(key) or {}).get("files") or ():
                if rec.get("path") in paths and not rec.get("pruned"):
                    rec["pruned"] = at
                    n += 1
            if n:
                self._save()
            return n

    def add_cost(self, key: str, reported: float, session_id: str | None = None) -> float:
        """Record one turn on `key`, given the cost its result event reported.

        That figure is the session's running total once a session is resumed
        (turncost.py), so the turn's own cost is the increase since this
        session's last turn -- found on this thread, or on whichever thread
        last ran it. Returns the turn's cost, which is what is added here and
        what the caller should record and show.
        """
        import turncost
        with self._lock:
            entry = self._data.setdefault(key, {})
            previous = None
            if session_id:
                previous = (entry.get("session_totals") or {}).get(session_id)
                if previous is None:
                    # The most recently written thread that knows it: one
                    # handed between threads has its newest total there.
                    for other in sorted(self._data.values(),
                                        key=lambda e: -(e.get("updated") or 0)):
                        seen = (other.get("session_totals") or {}).get(session_id)
                        if seen is not None:
                            previous = seen
                            break
            cost = turncost.per_turn(reported, previous)
            if session_id:
                totals = entry.setdefault("session_totals", {})
                totals.pop(session_id, None)          # newest last
                totals[session_id] = float(reported or 0.0)
                # Never the thread's own session: reviewers run a fresh one on
                # their parent's thread every round, and evicting the session
                # it resumes would charge its whole running total again.
                for old in list(totals)[:-turncost.KEEP_SESSIONS]:
                    if old not in (session_id, entry.get("session_id")):
                        del totals[old]
            entry["cost"] = round(entry.get("cost", 0.0) + cost, 6)
            entry["turns"] = entry.get("turns", 0) + 1
            # Per-turn history, so a turn that costs wildly more than this
            # thread's norm can be spotted (a cache regression looks like this).
            costs = entry.setdefault("costs", [])
            costs.append(round(cost, 6))
            del costs[:-50]
            entry["updated"] = time.time()
            self._save()
            return cost

    def correct_costs(self, key: str, targets: dict, pairs=(), seeds=None) -> bool:
        """Apply turncost.correct's correction to one thread. Returns whether
        anything changed.

        `targets` is {task id: how far the correction moves that task's cost
        from its original}. What has been applied is kept per task
        (`cost_corrections`), so the thread moves by the difference: a second
        pass, or one retried after a failure part-way, moves it by nothing.
        `pairs` are (recorded, corrected) per-turn figures, oldest first,
        replaced in the 50-turn history where they are still found; `seeds`
        are each session's last reported total, kept only where none is
        recorded yet -- a newer one is from a turn this correction never saw.
        Not activity, so `updated` is left alone.
        """
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return False
            changed = False
            applied = entry.setdefault("cost_corrections", {})
            delta = round(sum(t - applied.get(tid, 0.0) for tid, t in targets.items()), 6)
            for tid, t in targets.items():
                if applied.get(tid) != t:
                    applied[tid] = t
                    changed = True
            if delta:
                total = round(entry.get("cost", 0.0) + delta, 6)
                if total < 0:
                    log.warning("%s: corrected cost went below zero (%.4f); "
                                "its record predates some of its turns", key, total)
                    total = 0.0
                entry["cost"] = total
                changed = True
            costs = entry.get("costs") or []
            i = len(costs) - 1
            for cur, new in reversed(list(pairs)):
                j = i
                while j >= 0 and abs(costs[j] - round(cur, 6)) > 1e-9:
                    j -= 1
                if j < 0:
                    continue                   # aged out of the history
                costs[j] = round(new, 6)
                changed = True
                i = j - 1
            totals = entry.setdefault("session_totals", {})
            for sid, total in (seeds or {}).items():
                if sid not in totals:
                    totals[sid] = float(total)
                    changed = True
            if changed:
                self._save()
            return changed

    def add_event(self, key: str, kind: str, detail: str = "") -> None:
        """Record something notable that happened to this thread.

        Recovery, reaping and releases all used to leave no trace outside the
        log, so a thread that had been interrupted looked identical to one that
        had simply been quiet.
        """
        with self._lock:
            entry = self._data.setdefault(key, {})
            events = entry.setdefault("events", [])
            events.append({"at": time.time(), "kind": kind, "detail": detail[:200]})
            del events[:-50]
            self._save()

    def drop(self, key: str) -> bool:
        with self._lock:
            if self._data.pop(key, None) is not None:
                self._save()
                return True
            return False

    def all(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}

    def find_by_session(self, session_id: str) -> str | None:
        with self._lock:
            for key, entry in self._data.items():
                if entry.get("session_id") == session_id:
                    return key
            return None

    def file_under(self, mapping: dict) -> list[str]:
        """File threads under projects: {key: slug}. Returns the keys filed.

        Only a thread with no project, and not one unfiled on purpose, is
        touched -- so a thread you filed by hand keeps its project, and running
        this twice changes nothing the second time. One save for the batch,
        and not activity, so `updated` is left alone.
        """
        with self._lock:
            done = []
            for key, slug in mapping.items():
                rec = self._data.get(key)
                if rec is None or not slug or rec.get("project") or rec.get("unfiled"):
                    continue
                rec["project"] = slug
                done.append(key)
            if done:
                self._save()
            return done

    def set_hidden(self, key: str, hidden: bool) -> bool:
        """Hide or unhide one thread. Nothing is discarded either way."""
        with self._lock:
            rec = self._data.get(key)
            if rec is None:
                return False
            rec["hidden"] = bool(hidden)
            rec["updated"] = rec.get("updated", 0.0)   # hiding is not activity
            self._save()
            return True

    def hide_older_than(self, days: float, kinds=()) -> list[str]:
        """Hide threads untouched for `days`. Returns the keys hidden.

        `kinds` narrows it -- task-run threads are one-offs that pile up, while
        a quiet conversation may still be one you return to.
        """
        cutoff = time.time() - days * 86400
        with self._lock:
            keys = [k for k, v in self._data.items()
                    if not v.get("hidden")
                    and v.get("updated", 0) < cutoff
                    and not v.get("pending")
                    and (not kinds or (v.get("kind") or "thread") in kinds)]
            for k in keys:
                self._data[k]["hidden"] = True
            if keys:
                self._save()
            return keys

    #: What makes a record worth keeping, however old it gets. The first line
    #: is what hiding exists to protect -- a thread you are finished looking at
    #: is still one you spent money on. The second is live state: a session to
    #: resume, a terminal holding it, a binding you set, a redelivery guard, a
    #: decision to put it away. Between them, anything left is a husk.
    KEEPS = ("title", "summary", "cost", "turns", "costs", "files", "events",
             "session_totals", "cost_corrections",
             "session_id", "previous_sessions", "pending", "checked_out",
             "terminal_live", "project", "unfiled", "last_msg_ts", "hidden")

    def forget_empty(self, days: float, keep=()) -> list[str]:
        """Drop only records older than `days` that hold nothing. Returns the keys.

        Age used to be the whole rule, which quietly undid hiding: a task run
        was put away at 14 days, kept its old `updated` stamp because hiding is
        not activity, and was deleted outright at 30 -- taking the title,
        summary, cost history and file list with it. Nobody designed that
        pipeline; it fell out of two features that never met.

        So age is a floor now, not a reason. A record only goes if there is
        nothing in it to lose (see KEEPS) and nothing pointing at it: `keep`
        carries the thread keys tasks still refer to, because deleting one of
        those orphans a task that may be waiting on a person.
        """
        cutoff = time.time() - days * 86400
        keep = set(keep)
        with self._lock:
            gone = [k for k, v in self._data.items()
                    if v.get("updated", 0) < cutoff
                    and k not in keep
                    and not any(v.get(f) for f in self.KEEPS)]
            for k in gone:
                del self._data[k]
            if gone:
                self._save()
            return gone

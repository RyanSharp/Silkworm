"""Persistent thread -> Claude session state.

sessions.json maps "channel:thread_ts" to one session record; every field of
that record is declared in schema.py, which is also where its default and
meaning live. Records are migrated forward on load.
"""

import json
import logging
import threading
import time
from pathlib import Path

import schema

log = logging.getLogger("silkworm.store")


class SessionStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if path.exists():
            raw = json.loads(path.read_text())
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
        self._path.write_text(json.dumps(self._data, indent=2))

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

    def add_cost(self, key: str, cost: float) -> None:
        with self._lock:
            entry = self._data.setdefault(key, {})
            entry["cost"] = round(entry.get("cost", 0.0) + (cost or 0.0), 6)
            entry["turns"] = entry.get("turns", 0) + 1
            # Per-turn history, so a turn that costs wildly more than this
            # thread's norm can be spotted (a cache regression looks like this).
            costs = entry.setdefault("costs", [])
            costs.append(round(cost or 0.0, 6))
            del costs[:-50]
            entry["updated"] = time.time()
            self._save()

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

    def sweep(self, max_age_days: float) -> int:
        cutoff = time.time() - max_age_days * 86400
        with self._lock:
            stale = [k for k, v in self._data.items() if v.get("updated", 0) < cutoff]
            for k in stale:
                del self._data[k]
            if stale:
                self._save()
            return len(stale)

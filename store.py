"""Persistent thread -> Claude session state.

sessions.json maps "channel:thread_ts" to a dict:
  {session_id, model, cwd, cost, turns, updated}
Older versions stored a bare session-id string; those are migrated on load.
"""

import json
import threading
import time
from pathlib import Path


class SessionStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if path.exists():
            raw = json.loads(path.read_text())
            for key, val in raw.items():
                if isinstance(val, str):  # v1 format
                    val = {"session_id": val, "updated": time.time()}
                self._data[key] = val

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._data, indent=2))

    def get(self, key: str) -> dict | None:
        with self._lock:
            entry = self._data.get(key)
            return dict(entry) if entry else None

    def update(self, key: str, **fields) -> dict:
        with self._lock:
            entry = self._data.setdefault(key, {})
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

    def sweep(self, max_age_days: float) -> int:
        cutoff = time.time() - max_age_days * 86400
        with self._lock:
            stale = [k for k, v in self._data.items() if v.get("updated", 0) < cutoff]
            for k in stale:
                del self._data[k]
            if stale:
                self._save()
            return len(stale)

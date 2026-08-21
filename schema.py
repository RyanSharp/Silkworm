"""The shape of a session record, in one place.

sessions.json grew to eighteen fields by accretion, with no record of what any
of them meant or when they appeared. Anything reading an entry had to guess
whether a missing field meant "old record" or "nothing happened" -- a guess
that has already caused one bug (treating a missing summary_turns as 0 marked
every existing thread stale).

So: every field is declared here with its default and its meaning, records
carry a version, and migrate() brings an old record forward. Unknown fields are
kept, not dropped -- a newer Silkworm may have written them.
"""

import logging

log = logging.getLogger("silkworm.schema")

VERSION = 2

# name -> (default, description). The default is what a reader should assume
# when the field is absent; None means "unknown", which is not the same as 0.
FIELDS: dict[str, tuple] = {
    "v":                 (0,     "schema version of this record"),
    "session_id":        (None,  "Claude Code session this thread resumes"),
    "previous_sessions": (list,  "session ids this thread used before (newest last)"),
    "cwd":               ("",    "directory the session runs in"),
    "model":             (None,  "per-thread model override, or None for the default"),
    "title":             ("",    "short name shown in the dashboard"),
    # "thread" is a conversation you had; "task" is an anchor the queue runner
    # created to give a UI task somewhere to report. They are not peers.
    "kind":              ("thread", "thread | task — what this session is for"),
    # Bind a thread to a project and its tasks inherit it, so you say it once
    # rather than on every message.
    "project":           ("",    "project slug this thread's tasks belong to"),
    "summary":           ("",    "1-3 sentence description of the conversation"),
    "summary_ts":        ("",    "transcript timestamp the summary was written from"),
    "summary_turns":     (None,  "turn count when summarised; None = unknown, not 0"),
    "turns":             (0,     "completed turns"),
    "cost":              (0.0,   "total USD spent on this thread"),
    "costs":             (list,  "per-turn USD, newest last, capped at 50"),
    "files":             (list,  "files exchanged with this thread"),
    "events":            (list,  "notable occurrences (recovered, reaped, ...), capped at 50"),
    "updated":           (0.0,   "unix time of the last write"),
    "last_msg_ts":       (None,  "newest Slack ts picked up; guards against redelivery"),
    "pending":           (None,  "in-flight turn marker, or None -- see recovery.py"),
    "checked_out":       (False, "handed to a terminal session"),
    "terminal_live":     (False, "that terminal session is currently running"),
}


def default(name: str):
    """The value a reader should assume when `name` is absent."""
    if name not in FIELDS:
        return None
    d = FIELDS[name][0]
    return d() if callable(d) else d


def migrate(entry: dict) -> dict:
    """Bring one record up to VERSION. Never drops data."""
    if not isinstance(entry, dict):                 # v1 stored a bare session id
        return {"v": VERSION, "session_id": str(entry)}
    v = entry.get("v", 0)
    if v >= VERSION:
        return entry
    # v0/v1 -> v2 is purely additive: absent fields keep meaning "unknown", and
    # readers already handle that. Only the version stamp is new.
    entry["v"] = VERSION
    return entry


def unknown_fields(entry: dict) -> list[str]:
    return sorted(k for k in entry if k not in FIELDS)


def describe() -> str:
    lines = [f"session record v{VERSION}", ""]
    for name, (dflt, doc) in FIELDS.items():
        shown = dflt() if callable(dflt) else dflt
        lines.append(f"  {name:18} {str(shown):8} {doc}")
    return "\n".join(lines)

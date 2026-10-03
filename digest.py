"""The daily digest: what happened in the last day, without anyone asking.

The board says what needs you *now*. It does not say what went wrong
overnight and then quietly recovered, what landed while you slept, or that a
task has been "queued" for nine hours. Each of those has been found only by
someone going and looking, so once a day this says them.

Deterministic on purpose -- no model call. It is read off the task records the
same way the board is, so it can be tested from a fixture store and it costs
nothing to send. Posted to your DM, never the board channel: anything posted
there pushes the board message up the screen it exists to stay on.
"""

import logging
import statistics
import time
from datetime import datetime

import branches
import costs
import jsonstore
import merge
import tasks

log = logging.getLogger("silkworm.digest")

#: What "today's digest" covers.
WINDOW_S = 24 * 3600

#: Default local time to post, overridden by DIGEST_AT in .env.
DEFAULT_AT = "08:00"
#: DIGEST_AT values that switch it off. Not "0", which reads as midnight.
OFF = ("", "off", "none")

#: Titles listed per line before the rest are counted instead.
LIST_LIMIT = 5

#: Landing stages that are not refusals even when merge.needs_a_person would
#: say so: still going (bot.LANDING_UNDERWAY), or dropped on purpose.
NOT_REFUSALS = ("in-progress", "dropped")

#: "Stuck" is measured against how long tasks usually sit in a state -- three
#: times the median -- but never less than these, so a history of instant
#: claims does not turn ten minutes in the queue into an alarm.
STUCK_FLOOR_S = {tasks.QUEUED: 6 * 3600, tasks.RUNNING: 2 * 3600,
                 tasks.BLOCKED: 12 * 3600}
STUCK_FACTOR = 3

#: Where records filed under no project are grouped.
UNFILED = "(no project)"

WAITING = ((tasks.PROPOSED, "proposed"),
           (tasks.AWAITING_APPROVAL, "awaiting approval"),
           (tasks.NEEDS_INPUT, "needs input"))


# --- when ----------------------------------------------------------------------

def due(now: datetime, at: str, last_on: str) -> bool:
    """Whether today's digest should go out now.

    The same wall-clock catch-up as projects.due_for_ideation: a bot that was
    down or restarting at 08:00 still posts when it comes back, and one that
    already posted today does not post again.
    """
    return bool(at) and last_on != now.strftime("%Y-%m-%d") and now.strftime("%H:%M") >= at


class Schedule:
    """The once-a-day guard, durable across restarts.

    The day is remembered in memory as well as on disk: if the post went out
    but writing the file failed, this process still must not post it twice.
    """

    def __init__(self, path, at: str = DEFAULT_AT):
        self.path = path
        self.at = at
        self._last_on = self._read(path)

    @property
    def last_on(self) -> str:
        return self._last_on

    def due(self, now: datetime) -> bool:
        return due(now, self.at, self._last_on)

    def mark(self, now: datetime) -> None:
        self._last_on = now.strftime("%Y-%m-%d")
        jsonstore.save(self.path, {"last_on": self._last_on, "at": time.time()})

    @staticmethod
    def _read(path) -> str:
        # Lenient: an unreadable marker must not stop the digest for good. The
        # worst it costs is one repeat of a day already posted.
        state = jsonstore.load(path, default={}, strict=False)
        return str(state.get("last_on") or "") if isinstance(state, dict) else ""


# --- what ----------------------------------------------------------------------

def _esc(text: str) -> str:
    """Slack mrkdwn treats &, < and > as markup."""
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _title(rec: dict) -> str:
    t = (rec.get("title") or rec.get("id") or "").strip().splitlines()
    return _esc((t[0] if t else rec.get("id") or "")[:70])


def _listed(items: list[str]) -> str:
    shown = items[:LIST_LIMIT]
    more = len(items) - len(shown)
    return "; ".join(shown) + (f"; +{more} more" if more > 0 else "")


def _age(seconds: float) -> str:
    h = seconds / 3600
    return f"{h / 24:.0f}d" if h >= 48 else f"{h:.0f}h" if h >= 1 else f"{seconds / 60:.0f}m"


def _events(rec: dict) -> list[dict]:
    return [e for e in (rec.get("events") or []) if isinstance(e, dict) and e.get("at")]


def _last_event(rec: dict, kind: str | None = None) -> dict | None:
    for e in reversed(_events(rec)):
        if kind is None or e.get("kind") == kind:
            return e
    return None


def _landing_at(rec: dict):
    """When its landing outcome was recorded. A landing recorded before they
    were dated falls back to its last `done` (it lands as it finishes or as it
    is approved). An undated refusal has no such anchor -- any later event
    would make a week-old refusal read as today's -- so it is not dated at all;
    its branch still shows under "unmerged". Never `updated`, which any later
    write moves."""
    result = rec.get("result") or {}
    landing = result.get("landing") or {}
    if landing.get("at"):
        return landing["at"]
    e = _last_event(rec, tasks.DONE) if result.get("landed") else None
    return e.get("at") if e else None


def _entered(rec: dict) -> float:
    """When the record entered the state it is in now."""
    e = _last_event(rec, rec.get("state"))
    return (e or {}).get("at") or rec.get("created") or rec.get("updated") or 0


def usual_dwell(records) -> dict:
    """{state: median seconds a task spends in it}, from the event logs."""
    spans: dict = {}
    for rec in records:
        ev = _events(rec)
        for a, b in zip(ev, ev[1:]):
            if a.get("kind") in STUCK_FLOOR_S:
                spans.setdefault(a["kind"], []).append(b["at"] - a["at"])
    return {k: statistics.median(v) for k, v in spans.items() if v}


def stuck_limit(state: str, usual: dict) -> float:
    return max(STUCK_FLOOR_S[state], STUCK_FACTOR * usual.get(state, 0))


def _project_of(rec: dict, by_id: dict) -> str:
    """A reviewer is filed under no project; it belongs to its parent's."""
    parent = by_id.get(rec.get("parent")) or {}
    return rec.get("project") or parent.get("project") or UNFILED


def sections(records, now: float, *, branch_rows=(), released=None) -> dict:
    """{project: {section: [lines]}} for the window ending at `now`.

    `branch_rows` is branches.survey's output and `released` is
    {project: [tag, ...]}; both need git, so the caller gathers them and this
    stays a pure function of what it is given.
    """
    records = list(records)
    by_id = {r.get("id"): r for r in records}
    since = now - WINDOW_S
    usual = usual_dwell(records)
    out: dict = {}

    def add(proj, section, line):
        out.setdefault(proj, {}).setdefault(section, []).append(line)

    for rec in sorted(records, key=lambda r: r.get("created") or 0):
        proj = _project_of(rec, by_id)
        result = rec.get("result") if isinstance(rec.get("result"), dict) else {}
        landing = result.get("landing") or {}
        at = _landing_at(rec)

        if result.get("landed") and at and at >= since:
            add(proj, "landed", _title(rec))
        elif (merge.needs_a_person(landing) and landing.get("stage") not in NOT_REFUSALS
              # Handed to a task catching it up, which answers for it now.
              and not landing.get("reworked_by")
              and at and at >= since):
            stage = _esc(landing.get("stage") or "?")
            detail = _esc((landing.get("detail") or "").strip()[:90])
            add(proj, "refused", f"{_title(rec)} ({stage}{': ' + detail if detail else ''})")

        # Failed or held: the latest such event in the window, with its reason.
        # Held covers needs_input -- "not run: <project> needs a test command".
        bad = [e for e in _events(rec) if e["at"] >= since
               and e.get("kind") in (tasks.FAILED, tasks.NEEDS_INPUT)]
        if bad:
            e = bad[-1]
            what = "failed" if e["kind"] == tasks.FAILED else "held"
            why = _esc((e.get("detail") or "").strip()[:110]) or "no reason recorded"
            now_state = rec.get("state")
            later = f", now {now_state.replace('_', ' ')}" if now_state != e["kind"] else ""
            add(proj, "failed", f"{_title(rec)} — {what}: {why}{later}")

        runs = [e for e in _events(rec) if e.get("kind") == tasks.RUNNING and e["at"] >= since]
        if runs and (rec.get("attempts") or 0) > 1:
            add(proj, "reran", f"{_title(rec)} ({rec['attempts']} runs)")

        state = rec.get("state")
        if state in STUCK_FLOOR_S:
            # Blocked for a reason that is on the board already: a wake-up set
            # for later, or a blocker still open (which, if it is the thing
            # that is stuck, is listed itself -- or is waiting on you).
            excused = state == tasks.BLOCKED and (
                (rec.get("retry_at") and rec["retry_at"] > now)
                or any((by_id.get(b) or {}).get("state") not in tasks.ENDED
                       for b in rec.get("blocked_on") or () if b in by_id))
            dwell = now - _entered(rec)
            if not excused and dwell > stuck_limit(state, usual):
                add(proj, "stuck", f"{_title(rec)} — {state} {_age(dwell)}")

    waiting: dict = {}
    for rec in records:
        if rec.get("state") in dict(WAITING):
            row = waiting.setdefault(_project_of(rec, by_id), {})
            row[rec["state"]] = row.get(rec["state"], 0) + 1
    for proj, row in waiting.items():
        add(proj, "waiting", " · ".join(f"{row[s]} {label}" for s, label in WAITING if row.get(s)))

    grouped: dict = {}
    for r in branch_rows or ():
        grouped.setdefault(r.get("project") or UNFILED, []).append(r)
    for proj, rows in grouped.items():
        add(proj, "unmerged", branches.line(rows))

    for proj, tags in (released or {}).items():
        if tags:
            add(proj or UNFILED, "released", _esc(", ".join(tags)))

    for proj, fig in costs.by_project(records, now, days=WINDOW_S / 86400,
                                      unfiled=UNFILED).items():
        out.setdefault(proj, {})["cost"] = [costs.fmt(fig)]
    return out


#: Section, its label, and whether its lines are titles to be listed on one
#: line (with a count) rather than shown one per bullet.
ORDER = (("landed", "landed", True), ("released", "released", False),
         ("refused", "landing refused", False), ("failed", "failed or held", False),
         ("reran", "ran more than once", True), ("stuck", "stuck", False),
         ("waiting", "waiting on you", False), ("unmerged", "unmerged", False))


def render(records, now: float, *, branch_rows=(), released=None,
           when: datetime | None = None) -> str:
    """The digest as one compact Slack message."""
    records = list(records)
    data = sections(records, now, branch_rows=branch_rows, released=released)
    day = (when or datetime.fromtimestamp(now)).strftime("%a %-d %b")
    total = costs.by_project(records, now, days=WINDOW_S / 86400, unfiled=UNFILED)
    spent = {"usd": round(sum(f["usd"] for f in total.values()), 4),
             "complete": all(f["complete"] for f in total.values())}
    if not any(k != "cost" for s in data.values() for k in s):
        tail = f" (spent {costs.fmt(spent)})" if total else ""
        return f":seedling: *Silkworm daily* · {day} — nothing happened in the last 24h{tail}."

    head = f":seedling: *Silkworm daily* · {day} · last 24h"
    if total:
        head += f" · {costs.fmt(spent)} total"
    lines = [head]
    for proj in sorted(data, key=lambda p: (p == UNFILED, p.lower())):
        sec = data[proj]
        if not any(k != "cost" for k in sec):
            continue                    # spent money but nothing to say
        cost = f" · {sec['cost'][0]}" if sec.get("cost") else ""
        lines.append(f"*{_esc(proj)}*{cost}")
        for key, label, inline in ORDER:
            items = sec.get(key)
            if not items:
                continue
            if inline:
                lines.append(f"• {label} {len(items)}: {_listed(items)}")
            elif len(items) == 1:
                lines.append(f"• {label}: {items[0]}")
            else:
                lines.append(f"• {label}:")
                lines += [f"   ◦ {i}" for i in items[:LIST_LIMIT]]
                if len(items) > LIST_LIMIT:
                    lines.append(f"   ◦ +{len(items) - LIST_LIMIT} more")
    return "\n".join(lines)


# --- where ---------------------------------------------------------------------

def post(client, channel: str, text: str, refuse=()) -> dict:
    """Send the digest to `channel`, refusing any channel in `refuse`.

    `refuse` is the board channel, by id and by name. The home channel is a
    DM unless SILKWORM_HOME_CHANNEL says otherwise, and if someone points that
    at the board, the digest stays unsent rather than burying the board.
    """
    if not channel:
        return {"ok": False, "error": "no home channel (set SILKWORM_HOME_CHANNEL)"}
    if channel.lstrip("#") in {str(r).lstrip("#") for r in refuse if r}:
        return {"ok": False, "error": f"{channel} is the board channel; not posting there"}
    client.chat_postMessage(channel=channel, text=text, unfurl_links=False,
                            unfurl_media=False)
    return {"ok": True, "channel": channel}

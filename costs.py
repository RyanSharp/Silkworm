"""What work costs, read off the task records.

Every turn records `result.cost`, but nothing added it up: an implementor's
figure left out the reviewer it spawned, and nobody could say what a project
had spent this week without opening tasks.json. Fourteen days to 2026-10-01
came to $5,602 of task turns, and the board that decides what runs next showed
none of it.

A missing cost is unknown, never zero. A turn killed by a restart, or one
recorded before costs were, did cost something; adding it as $0 would make a
total read as complete when it is a floor. So every figure here travels with
whether it is complete, and is rendered with a `+` when it is not.
"""

import time

import tasks

#: The window a project's total covers.
WEEK_DAYS = 7

#: States a task can sit in without ever having run. A task still in one of
#: these with no attempts has no cost to know, and showing "unknown" against
#: every untriaged proposal would make the word mean nothing.
UNSTARTED = (tasks.PROPOSED, tasks.QUEUED, tasks.CANCELLED)


def own(rec: dict):
    """This record's recorded cost in dollars, or None if it has none."""
    cost = (rec.get("result") or {}).get("cost")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return None
    return float(cost)


def add_run(rec: dict | None, usd, now: float | None = None) -> dict:
    """`cost` and `cost_runs` for `rec` after one more run costing `usd`.

    Added to, not replaced: a task sent back for rework runs again under the
    same id, and overwriting reported only its last run. Each run is kept with
    when it ended, so a week's spend counts the money spent that week rather
    than a task's whole lifetime in whatever week someone last approved it.
    """
    rec = rec or {}
    result = rec.get("result") or {}
    now = time.time() if now is None else now
    usd = float(usd or 0.0)
    prior = own(rec) or 0.0
    runs = [list(r) for r in (result.get("cost_runs") or [])
            if isinstance(r, (list, tuple)) and len(r) == 2]
    if prior and not runs:
        # Spent before runs were dated: dated now by when the previous run
        # ended -- the last thing that happened before this run was claimed.
        runs = [[_previous_end(rec), prior]]
    return {"cost": prior + usd, "cost_runs": runs + [[now, usd]]}


def _previous_end(rec: dict):
    """When the run before the current one ended: `_ended` of the record as
    it stood before it last entered `running`. Not simply the event before
    that entry -- that is as likely an approval days after the money went."""
    events = rec.get("events") or []
    for i in range(len(events) - 1, -1, -1):
        if (events[i] or {}).get("kind") == tasks.RUNNING:
            return _ended({"events": events[:i]})
    return None


def _ended(rec: dict):
    """When a record's last run ended, for a cost recorded before
    `cost_runs`: the first event after the last time it entered `running`."""
    events = rec.get("events") or []
    for i in range(len(events) - 1, -1, -1):
        if (events[i] or {}).get("kind") == tasks.RUNNING:
            after = [e for e in events[i + 1:] if (e or {}).get("at")]
            return after[0]["at"] if after else None
    return None


def spent_since(rec: dict, cutoff: float):
    """Dollars this record spent at or after `cutoff`, or None if unknown.

    Dated per run when the runs were recorded; otherwise the whole cost is
    dated by when its last run ended, and only failing that by `updated`.
    """
    cost = own(rec)
    if cost is None:
        return None
    runs = (rec.get("result") or {}).get("cost_runs")
    if runs:
        # An undated carried-over run is counted only if the task itself is
        # this recent: unknown age is not evidence of recent spend.
        undated = rec.get("created") or 0
        return sum(float(usd or 0.0) for at, usd in runs
                   if (at if at is not None else undated) >= cutoff)
    when = _ended(rec) or rec.get("updated") or rec.get("created") or 0
    return cost if when >= cutoff else 0.0


def ran(rec: dict) -> bool:
    """Whether this record ever ran, and so ought to have a cost."""
    return (rec.get("attempts") or 0) > 0 or rec.get("state") not in UNSTARTED


def reviews_by_parent(records) -> dict:
    """{implementor id: [its reviewer records]}, built once per render."""
    out: dict = {}
    for rec in records:
        if rec.get("role") == "reviewer" and rec.get("parent"):
            out.setdefault(rec["parent"], []).append(rec)
    return out


def total(rec: dict, reviews=()) -> dict | None:
    """A task's cost including the reviews it spawned.

    {"usd": known dollars, "complete": bool}, or None when there is nothing
    to show -- a task that never ran and has no reviews.
    """
    usd, complete, any_part = 0.0, True, False
    for part in (rec, *reviews):
        cost = own(part)
        if cost is not None:
            usd += cost
            any_part = True
        elif ran(part):
            complete = False
            any_part = True
    if not any_part:
        return None
    return {"usd": round(usd, 4), "complete": complete}


def by_project(records, now: float | None = None,
               days: float = WEEK_DAYS, unfiled: str | None = None) -> dict:
    """{project: {"usd", "complete", "tasks"}} for the last `days`.

    Every record counts -- conversations cost money too -- and each is dated
    by when its money was spent (see `spent_since`), not by when it was last
    touched: approving or landing a month-old task is not a month of spend this
    week. A reviewer is filed under no project, so it is counted under its
    parent's. A record that ran in the window with no cost makes the total a
    floor. Records under no project are left out, unless `unfiled` names a
    row to count them under -- a total of everything spent needs them.
    """
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    records = list(records)
    project_of = {r.get("id"): r.get("project") or "" for r in records}
    out: dict = {}
    for rec in records:
        # Nothing touched since the cutoff spent anything in the window.
        if (rec.get("updated") or rec.get("created") or 0) < cutoff:
            continue
        proj = (rec.get("project") or project_of.get(rec.get("parent"), "")
                or unfiled or "")
        if not proj:
            continue
        usd = spent_since(rec, cutoff)
        if usd is None:
            # Unknown: only if it ran, and ran in the window.
            if not ran(rec) or (_ended(rec) or rec.get("updated") or 0) < cutoff:
                continue
        elif not usd:
            continue
        row = out.setdefault(proj, {"usd": 0.0, "complete": True, "tasks": 0})
        row["tasks"] += 1
        if usd is None:
            row["complete"] = False
        else:
            row["usd"] = round(row["usd"] + usd, 4)
    return out


def fmt(figure) -> str:
    """`$12.34`, `$12.34+` when some part is unknown, `$?` when all of it is.
    Empty for no figure at all."""
    if not figure:
        return ""
    usd, complete = figure.get("usd") or 0.0, figure.get("complete", True)
    if not usd and not complete:
        return "$?"
    text = f"${usd:,.0f}" if usd >= 100 else f"${usd:.2f}"
    return text + ("" if complete else "+")


def week_line(rows: dict, limit: int = 8) -> str:
    """Per-project spend over the window, biggest first, on one line."""
    if not rows:
        return ""
    ranked = sorted(rows.items(), key=lambda kv: -kv[1]["usd"])
    parts = [f"{p} {fmt(r)}" for p, r in ranked[:limit]]
    if len(ranked) > limit:
        parts.append(f"+{len(ranked) - limit} more")
    return f"last {WEEK_DAYS}d: " + " · ".join(parts)

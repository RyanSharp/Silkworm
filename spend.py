"""What the bot has spent, for the dashboard's spend view.

Per project per week for the last twelve weeks, this week against the four
before it, the month's most expensive tasks and a split by role. Pure over
the task records: every figure comes from costs.by_project_between, the same
windowing the daily digest and the panel's week line use, so for one window
the three cannot disagree. Spend is dated by when it was spent (each run's
own end, costs.spent_between), never by when a task was last touched, and a
resumed turn's cost is already its own increase (turncost.py).

A week is the seven days ending now, then the seven before that, and so on:
"this week" is then a whole week, and comparing it with the four before it
compares like with like (a calendar week three days in would always read as
a drop). The month is the calendar month.

Every figure is an API list-price equivalent (costs.NOTE): the bot runs on a
subscription. A record that ran with no recorded cost is unknown, not zero:
its figure carries `complete: False` and is drawn with a `+` (or `$?`).
"""

import time
from datetime import datetime

import costs

#: Weeks drawn, and how many before this one its trend is measured against.
WEEKS = 12
AVG_WEEKS = 4
#: The month's most expensive tasks listed.
TOP = 10
#: Projects drawn in their own colour (--series-1..5); past that, the
#: biggest five keep theirs and the rest are folded into "other" (--series-0).
SERIES = 5
OTHER = "other"
#: The roles named first in the split, in this order; any other role that
#: spent anything follows them.
ROLE_ORDER = ("implementor", "reviewer", "researcher", "operator", "assistant")
WEEK_S = 7 * 86400


def weeks(now: float, n: int = WEEKS) -> list:
    """[(start, end)] of the last `n` seven-day windows, oldest first, the
    last one ending at `now`."""
    return [(now - (n - i) * WEEK_S, now - (n - i - 1) * WEEK_S) for i in range(n)]


def month_start(now: float) -> float:
    """Local midnight on the first of `now`'s month."""
    d = datetime.fromtimestamp(now)
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def _sum(figs) -> dict:
    figs = list(figs)
    return {"usd": round(sum(f["usd"] for f in figs), 4),
            "complete": all(f["complete"] for f in figs)}


def _role(rec: dict, _proj=None) -> str:
    return rec.get("role") or "assistant"


def series(week_rows: list, limit: int = SERIES) -> list:
    """The projects given their own colour: the biggest `limit` spenders over
    the weeks shown, the rest folded into one "other". Slots go
    alphabetically among them, so a project's colour does not move when the
    ranking under it does."""
    totals: dict = {}
    for row in week_rows:
        for p, f in row.items():
            totals[p] = totals.get(p, 0.0) + f["usd"]
    ranked = sorted(totals, key=lambda p: (-totals[p], p))
    keep = ranked[:limit]
    out = [{"key": p, "slot": i + 1} for i, p in enumerate(sorted(keep))]
    if len(ranked) > len(keep):
        out.append({"key": OTHER, "slot": 0, "of": sorted(ranked[len(keep):])})
    return out


def top_tasks(records, start: float, end: float = float("inf"), n: int = TOP,
              unfiled: str = "") -> dict:
    """The `n` records that spent most in [start, end), each with the reviews
    it spawned counted in (as costs.total does), and how many records ran in
    the window with no cost recorded -- those cannot be ranked, only counted."""
    records = list(records)
    by_id = {r.get("id"): r for r in records}
    parents = {r.get("id"): r.get("project") or "" for r in records}
    # A review counts in the task it reviewed -- the first one up its chain
    # that is not itself a review -- and is not listed apart.
    def root(rec):
        seen = set()
        while (rec.get("role") == "reviewer" and rec.get("parent") in by_id
               and rec.get("id") not in seen):
            seen.add(rec.get("id"))
            rec = by_id[rec["parent"]]
        return rec
    reviews: dict = {}
    for rec in records:
        top = root(rec)
        if top is not rec:
            reviews.setdefault(top.get("id"), []).append(rec)
    rows, unknown = [], 0
    for rec in records:
        if root(rec) is not rec:
            continue            # counted in the task it reviewed
        usd, complete = 0.0, True
        for part in (rec, *reviews.get(rec.get("id"), ())):
            got = costs.spent_between(part, start, end)
            if got is None:
                if costs.unknown_between(part, start, end):
                    complete = False
            else:
                usd += got
        if not usd:
            if not complete:
                unknown += 1
            continue
        rows.append({"id": str(rec.get("id") or ""), "title": str(rec.get("title") or rec.get("id") or ""),
                     "project": str(costs.project_of(rec, parents, unfiled)), "role": str(_role(rec)),
                     "state": str(rec.get("state") or ""),
                     "cost": {"usd": round(usd, 4), "complete": complete}})
    rows.sort(key=lambda r: (-r["cost"]["usd"], r["id"]))
    return {"tasks": rows[:n], "unknown": unknown}


def roles(records, start: float, end: float = float("inf"), unfiled: str = "") -> list:
    """[{"role", "usd", "complete", "tasks"}] for [start, end): the named
    roles first, in ROLE_ORDER, then any other that spent."""
    rows = costs.by_project_between(records, start, end, unfiled=unfiled or "-", key=_role)
    order = [r for r in ROLE_ORDER if r in rows] + sorted(r for r in rows if r not in ROLE_ORDER)
    return [{"role": r, **rows[r]} for r in order]


def view(records, now: float | None = None, unfiled: str = "") -> dict:
    """The spend view's whole answer (see the module docstring)."""
    now = time.time() if now is None else now
    records = list(records)
    spans = weeks(now)
    # The last window runs on past `now`, as the digest's and the week line's
    # do (costs.by_project): a run stamped a moment ahead is still this week.
    inf = float("inf")
    rows = [costs.by_project_between(records, s, e if i < len(spans) - 1 else inf, unfiled=unfiled)
            for i, (s, e) in enumerate(spans)]
    wk = [{"start": s, "end": e, "projects": r, "total": _sum(r.values())}
          for (s, e), r in zip(spans, rows)]
    this = wk[-1]["total"]
    before = [w["total"] for w in wk[-1 - AVG_WEEKS:-1]]
    avg = {"usd": round(sum(f["usd"] for f in before) / len(before), 4),
           "complete": all(f["complete"] for f in before)}
    trend = (round((this["usd"] - avg["usd"]) / avg["usd"], 4) if avg["usd"] else None)
    mstart = month_start(now)
    return {"now": now, "note": costs.NOTE, "weeks": wk, "series": series(rows),
            "this_week": this, "avg": avg, "avg_weeks": AVG_WEEKS, "trend": trend,
            "month_start": mstart, "top": top_tasks(records, mstart, inf, unfiled=unfiled),
            "roles": {"week": roles(records, spans[-1][0], inf, unfiled),
                      "all": roles(records, spans[0][0], inf, unfiled)}}

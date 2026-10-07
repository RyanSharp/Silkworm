"""Per-project boards and the projects overview, for the dashboard.

The task panel answers "what needs me". It does not answer "where is this
project": what is queued behind what, what is in review, what landed this week
and what it all cost. That is spread across tasks.json, git and the release
configuration, and these functions put it in one place.

Read-only, and pure over the records it is handed: the bot's /tasks route
supplies the store's records and the slow inputs -- the unmerged-branch survey
and each project's release plan, both of which ask git -- through `TTLCache`,
so a board polled every few seconds does not walk every repository each time.
Nothing here moves a task. The actions a card offers go through the same
handle_tasks branches the task panel already uses.
"""

import threading
import time
from pathlib import Path

import branches
import costs
import projects as projects_mod
import releases
import slacklinks
import tasks

#: The board's columns, in order.
COLUMNS = ("backlog", "running", "review", "needs", "done")

#: How far back the Done column reaches.
DONE_DAYS = 14

#: The lane for work filed under no project. Not a valid slug (slugify never
#: produces a leading underscore), so it cannot collide with a real project.
UNFILED = "__unfiled__"

#: How long a survey or a release plan is reused. Both only change when work
#: lands or a branch is dropped, and the page polls every five seconds.
CACHE_TTL_S = 60

#: Characters of goal a card carries for the search to match against. The
#: detail view fetches the whole record.
GOAL_SNIPPET = 240


class TTLCache:
    """key -> value, recomputed when older than `ttl` seconds.

    A failure is not cached: the next caller tries again rather than being
    handed a stale error for a minute. Computation happens outside the lock,
    so one slow repository does not stall reads of another key.
    """

    def __init__(self, ttl: float = CACHE_TTL_S, clock=time.time):
        self.ttl = ttl
        self._clock = clock
        self._data: dict = {}
        self._lock = threading.Lock()

    def get(self, key, compute):
        now = self._clock()
        with self._lock:
            hit = self._data.get(key)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        value = compute()
        with self._lock:
            self._data[key] = (now, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


def survey_key(records) -> tuple:
    """A cache key for the branch survey: the finished records' ids and last
    writes. A running task rewriting its events does not change it; a task
    finishing, landing or being dropped does."""
    return tuple(sorted((r.get("id") or "", r.get("updated") or 0) for r in records
                        if r.get("state") not in branches.IN_FLIGHT))


# --- columns -------------------------------------------------------------------

def _reviewer_ids(records) -> set:
    return {r.get("id") for r in records if r.get("role") == "reviewer"}


def finished_at(rec: dict) -> float:
    """When a finished task finished: its landing if it has one, else the
    event that made it done, else its last write."""
    landing = (rec.get("result") or {}).get("landing") or {}
    if landing.get("at"):
        return landing["at"]
    for e in reversed(rec.get("events") or []):
        if (e or {}).get("kind") == tasks.DONE and e.get("at"):
            return e["at"]
    return rec.get("updated") or rec.get("created") or 0


def column(rec: dict, reviewers: set, now: float, done_days: float = DONE_DAYS):
    """Which column `rec` belongs in, or None for not on the board.

    Reviewer tasks are never cards of their own: they are filed under no
    project, so they would all land in the unfiled lane, and what they found
    is shown on the task they reviewed. A task `blocked` on one of them is
    "in review"; one blocked on anything else (a quota retry, a scheduled
    wake-up) is waiting to go round again, which is backlog.
    """
    if rec.get("role") == "reviewer":
        return None
    state = rec.get("state")
    if state in (tasks.PROPOSED, tasks.QUEUED):
        return "backlog"
    if state == tasks.RUNNING:
        return "running"
    if state == tasks.BLOCKED:
        return ("review" if any(b in reviewers for b in rec.get("blocked_on") or [])
                else "backlog")
    if state in (tasks.AWAITING_APPROVAL, tasks.NEEDS_INPUT, tasks.FAILED):
        return "needs"
    if state == tasks.DONE:
        return "done" if finished_at(rec) >= now - done_days * 86400 else None
    return None                                   # cancelled


def review_summary(rec: dict) -> dict | None:
    rv = (rec.get("result") or {}).get("review")
    if not rv:
        return None
    return {"ok": bool(rv.get("ok")), "summary": (rv.get("summary") or "")[:300],
            "findings": len(rv.get("findings") or []),
            "followups": len(rv.get("followups") or [])}


def landing_summary(rec: dict) -> dict | None:
    """The landing record, cut to what a card shows. `landed` on its own is
    the older record of a success, written before `landing` existed."""
    result = rec.get("result") or {}
    l = result.get("landing")
    if not l:
        return {"landed": True, "head": result["landed"]} if result.get("landed") else None
    return {k: l[k] for k in ("landed", "stage", "head", "branch", "eligible",
                              "reworked_by", "at") if k in l} | {
        "detail": str(l.get("detail") or "")[-300:]}


def card(rec: dict, reviews=(), unmerged: dict | None = None) -> dict:
    """What a board card needs: small, because the page polls the board."""
    goal = (rec.get("goal") or "").strip()
    return {
        "id": rec.get("id") or "",
        "title": rec.get("title") or goal[:60],
        "goal": goal[:GOAL_SNIPPET],
        "role": rec.get("role") or "",
        "state": rec.get("state") or "",
        "project": rec.get("project") or "",
        "source": rec.get("source") or "",
        "attempts": rec.get("attempts") or 0,
        "created": rec.get("created") or 0,
        "updated": rec.get("updated") or 0,
        "thread": rec.get("thread") or "",
        "cost_total": costs.total(rec, reviews),
        "review": review_summary(rec),
        "landing": landing_summary(rec),
        "finished": finished_at(rec) if rec.get("state") == tasks.DONE else None,
        # From the cached survey, never asked of git here.
        "unmerged": unmerged,
        # Why a failed task stopped -- the event that put it there.
        "why": _why(rec) if rec.get("state") == tasks.FAILED else "",
    }


def _why(rec: dict) -> str:
    ev = rec.get("events") or []
    e = ([x for x in ev if (x or {}).get("kind") == rec.get("state")][-1:]
         or ev[-1:] or [{}])[0]
    return str((e or {}).get("detail") or "")[:300]


def matches(rec: dict, role: str = "", state: str = "", q: str = "") -> bool:
    if role and (rec.get("role") or "") != role:
        return False
    if state and (rec.get("state") or "") != state:
        return False
    q = (q or "").strip().lower()
    if q and q not in f"{rec.get('id') or ''}\n{rec.get('title') or ''}\n" \
                      f"{rec.get('goal') or ''}".lower():
        return False
    return True


def in_project(rec: dict, project: str) -> bool:
    """`project` is a slug, UNFILED for work under none, or "" for all."""
    if not project:
        return True
    have = rec.get("project") or ""
    return not have if project == UNFILED else have == project


def board(records, project: str = "", *, role: str = "", state: str = "",
          q: str = "", unmerged_rows=(), now: float | None = None,
          done_days: float = DONE_DAYS) -> dict:
    """{columns: {name: [card]}, counts: {column: n}, roles: [...]}.

    Backlog and the open columns are oldest first -- the queue runs in that
    order, and the oldest open item is the one most likely forgotten. Done is
    newest first: "what landed lately" reads from the top.
    """
    now = time.time() if now is None else now
    records = list(records)
    reviewers = _reviewer_ids(records)
    reviews = costs.reviews_by_parent(records)
    by_id = {r.get("id"): r for r in unmerged_rows}
    cols: dict = {c: [] for c in COLUMNS}
    roles_seen = set()
    for rec in records:
        if not in_project(rec, project):
            continue
        col = column(rec, reviewers, now, done_days)
        if col is None:
            continue
        roles_seen.add(rec.get("role") or "")
        if not matches(rec, role, state, q):
            continue
        tid = rec.get("id")
        row = by_id.get(tid)
        cols[col].append(card(rec, reviews.get(tid, ()),
                              {"commits": row.get("commits"), "branch": row.get("branch"),
                               "local": row.get("local"), "remote": row.get("remote")}
                              if row else None))
    for name, items in cols.items():
        if name == "done":
            items.sort(key=lambda c: -(c["finished"] or 0))
        else:
            items.sort(key=lambda c: c["created"])
    return {"columns": cols, "counts": {c: len(v) for c, v in cols.items()},
            "roles": sorted(r for r in roles_seen if r)}


def detail(rec: dict, records=()) -> dict:
    """One task in full, for the card's detail view: the whole goal, its
    events, the review and the landing as recorded, and its reviewers."""
    records = list(records)
    reviews = [r for r in records if r.get("role") == "reviewer"
               and r.get("parent") == rec.get("id")]
    out = dict(rec)
    out["cost_total"] = costs.total(rec, reviews)
    out["thread_link"] = slacklinks.for_key(rec.get("thread") or "")
    out["reviews"] = [{"id": r.get("id"), "state": r.get("state"),
                       "created": r.get("created") or 0} for r in reviews]
    # The reply text can run to tens of kilobytes; the detail shows its tail.
    result = dict(out.get("result") or {})
    if isinstance(result.get("text"), str) and len(result["text"]) > 4000:
        result["text"] = "…" + result["text"][-4000:]
    out["result"] = result
    return out


# --- projects overview ---------------------------------------------------------

def release_status(repo) -> dict | None:
    """What is ready to release per target, or None for a project that has no
    release.toml. `releases.plan` and `pending` only read git; nothing here
    runs a target's preview or ship command."""
    if not repo or not (Path(repo) / releases.CONFIG).exists():
        return None
    try:
        targets = releases.load(repo)
        steps = {s["target"]: s for s in releases.plan(repo)}
    except Exception as e:                       # ReleaseError, git, toml
        return {"error": str(e)[:300], "targets": []}
    out = []
    for name in releases.order(targets, list(targets)):
        step = steps.get(name)
        out.append({"target": name, "ship": targets[name]["ship"],
                    "commits": len(step["commits"]) if step else 0,
                    "version": step["version"] if step else None,
                    "tag": step["tag"] if step else None})
    return {"targets": out}


def _last_landed(recs) -> dict | None:
    best = None
    for r in recs:
        result = r.get("result") or {}
        head = result.get("landed") or ((result.get("landing") or {}).get("head")
                                        if (result.get("landing") or {}).get("landed") else None)
        if not head:
            continue
        at = finished_at(r)
        if best is None or at > best["at"]:
            best = {"id": r.get("id"), "title": r.get("title") or "", "head": head[:8],
                    "at": at}
    return best


def _summary(slug: str, title: str, recs: list, spend: dict, unmerged_rows) -> dict:
    counts: dict = {}
    for r in recs:
        if r.get("role") == "reviewer":
            continue
        counts[r.get("state") or "?"] = counts.get(r.get("state") or "?", 0) + 1
    running = [{"id": r.get("id"), "title": r.get("title") or "", "role": r.get("role"),
                "thread": r.get("thread") or ""}
               for r in recs if r.get("state") == tasks.RUNNING
               and r.get("role") != "reviewer"]
    branches_ = [u for u in unmerged_rows if (u.get("project") or "") == (
        "" if slug == UNFILED else slug)]
    return {
        "slug": slug, "title": title, "counts": counts,
        "needs": sum(counts.get(s, 0) for s in tasks.NEEDS_ATTENTION),
        "running": running,
        "last_landed": _last_landed(recs),
        "unmerged": {"branches": len(branches_),
                     # None is git declining to count: unknown, not zero.
                     "commits": sum(u.get("commits") or 0 for u in branches_),
                     "unknown": sum(u.get("commits") is None for u in branches_)},
        "cost_week": spend.get(slug),
    }


def overview(project_records, records, *, unmerged_rows=(), releases_by_slug=None,
             now: float | None = None) -> dict:
    """One card per active project, plus the unfiled lane's.

    `releases_by_slug` is {slug: release_status(...)}, supplied already
    computed (and cached) by the caller.
    """
    now = time.time() if now is None else now
    records = list(records)
    releases_by_slug = releases_by_slug or {}
    spend = costs.by_project(records, now=now, unfiled=UNFILED)
    by_slug: dict = {}
    parents = {r.get("id"): r.get("project") or "" for r in records}
    for r in records:
        # A reviewer is filed under no project; its cost already counts
        # towards its parent's, and so does it here.
        slug = r.get("project") or (parents.get(r.get("parent"), "")
                                    if r.get("role") == "reviewer" else "")
        by_slug.setdefault(slug or UNFILED, []).append(r)
    out = []
    for p in project_records:
        if p.get("archived"):
            continue
        slug = p["slug"]
        row = _summary(slug, p.get("title") or slug, by_slug.get(slug, []), spend,
                       unmerged_rows)
        row["readiness"] = {
            "test_cmd": (p.get("test_cmd") or "").strip(),
            "auto_merge": bool(p.get("auto_merge")),
            "publish": bool(p.get("publish")),
            "unready": projects_mod.unready(p),
        }
        row["cwd"] = (p.get("scope") or {}).get("cwd") or ""
        row["release"] = releases_by_slug.get(slug)
        out.append(row)
    out.sort(key=lambda r: (-r["needs"], -len(r["running"]), r["title"].lower()))
    unfiled = _summary(UNFILED, "Unfiled", by_slug.get(UNFILED, []), spend, unmerged_rows)
    return {"projects": out, "unfiled": unfiled, "note": costs.NOTE}

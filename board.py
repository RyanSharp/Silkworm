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

import basered
import health
import branches
import inbox
import costs
import merge
import projects as projects_mod
import releases
import research
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

#: What the board's "Needs you" column holds, and so what a project card's
#: count means. Not tasks.NEEDS_ATTENTION: that includes `proposed`, which the
#: board keeps in Backlog, and the card must agree with the column it opens.
NEEDS_YOU = (tasks.AWAITING_APPROVAL, tasks.NEEDS_INPUT, tasks.FAILED)

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
            # Expired entries go as new ones arrive: the survey's key changes
            # with every finished task's write, so without this the cache
            # would keep one survey per write for the life of the bot.
            self._data = {k: v for k, v in self._data.items() if now - v[0] < self.ttl}
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
    """Reviewers still open. `blocked_on` outlives the review it named (see
    TaskStore._waiting_on_open), so a finished reviewer there is history, not
    a wait: a reviewed task later blocked on a quota retry is backlog."""
    return {r.get("id") for r in records if r.get("role") == "reviewer"
            and r.get("state") not in tasks.TERMINAL}


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
    if state in NEEDS_YOU:
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


def findings(rec: dict, kids=()) -> dict | None:
    """A research task's next steps: the children it filed, and the ones the
    cap or dedup refused. None for anything that is not research."""
    if not research.is_research(rec):
        return None
    kids, held = list(kids or ()), list(rec.get("next_steps_held") or [])
    return {"children": kids, "held": held, "line": research.line(kids, held)}


def card(rec: dict, reviews=(), unmerged: dict | None = None, kids=(),
         by_id: dict | None = None) -> dict:
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
        "priority": rec.get("priority") or 0,
        "driver": rec.get("driver") or "",
        "created": rec.get("created") or 0,
        "updated": rec.get("updated") or 0,
        "thread": rec.get("thread") or "",
        # One way to link a thread (slacklinks), for Discuss and Thread alike.
        "thread_link": slacklinks.for_key(rec.get("thread") or ""),
        "cost_total": costs.total(rec, reviews),
        "review": review_summary(rec),
        "landing": landing_summary(rec),
        "finished": finished_at(rec) if rec.get("state") == tasks.DONE else None,
        # From the cached survey, never asked of git here.
        "unmerged": unmerged,
        # Why a failed task stopped -- the event that put it there.
        "why": _why(rec) if rec.get("state") == tasks.FAILED else "",
        # The step its run left for you, which keeps it in Needs you.
        "needs_user": rec.get("needs_user") or None,
        # Research: the next steps it filed, and any it could not.
        "research": findings(rec, kids),
        # Waiting on you with findings: what the page offers Close and Dig
        # deeper on, decided here so the page names no role of its own.
        "findings": research.has_findings(rec),
        # Which round of them: Close and Dig deeper send it back, so a click
        # drawn before a dig cannot act on the findings that dig produced.
        "round": research.round_of(rec),
        # Why Retry is left off this card, or "" (tasks.no_retry).
        "no_retry": tasks.no_retry(rec, by_id or {}),
    }


def _why(rec: dict, limit: int | None = 300) -> str:
    ev = rec.get("events") or []
    e = ([x for x in ev if (x or {}).get("kind") == rec.get("state")][-1:]
         or ev[-1:] or [{}])[0]
    return str((e or {}).get("detail") or "")[:limit]


#: What a task panel row reads of a task's result: the review and the landing
#: are drawn on the row itself. The reply text is behind the row's disclosure.
ROW_RESULT = ("review", "landing", "landed")

#: The rest of what a task panel row reads, copied as they are.
ROW_FIELDS = ("id", "title", "state", "role", "project", "source", "attempts",
              "created", "thread", "thread_link", "cost_total", "needs_user",
              "findings", "round", "research", "no_retry", "lens")


def panel_row(t: dict) -> dict:
    """A task panel row: a task as handle_tasks' `list` builds it (costs and
    research added), cut to what the row draws.

    The whole goal and the reply text are most of tasks.json -- 6 MB for 1,500
    tasks -- and the panel showed them only behind a closed disclosure, while
    the badge polled the lot every five seconds. The row says instead whether
    there is anything behind its title (`more`) and, for a failure, why it
    stopped (`why`, uncut, as the row showed it); opening it asks for the task
    itself (the `task` action, with `full`).
    """
    row = {k: t.get(k) for k in ROW_FIELDS}
    result = t.get("result") or {}
    row["result"] = {k: result[k] for k in ROW_RESULT if k in result}
    goal = (t.get("goal") or "").strip()
    row["more"] = bool((goal and goal != (t.get("title") or "").strip())
                       or (t.get("findings") and result.get("text")))
    row["why"] = _why(t, None) if t.get("state") == tasks.FAILED else ""
    return row


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


def unmerged_of(row: dict | None) -> dict | None:
    """A survey row cut to what a card (and its detail) shows; one shape for
    both, so the detail panel offers Land and Drop exactly when the card does."""
    if not row:
        return None
    return {"commits": row.get("commits"), "branch": row.get("branch"),
            "local": row.get("local"), "remote": row.get("remote"),
            # Why it is still here (branches.REASONS), and whether only a
            # person can move it on -- Refile then stands in for Land.
            "reason": row.get("reason") or "", "why": row.get("why") or "",
            "stranded": bool(row.get("stranded"))}


def board(records, project: str = "", *, role: str = "", state: str = "",
          q: str = "", unmerged_rows=(), now: float | None = None,
          done_days: float = DONE_DAYS, turns: bool = False) -> dict:
    """{columns: {name: [card]}, counts: {column: n}, roles: [...]}.

    Conversation turns are left off unless `turns` (tasks.on_board): they are
    shown on their thread, and a running one as its thread's indicator. One
    waiting on you -- a step handed to you, or a turn that failed -- stays.

    Backlog and the open columns are oldest first -- the queue runs in that
    order, and the oldest open item is the one most likely forgotten. Done is
    newest first: "what landed lately" reads from the top.
    """
    now = time.time() if now is None else now
    records = list(records)
    reviewers = _reviewer_ids(records)
    reviews = costs.reviews_by_parent(records)
    kids = research.children_by_parent(records)
    by_id = {r.get("id"): r for r in unmerged_rows}
    tasks_by_id = {r.get("id"): r for r in records}
    cols: dict = {c: [] for c in COLUMNS}
    roles_seen = set()
    for rec in records:
        if not in_project(rec, project) or not tasks.on_board(rec, turns):
            continue
        col = column(rec, reviewers, now, done_days)
        if col is None:
            continue
        roles_seen.add(rec.get("role") or "")
        if not matches(rec, role, state, q):
            continue
        tid = rec.get("id")
        cols[col].append(card(rec, reviews.get(tid, ()), unmerged_of(by_id.get(tid)),
                              kids.get(tid, ()), tasks_by_id))
    for name, items in cols.items():
        if name == "done":
            items.sort(key=lambda c: -(c["finished"] or 0))
        elif name == "backlog":
            items.sort(key=backlog_order)
        else:
            items.sort(key=lambda c: c["created"])
    # Where each queued card stands in the runner's queue, counted over the
    # whole queue rather than this board, so "next" on one project's board
    # is not a claim about a queue that also holds other projects' work.
    queue = sorted((r for r in records if _in_queue(r)), key=tasks.claim_order)
    pos = {r.get("id"): i + 1 for i, r in enumerate(queue)}
    for c in cols["backlog"]:
        c["queue_pos"] = pos.get(c["id"])
    return {"columns": cols, "counts": {c: len(v) for c, v in cols.items()},
            "roles": sorted(r for r in roles_seen if r)}


def _in_queue(rec: dict) -> bool:
    """Whether the task lane's claim will consider this task at all."""
    return (rec.get("state") == tasks.QUEUED and rec.get("driver") == "queue"
            and rec.get("role") != "reviewer")


def backlog_order(c: dict) -> tuple:
    """Backlog top to bottom: the queue in the order it will be claimed
    (tasks.claim_order), then what is waiting to go round again, then the
    proposals nothing runs until you accept them."""
    rank = {tasks.QUEUED: 0, tasks.BLOCKED: 1, tasks.PROPOSED: 2}.get(c.get("state"), 3)
    return (rank,) + tasks.claim_order(c)


def detail(rec: dict, records=(), unmerged_rows=(), full: bool = False) -> dict:
    """One task in full, for the card's detail view: the whole goal, its
    events, the review and the landing as recorded, and its reviewers --
    plus the same cached survey row its card carries. `full` keeps the whole
    reply text rather than its tail: the task panel's rows showed all of it
    before they fetched it on opening."""
    records = list(records)
    reviews = [r for r in records if r.get("role") == "reviewer"
               and r.get("parent") == rec.get("id")]
    out = dict(rec)
    out["cost_total"] = costs.total(rec, reviews)
    out["thread_link"] = slacklinks.for_key(rec.get("thread") or "")
    out["research"] = findings(rec, research.children(rec.get("id"), records))
    out["findings"] = research.has_findings(rec)
    out["round"] = research.round_of(rec)
    out["no_retry"] = tasks.no_retry(rec, {r.get("id"): r for r in records})
    out["unmerged"] = unmerged_of(next(
        (r for r in unmerged_rows if r.get("id") == rec.get("id")), None))
    out["reviews"] = [{"id": r.get("id"), "state": r.get("state"),
                       "created": r.get("created") or 0} for r in reviews]
    # The reply text can run to tens of kilobytes; the detail shows its tail.
    result = dict(out.get("result") or {})
    if not full and isinstance(result.get("text"), str) and len(result["text"]) > 4000:
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


def _summary(slug: str, title: str, recs: list, spend: dict, unmerged_rows,
             turns: bool = False) -> dict:
    # Counted and listed as the board is (tasks.on_board); landings and spend
    # are read off every record.
    shown = [r for r in recs if tasks.on_board(r, turns)]
    counts: dict = {}
    for r in shown:
        if r.get("role") == "reviewer":
            continue
        counts[r.get("state") or "?"] = counts.get(r.get("state") or "?", 0) + 1
    running = [{"id": r.get("id"), "title": r.get("title") or "", "role": r.get("role"),
                "thread": r.get("thread") or ""}
               for r in shown if r.get("state") == tasks.RUNNING
               and r.get("role") != "reviewer"]
    branches_ = [u for u in unmerged_rows if (u.get("project") or "") == (
        "" if slug == UNFILED else slug)]
    return {
        "slug": slug, "title": title, "counts": counts,
        "needs": sum(counts.get(s, 0) for s in NEEDS_YOU),
        "running": running,
        "last_landed": _last_landed(recs),
        "unmerged": {"branches": len(branches_),
                     # None is git declining to count: unknown, not zero.
                     "commits": sum(u.get("commits") or 0 for u in branches_),
                     "unknown": sum(u.get("commits") is None for u in branches_)},
        "cost_week": spend.get(slug),
    }


def readiness(p: dict) -> dict:
    """Whether a project can take unsupervised work, and its landing switches."""
    return {
        "test_cmd": (p.get("test_cmd") or "").strip(),
        "auto_merge": bool(p.get("auto_merge")),
        "publish": bool(p.get("publish")),
        "unready": projects_mod.unready(p),
    }


def failing_lines(p: dict) -> dict:
    """{main_failing, checkout_failing}: the project's one line while its base
    fails its own tests, and while its own checkout fails tests the base
    passes elsewhere; None for each that is not so."""
    slug = p.get("slug") or ""
    red = p.get("main_failing") or None
    bad = p.get("checkout_failing") or None
    return {
        "main_failing": ({"line": basered.line(slug, red), "sha": red.get("sha"),
                          "tests": list(red.get("tests") or []),
                          "since": red.get("since")} if red else None),
        "checkout_failing": ({"line": basered.checkout_line(slug, bad),
                              "sha": bad.get("sha"),
                              "tests": list(bad.get("tests") or []),
                              "since": bad.get("since")} if bad else None),
    }


def overview(project_records, records, *, unmerged_rows=(), releases_by_slug=None,
             now: float | None = None, health_rows=(), turns: bool = False) -> dict:
    """One card per active project, plus the unfiled lane's.

    `releases_by_slug` is {slug: release_status(...)}, supplied already
    computed (and cached) by the caller. `health_rows` is the health store's
    records (health.py): a project gets one line while a check fails or warns.
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
    health_by = health.by_project(health_rows or ())
    out = []
    for p in project_records:
        if p.get("archived"):
            continue
        slug = p["slug"]
        row = _summary(slug, p.get("title") or slug, by_slug.get(slug, []), spend,
                       unmerged_rows, turns)
        row["readiness"] = readiness(p)
        row["cwd"] = (p.get("scope") or {}).get("cwd") or ""
        row.update(failing_lines(p))
        row["release"] = releases_by_slug.get(slug)
        row["health"] = health.board_line(health_by.get(slug, ()))
        out.append(row)
    out.sort(key=lambda r: (-r["needs"], -len(r["running"]), r["title"].lower()))
    unfiled = _summary(UNFILED, "Unfiled", by_slug.get(UNFILED, []), spend, unmerged_rows,
                       turns)
    # Archived projects are off the overview but not gone: listed, small, so
    # they can be brought back from the same page they were archived on.
    archived = sorted(({"slug": p["slug"], "title": p.get("title") or p["slug"],
                        "tasks": sum(tasks.on_board(r, turns)
                                     for r in by_slug.get(p["slug"], []))}
                       for p in project_records if p.get("archived")),
                      key=lambda r: r["title"].lower())
    return {"projects": out, "unfiled": unfiled, "archived": archived, "note": costs.NOTE}


# --- one project's page ------------------------------------------------------------

#: How far back the page's landings and releases reach.
PAGE_DAYS = 14

#: The health checks that are an app's store state, drawn as their own section.
STORE_CHECKS = ("app-store", "testflight")

#: Evidence lines a health row carries to the page.
PAGE_EVIDENCE = 4


def _health_row(rec: dict, action_of=None) -> dict:
    """One stored health result, cut to what the page draws. Named fields
    only: the store's records carry pointers and probe caches the page has
    no business with, and nothing here is passed through whole."""
    kind, words = ("", "")
    if action_of and rec.get("status") != health.OK:
        try:
            kind, words = action_of(rec) or ("", "")
        except Exception:
            kind, words = "", ""
    last = rec.get("last_known") or {}
    return {
        "check": str(rec.get("check") or ""),
        "status": str(rec.get("status") or health.UNKNOWN),
        "summary": str(rec.get("summary") or "")[:300],
        "evidence": [str(e)[:300] for e in (rec.get("evidence") or [])[:PAGE_EVIDENCE]],
        "link": str(rec.get("link") or ""),
        "since": rec.get("last_changed") or None,
        "checked_at": rec.get("checked_at") or None,
        "was": str(rec.get("was") or ""),
        "last_known": str(last.get("status") or "") if rec.get("status") == health.UNKNOWN else "",
        "action_kind": kind or "",
        "action": " ".join(str(words or "").split())[:300],
    }


def _task_row(rec: dict, queue_pos: dict) -> dict:
    return {"id": rec.get("id"), "title": rec.get("title") or rec.get("id") or "",
            "role": rec.get("role") or "", "state": rec.get("state") or "",
            "created": rec.get("created") or 0, "attempts": rec.get("attempts") or 0,
            "priority": bool(rec.get("priority")), "thread": rec.get("thread") or "",
            "queue_pos": queue_pos.get(rec.get("id"))}


def project_page(prec: dict, records, *, unmerged_rows=(), health_rows=(), release=None,
                 release_tags=(), action_of=None, now: float | None = None,
                 days: float = PAGE_DAYS, registry: dict | None = None,
                 operator_thread: str = "") -> dict:
    """Everything about one project on one page, read-only.

    The header (its readiness and the base-red lines), its latest health
    checks with what is being done or decided about each, its App Store and
    TestFlight state where it is an app (those checks' stored results: no
    probe is run for the page), the landings and release tags of the last
    `days`, what waits on you (inbox.py's items for it), what is queued and
    running, and its unmerged branches. `action_of(health_rec)` is
    health.action bound to the task store; `release_tags` is
    releases.tags_since's answer for its repository. `health_rows` None is
    "the store could not be read", said as such (`health_unread`), never
    drawn as a project with no checks.
    """
    health_unread = health_rows is None
    now = time.time() if now is None else now
    slug = prec["slug"]
    since = now - days * 86400
    records = list(records)
    by_id = {r.get("id"): r for r in records}

    def mine(r):
        # A reviewer is filed under no project; it belongs to its parent's.
        own = r.get("project") or (by_id.get(r.get("parent") or "", {}).get("project")
                                   if r.get("role") == "reviewer" else "")
        return (own or "") == slug
    recs = [r for r in records if mine(r)]

    rows = [r for r in (health_rows or ()) if (r.get("project") or "") == slug]
    rows.sort(key=lambda r: (health.SEVERITY.get(r.get("status"), 9), r.get("check") or ""))
    checks = [_health_row(r, action_of) for r in rows]
    store = [c for c in checks if c["check"] in STORE_CHECKS]
    store.sort(key=lambda c: STORE_CHECKS.index(c["check"]))

    landed = []
    for r in recs:
        result = r.get("result") or {}
        l = result.get("landing") or {}
        head = result.get("landed") or (l.get("head") if l.get("landed") else None)
        if not head:
            continue
        at = finished_at(r)
        if at < since:
            continue
        landed.append({"id": r.get("id"), "title": r.get("title") or "", "head": str(head)[:8],
                       "at": at, "base": str(l.get("base") or ""),
                       "published": l.get("published")})
    landed.sort(key=lambda x: -x["at"])

    tags = sorted(({"tag": str(t.get("tag") or ""), "at": t.get("at") or 0}
                   for t in (release_tags or ()) if (t.get("at") or 0) >= since),
                  key=lambda t: -t["at"])

    inbox_ = inbox.build(records, unmerged_rows=unmerged_rows, health_rows=rows,
                         operator_thread=operator_thread,
                         project_titles={slug: prec.get("title") or slug}, registry=registry)
    needs = next((g["items"] for g in inbox_["groups"] if g["project"] == slug), [])

    queue = sorted((r for r in records if _in_queue(r)), key=tasks.claim_order)
    pos = {r.get("id"): i + 1 for i, r in enumerate(queue)}
    shown = [r for r in recs if tasks.on_board(r)]
    queued = sorted((_task_row(r, pos) for r in shown if r.get("state") == tasks.QUEUED
                     and r.get("role") != "reviewer"),
                    key=lambda t: (t["queue_pos"] or 10 ** 9, t["created"]))
    running = sorted((_task_row(r, pos) for r in shown if r.get("state") == tasks.RUNNING),
                     key=lambda t: t["created"])

    titles = {r.get("id"): r.get("title") or "" for r in recs}
    unmerged = sorted(({"id": u.get("id"), "title": titles.get(u.get("id")) or u.get("title") or "",
                        **unmerged_of(u)}
                       for u in unmerged_rows or () if (u.get("project") or "") == slug),
                      key=lambda u: -(u.get("commits") or 0))

    counts: dict = {}
    for r in shown:
        if r.get("role") != "reviewer":
            counts[r.get("state") or "?"] = counts.get(r.get("state") or "?", 0) + 1
    return {
        "slug": slug, "title": prec.get("title") or slug,
        "archived": bool(prec.get("archived")),
        "readiness": readiness(prec), **failing_lines(prec),
        "ci_gate": str(prec.get("ci_gate") or ""),
        "health": checks, "app": store or None, "health_unread": health_unread,
        "landed": landed, "releases": tags,
        "release": release,
        "needs": needs, "queued": queued, "running": running,
        "unmerged": unmerged, "counts": counts,
        "cost_week": costs.by_project(records, now=now, unfiled=UNFILED).get(slug),
        "days": days, "note": costs.NOTE,
    }


# --- one task's page ---------------------------------------------------------------

#: The reply text a page carries. Replies run to tens of kilobytes; past this
#: the page says it was cut rather than shipping a megabyte to a phone.
PAGE_TEXT_MAX = 120_000

#: What each state means, in plain words, for the line under a task's title.
STATE_WORDS = {
    tasks.PROPOSED: "Proposed. Nothing runs until you accept it.",
    tasks.QUEUED: "Queued. The runner takes it when a worker is free.",
    tasks.RUNNING: "Running now.",
    tasks.BLOCKED: "Waiting on something else before it can go on.",
    tasks.AWAITING_APPROVAL: "Finished and waiting for your approval. Approving lands it.",
    tasks.NEEDS_INPUT: "Stopped to wait for you.",
    tasks.FAILED: "Stopped without finishing.",
    tasks.DONE: "Done.",
    tasks.CANCELLED: "Cancelled. It will not run again unless it is filed again.",
}

#: The timeline's words for an event kind.
EVENT_WORDS = {
    tasks.QUEUED: "Queued", tasks.RUNNING: "Run started", tasks.BLOCKED: "Waiting",
    tasks.AWAITING_APPROVAL: "Waiting for approval", tasks.NEEDS_INPUT: "Waiting for you",
    tasks.FAILED: "Failed", tasks.DONE: "Done", tasks.CANCELLED: "Cancelled",
    tasks.PROPOSED: "Proposed", "decided": "Decided by you", "edited": "Edited",
    "priority": "Priority changed",
}


def _event_line(e: dict) -> tuple[str, str]:
    """(label, detail) for one stored event, the who-and-how said first."""
    kind, detail = str(e.get("kind") or ""), str(e.get("detail") or "")
    words = EVENT_WORDS.get(kind, kind.replace("_", " ").capitalize() or "Event")
    for verb, label in (("accept via ", "Accepted"), ("retry via ", "Retried"),
                        ("dismiss via ", "Dismissed"), ("cancel via ", "Cancelled"),
                        ("approve via ", "Approved")):
        if detail.startswith(verb):
            # "approve via ui (a note)": the who, then whatever was said with it.
            who, _, note = detail[len(verb):].partition(" (")
            who = "the dashboard" if who in ("ui", "you", "dashboard") else who
            return f"{label} via {who}", note[:-1] if note.endswith(")") else note
    via = str(e.get("via") or "")
    if via and via not in detail:
        detail = f"{detail} ({via})" if detail else via
    return words, detail


def state_words(rec: dict, by_id: dict, queue_pos: int | None = None) -> str:
    """The line under a task's title: what its state means for this task."""
    state = rec.get("state") or ""
    result = rec.get("result") or {}
    needs = rec.get("needs_user") or {}
    if state == tasks.QUEUED and queue_pos:
        return f"Queued, #{queue_pos} in line. The runner takes it when a worker is free."
    if state == tasks.BLOCKED:
        # A blocker no longer on record is not something to wait on.
        open_ = [by_id[b] for b in rec.get("blocked_on") or [] if b in by_id]
        open_ = [b for b in open_ if b.get("state") not in tasks.TERMINAL]
        if any(b.get("role") == "reviewer" for b in open_):
            return "Its run finished; waiting for its review before it comes to you."
        if rec.get("retry_at"):
            return "Held after a failure outside it (a quota or an outage); it retries on its own."
        if open_:
            return f"Waiting on {', '.join(b.get('id') or '?' for b in open_)} to finish."
    if state == tasks.NEEDS_INPUT:
        if research.has_findings(rec):
            return "Research finished: read its findings, then close it or dig deeper."
        if needs.get("action"):
            return "It did everything it could and left one step that is yours to take."
        return "It asked you something and waits for your answer."
    if state == tasks.AWAITING_APPROVAL and needs.get("action"):
        return "Finished, and it left one step for you; approving lands it."
    if state == tasks.FAILED:
        why = inbox._event_why(rec)
        return f"Stopped without finishing: {why}" if why else STATE_WORDS[state]
    if state == tasks.DONE:
        l = result.get("landing") or {}
        if l.get("landed") or result.get("landed"):
            head = str(l.get("head") or result.get("landed") or "")[:8]
            return f"Done, and landed onto {l.get('base') or 'its base'} as {head}."
        words = landing_words(l)
        if words:
            return words
        if research.is_research(rec):
            return "Done; its findings were closed."
    if state == tasks.CANCELLED and not (rec.get("attempts") or 0):
        return "Dismissed before it ever ran."
    return STATE_WORDS.get(state, state)


#: Landing stages that are under way, not refused (the dashboard's landing()).
LANDING_UNDERWAY = {"in-progress": "landing under way", "ci-wait": "waiting for CI before it lands"}


def landing_words(l: dict) -> str:
    """What a done task's landing record says, for its state line; "" when
    there is none (or it landed: said with its head by the caller)."""
    if not l or l.get("landed"):
        return ""
    stage = str(l.get("stage") or "?")
    if stage == "nothing-to-land":
        return "Done; its work was already on the base, so there was nothing to land."
    if stage in LANDING_UNDERWAY:
        return f"Done; {LANDING_UNDERWAY[stage]}."
    if l.get("reworked_by"):
        return (f"Done, but not landed ({stage}): handed to {l['reworked_by']} "
                "to catch up with the base.")
    if merge.needs_a_person(l):
        return f"Done, but not landed ({stage}): its branch waits for you."
    return f"Done, but not landed ({stage})."


def _brief(r: dict | None) -> dict | None:
    if not r:
        return None
    return {"id": r.get("id") or "", "title": r.get("title") or r.get("id") or "",
            "state": r.get("state") or "", "role": r.get("role") or ""}


def task_page(rec: dict, records, *, unmerged_rows=(), changes=None, project_title: str = "",
              now: float | None = None) -> dict:
    """Everything about one task on one page, read-only.

    Named fields only: a record carries its scope (a checkout, an
    environment), a session and checkpoint, and none of that is the page's.
    `changes` is taskchanges.read's answer (bot.task_changes) -- None when the
    task has no repository to read. The buttons are inbox.py's own for it, so
    the page offers exactly what Needs you would, through the same actions.
    """
    now = time.time() if now is None else now
    records = list(records)
    by_id = {r.get("id"): r for r in records}
    tid = rec.get("id") or ""
    reviews = sorted((r for r in records if r.get("role") == "reviewer"
                      and r.get("parent") == tid), key=lambda r: r.get("created") or 0)
    kids = sorted((r for r in records if r.get("parent") == tid and r.get("role") != "reviewer"),
                  key=lambda r: r.get("created") or 0)
    # A branch that could not land is handed to a catch-up task filed with
    # source_ref (not parent): its runs are this work's reruns.
    catchups = sorted((r for r in records if r.get("source") == "rework"
                       and r.get("source_ref") == tid and r.get("id") != tid),
                      key=lambda r: r.get("created") or 0)
    unmerged = {u.get("id"): u for u in unmerged_rows or ()}
    item = inbox._task_item(rec, by_id, unmerged)
    queue = sorted((r for r in records if _in_queue(r)), key=tasks.claim_order)
    pos = next((i + 1 for i, r in enumerate(queue) if r.get("id") == tid), None)
    result = rec.get("result") or {}
    text = result.get("text") if isinstance(result.get("text"), str) else ""
    cut = len(text) > PAGE_TEXT_MAX
    project = rec.get("project") or (by_id.get(rec.get("parent") or "", {}).get("project")
                                     if rec.get("role") == "reviewer" else "") or ""

    # Cost: its own runs, each dated, and each review's.
    runs = []
    for r in result.get("cost_runs") or []:
        if not (isinstance(r, (list, tuple)) and len(r) == 2):
            continue
        at, usd = r
        runs.append({"at": at if isinstance(at, (int, float)) and not isinstance(at, bool) else None,
                     "usd": round(float(usd), 4) if isinstance(usd, (int, float))
                     and not isinstance(usd, bool) else None})
    total = costs.total(rec, [*reviews, *catchups])
    cost = {"total": total, "own": costs.own(rec), "runs": runs,
            "reviews": [{"id": r.get("id"), "state": r.get("state") or "",
                         "usd": costs.own(r), "ran": costs.ran(r)} for r in reviews],
            "catchups": [{"id": r.get("id"), "state": r.get("state") or "",
                          "usd": costs.own(r), "ran": costs.ran(r)} for r in catchups],
            "attempts": rec.get("attempts") or 0}

    # The timeline: filed, every stored event, each review, the landing, the
    # release that shipped it and whether the running bot has it.
    tl = [{"at": rec.get("created") or 0, "kind": "created",
           "label": "Filed" + (f" by the {rec['filed_by']}" if rec.get("filed_by") else ""),
           "detail": f"from {rec.get('source')}" if rec.get("source") else ""}]
    runs_seen = 0
    for e in rec.get("events") or []:
        if not isinstance(e, dict):
            continue
        label, detail = _event_line(e)
        if e.get("kind") == tasks.RUNNING:
            runs_seen += 1
            label = f"Run {runs_seen} started"
        at = e.get("at")
        tl.append({"at": at if isinstance(at, (int, float)) and not isinstance(at, bool) else 0,
                   "kind": str(e.get("kind") or ""), "label": label, "detail": detail[:300]})
    for r in runs:
        if r["at"]:
            tl.append({"at": r["at"], "kind": "run-ended", "label": "Run ended",
                       "detail": f"${r['usd']:.2f}" if r["usd"] is not None else "cost unknown"})
    for r in catchups:
        tl.append({"at": r.get("created") or 0, "kind": "catchup",
                   "label": "Handed to a catch-up task", "detail": r.get("id") or "",
                   "id": r.get("id") or ""})
    for r in reviews:
        rv_done = finished_at(r) if r.get("state") in tasks.TERMINAL else None
        tl.append({"at": r.get("created") or 0, "kind": "review", "label": "Review started",
                   "detail": r.get("id") or "", "id": r.get("id") or ""})
        if rv_done:
            tl.append({"at": rv_done, "kind": "review", "label": f"Review {r.get('state')}",
                       "detail": r.get("id") or "", "id": r.get("id") or ""})
    landing = result.get("landing") or {}
    if landing.get("at"):
        tl.append({"at": landing["at"], "kind": "landing",
                   "label": (f"Landed onto {landing.get('base') or 'the base'}" if landing.get("landed")
                             else "Nothing to land" if landing.get("stage") == "nothing-to-land"
                             else (lambda w: w[:1].upper() + w[1:])(
                                 LANDING_UNDERWAY.get(landing.get("stage") or "", ""))
                             or f"Not landed ({landing.get('stage') or '?'})"),
                   "detail": (str(landing.get("head") or "")[:8] if landing.get("landed")
                              else str(landing.get("detail") or "")[-300:])})
    ch = changes or {}
    if (ch.get("released") or {}).get("at"):
        tl.append({"at": ch["released"]["at"], "kind": "release",
                   "label": f"Released in {ch['released']['tag']}", "detail": ""})
    if ch.get("deployed") is True:
        tl.append({"at": ch.get("deployed_at") or now, "kind": "deploy",
                   "label": "Running in the bot", "detail": "the bot's running revision has it"})
    tl.sort(key=lambda x: (x["at"] or 0))

    review = result.get("review") or None
    return {
        "id": tid, "title": rec.get("title") or tid, "state": rec.get("state") or "",
        "state_words": state_words(rec, by_id, pos), "role": rec.get("role") or "",
        "project": project, "project_title": project_title or project,
        "source": rec.get("source") or "", "filed_by": rec.get("filed_by") or "",
        "created": rec.get("created") or 0, "updated": rec.get("updated") or 0,
        "attempts": rec.get("attempts") or 0, "queue_pos": pos,
        "goal": (rec.get("goal") or "")[:PAGE_TEXT_MAX],
        "text": text[-PAGE_TEXT_MAX:] if cut else text, "text_cut": cut,
        "needs_user": ({k: str((rec.get("needs_user") or {}).get(k) or "")[:500]
                        for k in ("action", "why")}
                       if (rec.get("needs_user") or {}).get("action") else None),
        "item": item,
        "result": {"review": review,
                   "landing": ({**landing, "detail": str(landing.get("detail") or "")[-600:]}
                               if landing else None),
                   **({"decided_by": result["decided_by"]} if result.get("decided_by") else {}),
                   **({"decided_via": result["decided_via"]} if result.get("decided_via") else {})},
        "branch": rec.get("branch") or "", "commits": rec.get("commits") or 0,
        "unmerged": unmerged_of(unmerged.get(tid)),
        "changes": changes,
        "cost": cost, "cost_total": cost["total"],
        "timeline": tl,
        "thread": rec.get("thread") or "",
        "thread_link": slacklinks.for_key(rec.get("thread") or ""),
        "parent": _brief(by_id.get(rec.get("parent") or "")),
        "children": [_brief(r) for r in kids], "reviews": [_brief(r) for r in reviews],
        "catchups": [_brief(r) for r in catchups],
        "finished": finished_at(rec) if rec.get("state") in tasks.TERMINAL else None,
        "note": costs.NOTE,
    }

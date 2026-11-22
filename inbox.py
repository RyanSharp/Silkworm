"""The dashboard's home screen: one list of everything waiting on you.

The task panel's "Needs you" tab, the board's column, the operator's plan in
its thread and a health check's "decide:" line each held part of the answer
to "what is mine to do?", in four places. This puts them in one list: every
proposal, approval, question, failure and refused landing, the operator's
recorded ops plan, and each health check that names a decision of yours --
grouped by project, the most urgent group and item first, newest first
among equals.

Each item says, in plain words, what it is (`title`), the one line of why or
what the step is (`line`), the operator's recommendation if it made one for
the item as it stands (tasks.recommendation_for), and which actions it
allows (`actions`): the page draws exactly those, each through the same
/tasks action (and the same checks) the task panel and the board use.
Decided here, so the page names no role or state rule of its own.

Pure over what it is handed: the bot's /tasks `inbox` route supplies the
records, the cached branch survey, the live plan and the health rows.
"""

import health
import merge
import research
import slacklinks
import tasks

#: Lower is more urgent. Something stopped (a failure), then a decision only
#: you can unblock (approve, run the plan), then questions and refused
#: landings, then the owner's health decisions, and last the proposals --
#: nothing runs until you accept one, and nothing breaks while it waits.
URGENCY = {"failed": 0, "approval": 1, "plan": 1, "question": 2, "findings": 2,
           "landing": 2, "decision": 3, "proposal": 4}

#: The plain-words label each kind's card is headed with.
KIND_LABEL = {"failed": "Failed", "approval": "Waiting for approval", "plan": "Ops plan",
              "question": "Your step", "findings": "Findings to read",
              "landing": "Not landed", "decision": "Decide", "proposal": "Proposal"}

#: The group for items filed under no project, and the operator's own (its plan).
UNFILED = "__unfiled__"
OPERATOR = "__operator__"

LINE_MAX = 200


def _clip(text, n: int = LINE_MAX) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _goal_line(rec: dict) -> str:
    """The goal's first line that says more than the title does."""
    title = (rec.get("title") or "").strip()
    for ln in (rec.get("goal") or "").splitlines():
        ln = ln.strip()
        if ln and ln != title and not title.startswith(ln):
            return ln
    return ""


def _event_why(rec: dict) -> str:
    ev = rec.get("events") or []
    e = ([x for x in ev if (x or {}).get("kind") == rec.get("state")][-1:] or ev[-1:] or [{}])[0]
    return str((e or {}).get("detail") or "")


def _task_item(rec: dict, by_id: dict, unmerged: dict) -> dict | None:
    """The item a task is in the inbox, or None if it is not waiting on you."""
    state = rec.get("state")
    tid = rec.get("id") or ""
    needs = rec.get("needs_user") or {}
    acts: list = []
    if state == tasks.PROPOSED:
        kind = "proposal"
        acts = ["accept", "dismiss"]
        line = _goal_line(rec) or (f"proposed by {rec.get('source')}" if rec.get("source")
                                   else "proposed; nothing runs until you accept it")
    elif state == tasks.AWAITING_APPROVAL:
        kind = "approval"
        rv = (rec.get("result") or {}).get("review") or {}
        if needs.get("action"):
            line = f"yours to do: {needs['action']}" + (f" — {needs['why']}" if needs.get("why") else "")
        elif rv:
            line = ("review passed" if rv.get("ok") else "review flagged") + (
                f": {rv.get('summary')}" if rv.get("summary") else "")
        else:
            line = "finished; approving lands it"
        acts = ["approve", "rework", "dismiss"]
    elif state == tasks.NEEDS_INPUT and research.has_findings(rec):
        kind = "findings"
        line = (f"yours to do: {needs['action']}" if needs.get("action") else
                _goal_line(rec) or "research finished; read its findings")
        acts = (["release"] if needs.get("release") else []) + ["close", "dig", "dismiss"]
    elif state == tasks.NEEDS_INPUT:
        kind = "question"
        if needs.get("action"):
            line = f"yours to do: {needs['action']}" + (f" — {needs['why']}" if needs.get("why") else "")
        else:
            line = _event_why(rec) or "waiting for your answer"
        acts = ((["release"] if needs.get("release") else [])
                + (["resolve"] if needs.get("action") else []) + ["rework", "dismiss"])
    elif state == tasks.FAILED:
        kind = "failed"
        line = _event_why(rec) or "stopped with no reason recorded"
        no_retry = tasks.no_retry(rec, by_id)
        acts = ([] if no_retry else ["retry"]) + ["dismiss"]
    elif state == tasks.DONE:
        landing = (rec.get("result") or {}).get("landing") or {}
        row = unmerged.get(tid)
        # A landing git refused, on a branch still here to land: the one done
        # task that is yours (merge.needs_a_person). Not one handed on to a
        # catch-up task, and not one whose branch lives only on a remote --
        # Land and Drop act on the local branch, as the task panel says.
        if (not merge.needs_a_person(landing) or landing.get("reworked_by") or not row
                or row.get("local") is False):
            return None
        kind = "landing"
        line = f"not landed ({landing.get('stage') or '?'})" + (
            f" — {landing.get('detail')}" if landing.get("detail") else "")
        acts = ["land", "drop"]
    else:
        return None
    if not tasks.on_board(rec):
        return None
    # A reviewer is filed under no project; it belongs to its parent's.
    project = rec.get("project") or (by_id.get(rec.get("parent") or "", {}).get("project")
                                     if rec.get("role") == "reviewer" else "") or ""
    rec_ = tasks.recommendation_for(rec)
    if rec_ and rec_["action"] not in acts:
        rec_ = None                           # for a button this card does not have
    return {
        "kind": kind, "label": KIND_LABEL[kind], "id": tid, "state": state,
        "role": rec.get("role") or "", "project": project,
        "title": _clip(rec.get("title") or rec.get("goal") or tid, 120),
        "line": _clip(line), "actions": acts, "recommendation": rec_,
        "round": research.round_of(rec),
        "needs_user": needs or None,
        "thread": rec.get("thread") or "",
        "thread_link": slacklinks.for_key(rec.get("thread") or ""),
        # When it came to need you: not its last write, which a
        # recommendation (or any note on it) would bump to the top.
        "at": tasks.entered(rec),
    }


def plan_item(plan: dict | None) -> dict | None:
    """The operator's live ops plan, as one item: the dashboard draws it as
    the plan's card (its entries ticked or not, Run all, Run selected, Not
    now), whose buttons answer it as your typed reply would (bot.handle_plan)."""
    if not plan or plan.get("answered"):
        # Answered (yes, no, or some of it): no later message answers it
        # (OperatorState.answer_plan), so its buttons would answer nothing.
        return None
    left = [e for e in plan.get("entries") or [] if not e.get("used")]
    if not left:
        return None
    steps = [" ".join([e.get("name") or "?", *map(str, e.get("args") or [])]) for e in left]
    return {"kind": "plan", "label": KIND_LABEL["plan"], "id": str(plan.get("id") or ""),
            "state": "", "role": "", "project": OPERATOR,
            "title": f"The operator's plan: {len(left)} step{'' if len(left) == 1 else 's'}",
            "line": _clip("; ".join(f"{e.get('n')}. {s}" for e, s in zip(left, steps))),
            "steps": steps, "entries": [{"n": e.get("n"), "command": s} for e, s in zip(left, steps)],
            "actions": ["run-all", "run-selected", "not-now"], "recommendation": None,
            "round": "", "needs_user": None, "thread": plan.get("thread") or "",
            "thread_link": slacklinks.for_key(plan.get("thread") or ""),
            "at": plan.get("at") or 0}


def decision_items(rows, operator_thread: str = "", registry: dict | None = None) -> list[dict]:
    """A health check that names a decision of yours ("decide: …"): yours to
    make, so on the list -- with nothing to press but Discuss, since what is
    decided is said to the operator, not done by a button."""
    out = []
    for r in rows or ():
        owner = health.decision(r, registry)
        if not owner:
            continue
        slug, check = r.get("project") or "", r.get("check") or ""
        out.append({"kind": "decision", "label": KIND_LABEL["decision"],
                    "id": f"health:{slug}:{check}", "state": r.get("status") or "",
                    "role": "", "project": slug,
                    "title": _clip(f"{check}: {r.get('summary') or r.get('status') or ''}", 120),
                    "line": _clip(f"decide: {owner}"),
                    "actions": ["discuss"] if operator_thread else [],
                    "recommendation": None, "round": "", "needs_user": None,
                    "thread": operator_thread,
                    "thread_link": slacklinks.for_key(operator_thread) if operator_thread else "",
                    "at": r.get("last_changed") or r.get("checked_at") or 0})
    return out


def _order(item: dict) -> tuple:
    return (URGENCY.get(item["kind"], 9), -(item.get("at") or 0), item.get("id") or "")


def build(records, *, unmerged_rows=(), plan: dict | None = None, health_rows=(),
          operator_thread: str = "", project_titles: dict | None = None,
          registry: dict | None = None) -> dict:
    """{groups: [{project, title, items}], count}: what waits on you.

    Groups are ordered by their most urgent item, then by their newest; items
    within a group the same way. `project_titles` is {slug: title}."""
    records = list(records)
    by_id = {r.get("id"): r for r in records}
    unmerged = {u.get("id"): u for u in unmerged_rows or ()}
    items = [i for i in (_task_item(r, by_id, unmerged) for r in records) if i]
    p = plan_item(plan)
    if p:
        items.append(p)
    items += decision_items(health_rows, operator_thread, registry)
    titles = dict(project_titles or {})
    groups: dict = {}
    for it in items:
        slug = it["project"] or UNFILED
        groups.setdefault(slug, []).append(it)
    out = []
    for slug, its in groups.items():
        its.sort(key=_order)
        title = ("Operator" if slug == OPERATOR else "Unfiled" if slug == UNFILED
                 else titles.get(slug) or slug)
        out.append({"project": slug, "title": title, "items": its})
    out.sort(key=lambda g: (min(URGENCY.get(i["kind"], 9) for i in g["items"]),
                            -max(i.get("at") or 0 for i in g["items"]), g["title"].lower()))
    return {"groups": out, "count": len(items)}

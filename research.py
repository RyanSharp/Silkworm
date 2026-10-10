"""Research: work whose output is findings for you to read and decide on.

Research used to be filed as `assistant` on the queue. It finished `done`, and
`done` is not on the board -- the board shows what needs you -- so findings sat
unread in the task's thread with nothing to turn them into next steps. And a
scheduled wake-up also runs as a queued `assistant` task, so the role could
not tell the two apart.

So research is a role of its own. It runs exactly as `assistant` does, but a
run that completes parks in `needs_input` with its findings (tasks.ending),
where you Close it, send it to Dig deeper with a question, or Discuss it in its
thread. The next steps it suggests are filed as *proposed* children of the
research task (bot.handle_file_task), through the same proposal gate, backlog
cap and dedup as the nightly pass, so a research run can never commission work
that bypasses them. What the cap or dedup refused is recorded on the research
task, so it is said rather than dropped.

Pure: no Slack, no store. The board, the dashboard and the filing route all
read from here so they cannot disagree about what research is.
"""

import roles
import tasks

ROLE = roles.RESEARCHER

#: `source` on the next steps a research task files, so they can be told from
#: the nightly pass's proposals and listed under the task that suggested them.
SOURCE = "research"

#: The event detail a finished research task parks with.
FINDINGS_DETAIL = tasks.FINDINGS_DETAIL

#: Bounds on what is kept of a refused next step: it is shown, not re-filed.
MAX_HELD = 10


def is_research(rec: dict | None) -> bool:
    return (rec or {}).get("role") == ROLE


def has_findings(rec: dict | None) -> bool:
    """A research task waiting for you to decide on its findings."""
    return is_research(rec) and (rec or {}).get("state") == tasks.NEEDS_INPUT


def refusal(rec: dict | None) -> str:
    """Why a findings action (Close, Dig deeper) cannot apply to `rec`, or "".

    Running is asked again under the store's lock by the move itself; this is
    the early, readable answer for the clicks that are plainly stale.
    """
    if not rec:
        return "unknown task"
    if not is_research(rec):
        return "only a research task has findings to close or dig into"
    state = rec.get("state")
    if state == tasks.RUNNING:
        return tasks.RUNNING_BUSY
    if state != tasks.NEEDS_INPUT:
        return f"its findings are no longer waiting on you; it is {state}"
    return ""


def dig_addendum(question: str, by: str = "") -> str:
    """What a Dig deeper appends to the goal: the user's question, labelled."""
    return (f"Dig deeper — a follow-up question from {by or 'the user'} on "
            f"your findings above. Answer it, building on what you already "
            f"found, and end with findings in the same shape:\n{question.strip()}")


def children(tid: str, records) -> list[dict]:
    """The next steps a research task filed, oldest first, cut to a row."""
    if not tid:
        return []
    kids = [r for r in records if r.get("parent") == tid
            and r.get("source") == SOURCE]
    kids.sort(key=lambda r: r.get("created") or 0)
    return [{"id": r.get("id") or "", "state": r.get("state") or "",
             "title": r.get("title") or (r.get("goal") or "")[:60]} for r in kids]


def children_by_parent(records) -> dict[str, list[dict]]:
    """children() for every research task in `records` at once."""
    records = list(records)
    return {r.get("id"): children(r.get("id"), records)
            for r in records if is_research(r)}


def held_entry(goal: str, why: str, at: float) -> dict:
    """A next step the filing route refused, as kept on the research task."""
    return {"goal": " ".join((goal or "").split())[:300],
            "why": " ".join((why or "").split())[:300], "at": at}


def with_held(rec: dict, goal: str, why: str, at: float) -> list[dict]:
    """`rec`'s held next steps with one more, bounded, newest kept."""
    held = list(rec.get("next_steps_held") or []) + [held_entry(goal, why, at)]
    return held[-MAX_HELD:]


def line(kids: list[dict], held: list[dict]) -> str:
    """One line for a board row: how many next steps, and how many held."""
    parts = []
    if kids:
        open_ = sum(1 for k in kids if k.get("state") == tasks.PROPOSED)
        parts.append(f"{len(kids)} next step{'s' if len(kids) != 1 else ''} filed"
                     + (f" ({open_} awaiting accept)" if open_ else ""))
    if held:
        parts.append(f"{len(held)} not filed")
    return ", ".join(parts)


def parked_note(rec: dict, kids: list[dict]) -> str:
    """What the research task's thread is told when its findings park."""
    held = rec.get("next_steps_held") or []
    summary = line(kids, held)
    return (":mag: *Findings ready.* It waits in Needs you: *Close* it, "
            "*Dig deeper* with a question, or *Discuss* it here."
            + (f"\n_{summary} — accept or dismiss them on the board._" if kids else
               f"\n_{summary}._" if summary else ""))


def answer(rec: dict | None, width: int = 400) -> str:
    """The short answer out of a research task's findings, for a board line.

    The prompt asks for an `Answer:` heading; a reply without one is shown by
    its opening instead, never as nothing.
    """
    text = (((rec or {}).get("result") or {}).get("text") or "").strip()
    if not text:
        return ""
    low = text.lower()
    at = low.rfind("answer:")
    if at >= 0:
        text = text[at + len("answer:"):]
        stop = text.lower().find("confidence:")
        if stop >= 0:
            text = text[:stop]
    text = " ".join(text.replace("*", " ").split())
    return text if len(text) <= width else text[:width - 1].rstrip() + "…"

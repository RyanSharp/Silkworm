"""Turn a scoping conversation into filed work.

Nearly everything arrives as a Slack message someone is waiting on: of 143
recent tasks, 130 were live conversation and 3 were created in the dashboard.
That is not a preference for chat — it is that a conversation could not *emit*
anything. Scoping a piece of work ended with a plan in a thread and no way to
act on it short of retyping each piece into a form.

So a turn can file tasks. The plan you just agreed becomes the queue, in the
place you were already talking, and the pieces run unattended in their own
checkouts with a reviewer between them and "done".

Filed at `queued`, not `proposed`: you scoped these in conversation, so they
are already reviewed by the only person whose review the gate exists to get.
`proposed` is for things that arrive without you.
"""

import os
import re

import tasks

#: Per turn. A plan larger than this is not a plan, it is a model that has
#: stopped counting -- and every task costs a session, or two with review.
MAX_PER_TURN = 10

#: A night that finds ten things has not prioritised. Fewer, better -- and
#: five was still too many: unattended nights outran review until the board sat
#: 58 proposals deep, 11 of 16 on one project re-proposing the same three jobs
#: because none of them could reach done. Two. Enforced rather than asked for:
#: the ideator runs unattended, so a run that miscounts would spend the one
#: thing the whole gate protects -- a decision per proposal.
MAX_PROPOSALS = 2


def _env_int(name: str, default: int) -> int:
    """An override from the environment, or the default if it is not usable.

    A limit below one would mean the nightly pass could never file anything,
    which is what turning ideation off is for -- so a value that low, or one
    that is not a number at all, falls back rather than silently disabling a
    schedule the user still believes is running.
    """
    try:
        value = int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


#: Standing, across passes, per project. MAX_PROPOSALS bounds one night; this
#: bounds the pile. The per-pass cap counts only what the running pass filed,
#: so every night starts from zero however many of its predecessors' proposals
#: are still untriaged, and the board can only grow while acceptance lags --
#: which is how `proposed` came to hold 41 tasks at once, several of them
#: describing problems that had since been fixed on main.
#:
#: Two nights' worth of the per-pass cap: one night you have not got to yet is
#: a normal morning, two is the board outpacing you, and a third is not
#: information you did not already have. It is also about as many proposals as
#: can sit in the "what needs me?" list beside the other attention states and
#: still be read rather than scrolled past -- and if the board cannot reach
#: empty, it is the wrong board.
DEFAULT_OPEN_PROPOSALS = 2 * MAX_PROPOSALS


def max_open_proposals() -> int:
    """The standing limit in force, from the environment or the default.

    Read per call rather than bound at import: bot.py imports this module
    before it calls load_dotenv(), so a constant evaluated here would be
    fixed before `.env` was read and MAX_OPEN_PROPOSALS would be documented
    but inert -- settable only by exporting it into the real environment,
    which is not how anything else here is configured.
    """
    return _env_int("MAX_OPEN_PROPOSALS", DEFAULT_OPEN_PROPOSALS)


#: Long enough to act on without the implementor guessing what you meant.
MIN_GOAL_CHARS = 15
MAX_GOAL_CHARS = 4000

#: A ceiling on a pathological list, not a budget for a real one, and one
#: ceiling for every list the nightly pass is shown: the unmerged branches
#: below, and the open and dismissed items in `board_note`. Both of the lower
#: numbers tried cut precisely the rows worth keeping. At 12 it cut ten of the
#: twenty-two unmerged branches this repo held when it was measured; at 20 it
#: cut six of trader's twenty-six open items, two of the seven duplicate
#: gauntlet proposals these notes exist to stop. Both times the cut fell at the
#: tail, and because the rows arrive newest-first the ones dropped were the
#: oldest -- precisely the ones a nightly pass has had the most chances to
#: re-derive, and the same ones every night, for ever. Forty is above any list
#: we have actually seen, so the cut is a guard against a runaway board rather
#: than a thing that happens; each list is capped separately, so a long backlog
#: cannot push every dismissal out; and when one is cut the note says how many
#: are missing rather than presenting a slice as the whole board. Worst case is
#: a few thousand characters once a night, against a whole session and a
#: reviewer spent re-implementing something already written down -- which is
#: also why this is not bounded by MAX_GOAL_CHARS: that limit is about a goal a
#: person has to act on, and this is a prompt nobody files.
MAX_LISTED = 40


def limit_for(propose: bool = False) -> int:
    """How many tasks one turn may file, given what it is filing.

    A proposal costs you an accept or a dismiss whether or not it was worth
    making, so an unattended pass gets the smaller budget; work you scoped in
    conversation is already agreed and gets the full one.
    """
    return MAX_PROPOSALS if propose else MAX_PER_TURN


def open_proposals(project_tasks) -> int:
    """How many of a project's proposals are still waiting to be triaged.

    Only `proposed`: that is the ideator's own untriaged output, and the only
    pile a further night makes worse. Work you already accepted is queued, and
    a task waiting on your approval ran and produced something.
    """
    return sum(1 for t in project_tasks if t.get("state") == tasks.PROPOSED)


def backlog_full(open_now: int) -> bool:
    """Whether a project already has as many open proposals as it may hold."""
    return open_now >= max_open_proposals()


def backlog_refusal(slug: str, open_now: int) -> str:
    """Why a proposal is being refused, addressed to whoever tried to file it.

    A reason rather than a silent drop: the pass that hits this should stop
    and say so in its reply, not keep looking for a proposal that would be
    accepted.
    """
    return (f"{open_now} proposals for {slug or 'this project'} are already "
            f"waiting to be triaged, and the standing limit is "
            f"{max_open_proposals()} — file nothing further and say so in your "
            "reply. Accepting or dismissing what is already on the board is "
            "worth more than another idea on top of it.")


def validate(goal: str, filed_already: int = 0, propose: bool = False,
             open_now: int = 0, slug: str = "") -> str:
    """Returns an error message, or '' if this task may be filed.

    `open_now` is how many proposals the project already has untriaged, which
    only bounds proposals: work you scoped in conversation is agreed, and
    refusing it because a nightly pass got ahead of itself would punish the
    wrong filing.
    """
    goal = (goal or "").strip()
    if len(goal) < MIN_GOAL_CHARS:
        return (f"a task needs a goal of at least {MIN_GOAL_CHARS} characters — "
                "whoever picks it up has only this to go on")
    if len(goal) > MAX_GOAL_CHARS:
        return f"that goal is over {MAX_GOAL_CHARS} characters; split it"
    if propose and backlog_full(open_now):
        return backlog_refusal(slug, open_now)
    limit = limit_for(propose)
    if filed_already >= limit:
        if propose:
            return (f"{limit} proposals is the limit for one pass — file the "
                    "ones most worth doing, not every one you found")
        return (f"{limit} tasks is the limit for one turn — file the "
                "next slice after these have run")
    return ""


def _clip(title, width: int = 90) -> str:
    """A title, marked when it has been cut, so a truncation is not read as
    the whole of what the branch does."""
    title = (title or "").strip()
    return title if len(title) <= width else title[:width - 1].rstrip() + "…"


def unmerged_note(rows, limit: int = MAX_LISTED) -> str:
    """Tell the nightly pass what is already fixed on a branch nobody merged.

    The ideator reads the base branch, which is the honest thing to read -- and
    a gap fixed on an unmerged branch is still a gap there. So it re-derives it
    and files it again, correctly, for as long as the branch sits unlanded.
    That is not hypothetical: one fix was proposed on two different nights and
    implemented twice, each time costing a session and a reviewer, because the
    first branch never landed.

    Ordered oldest-first here, against the newest-first order `survey` returns
    for the dashboard, where the question is "what did last night leave?". This
    paragraph asks the opposite question. A branch that has sat for a month has
    given a month of nightly passes the chance to rediscover what is on it;
    last night's branch was named in last night's reply. Taking the caller's
    order meant a cut fell on the oldest rows -- the ones this whole paragraph
    exists to protect -- so the order is decided here rather than inherited.

    Empty when there is nothing to say, so a healthy project pays no tokens
    for the paragraph.
    """
    if not rows:
        return ""
    # Branch name breaks ties so two rows updated in the same second cannot
    # swap places between nights and make the cut look arbitrary.
    rows = sorted(rows, key=lambda r: ((r.get("updated") or 0),
                                       r.get("branch") or ""))
    shown = rows[:limit]
    body = "\n".join(
        f"  - {r.get('branch') or '?'} ({r.get('commits') or 0} commit"
        f"{'s' if (r.get('commits') or 0) != 1 else ''}, off "
        f"{r.get('base') or 'the base'}) — {_clip(r.get('title'))}"
        for r in shown)
    if len(rows) > len(shown):
        # Said out loud for the same reason `board_note` says it: the closing
        # instruction below reads as a claim about every unmerged branch there
        # is, and a silent slice would make that claim false.
        body += (f"\n  …and {len(rows) - len(shown)} newer ones, not listed "
                 "here — treat this as a sample of the branches, not all of "
                 "them")
    return ("Already done, and sitting on a branch that was never merged:\n\n"
            f"{body}\n\n"
            "Those gaps are fixed on those branches and not on the base you "
            "are reading, so the code will look like it still has them. Check "
            "the branch before proposing anything it covers. If the only thing "
            "wrong is that it has not been merged, say that in your reply — do "
            "not file it as work, because implementing it again is exactly the "
            "mistake this list exists to stop.")


# --- what the board already holds ---------------------------------------------

#: Anything not finished and not turned down is still on the board. Derived
#: rather than listed, so a state added later is covered without anyone
#: remembering to come back here.
OPEN_STATES = tuple(s for s in tasks.STATES if s not in tasks.TERMINAL)

#: How long a dismissal is remembered. Long enough that a nightly job cannot
#: wear you down by refiling; short enough that "not now" is not "never".
DISMISSAL_MEMORY_DAYS = 30

#: How much of a task's name a listing slot is worth. MAX_LISTED, which caps
#: how many slots there are, is shared with the branch list and so is defined
#: with the other budgets at the top of this module.
MAX_NAME_CHARS = 100


#: The board's own machinery: the review gate, and the nightly pass itself.
#: Neither is an idea, and listing them as ones already had would be nonsense.
MACHINERY_ROLES = ("reviewer", "ideator")

#: Where a conversation turn comes from. A Slack or web message and a scheduled
#: wake-up are all filed as tasks, all inherit their thread's project, and all
#: sit in a non-terminal state while they run -- and one killed by a restart or
#: a quota error stays there indefinitely. Telling the nightly pass not to file
#: "check whether the paper book's rejects cleared" again costs a listing slot
#: and tells it nothing. Paired with the role below, because `ui` is also where
#: work typed into the dashboard comes from, and that *is* on the board.
CONVERSATION_SOURCES = ("slack", "ui", "defer")


def is_work(rec: dict) -> bool:
    """Is this record a piece of work on the board, or the board running?"""
    role = rec.get("role") or ""
    if role in MACHINERY_ROLES:
        return False
    return not (role == "assistant" and rec.get("source") in CONVERSATION_SOURCES)


def task_name(rec: dict) -> str:
    """A short, recognisable label for a task in a list."""
    name = (rec.get("title") or "").strip() or (rec.get("goal") or "").strip()
    name = " ".join(name.split())
    return _clip(name, MAX_NAME_CHARS)


def already_filed(records, now: float) -> tuple[list[str], list[str]]:
    """Split a project's tasks into (still open, recently dismissed) names.

    Newest first: `board_note` cuts at the tail, so if a board is longer than
    it will list, the oldest go -- and those are the same ones every night.

    Dismissal is remembered only for a proposal that never ran: a task you
    scoped yourself and then cancelled says nothing about whether the idea is
    welcome, and one you accepted and then stopped mid-run was wanted -- its
    stopping is not a reason never to raise it again. `attempts` is the
    durable record of that, incremented only on entering `running`.

    Both halves are filtered by `is_work`: the board's own machinery and the
    conversation turns that happen to carry a project are not ideas.
    """
    cutoff = now - DISMISSAL_MEMORY_DAYS * 86400
    open_names, dismissed = [], []
    for rec in sorted(records, key=lambda r: -(r.get("created") or 0)):
        if not is_work(rec):
            continue
        name = task_name(rec)
        if not name:
            continue
        state = rec.get("state")
        if state in OPEN_STATES:
            open_names.append(name)
        elif (state == tasks.CANCELLED and rec.get("source") == "ideation"
              and not (rec.get("attempts") or 0)
              and (rec.get("updated") or rec.get("created") or 0) >= cutoff):
            dismissed.append(name)
    return open_names, dismissed


def _listing(heading: str, names, limit: int) -> str:
    """One headed list, and a last line when it does not hold everything.

    Truncating in silence would be the bug this module exists to stop: the
    note says "do not file any of these again" and "if everything you find is
    already listed, file nothing", and both read as claims about the whole
    board. `unmerged_note` says how many it left out for the same reason.
    """
    shown = names[:limit]
    body = "\n".join(f"  - {n}" for n in shown)
    if len(names) > limit:
        body += (f"\n  …and {len(names) - limit} more, not listed here — "
                 "treat this as a sample of the board, not all of it")
    return f"{heading}\n{body}\n\n"


def board_note(open_names=(), dismissed=(), limit: int = MAX_LISTED) -> str:
    """Tell the nightly pass what is already sitting in front of the user.

    The ideator starts a fresh session every night and is shown the code, not
    the board. A real gap is still a gap tomorrow, so it re-derives the same
    idea and files it again -- correctly, for a job told nothing. That is not
    hypothetical: the same defect was proposed on two consecutive nights under
    two different framings, and one project collected seven proposals asking
    for the same re-run. Every duplicate costs a dismissal, until a board full
    of things you have already said no to stops being read.

    Empty when there is nothing to say, so a project with a clear board pays
    no tokens for the paragraph.
    """
    open_names, dismissed = list(open_names), list(dismissed)
    if not open_names and not dismissed:
        return ""

    note = ""
    if open_names:
        note += _listing(
            "Already on the board for this project — do not file any of "
            "these again:", open_names, limit)
    if dismissed:
        note += _listing(
            "Already proposed and dismissed — the user said no; do not "
            "bring them back:", dismissed, limit)
    return note + (
        "Those ideas already exist and are already costing a decision. Say "
        "something new, or sharpen one of them in your reply without filing "
        "it again. If everything you find tonight is already listed, file "
        "nothing and say so — that is a good night's work, not a failed "
        "one.")


HOW_TO = """When the user asks you to scope, plan or break down a piece of \
work, you can file the pieces as real tasks rather than only describing them:

    {bin} task --project <slug> [--role implementor|assistant] "<goal>"

Each becomes a queued task that runs unattended in its own git worktree, off \
fresh main, and reports back in its own thread. `implementor` is the default \
and puts the result through an independent reviewer before it can complete; \
use `assistant` for investigation with nothing to review.

Write each goal so it stands alone — the session that runs it has this \
conversation's context only through the project, not through this thread. \
State what "done" looks like.

File tasks when the user has agreed a plan, not to record your own intentions \
mid-answer. At most {max} per turn."""

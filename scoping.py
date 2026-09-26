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

import re

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

#: Long enough to act on without the implementor guessing what you meant.
MIN_GOAL_CHARS = 15
MAX_GOAL_CHARS = 4000

#: A ceiling on a pathological list, not a budget for a real one. At 12 it cut
#: ten of this repo's twenty-two unmerged branches, every night, in near
#: silence -- and because the rows arrive newest-first the ten cut were the ten
#: oldest, which are precisely the branches a nightly pass has had the most
#: chances to re-derive. Forty is above any list we have actually seen, so the
#: cut is a guard against a runaway board rather than a thing that happens.
#: Worst case is a few thousand characters once a night, against a whole
#: session and a reviewer spent re-implementing something already written down.
MAX_LISTED = 40


def limit_for(propose: bool = False) -> int:
    """How many tasks one turn may file, given what it is filing.

    A proposal costs you an accept or a dismiss whether or not it was worth
    making, so an unattended pass gets the smaller budget; work you scoped in
    conversation is already agreed and gets the full one.
    """
    return MAX_PROPOSALS if propose else MAX_PER_TURN


def validate(goal: str, filed_already: int = 0, propose: bool = False) -> str:
    """Returns an error message, or '' if this task may be filed."""
    goal = (goal or "").strip()
    if len(goal) < MIN_GOAL_CHARS:
        return (f"a task needs a goal of at least {MIN_GOAL_CHARS} characters — "
                "whoever picks it up has only this to go on")
    if len(goal) > MAX_GOAL_CHARS:
        return f"that goal is over {MAX_GOAL_CHARS} characters; split it"
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

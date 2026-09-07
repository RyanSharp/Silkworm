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

#: A night that finds ten things has not prioritised. Fewer, better.
MAX_PROPOSALS = 5

#: Long enough to act on without the implementor guessing what you meant.
MIN_GOAL_CHARS = 15
MAX_GOAL_CHARS = 4000


def validate(goal: str, filed_already: int = 0) -> str:
    """Returns an error message, or '' if this task may be filed."""
    goal = (goal or "").strip()
    if len(goal) < MIN_GOAL_CHARS:
        return (f"a task needs a goal of at least {MIN_GOAL_CHARS} characters — "
                "whoever picks it up has only this to go on")
    if len(goal) > MAX_GOAL_CHARS:
        return f"that goal is over {MAX_GOAL_CHARS} characters; split it"
    if filed_already >= MAX_PER_TURN:
        return (f"{MAX_PER_TURN} tasks is the limit for one turn — file the "
                "next slice after these have run")
    return ""


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

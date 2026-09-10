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

import tasks

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


# --- what the nightly look already knows about ---------------------------------
# The ideator starts a fresh session every night, so left to itself it re-derives
# the same gaps and files them again. A real gap is still there tomorrow, which
# makes duplication the *correct* behaviour for a job told nothing -- and every
# duplicate costs a dismissal, until a board full of things you have already said
# no to stops being read. So the goal carries what is already open, and what you
# already turned down.

#: States that mean "this is already on the board". Terminal ones are excluded:
#: work that finished is not a reason to never suggest anything near it again.
OPEN_STATES = tuple(s for s in tasks.STATES if s not in tasks.TERMINAL)

#: How long a dismissal is remembered. Long enough that a nightly job cannot
#: wear you down by refiling; short enough that "not now" is not "never".
DISMISSAL_MEMORY_DAYS = 30

#: Prompt hygiene. A project with fifty open tasks does not need all fifty
#: names in the goal to make the point, and the goal is sent every night.
MAX_LISTED = 20
MAX_NAME_CHARS = 100


def task_name(rec: dict) -> str:
    """A short, recognisable label for a task in a list."""
    name = (rec.get("title") or "").strip() or (rec.get("goal") or "").strip()
    name = " ".join(name.split())
    return name[:MAX_NAME_CHARS - 1] + "…" if len(name) > MAX_NAME_CHARS else name


def already_filed(records, now: float) -> tuple[list[str], list[str]]:
    """Split a project's tasks into (still open, recently dismissed) names.

    Dismissal is remembered only for proposals: a task the user cancelled after
    scoping it themselves says nothing about whether the idea is welcome.
    """
    cutoff = now - DISMISSAL_MEMORY_DAYS * 86400
    open_names, dismissed = [], []
    for rec in sorted(records, key=lambda r: -(r.get("created") or 0)):
        name = task_name(rec)
        if not name:
            continue
        if rec.get("role") == "ideator":
            continue                      # the nightly look itself, not an idea
        state = rec.get("state")
        if state in OPEN_STATES:
            open_names.append(name)
        elif (state == tasks.CANCELLED and rec.get("source") == "ideation"
              and (rec.get("updated") or rec.get("created") or 0) >= cutoff):
            dismissed.append(name)
    return open_names[:MAX_LISTED], dismissed[:MAX_LISTED]


def _listing(heading: str, names: list[str]) -> str:
    return heading + "\n" + "\n".join(f"  - {n}" for n in names) + "\n\n"


def ideation_goal(slug: str, title: str, bin: str,
                  open_names=(), dismissed=()) -> str:
    """The prompt for one project's nightly look."""
    open_names, dismissed = list(open_names), list(dismissed)
    known = ""
    if open_names:
        known += _listing(
            "Already on the board for this project — do not file any of these "
            "again:", open_names)
    if dismissed:
        known += _listing(
            "Already proposed and dismissed — the user said no; do not bring "
            "them back:", dismissed)
    if known:
        known += (
            "Those are the ideas that already exist. Say something new, or "
            "sharpen one of them in your reply without filing it again. If "
            "everything you find tonight is already listed, file nothing and "
            "say so — that is a good night's work, not a failed one.\n\n")

    return (
        f"Look over the {title or slug} project and propose work worth doing.\n\n"
        "Read what is actually there before suggesting anything: the code, "
        "recent commits, the tests, CLAUDE.md. Then file each proposal with\n"
        f"    {bin} task --propose --project {slug} \"<goal>\"\n"
        f"at most {MAX_PROPOSALS} of them, fewer if fewer are warranted, and "
        "none at all if the project is in good shape. Each goal must stand "
        "alone: whoever picks it up will not have read this.\n\n"
        + known +
        "Then say what you looked at and what you filed."
    )

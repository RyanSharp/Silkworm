"""What the nightly review looks for tonight: its lens.

One generic goal, under a system prompt that steered toward "a real gap, a
real inconsistency, a real risk", made every night's proposals fixes. So each
night now looks through one lens -- new features, code quality, or
correctness -- rotated by project and date, and the lens is recorded on the
ideation task and on everything it files, so the acceptance rate per lens can
be read off the board later.

Pure: no stores, no Slack, so the rotation and the goal text are tested
directly. bot.run_ideation builds the goal from it; handle_file_task asks it
which lens a proposal was filed under.
"""
from __future__ import annotations

import zlib
from datetime import date, datetime

FEATURES, QUALITY, CORRECTNESS = "features", "quality", "correctness"

#: The default cycle, in order. A project's override is a list of these.
LENSES: tuple[str, ...] = (FEATURES, QUALITY, CORRECTNESS)

#: One paragraph per lens, put in the goal. Each says what tonight is for and
#: which kinds of work are other nights', so a features night is not spent
#: re-filing the fixes every night used to produce.
PARAGRAPHS: dict[str, str] = {
    FEATURES: (
        "Tonight's lens is features: new user-facing capabilities or product "
        "improvements. Ground them in where the project is already going -- "
        "its brief (CLAUDE.md), any roadmap, premium or scope documents under "
        "docs/, and what recent user-facing work has been building toward. "
        "For each proposal say who it is for, why they would want it, roughly "
        "how big it is (hours, a day, several days), and what it costs to run "
        "-- per-user egress or storage, API calls, or nothing extra. Propose "
        "nothing that contradicts a documented product decision or the "
        "privacy policy; if an idea would need one changed, say that plainly "
        "instead of proposing around it. Refactors and bug fixes are other "
        "nights' work."
    ),
    QUALITY: (
        "Tonight's lens is quality: making the code easier and safer to "
        "change without changing what it does. Refactors that remove real "
        "duplication or tangles, test coverage on risky code that has none, "
        "simplification, performance where something is measurably slow, "
        "developer tooling, and docs or comments that have drifted from the "
        "code. For each proposal say what it buys and how it would be shown "
        "not to change behaviour. New features and bug fixes are other "
        "nights' work."
    ),
    CORRECTNESS: (
        "Tonight's lens is correctness: what today's behaviour gets wrong. "
        "Bugs, security holes, data-integrity risks, races, error paths that "
        "lose or misreport something. For each proposal name the input or "
        "sequence that goes wrong and what happens when it does. New features "
        "and refactors are other nights' work."
    ),
}

#: The rules every lens shares. In the goal as well as the ideator's system
#: prompt, so the goal a pass was given can be read whole on the task.
SHARED_RULES = (
    "Ground every proposal in something you actually saw in this project -- "
    "a file, a commit, a test, a document -- and say where. Prefer a few "
    "concrete proposals to many vague ones. Do not propose work merely to "
    "have proposed something: filing nothing is a perfectly good outcome on "
    "a night the lens finds nothing worth a person's decision, and far "
    "better than manufacturing busywork. A serious defect you come across is "
    "still worth filing whatever tonight's lens is."
)


def parse(text) -> list[str]:
    """A lens override, from "features,correctness" or a list.

    Empty (or "default", "off", "clear", "none") means the default cycle and
    returns []. An unknown name is refused with the ones that exist, rather
    than stored as a lens no night would ever look through. Repeats are
    dropped, keeping the first.
    """
    if text is None:
        return []
    if isinstance(text, str):
        if text.strip().lower() in ("", "default", "off", "clear", "none"):
            return []
        names = text.replace(" ", ",").split(",")
    elif isinstance(text, (list, tuple)):
        names = list(text)
    else:
        raise ValueError(f"lenses must be a list or comma-separated names, not {text!r}")
    out: list[str] = []
    for raw in names:
        if not isinstance(raw, str):
            raise ValueError(f"lens names are text, not {raw!r}")
        name = raw.strip().lower()
        if not name:
            continue
        if name not in LENSES:
            raise ValueError(f"unknown lens {raw.strip()!r}: choose from "
                             + ", ".join(LENSES))
        if name not in out:
            out.append(name)
    return out


def cycle(override=()) -> tuple[str, ...]:
    """The lenses a project rotates through: its override if it has a valid
    one, the default otherwise. A stored override is re-validated, so a
    hand-edited record with a typo falls back to the default rather than
    raising in the scheduler."""
    try:
        chosen = parse(override if isinstance(override, str)
                       else list(override) if isinstance(override, (list, tuple))
                       else [])
    except ValueError:
        chosen = []
    return tuple(chosen) or LENSES


def lens_for(slug: str, day, override=()) -> str:
    """Tonight's lens for `slug`.

    Deterministic from the date, not from what ran last: a restart cannot
    make a night repeat its lens, and a skipped night (a full backlog, the
    bot down) moves on rather than leaving its lens for the next one. Offset
    by the project, so projects reviewed on the same night do not all look
    through the same lens.
    """
    if isinstance(day, datetime):
        day = day.date()
    if not isinstance(day, date):
        raise TypeError(f"lens_for needs a date, not {day!r}")
    lenses = cycle(override)
    offset = zlib.crc32((slug or "").encode("utf-8"))
    return lenses[(day.toordinal() + offset) % len(lenses)]


def goal_lens(lens: str) -> str:
    """The lens paragraph and the shared rules, as they go into the goal."""
    return PARAGRAPHS[lens] + "\n\n" + SHARED_RULES


def title(lens: str, name: str) -> str:
    return f"Nightly review ({lens}): {name}" if lens else f"Nightly review: {name}"


def filed_under(task_store, caller_task: str, key: str) -> str:
    """The lens of the nightly pass a filing on thread `key` comes from, or "".

    Only an ideation task the bot itself has running on that thread counts,
    as with research_parent: the run's own task id picks between them, and
    without one the ideator running there is it. A conversation turn on some
    other thread inherits nothing.
    """
    if not key:
        return ""
    running = [t for t in task_store.by_state("running")
               if t.get("thread") == key and t.get("role") == "ideator"
               and t.get("source") == "ideation"]
    named = [t for t in running if caller_task and t.get("id") == caller_task]
    pick = named or running
    lens = (pick[0].get("lens") or "") if pick else ""
    return lens if lens in LENSES else ""

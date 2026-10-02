"""Refuse a proposal the board already holds.

The nightly ideator and a review's follow-ups both file `proposed` tasks, and
both start from a fresh session that reads the code, not the board. Telling
them what is already open (scoping.board_note) helped, but a note is advice:
in the fourteen days to 2026-10-01, 226 of 228 proposals were accepted, and
twelve implementor runs finished with nothing to land because the work already
existed on the base. Acceptance was not filtering anything, so the filter has
to sit where the filing happens.

Deterministic on purpose: the same goal against the same board gives the same
answer, a refusal names the task it matched, and the threshold is a number that
was measured against the real board rather than a model's mood. Measured, it
errs toward filing -- a false positive throws away a real idea nobody will see
again, while a false negative costs one dismiss.
"""

import logging
import re
import time

import scoping
import tasks

log = logging.getLogger("silkworm.dedup")

#: Jaccard overlap of the two word sets at or above which a proposal is a
#: duplicate. Measured on 2026-10-02 against a copy of the live board: each of
#: the 166 historical proposals replayed against what its project held when it
#: was filed. At 0.30, 21 would have been refused and every one of those pairs
#: was the same job re-proposed. Below it the band is mixed -- 0.26 to 0.29
#: still holds real repeats, but also the first different jobs on the same
#: subsystem (a re-measure of the docs against a re-derivation of the config,
#: 0.26). A wrong refusal loses an idea; a missed one costs a dismiss. So 0.30.
THRESHOLD = 0.30

#: How long a finished task still counts. Long enough to cover the nights a
#: fix takes to reach the base the ideator reads; short enough that a job done
#: a month ago and regressed since can be proposed again.
RECENT_DAYS = 14

#: How much of a goal is compared. The opening says what the job is; the
#: rest is how. Measured: at 400 characters a re-proposal phrased differently
#: scored below two different jobs that open on the same file paths; at 700 the
#: two separated, and longer added nothing.
GOAL_CHARS = 700

#: Not open, but not something to raise again either -- finished, and recently.
FINISHED = (tasks.DONE,)

#: Words that say nothing about which job this is. Ordinary English, plus the
#: framing every proposal on this board carries whatever it asks for.
STOPWORDS = frozenset("""
a about above after again against all also an and any are as at be because
been before being below between both but by can could did do does doing done
down during each either else even ever every few for from further get gets had
has have having here how if in into is it its itself just less like make makes
may might more most much must my no nor not now of off on once one only or
other our out over own per rather same should since so some still such than
that the their them then there these they this those through to too under
until up upon us use used uses using very via was we were what when where
whether which while who whom why will with within without would yet you your
instead already new add adds added fix fixes fixed make change changes changed
ensure task tasks silkworm review found alongside check true anything
stop say said moved main tree read
""".split())

_REVIEW_LEAD = re.compile(r"^A review of \S+ \(.*?\) found this alongside that "
                          r"task, rather than in it:\s*", re.S)
_REVIEW_TAIL = re.compile(r"\s*Check it is still true before changing anything"
                          r".*$", re.S)
_TITLE_LEAD = re.compile(r"^(From review:|Rework of \S+:?)\s*", re.I)
_PATH = re.compile(r"(?:~|/)[\w./~-]+")
_WORD = re.compile(r"[a-z0-9_]+")


def gist(rec: dict) -> str:
    """The part of a task that says what the job is.

    A review follow-up's goal wraps its finding in two paragraphs that every
    follow-up shares; left in, they make any two follow-ups look alike and
    hide that a follow-up repeats an ideator proposal.
    """
    goal = rec.get("goal") or ""
    goal = _REVIEW_TAIL.sub("", _REVIEW_LEAD.sub("", goal))
    title = _TITLE_LEAD.sub("", rec.get("title") or "")
    # The title is the opening of the goal for every filer here, so it adds
    # nothing when it is a prefix -- only when someone named the task.
    head = goal[:GOAL_CHARS]
    return head if title and head.startswith(title.rstrip("…").strip()) \
        else f"{title}\n{head}"


def _stem(word: str) -> str:
    """Crude, but the same on both sides, which is all a comparison needs."""
    for suf in ("ing", "ed", "es", "s"):
        if len(word) > len(suf) + 3 and word.endswith(suf):
            return word[:-len(suf)]
    return word


def words(text: str) -> frozenset:
    """The normalised word set of a piece of text."""
    text = _PATH.sub(" ", (text or "").lower())
    return frozenset(_stem(w) for w in _WORD.findall(text)
                     if len(w) > 2 and w not in STOPWORDS and not w.isdigit())


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of two texts' word sets, 0.0 when either is empty."""
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def candidates(records, unmerged_ids=(), now: float | None = None,
               exclude=()):
    """Which of a project's tasks a new proposal must not repeat.

    Open work (anything not terminal), work finished on a branch that never
    merged, and work finished in the last RECENT_DAYS. Not dismissals: those
    are the board note's business, and a re-raised idea someone turned down is
    still an idea. Not the board's own machinery or conversation turns.
    """
    now = time.time() if now is None else now
    cutoff = now - RECENT_DAYS * 86400
    unmerged = set(unmerged_ids)
    for rec in records:
        if not scoping.is_work(rec) or rec.get("id") in exclude:
            continue
        state = rec.get("state")
        if (state in scoping.OPEN_STATES or rec.get("id") in unmerged
                or (state in FINISHED
                    and (rec.get("updated") or rec.get("created") or 0) >= cutoff)):
            yield rec


def find(goal: str, records, unmerged_ids=(), now: float | None = None,
         title: str = "", threshold: float = THRESHOLD, exclude=()):
    """The task this goal duplicates, as (record, score), or None.

    The closest match wins, so the refusal names the most similar task rather
    than the first one over the line.
    """
    new = gist({"goal": goal, "title": title})
    best = None
    for rec in candidates(records, unmerged_ids, now, exclude):
        score = similarity(new, gist(rec))
        if score >= threshold and (best is None or score > best[1]):
            best = (rec, score)
    return best


def refusal(match) -> str:
    """What a refused filer is told, naming the task it matched."""
    rec, _score = match
    return (f"duplicate of {rec.get('id')}: {scoping.task_name(rec)} "
            f"({rec.get('state')}) — not filed. If yours is different, say how "
            "in your reply rather than filing it again.")


def duplicate(goal: str, records, survey=None, title: str = "",
              now: float | None = None, exclude=()):
    """The refusal for a proposal the board already holds, or ''.

    `survey` is branches.survey, passed in so this module stays free of git.
    It asks git about every branch, and a git that will not answer must cost
    the unmerged half of the comparison, never the filing: an unknown is not a
    reason to refuse anything.
    """
    records = list(records)
    unmerged = ()
    if survey is not None:
        try:
            unmerged = [r.get("id") for r in survey(records)]
        except Exception:
            log.exception("unmerged survey failed; comparing open and recent only")
    match = find(goal, records, unmerged, now=now, title=title, exclude=exclude)
    return refusal(match) if match else ""

"""The operator: a head agent that keeps the routine pile moving.

It is not a router in front of every message. You talk to it in its own thread
(claimed with `!operator`) when you want coordination, and once a day it wakes
on its own, after the digest, to work the routine pile and post one message:
the morning's health brief, what it did, and what needs you. It is the
owner's chief of staff: health checks run just before it wakes, and it reads
them first -- stating the decisions that are the owner's, and filing at most
one fix proposal per (project, check) for the code work (health.py's
proposal_refusal holds it to that).

Three things here keep it honest, and none of them is the prompt.

Its context is rebuilt from live state on every turn (`context`): the projects
and their settings, `silkworm status --json`, recent landings, deploys, failing
bases and releases, and a short notes file it keeps itself. A session's history
remembers what was true when it was said; this says what is true now.

Its tools are an allowlist (roles.OPERATOR_TOOLS): the action CLI, a deploy
that waits for its own turn to end, filing and deferring, and read-only git. No
curl, so the bot's routes are reached only through bin/silkworm, which stamps
every request with the caller's role from the environment the bot set. A
prefix-matched allowlist leaves no way to put `env -u` or an assignment in
front of the command, so the stamp cannot be dropped.

And what it may not do is refused in code (`refusal`), by the CLI and again by
the bot, whatever it was told: deciding on proposals, reviewed work or research
findings either way (accepting, approving, dismissing, closing, digging),
cancelling work it does not own, releases, publishing where publish is off,
changing trader's live configuration, and deleting anything not proven redundant. Each refusal is
lifted for exactly one item by an approval you create with your own Slack
message (`!approve <what> <item>`), used once and gone within a day. A model
can ask you for one; it cannot make one -- no route writes them, only a Slack
message from a person does.

Or by saying so. A turn you start yourself, with a Slack message in its home
thread, carries a grant (`Grants`): a token the bot made for that one turn,
handed to its CLI in the environment and forgotten when the turn ends. While
it lives, the operator may do what you told it -- accept, dismiss, close,
dig, rework, land, drop, file, release -- on the items you named, each with
--notes saying what you decided (`said_refusal`), and the task records your
message and `decided_by`. A sweep, a wake-up, a deploy note or the web chat
starts no turn of yours and carries none. Publishing where it is off and
trader's live configuration still wait for `!approve`, and a running task is
cancelled only if your message named it.

Your word can be plain. Ops it proposes are recorded as a numbered pending
plan (`State.set_plan`), and a "yes" or "do 2" in your next message runs
those entries (`plan_approval`, `plan_refusal`). Work you ask it for, in so
many words, it files with `--requested-by-user`: past the open-proposal cap,
straight to the queue, with your message recorded on the task
(bot.requested_filing). Neither is open to a sweep, a wake-up or ideation.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import health
import jsonstore

log = logging.getLogger("silkworm.operator")

ROLE = "operator"

#: `source_ref` of the daily sweep task. Its source is the wake-up one, so it
#: runs in the wake lane like any other scheduled turn; this tells the two apart.
SWEEP_REF = "operator-sweep"
#: When the sweep runs if OPERATOR_SWEEP_AT is unset: after the digest's 08:00.
DEFAULT_SWEEP_AT = "08:30"
#: How long past its time the sweep waits for the digest before running
#: without it. A digest that fails to send must not silently cost the day's
#: sweep too.
DIGEST_GRACE_MIN = 180

#: The notes file it keeps, at most this long. Read into every turn, so it has
#: to stay a summary rather than become a log.
NOTES_MAX = 4096
#: The whole injected context, notes included, at most this many characters.
CONTEXT_MAX = 16000
#: How long an approval waits to be used.
APPROVAL_TTL_S = 24 * 3600
#: How long a pending plan of ops waits for your answer.
PLAN_TTL_S = 24 * 3600
#: At most this many ops in one plan: past that it is not a plan you can read
#: on a phone and say "yes" to.
MAX_PLAN = 10

# --- what it may not do --------------------------------------------------------

#: Guard -> what it covers, for refusals and `!approve` help. The keys are what
#: you type after `!approve`.
GUARDS = {
    "accept": ("deciding on a proposal, reviewed work or research findings -- accepting, "
               "approving, dismissing, closing or digging -- or dismissing or cancelling "
               "queued, blocked, failed or running work it does not own "
               "(those decisions are yours)"),
    "release": "starting a release (TestFlight, App Store, !release)",
    "publish": "pushing or publishing a project whose publish setting is off",
    "trader-config": "changing trader's live configuration",
    "delete": "deleting something not proven redundant",
}

#: The guards your own words lift, in a turn you started in its home thread
#: (`Grants`). Not publish or trader-config: those stay `!approve`-only.
SAID_GUARDS = frozenset({"accept", "release", "delete"})
#: Where a turn you started finds its grant: set by the bot for that turn
#: alone, sent back by bin/silkworm as `caller_grant`.
GRANT_VAR = "SILKWORM_USER_GRANT"
#: `result.decided_by` on a task the operator acted on because you said so.
DECIDED_BY = "user via operator"

#: Per project, the paths that are its live configuration: a branch touching
#: one of these is not the operator's to land. Prefixes end in "/".
LIVE_CONFIG = {
    "trader": ("scripts/paper_trade.py", "trader/config.py",
               "trader/research/deployed_universe.py",
               "data/research/liquid_universe.json", "ops/", ".env"),
}

#: /projects actions that only read.
_PROJECT_READS = frozenset({"list", "get", "unregistered"})


def caller_is_operator(payload: dict, home_thread: str = "") -> bool:
    """Whether a request to the bot comes from an operator turn.

    Two signals, either enough: the role the CLI read from the environment the
    bot set for the turn, and the thread it is running in. A run that somehow
    lost one still carries the other.
    """
    payload = payload or {}
    if str(payload.get("caller_role") or "") == ROLE:
        return True
    return bool(home_thread) and str(payload.get("caller_thread") or "") == home_thread


def refusal(guard: str, target: str) -> str:
    """The words a refusal is given in, naming the approval that lifts it."""
    return (f"refused for the operator: {GUARDS[guard]}. Ask the user to send "
            f"`!approve {guard} {target}` in Slack (it lifts this once), or to "
            "do it themselves")


def tasks_guard(action: str, payload: dict, task: dict | None, *,
                contained=None, live_paths=None) -> tuple[str, str] | None:
    """(guard, item) an operator's /tasks request needs approved, or None.

    `contained(task) -> (bool, why)` and `live_paths(task) -> [paths]` are
    asked only when the action needs them: both run git.
    """
    tid = str((payload or {}).get("id") or "")
    state = (task or {}).get("state")
    # A landing that carries a project's live configuration needs that
    # approved, before anything else: Approve lands (bot.approve_task), and so
    # does Land on work you never accepted. Answering "accept" first let an
    # `!approve accept` land trader's live config unasked.
    if action in ("land", "approve") and task and live_paths and live_paths(task):
        return "trader-config", tid
    if action in ("accept", "approve"):
        return "accept", tid
    # Every other way into the queue for work you never accepted is
    # accepting it: retry or rework on a proposal (proposed -> queued is
    # legal; cancelled -> queued is not, so a dismissed one cannot), and a
    # goal the operator wrote itself -- an edit, or a rework of anything but a
    # landing git refused, which is the one rework that is its to do.
    if task and state == "proposed" and action in ("retry", "rework", "edit"):
        return "accept", tid
    if action == "edit" and (payload or {}).get("goal") is not None:
        return "accept", tid
    # The opposite decisions are yours too. Closing research's parked
    # findings, or digging on them with a goal it wrote itself, is deciding
    # on them -- in any state, so the CLI can refuse it before asking. A
    # dismiss or cancel of what waits for you (a proposal, reviewed work,
    # findings) is final: cancelled -> queued is illegal, so nothing brings it
    # back. Its own proposals are the exception, as nobody else saw them.
    if action in ("close", "dig"):
        return "accept", tid
    # A retry of findings or reviewed work waiting on you reruns them unread:
    # a dig without the question, or a send-back of work you were to judge.
    if action == "retry" and state in ("awaiting_approval", "needs_input"):
        return "accept", tid
    if action in ("dismiss", "cancel") and task:
        if state == "proposed" and task.get("filed_by") == ROLE:
            return None
        if state in ("proposed", "awaiting_approval", "needs_input"):
            return "accept", tid
        # Everything else not yet finished is work you decided on, or are to:
        # queued is a proposal you accepted (dismissing it undoes the accept,
        # for good), blocked is an implementor waiting on its review, failed is
        # in front of you to judge, and cancelling a running task kills its
        # child (stop_task). Only its own turns are its to end.
        if (state in ("queued", "blocked", "failed", "running")
                and task.get("role") != ROLE):
            return "accept", tid
    if action == "rework" and task and not _landing_refused(task):
        return "accept", tid
    # Landing finished work is its job; landing anything else (a task you
    # dismissed from review, say) would be approving it.
    if action == "land" and task and state != "done":
        return "accept", tid
    if action == "create" and (payload.get("state") or "queued") != "proposed":
        # Filing straight into the queue is accepting your own proposal.
        return "accept", "new"
    if action == "release":
        return "release", tid
    if action == "drop" and task:
        ok, _why = contained(task) if contained else (False, "")
        if not ok:
            return "delete", tid
    return None


def said_refusal(action: str, payload: dict, task: dict | None, grant: dict,
                 live_paths=None) -> str:
    """Why a guarded request still may not go ahead on your own words, or ""
    if it may: the guard it hit is one SAID_GUARDS lifts, and `grant` is the
    live grant of the turn you started (Grants.get).

    It must say what you decided (--notes, recorded on the task); a running
    task is cancelled only if your message named it; and a landing -- Land,
    or Approve, which lands -- never carries a project's live configuration.
    """
    payload = payload or {}
    tid = str(payload.get("id") or "")
    if not str(payload.get("notes") or "").strip():
        return ("refused for the operator: acting on the user's word needs --notes "
                "quoting or paraphrasing what they decided")
    if (action in ("dismiss", "cancel") and (task or {}).get("state") == "running"
            and tid.lower() not in str(grant.get("text") or "").lower()):
        return (f"refused for the operator: {tid} is running, and cancelling it "
                "kills its child; the user's message has to name it")
    # Approving lands too (bot.approve_task).
    if action in ("land", "approve") and task and live_paths and live_paths(task):
        return refusal("trader-config", tid)
    return ""


class Grants:
    """The turns you started in the operator's home thread, while they run.

    In memory, in the bot, and nowhere else: a token per turn, made when your
    Slack message's turn starts and dropped when it ends. The turn's CLI is
    handed its token in the environment (SILKWORM_USER_GRANT) and sends it
    back; nothing else can have it. A restart forgets every one, which is
    right -- the turns went with it.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._live: dict[str, dict] = {}

    def open(self, key: str, ts: str, user: str, text: str, task_id: str,
             plan: dict | None = None) -> str:
        """`plan`: the pending plan your message answers (State.plan_before),
        snapshotted as the turn starts: one recorded during it was not one you
        had seen."""
        if not (key and ts and user and task_id):
            raise ValueError("a grant names its thread, message, person and turn")
        token = secrets.token_hex(16)
        with self._lock:
            self._live[token] = {"key": key, "ts": ts, "user": user,
                                 "text": text or "", "task": task_id, "at": time.time(),
                                 "plan": plan or None}
        return token

    def close(self, token: str) -> None:
        with self._lock:
            self._live.pop(token or "", None)

    def get(self, token: str, key: str, task_id: str) -> dict | None:
        """The live grant `token` names, if it is the turn of `task_id` in
        thread `key` -- both as the request says and as the grant recorded."""
        if not token:
            return None
        with self._lock:
            g = self._live.get(token)
            g = dict(g) if g else None
        if not g or not key or g["key"] != key or not task_id or g["task"] != task_id:
            return None
        return g


def decision_note(grant: dict, action: str, payload: dict) -> str:
    """The event a task gets when the operator acted on your word."""
    by = grant.get("user") or "the user"
    notes = " ".join(str((payload or {}).get("notes") or "").split())
    return f"{action} decided by {by} (Slack message {grant.get('ts')}) via operator: {notes}"


# --- your answer to a plan, in plain words ---------------------------------------
#
# On 2026-11-15 "go with your recommendations, and run the trader ops" was
# refused for all four of trader's ops: it named none of them. Now the operator
# records the ops it proposes as a numbered plan (State.set_plan), and your
# next message in its thread approves them by saying yes -- "yes", "go", "do
# it", "run them", "do 1 and 3", "all but 2". Read here, by rules a test pins
# down, never by a model: fail closed, so a question ("what does 2 do?"), a
# no, a wait, or a number that is not an entry approves nothing.

_SLACK_YES = re.compile(r":(?:\+1|thumbsup|white_check_mark|heavy_check_mark|ok_hand|"
                        r"ok)(?::skin-tone-\d:)?:")
_SLACK_EMOJI = re.compile(r":[a-z0-9_+'-]+:")
_ITEM = r"#?\d+(?:\s*(?:-|–|to)\s*\d+)?"
_ITEMS = (rf"(?:(?:entries|entry|items|item|numbers|number|steps|step|ops|op|no\.)\s*)?"
          rf"{_ITEM}(?:\s*(?:,|&|\band\b|\bor\b|\bplus\b)\s*{_ITEM})*")
#: "all but 2", "except 1 and 3", "not 4", "skip 2": numbers left out.
_EXCLUDE = re.compile(r"\b(?:but\s+not|but|except(?:\s+for)?|other\s+than|apart\s+from|"
                      rf"minus|skip(?:ping)?|without|not)\s+({_ITEMS})")
#: A request phrased as a question is still an approval: "can you run them?".
_ASKED_TO = re.compile(r"^(?:please\s+)?(?:can|could|would|will)\s+you\s+(?:please\s+)?"
                       r"(?:go\s+ahead|run|do|execute|proceed|approve)\b")
#: Words that say yes, or say to go ahead.
_AFFIRM = frozenset({
    "yes", "y", "yep", "yeah", "yea", "yup", "ya", "ok", "okay", "k", "kk", "sure",
    "go", "proceed", "approved", "approve", "lgtm", "do", "run", "execute", "fine",
    "agreed", "agree", "confirmed", "confirm", "absolutely", "definitely",
    "affirmative", "correct", "yess", "yesss"})
#: Words an approval may hold besides those: what it refers to, and how.
#: Anything else -- "later", "first", "except the trader ones", "file a task",
#: "thanks" -- means the message says more than yes, and it approves nothing:
#: the operator asks again rather than reading a yes into it.
_FILLER = frozenset({
    ",", "&", "and", "or", "plus", "to", "with", "for", "on", "of", "ahead", "the",
    "please", "pls", "just", "only", "also", "then", "now", "it", "them", "that", "this",
    "those", "these", "all", "both", "everything", "entry", "entries", "item", "items",
    "number", "numbers", "step", "steps", "op", "ops", "plan", "your", "recommendation",
    "recommendations", "recommended", "sounds", "good", "great", "perfect"})
_TOKEN = re.compile(r"#?\d+\s*(?:-|–|to)\s*\d+|#?\d+|[a-z]+(?:'[a-z]+)?|[,&]|[^\s.!]")


def _numbers(item_text: str) -> list:
    """The entry numbers in "1, 3-4 and #6"."""
    out = []
    for lo, hi in re.findall(r"#?(\d+)(?:\s*(?:-|–|to)\s*#?(\d+))?", item_text):
        a, b = int(lo), int(hi or lo)
        if b < a or b - a > 50:
            out.append(-1)                 # a range that makes no sense: nothing
        else:
            out.extend(range(a, b + 1))
    return out


def plan_words(entries) -> frozenset:
    """The words of a plan's own ops ("trader", "tailscale", "momentum",
    "pit"): an approval may name what it approves."""
    out = set()
    for e in entries or []:
        for part in [e.get("name") or "", *(e.get("args") or [])]:
            out.update(w for w in re.split(r"[^a-z0-9]+", str(part).lower()) if w.isalpha())
    return frozenset(out)


def plan_approval(text: str, count: int, words=()) -> tuple[frozenset, str]:
    """Which entries (1..count) of a pending plan your message approves, and,
    if none, why. Deterministic, and fail closed.

    Yes to all: "yes", "go", "ok", "do it", "run them", ":+1:", "go with your
    recommendations and run the trader ops". Some: "do 1 and 3", "yes, 2",
    "1-3", "just 2", "2". All but some: "all but 2", "yes except 1 and 3".
    Nothing: a question ("what does 2 do?"), a no, wait, hold, later or
    don't, a number that is not an entry, or any word beyond saying yes and
    naming entries or the plan's ops (`words`) -- "ok thanks", "do it in 2
    hours", "all except the trader ones", "yes, and file a task for X".
    """
    if count < 1:
        return frozenset(), "there is no plan"
    t = (text or "").lower().replace("\u2019", "'").replace("\u2018", "'")
    t = _SLACK_YES.sub(" yes ", t)
    t = _SLACK_EMOJI.sub(" ", t)
    t = t.replace("\U0001f44d", " yes ").replace("\u2705", " yes ")
    t = " ".join(t.split())
    t = re.sub(r"\bsounds?\s+(?:good|great|right|fine)\b", "yes", t)
    if not t:
        return frozenset(), "the message is empty"
    excluded = []
    for m in _EXCLUDE.finditer(t):
        excluded += _numbers(m.group(1))
    rest = " ".join(_EXCLUDE.sub(" ", t).split())
    asked = _ASKED_TO.match(rest)
    if "?" in rest and not asked:
        return frozenset(), "the message asks a question; it approves nothing"
    if asked:
        # "can you run them?" -- the request, without its question form.
        rest = re.sub(r"^(?:please\s+)?(?:can|could|would|will)\s+you\s+", "", rest)
    tokens = [tok for tok in _TOKEN.findall(rest) if tok not in ("?",)]
    known = _AFFIRM | _FILLER | set(words or ())
    other = sorted({tok for tok in tokens if not tok[-1].isdigit() and tok not in known})
    if other:
        return frozenset(), ("the message says more than yes ("
                             + ", ".join(f"{w!r}" for w in other[:4])
                             + "); it approves nothing -- ask them to answer the plan")
    selected = [n for tok in tokens if tok[-1].isdigit() for n in _numbers(tok)]
    # "all but 2": the exception says which, and "all" says yes to the rest.
    affirmed = any(tok in _AFFIRM for tok in tokens) or (
        bool(excluded) and any(tok in ("all", "everything", "them", "both") for tok in tokens))
    only_numbers = bool(selected) and not affirmed and all(
        tok[-1].isdigit() or tok in (",", "&", "and", "or", "plus", "just", "only")
        for tok in tokens)
    if not affirmed and not only_numbers:
        return frozenset(), "the message does not say yes or name entries"
    bad = sorted({n for n in selected + excluded if not 1 <= n <= count})
    if bad:
        named_ = ", ".join(str(n) for n in bad if n > 0) or "such entry"
        return frozenset(), f"the plan has entries 1-{count}; there is no {named_}"
    approved = (set(selected) if selected else set(range(1, count + 1))) - set(excluded)
    if not approved:
        return frozenset(), "the message leaves every entry out"
    return frozenset(approved), ""


def plan_refusal(grant: dict | None, n, name: str, args, live: dict | None) -> str:
    """Why entry `n` of the plan your message answered may not run as `name
    args`, or "" if it may. `live` is the pending plan now (State.plan): the
    grant's must still be it -- not expired, not replaced."""
    if not grant:
        return ("refused: running an op from a plan needs the user's answer -- a turn "
                "they started with their own Slack message in the operator's thread")
    gp = grant.get("plan") or None
    if not gp:
        return ("refused: no plan was waiting for the user's message (none was recorded "
                "before it, or an earlier message of theirs already answered it); record "
                "one (`ops plan`) and ask them, or have their message name the op")
    if not live or live.get("id") != gp.get("id"):
        return (f"refused: the plan the user answered ({gp.get('id')}) has expired or "
                "been replaced; ask them about the current one")
    if str(live.get("answered") or "") != str(grant.get("ts") or ""):
        return ("refused: the plan was answered by another of the user's messages, not "
                "this one; record it again and ask")
    try:
        n = int(n)
    except (TypeError, ValueError):
        return f"refused: --plan-entry takes an entry number, not {n!r}"
    entry = next((e for e in live.get("entries") or [] if e.get("n") == n), None)
    if not entry:
        return f"refused: the plan has no entry {n}"
    if entry.get("used"):
        return f"refused: entry {n} of the plan has already run"
    want = f"{entry['name']} {' '.join(entry.get('args') or [])}".strip()
    got = f"{name} {' '.join(args or [])}".strip()
    if entry["name"] != name or list(entry.get("args") or []) != [str(a) for a in args or []]:
        return (f"refused: entry {n} of the plan is `{want}`, not `{got}`; only exactly "
                "what the user was shown runs on their yes")
    ok, why = plan_approval(grant.get("text") or "", len(live.get("entries") or []),
                            plan_words(live.get("entries")))
    if n not in ok:
        said = " ".join(str(grant.get("text") or "").split())[:120]
        return (f"refused: the user's message ({said!r}) does not approve entry {n}"
                + (f": {why}" if why else f" (it approves {', '.join(map(str, sorted(ok)))})"))
    return ""


def plan_lines(rec: dict) -> list:
    """A plan's entries as the operator's reply numbers them."""
    return [f"{e['n']}. `{' '.join([e['name'], *e.get('args', [])])}`"
            + (" (ran)" if e.get("used") else "")
            for e in (rec or {}).get("entries") or []]


def _landing_refused(task: dict) -> bool:
    """Whether the task's last landing was tried against git and refused."""
    import merge
    return merge.needs_a_person(((task or {}).get("result") or {}).get("landing") or {})


def projects_guard(action: str, payload: dict) -> tuple[str, str] | None:
    """(guard, item) an operator's /projects request needs approved, or None."""
    payload = payload or {}
    slug = str(payload.get("slug") or payload.get("name") or "").strip().lower()
    if action in _PROJECT_READS:
        return None
    if slug in LIVE_CONFIG:
        return "trader-config", slug
    if action == "publish" and bool(payload.get("on")):    # as the route reads it
        return "publish", slug
    return None


def releases_guard(action: str, payload: dict) -> tuple[str, str] | None:
    """(guard, item) an operator's /releases request needs approved, or None."""
    if action == "start":
        return "release", str((payload or {}).get("slug") or "").strip().lower()
    return None


# --- approvals -----------------------------------------------------------------

_APPROVE = re.compile(r"^!approve(?:\s+(\S+))?(?:\s+(\S+))?\s*$", re.I)


def parse_approve(text: str) -> tuple[str, str, str]:
    """(guard, item, error) from an `!approve` message. Both empty to list."""
    m = _APPROVE.match((text or "").strip())
    if not m:
        return "", "", "usage: `!approve <what> <item>`"
    guard, item = (m.group(1) or "").lower(), m.group(2) or ""
    if not guard:
        return "", "", ""
    if guard not in GUARDS:
        return "", "", (f"`{guard}` is not something the operator is held back from; "
                        f"one of: {', '.join(GUARDS)}")
    if not item:
        return "", "", f"approve {guard} for what? `!approve {guard} <task id or project>`"
    return guard, item.strip("`").lower(), ""


#: Every read-modify-write of operator.json holds this: one writer's stale
#: copy (a sweep's mark, an approval used up) must never save over another's
#: -- least of all over a plan entry marked run, which would let it run twice.
_STATE_LOCK = threading.RLock()


class State:
    """operator.json: its home thread, the sweep's day, and your approvals.

    Read by the bot and, read-only, by the CLI -- which checks an approval
    before asking the bot to do something it would otherwise refuse.
    """

    def __init__(self, path, readonly: bool = False):
        self.path = Path(path)
        # The CLI reads beside the bot, which owns the file: it must never
        # repair, move or rewrite it (jsonstore's repair=False).
        self.readonly = readonly

    def _load(self) -> dict:
        data = jsonstore.load(self.path, default={}, strict=False,
                              repair=not self.readonly)
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        if self.readonly:
            raise PermissionError("a read-only view of operator.json cannot write it")
        jsonstore.save(self.path, data)

    # the home thread
    def thread(self) -> str:
        return str(self._load().get("thread") or "")

    def set_thread(self, key: str, by: str = "") -> None:
        with _STATE_LOCK:
            data = self._load()
            data.update(thread=key, claimed_by=by, claimed_at=time.time())
            self._save(data)

    # approvals
    def approve(self, guard: str, item: str, by: str, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        if guard not in GUARDS or not item or not by:
            raise ValueError("an approval names a guard, an item and who gave it")
        rec = {"guard": guard, "item": item, "by": by, "at": now, "used": None}
        with _STATE_LOCK:
            data = self._load()
            data["approvals"] = [a for a in self._live(data, now)] + [rec]
            self._save(data)
        return rec

    @staticmethod
    def _live(data: dict, now: float) -> list:
        return [a for a in data.get("approvals") or []
                if isinstance(a, dict) and not a.get("used")
                and now - float(a.get("at") or 0) < APPROVAL_TTL_S]

    def approvals(self, now: float | None = None) -> list:
        return self._live(self._load(), time.time() if now is None else now)

    def has(self, guard: str, item: str, now: float | None = None) -> bool:
        return any(a.get("guard") == guard and a.get("item") == item
                   for a in self.approvals(now))

    def consume(self, guard: str, item: str, now: float | None = None) -> bool:
        """Use one approval up. True if there was one to use."""
        now = time.time() if now is None else now
        with _STATE_LOCK:
            data = self._load()
            for a in self._live(data, now):
                if a.get("guard") == guard and a.get("item") == item:
                    a["used"] = now
                    self._save(data)
                    return True
        return False

    # the pending plan: ops it proposed, for you to answer in plain words
    def set_plan(self, entries, by: str, thread: str, task: str = "",
                 now: float | None = None) -> dict:
        """Record the ops the operator just proposed, numbered from 1, as the
        one pending plan: it replaces any earlier one. `entries` are
        [{name, args}], already checked by the caller (bot.handle_ops)."""
        now = time.time() if now is None else now
        if not entries or not by or not thread:
            raise ValueError("a plan names its ops, who recorded it and its thread")
        rec = {"id": "plan_" + secrets.token_hex(5), "at": now, "by": by,
               "thread": thread, "task": task,
               "entries": [{"n": i, "name": str(e["name"]),
                            "args": [str(a) for a in e.get("args") or []], "used": None}
                           for i, e in enumerate(entries, 1)]}
        with _STATE_LOCK:
            data = self._load()
            data["plan"] = rec
            self._save(data)
        return rec

    def plan(self, now: float | None = None) -> dict | None:
        """The pending plan, while it is live: recorded in the current home
        thread less than PLAN_TTL_S ago, and with an entry not yet run."""
        now = time.time() if now is None else now
        data = self._load()
        p = data.get("plan")
        if not isinstance(p, dict) or not p.get("entries"):
            return None
        if now - float(p.get("at") or 0) >= PLAN_TTL_S:
            return None
        if not data.get("thread") or p.get("thread") != data.get("thread"):
            return None
        if all(e.get("used") for e in p["entries"]):
            return None
        return p

    def plan_before(self, ts, now: float | None = None) -> dict | None:
        """The live plan, if it was recorded before the Slack message sent at
        `ts`: a message queued while the operator was still writing a plan
        was not an answer to it."""
        p = self.plan(now)
        try:
            sent = float(ts)
        except (TypeError, ValueError):
            return None
        if not p or float(p.get("at") or 0) >= sent:
            return None
        return {"id": p["id"], "at": p["at"],
                "entries": [{"n": e["n"], "name": e["name"], "args": list(e["args"])}
                            for e in p["entries"]]}

    def answer_plan(self, ts, now: float | None = None) -> dict | None:
        """plan_before(ts), for the message that answers the plan: the first
        message of yours after it was recorded, and only that one. Marks the
        plan answered by `ts`, so a later message -- "ok thanks", a yes to
        something else an hour on -- is no answer to it. The same message's
        turn asking again (a retry) gets it again. To be asked again, the
        operator records the plan again."""
        with _STATE_LOCK:
            snap = self.plan_before(ts, now)
            if not snap:
                return None
            data = self._load()
            answered = data["plan"].get("answered")
            if answered and answered != str(ts):
                return None
            if not answered:
                data["plan"]["answered"] = str(ts)
                self._save(data)
            return dict(snap, answered=str(ts))

    def use_plan_entry(self, plan_id: str, n: int, run_by: str = "",
                       now: float | None = None) -> bool:
        """Mark entry `n` of plan `plan_id` run, if that plan is still the live
        one and the entry has not run. True if it was claimed now."""
        now = time.time() if now is None else now
        with _STATE_LOCK:
            data = self._load()
            p = self.plan(now)
            if not p or p.get("id") != plan_id:
                return False
            for e in data["plan"]["entries"]:
                if e.get("n") == n:
                    if e.get("used"):
                        return False
                    e["used"] = now
                    e["used_by"] = run_by
                    self._save(data)
                    return True
        return False

    def release_plan_entry(self, plan_id: str, n: int) -> None:
        """Undo a claim whose run never started (the op was already running)."""
        with _STATE_LOCK:
            data = self._load()
            p = data.get("plan")
            if not isinstance(p, dict) or p.get("id") != plan_id:
                return
            for e in p.get("entries") or []:
                if e.get("n") == n:
                    e["used"] = None
                    e.pop("used_by", None)
                    self._save(data)
                    return

    # the sweep
    def sweep_due(self, now: datetime, at: str, digest_last_on: str = "",
                  digest_on: bool = True, grace_min: int = DIGEST_GRACE_MIN,
                  health_last_on: str = "", health_on: bool = False) -> bool:
        """Whether today's sweep should be filed now: its time has passed, it
        has not been filed today, and the digest and today's health pass have
        both finished first (each unless it is off). The wall-clock catch-up
        projects.due_for_ideation uses.

        If either still has not finished `grace_min` minutes after `at`, the
        sweep is due without it (see digest_missed), rather than skipped: a
        brief from yesterday's results, said to be, beats no sweep."""
        if not self.thread():
            return False
        today = now.strftime("%Y-%m-%d")
        if self._load().get("sweep_on") == today:
            return False
        if now.strftime("%H:%M") < at:
            return False
        waiting = ((digest_on and digest_last_on != today)
                   or (health_on and health_last_on != today))
        if waiting:
            return now.strftime("%H:%M") >= _plus_minutes(at, grace_min)
        return True

    @staticmethod
    def digest_missed(now: datetime, digest_last_on: str, digest_on: bool = True) -> bool:
        """Whether a sweep filed now runs without today's digest having posted."""
        return digest_on and digest_last_on != now.strftime("%Y-%m-%d")

    def mark_sweep(self, now: datetime, task_id: str) -> None:
        with _STATE_LOCK:
            data = self._load()
            data.update(sweep_on=now.strftime("%Y-%m-%d"), sweep_task=task_id)
            self._save(data)


def _plus_minutes(at: str, minutes: int) -> str:
    """`at` (HH:MM) plus `minutes`, held at 23:59 rather than wrapping into
    tomorrow -- so a late sweep time still gets its fallback the same day."""
    h, m = (int(x) for x in at.split(":"))
    total = min(h * 60 + m + max(0, int(minutes)), 23 * 60 + 59)
    return f"{total // 60:02d}:{total % 60:02d}"


def check(state: State, guard_item, now: float | None = None,
          consume: bool = False) -> str:
    """The refusal for a guarded request, or "" if it may go ahead.

    `consume`: the bot uses the approval up as it acts. The CLI only looks.
    """
    if not guard_item:
        return ""
    guard, item = guard_item
    ok = (state.consume(guard, item, now) if consume else state.has(guard, item, now))
    return "" if ok else refusal(guard, item)


# --- what it may do: drop a branch only once main holds its change ----------------

def _git(repo, *args, timeout: int = 60):
    try:
        r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def contained(repo, branch: str, base: str) -> tuple[bool, str]:
    """Whether everything `branch` changes is already on `base`, by content.

    Not patch-id alone: a branch landed by rebase carries new commits whose
    patches can differ (context moved), so `git cherry` shows it unmerged. The
    question asked is the one that matters -- would merging it change the
    base? -- answered by a merge computed in memory (git merge-tree, which
    writes no ref and touches no checkout). Nothing else counts: a commit
    patch-equivalent to one on the base proves nothing once that one has been
    reverted, and a merge that conflicts is not proven either way, so both
    are left for a person.
    """
    code, _ = _git(repo, "rev-parse", "--verify", "-q", f"{branch}^{{commit}}")
    if code:
        return False, f"no branch {branch}"
    code, base_tree = _git(repo, "rev-parse", "--verify", "-q", f"{base}^{{tree}}")
    if code:
        return False, f"cannot resolve base {base}"
    code, mb = _git(repo, "merge-base", base, branch)
    if code or not mb:
        return False, f"{branch} shares no history with {base}"
    code, _ = _git(repo, "diff", "--quiet", mb, branch)
    if code == 0:
        return True, "it changes nothing against its fork point"
    code, out = _git(repo, "merge-tree", "--write-tree", base, branch)
    if code == 0 and out.splitlines() and out.splitlines()[0] == base_tree:
        return True, f"merging it into {base} would change nothing"
    if code == 1:
        return False, f"it conflicts with {base}, so whether {base} holds it is not proven"
    return False, f"merging it into {base} would still change something"


def live_paths_touched(repo, branch: str, base: str, prefixes) -> list[str]:
    """The live-configuration paths `branch` changes against its fork point.

    Fails closed: if git cannot say what it changes, it is treated as touching
    them all, and the landing waits for your approval.
    """
    if not prefixes:
        return []
    # base...branch: what the branch changed since it forked, in one call, so
    # there is one way for git to fail and it fails closed.
    code, out = _git(repo, "diff", "--name-only", f"{base}...{branch}")
    if code:
        return list(prefixes)
    hit = []
    for path in out.splitlines():
        if any(path.startswith(p) if p.endswith("/") else path == p
               for p in prefixes):
            hit.append(path)
    return hit


# --- read-only git, vetted --------------------------------------------------------

#: What `silkworm operator git` runs. Raw git is not on the operator's
#: allowlist: `git log --output=<file>` (and diff, show) writes any file it is
#: given -- its own approvals, bin/silkworm -- and a prefix-matched allowlist
#: cannot exclude a flag. So the CLI runs these, after vetting the arguments.
GIT_READS = frozenset({"log", "diff", "show", "status", "rev-parse", "merge-base",
                       "cherry", "ls-files", "for-each-ref", "blame", "grep",
                       "shortlog", "describe", "cat-file", "rev-list"})
#: Options that write, or run something, refused with any abbreviation git
#: would accept for them (it takes unique prefixes of long options).
#: --remerge-diff re-runs each merge, with the repository's merge drivers.
_GIT_REFUSED = ("--output", "--ext-diff", "--textconv", "--open-files-in-pager",
                "--exec", "--remerge-diff")
#: Real options that merely look like an abbreviation of a refused one: git
#: takes an exact match over a prefix, so `--text` is -a, not --textconv.
_GIT_EXACT = frozenset({"--text"})
#: Options refused only with some values: (option, shortest abbreviation, the
#: values that run something). --diff-merges=remerge runs merge drivers;
#: --submodule=diff runs the submodule's own diff drivers, unflagged.
_GIT_VALUED = (("--diff-merges", "--diff-m", lambda v: v.startswith("r")),
               ("--submodule", "--sub", lambda v: v == "diff"))


def git_refusal(argv) -> str:
    """Why `silkworm operator git <argv>` may not run, or "" if it may."""
    if not argv:
        return "say which git command: " + ", ".join(sorted(GIT_READS))
    sub, args = argv[0], list(argv[1:])
    if sub == "worktree":
        if args[:1] != ["list"]:
            return "only `git worktree list`"
        args = args[1:]
    elif sub not in GIT_READS:
        return f"`git {sub}` is not a read-only command it may run"
    for i, a in enumerate(args):
        name, eq, value = a.partition("=")
        if name.startswith("--") and len(name) > 3 and name not in _GIT_EXACT and any(
                r.startswith(name) for r in _GIT_REFUSED):
            return f"`{a}` writes a file or runs a program"
        for opt, shortest, bad in _GIT_VALUED:
            if name.startswith(shortest) and opt.startswith(name):
                # --diff-merges takes its value as the next argument too.
                v = value if eq else (args[i + 1] if opt == "--diff-merges"
                                      and i + 1 < len(args) else "")
                if bad(v):
                    shown = a if eq else f"{a} {v}"
                    return f"`{shown}` runs the repository's own programs"
        if sub == "grep" and name.startswith("-O"):
            return f"`{a}` runs a program"
        if sub == "cat-file" and name.startswith("--fi") and "--filters".startswith(name):
            return f"`{a}` runs the repository's filter programs"
    return ""


#: Flags put straight after the subcommand. A repository's own config can name
#: a program per diff driver (diff.<driver>.textconv, diff.<driver>.command)
#: that log/show/blame run by default and diff runs as an external diff --
#: driver names are the repo's to choose, so `-c` cannot blank them all, and a
#: blank textconv makes `log -p` fail rather than skip it. These flags can.
_GIT_SAFE_FLAGS = {
    "diff": ("--no-ext-diff", "--no-textconv"),
    "log": ("--no-ext-diff", "--no-textconv"),
    "show": ("--no-ext-diff", "--no-textconv"),
    "blame": ("--no-textconv",),
    "grep": ("--no-textconv",),
}

#: Global options and config the repository cannot override: no lazy fetch
#: (a partial clone would run core.sshCommand to fetch a missing blob), no
#: signature checks (gpg.*program -- %G? formats, log.showSignature), and
#: config defaults that would otherwise turn a plain -m or diff into remerge
#: or submodule diffs.
_GIT_PINNED = ("--no-lazy-fetch", "-c", "core.fsmonitor=false", "-c", "diff.external=",
               "-c", "core.pager=cat", "-c", "gpg.program=false",
               "-c", "gpg.ssh.program=false", "-c", "gpg.x509.program=false",
               "-c", "log.showSignature=false", "-c", "log.diffMerges=separate",
               "-c", "diff.submodule=log")


def git_command(argv) -> list:
    """The command line `silkworm operator git` runs: no pager, and none of the
    programs a repository's config can name -- external diff, textconv,
    fsmonitor, gpg, a lazy fetch's ssh -- whatever that config says."""
    argv = list(argv)
    if argv:
        argv[1:1] = _GIT_SAFE_FLAGS.get(argv[0], ())
    return ["git", "--no-pager", *_GIT_PINNED, *argv]


# --- notes ---------------------------------------------------------------------

def read_notes(path) -> str:
    try:
        return Path(path).read_text()[:NOTES_MAX]
    except OSError:
        return ""


def write_notes(path, text: str) -> str:
    """Replace the notes. Returns "" or why not -- never truncates silently."""
    text = (text or "").strip()
    if len(text.encode()) > NOTES_MAX:
        return (f"notes are {len(text.encode())} bytes; the cap is {NOTES_MAX}. "
                "Keep a summary, not a log")
    jsonstore._write(Path(path), text + ("\n" if text else ""))
    return ""


# --- context, rebuilt from live state every turn -------------------------------

def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:max(0, n - 1)].rstrip() + "…"


def project_line(rec: dict, brief: str = "", excerpt: int = 300) -> str:
    scope = rec.get("scope") or {}
    settings = {
        "auto_merge": bool(rec.get("auto_merge")), "publish": bool(rec.get("publish")),
        "test_cmd": rec.get("test_cmd") or "", "generated": rec.get("generated") or [],
        "base": scope.get("branch") or "",
    }
    ready = rec.get("_unready") or "ready for unsupervised work"
    head = (f"- *{rec.get('slug')}* ({rec.get('title') or rec.get('slug')}) "
            f"{scope.get('cwd') or ''} — {ready}; {json.dumps(settings)}")
    if rec.get("main_failing"):
        head += f"; base failing its own tests: {json.dumps(rec['main_failing'])[:200]}"
    return head + (f"\n  brief: {_clip(' '.join(brief.split()), excerpt)}" if brief else "")


def recent_landings(records, now: float, window_s: float = 48 * 3600,
                    limit: int = 20) -> list[str]:
    """One line per landing attempt in the window, newest first."""
    rows = []
    for rec in records:
        landing = (rec.get("result") or {}).get("landing") or {}
        at = landing.get("at") or 0
        if not at or now - at > window_s:
            continue
        rows.append((at, f"- {rec.get('id')} [{rec.get('project') or '-'}] "
                         f"{landing.get('stage') or '?'}"
                         + (" (landed)" if landing.get("landed") else "")
                         + f": {_clip(landing.get('detail') or '', 160)}"))
    return [r for _, r in sorted(rows, reverse=True)[:limit]]


def tail(path, lines: int = 12) -> list[str]:
    try:
        return Path(path).read_text(errors="replace").splitlines()[-lines:]
    except OSError:
        return []


def context(project_recs, brief_for, status: dict | None, landings, deploys,
            holds: dict, released: dict, notes: str,
            budget: int = CONTEXT_MAX, health_rows=None, health_actions=None) -> str:
    """What an operator turn is told about the world, within `budget` chars.

    Sections in the order they matter. When the whole is over budget the
    later ones are cut first -- with a line saying so, never silently -- and
    the notes, which are its own memory, are kept whole (they are capped at
    NOTES_MAX already).
    """
    sections = []
    sections.append(("Your notes (keep them current with `operator notes --set`)",
                     notes.strip() or "(none yet)"))
    if status is None:
        sections.append(("silkworm status --json", "(could not be read this turn)"))
    else:
        sections.append(("silkworm status --json", json.dumps(status, separators=(",", ":"))))
    lines = []
    for rec in project_recs:
        try:
            brief = brief_for(rec) or ""
        except Exception:
            brief = ""
        lines.append(project_line(rec, brief))
    sections.append(("Projects", "\n".join(lines) or "(none)"))
    # Probe results Silkworm gathered (health.py): reason over these, do not
    # go fetching them. None is "could not be read", not "all healthy".
    sections.append(("Project health (probes Silkworm ran; non-ok checks in detail)",
                     "(could not be read this turn)" if health_rows is None
                     else health.context_section(health_rows, actions=health_actions)))
    sections.append(("Landings in the last two days", "\n".join(landings) or "(none)"))
    sections.append(("Bases failing their own tests (work held for them)",
                     "\n".join(f"- {s}: {json.dumps(v)[:240]}" for s, v in holds.items())
                     or "(none)"))
    sections.append(("Releases tagged in the last day",
                     "\n".join(f"- {s}: {', '.join(map(str, v))}" for s, v in released.items())
                     or "(none)"))
    sections.append(("Recent deploys (logs/deploy-after-turn.log)",
                     "\n".join(deploys) or "(none recorded)"))

    head = ("Live state, rebuilt for this turn. Trust it over anything earlier in "
            "the conversation.")
    out = [head]
    used = len(head)
    for i, (title, body) in enumerate(sections):
        block = f"\n\n## {title}\n{body}"
        if i == 0 or used + len(block) <= budget:
            out.append(block)
            used += len(block)
            continue
        omitted = [t for t, _ in sections[i + 1:]]
        said = f"\n\n(left out to fit the budget: {', '.join(omitted)})" if omitted else ""
        cut, mark = f"\n\n## {title}\n", "\n…(cut to fit the context budget)"
        room = budget - used - len(cut) - len(mark) - len(said)
        if room > 200:
            out.append(cut + body[:room] + mark)
        else:
            said = f"\n\n(left out to fit the budget: {', '.join([title] + omitted)})"
        out.append(said)
        break
    return "".join(out)[:budget]


# --- the sweep -----------------------------------------------------------------

SWEEP_GOAL = (
    "Morning sweep. Read Project health first, then work the routine pile, "
    "then report.\n\n"
    "Health, as chief of staff: every check that is not ok says under it "
    "what is being done (→). Where it says a fix may be proposed, file at "
    "most one proposal for it: `{bin} task --propose --health "
    "<project>/<check> \"<what to fix and what done looks like>\"` (its "
    "evidence is attached for you; the caps and dedup still apply, and a "
    "refusal is final for today). Where it says `decide:`, that is the "
    "user's, not code work: never file it, put it in Needs you as the "
    "decision. Never refile a check that already has a proposal.\n\n"
    "Routine, yours to do without asking: land or retry refused landings; drop "
    "branches `{bin} operator contained <id>` proves are already on their "
    "base; retry tasks that failed on quota or overload; rework conflicted "
    "landings onto current main; run-next whatever unblocks the most; tidy "
    "finished checkouts whose only changes are generated files; and, if "
    "Silkworm's main is ahead of the running revision and nothing is running, "
    "`{bin} deploy --after-turn`. Keep your notes current.\n\n"
    "Named ops (`{bin} ops list`): one that only reads -- `{bin} ops run "
    "tailscale-status`, a trader-refetch-revisions plan without --apply -- "
    "you may run when a check needs a closer look. One that changes something "
    "(tailscale-on, trader-launchd-reinstall, trader-launchd-retire, "
    "trader-env-set, a refetch with --apply, "
    "project-test, which runs a suite in the live checkout) is never run in a "
    "sweep: record the ones you propose as a plan (`{bin} ops plan <op> "
    "[args] [--op <op> [args]]… --by operator`), list them under Needs you "
    "numbered as it prints them, and say a plain \"yes\" in this thread "
    "runs them (\"do 2\" one, \"all but 1\" the rest) -- never ask the user "
    "to run a command themselves.\n\n"
    "Not yours, whatever you are asked: deciding on proposals, reviewed work "
    "or research findings either way -- accepting, approving, dismissing, "
    "closing or digging on them -- dismissing or cancelling a queued, blocked, "
    "failed or running task that is not your own, releases, publishing, "
    "trader's live configuration, deleting anything not proven redundant. Those are refused unless the user approved that one "
    "item -- ask them for it. This sweep is not a turn the user started, so "
    "nothing the user said earlier lifts them now.")

SWEEP_NOTE = (
    "This is your daily sweep, filed by the scheduler, not a message from the "
    "user. Silkworm puts a per-project health brief above your reply (one "
    "line per project, what changed, and the proposal or decision each "
    "problem has), so do not repeat it. Your reply is posted beneath it, in "
    "the same single message, and is read on a phone: first what you did "
    "(one short line per action, with task ids -- fix proposals you filed "
    "for health checks included), then *Needs you:* one line per item with "
    "the specific decision or question, or the exact `!approve …` to send "
    "(or that they can simply tell you in this thread). "
    "If nothing needs them, say so in one line. Do not schedule a wake-up "
    "for this.")


def one_message(text: str, limit: int) -> str:
    """The sweep's reply as exactly one Slack message of at most `limit` chars.

    A long reply would otherwise be chunked into several posts; the sweep
    promises one. What does not fit is cut with a line that says so, keeping
    the end -- where its questions for you are -- over the middle.
    """
    text = (text or "").strip() or "(the sweep said nothing)"
    if len(text) <= limit:
        return text
    mark = "\n…(cut to one message; the rest is in the task's record)…\n"
    keep_tail = min(len(text) // 2, (limit - len(mark)) // 2)
    keep_head = limit - len(mark) - keep_tail
    return text[:keep_head] + mark + text[len(text) - keep_tail:]


#: The share of the one message the health brief may take when the reply
#: needs the rest: half, so neither the brief nor what the operator did and
#: what needs you is ever cut to nothing by the other.
BRIEF_SHARE = 0.5


def _clip_lines(text: str, limit: int) -> str:
    """`text` cut at a line boundary to at most `limit` chars, saying how many
    lines were left out and where the rest is."""
    if len(text) <= limit:
        return text
    lines, out = text.splitlines(), []
    for i, line in enumerate(lines):
        mark = f"\n…({len(lines) - i} more line(s): `silkworm health`)"
        if len("\n".join(out + [line])) + len(mark) > limit:
            return ("\n".join(out) + mark)[:limit]
        out.append(line)
    return "\n".join(out)[:limit]


def compose_sweep(brief: str, reply: str, limit: int) -> str:
    """The sweep's one message: the health brief, then the operator's reply
    (what it did, then what needs you), within `limit` chars together.

    The brief gets what the reply does not need, and at least BRIEF_SHARE of
    the message; past that it is cut by whole lines, and the reply by
    one_message, which keeps its end -- where the questions are."""
    brief = (brief or "").strip()
    if not brief:
        return one_message(reply, limit)
    reply = (reply or "").strip() or "(the sweep said nothing)"
    room = max(int(limit * BRIEF_SHARE), limit - len(reply) - 2)
    head = _clip_lines(brief, min(room, limit - 200))
    return head + "\n\n" + one_message(reply, limit - len(head) - 2)


def post_sweep(client, channel: str, thread_ts: str, progress_ts, text: str,
               footer: str = "", limit: int = 3800, brief: str = "") -> int:
    """Post the sweep's report into its thread as exactly one message: the
    health brief (if any), then its reply.

    Into the "working…" placeholder when there is one (so the thread does not
    keep a stale placeholder beside the report); as a new message when there
    is none, or it could not be edited. Returns how many messages it posted
    or edited: 1, or 0 if Slack refused both.
    """
    body = compose_sweep(brief, text, max(400, limit - len(footer))) + footer
    if progress_ts:
        try:
            client.chat_update(channel=channel, ts=progress_ts, text=body)
            return 1
        except Exception:
            log.exception("operator: could not edit the sweep's placeholder; posting")
    try:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=body)
        return 1
    except Exception:
        log.exception("operator: could not post the sweep's report")
        return 0


def is_sweep(rec: dict) -> bool:
    return (rec or {}).get("source_ref") == SWEEP_REF

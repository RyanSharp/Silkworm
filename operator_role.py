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
message from a person does. You need never type one: your word lifts them all
(below).

Or by saying so. A turn you start yourself, with your message in its home
thread -- a Slack message, or one typed in the dashboard's chat that the
dashboard vouches is you (dashowner.verify: through tailscale serve, as
VIZ_TAILSCALE_LOGIN, from another tailnet device, never from the mini) --
carries a grant (`Grants`): a token the bot made for that one turn,
handed to its CLI in the environment and forgotten when the turn ends. While
it lives, the operator may do what you told it -- accept, dismiss, close,
dig, rework, land, drop, file, release -- on the items you named, each with
--notes saying what you decided (`said_refusal`), and the task records your
message, where it came from, and `decided_by`. A sweep, a wake-up, a deploy
note, or a dashboard message it cannot vouch for starts no turn of yours and
carries none. A running task is cancelled only if your message named it.

Publishing where it is off, and landing work that changes trader's live
configuration, your general word does not lift (PLAN_GUARDS): only your word
on that one item, once (`guard_lift`) -- an entry of its plan (`ops plan
--land tsk_X`, which shows the guard and the live files it changes) that
your answer approves, or your message naming the action and the item ("land
tsk_X"). The task records the plan id and entry, your message and where it
came from.

Your word can be plain. Ops it proposes are recorded as a numbered pending
plan (`State.set_plan`), and a "yes" or "do 2" in the message of yours that
answers it runs those entries (`plan_approval`, `plan_refusal`); a message
that is no answer to it ("file it") leaves it waiting (`plan_answer`). Work you ask it for, in so
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
import unicodedata
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
#: (`Grants`), on whatever items your message is about. Not publish or
#: trader-config: those are lifted item by item (PLAN_GUARDS).
SAID_GUARDS = frozenset({"accept", "release", "delete"})
#: The guards a turn you started lifts only for one named item, once: by an
#: entry of the plan your message said yes to (`ops plan --land/--approve/
#: --publish`), or by your message naming the action and the item ("land
#: tsk_b921b84c73", "publish cadence") -- `guard_lift`. Never in a sweep or a
#: wake-up, which carry no grant. `!approve <guard> <item>` still lifts them too.
PLAN_GUARDS = frozenset({"trader-config", "publish"})
#: A plan entry's action -> the guard it lifts. An entry lifts it for its own
#: action only: land is not approve (approving work still under review closes
#: it unlanded, a decision of its own).
GUARDED_ACTIONS = {"land": "trader-config", "approve": "trader-config",
                   "publish": "publish"}
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


def refusal(guard: str, target: str, action: str = "") -> str:
    """The words a refusal is given in, naming what lifts it.

    trader-config on a landing, and publish, are lifted by a plan entry the
    user says yes to, or by their message naming the action and the item: the
    refusal says so, and no longer asks them for `!approve` (which still
    works). trader-config on a project setting has no plan entry, and still
    names `!approve`."""
    if guard in PLAN_GUARDS and (GUARDED_ACTIONS.get(action) == guard
                                 or (guard == "publish" and not action)):
        verb = action if action in GUARDED_ACTIONS else "publish"
        return (f"refused for the operator ({guard}): {GUARDS[guard]}, and only the "
                f"user's word on this one item lifts it -- never a sweep, a wake-up or "
                f"your own judgement. Record it as a plan entry (`silkworm ops plan "
                f"--{verb} {target} --by operator`, alongside any ops), show the user "
                f"the numbered entry and what it changes, and carry it out in the turn "
                f"their answer starts (\"yes\", \"do 2\"); or in a turn whose message "
                f"names it (\"{verb} {target}\"). Either lifts it once")
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
    # A recommendation is a suggestion on the inbox card and nothing else: no
    # state moves and nothing runs, so it is the operator's to set, unasked.
    if action == "recommend":
        return None
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
    # Approving lands too (bot.approve_task). Your word in general never
    # lifts trader-config; guard_lift, for the one item, does.
    if action in ("land", "approve") and task and live_paths and live_paths(task):
        return refusal("trader-config", tid, action)
    return ""


# --- the guards your word lifts one item at a time --------------------------------
#
# On 2026-11-19 two trader landings (tsk_b921b84c73, tsk_e8758becd0) waited on
# a typed `!approve trader-config`: the one thing left that needed Slack. Now
# the operator records them as plan entries -- `ops plan --land tsk_X`, each
# showing the guard and the live-config files it changes -- and your answer
# ("yes", "do 2", from Slack or the dashboard) lifts exactly those entries,
# once; or your message names the action and the item ("land tsk_X").

#: Verbs that name a guarded action in your message, and the request actions
#: each names. "land"/"merge" is a landing; "approve" is approving reviewed
#: work (which lands it): not the same request -- approving work still under
#: review closes it unlanded -- so neither names the other.
_NAMED_VERBS = {"trader-config": {"land": ("land",), "merge": ("land",),
                                  "approve": ("approve",)},
                "publish": {"publish": ("publish",)}}
#: The only other words a clause naming an item may hold: asking and
#: joining. Any word beyond these, the verb and the items -- "think",
#: "should", "once", "why", "investigate", "failed" -- means the clause says
#: more than "do this to these", and it names nothing (as plan_approval
#: refuses an answer that says more than yes).
_NAMED_FILLER = frozenset({
    "please", "pls", "and", "also", "too", "now", "go", "ahead", "ok", "okay", "yes",
    "yeah", "yep", "sure", "just", "the", "task", "tasks", "branch", "work", "it"})
#: For publish only: "turn publish on for cadence", "turn on publishing for".
_PUBLISH_FILLER = frozenset({"turn", "switch", "on", "for", "in"})
#: A word of your message as names_action reads it: task ids and slugs whole.
_NAMED_TOKEN = re.compile(r"[a-z0-9_][a-z0-9_'-]*")
_TASK_ID = re.compile(r"tsk_[a-z0-9]+")


def names_action(text: str, guard: str, item: str, action: str = "") -> bool:
    """Whether your message names `item` for the guarded `action` (any of
    the guard's, if not given): some clause of it is nothing but the verb
    ("land", "merge", "approve"; "publish"), the items, and asking or joining
    words -- "land tsk_x", "please approve tsk_x and tsk_y", "go ahead and
    merge tsk_x", "turn publish on for cadence". Fail closed: any other word
    in the clause ("I don't think we should land tsk_x", "land tsk_x once the
    backtest passes", "land tsk_a and investigate tsk_x", "the landing of
    tsk_x failed"), a question, or a quoted line ("> land tsk_x") names
    nothing; a plan entry is the way to ask."""
    item = str(item or "").strip().lower()
    verbs = _NAMED_VERBS.get(guard) or {}
    if not item or not verbs:
        return False
    filler = _NAMED_FILLER | (_PUBLISH_FILLER if guard == "publish" else frozenset())
    for line in str(text or "").splitlines():
        if line.lstrip().startswith(">"):
            continue                      # quoted, not said
        for clause in re.split(r"[.;!]+(?:\s|$)|\bthen\b", normalise_answer(line)):
            if "?" in clause:
                continue
            toks = _NAMED_TOKEN.findall(clause)
            said = [t for t in toks if t in verbs]
            if (item not in toks or len(set(said)) != 1
                    or (action and action not in verbs[said[0]])):
                continue
            items = [t for t in toks if t not in verbs and t not in filler]
            if all(t == item or (guard != "publish" and _TASK_ID.fullmatch(t))
                   for t in items):
                return True
    return False


def guard_lift(grant: dict | None, guard: str, action: str, item: str,
               live: dict | None) -> tuple[dict | None, str]:
    """How your word lifts `guard` (a PLAN_GUARDS one) for `action` on
    `item`, or (None, why not). Checks only: the bot claims it, once
    (State.use_plan_entry / use_said_lift).

    `grant`: the live grant of the turn you started (none in a sweep or a
    wake-up). `live`: the pending plan now (State.plan). Lifted by an entry
    of that plan for this guard and item, if your message is the answer that
    approved it (plan_approval) -- or by your message naming the action and
    the item (names_action)."""
    item = str(item or "").strip().lower()
    if not grant:
        return None, refusal(guard, item, action)
    base = {"guard": guard, "item": item, "action": action,
            "ts": str(grant.get("ts") or ""), "via": grant_source(grant)}
    gp = grant.get("plan") or None
    if (gp and live and live.get("id") == gp.get("id")
            and str(live.get("answered") or "") == str(grant.get("ts") or "")):
        entries = live.get("entries") or []
        ok, _ = plan_approval(grant.get("text") or "", len(entries), plan_words(entries))
        for e in entries:
            if (e.get("guard") == guard and e.get("name") == action
                    and [str(a).lower() for a in e.get("args") or []] == [item]):
                if e.get("used"):
                    return None, (f"refused: entry {e['n']} of the plan (`{e['name']} "
                                  f"{item}`) has already been carried out; it lifts "
                                  f"{guard} once")
                if e["n"] in ok:
                    return dict(base, how="plan", plan_id=live["id"], entry=e["n"]), ""
    if names_action(grant.get("text") or "", guard, item, action):
        return dict(base, how="named"), ""
    said = " ".join(str(grant.get("text") or "").split())[:120]
    return None, (f"{refusal(guard, item, action)}. The user's message ({said!r}) neither "
                  f"approves a plan entry for {item} nor names it")


def lift_note(lift: dict | None) -> str:
    """How a guard was lifted, for the task's record."""
    if not lift:
        return ""
    if lift.get("how") == "plan":
        return (f"; {lift['guard']} lifted by entry {lift['entry']} of plan "
                f"{lift['plan_id']}")
    return f"; {lift['guard']} lifted by the message naming it"


class Grants:
    """The turns you started in the operator's home thread, while they run.

    In memory, in the bot, and nowhere else: a token per turn, made when your
    message's turn starts (Slack, or the dashboard vouching for you) and dropped when it ends. The turn's CLI is
    handed its token in the environment (SILKWORM_USER_GRANT) and sends it
    back; nothing else can have it. A restart forgets every one, which is
    right -- the turns went with it.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._live: dict[str, dict] = {}

    def open(self, key: str, ts: str, user: str, text: str, task_id: str,
             plan: dict | None = None, via: dict | None = None) -> str:
        """`plan`: the pending plan your message answers (State.plan_before),
        snapshotted as the turn starts: one recorded during it was not one you
        had seen. `via`: where your message came from (`provenance`) -- a
        Slack message unless the dashboard vouched for it."""
        if not (key and ts and user and task_id):
            raise ValueError("a grant names its thread, message, person and turn")
        token = secrets.token_hex(16)
        with self._lock:
            self._live[token] = {"key": key, "ts": ts, "user": user,
                                 "text": text or "", "task": task_id, "at": time.time(),
                                 "plan": plan or None, **provenance(via)}
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


#: Where your message came from: your own Slack message, or the dashboard's
#: chat when it vouched it was you (dashowner.verify).
VIA_SLACK, VIA_DASHBOARD = "slack", "dashboard"


def provenance(via: dict | None) -> dict:
    """A grant's provenance fields: {"via": "slack"}, or {"via": "dashboard",
    "login", "node", "peer"} for a message the dashboard vouched for."""
    via = via or {}
    if via.get("via") != VIA_DASHBOARD:
        return {"via": VIA_SLACK}
    return {"via": VIA_DASHBOARD, "login": str(via.get("login") or ""),
            "node": str(via.get("node") or ""), "peer": str(via.get("peer") or "")}


def grant_source(grant: dict | None) -> str:
    """Your message, as the record names it: "Slack message <ts>", or
    "dashboard message <ts> (<login> on <device>, <peer>)"."""
    g = grant or {}
    ts = g.get("ts") or g.get("decided_ts") or "?"
    if g.get("via") != VIA_DASHBOARD:
        return f"Slack message {ts}"
    where = ", ".join(x for x in (g.get("node"), g.get("peer")) if x)
    return (f"dashboard message {ts} ({g.get('login') or '?'}"
            + (f" on {where}" if where else "") + ")")


#: The fewest words of your message --notes must quote for a filing made on
#: your request (`request_quote_refusal`), unless the message is shorter.
QUOTE_MIN_WORDS = 4

_QUOTED = re.compile(r'"([^"]+)"|\u201c([^\u201d]+)\u201d|\u2018([^\u2019]+)\u2019|'
                     r"(?<![a-z0-9])'([^']+)'(?![a-z0-9])", re.I)


def _words(text: str) -> list:
    return re.findall(r"[a-z0-9]+", str(text or "").lower().replace("\u2019", "'"))


def _within(needle: list, hay: list) -> bool:
    n = len(needle)
    return any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))


def request_quote_refusal(notes: str, text: str) -> str:
    """"" when --notes for a `task --requested-by-user` filing quotes your
    message (the grant's text), or why not.

    The notes are the record of what you asked for, so they must come from
    what you said, not what the operator says you said: the whole of them, the
    part after a leading label ("user: ..."), or a quoted part, has to appear
    word for word (case, spacing and punctuation aside) in your message -- at
    least QUOTE_MIN_WORDS words of it, or all of it if it is shorter."""
    said = _words(text)
    if not said:
        return "the user's message has no words to quote"
    need = min(QUOTE_MIN_WORDS, len(said))
    notes = str(notes or "")
    candidates = [notes]
    if ":" in notes:
        candidates.append(notes.split(":", 1)[1])
    candidates += [next(g for g in m.groups() if g) for m in _QUOTED.finditer(notes)]
    for c in candidates:
        w = _words(c)
        if len(w) >= need and _within(w, said):
            return ""
    return (f"--requested-by-user needs --notes quoting the user's own message, word for "
            f"word (at least {need} words of it); it does not contain what the notes say")


#: How many of your message's own words the goal of a filing made on your
#: request must share (`request_goal_refusal`), unless it has fewer.
GOAL_MIN_SHARED = 2

#: Words that say nothing about *what* was asked: filler, the asking and
#: filing itself ("please file a task to ..."), and saying yes or pointing at
#: something already on the table ("sounds good, go ahead with the first
#: suggested one"), so they cannot be what ties a goal to your message -- nor
#: what a goal must repeat to be filed on it.
_GOAL_FILLER = frozenset("""
    a an the and or but nor so if then than that this these those there here it its
    to of in on at by for from with without into onto about over under up down out off
    as is are was were be been being am do does did done doing have has had having
    will would shall should can could may might must not no yes ok okay please pls
    i me my mine we us our you your yours he she they them their his her
    what which who whom whose when where why how all any some each every both
    just also too very really now today tonight tomorrow later soon again still
    file filed filing task tasks ticket proposal propose queue queued ask asked asking
    want wants wanted need needs needed get got let make made go going thing things
    user operator one two new
    isn aren wasn weren doesn don didn won wouldn couldn shouldn hasn haven hadn
    re ve ll im id
    ahead sounds sound good great perfect sure fine nice cool awesome thanks thank
    yeah yep yup alright right lgtm agreed agree approve approved
    first second third fourth fifth last next previous above below earlier latter former
    proposed suggest suggested suggestion suggestions
    recommend recommended recommendation recommendations
    idea ideas option options
""".split())

_SUFFIX = re.compile(r"(?:ies|es|ed|ing|s)$")


def _stem(w: str) -> str:
    """A short stem, so "flaky" and "flakiness", "fix" and "fixes", or "bug"
    and "bugs" count as the same word."""
    if len(w) > 4:
        w = _SUFFIX.sub("", w) or w
    elif len(w) == 4 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return w[:4]


def _filler(w: str) -> bool:
    """Filler, or another form of a filler word ("proposals", "approves",
    "recommends") -- its inflection cut, at full length, then with an "e"
    back on. Not `_stem`: cut to four letters the filler would swallow real
    words that merely start alike ("perfect" -> "performance", "recommended"
    -> "recovery"); and only filler of five letters or more has other forms
    here, or "noted" would be "not", "cooling" "cool" and "mining" "mine"."""
    if w in _GOAL_FILLER:
        return True
    root = _SUFFIX.sub("", w)
    return root != w and any(len(r) >= 5 and r in _GOAL_FILLER for r in (root, root + "e"))


def _content(words: list) -> set:
    """Your words that name something (two-letter ones too: CI, UI, DB),
    stemmed; filler in any of its forms, the asking and filing words,
    numbers and the pieces of a contraction ("isn't" -> isn, t) are not."""
    return {_stem(w) for w in words
            if len(w) >= 2 and not _filler(w) and not w.isdigit()}


def request_goal_refusal(goal: str, text: str) -> str:
    """"" when the goal of a `task --requested-by-user` filing is about what
    your message (the grant's text) asked for, or why not.

    The quote rule (request_quote_refusal) proves the notes are your words;
    it says nothing of the goal beside them, so a real quote could carry an
    unrelated goal past the proposal cap. The goal must share at least
    GOAL_MIN_SHARED of your message's own words (or all of them, if it has
    fewer) -- a floor, read by a rule a test pins down, not proof: the
    operator still writes the goal, and the task records your quote for you
    to hold it to."""
    said = _content(_words(text))
    if not said:
        return ""  # nothing of yours to hold the goal to: the quote rule decides
    need = min(GOAL_MIN_SHARED, len(said))
    shared = said & _content(_words(goal))
    if len(shared) >= need:
        return ""
    return (f"--requested-by-user files what the user asked for: the goal must be about "
            f"their message, sharing at least {need} of its own words, and it shares "
            f"{len(shared)}; file it as a proposal (the cap applies) or ask them")


def decision_note(grant: dict, action: str, payload: dict) -> str:
    """The event a task gets when the operator acted on your word."""
    by = grant.get("user") or "the user"
    notes = " ".join(str((payload or {}).get("notes") or "").split())
    return f"{action} decided by {by} ({grant_source(grant)}) via operator: {notes}"


# --- your answer to a plan, in plain words ---------------------------------------
#
# On 2026-11-15 "go with your recommendations, and run the trader ops" was
# refused for all four of trader's ops: it named none of them. Now the operator
# records the ops it proposes as a numbered plan (State.set_plan), and your
# message in its thread that answers it approves them by saying yes -- "yes",
# "go", "do it", "run them", "do 1 and 3", "all but 2" (plan_answer: a message
# that is no answer leaves it waiting). Read here, by rules a test pins
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
#: Words that say no or not now. ("but not 2" is read as an exception
#: before these are looked for, so it never reaches them.)
_NEGATE = frozenset({
    "no", "nope", "nah", "not", "don't", "dont", "never", "wait", "hold", "stop",
    "cancel", "later", "nevermind", "nvm", "negative", "n"})
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


# How a phone types it. On 2026-11-16 the answer 'for trader… “yes”' was
# refused for every entry: iOS had curled the quotes and folded the three
# dots into an ellipsis, and the user had said which plan they meant. So the
# message is normalised first (normalise_answer) and may open by naming the
# plan (strip_lead_in) -- still by rules, still failing closed.

#: Emoji that say yes, read as the word; skin tones and variation selectors
#: are dropped first, so 👍🏽 and ✔️ count.
_EMOJI_YES = ("\U0001f44d", "\u2705", "\u2714", "\u2611", "\U0001f44c", "\U0001f197",
              "\U0001f4af")
#: Emoji that decorate a message without saying anything else.
_EMOJI_NOISE = ("\U0001f64f", "\U0001f642", "\U0001f60a", "\U0001f600", "\U0001f604",
                "\U0001f680", "\U0001f525", "\U0001f389", "\u263a")
#: Slack shortcodes that ask or say no, read as words so they refuse, and
#: ones that only decorate. Any other shortcode is left in the message, and
#: refuses: an emoji we cannot read is not a yes.
_SLACK_WORDS = {
    "x": " no ", "-1": " no ", "thumbsdown": " no ", "no_entry": " no ",
    "no_entry_sign": " no ", "octagonal_sign": " no ", "stop_sign": " no ",
    "raised_hand": " wait ", "hand": " wait ", "question": " ? ", "grey_question": " ? ",
    "thinking_face": " ? ", "eyes": " ? "}
_SLACK_NOISE = frozenset({
    "pray", "tada", "rocket", "fire", "smile", "slightly_smiling_face", "blush",
    "grinning", "raised_hands", "muscle", "sunglasses"})
#: Emoji that ask or say no: kept as words, so they refuse.
_EMOJI_WORDS = {"\u2753": " ? ", "\u2754": " ? ", "\U0001f914": " ? ",
                "\U0001f44e": " no ", "\u274c": " no ", "\U0001f6d1": " no ",
                "\u270b": " wait "}
_EMOJI_MODIFIERS = re.compile("[\U0001f3fb-\U0001f3ff\ufe0e\ufe0f\u200d\u200b\u200c\u2060\ufeff\u20e3]")


def _slack_word(m) -> str:
    name = m.group(0).strip(":")
    if name in _SLACK_WORDS:
        return _SLACK_WORDS[name]
    return " " if name in _SLACK_NOISE else f" {name} "


def normalise_answer(text: str) -> str:
    """Your answer as a keyboard without autocorrect would have typed it:
    lower case, straight apostrophes, no quotation marks, dots for an
    ellipsis, a comma for a dash between words (a hyphen between numbers, so
    "1–3" stays a range), approving emoji as "yes", single spaces, and no
    punctuation trailing the end. Nothing here can turn a refusal into a yes:
    a "?" or a word it does not know survives it."""
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = _EMOJI_MODIFIERS.sub("", t)
    t = _SLACK_YES.sub(" yes ", t)
    t = _SLACK_EMOJI.sub(_slack_word, t)
    for e in _EMOJI_YES:
        t = t.replace(e, " yes ")
    for e in _EMOJI_NOISE:
        t = t.replace(e, " ")
    for e, word in _EMOJI_WORDS.items():
        t = t.replace(e, word)
    # Apostrophes and single quotes: an apostrophe inside a word ("don't")
    # stays one; a quote around one ("'yes'") goes.
    t = re.sub("[\u2018\u2019\u201a\u201b\u2032`\u00b4]", "'", t)
    t = re.sub(r"(?<![a-z])'|'(?![a-z])", " ", t)
    t = re.sub("[\"\u201c\u201d\u201e\u201f\u2033\u00ab\u00bb\u2039\u203a]", " ", t)
    # Dashes: a range between numbers, a pause between words.
    t = re.sub(r"(?<=\d)\s*[\u2010-\u2015\u2212-]\s*(?=#?\d)", "-", t)
    # Before a number, though, a dash may mean "minus" ("all - 2"): it stays,
    # and refuses.
    t = re.sub(r"(?:[\u2010-\u2015\u2212]|(?<!\S)-(?!\S)|(?<!\S)--+|--+(?!\S))(?!\s*#?\d)",
               " , ", t)
    t = t.replace(";", ",")
    t = re.sub(r":(?=\s)", " ,", t)
    t = " ".join(t.split())
    t = re.sub(r"(?:\s*[.!,:~])+$", "", t)
    t = re.sub(r"^(?:[.!,:~]\s*)+", "", t)
    return t


#: "for trader", "re the ops", "trader:", "about the trader ops -- ": a
#: short opening naming what the answer is about. The subject must be the
#: plan's own (its op words) or the plan itself; a lead-in naming anything
#: else is left in place, and so refuses.
_LEAD_PREP = r"(?:(?:for|re|about|regarding|as\s+for|on)\s+)"
#: No dash: the normaliser has already made a dash between words a comma,
#: so a dash that reaches here stands before a number and may mean "minus"
#: ("the plan - 2"); stripping through it would read "2". It stays, and refuses.
_LEAD_SEP = r"(?:\s*(?::|,|\.{2,})\s*|\s+)"
_LEAD_GENERIC = ("ops", "op", "plan", "ones")


def strip_lead_in(t: str, words=()) -> str:
    """`t` (normalised) without a lead-in naming the plan it answers, or
    unchanged when it has none. At most three subject words, and something
    must follow."""
    subject = sorted({w for w in words if w.isalpha() and w not in _NEGATE
                      and w not in _AFFIRM} | set(_LEAD_GENERIC), key=len,
                     reverse=True)
    if not subject:
        return t
    subj = "(?:" + "|".join(re.escape(w) for w in subject) + ")"
    m = re.match(rf"^{_LEAD_PREP}?(?:the\s+)?{subj}(?:\s+{subj}){{0,2}}{_LEAD_SEP}(?=\S)", t)
    if not m:
        return t
    head = m.group(0)
    # Without "for/re/..." or a separator, a bare subject word is just a
    # word of the answer ("trader ops yes"): leave it for the filler rules.
    if not re.match(_LEAD_PREP, head) and not re.search(r"[:,.]\s*$", head):
        return t
    return t[m.end():]


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
    Phone typing is normalised first (normalise_answer) and a lead-in naming
    the plan is dropped (strip_lead_in): 'for trader\u2026 \u201cyes\u201d' is a yes.
    """
    if count < 1:
        return frozenset(), "there is no plan"
    t = strip_lead_in(normalise_answer(text), words)
    t = re.sub(r"\bsounds?\s+(?:good|great|right|fine)\b", "yes", t)
    if not t:
        return frozenset(), "the message is empty"
    if re.search(r"\d\s*\.+\s*#?\d", t):
        # "1.2", "1...3": a range, a list, or a decimal -- not for us to guess.
        return frozenset(), "the message joins numbers with dots; say which entries plainly"
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
    said_no = sorted(set(tokens) & _NEGATE)
    if said_no:
        return frozenset(), (f"the message says no or not yet ({', '.join(map(repr, said_no))});"
                             " it approves nothing")
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


# Is it an answer? On 2026-11-17 the operator recorded plan_4d88248842 and
# the user's next message was "file it" -- another request. That message used
# the plan up, and their "yes" after it was refused. Now only a message that
# answers the plan spends it: one that approves entries (plan_approval) or
# says no to it (plan_declines). Anything else -- "file it", "what does 3
# do?", "thanks" -- leaves it waiting for the next one.

#: Words that turn a plan down, or put it off, besides _NEGATE's. Any of
#: them in a message that approves no entry makes it a decline, and spends
#: the plan: a no fails toward spent, never toward still runnable. "no,
#: don't run 3", "absolutely not", "stop the trader" (a request that only
#: sounds like no) all spend it -- the operator records it again and asks.
_DECLINE = _NEGATE | frozenset({
    "nah", "skip", "pass", "scrap", "forget", "abort", "decline", "declined",
    "cancelled", "canceled", "won't", "wont", "can't", "cant", "shouldn't",
    "shouldnt", "mustn't", "neither", "nor", "nothing"})
#: Words that acknowledge as readily as they approve. Once a message has
#: passed a plan by without answering it ("file it"), the operator's reply
#: is about that, and your "ok" or "sure" after it is about that too: these
#: alone answer the plan no longer. "yes", "go", "run them", entry numbers
#: still do.
_ACK = frozenset({"ok", "okay", "k", "kk", "sure", "fine", "correct", "agreed",
                  "agree", "confirmed", "confirm"})


def plan_declines(text: str, words=()) -> bool:
    """Whether your message says no to the plan, or not now: it holds a
    word of _DECLINE -- "no", "not now", "skip it", "cancel the plan",
    "don't", "wait", "later", 👎 (read as "no" by normalise_answer).
    Called only for a message that approves no entry (plan_answer), and
    loose by design: anything that sounds like no spends the plan."""
    return bool(set(_TOKEN.findall(normalise_answer(text))) & _DECLINE)


def _only_acknowledges(text: str, words=()) -> bool:
    """Whether the yes in your message is only a word that acknowledges
    anything (_ACK): "ok", "sure thanks", 👍 -- no number, no "yes", "go" or
    "run". "sounds good" acknowledges too. An approving emoji is Slack's
    commonest acknowledgement, so here it reads as "ok", not "yes"."""
    t = unicodedata.normalize("NFKC", text or "")
    t = _SLACK_YES.sub(" ok ", _EMOJI_MODIFIERS.sub("", t.lower()))
    for e in _EMOJI_YES:
        t = t.replace(e, " ok ")
    t = strip_lead_in(normalise_answer(t), words)
    tokens = set(_TOKEN.findall(t))
    if any(tok[-1].isdigit() for tok in tokens):
        return False
    return not (tokens & (_AFFIRM - _ACK))


def plan_answer(text: str, entries, passed: bool = False) -> str:
    """"approve" if your message approves any entry of the plan, "decline"
    if it says no to it (plan_declines), else "": it is no answer to the
    plan (a question, another request), which keeps waiting for one.
    `passed`: a message has already passed the plan by unanswered, so a
    bare acknowledgement ("ok", "sure") no longer approves it."""
    words = plan_words(entries)
    approved, _ = plan_approval(text, len(entries or []), words)
    if approved and not (passed and _only_acknowledges(text, words)):
        return "approve"
    return "decline" if plan_declines(text, words) else ""


def plan_refusal(grant: dict | None, n, name: str, args, live: dict | None) -> str:
    """Why entry `n` of the plan your message answered may not run as `name
    args`, or "" if it may. `live` is the pending plan now (State.plan): the
    grant's must still be it -- not expired, not replaced."""
    if not grant:
        return ("refused: running an op from a plan needs the user's answer -- a turn "
                "they started with their own message (Slack or the dashboard) in the "
                "operator's thread")
    gp = grant.get("plan") or None
    if not gp:
        return ("refused: no plan was waiting for the user's message (none was recorded "
                "before it, an earlier message of theirs already answered it, or this "
                "one is no answer to it -- it neither says yes or entry numbers nor "
                "says no); record one (`ops plan`) and ask them, or have their message "
                "name the op")
    if not live or live.get("id") != gp.get("id"):
        return (f"refused: the plan the user answered ({gp.get('id')}) has expired or "
                "been replaced; ask them about the current one")
    if str(live.get("answered") or "").startswith("dashboard:"):
        return ("refused: the user answered this plan with its card on the dashboard, and "
                "the bot ran what they chose there (`silkworm ops plan` shows each "
                "entry's outcome); there is nothing left for you to run")
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
    if entry.get("guard"):
        item = " ".join(entry.get("args") or [])
        return (f"refused: entry {n} of the plan is `{entry['name']} {item}`, not an op: "
                f"carry it out with `silkworm {entry['name']} {item} --by operator "
                "--notes …`")
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
    """A plan's entries as the operator's reply numbers them: a guarded
    action with the guard it needs and what it changes live."""
    return [f"{e['n']}. `{' '.join([e['name'], *e.get('args', [])])}`"
            + (f" -- needs {e['guard']}: {e.get('summary') or ''}".rstrip(": ")
               if e.get("guard") else "")
            + (" (done)" if e.get("used") and e.get("guard") else
               " (ran)" if e.get("used") else "")
            # Run from the dashboard's card: what it came to.
            + ({"ok": " -- ok", "failed": " -- failed"}.get(
                (e.get("result") or {}).get("state"), ""))
            for e in (rec or {}).get("entries") or []]


#: What each guarded plan entry is, said plainly, for the dashboard's card.
_GUARDED_PLAIN = {"land": "Land this task's branch into its base",
                  "approve": "Approve this task, which lands its branch",
                  "publish": "Turn publishing on for this project"}


def _op_changes(name: str, args) -> str:
    """What running op `name` with `args` changes, in a line."""
    args = [str(a) for a in args or []]
    if name == "trader-env-set" and len(args) == 2:
        return (f"sets {args[0]}={args[1]} in trader's .env (backed up first); "
                "the trading daemon reads it at its next restart")
    if name == "trader-refetch-revisions":
        return ("writes Yahoo's revised bars into trader's store (backed up first)"
                if "--apply" in args else "writes nothing: it reports a plan")
    if name.startswith("trader-launchd"):
        return f"trader's installed launchd job {args[0] if args else '?'}"
    if name == "tailscale-on":
        return "what this machine serves to the tailnet (never Funnel)"
    if name == "project-test":
        return f"nothing: it runs {args[0] if args else 'a project'}'s test suite"
    return "something on this machine"


def plan_card(view: dict | None, descriptions: dict | None = None) -> dict | None:
    """The dashboard's card for a plan (State.plan_view): its entries
    numbered, each with the command, a plain description and what it
    changes, whether it has run and what it came to; what can be pressed
    (`runnable`: only while it waits for its answer, and only the entries
    not yet run). `descriptions`: op name -> its plain description
    (ops.describe)."""
    if not view:
        return None
    descriptions = descriptions or {}
    status = view.get("status") or ""
    entries = []
    for e in view.get("entries") or []:
        args = [str(a) for a in e.get("args") or []]
        guard = e.get("guard") or ""
        entries.append({
            "n": e.get("n"), "command": " ".join([str(e.get("name") or "?"), *args]),
            "description": (_GUARDED_PLAIN.get(e.get("name"), "") if guard
                            else descriptions.get(e.get("name"), "")),
            "changes": (e.get("summary") or "") if guard else _op_changes(e.get("name"), args),
            "guard": guard, "used": bool(e.get("used")),
            "chosen": e.get("n") in (view.get("chosen") or []),
            "result": e.get("result") or None})
    return {"id": view.get("id") or "", "thread": view.get("thread") or "",
            "at": view.get("at") or 0, "expires_at": view.get("expires_at") or 0,
            "status": status, "runnable": status == "waiting",
            "answered_via": view.get("answered_via") or ("message" if view.get("answered") else ""),
            "answered_by": view.get("answered_by") or "",
            "declined": bool(view.get("declined")), "entries": entries}


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
               "entries": [dict({"n": i, "name": str(e["name"]),
                                 "args": [str(a) for a in e.get("args") or []],
                                 "used": None},
                                **({"guard": str(e["guard"]),
                                    "summary": str(e.get("summary") or "")[:300]}
                                   if e.get("guard") else {}))
                           for i, e in enumerate(entries, 1)]}
        with _STATE_LOCK:
            data = self._load()
            data["plan"] = rec
            self._save(data)
        return rec

    def plan(self, now: float | None = None, spent: bool = False) -> dict | None:
        """The pending plan, while it is live: recorded in the current home
        thread less than PLAN_TTL_S ago, and with an entry not yet run --
        or, with `spent`, even if every entry has (so a second use of one
        can be told it was used, rather than that there is no plan)."""
        now = time.time() if now is None else now
        data = self._load()
        p = data.get("plan")
        if not isinstance(p, dict) or not p.get("entries"):
            return None
        if now - float(p.get("at") or 0) >= PLAN_TTL_S:
            return None
        if not data.get("thread") or p.get("thread") != data.get("thread"):
            return None
        if not spent and all(e.get("used") for e in p["entries"]):
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
                "entries": [dict({"n": e["n"], "name": e["name"], "args": list(e["args"])},
                                 **({"guard": e["guard"], "summary": e.get("summary") or ""}
                                    if e.get("guard") else {}))
                            for e in p["entries"]]}

    def answer_plan(self, ts, text: str, now: float | None = None) -> dict | None:
        """plan_before(ts), for the message that answers the plan: the first
        message of yours after it was recorded that approves entries of it or
        says no to it (plan_answer), and only that one. Marks the plan
        answered by `ts`, so after it no message is an answer to it. A
        message that is no answer ("file it", "what does 3 do?") leaves it
        waiting and gets None: its turn runs no entry. It is noted in the
        plan's `passed`, and from then on a bare "ok" or "sure" -- likely
        about what that message asked -- answers it no longer (_ACK). The same message's turn asking
        again (a retry) gets it again. To be asked again, the operator
        records the plan again."""
        with _STATE_LOCK:
            snap = self.plan_before(ts, now)
            if not snap:
                return None
            data = self._load()
            answered = data["plan"].get("answered")
            if answered and answered != str(ts):
                return None
            if not answered:
                if not plan_answer(text, snap["entries"],
                                   passed=bool(data["plan"].get("passed"))):
                    # No answer: the plan waits -- but a bare "ok" after
                    # this is about this, not the plan (_ACK).
                    if str(ts) not in data["plan"].setdefault("passed", []):
                        data["plan"]["passed"].append(str(ts))
                        self._save(data)
                    return None
                data["plan"]["answered"] = str(ts)
                self._save(data)
            return dict(snap, answered=str(ts))

    def plan_view(self, now: float | None = None) -> dict | None:
        """The last plan recorded, live or not, for the dashboard's card: a
        copy with `status` -- "waiting" (live, unanswered), "answered"
        (answered by a message or a button: what it chose runs, nothing else
        does), "spent" (every entry has run), "expired" (PLAN_TTL_S passed
        unanswered), "replaced" (the operator moved to another thread) -- and
        `expires_at`. None if none was ever recorded."""
        now = time.time() if now is None else now
        data = self._load()
        p = data.get("plan")
        if not isinstance(p, dict) or not p.get("entries"):
            return None
        out = json.loads(json.dumps(p))
        out["expires_at"] = float(p.get("at") or 0) + PLAN_TTL_S
        if all(e.get("used") for e in p["entries"]):
            out["status"] = "spent"
        elif p.get("answered"):
            out["status"] = "answered"
        elif now >= out["expires_at"]:
            out["status"] = "expired"
        elif not data.get("thread") or p.get("thread") != data.get("thread"):
            out["status"] = "replaced"
        else:
            out["status"] = "waiting"
        return out

    def answer_plan_from_dashboard(self, plan_id: str, chosen, by: str, via: str = "",
                                   now: float | None = None) -> tuple[dict | None, str]:
        """Your press of Run all, Run selected or Not now on the plan's card,
        as the answer to plan `plan_id` -- exactly what a typed reply is
        (answer_plan): the first answer, and the only one; the plan must be
        the live one (not expired or replaced) and unanswered. `chosen`: the
        entry numbers to run, none to decline it. Marks it answered (by
        "dashboard:<token>", which no message's ts can equal, so no later
        message answers it) and returns (the plan as answered, ""), or
        (None, why not)."""
        now = time.time() if now is None else now
        try:
            chosen = sorted({int(n) for n in chosen or ()})
        except (TypeError, ValueError):
            return None, "entries are entry numbers"
        with _STATE_LOCK:
            p = self.plan(now)
            if not p or p.get("id") != plan_id:
                view = self.plan_view(now)
                if view and view.get("id") == plan_id and view["status"] != "waiting":
                    return None, {"answered": "this plan has already been answered",
                                  "spent": "every entry of this plan has run",
                                  "expired": "this plan expired unanswered (plans wait a day)",
                                  "replaced": "the operator has moved to another thread"
                                  }.get(view["status"], "this plan is no longer live")
                return None, "that is not the plan waiting now; read it again"
            data = self._load()
            if data["plan"].get("answered"):
                return None, "this plan has already been answered"
            left = {e["n"] for e in data["plan"]["entries"] if not e.get("used")}
            bad = [n for n in chosen if n not in left]
            if bad:
                return None, (f"the plan has no entry {bad[0]} left to run"
                              if bad[0] not in {e["n"] for e in data["plan"]["entries"]}
                              else f"entry {bad[0]} of the plan has already run")
            data["plan"].update(answered="dashboard:" + secrets.token_hex(6),
                                answered_via=VIA_DASHBOARD, answered_by=str(by),
                                answered_detail=str(via)[:200], answered_at=now,
                                chosen=chosen, declined=not chosen)
            self._save(data)
            return json.loads(json.dumps(data["plan"])), ""

    def clear_interrupted_results(self, now: float | None = None) -> list:
        """At startup: entries a press left "queued" or "running" when the bot
        stopped (a crash; a deploy drains first) are marked failed, saying a
        restart cut them off, rather than showing as running for ever. Returns
        the entry numbers changed."""
        now = time.time() if now is None else now
        with _STATE_LOCK:
            data = self._load()
            p = data.get("plan")
            if not isinstance(p, dict):
                return []
            hit = []
            for e in p.get("entries") or []:
                if (e.get("result") or {}).get("state") in ("queued", "running"):
                    e["result"] = {"state": "failed", "at": now, "tail": (
                        "not finished: the bot restarted while it was "
                        + ("running; check what it did" if e["result"]["state"] == "running"
                           else "waiting its turn, so it never ran")
                        + " -- ask the operator to record it again")}
                    hit.append(e.get("n"))
            if hit:
                self._save(data)
            return hit

    def set_entry_result(self, plan_id: str, n: int, result: dict) -> None:
        """What running entry `n` of plan `plan_id` came to (or that it is
        running), for the card to show."""
        with _STATE_LOCK:
            data = self._load()
            p = data.get("plan")
            if not isinstance(p, dict) or p.get("id") != plan_id:
                return
            for e in p.get("entries") or []:
                if e.get("n") == n:
                    e["result"] = dict(result)
                    self._save(data)
                    return

    def use_plan_entry(self, plan_id: str, n: int, run_by: str = "",
                       now: float | None = None, answer: str = "") -> bool:
        """Mark entry `n` of plan `plan_id` run, if that plan is still the live
        one and the entry has not run. True if it was claimed now.

        `answer`: the press on the dashboard's card that answered it
        (answer_plan_from_dashboard's "dashboard:<token>"), and that chose
        entry `n`. Its entries were chosen while the plan was live, so they
        run even if the day runs out, or the operator moves thread, while
        the earlier ones are still running."""
        now = time.time() if now is None else now
        with _STATE_LOCK:
            data = self._load()
            if answer:
                p = data.get("plan") if isinstance(data.get("plan"), dict) else None
                if (not p or p.get("id") != plan_id or p.get("answered") != answer
                        or n not in (p.get("chosen") or [])):
                    return False
            else:
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

    def use_said_lift(self, ts: str, guard: str, item: str,
                      now: float | None = None) -> bool:
        """Claim the one lift of `guard` for `item` your message (sent at
        `ts`) gave by naming it (guard_lift's "named"). True if claimed now;
        False if that message already lifted it once."""
        now = time.time() if now is None else now
        key = [str(ts), guard, str(item).lower()]
        with _STATE_LOCK:
            data = self._load()
            kept = [x for x in data.get("said_lifts") or [] if isinstance(x, dict)
                    and now - float(x.get("at") or 0) < APPROVAL_TTL_S]
            if any(x.get("key") == key for x in kept):
                return False
            data["said_lifts"] = kept + [{"key": key, "at": now}]
            self._save(data)
            return True

    def release_said_lift(self, ts: str, guard: str, item: str) -> None:
        """Undo a claim whose action never happened."""
        key = [str(ts), guard, str(item).lower()]
        with _STATE_LOCK:
            data = self._load()
            data["said_lifts"] = [x for x in data.get("said_lifts") or []
                                  if isinstance(x, dict) and x.get("key") != key]
            self._save(data)

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
          consume: bool = False, action: str = "") -> str:
    """The refusal for a guarded request, or "" if it may go ahead.

    `consume`: the bot uses the approval up as it acts. The CLI only looks.
    """
    if not guard_item:
        return ""
    guard, item = guard_item
    ok = (state.consume(guard, item, now) if consume else state.has(guard, item, now))
    return "" if ok else refusal(guard, item, action)


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
    if rec.get("checkout_failing"):
        head += (f"; checkout failing tests its base passes (landings held): "
                 f"{json.dumps(rec['checkout_failing'])[:200]}")
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
    "trader's live configuration, deleting anything not proven redundant. Those "
    "wait for the user's word -- ask them for it. A landing that changes "
    "trader's live configuration, or publish to turn on, record as a plan "
    "entry (`{bin} ops plan --land <id>`, `--approve <id>`, `--publish "
    "<project>`, alongside any ops) and list it under Needs you numbered as "
    "it prints, with what it changes; their \"yes\" in this thread lifts it "
    "for that item. This sweep is not a turn the user started, so "
    "nothing the user said earlier lifts them now.\n\n"
    "Each task you list under Needs you, give your recommendation on its "
    "card in the dashboard's inbox: `{bin} edit <id> --recommend <accept|"
    "dismiss|approve|land|drop|close|dig|rework|retry|resolve|release> "
    "--why \"<one line>\"`. It decides nothing, so it needs no approval; "
    "`--recommend clear` takes it off.")

SWEEP_NOTE = (
    "This is your daily sweep, filed by the scheduler, not a message from the "
    "user. Silkworm puts a per-project health brief above your reply (one "
    "line per project, what changed, and the proposal or decision each "
    "problem has), so do not repeat it. Your reply is posted beneath it, in "
    "the same single message, and is read on a phone: first what you did "
    "(one short line per action, with task ids -- fix proposals you filed "
    "for health checks included), then *Needs you:* one line per item with "
    "the specific decision or question, numbered plan entries included "
    "(they answer in this thread: \"yes\", \"do 2\") -- never ask them to "
    "type `!approve`. "
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

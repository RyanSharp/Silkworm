"""Project health: deterministic probes, run on a schedule, read by everyone.

On 2026-10-19 Cadence's GitHub "Tests" workflow went red on main -- a flaky
StoreServiceTests case that had failed four times -- and nothing in Silkworm
noticed. Base-red tracking (basered.py) only ever reads the project's local
test command, and only when a task's own tests fail; the forge's view of the
same branch was nobody's to look at.

So this is the layer that looks. A *check* is a named probe that, for one
project, returns one of four statuses with a one-line summary, its evidence
and a link:

  ok       -- nothing to say. Counted, not listed.
  warn     -- worth knowing, not worth paging about (a flaky test that passed
              on its latest run).
  fail     -- one needs-you item per (project, check), modelled on base-red's:
              opened once, reworded while it stays failing, closed when the
              check comes back ok or warn. A failure that persists is one item
              and one line, not an alert an hour.
  unknown  -- the probe could not run: gh missing, the network down, a rate
              limit. Never "fail", never paged about, and it neither opens nor
              closes an item: not being able to look says nothing about the
              thing you could not look at.

Silkworm runs the probes and hands their results to whoever reasons about
them -- the operator's live state, the digest, the board, `silkworm health`
-- so the operator reasons over facts rather than fetching them (it has no
gh and no curl, on purpose).

Checks plug in through `register()`: the core here -- the store, the
transitions, the needs-you item -- knows nothing about what any probe looks
at. The first is "ci" (GitHub Actions, below); the project-specific probes
(the trader's daemon, Supabase drift, App Store Connect, the mini itself) add
themselves the same way, from health_probes.py.

Pure logic, testable without a token: every network call goes through the
`gh` callable a probe is handed.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import jsonstore
import tasks
import verify

log = logging.getLogger("silkworm.health")

OK, WARN, FAIL, UNKNOWN = "ok", "warn", "fail", "unknown"
STATUSES = (OK, WARN, FAIL, UNKNOWN)
#: Worst first, for sorting and for a project's overall status.
SEVERITY = {FAIL: 0, WARN: 1, UNKNOWN: 2, OK: 3}
ICON = {OK: "🟢", WARN: "🟡", FAIL: "🔴", UNKNOWN: "⚪"}

#: The `source` of a failing check's one needs-you item.
SOURCE = "health"

#: Lines of evidence kept per check: enough to act on, not a log.
EVIDENCE_MAX = 12

#: The daily pass runs this long before the operator's sweep (HEALTH_LEAD),
#: so the sweep's brief reads this morning's results; and the values that
#: switch it off. "0" is not one of them: it means "at the sweep's time".
DEFAULT_LEAD = "15m"
OFF = ("off", "none", "false", "no")
#: The longest lead accepted: further back than this is not "before the
#: sweep" but a different schedule.
MAX_LEAD_MIN = 12 * 60
#: A check warning this many days (HEALTH_WARN_DAYS) may get one fix proposal.
DEFAULT_WARN_DAYS = 3.0


# --- results -------------------------------------------------------------------

def result(status: str, summary: str, evidence=(), link: str = "",
           fingerprint: str = "") -> dict:
    """What a probe returns. `fingerprint` names *this* failure (the commits
    and workflows failing), so a person who dismissed its item is not asked
    again about the same one, and is about the next."""
    if status not in STATUSES:
        raise ValueError(f"unknown health status {status!r}")
    return {"status": status,
            "summary": " ".join(str(summary or "").split())[:300],
            "evidence": [str(e)[:300] for e in list(evidence or ())[:EVIDENCE_MAX]],
            "link": str(link or ""), "fingerprint": str(fingerprint or "")}


def unknown(why: str, link: str = "") -> dict:
    return result(UNKNOWN, why, link=link)


# --- the registry --------------------------------------------------------------

class Check:
    """A named probe. `probe(project_record, gh)` returns a result();
    `applies(project_record)` says whether it means anything for that project
    at all (a CI check for a project with no GitHub repository does not)."""

    def __init__(self, name: str, probe, applies=None, about: str = "",
                 decision=None, short=None):
        self.name, self.probe, self.about = name, probe, about
        self.applies = applies or (lambda rec: True)
        # `decision(record) -> str`: for a check whose trouble is the owner's
        # to settle (a release, an Apple review, a credential), the decision
        # it needs, in words -- or "" when this particular result is not one.
        # A check with a decision is never filed as code work.
        self.decision = decision
        # `short(record) -> str`: a few words for an ok result in the brief
        # ("CI green", "1.1.0 live").
        self.short = short
        # `identity(record) -> str`: which trouble this is, where the
        # fingerprint is too fine for "proposed once" (see health.identity).
        self.identity = None


REGISTRY: dict[str, Check] = {}


def register(name: str, probe, applies=None, about: str = "",
             decision=None, short=None) -> Check:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name or ""):
        raise ValueError(f"bad check name {name!r}")
    check = Check(name, probe, applies, about, decision, short)
    REGISTRY[name] = check
    return check


def checks_for(rec: dict, registry: dict | None = None) -> list[Check]:
    """The registered checks that apply to a project, by name."""
    registry = REGISTRY if registry is None else registry
    out = []
    for name in sorted(registry):
        try:
            if registry[name].applies(rec):
                out.append(registry[name])
        except Exception:
            log.exception("health: does %s apply to %s?", name, rec.get("slug"))
    return out


def run_check(check: Check, rec: dict, gh) -> dict:
    """One probe, which may not take the caller down with it: an exception,
    or something that is not a result, is `unknown` -- the probe failed, not
    the project."""
    try:
        out = check.probe(rec, gh)
        if not isinstance(out, dict) or out.get("status") not in STATUSES:
            return unknown(f"the {check.name} probe returned no result")
        return result(out["status"], out.get("summary") or "", out.get("evidence") or (),
                      out.get("link") or "", out.get("fingerprint") or "")
    except Exception as e:                  # noqa: BLE001 - reported as unknown
        log.exception("health: %s on %s failed", check.name, rec.get("slug"))
        return unknown(f"the {check.name} probe raised {type(e).__name__}: {e}"[:200])


# --- the store -----------------------------------------------------------------

def key(slug: str, check: str) -> str:
    return f"{slug}/{check}"


#: Fields of a record that outlive each new result: its needs-you item, its
#: fix proposal (and a filing that was held), and what it was before.
CARRIED = ("attention", "attention_for", "proposal", "proposal_at", "proposal_for",
           "proposal_held", "proposal_held_at", "was")


class HealthStore:
    """health.json: the latest result of every (project, check), atomically.

    Per record, besides the result: `first_seen` (when the pair was first
    checked), `last_changed` (when its status last became what it is now --
    so "failing since" is honest), `checked_at`, and `last_known`, the last
    status that was not unknown, with when it was established. `attention`
    is the id of its needs-you item while it has one.

    `readonly` is for other processes (the CLI): they load without repairing
    and never save. `strict` raises on a file (and backup) that will not
    parse; the bot's store is lenient, since every result is re-probed within
    the hour and an open item is found again by surface() without it.
    """

    VERSION = 1

    def __init__(self, path, readonly: bool = False, strict: bool = False):
        self.path = Path(path)
        self.readonly = readonly
        self._lock = threading.RLock()
        data = jsonstore.load(self.path, default={}, strict=strict,
                              repair=not readonly) or {}
        if not isinstance(data, dict):
            data = {}
        self._checks: dict = dict(data.get("checks") or {})
        self._cache: dict = dict(data.get("cache") or {})
        self._meta: dict = dict(data.get("meta") or {})

    def _save(self) -> None:
        if self.readonly:
            raise RuntimeError("a read-only health store does not save")
        jsonstore.save(self.path, {"version": self.VERSION, "checks": self._checks,
                                   "cache": self._cache, "meta": self._meta})

    # the daily pass
    def pass_on(self) -> str:
        """The day (YYYY-MM-DD) the scheduled pass last completed, or ""."""
        with self._lock:
            return str(self._meta.get("pass_on") or "")

    def mark_pass(self, now: datetime) -> None:
        with self._lock:
            self._meta.update(pass_on=now.strftime("%Y-%m-%d"), pass_at=now.timestamp())
            self._save()

    def get(self, slug: str, check: str) -> dict | None:
        with self._lock:
            rec = self._checks.get(key(slug, check))
            return json.loads(json.dumps(rec)) if rec else None

    def all(self) -> list[dict]:
        with self._lock:
            rows = json.loads(json.dumps(list(self._checks.values())))
        return sorted(rows, key=lambda r: (r.get("project") or "", r.get("check") or ""))

    def record(self, slug: str, check: str, res: dict, now: float | None = None) -> tuple:
        """Store a probe's result. Returns (previous record or None, new one)."""
        now = time.time() if now is None else now
        with self._lock:
            k = key(slug, check)
            prior = self._checks.get(k)
            known = (prior or {}).get("last_known") or {}
            if prior and prior.get("status") == res["status"]:
                changed = prior.get("last_changed") or now
            elif (prior and prior.get("status") == UNKNOWN
                  and known.get("status") == res["status"]):
                # Back from a blip it could not see through: still the same
                # failure, failing since it started, not since gh came back.
                changed = known.get("since") or now
            else:
                changed = now
            new = {"project": slug, "check": check, **res, "checked_at": now,
                   "first_seen": (prior or {}).get("first_seen") or now,
                   "last_changed": changed,
                   **{f: (prior or {}).get(f) or "" for f in CARRIED}}
            if prior and changed == now and prior.get("status") != res["status"]:
                # What it was before it became this, for "new since yesterday
                # (was ok)". Through a blip, the last thing it was seen to be.
                new["was"] = (known.get("status") if prior.get("status") == UNKNOWN
                              and known.get("status") else prior.get("status")) or ""
            if res["status"] == UNKNOWN:
                new["last_known"] = (prior or {}).get("last_known") or {}
            elif ((prior or {}).get("last_known") or {}).get("status") == res["status"]:
                new["last_known"] = prior["last_known"]
            else:
                new["last_known"] = {"status": res["status"], "since": now}
            self._checks[k] = new
            self._save()
            return (json.loads(json.dumps(prior)) if prior else None,
                    json.loads(json.dumps(new)))

    def update(self, slug: str, check: str, **fields) -> None:
        with self._lock:
            rec = self._checks.get(key(slug, check))
            if rec is None:
                return
            rec.update(fields)
            self._save()

    def forget(self, slug: str, check: str) -> dict | None:
        with self._lock:
            gone = self._checks.pop(key(slug, check), None)
            if gone is not None:
                self._save()
            return gone

    def cached(self, check: str, name: str):
        with self._lock:
            return (self._cache.get(check) or {}).get(name)

    def cache(self, check: str, name: str, value, keep: int = 200) -> None:
        """Remember something expensive a probe worked out (a run's failing
        tests), bounded: the oldest entries go first."""
        with self._lock:
            mine = dict(self._cache.get(check) or {})
            mine.pop(name, None)
            mine[name] = value
            while len(mine) > keep:
                mine.pop(next(iter(mine)))
            self._cache[check] = mine
            self._save()


# --- the needs-you item --------------------------------------------------------

def title_line(rec: dict) -> str:
    return f"{rec.get('project')} {rec.get('check')}: {rec.get('summary') or rec.get('status')}"


def _goal(rec: dict) -> str:
    ev = "\n".join(f"  {e}" for e in rec.get("evidence") or ()) or "  (no evidence recorded)"
    return (f"{rec.get('project')}'s {rec.get('check')} health check is failing: "
            f"{rec.get('summary')}\n\nEvidence:\n{ev}\n\n"
            + (f"Link: {rec['link']}\n\n" if rec.get("link") else "")
            + "This item closes on its own when the check passes again. Dismissing "
              "it silences this failure; a different one opens a new item.")


def ref_prefix(slug: str, check: str) -> str:
    return f"{slug}/{check}@"


def open_item(task_store, slug: str, check: str) -> dict | None:
    """The (project, check)'s open item found from the task store alone --
    for a health record that has lost its pointer (a store started empty),
    so the next failure rewords the item that is there instead of opening a
    second one beside it."""
    for t in task_store.all().values():
        if (t.get("source") == SOURCE and t.get("state") not in tasks.ENDED
                and str(t.get("source_ref") or "").startswith(ref_prefix(slug, check))):
            return t
    return None


def surface(store: HealthStore, task_store, slug: str, check: str,
            scope: dict | None = None) -> dict:
    """Bring the (project, check)'s needs-you item in line with its status.

    {"opened": item} the first time a failure is seen -- the moment worth
    telling anyone; {"updated": item} while it stays failing (reworded, never
    duplicated); {"closed": item} when it is ok or warn again; {} otherwise.
    Unknown touches nothing: a probe that could not run neither raises an
    item nor clears one.

    An item a person closed stays closed for the failure it was about (its
    fingerprint); a different failure -- another commit, another workflow --
    opens a new one, as base-red does for a later failing commit.
    """
    rec = store.get(slug, check)
    if not rec or rec.get("status") == UNKNOWN:
        return {}
    item = task_store.get(rec["attention"]) if rec.get("attention") else None
    if item is None:
        item = open_item(task_store, slug, check)
        if item:
            store.update(slug, check, attention=item["id"])
    live = bool(item) and item.get("state") not in tasks.ENDED
    if rec["status"] == FAIL:
        title = title_line(rec)[:80]
        if live:
            task_store.update(item["id"], title=title, goal=_goal(rec))
            store.update(slug, check, attention_for=rec.get("fingerprint") or "")
            return {"updated": task_store.get(item["id"])}
        if item and rec.get("attention_for") == (rec.get("fingerprint") or ""):
            return {}                         # dismissed; the same failure
        item = task_store.create(
            _goal(rec), title=title, state=tasks.NEEDS_INPUT, role="assistant",
            driver="queue", isolate=False, source=SOURCE,
            source_ref=f"{ref_prefix(slug, check)}{(rec.get('fingerprint') or '')[:60]}",
            project=slug, scope=dict(scope or {}))
        store.update(slug, check, attention=item["id"],
                     attention_for=rec.get("fingerprint") or "")
        return {"opened": item}
    if rec["status"] == OK:
        retire_proposal(store, task_store, slug, check, f"the {check} check passes again")
    if not item:
        return {}
    store.update(slug, check, attention="", attention_for="")
    if not live or item.get("state") == tasks.RUNNING:
        return {}                             # someone's turn on it ends itself
    to = tasks.CANCELLED if item.get("state") == tasks.QUEUED else tasks.DONE
    try:
        # Only from the state just read: the runner claiming it meanwhile (a
        # reply asked for a fix) makes this refuse, rather than end a live turn.
        task_store.transition(item["id"], to, f"the {check} check is {rec['status']} again",
                              expect=item["state"])
    except Exception:
        log.exception("health: could not close %s", item["id"])
        return {}
    return {"closed": task_store.get(item["id"]) or item}


def retire(store: HealthStore, task_store, keep: set) -> list[str]:
    """Forget records for (project, check) pairs no longer checked -- a project
    archived, its repository removed, a check unregistered -- closing any item
    they hold, so nothing pages about a thing nobody looks at any more."""
    gone = []
    for rec in store.all():
        k = key(rec["project"], rec["check"])
        if k in keep:
            continue
        item = (task_store.get(rec["attention"]) if rec.get("attention") else None) \
            or open_item(task_store, rec["project"], rec["check"])
        if item and item.get("state") == tasks.RUNNING:
            continue                  # asked again next pass, once that turn ends
        if item and item.get("state") not in tasks.ENDED:
            try:
                task_store.transition(item["id"], tasks.CANCELLED
                                      if item.get("state") == tasks.QUEUED else tasks.DONE,
                                      f"the {rec['check']} check no longer runs here",
                                      expect=item["state"])
            except Exception:
                log.exception("health: could not close %s", item["id"])
                continue
        retire_proposal(store, task_store, rec["project"], rec["check"],
                        f"the {rec['check']} check no longer runs here")
        store.forget(rec["project"], rec["check"])
        gone.append(k)
    return gone


def run_all(store: HealthStore, task_store, project_recs, gh, *,
            registry: dict | None = None, scope_for=None, now=None,
            on_open=None, on_close=None) -> list[dict]:
    """Run every applicable check on every project, record it and surface it.

    Each step is its own try: one project's failure costs that project's
    result, never the rest. `on_open(record, item)` / `on_close(record, item)`
    are told of an item opened or closed (the bot announces them)."""
    out, keep = [], set()
    for rec in project_recs:
        if rec.get("archived"):
            continue
        slug = rec.get("slug") or ""
        for check in checks_for(rec, registry):
            keep.add(key(slug, check.name))
            res = run_check(check, rec, _bound(gh, store, check.name))
            try:
                store.record(slug, check.name, res, now)
                moved = surface(store, task_store, slug, check.name,
                                scope=(scope_for(slug) if scope_for else rec.get("scope")))
            except Exception:
                log.exception("health: recording %s/%s failed", slug, check.name)
                continue
            row = store.get(slug, check.name) or {}
            try:
                if moved.get("opened") and on_open:
                    on_open(row, moved["opened"])
                if moved.get("closed") and on_close:
                    on_close(row, moved["closed"])
            except Exception:
                log.exception("health: announcing %s/%s failed", slug, check.name)
            out.append(row)
    try:
        retire(store, task_store, keep)
    except Exception:
        log.exception("health: retiring old checks failed")
    return out


class _bound:
    """The `gh` a probe is handed, with its own slice of the store's cache."""

    def __init__(self, gh, store, check):
        self._gh, self._store, self._check = gh, store, check

    def __call__(self, *args, **kw):
        return self._gh(*args, **kw)

    def cached(self, name):
        return self._store.cached(self._check, name)

    def cache(self, name, value):
        self._store.cache(self._check, name, value)


# --- reading it ----------------------------------------------------------------

def overall(rows) -> str:
    rows = list(rows)
    return min((r.get("status") or UNKNOWN for r in rows),
               key=lambda s: SEVERITY.get(s, 9)) if rows else OK


def _age(seconds: float) -> str:
    h = seconds / 3600
    return f"{h / 24:.0f}d" if h >= 48 else f"{h:.0f}h" if h >= 1 else f"{max(0, seconds) / 60:.0f}m"


def describe(rec: dict, now: float | None = None) -> str:
    """One line: status, check, summary, how long it has been so."""
    now = time.time() if now is None else now
    line = (f"{ICON.get(rec.get('status'), '?')} {rec.get('check')} {rec.get('status')}: "
            f"{rec.get('summary') or ''} (since {_age(now - (rec.get('last_changed') or now))})")
    if rec.get("status") == UNKNOWN and (rec.get("last_known") or {}).get("status"):
        line += f"; last known {rec['last_known']['status']}"
    return line


def by_project(rows) -> dict:
    out: dict = {}
    for r in rows:
        out.setdefault(r.get("project") or "", []).append(r)
    return out


def context_section(rows, now: float | None = None, evidence: int = 4,
                    actions=None) -> str:
    """The operator's "Project health" section: every non-ok check in detail,
    with its evidence and link; the ok ones counted, not listed. `actions(rec)`
    is what is being done about one (see `action`), said under it."""
    rows = list(rows)
    if not rows:
        return "(no health checks have run yet)"
    bad = sorted((r for r in rows if r.get("status") != OK),
                 key=lambda r: (SEVERITY.get(r.get("status"), 9), r.get("project") or ""))
    oks = len(rows) - len(bad)
    lines = []
    for r in bad:
        lines.append(f"- {r.get('project')}: {describe(r, now)}"
                     + (f" [item {r['attention']}]" if r.get("attention") else ""))
        lines += [f"    {e}" for e in (r.get("evidence") or [])[:evidence]]
        if r.get("link"):
            lines.append(f"    {r['link']}")
        if actions:
            try:
                said = actions(r)
            except Exception:
                log.exception("health: what is done about %s/%s", r.get("project"), r.get("check"))
                said = ""
            if said:
                lines.append(f"    → {said}")
    projects = len({r.get("project") for r in rows})
    lines.append(f"({oks} of {len(rows)} checks ok across {projects} project"
                 f"{'s' if projects != 1 else ''})")
    return "\n".join(lines)


def digest_lines(rows) -> dict:
    """{project: line} for every project with a check that is not ok."""
    out = {}
    for slug, mine in by_project(rows).items():
        bad = sorted((r for r in mine if r.get("status") != OK),
                     key=lambda r: SEVERITY.get(r.get("status"), 9))
        if bad:
            out[slug] = "; ".join(f"{r['check']} {r['status']} — {r.get('summary') or ''}"
                                  for r in bad)
    return out


def board_line(rows) -> dict | None:
    """A project's one board line while any check fails or warns, else None."""
    bad = sorted((r for r in rows if r.get("status") in (FAIL, WARN)),
                 key=lambda r: SEVERITY.get(r.get("status"), 9))
    if not bad:
        return None
    return {"status": bad[0]["status"],
            "line": "; ".join(f"{r['check']}: {r.get('summary') or r['status']}" for r in bad),
            "link": bad[0].get("link") or ""}


# --- what is done about it: decisions and fix proposals -----------------------------

#: The `source_ref` prefix of a fix proposal filed for a check, so one is
#: found from the task store alone if the record lost its pointer.
PROPOSAL_REF = "health:"


def warn_days(text: str | None) -> float:
    """HEALTH_WARN_DAYS -> days, DEFAULT_WARN_DAYS for unset or unreadable."""
    try:
        v = float(str(text).strip()) if text not in (None, "") else DEFAULT_WARN_DAYS
    except ValueError:
        log.warning("HEALTH_WARN_DAYS %r is not a number; using %g", text, DEFAULT_WARN_DAYS)
        return DEFAULT_WARN_DAYS
    return max(0.0, v)


def identity(rec: dict) -> str:
    """Which trouble this is, for "proposed once": its fingerprint where the
    probe gives one (the commits failing), else the status and when it
    started -- the same warning, not the same wording, since a summary that
    counts days changes every day."""
    check = REGISTRY.get(rec.get("check") or "")
    if check and getattr(check, "identity", None):
        try:
            got = check.identity(rec)
            if got:
                return got
        except Exception:
            log.exception("health: the identity of %s", rec.get("check"))
    return rec.get("fingerprint") or f"{rec.get('status')}@{int(rec.get('last_changed') or 0)}"


def decision(rec: dict, registry: dict | None = None) -> str:
    """The owner's decision this result needs, in words, or ""."""
    registry = REGISTRY if registry is None else registry
    check = registry.get(rec.get("check") or "")
    if not check or not check.decision or rec.get("status") == OK:
        return ""
    try:
        return " ".join(str(check.decision(rec) or "").split())
    except Exception:
        log.exception("health: the decision for %s/%s", rec.get("project"), rec.get("check"))
        return ""


def proposal_ref(slug: str, check: str) -> str:
    return f"{PROPOSAL_REF}{key(slug, check)}"


def proposal_of(rec: dict, task_store) -> dict | None:
    """The (project, check)'s fix proposal: the one its record points at, or
    -- a record that lost the pointer -- an open task filed for it."""
    if rec.get("proposal"):
        t = task_store.get(rec["proposal"])
        if t:
            return t
    ref = proposal_ref(rec.get("project") or "", rec.get("check") or "")
    for t in task_store.all().values():
        if t.get("source_ref") == ref and t.get("state") not in tasks.ENDED:
            return t
    return None


def proposal_refusal(rec: dict | None, task_store, now: float | None = None, *,
                     registry: dict | None = None,
                     warn_after: float = DEFAULT_WARN_DAYS) -> str:
    """Why a fix proposal may not be filed for this check now, or "".

    Once per (project, check) and trouble: a check that newly fails, or has
    warned for `warn_after` days, may get one -- through the ordinary proposed
    gate, whose caps and dedup still apply on top of this. Never while one is
    open; never again for the same trouble once one was filed (dismissed is an
    answer); never for a decision that is the owner's to make, which is said,
    not filed as code work.
    """
    now = time.time() if now is None else now
    if not rec:
        return "no such health check has run"
    name = f"{rec.get('project')}/{rec.get('check')}"
    status = rec.get("status")
    if status in (OK, UNKNOWN):
        return f"{name} is {status}: there is nothing to fix"
    owner = decision(rec, registry)
    if owner:
        return (f"{name} is the owner's decision, not code work: {owner}. Say it in "
                "your report; do not file it")
    if status == WARN:
        age = (now - (rec.get("last_changed") or now)) / 86400
        if age < warn_after:
            return (f"{name} has warned for {age:.1f}d; a fix is proposed only once it "
                    f"has warned for {warn_after:g}d")
    item = task_store.get(rec["attention"]) if rec.get("attention") else None
    if item and item.get("state") == tasks.RUNNING:
        return f"{name}'s item {item['id']} is being worked on right now"
    prop = proposal_of(rec, task_store)
    if prop and prop.get("state") not in tasks.ENDED:
        return f"{name} already has a fix proposal open: {prop['id']} ({prop.get('state')})"
    if rec.get("proposal") and rec.get("proposal_for") == identity(rec):
        state = (prop or {}).get("state") or "gone"
        return (f"{name} was already proposed as {rec['proposal']} ({state}) for this same "
                "trouble; a different failure may be proposed when it comes")
    return ""


def retire_proposal(store: HealthStore, task_store, slug: str, check: str, why: str) -> str:
    """Dismiss the check's fix proposal if it is still only a proposal, once
    the check passes (or stops being checked): a fix for something already
    fixed should not wait on the board, counting against the backlog cap, to
    be accepted by mistake. One already accepted is work under way, and left
    alone. Returns the id dismissed, or ""."""
    rec = store.get(slug, check) or {"project": slug, "check": check}
    prop = proposal_of(rec, task_store)
    if not prop or prop.get("state") != tasks.PROPOSED:
        return ""
    try:
        task_store.transition(prop["id"], tasks.CANCELLED, why, expect=tasks.PROPOSED)
    except Exception:
        log.exception("health: could not dismiss proposal %s", prop["id"])
        return ""
    return prop["id"]


def own_items(task_store, slug: str, check: str) -> set:
    """Every needs-you item this (project, check) ever opened, open or closed:
    each carries the check's evidence by construction, so a fix proposal is
    never a "duplicate" of its own alarms."""
    pre = ref_prefix(slug, check)
    return {t["id"] for t in task_store.all().values()
            if t.get("source") == SOURCE and str(t.get("source_ref") or "").startswith(pre)}


def proposal_goal(goal: str, rec: dict, now: float | None = None, limit: int = 3900) -> str:
    """The goal a fix proposal is filed with: the operator's words, then the
    check's evidence -- so whoever picks it up has what the probe saw, not
    the operator's paraphrase of it."""
    now = time.time() if now is None else now
    ev = "\n".join(f"  {e}" for e in (rec.get("evidence") or ())) or "  (no evidence recorded)"
    block = (f"\n\nHealth check {rec.get('project')}/{rec.get('check')}: {rec.get('status')} "
             f"for {_age(now - (rec.get('last_changed') or now))} — {rec.get('summary')}\n"
             f"Evidence:\n{ev}" + (f"\nLink: {rec['link']}" if rec.get("link") else ""))
    goal = (goal or "").strip()
    return (goal[:max(0, limit - len(block))] + block)[:limit]


def link_proposal(store: HealthStore, slug: str, check: str, task_id: str,
                  now: float | None = None) -> None:
    rec = store.get(slug, check) or {}
    store.update(slug, check, proposal=task_id, proposal_at=time.time() if now is None else now,
                 proposal_for=identity(rec), proposal_held="", proposal_held_at="")


def hold_proposal(store: HealthStore, slug: str, check: str, why: str,
                  now: float | None = None) -> None:
    """A filing the caps or dedup refused, kept so the brief says why."""
    store.update(slug, check, proposal_held=" ".join(str(why or "").split())[:240],
                 proposal_held_at=time.time() if now is None else now)


def _when(ts: float, now: float) -> str:
    """"today", a weekday within the week, else the date -- for a phone."""
    then, today = datetime.fromtimestamp(ts), datetime.fromtimestamp(now)
    days = (today.date() - then.date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return then.strftime("%A") if days < 7 else then.strftime("%b %d").replace(" 0", " ")


def action(rec: dict, task_store, now: float | None = None, *, registry: dict | None = None,
           warn_after: float = DEFAULT_WARN_DAYS) -> tuple[str, str]:
    """(kind, words) for what is being done about a non-ok check.

    kinds: "decision" (the owner's, stated), "proposal" (one is open),
    "proposed" (one was, for this same trouble), "held" (a filing the caps or
    dedup refused), "eligible" (a fix may be proposed now), "watching" (a
    warning not yet old enough), "looking" (unknown: the probe could not see).
    """
    now = time.time() if now is None else now
    status = rec.get("status")
    owner = decision(rec, registry)
    if owner:
        return "decision", f"decide: {owner}"
    if status == UNKNOWN:
        return "looking", "nothing to act on until it can look"
    if status == OK:
        return "", ""
    prop = proposal_of(rec, task_store)
    if prop and prop.get("state") not in tasks.ENDED:
        return "proposal", (f"proposal {prop['id']} open since "
                            f"{_when(prop.get('created') or rec.get('proposal_at') or now, now)}"
                            + (" — accept it to start the fix" if prop.get("state") == tasks.PROPOSED
                               else f" ({prop.get('state')})"))
    if rec.get("proposal") and rec.get("proposal_for") == identity(rec):
        return "proposed", (f"proposed as {rec['proposal']} "
                            f"({(prop or {}).get('state') or 'gone'}); not refiled for the same trouble")
    why = proposal_refusal(rec, task_store, now, registry=registry, warn_after=warn_after)
    if why and status == WARN and "has warned for" in why:
        left = warn_after - (now - (rec.get("last_changed") or now)) / 86400
        return "watching", f"watching; a fix is proposed if it still warns in {max(left, 0):.0f}d"
    if why:
        return "held", why
    # Held since the latest pass only: a cap that refused it yesterday says
    # nothing about today, and a stale hold would hide the offer for good.
    if rec.get("proposal_held") and (rec.get("proposal_held_at") or 0) >= (rec.get("checked_at") or 0):
        return "held", f"fix proposal held: {rec['proposal_held']}"
    return "eligible", "a fix may be proposed (once)"


def short(rec: dict, registry: dict | None = None) -> str:
    """A few words for an ok check in the brief."""
    registry = REGISTRY if registry is None else registry
    check = registry.get(rec.get("check") or "")
    if check and check.short:
        try:
            got = " ".join(str(check.short(rec) or "").split())
            if got:
                return got[:40]
        except Exception:
            log.exception("health: the short form of %s", rec.get("check"))
    return f"{rec.get('check')} ok"


def _new_at_last_pass(rec: dict, now: float) -> bool:
    """Whether the status became what it is at the latest pass -- "since
    yesterday" for a daily pass, by the passes themselves rather than a window
    of hours alone, which a pass a little late would make say it two days
    running. (And within a day: a latest pass days old is not news.)"""
    changed = rec.get("last_changed") or 0
    return changed >= (rec.get("checked_at") or 0) - 1 and now - changed < 26 * 3600


def _changed(rec: dict, now: float) -> str:
    """What changed since yesterday, in a few words."""
    since = rec.get("last_changed") or now
    if not _new_at_last_pass(rec, now):
        return f"unchanged {_age(now - since)}"
    was = rec.get("was")
    if was:
        return f"new since yesterday (was {was})"
    # Never anything else: it has been this since it was first looked at,
    # which says nothing about whether it is new.
    return f"first checked {_when(rec.get('first_seen') or since, now)}"


_LABEL = {FAIL: "failing", WARN: "warn", UNKNOWN: "can't look"}
#: The order a project's ok checks are named in: code, backend, store.
_SHORT_ORDER = ("ci", "backend-drift", "app-store", "testflight")


def brief(rows, task_store, now: float | None = None, *, registry: dict | None = None,
          warn_after: float = DEFAULT_WARN_DAYS, checked_on: str = "",
          summary_chars: int = 110) -> str:
    """The sweep's per-project health brief, Slack mrkdwn, read on a phone.

    One line per project -- its icon, its name, a few words per ok check --
    and under it, for each check that is not ok, how long and whether that
    changed since yesterday, the probe's summary, and either what is being
    done (a fix proposal and since when) or the exact decision it needs from
    the owner. Worst projects first. `rows` None is "could not be read".
    """
    now = time.time() if now is None else now
    if rows is None:
        return "*Project health:* could not be read this morning (see the bot log)."
    rows = list(rows)
    if not rows:
        return "*Project health:* no checks have run yet."
    groups = by_project(rows)
    order = sorted(groups, key=lambda slug: (SEVERITY.get(overall(groups[slug]), 9), slug))
    oks = sum(1 for r in rows if r.get("status") == OK)
    head = (f"*Project health* — {oks} of {len(rows)} checks ok"
            + (f", checked {checked_on}" if checked_on else ""))
    lines = [head]
    for slug in order:
        mine = sorted(groups[slug], key=lambda r: (SEVERITY.get(r.get("status"), 9),
                                                   r.get("check") or ""))
        good = [short(r, registry) for r in sorted(
            mine, key=lambda r: (_SHORT_ORDER.index(r.get("check"))
                                 if r.get("check") in _SHORT_ORDER else 99, r.get("check") or ""))
            if r.get("status") == OK]
        fixed = [r.get("check") for r in mine if r.get("status") == OK and r.get("was")
                 and r.get("was") != OK and _new_at_last_pass(r, now)]
        line = f"{ICON.get(overall(mine), '?')} *{slug}*"
        if good:
            line += " — " + ", ".join(good)
        if fixed:
            line += f" (ok again since yesterday: {', '.join(fixed)})"
        lines.append(line)
        for r in mine:
            if r.get("status") == OK:
                continue
            summ = _clip(r.get("summary") or r.get("status"), summary_chars)
            lines.append(f"   • {r.get('check')} {_LABEL.get(r.get('status'), r.get('status'))}, "
                         f"{_changed(r, now)}: {summ}")
            _kind, words = action(r, task_store, now, registry=registry, warn_after=warn_after)
            if words:
                lines.append(f"      → {words}")
    return "\n".join(lines)


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:max(0, n - 1)].rstrip() + "…"


# --- the schedule: once a day, just before the operator's sweep -----------------

def parse_lead(text: str | None) -> int | None:
    """HEALTH_LEAD -> whole minutes before the sweep, or None for off.
    `15`, `15m`, `1h`, `0` (at the sweep's own time). Raises ValueError on
    anything else, or on more than MAX_LEAD_MIN."""
    t = (text if text is not None else DEFAULT_LEAD).strip().lower() or DEFAULT_LEAD
    if t in OFF:
        return None
    m = re.fullmatch(r"(\d+)\s*([mh]?)", t)
    if not m:
        raise ValueError(f"HEALTH_LEAD {text!r} is not a lead like 15m or 1h")
    minutes = int(m.group(1)) * (60 if m.group(2) == "h" else 1)
    if minutes > MAX_LEAD_MIN:
        raise ValueError(f"HEALTH_LEAD {text!r} is more than {MAX_LEAD_MIN // 60}h")
    return minutes


def pass_at(sweep_at: str, lead_min: int) -> str:
    """The pass's time of day: `sweep_at` (HH:MM) less `lead_min`, held at
    00:00 rather than wrapping into yesterday -- a pass "before" an early
    sweep must still be today's."""
    h, m = (int(x) for x in sweep_at.split(":"))
    total = max(0, h * 60 + m - max(0, int(lead_min)))
    return f"{total // 60:02d}:{total % 60:02d}"


def pass_due(now: datetime, at: str, last_on: str) -> bool:
    """Whether today's pass should run now: its time has passed and it has not
    completed today. The wall-clock catch-up projects.due_for_ideation and
    the operator's sweep use, so a bot that was down at 08:15 runs it as soon
    as it is back, and a restart after it ran does not run it again."""
    return last_on != now.strftime("%Y-%m-%d") and now.strftime("%H:%M") >= at


def schedule(sweep_at_text: str, lead_text: str | None, default_sweep: str,
             parse_at) -> tuple[str | None, str]:
    """(HH:MM or None for off, the line the bot logs at startup).

    Anchored to the sweep's time whether or not the sweep runs: with
    OPERATOR_SWEEP_AT off (or unreadable) the pass keeps the time it would
    have run before the default sweep, so turning the operator off does not
    turn health off with it."""
    try:
        lead = parse_lead(lead_text)
    except ValueError as e:
        return None, f"off: {e}"
    if lead is None:
        return None, f"off (HEALTH_LEAD={lead_text})"
    sweep_text = (sweep_at_text or "").strip()
    note = ""
    if not sweep_text or sweep_text.lower() in ("off", "none"):     # digest.OFF
        sweep, note = default_sweep, " (the sweep is off; the pass runs at this time anyway)"
    else:
        try:
            sweep = parse_at(sweep_text)
        except ValueError:
            sweep, note = default_sweep, (f" (OPERATOR_SWEEP_AT {sweep_text!r} is unreadable;"
                                          " anchored to the default sweep time)")
    at = pass_at(sweep, lead)
    return at, (f"daily at {at}, {lead} min before the operator's sweep at {sweep}"
                f"{note}; caught up after a restart")


# --- gh ------------------------------------------------------------------------

class GhUnavailable(Exception):
    """gh could not answer: not installed, not logged in, offline, rate-limited.
    Its message is the reason, for an `unknown` result."""


def gh_cli(*args, timeout: float = 60) -> str:
    """Run `gh <args>` and return stdout, or raise GhUnavailable."""
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True,
                           timeout=timeout, errors="replace")
    except FileNotFoundError:
        raise GhUnavailable("gh is not installed")
    except subprocess.TimeoutExpired:
        raise GhUnavailable(f"gh timed out after {timeout:g}s")
    except OSError as e:
        raise GhUnavailable(f"gh could not run: {e}")
    if r.returncode != 0:
        err = " ".join((r.stderr or r.stdout or "").split())[:200]
        raise GhUnavailable(f"gh {args[0] if args else ''} failed: {err or r.returncode}")
    return r.stdout


def github_repo(rec: dict) -> str:
    """"owner/name" for a project whose scope names a GitHub repository, else ""."""
    repo = str(((rec or {}).get("scope") or {}).get("repo") or "").strip()
    m = re.fullmatch(r"(?:https?://|git@)?(?:www\.)?github\.com[/:]"
                     r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?", repo)
    return f"{m.group(1)}/{m.group(2)}" if m else ""


# --- the "ci" check: GitHub Actions on the base branch ---------------------------

#: A completed run's conclusions that say the code is broken...
FAILED = ("failure", "timed_out", "startup_failure")
#: ...and that it is not. Anything else (cancelled, skipped, neutral, stale,
#: action_required) says nothing either way and is looked past.
PASSED = ("success",)
#: Events that run on the branch itself. A pull request's run is about the
#: pull request, whatever its head branch is called.
#: Not workflow_run: its head_branch is the triggering run's, which can be a
#: fork's pull request from a branch that happens to be called main.
BRANCH_EVENTS = ("push", "schedule", "workflow_dispatch", "merge_group",
                 "repository_dispatch", "release")

#: Completed runs read per workflow: the latest, and enough before it to see
#: a test failing on and off.
HISTORY = 10
#: Logs fetched per workflow per probe that are not already cached. A log is
#: the one expensive read; each run's is fetched once and remembered.
LOGS_PER_PROBE = 4
#: How long a log that could not be read (expired, or gh failing) is left
#: before it is tried again, so it does not take every probe's budget.
LOG_RETRY_S = 24 * 3600
#: Bytes of a log looked at, from the end, where the summaries are.
LOG_TAIL = 2_000_000

_LOG_PREFIX = re.compile(r"^[^\t]*\t[^\t]*\t")
_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?")


def log_text(raw: str) -> str:
    """`gh run view --log-failed` output with its job/step columns and
    timestamps taken off each line."""
    return "\n".join(_STAMP.sub("", _LOG_PREFIX.sub("", line))
                     for line in (raw or "")[-LOG_TAIL:].splitlines())


def log_failures(text: str) -> list[str]:
    """The failing tests a CI log names, in order, once each.

    xcodebuild prints a "Failing tests:" block (one indented name per line,
    ended by a blank line); everything else is what verify.failed_tests
    already reads from a local run.
    """
    names: list[str] = []
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "Failing tests:":
            continue
        for item in lines[i + 1:]:
            if not item.strip():
                break
            name = item.strip()
            name = name[:-2] if name.endswith("()") else name
            if name not in names:
                names.append(name)
    for name in verify.failed_tests(text):
        if name not in names:
            names.append(name)
    return names[:50]


def _api_json(gh, path: str):
    out = gh("api", "-H", "Accept: application/vnd.github+json", path)
    try:
        return json.loads(out or "null")
    except json.JSONDecodeError:
        raise GhUnavailable(f"gh api {path.split('?')[0]} returned something that is not JSON")


def latest_decisive(runs) -> dict | None:
    """The newest run that passed or failed; cancelled and skipped ones say
    nothing about the code and are looked past."""
    for r in runs:
        if r.get("conclusion") in FAILED + PASSED:
            return r
    return None


def branch_runs(runs, branch: str) -> list[dict]:
    """Completed runs on the branch itself, newest first."""
    keep = [r for r in runs or ()
            if r.get("status") == "completed" and r.get("head_branch") == branch
            and r.get("event") in BRANCH_EVENTS]
    return sorted(keep, key=lambda r: (r.get("created_at") or "", r.get("id") or 0),
                  reverse=True)


def flaky(runs, names_of) -> list[dict]:
    """Tests failing on and off: [{test, shas, runs}].

    A test is flaky when it failed in at least two runs on different commits
    with a run that passed between them -- chronologically between, so a
    test that broke and stayed broken is not "flaky", and neither is one
    failure retried on the same commit. `runs` is newest first; `names_of(run)`
    is that run's failing tests, or None when they could not be read.
    """
    decisive = [r for r in reversed(runs) if r.get("conclusion") in FAILED + PASSED]
    seen: dict = {}
    for i, r in enumerate(decisive):
        if r.get("conclusion") not in FAILED:
            continue
        for name in names_of(r) or ():
            seen.setdefault(name, []).append(i)
    out = []
    for name, at in seen.items():
        shas = []
        for a, b in zip(at, at[1:]):
            if (decisive[a].get("head_sha") != decisive[b].get("head_sha")
                    and any(decisive[j].get("conclusion") in PASSED for j in range(a + 1, b))):
                for x in (a, b):
                    sha = decisive[x].get("head_sha") or ""
                    if sha not in shas:
                        shas.append(sha)
        if shas:
            out.append({"test": name, "shas": shas,
                        "runs": [decisive[i].get("id") for i in at]})
    return sorted(out, key=lambda f: f["test"])


def _pass(n: int) -> str:
    return f"{n} workflow{'s pass' if n != 1 else ' passes'}"


def ci_probe(rec: dict, gh) -> dict:
    """The latest completed run of every active workflow on the base branch.

    Per workflow, not the latest run overall: a path-filtered workflow
    (Cadence's "Backend tests" runs only when supabase/ changes) is often
    days behind the rest, and its latest run is still the last word on it.
    """
    repo = github_repo(rec)
    if not repo:
        return unknown("no GitHub repository in this project's scope")
    try:
        branch = ((rec.get("scope") or {}).get("branch") or "").strip()
        if not branch:
            meta = _api_json(gh, f"repos/{repo}") or {}
            branch = meta.get("default_branch") or ""
            if not branch:
                return unknown(f"{repo} has no default branch to read")
        flows = (_api_json(gh, f"repos/{repo}/actions/workflows?per_page=100") or {})
        flows = [w for w in flows.get("workflows") or () if w.get("state") == "active"]
        rows = []
        for w in sorted(flows, key=lambda w: w.get("name") or ""):
            got = _api_json(gh, f"repos/{repo}/actions/workflows/{w['id']}/runs"
                                f"?branch={branch}&status=completed&per_page={HISTORY * 2}") or {}
            runs = branch_runs(got.get("workflow_runs"), branch)[:HISTORY]
            last = latest_decisive(runs)
            if last:
                rows.append((w, runs, last))
    except GhUnavailable as e:
        return unknown(str(e))
    link = f"https://github.com/{repo}/actions"
    if not rows:
        return result(OK, f"no workflow has run on {branch}", link=link)

    fetched = {"n": 0}

    def names_of(run):
        """A failed run's failing tests: cached, else read from its log --
        at most LOGS_PER_PROBE fresh reads per workflow, each once ever."""
        rid = str(run.get("id"))
        hit = gh.cached(rid) if hasattr(gh, "cached") else None
        if isinstance(hit, list):
            return hit
        if isinstance(hit, dict) and time.time() - (hit.get("unreadable") or 0) < LOG_RETRY_S:
            return None                       # tried lately; its budget goes elsewhere
        if fetched["n"] >= LOGS_PER_PROBE:
            return None
        fetched["n"] += 1
        try:
            names = log_failures(log_text(gh("run", "view", rid, "-R", repo, "--log-failed",
                                             timeout=120)))
        except GhUnavailable as e:
            log.info("health: no log for %s run %s: %s", repo, rid, e)
            if hasattr(gh, "cache"):
                gh.cache(rid, {"unreadable": time.time()})
            return None
        if hasattr(gh, "cache"):
            gh.cache(rid, names)
        return names

    failing, warned, evidence, fps, first_link = [], [], [], [], ""
    for w, runs, last in rows:
        fetched["n"] = 0
        name, sha = w.get("name") or w.get("path"), (last.get("head_sha") or "")[:8]
        bad = last.get("conclusion") in FAILED
        tests = (names_of(last) or []) if bad else []
        flakes = flaky(runs, names_of)
        flaky_names = {f["test"] for f in flakes}
        if bad:
            failing.append((name, sha, tests, flakes))
            fps.append(f"{w.get('id')}@{last.get('head_sha')}")
            first_link = first_link or last.get("html_url") or ""
            evidence.append(f"{name}: {last.get('conclusion')} at {sha} "
                            f"({last.get('created_at')}) {last.get('html_url') or ''}".strip())
            if tests:
                evidence.append(f"  failing: {', '.join(tests[:6])}"
                                + (f" +{len(tests) - 6} more" if len(tests) > 6 else ""))
        else:
            evidence.append(f"{name}: passed at {sha} ({last.get('created_at')})")
        for f in flakes:
            evidence.append(f"  flaky: {f['test']} failed on "
                            f"{', '.join(s[:8] for s in f['shas'])}, passing between")
        if not bad and flakes:
            warned.append((name, sorted(flaky_names)))
            first_link = first_link or next(
                (r.get("html_url") for r in runs if r.get("conclusion") in FAILED), "")
    if failing:
        parts = []
        for name, sha, tests, flakes in failing:
            bit = f"{name} failed at {sha}"
            if tests:
                bit += f" ({', '.join(tests[:2])}{' …' if len(tests) > 2 else ''})"
            known = [t for t in tests if t in {f['test'] for f in flakes}]
            if known:
                bit += " — flaky: failed on other commits too, passing between"
            parts.append(bit)
        return result(FAIL, f"on {branch}: {'; '.join(parts)}", evidence,
                      first_link or link, fingerprint=",".join(sorted(fps)))
    if warned:
        bits = [f"{name}: {', '.join(t[:2])}" for name, t in warned]
        return result(WARN, f"{_pass(len(rows))} on "
                            f"{branch}, but flaky — {'; '.join(bits)}", evidence,
                      first_link or link)
    shas = sorted({(last.get("head_sha") or "")[:8] for _, _, last in rows})
    return result(OK, f"{_pass(len(rows))} on "
                      f"{branch} (latest at {', '.join(shas[:3])})", evidence, link)


def ci_short(rec: dict) -> str:
    return "no CI runs" if (rec.get("summary") or "").startswith("no workflow") else "CI green"


def ci_identity(rec: dict) -> str:
    """One red spell of the same workflows, whatever is pushed meanwhile: the
    fingerprint names the commit, so every push to a red main would read as a
    new trouble, and a dismissed fix would be proposed again the next day."""
    if rec.get("status") != FAIL:
        return ""
    flows = sorted({fp.split("@")[0] for fp in (rec.get("fingerprint") or "").split(",") if fp})
    return f"fail@{int(rec.get('last_changed') or 0)}:{','.join(flows)}"


register("ci", ci_probe, applies=lambda rec: bool(github_repo(rec)),
         about="GitHub Actions: the latest completed run of each workflow on the base branch",
         short=ci_short).identity = ci_identity

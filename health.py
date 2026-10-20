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

#: Default schedule (HEALTH_EVERY), and the values that switch it off.
DEFAULT_EVERY = "1h"
OFF = ("off", "none", "0", "false", "no")


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

    def __init__(self, name: str, probe, applies=None, about: str = ""):
        self.name, self.probe, self.about = name, probe, about
        self.applies = applies or (lambda rec: True)


REGISTRY: dict[str, Check] = {}


def register(name: str, probe, applies=None, about: str = "") -> Check:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name or ""):
        raise ValueError(f"bad check name {name!r}")
    check = Check(name, probe, applies, about)
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

    def _save(self) -> None:
        if self.readonly:
            raise RuntimeError("a read-only health store does not save")
        jsonstore.save(self.path, {"version": self.VERSION, "checks": self._checks,
                                   "cache": self._cache})

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
                   "attention": (prior or {}).get("attention") or "",
                   "attention_for": (prior or {}).get("attention_for") or ""}
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


def context_section(rows, now: float | None = None, evidence: int = 4) -> str:
    """The operator's "Project health" section: every non-ok check in detail,
    with its evidence and link; the ok ones counted, not listed."""
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


def parse_every(text: str) -> float | None:
    """HEALTH_EVERY -> seconds, or None for off. `90`, `90s`, `15m`, `2h`.
    Raises ValueError on anything else, or on less than a minute."""
    t = (text if text is not None else DEFAULT_EVERY).strip().lower() or DEFAULT_EVERY
    if t in OFF:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smh]?)", t)
    if not m:
        raise ValueError(f"HEALTH_EVERY {text!r} is not a duration like 30m or 1h")
    secs = float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]
    if secs < 60:
        raise ValueError(f"HEALTH_EVERY {text!r} is under a minute")
    return secs


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


register("ci", ci_probe, applies=lambda rec: bool(github_repo(rec)),
         about="GitHub Actions: the latest completed run of each workflow on the base branch")

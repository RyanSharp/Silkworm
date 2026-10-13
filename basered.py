"""Notice a base that fails its own tests once, instead of once per task.

On 2026-10-09 a deliberate review-by date in trader's baseline file passed, and
from then on trader's main failed its own suite with nobody's change on it.
Every trader task after that failed verification on that one test: sent back,
re-run, parked as "the change was never verified" -- and one agent spent a
whole run proving the failure was not its own. Nothing about the task could
have fixed it, and the next one was headed the same way.

So when a task's tests fail, the project's base is asked the same question
before anyone is blamed: the same suite, on a clean checkout of the base
commit, run at most once per commit (`check`). If the base passes, the failure
is the task's and is handled exactly as it always was. If the base fails:

  - the project is marked `main_failing`, with the failing tests and the
    commit, and gets *one* needs-you item (`mark_red`) -- not one per task;
  - a task whose failures are all among the base's (`only_failing_on_base`)
    is held, not sent back, failed or refused; a landing in the same position
    is held at stage merge.BASE_FAILING;
  - the runner takes no new implementor work for the project (tasks.claim's
    `hold_implementors`), since every one of them would only be held too;
  - and when a later base commit passes (`recover`), all of that is undone:
    the item closes, and the held tasks are verified or landed again.

A task that fails a test the base passes is a real failure, whatever else is
going on. That comparison is the one thing here that must not err towards
holding: a held task is one nobody is told about.

The bookkeeping lives here, against whatever stores it is handed; running the
suite, checking out the base and talking to Slack are the bot's.
"""

import logging
import threading
import time

import tasks
import verify

log = logging.getLogger("silkworm.basered")

#: How many base commits' results a project remembers. The base moves forward;
#: anything older than this is not going to be asked about again.
MAX_CHECKS = 8

#: The `source` of the one needs-you item a failing base gets.
SOURCE = "base-red"

#: How many failing tests a line names before it counts the rest.
SHOW = 4


def only_failing_on_base(task_failed, base_failed) -> bool:
    """Whether every test a task fails also fails on its base, untouched.

    Not "any in common": a task that breaks a second test while the base is
    already broken has broken something, and holding it would hide that until
    the base was fixed -- and then report it as new. And a run that names no
    failing test at all (a crash, a timeout, a launch failure) cannot be said
    to fail the base's tests: nothing in it can be compared, so it is real.
    """
    mine = set(task_failed or ())
    return bool(mine) and mine <= set(base_failed or ())


_check_locks: dict[str, threading.Lock] = {}
_check_guard = threading.Lock()
#: Around reading and writing a project's red/green state, so a detection and a
#: recovery racing each other cannot leave the item open over a green base.
_state_lock = threading.RLock()


def _check_lock(slug: str) -> threading.Lock:
    with _check_guard:
        return _check_locks.setdefault(slug, threading.Lock())


def cached(project_store, slug: str, sha: str) -> dict | None:
    """What the suite already said about this base commit, if anything."""
    rec = project_store.get(slug) or {}
    hit = (rec.get("base_checks") or {}).get(sha)
    return dict(hit) if hit else None


def check(project_store, slug: str, sha: str, run) -> dict | None:
    """The project's suite on base commit `sha`: {sha, ok, failed, at}.

    `run(sha)` runs it (the bot's job: a clean checkout, verify.run) and is
    called at most once per commit. Two tasks failing at once both wait on one
    run rather than starting two -- the second finds the first's answer.

    None when there is no answer: the suite could not be run, or never got as
    far as a test. Nothing is cached then, so the next failure asks again, and
    the caller treats its own failure as real -- an unknown base is not a red
    one.
    """
    if not slug or not sha:
        return None
    with _check_lock(slug):
        hit = cached(project_store, slug, sha)
        if hit:
            return hit
        try:
            result = run(sha) or {}
        except Exception:
            log.exception("running %s's suite on its base %s failed", slug, sha[:8])
            return None
        if not result.get("ran") or verify.launch_failure(result):
            log.warning("%s's suite could not be run on its base %s: %s", slug,
                        sha[:8], str(result.get("output", ""))[-200:])
            return None
        ok = bool(result.get("ok"))
        entry = {"sha": sha, "ok": ok, "at": time.time(),
                 "failed": [] if ok else list(
                     result.get("failed")
                     or verify.failed_tests(result.get("output", "")))}
        rec = project_store.get(slug) or {}
        checks = dict(rec.get("base_checks") or {})
        checks[sha] = entry
        if len(checks) > MAX_CHECKS:
            for old in sorted(checks, key=lambda s: checks[s].get("at") or 0)[
                    :len(checks) - MAX_CHECKS]:
                del checks[old]
        project_store.update(slug, base_checks=checks)
        log.info("%s's base %s %s its own suite%s", slug, sha[:8],
                 "passes" if ok else "fails",
                 "" if ok else f" ({len(entry['failed'])} named)")
        return dict(entry)


def failing(project_store) -> set:
    """Projects whose base is known to fail its own tests."""
    return {p["slug"] for p in project_store.all() if p.get("main_failing")}


def named(tests, show: int = SHOW) -> str:
    tests = list(tests or ())
    if not tests:
        return "a failure that names no test"
    out = ", ".join(tests[:show])
    return out + (f" +{len(tests) - show} more" if len(tests) > show else "")


def line(slug: str, state: dict) -> str:
    """The one line a failing base gets, wherever it is shown."""
    return f"{slug} main is failing: {named(state.get('tests'))}"


def _goal(slug: str, state: dict) -> str:
    tests = state.get("tests") or []
    listing = "\n".join(f"  {t}" for t in tests) or "  (the run named no test)"
    return (f"{slug}'s base, {state.get('base') or 'its base branch'} at "
            f"{(state.get('sha') or '')[:12]}, fails its own test suite with no "
            f"task's change on it. Failing there:\n\n{listing}\n\n"
            "Until a later base commit passes, implementor work on this project "
            "is not started, and tasks failing only these tests are held rather "
            "than sent back. Fix the base, and they are re-verified and landed "
            "on their own.")


def mark_red(project_store, task_store, slug: str, result: dict,
             base: str = "", scope: dict | None = None) -> tuple[dict, bool]:
    """Record that `slug`'s base fails, and see it has one needs-you item.

    Returns (state, news). `news` is True only the first time -- the moment
    it is worth telling anyone. A later commit that still fails updates the
    record and the item's wording, and says nothing new.

    The item is an ordinary task in needs_input, so every surface that lists
    what needs you -- the board, the dashboard, the digest -- lists it once.
    It is not re-opened for a commit it was closed on: a person who dismissed
    it has seen it. A later commit that still fails opens it again.
    """
    with _state_lock:
        rec = project_store.get(slug) or {}
        prior = rec.get("main_failing") or {}
        sha = result.get("sha") or ""
        state = {"sha": sha, "base": base or prior.get("base") or "",
                 "tests": list(result.get("failed") or []),
                 "since": prior.get("since") or time.time(),
                 "attention": prior.get("attention") or ""}
        item = task_store.get(state["attention"]) if state["attention"] else None
        title = line(slug, state)[:80]
        if item and item.get("state") not in tasks.ENDED:
            task_store.update(item["id"], title=title, goal=_goal(slug, state))
        elif not item or prior.get("sha") != sha:
            item = task_store.create(
                _goal(slug, state), title=title, state=tasks.NEEDS_INPUT,
                role="assistant", driver="queue", isolate=False, source=SOURCE,
                source_ref=f"{slug}@{sha[:12]}", project=slug,
                scope=dict(scope or rec.get("scope") or {}))
            state["attention"] = item["id"]
        project_store.update(slug, main_failing=state)
        return state, not prior


def hold(task_store, tid: str, kind: str, sha: str, approved: bool = False) -> None:
    """Note on a task that it waits on its base, and why it may go again."""
    task_store.update(tid, held_base={"kind": kind, "sha": sha,
                                      "approved": bool(approved),
                                      "at": time.time()})


def held(task_store, slug: str) -> list[dict]:
    """The project's tasks waiting on its base, oldest first."""
    return sorted((t for t in task_store.all().values()
                   if t.get("held_base") and (t.get("project") or "") == slug),
                  key=lambda t: t.get("created") or 0)


def recover(project_store, task_store, slug: str, sha: str) -> tuple[dict, list]:
    """The base passes at `sha`: clear the state and close the item.

    Returns (the state that was cleared, the tasks that were held), or
    ({}, []) if the project was not marked. Each held task keeps its
    `held_base` -- releasing it is the caller's, and clears it -- so a crash
    between here and there leaves the tasks findable rather than forgotten.
    """
    with _state_lock:
        rec = project_store.get(slug) or {}
        state = rec.get("main_failing") or {}
        if not state:
            return {}, []
        project_store.update(slug, main_failing=None)
        item = task_store.get(state.get("attention") or "")
        # Not one somebody sent off to fix the base: that turn ends on its own.
        if item and item.get("state") not in tasks.ENDED + (tasks.RUNNING,):
            try:
                task_store.transition(item["id"], tasks.DONE,
                                      f"the base passes again at {sha[:8]}")
            except Exception:
                log.exception("could not close %s", item["id"])
        log.warning("%s's base passes again at %s", slug, sha[:8])
        return state, held(task_store, slug)

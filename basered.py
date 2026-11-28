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
    `hold_implementors`), since every one of them would only be held too --
    except work filed from the item's own conversation (`fix_item`), or filed
    elsewhere naming the item as what it fixes (`named_fix`), or linked to it
    afterwards by the operator (bot.link_base_fix): the attempt to fix the
    base, which would otherwise wait for itself;
  - and when a later base commit passes (`recover`), all of that is undone:
    the item closes, and the held tasks are verified or landed again.

A task that fails a test the base passes is a real failure, whatever else is
going on. That comparison is the one thing here that must not err towards
holding: a held task is one nobody is told about.

The bookkeeping lives here, against whatever stores it is handed; running the
suite, checking out the base and talking to Slack are the bot's.
"""

import logging
import re
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


def fix_item(task_store, key: str, caller_task: str = "") -> dict | None:
    """The base-red item a filing on thread `key` is fixing, or None.

    Work filed from the item's own conversation -- the run it was sent back
    for (`caller_task`), or a reply on its thread -- is an attempt at the very
    failure that holds the project's implementors, and is linked to it as its
    parent so the runner lets it through (tasks.claim's `fixes_from`). Only the
    item itself can vouch: anything else filed while the base is red waits.
    """
    for t in task_store.all().values():
        if t.get("source") != SOURCE:
            continue
        if (caller_task and t.get("id") == caller_task) or (
                key and t.get("thread") == key):
            return t
    return None


def open_item(project_store, task_store, slug: str) -> dict | None:
    """The needs-you item of `slug`'s failing base, while both are current:
    the project is still marked and the item has not ended. Else None."""
    state = (project_store.get(slug) or {}).get("main_failing") or {} if slug else {}
    item = task_store.get(state.get("attention") or "") if state else None
    if item and item.get("source") == SOURCE and item.get("state") not in tasks.ENDED:
        return item
    return None


def named_fix(project_store, task_store, slug: str, goal: str, fixes: str = "",
              on_users_word: bool = False) -> dict | None:
    """The base-red item a filing made away from its thread fixes, or None.

    `fix_item` only vouches for work filed on the item's own thread, so a fix
    the operator filed from its home thread on your word (tsk_5c129269af,
    2026-11-25, to fix the base tsk_c903a669ad tracks) was held behind the
    very failure it was filed to end, and never ran. A filing counts as the
    fix when it names the item: `fixes` is the item's id, or the goal names
    it -- or, on your word only, `fixes` is "base", meaning `slug`'s red base
    whichever item tracks it. The goal naming it counts on your word only, and
    only under its project: a goal may mention a red base in passing ("not
    the one tsk_x tracks"), and that is no claim to fix it. Only an open item
    of a project still failing: naming one that has closed links nothing.
    With no `slug`, only `fixes` can name an item, of any failing project.
    """
    fixes = (fixes or "").strip()
    for s in ([slug] if slug else sorted(failing(project_store))):
        item = open_item(project_store, task_store, s)
        if not item:
            continue
        if fixes == item["id"] or (fixes == "base" and on_users_word and slug) or (
                on_users_word and slug and not fixes and re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(item['id'])}(?![A-Za-z0-9_])",
                    goal or "")):
            return item
    return None


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
    between here and there leaves them for `stranded` to find.
    """
    with _state_lock:
        rec = project_store.get(slug) or {}
        state = rec.get("main_failing") or {}
        if not state:
            return {}, []
        project_store.update(slug, main_failing=None)
        item = task_store.get(state.get("attention") or "")
        # Not one somebody sent off to fix the base: that turn ends on its own.
        # One sent off and still waiting in the queue has nothing left to do;
        # it cannot go straight to done from there, so it is cancelled.
        if item and item.get("state") not in tasks.ENDED + (tasks.RUNNING,):
            to = tasks.CANCELLED if item.get("state") == tasks.QUEUED else tasks.DONE
            try:
                task_store.transition(item["id"], to,
                                      f"the base passes again at {sha[:8]}")
            except Exception:
                log.exception("could not close %s", item["id"])
        log.warning("%s's base passes again at %s", slug, sha[:8])
        return state, held(task_store, slug)


def stranded(project_store, task_store) -> dict[str, list[dict]]:
    """Held tasks whose project's base is not failing: {slug: [task, ...]}.

    Recovery clears the project first and then releases its tasks one at a
    time, each a suite run long. A restart part-way leaves the rest held over
    a green base, and a hold written just after a recovery read its list is
    the same shape. Nothing else would ever ask about them again.
    """
    red = failing(project_store)
    out: dict[str, list[dict]] = {}
    for t in task_store.all().values():
        slug = t.get("project") or ""
        if t.get("held_base") and slug not in red:
            out.setdefault(slug, []).append(t)
    for rows in out.values():
        rows.sort(key=lambda t: t.get("created") or 0)
    return out


# --- a checkout that fails the suite its own base passes ----------------------
#
# The other half of the same question, asked from the other side. A landing
# runs the suite twice on one commit: in the task's worktree after the rebase,
# and in the project's own checkout after the fast-forward. When the second
# fails and the first passed, the tree is identical, so what differs is the
# checkout -- its untracked or live files, .env, data/. merge.land asks the
# checkout once more at the base without the branch, and if the same tests
# fail there the landing comes back at stage merge.CHECKOUT_FAILING.
#
# That is not the branch's to fix, and not the base's either: a clean checkout
# of the base passes. So it gets what a failing base gets, kept apart from it:
# the project is marked `checkout_failing`, gets one needs-you item, and the
# landing is held (`held_checkout`) rather than refused. Implementor work is not
# paused -- the work is fine; only landing in that checkout is not. It is landed
# again when the checkout passes (a later landing there succeeds, or the
# periodic look finds the suite green) or when a person closes the item.

#: The `source` of a failing checkout's one needs-you item.
CHECKOUT_SOURCE = "checkout-red"


def checkout_line(slug: str, state: dict) -> str:
    """The one line a failing checkout gets, wherever it is shown."""
    return f"{slug} checkout fails tests its base passes: {named(state.get('tests'))}"


def _checkout_goal(slug: str, state: dict, cwd: str = "") -> str:
    tests = state.get("tests") or []
    listing = "\n".join(f"  {t}" for t in tests) or "  (the run named no test)"
    return (f"{slug}'s checkout{f' at {cwd}' if cwd else ''} fails its own test "
            f"suite on {state.get('base') or 'its base branch'} at "
            f"{(state.get('sha') or '')[:12]}, with or without a landing's branch on "
            f"it, while the same tree passes in a clean worktree. Failing there:\n\n"
            f"{listing}\n\nSo the failure comes from something the checkout has "
            "that a fresh checkout does not -- untracked or live files, .env, "
            "data/ -- not from any branch. Landings are held rather than refused "
            "until the checkout passes; they are landed again on their own then, "
            "or when this item is closed.")


def mark_checkout(project_store, task_store, slug: str, result: dict,
                  base: str = "", scope: dict | None = None) -> tuple[dict, bool]:
    """Record that `slug`'s checkout fails, and see it has one needs-you item.

    `result` is a merge.CHECKOUT_FAILING landing outcome ({failed, onto}).
    Returns (state, news); `news` only the first time. Every later landing held
    the same way updates the wording of the one item rather than opening more.
    """
    with _state_lock:
        rec = project_store.get(slug) or {}
        prior = rec.get("checkout_failing") or {}
        state = {"sha": result.get("onto") or result.get("sha") or "",
                 "base": base or result.get("base") or prior.get("base") or "",
                 "tests": list(result.get("checkout_failed") or result.get("failed") or []),
                 "since": prior.get("since") or time.time(),
                 "attention": prior.get("attention") or ""}
        item = task_store.get(state["attention"]) if state["attention"] else None
        scope = dict(scope or rec.get("scope") or {})
        title = checkout_line(slug, state)[:80]
        goal = _checkout_goal(slug, state, scope.get("cwd") or "")
        if item and item.get("state") not in tasks.ENDED:
            task_store.update(item["id"], title=title, goal=goal)
        else:
            item = task_store.create(
                goal, title=title, state=tasks.NEEDS_INPUT, role="assistant",
                driver="queue", isolate=False, source=CHECKOUT_SOURCE,
                source_ref=f"{slug}@{state['sha'][:12]}", project=slug, scope=scope)
            state["attention"] = item["id"]
        project_store.update(slug, checkout_failing=state)
        return state, not prior


def hold_checkout(task_store, tid: str, sha: str, approved: bool = False) -> None:
    """Note on a task that its landing waits on its project's checkout."""
    task_store.update(tid, held_checkout={"sha": sha, "approved": bool(approved),
                                          "at": time.time()})


def checkout_failing(project_store) -> set:
    """Projects whose checkout is known to fail tests its base passes."""
    return {p["slug"] for p in project_store.all() if p.get("checkout_failing")}


def held_for_checkout(task_store, slug: str) -> list[dict]:
    """The project's tasks whose landing waits on its checkout, oldest first."""
    return sorted((t for t in task_store.all().values()
                   if t.get("held_checkout") and (t.get("project") or "") == slug),
                  key=lambda t: t.get("created") or 0)


def checkout_item_closed(project_store, task_store, slug: str) -> bool:
    """Whether a person has closed the failing checkout's item: their word
    that it is fixed, worth a landing again (which runs the suite anyway)."""
    state = (project_store.get(slug) or {}).get("checkout_failing") or {}
    item = task_store.get(state.get("attention") or "") if state else None
    return bool(state) and bool(item) and item.get("state") in tasks.ENDED


def clear_checkout(project_store, task_store, slug: str, why: str) -> tuple[dict, list]:
    """The checkout passes (or a person says it does): clear the mark and close
    the item. Returns (the state cleared, the tasks held), or ({}, []) if the
    project was not marked. Releasing them is the caller's; each keeps its
    `held_checkout` until then, so a crash part-way leaves them findable
    (`stranded_checkout`)."""
    with _state_lock:
        rec = project_store.get(slug) or {}
        state = rec.get("checkout_failing") or {}
        if not state:
            return {}, []
        project_store.update(slug, checkout_failing=None)
        item = task_store.get(state.get("attention") or "")
        if item and item.get("state") not in tasks.ENDED + (tasks.RUNNING,):
            to = tasks.CANCELLED if item.get("state") == tasks.QUEUED else tasks.DONE
            try:
                task_store.transition(item["id"], to, why[:200])
            except Exception:
                log.exception("could not close %s", item["id"])
        log.warning("%s's checkout: %s", slug, why)
        return state, held_for_checkout(task_store, slug)


def stranded_checkout(project_store, task_store) -> dict[str, list[dict]]:
    """Tasks held for a checkout whose project is no longer marked failing."""
    red = checkout_failing(project_store)
    out: dict[str, list[dict]] = {}
    for t in task_store.all().values():
        slug = t.get("project") or ""
        if t.get("held_checkout") and slug not in red:
            out.setdefault(slug, []).append(t)
    for rows in out.values():
        rows.sort(key=lambda t: t.get("created") or 0)
    return out

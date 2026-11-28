"""The shared harness for Silkworm's regression suite.

The suite is every test_* function in tests/test_*.py, run by tests/run.py:

    python3 tests/run.py                 # everything, as `silkworm test` runs it
    python3 tests/run.py test_health     # just these files (the guards still check all)

Every case corresponds to a real failure: a turn that hung for a day, a thread
that silently lost its history, a reply that was never delivered. They run
without Slack, without a bot, and without spending anything.

This file holds what the test files share: check() and its tallies, lift()
and what it needs, discovery, and the fixtures more than one test file uses.
Each test file starts `from harness import *` and gets all of it, underscored
names included. A fixture only one file uses lives in that file.
"""

import ast
import contextlib
import io
import json
import logging
import os
import re as _re
import shutil
import subprocess
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import procs                                     # noqa: E402
import recovery                                  # noqa: E402
from claude_runner import (ClaudeError, ClaudeStopped,  # noqa: E402
                           ClaudeTimeout, RunHandle)
from store import SessionStore                   # noqa: E402
import artifacts                                 # noqa: E402

BASE = Path(__file__).resolve().parent.parent
TESTS = BASE / "tests"
sys.path.insert(0, str(TESTS))
PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'✔' if cond else '✘'} {name}" + (f" — {detail}" if detail and not cond else ""))


def tmp_store():
    return SessionStore(Path(tempfile.mkdtemp()) / "s.json")


_BOT_TREE = None


def _bot_tree():
    """bot.py's syntax tree, parsed once for every lift in the run."""
    global _BOT_TREE
    if _BOT_TREE is None:
        _BOT_TREE = ast.parse((BASE / "bot.py").read_text())
    return _BOT_TREE


def _binds(node) -> set:
    """The module-level names a top-level bot.py statement binds."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, ast.Assign):
        return {n.id for t in node.targets for n in ast.walk(t)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    if isinstance(node, ast.AnnAssign):     # `_landing_now: set[str] = set()`
        return {node.target.id} if isinstance(node.target, ast.Name) else set()
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return {(a.asname or a.name).split(".")[0] for a in node.names}
    return set()


#: Helpers other lifted functions call as part of their own behaviour, so a
#: lifted caller gets the real one rather than a stub or a NameError. Sending
#: work back is one mechanism shared by the dashboard and the review gate.
#: Pulled in transitively, unless the test supplied its own.
SHARED_HELPERS = ("send_back", "review_addendum", "task_base", "unmerged_survey",
                  "held_reason", "edit_task", "_task_title", "take_step", "release_steps",
                  "release_drain_refusal", "unreviewed_role", "filer_role", "health_rows",
                  "user_grant", "record_user_decision", "outage_summary",
                  "dashboard_owner", "grant_starter", "plan_guard_refused", "release_lift",
                  "recommend_task")


#: What a lifted function gets for a bot.py global that is not a module and
#: the test did not supply -- the state an install that set nothing up has.
#: Everything else it reads must be supplied, lifted, or one of bot.py's own
#: imports (supplied as the real module), or the lift fails naming it.
STAND_INS = {
    "log": lambda: logging.getLogger("test"),
    # The phone's notifier: a test that is not about push sends nothing.
    "PUSH": lambda: types.SimpleNamespace(event=lambda *a, **k: None, reply=lambda *a, **k: None,
                                          task_moved=lambda *a, **k: None, resume=lambda: None),
    # filer_role asks who the operator is; a test that never set one up has
    # no operator, so nothing it files is the operator's.
    "OPERATOR": lambda: types.SimpleNamespace(thread=lambda: ""),
    # The all-projects views leave out archived projects' work; a test that
    # archived nothing has none archived.
    "archived_projects": lambda: (lambda: set()),
    # user_grant asks the bot's live grants; a test that made none has none,
    # so nothing it asks for is on the user's word.
    "OPERATOR_GRANTS": lambda: __import__("operator_role").Grants(),
    # dashboard_owner reads VIZ_TAILSCALE_LOGIN from the environment, else
    # .env under BASE_DIR -- here, an empty directory, so none; send_digest
    # reads the outage log there.
    "BASE_DIR": lambda: Path(tempfile.mkdtemp()),
    # handle_status reports the pinned checkout and when this process booted.
    "BOOTED": lambda: 0.0,
    "CODE_DIR": lambda: BASE,
    # health_rows reads the bot's health store; a test that set none up has
    # none, which reads as "could not be read", never as "all healthy".
    "HEALTH": lambda: None,
    # A project with no ci_gate gets no CI gate, and one landing at a time is
    # what a test that starts one landing has anyway.
    "ci_gate_for": lambda: (lambda *a, **k: None),
    "landing_serial": lambda: (lambda cwd: contextlib.nullcontext()),
    # resolve_review starts a CI-gated landing on its own thread; a test with
    # no CI-gated project never gets there.
    "start_landing": lambda: (lambda *a, **k: False),
    # live_worktree_tasks keeps the checkouts of landings under way; a test
    # that started none has none.
    "_landing_now": lambda: set(),
}


#: Names each router reads, in its own body, on routes a test of one other
#: route does not drive -- declared absent (see lift) rather than supplied.
#: A test that drives one of these routes supplies the name instead.
TASK_ROUTES = ("CLAUDE_CWD", "HEALTH_WARN_DAYS", "_spend", "_with_costs", "approve_task",
               "board_release", "board_release_tags", "board_survey", "decide_findings", "land_or_drop", "operator_refused",
               "refile_to_land", "link_base_fix",
               "project_store", "run_email_ingest", "stop_task", "task_changes", "task_contained",
               "tell_thread", "tidy_checkout")
PROJECT_ROUTES = ("GITHUB_OWNER", "install_git_guards", "operator_refused",
                  "retire_project", "set_ideate_lenses", "task_store", "unregistered_repos")
LANDING_ROUTES = ("LANDING_UNDERWAY", "hold_landing_for_base", "hold_landing_for_checkout",
                  "record_landing", "repo_guard", "task_store")
#: take_step's, lifted along with handle_tasks: landing and releasing.
STEP_ROUTES = ("LANDING_UNDERWAY", "RELEASE_STARTED", "release_command")


class LiftError(NameError):
    """A lifted function reads a name nothing will bind."""


def _global_reads(nodes) -> tuple:
    """(names the code reads as globals, names it binds at module level)."""
    import symtable
    src = ast.unparse(ast.Module(body=list(nodes), type_ignores=[]))
    top = symtable.symtable(src, "<lift>", "exec")
    reads, binds = set(), set()

    def walk(t, is_top):
        for s in t.get_symbols():
            name = s.get_name()
            # Compiler bookkeeping, not bot.py's names: Python 3.14 records
            # __conditional_annotations__ as read by any `x: T = ...`.
            if name.startswith("__") and name.endswith("__"):
                continue
            if is_top:
                if s.is_assigned() or s.is_imported():
                    binds.add(name)
                elif s.is_referenced():
                    reads.add(name)
            elif s.is_global():
                if s.is_referenced():
                    reads.add(name)
                if s.is_declared_global() and s.is_assigned():
                    binds.add(name)
        for c in t.get_children():
            walk(c, False)
    walk(top, True)
    return reads, binds


def lift(*names, ns=None, stub=None, absent=(), **supplied) -> dict:
    """Lift named functions and module-level assignments out of bot.py and
    run them against fakes.

    Importing bot.py needs a Slack token and would open the live tasks.json, so
    most checks here read it as text. Compiling just the pieces under test into
    a namespace we control buys the real thing instead: the code actually runs,
    against a scratch store, and a mistake in it fails rather than merely
    reading differently.

    `names` are strings or iterables of them; `ns` is the namespace to fill
    (a new dict if not given), `supplied` more entries for it. The test's own
    entries always win. A free name the lifted code reads is resolved, in
    order, from: what the test supplied, the builtins, SHARED_HELPERS (lifted
    too), bot.py's own top-level imports (the real module), STAND_INS. Any
    left over raise one LiftError naming them all, here, rather than a
    NameError later on the one path that happens to read it -- unless `stub`
    is given, in which case every name the test did not supply, bot.py's
    imports and STAND_INS included, becomes stub(name) instead.

    `absent` declares names the lifted code reads only on paths this test
    does not drive (a router's other routes). They stay unbound -- reaching
    one is the NameError it always was -- but each is a decision written
    down, not a gap nobody saw. Anything supplied or lifted wins over a
    declaration (so the shared *_ROUTES tuples serve tests that drive one of
    those routes), and declaring a name the code never reads is an error,
    so the declarations cannot outlive the code.

    Returns the namespace; ns["__lifted__"] is the set of names compiled in.
    """
    import builtins
    want = set()
    for n in names:
        want.update([n] if isinstance(n, str) else n)
    ns = {} if ns is None else ns
    ns.update(supplied)
    tree = _bot_tree()
    defs = [n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Assign, ast.AnnAssign))]
    nodes = [n for n in defs if _binds(n) & want]
    missing = want - {b for n in nodes for b in _binds(n)}
    assert not missing, f"bot.py has no {', '.join(sorted(missing))}"
    helpers = {n.name: n for n in defs
               if isinstance(n, ast.FunctionDef) and n.name in SHARED_HELPERS}
    imports = {b: n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
               for b in _binds(n)}
    while True:
        reads, binds = _global_reads(nodes)
        more = [helpers[r] for r in sorted(reads - binds - set(ns)) if r in helpers]
        if not more:
            break
        nodes += more
    absent = set(absent)
    unread = absent - reads
    if unread:
        raise LiftError(f"lifting {', '.join(sorted(want))} out of bot.py: "
                        f"declared absent but never read: {', '.join(sorted(unread))}")
    unresolved = []
    for name in sorted(reads - binds - set(ns) - set(dir(builtins))):
        if name in absent:
            continue
        if stub is not None:
            ns[name] = stub(name)
        elif name in imports:
            node = imports[name]
            got = {}
            exec(compile(ast.Module(body=[node], type_ignores=[]), "bot.py", "exec"), got)
            ns[name] = got[name]
        elif name in STAND_INS:
            ns[name] = STAND_INS[name]()
        else:
            unresolved.append(name)
    if unresolved:
        raise LiftError(f"lifting {', '.join(sorted(want))} out of bot.py: nothing "
                        f"supplies {', '.join(unresolved)} -- pass it, declare it "
                        "absent= if this test never reaches it, or add it to "
                        "STAND_INS if every test wants the same one")
    nodes.sort(key=lambda n: n.lineno)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "bot.py", "exec"), ns)
    ns["__lifted__"] = {b for n in nodes for b in _binds(n)}
    return ns


# --- discovery ----------------------------------------------------------------
# Found, not listed. The suite used to be a tuple of names written out by hand,
# and a tuple is silent about what is missing from it: a test left off it never
# ran, reported nothing, and the closing "N passed, 0 failed" still read as
# healthy. That is the wrong kind of silence now that a green run is what
# authorises a branch to be rebased and landed with nobody watching.
#
# Each way a test could be written and still not run fails the whole run:
# no test files, a test file with no tests in it, a file that will not import,
# a def a __main__ guard hides, a name defined twice in one file (Python keeps
# the last and says nothing), and one name in two files (one of them would be
# indistinguishable from the other in every report).

def suite_files() -> list:
    """Every tests/test_*.py, in name order -- the order the suite runs them in."""
    return sorted(TESTS.glob("test_*.py"))


def defined_tests(tree) -> list:
    """The test_* functions a test file's source defines, in order.

    Top level, class bodies, and blocks at module level (if, try, with): every
    place a def is either bound on import or hidden from it. Not inside
    functions -- a helper named test_* inside another test is not a test.
    """
    found = []

    def scan(stmts):
        for n in stmts:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if n.name.startswith("test_"):
                    found.append(n.name)
            elif isinstance(n, ast.ClassDef):
                scan(n.body)
            else:
                for field in ("body", "orelse", "finalbody", "handlers"):
                    scan(getattr(n, field, None) or [])
    scan(tree.body)
    return found


def _is_main_guard(n) -> bool:
    return isinstance(n, ast.If) and "__main__" in ast.unparse(n.test)


def collect() -> tuple:
    """(every test function in the suite in run order, every reason not to trust that list)."""
    import importlib
    import inspect
    tests, problems, where = [], [], {}
    files = suite_files()
    if not files:
        problems.append(f"no test files: nothing matches {TESTS}/test_*.py")
    for path in files:
        try:
            tree = ast.parse(path.read_text(), str(path))
        except SyntaxError as exc:
            problems.append(f"{path.name} does not parse: {exc}")
            continue
        if any(_is_main_guard(n) for n in tree.body):
            # A test file is imported, never run as __main__: a guard has no job
            # here except hiding whatever sits below it when the file is run on
            # its own -- which is how a test once silently stopped running.
            problems.append(f"{path.name} has a __main__ guard; test files are run by tests/run.py")
        names = defined_tests(tree)
        if not names:
            problems.append(f"{path.name} defines no tests")
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            problems.append(f"{path.name} defines {', '.join(dupes)} more than once, "
                            "so only the last of each runs")
        try:
            mod = importlib.import_module(path.stem)
        except (Exception, SystemExit) as exc:     # a sys.exit(0) here would end the run green
            problems.append(f"{path.name} failed to import: {exc!r}")
            continue
        found = [fn for name, fn in vars(mod).items()
                 if name.startswith("test_") and isinstance(fn, types.FunctionType)
                 and fn.__module__ == mod.__name__]
        unbound = sorted(set(names) - {fn.__name__ for fn in found})
        if unbound:
            problems.append(f"{path.name} defines {', '.join(unbound)} where the runner "
                            "never sees it (under a guard, or not at top level)")
        for fn in found:
            if inspect.iscoroutinefunction(fn) or inspect.isgeneratorfunction(fn):
                # Calling one only makes a coroutine or generator: none of its
                # checks would run, and it would still count as run.
                problems.append(f"{path.name}: {fn.__name__} is async or a generator, "
                                "so calling it would run none of its checks")
                continue
            if fn.__name__ in where:
                problems.append(f"{fn.__name__} is defined in both {where[fn.__name__]} and {path.name}")
            else:
                where[fn.__name__] = path.name
                tests.append(fn)
    if not tests:
        # Otherwise the run prints "0 passed, 0 failed" and exits 0: a green run
        # that verified nothing, which is exactly what landing must not trust.
        problems.append("no tests were discovered")
    return tests, problems


def discover() -> list:
    """Every test in the suite, in run order: file by file, each in definition order."""
    return collect()[0]


# --- fixtures more than one test file uses ----------------------------------

def _repo_guard_impl():
    """Loaded from bot.py without importing it (bot.py needs Slack tokens)."""
    mod = types.ModuleType("guard")
    lift("_repo_lock", "repo_guard", "_repo_locks", "_repo_locks_guard", ns=mod.__dict__)
    return mod


def _run_ideation_impl():
    """Load run_ideation out of bot.py without importing it.

    Mirrors _file_task_impl: bot.py needs Slack tokens to import, so the
    function is compiled on its own against the real stores and the real
    scoping/tasks modules. Only `branches.survey` is stubbed, because it
    shells out to git; the decision under test is the shipped code.
    """
    import types
    import projects as P, scoping as S, tasks as T
    from tasks import TaskStore

    root = Path(tempfile.mkdtemp())
    ts, ps = TaskStore(root / "t.json"), P.ProjectStore(root / "p.json")
    mod = types.ModuleType("ideating")
    mod.__dict__.update(
        scoping=S, tasks=T, task_store=ts, project_store=ps, datetime=datetime, projects=P,
        time=time, ideation=__import__("ideation"),
        log=logging.getLogger("test"), CLAUDE_CWD=root, SILKWORM_BIN="silkworm",
        branches=types.SimpleNamespace(survey=lambda _t, **_: []))
    lift("run_ideation", ns=mod.__dict__)
    return mod.run_ideation, ts, ps


def _file_task_impl():
    """Load handle_file_task out of bot.py without importing it.

    bot.py needs Slack tokens to import, so the route is compiled on its own
    against the real roles/scoping/tasks/projects modules and a temporary task
    store.
    Only the two session lookups are stubbed -- the decision under test, and
    every rule it depends on, is the shipped code.
    """
    import types
    import projects as P, roles as R, scoping as S, tasks as T
    from tasks import TaskStore

    want = ("handle_file_task", "filed_by_restricted_role", "_filed_this_turn",
            "begin_turn", "unmerged_survey", "research_parent", "filer_role")
    ts = TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    # Naming a project writes: a record, and a home directory on disk. Recorded
    # rather than stubbed away, so a filing that gets refused can be shown to
    # have left nothing behind.
    made: list = []
    mod = types.ModuleType("filing")
    mod.__dict__.update(
        re=__import__("re"), roles=R, scoping=S, tasks=T, projects=P,
        dedup=__import__("dedup"), branches=__import__("branches"),
        research=__import__("research"), basered=__import__("basered"), time=time,
        ideation=__import__("ideation"),
        task_store=ts, operator_role=__import__("operator_role"),
        OPERATOR=types.SimpleNamespace(thread=lambda: "D5:5.0"),
        log=logging.getLogger("test"), CLAUDE_CWD=Path(tempfile.mkdtemp()),
        store=types.SimpleNamespace(get=lambda k: {}),
        project_store=types.SimpleNamespace(
            ensure=lambda n, **kw: (made.append(n), {"slug": n})[1],
            # What the real store answers when no project was renamed.
            slug_for=lambda n: __import__("projects").slugify(n),
            home=lambda n, create=False: made.append(n),
            scope_for=lambda n: None,
            # No project's base is failing (basered.named_fix asks).
            get=lambda n: None, all=lambda: []),
    )
    lift(want, ns=mod.__dict__, absent=('HEALTH_WARN_DAYS', 'requested_filing'))
    return mod.handle_file_task, mod._filed_this_turn, mod.begin_turn, ts, made


def _cli_file_task_impl():
    """Load `silkworm task` out of the CLI without a bot to call.

    bot_call is replaced with a recorder, so what the CLI would have sent is
    inspectable; everything up to it -- the argument parsing and the read of
    the environment the bot set up -- is the shipped code.
    """
    import types
    src = (BASE / "bin" / "silkworm").read_text()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "do_file_task")
    sent: list = []
    def bot_call(path, payload, timeout=3):
        sent.append((path, payload))
        return {"ok": True, "id": "tsk_0", "state": "proposed",
                "role": payload.get("role"), "project": payload.get("project"),
                "cwd": "/tmp", "remaining": 4}
    mod = types.ModuleType("cli")
    mod.__dict__.update(os=os, bot_call=bot_call)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<cli>", "exec"),
         mod.__dict__)
    return mod.do_file_task, sent


def _bot_func(name, **namespace):
    """One function out of bot.py, loaded without importing it.

    bot.py wants Slack tokens at import time, so the function is compiled on
    its own into a namespace the caller supplies. Every free name it reads and
    the caller did not supply becomes a MagicMock, so a big function can be
    driven by handing it only the few things the behaviour under test turns
    on -- and so a name the function stops using is not silently still stubbed.
    """
    from unittest.mock import MagicMock
    # Pure arithmetic over records, with nothing to stub: a mock here would
    # only turn a cost into an object the store cannot serialise.
    real = {r: __import__(r) for r in ("costs", "dedup")}
    mod = types.ModuleType(f"bot_{name}")
    mod.__dict__.update(namespace)
    # The shared helpers lifted alongside it read globals of their own
    # (task_base reads `branches` and `project_store`), which the caller has
    # as little reason to supply as the function's own -- stubbed too.
    # A mock archive answers "these are archived" (a mock is truthy) and the
    # lists then drop everything; nothing archived is the install's state.
    real["archived_projects"] = STAND_INS["archived_projects"]()
    lift(name, ns=mod.__dict__,
         stub=lambda free: real[free] if free in real else MagicMock(name=free))
    return mod.__dict__[name]


def _board_for(store, call, allowed_users=("U_ME",), **kw):
    """A Board on a configured channel id, as the click tests drive it."""
    import home
    return home.Board(state_path=Path(tempfile.mkdtemp()) / "board.json",
                      channel="GBOARD", bot_user="B_BOT", store=store, call=call,
                      allowed_users=allowed_users, **kw)


def _load_cli():
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("silkworm_cli_deploy",
                                                  str(BASE / "bin" / "silkworm"))
    spec = importlib.util.spec_from_loader("silkworm_cli_deploy", loader)
    cli = importlib.util.module_from_spec(spec)
    loader.exec_module(cli)
    # Never the checkout's own logs/: a suite run as a deploy's preflight
    # would otherwise find that deploy's lock held, and refuse itself.
    scratch = Path(tempfile.mkdtemp())
    cli.AFTER_TURN_LOCK = scratch / "deploy-after-turn.lock"
    cli.AFTER_TURN_LOG = scratch / "deploy-after-turn.log"
    return cli


def _board_fixture():
    """A TaskStore and ProjectStore in a temp dir, one task in every column."""
    import tasks as T
    import projects as P
    d = Path(tempfile.mkdtemp())
    ts = T.TaskStore(d / "tasks.json")
    ps = P.ProjectStore(d / "projects.json")
    ps.ensure("Alpha", test_cmd="./bin/test", auto_merge=True, scope={"cwd": str(d / "alpha")})
    ps.ensure("Beta")
    ps.ensure("Gone")
    ps.set_archived("gone", True)
    now = time.time()
    mk = lambda goal, **kw: ts.create(goal, driver="queue", **kw)
    ids = {}
    ids["proposed"] = mk("Propose a thing\n\nwith a long case about <b>it</b>",
                         project="alpha", state=T.PROPOSED, role="implementor")["id"]
    ids["queued"] = mk("Queue a thing", project="alpha", state=T.QUEUED,
                       role="implementor")["id"]
    ids["running"] = mk("Run a thing", project="alpha", state=T.RUNNING,
                        role="implementor", thread="C1:1.2")["id"]
    impl = mk("Under review", project="alpha", state=T.BLOCKED, role="implementor")
    rev = mk("Review: Under review", state=T.RUNNING, role="reviewer", parent=impl["id"])
    ts.update(impl["id"], blocked_on=[rev["id"]])
    ts.update(rev["id"], result={"cost": 1.5})
    ids["review"], ids["reviewer"] = impl["id"], rev["id"]
    ids["waiting"] = mk("Quota wait", project="alpha", state=T.BLOCKED,
                        role="implementor")["id"]
    ids["awaiting"] = mk("Awaits you", project="alpha", state=T.AWAITING_APPROVAL,
                         role="implementor")["id"]
    ts.update(ids["awaiting"], result={"cost": 2.0, "cost_runs": [[now, 2.0]],
              "review": {"ok": False, "summary": "two problems",
                         "findings": ["first finding", "second finding"],
                         "followups": ["a followup"]}})
    ids["failed"] = mk("Broke", project="alpha", state=T.FAILED, role="implementor")
    ts.update(ids["failed"]["id"], events=[{"at": now, "kind": "failed",
                                            "detail": "claude exited 1: NO WORKTREE"}])
    ids["failed"] = ids["failed"]["id"]
    ids["landed"] = mk("Landed it", project="alpha", state=T.DONE, role="implementor")["id"]
    ts.update(ids["landed"], result={"landed": "abcdef1234567890",
              "landing": {"landed": True, "stage": "done", "head": "abcdef1234567890",
                          "at": now - 3600}})
    ids["refused"] = mk("Refused", project="alpha", state=T.DONE, role="implementor")["id"]
    ts.update(ids["refused"], result={"landing": {"eligible": True, "landed": False,
              "stage": "rebase", "branch": "silkworm/" + ids["refused"],
              "detail": "CONFLICT in x.py", "at": now - 7200}})
    ids["stranded"] = mk("Stranded", project="alpha", state=T.DONE, role="implementor")["id"]
    ids["old"] = mk("Old work", project="alpha", state=T.DONE, role="implementor")["id"]
    ts.update(ids["old"], result={"landed": "0123456789",
              "landing": {"landed": True, "head": "0123456789", "at": now - 20 * 86400}})
    ids["cancelled"] = mk("Dropped", project="alpha", state=T.CANCELLED)["id"]
    ids["beta"] = mk("Beta work", project="beta", state=T.QUEUED, role="assistant")["id"]
    ids["unfiled"] = mk("Nobody's work", state=T.QUEUED, role="implementor")["id"]
    survey = [{"id": ids["stranded"], "project": "alpha", "branch": "silkworm/" + ids["stranded"],
               "commits": 3, "local": True, "remote": ""},
              {"id": ids["refused"], "project": "alpha", "branch": "silkworm/" + ids["refused"],
               "commits": None, "local": True, "remote": ""}]
    return ts, ps, ids, survey, d


def _edit_routes(tmp=None):
    """handle_tasks and handle_projects out of bot.py, over a real TaskStore
    and ProjectStore in a temp dir. Alpha is ready, beta is not, gone is
    archived."""
    import board as B, tasks as T, projects as P, costs, branches, worktrees
    import holding, roles, scoping
    d = Path(tmp or tempfile.mkdtemp())
    ts = T.TaskStore(d / "tasks.json")
    ps = P.ProjectStore(d / "projects.json")
    ps.ensure("Alpha", test_cmd="./bin/test", auto_merge=True, scope={"cwd": str(d / "alpha")})
    ps.ensure("Beta", scope={"cwd": str(d / "beta")})
    ps.ensure("Gone")
    ps.set_archived("gone", True)
    guards = []
    ns = {"task_store": ts, "project_store": ps, "board": B, "tasks": T, "costs": costs,
          "branches": branches, "worktrees": worktrees, "holding": holding, "roles": roles,
          "projects": P, "scoping": scoping, "workspaces": __import__("workspaces"),
          "unmerged_survey": lambda recs: [], "tell_thread": lambda *a: None,
          "stop_task": lambda tid: True, "install_git_guards": guards.append,
          "unregistered_repos": lambda: [], "GITHUB_OWNER": "",
          "CLAUDE_CWD": d / "scratch", "log": logging.getLogger("test"), "time": time}
    found = lift({"handle_tasks", "handle_projects", "edit_task", "held_reason",
                      "_task_title", "board_survey", "board_release", "_board_survey",
                      "_board_releases", "_with_costs", "_spend", "retire_project",
                      "archived_projects"}, ns=ns,
                 absent=TASK_ROUTES + PROJECT_ROUTES + STEP_ROUTES)["__lifted__"]
    return ns, ts, ps, found


def _task_state_for(st):
    """task_state as shipped, against a real store."""
    import tasks as T
    return _bot_func("task_state", task_store=st, tasks=T, log=logging.getLogger("t"))


def _release_fixture():
    """A project repo with a bare origin and fake targets: backend deploys by
    appending to a log outside the checkout and previews with an echo; ios is
    a versioned tag target after backend; web fails with exit 3."""
    import threading as _th
    import releases as R, projects as P, worktrees as W, board as B
    root = Path(tempfile.mkdtemp())
    origin, repo = root / "origin.git", root / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], capture_output=True)
    g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                                   "-c", "user.name=t", *a], capture_output=True, text=True)
    def commit(path, text, msg, push=True):
        p = repo / path; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)
        g("add", path); g("commit", "-q", "-m", msg)
        if push:
            g("push", "-q", "origin", "main")
    log_ = root / "deploy.log"
    commit(".silkworm/release.toml", f'''
[targets.backend]
paths = ["supabase/"]
ship = "command"
commands = ["echo deployed >> {log_}"]
preview = ["echo would-apply-0002"]

[targets.ios]
paths = ["ios/"]
ship = "tag"
version = {{ file = "ios/project.yml", key = "CFBundleShortVersionString" }}
after = ["backend"]
describe = "CI builds the tag for TestFlight"

[targets.web]
paths = ["web/"]
ship = "command"
commands = ["exit 3"]
''', "config", push=False)
    commit("ios/project.yml", 'CFBundleShortVersionString: "1.1.0"\n', "ios", push=False)
    commit("supabase/migrations/0001.sql", "x\n", "migration one", push=False)
    commit("supabase/migrations/0002.sql", "x\n", "migration two", push=False)
    commit("supabase/functions/f/index.ts", "x\n", "a function", push=False)
    commit("web/index.html", "x\n", "site")
    ps = P.ProjectStore(root / "projects.json")
    ps.ensure("Alpha", scope={"cwd": str(repo)})
    posts = []

    class Client:
        def chat_postMessage(self, channel, text, thread_ts=None, **kw):
            posts.append((channel, thread_ts, text))
            return {"ts": f"9.{len(posts)}"}
    ns = {"releases": R, "project_store": ps, "worktrees": W, "threading": _th,
          "log": logging.getLogger("test"), "time": time, "json": json,
          "repo_guard": lambda cwd: contextlib.nullcontext(),
          "ALLOWED_USERS": {"U_ME"}, "home_channel": lambda: "D1", "board": B,
          "app": types.SimpleNamespace(client=Client()),
          "board_release": lambda slug: B.release_status(ps.scope_for(slug).get("cwd")),
          "DRAIN": __import__("drain").Drain()}
    lift({"release_command", "release_plan", "run_release", "_releasing",
              "_releasing_guard", "release_steps", "_release_runs", "release_allowed",
              "_release_names", "release_offer", "start_release", "release_thread",
              "releasable_projects", "handle_releases", "_board_releases",
              "release_checkout", "RELEASE_STARTED"}, ns=ns, absent=('operator_refused',))
    return types.SimpleNamespace(ns=ns, repo=repo, origin=origin, root=root, g=g,
                                 commit=commit, log=log_, posts=posts, ps=ps)


def _wait_release(hr, slug, secs=60):
    end = time.time() + secs
    while time.time() < end:
        st = hr({"action": "status", "slug": slug})
        if st["run"] and st["run"]["finished"] and not st["running"]:
            return st
        time.sleep(0.05)
    return hr({"action": "status", "slug": slug})


_FINDINGS = ("*Answer:* the cache is never invalidated on deploy.\n"
             "*Confidence:* high — read the code path end to end.\n"
             "*Key evidence:* cache.py:40 sets no TTL.\n"
             "*Next steps:* 1. add a TTL (filed tsk_x)")


def _research_parked(st, project="alpha", thread="C1:1.0", findings=_FINDINGS):
    import tasks as T
    t = st.create("Why does the cache go stale after deploys?", role="researcher",
                  project=project, thread=thread, driver="queue")
    st.transition(t["id"], T.RUNNING, "claimed")
    st.update(t["id"], result={"text": findings})
    st.transition(t["id"], T.NEEDS_INPUT, T.FINDINGS_DETAIL)
    return t["id"]


def _viz_fixture():
    """A sessions.json and a projects dir of transcripts, in a temp dir."""
    d = Path(tempfile.mkdtemp())
    proj = d / "projects"
    (proj / "-a").mkdir(parents=True)
    (proj / "-b").mkdir()
    started = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 30))
    sessions = {"D1:1.1": {"session_id": "sid-one", "title": "one", "turns": 2,
                           "updated": time.time(), "cwd": "/tmp"},
                "D1:2.2": {"session_id": "sid-two", "title": "two", "turns": 1,
                           "updated": time.time() - 60, "summary": "x" * 3000,
                           "pending": {"started": started, "session_id": "sid-two",
                                       "prompt": "go"}}}
    (d / "sessions.json").write_text(json.dumps(sessions))
    return d, proj, sessions


def _tline(role, text, ts="2026-10-12T10:00:00Z", usage=None, model="claude-x"):
    msg = {"content": [{"type": "text", "text": text}]}
    if usage:
        msg.update(usage=usage, model=model)
    return json.dumps({"type": role, "timestamp": ts, "message": msg}) + "\n"


def _operator_bot(repo=None):
    """handle_tasks and the operator's guard as shipped, over a real TaskStore
    and a real operator.json, serving the real CLI on a free port."""
    import socket as _socket
    import board, costs, research, retry, slacklinks, drain
    import operator_role as O
    import tasks as T
    import branches as B
    from localserver import LocalServer
    tmp = Path(tempfile.mkdtemp())
    st = T.TaskStore(tmp / "t.json")
    state = O.State(tmp / "operator.json")
    landings, steps = [], []
    ns = lift(
        "handle_tasks", "approve_task", "decide_findings", "land_or_drop", "_with_costs",
        "_spend", "operator_refused", "task_contained", "task_live_paths", "task_base",
        tasks=T, task_store=st, research=research, holding=__import__("holding"),
        task_state=_task_state_for(st), merge=__import__("merge"),
        roles=__import__("roles"), board=board, costs=costs, slacklinks=slacklinks,
        defer=__import__("defer"), scoping=__import__("scoping"), projects=__import__("projects"),
        branches=B, operator_role=O, OPERATOR=state,
        project_store=types.SimpleNamespace(scope_for=lambda slug: {"branch": "main"},
                                            get=lambda slug: None),
        tell_thread=lambda *a: None, stop_task=lambda tid: False,
        take_step=lambda action, p: steps.append(p["id"]) or {"ok": True},
        start_landing=lambda tid, **k: landings.append(tid) or True,
        repo_guard=lambda *a, **k: contextlib.nullcontext(),
        discard=types.SimpleNamespace(drop=lambda repo, br: (True, "discarded/x", "")),
        record_landing=lambda *a, **k: None, os=os, absent=TASK_ROUTES)
    with _socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = LocalServer(port)
    server.route("/tasks", ns["handle_tasks"])
    server.start()
    return types.SimpleNamespace(port=port, store=st, state=state, ns=ns, tmp=tmp,
                                 landings=landings, steps=steps, server=server,
                                 statefile=tmp / "operator.json")


def _op_cli(bot, *argv, role="operator", thread="D1:1.0", grant="", task=""):
    env = {k: v for k, v in os.environ.items()
           if k not in ("SILKWORM_ROLE", "SILKWORM_THREAD", "SILKWORM_TASK_ID",
                        "SILKWORM_USER_GRANT")}
    env.update(SILKWORM_BOT_PORT=str(bot.port), SILKWORM_OPERATOR_STATE=str(bot.statefile))
    if role:
        env["SILKWORM_ROLE"] = role
    if thread:
        env["SILKWORM_THREAD"] = thread
    if grant:
        env["SILKWORM_USER_GRANT"] = grant
    if task:
        env["SILKWORM_TASK_ID"] = task
    return subprocess.run([sys.executable, str(BASE / "bin" / "silkworm"), *argv],
                          capture_output=True, text=True, env=env, timeout=60)


def _viz_routes(fn_name):
    """Every path do_GET / do_POST answers, read from the code itself, so a
    route added later is covered by the gate tests without editing them."""
    tree = ast.parse((BASE / "visualizer.py").read_text())
    handler = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
    fn = next(n for n in handler.body if isinstance(n, ast.FunctionDef) and n.name == fn_name)
    # `url.path in ICONS`: a module-level dict or tuple of paths counts too.
    tables = {t.id: n.value for n in tree.body if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name)}
    paths = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Compare) and ast.unparse(node.left) == "url.path":
            right = node.comparators[0]
            if isinstance(right, ast.Name):
                right = tables[right.id]
                right = ast.Tuple(elts=right.keys, ctx=ast.Load()) if isinstance(right, ast.Dict) else right
                for c in (right.elts if isinstance(right, (ast.Tuple, ast.List, ast.Set)) else []):
                    if isinstance(c, ast.Constant) and isinstance(c.value, str):
                        paths.add(c.value)
                continue
            for c in ast.walk(right):
                if isinstance(c, ast.Constant) and isinstance(c.value, str):
                    paths.add(c.value)
    return sorted(paths)


def _web_bot(tmp: Path):
    """handle_web_message, handle_thread_state, handle_prompt and handle_command
    as shipped, against scratch stores and a fake Slack client. Only Slack, the
    claude child and git-touching filing are faked; the routing between them is
    the real code."""
    import threading
    import projects as P
    import tasks as T
    import webthread
    from unittest.mock import MagicMock
    st = tmp_store()
    ts = T.TaskStore(tmp / "t.json")
    ps = P.ProjectStore(tmp / "projects.json")
    posted, prompts, running, pushed = [], [], {}, []
    seq = [1000]

    class Client:
        def chat_postMessage(self, channel, text, thread_ts=None, **kw):
            seq[0] += 1
            posted.append({"channel": channel, "text": text, "thread_ts": thread_ts})
            return {"ts": f"{seq[0]}.000100"}

        def chat_update(self, **kw):
            posted.append({"update": kw.get("text")})

        def chat_delete(self, channel, ts):
            posted.append({"deleted": f"{channel}:{ts}"})

        def __getattr__(self, name):           # reactions, chat_delete, files...
            return MagicMock(name=name)
    app = types.SimpleNamespace(client=Client())
    locks: dict = {}

    class Progress:                            # ProgressMessage, minus the throttle
        def __init__(self, client, channel, thread_ts):
            self.ts = f"p-{thread_ts}"
            posted.append({"channel": channel, "thread_ts": thread_ts, "text": "Starting Claude"})

        def update(self, text):
            posted.append({"update": text})

        finalize = update

    class Reactions:
        def __init__(self, client, channel, thread_ts, msg_ts):
            self.msg = msg_ts if msg_ts and msg_ts != thread_ts else None
        working = done = failed = cleared = lambda self: None

    def thread_lock(key):
        return locks.setdefault(key, threading.Lock())

    def run_turn(prompt, session_id=None, **kw):
        prompts.append({"prompt": prompt, "session_id": session_id, "cwd": str(kw.get("cwd"))})
        if "make it fail" in prompt:
            raise ClaudeError("claude exited 1: no such thing")
        if kw.get("on_init"):
            kw["on_init"]("sid-new" if session_id is None else session_id)
        return types.SimpleNamespace(session_id=session_id or "sid-new", cost_usd=0.01,
                                     text="the reply", duration_ms=5)
    cmd = lift("handle_command", "file_thread_under", store=st, project_store=ps,
                        RUNNING=running, HELP="help", Path=Path,
               absent=('CLAUDE_CWD', 'CLAUDE_MODEL', 'COST_NOTE', 'fmt_age',
                       'github_for_thread_project', 'install_git_guards', 'kill_terminal',
                       'learnings', 'new_project_from_thread', 'operator_command',
                       'release_command', 'set_ideate_lenses', 'shelve_from_slack'))
    shared = dict(store=st, task_store=ts, tasks=T, webthread=webthread, RUNNING=running,
                  RUNNING_TASKS={}, app=app, Path=Path, UPLOADS_ROOT=tmp / "uploads",
                  OUTBOX_ROOT=tmp / "outbox", CLAUDE_CWD=tmp / "scratch")
    prompt_fn = _bot_func(
        "handle_prompt", **shared,
        _dedup=lambda c, t: False, _already_handled=lambda k, t: False, ALLOWED_USERS=set(),
        MENTION_RE=_re.compile(r"<@\w+>"), handle_command=cmd["handle_command"],
        resolve_cwd=lambda client, ch: tmp / "scratch",
        file_by_directory=lambda key, cwd: (st.get(key) or {}).get("project") or "",
        repos=types.SimpleNamespace(identity=lambda c: ""), _thread_lock=thread_lock,
        repo_guard=lambda cwd, progress=None: contextlib.nullcontext(), contextlib=contextlib,
        ProgressMessage=Progress, ThreadReactions=Reactions, ACTIVE_SESSIONS={},
        begin_turn=lambda key: None, refresh_brief=lambda *a: None, refresh_summary=lambda *a: None,
        name_thread=lambda *a: None, RUNNER_HOLD=types.SimpleNamespace(open=lambda: None),
        claude_env=lambda *a, **k: {}, permission_args=lambda: [],
        recovery=recovery, task_state=_task_state_for(ts), run_turn=run_turn,
        upload_outbox=lambda *a: 0, shutil=shutil, chunk=lambda t: [t], to_mrkdwn=lambda t: t,
        fmt_duration=lambda ms: f"{ms}ms", COST_NOTE="", uuid=__import__("uuid"),
        threading=threading, release_turn=lambda *a: None, ClaudeStopped=ClaudeStopped,
        ClaudeTimeout=ClaudeTimeout, ClaudeError=ClaudeError, roles=__import__("roles"),
        OPERATOR=types.SimpleNamespace(thread=lambda: ""), render_block=lambda x: "",
        defer=__import__("defer"), scoping=__import__("scoping"), SILKWORM_BIN="silkworm",
        harvester=types.SimpleNamespace(find_transcript=lambda s: True),
        PUSH=types.SimpleNamespace(reply=lambda *a, **k: pushed.append({"args": a, **k})))
    hooks = {"prompt": prompt_fn}

    def handle_prompt(event, say, client):
        return hooks["prompt"](event, say, client)
    channel = {"home": "D9"}
    deliver = _bot_func("deliver_web_message", **shared, handle_prompt=handle_prompt,
                        handle_command=cmd["handle_command"], project_store=ps,
                        home_channel=lambda: channel["home"], slacklinks=__import__("slacklinks"),
                        WEB_COMMAND_WAIT_S=5.0, THREAD_KEY_RE=_re.compile(r"[A-Z0-9]+:[0-9.]+"),
                        threading=threading, time=time)
    sends = webthread.Sends()
    web = _bot_func("handle_web_message", webthread=webthread, WEB_SENDS=sends,
                    WEB_REPEAT_WAIT_S=5.0, deliver_web_message=deliver)
    state = _bot_func("handle_thread_state", store=st, task_store=ts, webthread=webthread,
                      RUNNING=running, THREAD_KEY_RE=_re.compile(r"[A-Z0-9]+:[0-9.]+"))
    return types.SimpleNamespace(web=web, state=state, store=st, tasks=ts, projects=ps,
                                 posted=posted, prompts=prompts, running=running,
                                 lock=thread_lock, hooks=hooks, cmd=cmd, client=app.client,
                                 channel=channel, sends=sends, pushed=pushed)


def _wait(cond, secs=5.0):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def _ua_subscription(host="web.push.apple.com"):
    """A browser's side of a subscription: its private key (to read what was
    sent) and the PushSubscription.toJSON() the page would hand over."""
    import push
    from cryptography.hazmat.primitives.asymmetric import ec
    priv = ec.generate_private_key(ec.SECP256R1())
    auth = os.urandom(16)
    sub = {"endpoint": f"https://{host}/{push.b64u(os.urandom(12))}",
           "keys": {"p256dh": push.b64u(push._public_bytes(priv)), "auth": push.b64u(auth)}}
    return priv, sub


def _ua_decrypt(body: bytes, ua_priv, auth_b64: str) -> bytes:
    """RFC 8291 from the receiving end: what a browser does with a push."""
    import hashlib as _h
    import hmac as _hm
    import push
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    def mac(k, d):
        return _hm.new(k, d, _h.sha256).digest()
    salt, idlen = body[:16], body[20]
    as_pub, sealed = body[21:21 + idlen], body[21 + idlen:]
    shared = ua_priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), as_pub))
    ua_pub = push._public_bytes(ua_priv)
    ikm = mac(mac(push.unb64u(auth_b64), shared),
              b"WebPush: info\x00" + ua_pub + as_pub + b"\x01")
    prk = mac(salt, ikm)
    plain = AESGCM(mac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]).decrypt(
        mac(prk, b"Content-Encoding: nonce\x00\x01")[:12], sealed, None)
    assert plain.endswith(b"\x02"), "not a last record"
    return plain[:-1]


def _probes():
    """health_probes, imported without its registrations reaching the global
    registry: the CI tests above run H.run_all on a 'cadence' record, and must
    not find live App Store and Supabase probes registered beside "ci"."""
    import health as H
    saved = dict(H.REGISTRY)
    import health_probes as P
    H.REGISTRY.clear()
    H.REGISTRY.update(saved)
    return P


def _types(tables=None, functions=(), views=None):
    """The Management API's /types/typescript answer, shaped as `supabase gen
    types` writes it: entries at six spaces, a table's Row columns at ten."""
    def table(name, cols):
        rows = "".join(f"          {c}: string\n" for c in cols)
        ins = "".join(f"          {c}?: string\n" for c in cols)
        return (f"      {name}: {{\n        Row: {{\n{rows}        }}\n"
                f"        Insert: {{\n{ins}        }}\n        Relationships: []\n      }}\n")
    tabs = "".join(table(n, c) for n, c in (tables or {}).items()) \
        or "      [_ in never]: never\n"
    vws = "".join(table(n, c) for n, c in (views or {}).items()) \
        or "      [_ in never]: never\n"
    fns = "".join(f"      {f}: {{ Args: never; Returns: undefined }}\n" for f in functions) \
        or "      [_ in never]: never\n"
    return json.dumps({"types": (
        "export type Database = {\n  __InternalSupabase: {\n    PostgrestVersion: \"14\"\n  }\n"
        f"  public: {{\n    Tables: {{\n{tabs}    }}\n    Views: {{\n{vws}    }}\n"
        f"    Functions: {{\n{fns}    }}\n    Enums: {{\n      issue_state: \"open\" | \"closed\"\n"
        "    }\n  }\n}\n")})


HOME = "D1:1.0"


def _said_bot():
    """_operator_bot with its home thread claimed, a turn of yours open in it,
    and the discard path watched (a dismiss must never reach it)."""
    bot = _operator_bot()
    # Its own, not left to user_grant's lifting: a mutation that stops asking
    # for grants must fail these checks, not crash them.
    import operator_role as O
    bot.ns.setdefault("OPERATOR_GRANTS", O.Grants())
    bot.state.set_thread(HOME)
    bot.dropped = []
    bot.ns["discard"] = types.SimpleNamespace(
        drop=lambda repo, br: bot.dropped.append(br) or (True, "discarded/x", ""))
    bot.turn = "tsk_turn_mine"
    return bot


def _as_user(bot, grant, *argv, turn=None):
    return _op_cli(bot, *argv, "--by", "operator", thread=HOME, grant=grant,
                   task=turn or bot.turn)


def _as_sweep(bot, *argv):
    """The same command from a turn you did not start: the daily sweep, a
    wake-up -- the operator's role and thread, and no grant."""
    return _op_cli(bot, *argv, "--by", "operator", thread=HOME, task="tsk_sweep")


def _proposal(st, title, **kw):
    import tasks as T
    return st.create(title, state=T.PROPOSED, role="implementor", project="alpha", **kw)["id"]


def _ops_world():
    """A trader checkout, fake programs on PATH (each logging its argv), and a
    bot serving /ops -- handle_ops and user_grant as shipped -- on a free port.
    """
    import socket as _socket
    import plistlib
    import git_guard
    import ops as OPS
    import operator_role as O
    from localserver import LocalServer
    tmp = Path(tempfile.mkdtemp())
    calls = tmp / "calls.log"
    calls.write_text("")
    fakebin = tmp / "fakebin"
    fakebin.mkdir()

    def fake(path, body="exit 0"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/sh\necho "{path.name} $*" >> "{calls}"\n{body}\n')
        path.chmod(0o755)

    fake(fakebin / "tailscale", 'case "$*" in *--json*) echo "{}";; esac\nexit 0')
    fake(fakebin / "launchctl",
         'case "$1" in bootout) exit "${FAKE_BOOTOUT:-0}";; '
         'bootstrap) exit "${FAKE_BOOTSTRAP:-0}";; '
         'print) echo "environment = { FAKE_TOKEN => ${FAKE_PRINT_LEAK:-x} }"; '
         'exit "${FAKE_PRINT:-113}";; esac\nexit 0')
    fake(fakebin / "mkdir", 'exec /bin/mkdir "$@"')
    fake(fakebin / "mv", 'exec /bin/mv "$@"')
    fake(fakebin / "test", 'exec /bin/test "$@"')
    fake(fakebin / "plutil", 'exec /usr/bin/plutil "$@"')
    fake(fakebin / "cp", 'exec /bin/cp "$@"')
    fake(fakebin / "runtests", 'echo "12 passed, 0 failed"\nexit "${FAKE_TESTS:-0}"')
    trader = tmp / "trader"
    (trader / "ops").mkdir(parents=True)
    (trader / "scripts").mkdir()
    fake(trader / ".venv" / "bin" / "python3", 'echo "plan: 3 symbols revised"')
    for label, file in (("com.traderjoe.pooldata", "com.traderjoe.pooldata"),
                        ("com.traderjoe.other", "com.traderjoe.mislabel")):
        with open(trader / "ops" / f"{file}.plist", "wb") as f:
            plistlib.dump({"Label": label, "ProgramArguments": ["/bin/true"]}, f)
    outside = tmp / "elsewhere.plist"
    with open(outside, "wb") as f:
        plistlib.dump({"Label": "com.traderjoe.link"}, f)
    (trader / "ops" / "com.traderjoe.link.plist").symlink_to(outside)
    # Committed, as trader's are: the reinstall installs only what is.
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "ops"]):
        subprocess.run(["git", "-C", str(trader), *args], check=True,
                       capture_output=True, env=git_guard.strip(dict(os.environ)))
    alpha = tmp / "alpha"
    alpha.mkdir()
    agents = tmp / "LaunchAgents"
    agents.mkdir()
    # Installed jobs, and trader's gitignored data/ and .env -- made after the
    # commit, as they are not trader's tracked files. The .env's secrets are
    # built here in pieces, so no secret-shaped literal sits in this file.
    for label in ("com.traderjoe.shadowoptions", "com.traderjoe.stocktwits"):
        with open(agents / f"{label}.plist", "wb") as f:
            plistlib.dump({"Label": label, "ProgramArguments": ["/bin/true"]}, f)
    (agents / "com.silkworm.bot.plist").write_bytes(b"<plist/>")
    (trader / "data").mkdir()
    secrets = ["sk" + "-live-" + os.urandom(12).hex(), "ib" + "pw" + os.urandom(8).hex()]
    env_text = (f"# trader settings\nBROKER_KEY={secrets[0]}\n"
                f"export IB_PASSWORD='{secrets[1]}'\nALERT_CHANNEL=C123\n")
    (trader / ".env").write_text(env_text)
    (trader / ".env").chmod(0o600)
    projects_ = {"trader": {"slug": "trader", "scope": {"cwd": str(trader)}},
                 "alpha": {"slug": "alpha", "scope": {"cwd": str(alpha)},
                           "test_cmd": f"{fakebin}/runtests --quick"},
                 "beta": {"slug": "beta", "scope": {"cwd": str(alpha)}, "test_cmd": ""}}
    state = O.State(tmp / "operator.json")
    state.set_thread("D1:1.0")
    told = []
    ns = lift("handle_ops", "ops_context", "user_grant", "record_ops_plan", "decline_ops_plan",
                       ops=OPS, OPS_LOG=tmp / "ops.jsonl", sys=sys, os=os, Path=Path,
                       tell_thread=lambda key, text: told.append((key, text)),
                       _plan_runs=set(), _plan_runs_guard=__import__("threading").Lock(),
                       git_guard=git_guard, operator_role=O, OPERATOR=state,
                       OPERATOR_GRANTS=O.Grants(), SILKWORM_BIN=str(BASE / "bin" / "silkworm"),
                       project_store=types.SimpleNamespace(get=lambda s: projects_.get(s)),
              absent=('guarded_plan_entry',))
    real_context = ns["ops_context"]
    ctx = OPS.Context(project=lambda s: projects_.get(s), python=sys.executable,
                      silkworm_bin=str(BASE / "bin" / "silkworm"), uid=501,
                      launch_agents=agents)
    ns["ops_context"] = lambda: ctx
    with _socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = LocalServer(port)
    server.route("/ops", ns["handle_ops"])
    server.start()
    w = types.SimpleNamespace(tmp=tmp, calls=calls, fakebin=fakebin, trader=trader,
                              agents=agents, ns=ns, ctx=ctx, port=port, state=state,
                              log=tmp / "ops.jsonl", real_context=real_context,
                              grants=ns["OPERATOR_GRANTS"], statefile=tmp / "operator.json",
                              projects=projects_, secrets=secrets, env_text=env_text,
                              told=told)
    return w


@contextlib.contextmanager
def _fake_path(w, **env):
    """The fakes first on PATH -- for the bot in this process and the CLI it
    serves -- and any FAKE_* switches, put back after."""
    saved = {k: os.environ.get(k) for k in ("PATH", *env)}
    os.environ["PATH"] = f"{w.fakebin}:{saved['PATH']}"
    os.environ.update(env)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _calls(w):
    return [line for line in w.calls.read_text().splitlines() if line.strip()]


def _ops_cli(w, *argv, role="operator", thread="D1:1.0", grant="", task=""):
    env = {k: v for k, v in os.environ.items()
           if k not in ("SILKWORM_ROLE", "SILKWORM_THREAD", "SILKWORM_TASK_ID",
                        "SILKWORM_USER_GRANT")}
    env.update(SILKWORM_BOT_PORT=str(w.port), SILKWORM_OPS_LOG=str(w.log))
    for k, v in (("SILKWORM_ROLE", role), ("SILKWORM_THREAD", thread),
                 ("SILKWORM_USER_GRANT", grant), ("SILKWORM_TASK_ID", task)):
        if v:
            env[k] = v
    return subprocess.run([sys.executable, str(BASE / "bin" / "silkworm"), "ops", *argv],
                          capture_output=True, text=True, env=env, timeout=120)


def _ops_as_user(w, grant, *argv, turn="tsk_turn_mine"):
    return _ops_cli(w, "run", *argv, "--by", "operator", grant=grant, task=turn)


def _plan_turn(w, text, turn="tsk_turn_mine", ts=None):
    """Your message's turn, with the plan it answers attached as handle_prompt
    attaches it (bot.pending_plan -> State.answer_plan)."""
    ts = ts or f"{time.time() + 1:.6f}"
    return w.grants.open("D1:1.0", ts, "U_ME", text, turn, plan=w.state.answer_plan(ts, text))


def _plan_record(w, *argv, task="tsk_sweep"):
    return _ops_cli(w, "plan", *argv, "--by", "operator", task=task)


def _plan_run(w, grant, n, *argv, notes="user: yes", turn="tsk_turn_mine"):
    return _ops_cli(w, "run", *argv, "--plan-entry", str(n), "--by", "operator",
                    "--notes", notes, grant=grant, task=turn)


def _env_value(w, key):
    for line in (w.trader / ".env").read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1]
    return None


def _requested_world():
    """handle_file_task as shipped, with the operator's home thread, its live
    grants, and a project already holding a full board of proposals."""
    import operator_role as O
    import projects as P, roles as R, scoping as S, tasks as T
    from tasks import TaskStore
    tmp = Path(tempfile.mkdtemp())
    ts = TaskStore(tmp / "t.json")
    for i in range(S.max_open_proposals()):
        ts.create(f"an idea nobody asked for, number {i}", project="alpha",
                  state=T.PROPOSED, driver="queue")
    state = O.State(tmp / "operator.json")
    state.set_thread("D5:5.0")
    grants = O.Grants()
    ns = lift(
        "handle_file_task", "filed_by_restricted_role", "begin_turn", "research_parent",
        "requested_filing",
        _filed_this_turn={}, re=__import__("re"), roles=R, scoping=S, tasks=T, projects=P,
        dedup=__import__("dedup"), branches=__import__("branches"),
        research=__import__("research"), basered=__import__("basered"),
        health=__import__("health"), HEALTH=None, HEALTH_WARN_DAYS=3,
        task_store=ts, operator_role=O, OPERATOR=state, OPERATOR_GRANTS=grants,
        CLAUDE_CWD=tmp, store=types.SimpleNamespace(get=lambda k: {}),
        project_store=types.SimpleNamespace(
            ensure=lambda n, **kw: {"slug": n}, slug_for=lambda n: P.slugify(n),
            home=lambda n, create=False: None, scope_for=lambda n: {"cwd": str(tmp)},
            # No project's base is failing (basered.named_fix asks).
            get=lambda n: None, all=lambda: []))
    return types.SimpleNamespace(file=ns["handle_file_task"], ts=ts, state=state,
                                 grants=grants, ns=ns)


PHONE, MINI = "100.64.0.2", "100.64.0.1"


class _VizProbe:
    def __init__(self, job=4242, opener=4242):
        self.job, self.opener, self.asked = job, opener, []

    def job_pid(self, label):
        self.asked.append(("job", label))
        return self.job

    def connector(self, client, server):
        self.asked.append(("conn", client, server))
        return {"pid": self.opener, "uid": 501, "name": "python3.12"} if self.opener else {}


TRADER_REAL_FIXTURE = BASE / "tests" / "fixtures" / "trader_status_real.json"


# A small DOM for the dashboard's motion and touch: elements with classes,
# attributes, a selector engine (compound, descendant, attribute equality),
# events dispatched capture-first, Element.animate recorded, and timers the
# driver advances. argv[3] == "reduced" answers prefers-reduced-motion.
MOTION_HARNESS = r"""const fs = require("fs");
const SRC = fs.readFileSync(process.argv[2], "utf8");
const REDUCED = process.argv[3] === "reduced";
// --- a small DOM: elements, selectors, events, animations, timers ---
const all = [];
let timers = [], now = 0;
globalThis.setTimeout = (f, ms) => { timers.push({f, at: now + (ms || 0)}); return timers.length; };
globalThis.clearTimeout = id => { if (timers[id - 1]) timers[id - 1].f = null; };
globalThis.setInterval = () => 0;
globalThis.requestAnimationFrame = f => setTimeout(() => f(now), 16);
function runTimers(until) {
  for (let guard = 0; guard < 500; guard++) {
    const due = timers.map((t, i) => [t, i]).filter(([t]) => t.f && (until === undefined || t.at <= until))
      .sort((a, b) => a[0].at - b[0].at)[0];
    if (!due) break;
    const [t] = due; now = Math.max(now, t.at); const f = t.f; t.f = null; f();
  }
  if (until !== undefined) now = Math.max(now, until);
}
const anims = [];
let animTotal = 0;
function matchesOne(el, simple) {
  const re = /([#.]?[\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g;
  let m, any = false;
  while ((m = re.exec(simple))) {
    any = true;
    if (m[1]) {
      const t = m[1];
      if (t[0] === "#") { if (el.id !== t.slice(1)) return false; }
      else if (t[0] === ".") { if (!el.cls.has(t.slice(1))) return false; }
      else if ((el.tagName || "").toLowerCase() !== t.toLowerCase()) return false;
    } else {
      const v = el.getAttribute(m[2]);
      if (v === null || v === undefined) return false;
      if (m[3] !== undefined && v !== m[3]) return false;
    }
  }
  return any;
}
function matches(el, sel) {
  if (typeof sel !== "string" || !sel) return false;
  return sel.split(",").some(part => {
    const chain = part.trim().split(/\s+/).filter(x => x !== ">");
    if (!matchesOne(el, chain[chain.length - 1])) return false;
    let a = el.parentNode;
    for (let i = chain.length - 2; i >= 0; i--) {
      while (a && !matchesOne(a, chain[i])) a = a.parentNode;
      if (!a) return false;
      a = a.parentNode;
    }
    return true;
  });
}
function E(tag, props) {
  const cls = new Set(), attrs = {};
  const e = {tagName: tag || "DIV", id: "", cls, attrs, children: [], parentNode: null, style: {},
    dataset: {}, value: "", _text: "", _inner: "", scrollTop: 0, offsetWidth: 300,
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); this.children.forEach(c => c.parentNode = null); this.children = []; },
    listeners: {}, rect: {top: 0, left: 0, width: 300, height: 40},
    classList: {add: (...c) => c.forEach(x => cls.add(x)), remove: (...c) => c.forEach(x => cls.delete(x)),
      contains: c => cls.has(c), toggle: (c, on) => { if (on === undefined ? !cls.has(c) : on) cls.add(c); else cls.delete(c); return cls.has(c); }},
    get className() { return [...cls].join(" "); },
    set className(v) { cls.clear(); String(v).split(/\s+/).filter(Boolean).forEach(x => cls.add(x)); },
    get innerHTML() { return this._inner; },
    set innerHTML(v) { this._inner = v; this.children.forEach(c => c.parentNode = null); this.children = [];
                       if (this.onHTML) this.onHTML(v); },
    getAttribute(k) { if (k === "id") return this.id || null; if (k === "class") return this.className;
                      if (k.startsWith("data-")) { const d = k.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase());
                        return this.dataset[d] !== undefined ? this.dataset[d] : null; }
                      return k in attrs ? attrs[k] : null; },
    setAttribute(k, v) { if (k === "id") this.id = v; else attrs[k] = String(v); },
    removeAttribute(k) { if (k === "id") this.id = ""; else delete attrs[k]; },
    appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c);
                     if (!all.includes(c)) all.push(c); return c; },
    removeChild(c) { this.children = this.children.filter(x => x !== c); c.parentNode = null; return c; },
    remove() { if (this.parentNode) this.parentNode.removeChild(this); },
    contains(o) { for (let a = o; a; a = a.parentNode) if (a === this) return true; return false; },
    closest(sel) { for (let a = this; a && a.tagName; a = a.parentNode) if (matches(a, sel)) return a; return null; },
    matches(sel) { return matches(this, sel); },
    querySelectorAll(sel) { return all.filter(x => x !== this && this.contains(x) && matches(x, sel)); },
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
    addEventListener(k, f) { (this.listeners[k] = this.listeners[k] || []).push(f); },
    getBoundingClientRect() { return Object.assign({}, this.rect, {bottom: this.rect.top + this.rect.height}); },
    animate(frames, opts) { const a = {el: this, frames, opts, onfinish: null, playState: "running",
                                       cancel() { this.playState = "idle"; }, finish() { this.playState = "finished"; if (this.onfinish) this.onfinish(); }};
                            anims.push(a); animTotal++; return a; },
    cloneNode() { const c = E(this.tagName); c.id = this.id; c.className = this.className; c._inner = this._inner;
                  Object.assign(c.style, this.style); c.cloned = true; return c; },
    setPointerCapture() {}, focus() {}, scrollIntoView() {}, insertAdjacentHTML(_, h) { this._inner += h; }};
  Object.assign(e, props || {});
  return e;
}
const docListeners = {};
const body = E("BODY");
const html = E("HTML"); html.dataset.theme = "dark";
const byIdMap = {};
const byId = id => {
  if (!byIdMap[id]) { const e = E("DIV"); e.id = id; byIdMap[id] = e; body.appendChild(e); }
  return byIdMap[id];
};
const metas = {theme: E("META")}; metas.theme.setAttribute("content", "#2C0E2E");
globalThis.document = {
  getElementById: byId, body, documentElement: html, hidden: false, createElement: t => E(t.toUpperCase()),
  addEventListener(k, f, o) { (docListeners[k] = docListeners[k] || []).push({f, capture: !!(o === true || (o && o.capture))}); },
  querySelectorAll(sel) { return all.filter(x => x.parentNode && matches(x, sel)); },
  querySelector(sel) { if (sel === 'meta[name="theme-color"]') return metas.theme; return this.querySelectorAll(sel)[0] || null; },
};
all.push(body);
body.appendChild(E("HEADER"));        // the page's document click closes the header's ⋯ menu
function fire(type, ev) {
  ev = Object.assign({type, stopped: false, prevented: false,
    stopPropagation() { this.stopped = true; }, preventDefault() { this.prevented = true; }}, ev);
  for (const l of (docListeners[type] || []).filter(l => l.capture)) l.f(ev);
  if (!ev.stopped) for (const l of (docListeners[type] || []).filter(l => !l.capture)) l.f(ev);
  return ev;
}
const store = {};
globalThis.localStorage = {getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }};
globalThis.window = {addEventListener() {}, location: {search: ""}};
globalThis.history = {state: null, replaceState() {}, pushState() {}, back() {}};
globalThis.location = {search: "", href: "http://x/"};
let PHONE = true, LIGHT = false;
globalThis.matchMedia = q => ({matches: q === "(max-width: 700px)" ? PHONE
  : q === "(prefers-reduced-motion: reduce)" ? REDUCED : q === "(prefers-color-scheme: light)" ? LIGHT : false});
Object.defineProperty(globalThis, "navigator", {value: {vibrate() { return true; }}, configurable: true});
globalThis.getComputedStyle = () => ({});
globalThis.CSS = {escape: s => s};
const calls = [];
const BASE = url => url.startsWith("/api/sessions") ? {bot_online: true, sessions: [], slack: {}}
  : url.startsWith("/api/stats") ? {total_cost: 0, cache_rate: null, threads: 0, days: [], models: []}
  : {ok: true, tasks: [], counts: {}, projects: []};
let answer = BASE, gate = null;
globalThis.fetch = async (url, opts) => {
  const b = opts && opts.body ? JSON.parse(opts.body) : null;
  calls.push({url, body: b});
  if (gate) await gate;
  const out = answer(url, b);
  if (out instanceof Error) throw out;
  return {status: 200, headers: {get: () => null}, json: async () => out, text: async () => ""};
};
// the markup the script reads at load: the session list's skeleton and the panels
const list = byId("list");
const listskel = E("DIV"); listskel.id = "listskel"; byIdMap.listskel = listskel; list.appendChild(listskel);
for (const id of ["taskmodal", "boardmodal", "learnmodal", "tradermodal", "fmodal"]) {
  const m = byId(id); m.style.display = "none";
  const box = E("DIV"); box.className = id === "fmodal" ? "fbox" : "box"; m.appendChild(box);
  if (id !== "fmodal") { const h = E("DIV"); h.className = "hint"; box.appendChild(h); }
}
byId("bdetail").style.display = "none";
byId("tproj").value = "";
const toastEl = byId("toast");
toastEl.onHTML = v => {
  if (!/class="undo"/.test(v)) return;
  const m = E("SPAN"); m.className = "tmsg"; toastEl.appendChild(m);
  const u = E("BUTTON"); u.className = "undo"; u.textContent = "Undo"; toastEl.appendChild(u);
};
Object.defineProperty(toastEl, "text", {get() {
  return this.children.length ? this.children.map(c => c.textContent).join(" ") : this.textContent; }});

(0, eval)(SRC + `
;globalThis.__m = {pendingGone, deferTask, toast, toggleTasks, closeModal, openModal, closeDetail, renderList,
  loadList, hideThread, taskAction, cycleTheme, theme, applyTheme, unstick, drawKeyed, skel, skelIfEmpty,
  setHTML, canAnimate, renderColumns, boardCard, taskButtons, swapIn, sendAllDeferred, renderTasks,
  swipeAttr, get lastSessions() { return lastSessions; }};`);
const M = globalThis.__m, out = {};
out.skelAtLoad = list.children.includes(listskel);
const flushAsync = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r)); };

(async () => {
  // --- swipe and undo ---
  const col = byId("bcolx");
  function card(id, title) {
    const c = E("DIV"); c.className = "bcard"; c.dataset.id = id; c.dataset.swipe = "1";
    const t = E("DIV"); t.className = "tt"; t.textContent = title; c.appendChild(t);
    col.appendChild(c); return {c, t};
  }
  const drag = (target, dx, dy, kind) => {
    fire("pointerdown", {pointerType: kind || "touch", target, pointerId: 7, clientX: 200, clientY: 100});
    fire("pointermove", {pointerId: 7, clientX: 200 + dx * 0.3, clientY: 100 + dy * 0.3});
    fire("pointermove", {pointerId: 7, clientX: 200 + dx, clientY: 100 + dy});
  };
  const up = () => fire("pointerup", {pointerId: 7});
  const posts = () => calls.filter(c => c.url === "/api/tasks" && c.body && (c.body.action === "accept" || c.body.action === "dismiss"));
  const finishAll = () => { for (const a of anims.splice(0)) if (a.playState === "running") a.finish(); };

  const a = card("tsk_a", "Tidy the board");
  drag(a.t, -200, 4);
  out.during = {transform: a.c.style.transform, dismissShown: a.c.classList.contains("to-dismiss"),
                armed: a.c.classList.contains("armed"), swiping: a.c.classList.contains("swiping")};
  up();
  out.flewOut = anims.some(x => x.el === a.c && JSON.stringify(x.frames).includes("-110%"));
  finishAll();
  out.afterSwipe = {pending: M.pendingGone.has("tsk_a") && M.pendingGone.get("tsk_a").action, posted: posts().length,
                    toast: toastEl.text, undoable: toastEl.classList.contains("undoable"),
                    display: toastEl.style.display};
  // The click a swipe ends with does not also open the card.
  const clk = fire("click", {target: a.c});
  out.clickSwallowed = clk.stopped && clk.prevented;
  // Undo: nothing is sent, the card is drawn again.
  toastEl.children.find(c => c.className === "undo").onclick();
  runTimers(now + 6000);
  out.afterUndo = {pending: M.pendingGone.has("tsk_a"), posted: posts().length};

  // A swipe right past the threshold accepts -- once the undo window is up.
  const b = card("tsk_b", "Ship it");
  drag(b.t, 220, -3); up(); finishAll();
  out.acceptPending = M.pendingGone.get("tsk_b") && M.pendingGone.get("tsk_b").action;
  runTimers(now + 4000);
  out.beforeWindow = posts().length;
  runTimers(now + 2000); await flushAsync();
  out.afterWindow = {posts: posts().map(c => c.body), pending: M.pendingGone.has("tsk_b")};

  // The server refuses: the card comes back and the toast says why.
  answer = (url, body) => (body && body.action === "dismiss") ? {ok: false, error: "already running"} : BASE(url);
  const c = card("tsk_c", "Refused one");
  drag(c.t, -250, 0); up(); finishAll();
  runTimers(now + 5100); await flushAsync();
  out.refused = {toast: toastEl.text, bad: toastEl.classList.contains("bad"), pending: M.pendingGone.has("tsk_c")};
  answer = BASE;

  // Put away before Undo ran out: it is sent at once.
  const d = card("tsk_d", "Away");
  drag(d.t, -250, 0); up(); finishAll();
  const n0 = posts().length;
  const lateUndo = toastEl.children.find(c => c.className === "undo");
  out.swipedCardRemoved = !d.c.parentNode;
  document.hidden = true; fire("visibilitychange", {}); document.hidden = false;
  await flushAsync();
  out.sentOnHide = posts().length - n0 === 1 && posts().slice(-1)[0].body.id === "tsk_d";
  out.undoToastGoesWithTheSend = toastEl.classList.contains("leaving") || toastEl.style.display === "none";
  runTimers(now + 300);
  lateUndo.onclick();                       // the toast's Undo, tapped once it was already sent
  runTimers(now + 10);
  out.lateUndo = {toast: toastEl.text, bad: toastEl.classList.contains("bad"), posts: posts().length - n0,
                  pending: M.pendingGone.has("tsk_d")};

  // Undo tapped while the action is on its way to the server: too late too.
  const g = card("tsk_g2", "In flight");
  drag(g.t, -250, 0); up(); finishAll();
  const flightUndo = toastEl.children.find(c => c.className === "undo");
  let open_; gate = new Promise(ok => { open_ = ok; });
  document.hidden = true; fire("visibilitychange", {}); document.hidden = false;
  await flushAsync();
  flightUndo.onclick(); runTimers(now + 10);
  out.inFlightUndo = {toast: toastEl.text, pendingWhileSending: M.pendingGone.has("tsk_g2")};
  gate = null; open_(); await flushAsync();
  out.inFlightSent = posts().filter(c => c.body.id === "tsk_g2").length;

  // A second swipe while the first's Undo is up joins it: neither is sent
  // while that Undo shows, and "Undo 2" brings both back.
  const s1 = card("tsk_s1", "First"), s2 = card("tsk_s2", "Second");
  const s0 = posts().length;
  drag(s1.t, -250, 0); up(); finishAll();
  runTimers(now + 3000);
  drag(s2.t, 250, 0); up(); finishAll();
  out.twoToast = {text: toastEl.text, undo: toastEl.children.find(c => c.className === "undo").textContent};
  runTimers(now + 3000); await flushAsync();       // past the first swipe's own 5s
  out.twoBeforeWindow = {posts: posts().length - s0, pending: M.pendingGone.has("tsk_s1") && M.pendingGone.has("tsk_s2")};
  toastEl.children.find(c => c.className === "undo").onclick();
  runTimers(now + 6000); await flushAsync();
  out.twoUndone = {posts: posts().length - s0, pending: M.pendingGone.has("tsk_s1") || M.pendingGone.has("tsk_s2")};
  // Left alone, both go once the shared window is up, the toast with them.
  const s3 = card("tsk_s3", "Third"), s4 = card("tsk_s4", "Fourth");
  drag(s3.t, -250, 0); up(); finishAll();
  runTimers(now + 2000);
  drag(s4.t, -250, 0); up(); finishAll();
  runTimers(now + 5100); await flushAsync();
  out.twoSent = {ids: posts().slice(s0).map(c => c.body.id), pending: M.pendingGone.size,
                 toastGone: toastEl.classList.contains("leaving") || toastEl.style.display === "none"};

  // Another toast over a swipe's Undo (a thread hidden, a refusal): the swipe
  // is not sent while it is covered, and its Undo comes back after.
  const c1 = card("tsk_c1", "Covered"), c0 = posts().length;
  drag(c1.t, -250, 0); up(); finishAll();
  runTimers(now + 1000);
  M.toast("Hidden: Some thread", 5000, {undo: () => {}});
  out.covered = {text: toastEl.text};
  runTimers(now + 4500); await flushAsync();       // past the swipe's own 5s, the other toast still up
  out.coveredBeforeBack = {posts: posts().length - c0, pending: M.pendingGone.has("tsk_c1")};
  runTimers(now + 600); runTimers(now + 300); await flushAsync();
  out.coveredBack = {text: toastEl.text, display: toastEl.style.display, posts: posts().length - c0};
  runTimers(now + 4000); await flushAsync();
  out.coveredStillHeld = posts().length - c0;        // a whole window from when it came back
  runTimers(now + 1100); await flushAsync();
  out.coveredSent = {ids: posts().slice(c0).map(c => c.body.id), pending: M.pendingGone.has("tsk_c1")};
  // The covering toast's own Undo tapped: the swipe's Undo is back at once.
  const c2 = card("tsk_c2", "Covered again"), c3 = posts().length;
  drag(c2.t, 250, 0); up(); finishAll();
  M.toast("Hidden: Another", 5000, {undo: () => {}});
  M.toast("Saved");                                  // and a third over that: still waiting
  runTimers(now + 2600); runTimers(now + 300);
  out.chainBack = toastEl.text;
  M.toast("Hidden: Third", 5000, {undo: () => {}});
  toastEl.children.find(c => c.className === "undo").onclick();
  out.tappedBack = {text: toastEl.text, posts: posts().length - c3};
  toastEl.children.find(c => c.className === "undo").onclick();
  runTimers(now + 6000); await flushAsync();
  out.coveredUndone = {posts: posts().length - c3, pending: M.pendingGone.has("tsk_c2")};

  // Not far enough: it springs back, nothing is pending.
  const e = card("tsk_e", "Almost");
  drag(e.t, -60, 0); up(); finishAll();
  out.short = {pending: M.pendingGone.has("tsk_e"), transform: e.c.style.transform,
               swiping: e.c.classList.contains("swiping")};
  // Mostly vertical: a scroll, not a swipe.
  const f = card("tsk_f", "Scroll");
  drag(f.t, -40, 160); up(); finishAll();
  out.vertical = {pending: M.pendingGone.has("tsk_f"), swiping: f.c.classList.contains("swiping")};
  // A mouse is not a finger: the buttons are its way.
  drag(f.t, -250, 0, "mouse"); up(); finishAll();
  out.mouse = M.pendingGone.has("tsk_f");
  // A swipe that starts on a button is that button's.
  const btn = E("BUTTON"); f.c.appendChild(btn);
  drag(btn, -250, 0); up(); finishAll();
  out.onButton = M.pendingGone.has("tsk_f");

  // While Undo runs, the card is left out of every redraw (polls included).
  M.deferTask("tsk_g", "dismiss", "Dismissed: g");
  const cols = M.renderColumns({columns: {backlog: [{id: "tsk_g", title: "g", state: "proposed", role: "implementor", created: 0},
                                                    {id: "tsk_h", title: "h", state: "proposed", role: "implementor", created: 0}]}});
  out.redraw = {hidesPending: !cols.includes('data-id="tsk_g"'), keepsOther: cols.includes('data-id="tsk_h"'),
                count: /Backlog<b>1<\/b>/.test(cols), swipeMarked: cols.includes('data-id="tsk_h" data-swipe="1"')};
  toastEl.children.find(c => c.className === "undo").onclick();
  out.reviewerNotSwiped = !M.boardCard({id: "r1", title: "r", state: "proposed", role: "reviewer", created: 0}).includes("data-swipe");
  out.queuedNotSwiped = !M.boardCard({id: "q1", title: "q", state: "queued", role: "implementor", created: 0}).includes("data-swipe");
  // The task list's rows go by the same rule as the board's cards.
  out.sameRule = ["implementor", "reviewer", "researcher"].every(role => ["proposed", "queued"].every(state =>
    M.boardCard({id: "x1", title: "x", state, role, created: 0}).includes("data-swipe")
      === (M.swipeAttr({state, role}) !== "")))
    && M.swipeAttr({state: "proposed", role: "reviewer"}) === "" && M.swipeAttr({state: "proposed", role: "implementor"}) !== "";

  // --- sheets ---
  const tm = byId("taskmodal");
  M.toggleTasks();
  out.sheetOpen = {display: tm.style.display, skeleton: byId("tlist").innerHTML.includes("skel skel-row")};
  await flushAsync();
  out.skeletonReplaced = !byId("tlist").innerHTML.includes("skel");
  const before = body.children.length;
  M.toggleTasks();
  const ghost = body.children.slice(before).find(g => g.classList.contains("sheet-ghost"));
  out.sheetClose = {display: tm.style.display, ghost: !!ghost, ghostCloned: !!(ghost && ghost.cloned),
                    ghostInert: !!(ghost && ghost.getAttribute("inert") !== null && ghost.getAttribute("aria-hidden") === "true")};
  runTimers(now + 600);
  out.ghostGone = !body.children.some(g => g.classList.contains("sheet-ghost"));
  M.openModal("<p>form</p>", null);
  out.formOpen = byId("fmodal").style.display;
  const b2 = body.children.length;
  M.closeModal();
  out.formClose = {display: byId("fmodal").style.display,
                   ghost: body.children.slice(b2).some(g => g.classList.contains("sheet-ghost"))};
  byId("bdetail").style.display = "block";
  const b3 = body.children.length;
  M.closeDetail();
  out.detailClose = {display: byId("bdetail").style.display,
                     ghost: body.children.slice(b3).some(g => g.classList.contains("ghost-detail"))};
  runTimers(now + 600);

  // --- toast ---
  M.toast("Saved");
  out.toast = {text: toastEl.text, show: toastEl.classList.contains("show"), display: toastEl.style.display};
  runTimers(now + 2600);
  out.toastLeaving = REDUCED ? toastEl.style.display : toastEl.classList.contains("leaving");
  runTimers(now + 300);
  out.toastGone = toastEl.style.display;

  // --- the session list: skeleton first, then cards that fade in; one hidden goes at once ---
  out.listSkel = list.children.includes(listskel);
  const sessions = [1, 2, 3].map(i => ({key: `D1:${i}.0`, title: `Thread ${i}`, kind: "thread", turns: 2,
                                       updated: 1e9, cost: 0, summary: "", hidden: false}));
  answer = url => url.startsWith("/api/sessions") ? {bot_online: true, sessions, slack: {}}
                : url.startsWith("/api/hide") ? {ok: true} : BASE(url);
  anims.length = 0;
  await M.loadList(); await flushAsync();
  out.listDrawn = {skelGone: !list.children.includes(listskel), cards: list.children.length,
                   fadedIn: anims.filter(x => JSON.stringify(x.frames).includes('"opacity":0')).length};
  const hidePromise = M.hideThread("D1:2.0", true);
  out.hiddenAtOnce = list.children.length;   // before the server has answered
  await hidePromise; await flushAsync();
  out.hideToast = {text: toastEl.text, undoable: toastEl.classList.contains("undoable"),
                   sent: calls.filter(c => c.url === "/api/hide").map(c => c.body)};
  toastEl.children.find(c => c.className === "undo").onclick();
  await flushAsync();
  out.hideUndone = calls.filter(c => c.url === "/api/hide").slice(-1)[0].body;
  // The server cannot be reached: the card comes back and it says so.
  answer = url => url.startsWith("/api/hide") ? new Error("offline")
                : url.startsWith("/api/sessions") ? {bot_online: true, sessions: sessions.map(s => Object.assign({}, s, {hidden: false})), slack: {}}
                : BASE(url);
  await M.hideThread("D1:3.0", true); await flushAsync();
  out.hideFailed = {hidden: (M.lastSessions.find(s => s.key === "D1:3.0") || {}).hidden, toast: toastEl.text,
                    bad: toastEl.classList.contains("bad"), cards: list.children.length};
  answer = BASE;

  // --- an action marks its row busy until the server answers ---
  const row = E("DIV"); row.className = "task"; row.dataset.task = "tsk_z"; body.appendChild(row);
  let busyDuring = null;
  answer = (url, b) => { if (b && b.action === "retry") busyDuring = row.classList.contains("busy"); return BASE(url); };
  await M.taskAction("tsk_z", "retry");
  out.busy = {during: busyDuring, after: row.classList.contains("busy")};
  answer = BASE;

  // --- the task panel's rows are keyed, so a redraw animates them too ---
  const tl = byId("tlist");
  byId("taskmodal").style.display = "flex";
  let rowsPlan = [];
  tl.onHTML = () => { for (const [k, top] of rowsPlan) { const x = E("DIV"); x.className = "task"; x.dataset.task = k;
                      x.rect = {top, left: 0, width: 300, height: 60}; tl.appendChild(x); } };
  const T = (id, state) => ({id, title: id, state, source: "ui", created: 0, attempts: 1, role: "implementor"});
  answer = (url, b) => b && b.action === "attention" ? {ok: true, counts: {}, tasks: [T("t1", "proposed"), T("t2", "failed")]} : BASE(url);
  rowsPlan = [["t1", 0], ["t2", 60]];
  await M.renderTasks(); await flushAsync();
  anims.length = 0;
  answer = (url, b) => b && b.action === "attention" ? {ok: true, counts: {}, tasks: [T("t2", "failed")]} : BASE(url);
  rowsPlan = [["t2", 0]];
  await M.renderTasks(); await flushAsync();
  out.taskRowsFlip = anims.filter(x => x.el.dataset && x.el.dataset.task === "t2").map(x => JSON.stringify(x.frames[0]));
  byId("taskmodal").style.display = "none"; tl.onHTML = null; answer = BASE;

  // --- flip: a keyed redraw animates only what moved, only when it changed ---
  const box = byId("flipbox");
  let plan = [];
  box.onHTML = () => { for (const [k, top] of plan) { const x = E("DIV"); x.className = "bcard"; x.dataset.id = k;
                       x.rect = {top, left: 0, width: 300, height: 40}; box.appendChild(x); } };
  plan = [["a", 0], ["b", 50], ["c", 100]];
  M.drawKeyed(box, "<one>", ".bcard[data-id]", "data-id");
  anims.length = 0;
  plan = [["b", 0], ["a", 50], ["c", 100], ["d", 150]];
  M.drawKeyed(box, "<two>", ".bcard[data-id]", "data-id");
  const moved = k => anims.filter(x => x.el.dataset.id === k).map(x => JSON.stringify(x.frames[0]));
  out.flip = {a: moved("a"), b: moved("b"), c: moved("c"), d: moved("d")};
  anims.length = 0;
  out.flipSame = M.drawKeyed(box, "<two>", ".bcard[data-id]", "data-id") === false && anims.length === 0;

  // --- unstick: an animation held at its first frame is finished; a spinner is not ---
  const held = E("DIV");
  const stuck = {playState: "running", currentTime: 0, effect: {getComputedTiming: () => ({iterations: 1})},
                 finish() { this.playState = "finished"; }};
  const spinner = {playState: "running", currentTime: 0, effect: {getComputedTiming: () => ({iterations: Infinity})},
                   finish() { this.playState = "finished"; }};
  const moving_ = {playState: "running", currentTime: 90, effect: {getComputedTiming: () => ({iterations: 1})},
                   finish() { this.playState = "finished"; }};
  held.getAnimations = () => [stuck, spinner, moving_];
  M.unstick(held, 500);
  runTimers(now + 600);
  out.unstick = [stuck.playState, spinner.playState, moving_.playState];

  // --- theme ---
  out.theme0 = M.theme();
  const seen = [];
  for (let i = 0; i < 3; i++) { M.cycleTheme(); seen.push([html.dataset.theme, store["sw-theme"], metas.theme.getAttribute("content")]); }
  out.themes = seen;
  out.themeLabel = byId("themelbl").textContent;
  M.applyTheme("nonsense");
  out.themeBad = html.dataset.theme;
  M.applyTheme("light", false);
  out.themeLink = [html.dataset.theme, store["sw-theme"]];
  M.applyTheme("dark", false);

  // --- pull to refresh, on the phone's session list ---
  const ptr = byId("ptr");
  const pull = (dy, scrollTop) => {
    list.scrollTop = scrollTop || 0;
    fire("touchstart", {target: list, touches: [{clientY: 100}]});
    fire("touchmove", {touches: [{clientY: 100 + dy}]});
    fire("touchend", {});
  };
  const loads = () => calls.filter(c => c.url.startsWith("/api/sessions")).length;
  let n = loads(); pull(220); await flushAsync();
  out.pullFar = loads() - n;
  runTimers(now + 400);
  out.ringAfterRefresh = [ptr.style.opacity, ptr.style.transform];
  n = loads();
  list.scrollTop = 0;
  fire("touchstart", {target: list, touches: [{clientY: 100}]});
  fire("touchmove", {touches: [{clientY: 104}]});
  out.ringBarelyPulled = ptr.style.opacity;
  fire("touchmove", {touches: [{clientY: 160}]});
  fire("touchend", {});
  await flushAsync();
  out.pullShort = loads() - n;
  out.ringAfterShort = ptr.style.opacity;
  runTimers(now + 400);
  out.ringRested = [ptr.style.opacity, ptr.style.transform];
  // A scroller inside the list scrolled down: its drag, not a pull.
  const inner = E("DIV"); inner.scrollTop = 30; list.appendChild(inner);
  const leaf = E("DIV"); inner.appendChild(leaf);
  n = loads(); list.scrollTop = 0;
  fire("touchstart", {target: leaf, touches: [{clientY: 100}]});
  fire("touchmove", {touches: [{clientY: 330}]}); fire("touchend", {});
  await flushAsync();
  out.pullNested = loads() - n;
  n = loads(); pull(220, 40); await flushAsync();
  out.pullScrolled = loads() - n;
  PHONE = false; n = loads(); pull(220); await flushAsync(); PHONE = true;
  out.pullLaptop = loads() - n;

  // --- skeleton helpers ---
  const empty = E("DIV"), full = E("DIV"); full._inner = "<p>x</p>";
  M.skelIfEmpty(empty, "card", 3); M.skelIfEmpty(full, "card", 3);
  out.skel = {empty: (empty.innerHTML.match(/skel skel-card/g) || []).length, full: full.innerHTML,
              hidden: empty.innerHTML.includes('aria-hidden="true"')};
  out.canAnimate = M.canAnimate(E("DIV"));
  out.animTotal = animTotal;
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


#: What `from harness import *` gives a test file: everything above, underscored
#: names too, so a test reads the same names it did when the suite was one file.
__all__ = [n for n in dir() if not n.startswith("__")]

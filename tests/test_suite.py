"""The suite itself: every module imported, every test run.

Run by tests/run.py with every other tests/test_*.py; see tests/harness.py."""

from harness import *  # noqa: F401,F403


# --- a module used but never imported ----------------------------------------
# `retry` was used in fail_or_retry and never imported, so every transient
# failure raised NameError instead of being requeued. Its own tests passed:
# they exercised retry.py directly and checked bot.py as *text*, which cannot
# tell a used name from an imported one.

def test_modules_are_imported():
    print("\nevery module a file uses is imported")
    siblings = {p.stem for p in BASE.glob("*.py")}
    for path in sorted(BASE.glob("*.py")):
        tree = ast.parse(path.read_text())
        imported, bound = set(), set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imported |= {(a.asname or a.name).split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                imported |= {a.asname or a.name for a in n.names}
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(n.name)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                bound.add(n.id)
            elif isinstance(n, ast.arg):
                bound.add(n.arg)
        used = {n.value.id for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
        missing = sorted((used & siblings) - imported - bound - {path.stem})
        check(f"{path.name} imports every sibling module it uses", not missing,
              f"uses but never imports: {', '.join(missing)}")

    # And every test this file defines is actually run. The list below __main__
    # used to be hand-maintained, so a test could be written, pass on its own, and never
    # run in the suite -- which is how test_every_open_state_has_a_button first
    # went in: 970 checks passed without it. A test nothing calls is worse than
    # no test, because it reads as cover.
    # Every tests/test_*.py, read as source: top level, class bodies and
    # module-level blocks, not every nested def -- a helper named test_* inside
    # another test is not a test. async too -- a plain FunctionDef check would
    # let `async def test_x` past, and the runner cannot await one anyway.
    names = [n for path in suite_files() for n in defined_tests(ast.parse(path.read_text()))]
    # Names the runner actually discovers, not every name defined: the list
    # below __main__ was replaced by discover(), and a def that lands below the
    # guard is still not bound when it looks.
    listed = {fn.__name__ for fn in discover()}
    check("this file defines tests at all", len(names) > 40, f"found {len(names)}")
    # Python takes the last of two same-named defs and says nothing. That is a
    # silent regression with a green suite -- the shadowed test simply stops
    # running -- and a set of names cannot see it.
    dupes = sorted({n for n in names if names.count(n) > 1})
    check("no test is defined twice", not dupes,
          f"shadowed, so only the last one runs: {', '.join(dupes)}")
    unrun = sorted(set(names) - listed)
    check("every test this file defines is one the runner discovers", not unrun,
          f"defined but never run: {', '.join(unrun)}")


# --- no test is defined and then never run ------------------------------------
# For as long as the suite ran a hand-written list, a test added to this file
# but left off that list never ran, said nothing about it, and left the total
# looking healthy. Discovery removed the list; this covers the last way a test
# defined here could still fail to run.

def test_every_test_runs():
    print("\nevery test defined here is actually run")
    # Each file's tests, read back from its source. A name in two files counts
    # twice here, which is as much a shadowing as two defs in one file.
    defined = [n for path in suite_files() for n in defined_tests(ast.parse(path.read_text()))]
    # Without this, a parse that found nothing would leave the check below
    # passing over an empty list.
    check("this file's tests can be read back from its source", len(defined) > 1,
          "found no test definitions to compare against")
    dupes = sorted({n for n in defined if defined.count(n) > 1})
    check("no test is defined twice, shadowing the earlier one", not dupes,
          f"defined more than once: {', '.join(dupes)}")
    missing = sorted(set(defined) - {fn.__name__ for fn in discover()})
    check("every test_* defined in this file is discovered and run", not missing,
          f"never runs: {', '.join(missing)} — defined below the __main__ guard?")

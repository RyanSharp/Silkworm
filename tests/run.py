"""Run Silkworm's regression suite: every test_* in tests/test_*.py.

    python3 tests/run.py                 # the whole suite (`silkworm test`)
    python3 tests/run.py test_health     # only these files' tests

Discovery and its guards are in harness.collect(). Anything that makes the
list of tests untrustworthy -- no tests, an empty test file, a def a __main__
guard hides, a name defined twice in a file or once in each of two -- fails the
run before a single test runs, even when only some files were asked for.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402


def main(argv) -> int:
    tests, problems = harness.collect()
    if problems:
        for p in problems:
            print(f"  ✘ {p}")
        print(f"\nthe suite cannot be trusted to run every test: {len(problems)} problem(s)")
        return 1
    if argv:
        wanted = {a[:-3] if a.endswith(".py") else a for a in (Path(a).name for a in argv)}
        known = {p.stem for p in harness.suite_files()}
        unknown = sorted(wanted - known)
        if unknown:
            print(f"  ✘ no such test file: {', '.join(unknown)}")
            return 1
        tests = [t for t in tests if t.__module__ in wanted]
    for t in tests:
        try:
            t()
        except (Exception, SystemExit) as exc:     # a sys.exit(0) must not end the run green
            harness.FAILED.append(f"{t.__name__} raised {exc!r}")
            print(f"  ✘ {t.__name__} raised {exc!r}")
    print(f"\n{len(harness.PASSED)} passed, {len(harness.FAILED)} failed")
    for f in harness.FAILED:
        print(f"  FAILED: {f}")
    return 1 if harness.FAILED else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

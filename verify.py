"""Run a project's own tests against a task's work, and believe the exit code.

Deliberately not an agent. Running a test command and reading its status needs
no model, and a model in the loop could report a suite as green that was not --
which is the one thing this exists to make impossible. The reviewer stays a
judgement; this is evidence.

It exists because the reviewer cannot run anything. It is read-only by design,
so it verifies code by reading it, and one real review said so outright: "could
not run ./bin/silkworm test here, so the '567 passed' claim is unverified."
Merging on that would be merging on an opinion.

A project with no declared command is never verified and never auto-merged.
Silence is not confidence.
"""

import contextlib
import logging
import re
import shlex
import subprocess
import threading

log = logging.getLogger("silkworm.verify")

#: A suite that has not finished in this long is not going to.
TIMEOUT_S = 1800

#: Enough of the tail to see what failed, in a Slack message.
OUTPUT_CHARS = 2000


#: What a suite says when it never got as far as running a test: the simulator
#: or the app under test could not be brought up. Measured, not imagined -- a
#: Cadence landing was refused and rolled back on exactly the first of these,
#: while a second Cadence run held the one simulator its suite pins.
LAUNCH_FAILURES = re.compile(
    r"failed preflight checks"
    r"|Unable to boot"
    r"|CoreSimulator"
    r"|Failed to install or launch the test runner",
    re.IGNORECASE)

#: ...unless a test actually ran and failed. CoreSimulator in particular is
#: printed beside ordinary failures too, and retrying one of those could land
#: a flaky test on its second roll. Both frameworks: XCTest ("Test Case '-[T t]'
#: failed", "XCTAssertEqual failed"), and Swift Testing, which xcodebuild
#: reports as "Test case 'T/t()' failed" and `swift test` as "Test t() recorded
#: an issue" / "Test t() failed after 0.1 seconds". The run summary ("Test run
#: with 3 tests failed after") is left out: it says nothing about whether any
#: test got as far as running.
TEST_FAILURES = re.compile(
    r"Test Case '[^']*' failed"
    r"|XCTAssert\w* failed"
    r"|\bTest (?!run with )\S.*? (?:recorded an issue|failed after)",
    re.IGNORECASE)

#: The lines that say *what* failed, wherever they sit in the output. The
#: tail kept below is not enough: the output is stdout then stderr, so its end
#: is whatever the suite logged -- a landing refused at tests-after-merge kept
#: exactly that, a deliberate traceback and some fixture chatter, and never
#: named the failing test. A passing check can quote a failure ("✔ ... is not a
#: launch failure: '✘ Test foo() ...'"), so a line marked passing never counts,
#: and Silkworm's markers count only at the start of a line. Silkworm's own
#: runner ("✘ name", "FAILED: name", "N passed, M failed"), pytest ("FAILED
#: t.py::x", "== 1 failed, 2 passed =="), xcodebuild, and TEST_FAILURES.
#: FAILED is upper-case only: "Failed to install or launch the test runner" is
#: a launch failure, not a test.
FAILURE_LINE = re.compile(
    r"^\s*(?:✘|✗|(?-i:FAILED)\b|=+ .*\bfailed\b"
    r"|\d+ passed, \d+ failed"
    r"|\*\* TEST FAILED \*\*"
    r"|Executed \d+ tests?, with [1-9]\d* failures?)"
    r"|" + TEST_FAILURES.pattern,
    re.IGNORECASE)

#: How a line says its check passed, whatever it goes on to quote.
PASSED_MARKS = ("✔", "✓")

#: Enough of them to name the failures without becoming the whole output.
FAILURE_LINES = 20


def failures(output: str) -> str:
    """The failing checks and the pass/fail summary in `output`, in order, or
    "" when it names none (a launch failure, a timeout, a crash)."""
    found = [line.rstrip() for line in (output or "").splitlines()
             if not line.lstrip().startswith(PASSED_MARKS)
             and FAILURE_LINE.search(line)]
    if len(found) > FAILURE_LINES:
        found = found[:FAILURE_LINES] + [f"  … and {len(found) - FAILURE_LINES} more"]
    return "\n".join(found)


#: One lock per project, around every run of that project's suite.
_project_locks: dict[str, threading.Lock] = {}
_project_locks_guard = threading.Lock()


@contextlib.contextmanager
def project_lock(project):
    """Hold `project`'s suite for one run, so two runs of it queue, not overlap.

    A suite is not always self-contained. Cadence's pins one named simulator,
    and two runs at once fight over it: one of them fails "preflight checks"
    for a reason that has nothing to do with the code. With a review lane and
    landings started by hand, two runs for one project at once became normal --
    an implementor being verified while another task lands.

    Different projects share nothing and stay concurrent. No project, no lock.

    Always the innermost lock. It is held only around the subprocess, and
    nothing is acquired while it is held, so a landing that takes it inside
    `repo_guard` cannot deadlock against a run waiting on it: that run never
    goes on to want the checkout.
    """
    if not project:
        yield
        return
    with _project_locks_guard:
        lock = _project_locks.setdefault(str(project), threading.Lock())
    if not lock.acquire(blocking=False):
        # Visible, because a run waiting thirty minutes on another is
        # otherwise indistinguishable from a wedged one.
        log.info("tests for %s waiting on another run of the same suite", project)
        lock.acquire()
        log.info("tests for %s no longer waiting", project)
    try:
        yield
    finally:
        lock.release()


def launch_failure(result: dict) -> bool:
    """True when a failed run never got as far as testing anything."""
    return (bool(result.get("ran")) and not result.get("ok")
            and bool(result.get("launch_failure")))


def run(command: str, cwd, timeout: int = TIMEOUT_S, project=None,
        retry_launch: bool = False) -> dict:
    """Run `command` in `cwd`. Returns {ran, ok, code, output, launch_failure}.

    `ran` is False when there was nothing to run -- which is different from
    failing, and callers must not treat the two the same.

    `project` serialises this run with every other run for the same project;
    see project_lock. `retry_launch` runs it once more, still holding the lock,
    when it failed to *launch* rather than failed a test -- for a landing,
    where a refusal rolls work back.
    """
    with project_lock(project):
        result = _run(command, cwd, timeout)
        if retry_launch and launch_failure(result):
            log.warning("tests for %s failed to launch, not on a test; "
                        "running them once more", project or cwd)
            result = _run(command, cwd, timeout)
    return result


def _run(command: str, cwd, timeout: int) -> dict:
    command = (command or "").strip()
    if not command:
        return {"ran": False, "ok": False, "code": None,
                "output": "no test command is configured for this project"}
    try:
        proc = subprocess.run(shlex.split(command), cwd=str(cwd), timeout=timeout,
                              capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return {"ran": True, "ok": False, "code": None,
                "output": f"tests did not finish within {timeout}s"}
    except (OSError, ValueError) as e:
        # A missing binary or an unparseable command line is a configuration
        # problem, not a failing test -- say which.
        return {"ran": False, "ok": False, "code": None,
                "output": f"could not run {command!r}: {e}"}
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    # Read off the whole output, not the tail kept below: the launch error can
    # sit above a long summary.
    return {"ran": True, "ok": proc.returncode == 0, "code": proc.returncode,
            "output": out[-OUTPUT_CHARS:],
            "failures": failures(out) if proc.returncode != 0 else "",
            "launch_failure": (proc.returncode != 0
                               and bool(LAUNCH_FAILURES.search(out))
                               and not TEST_FAILURES.search(out))}


def summary(result: dict) -> str:
    """One line for a Slack reply."""
    if not result.get("ran"):
        return f":grey_question: _Not verified — {result.get('output', '')}._"
    if result.get("ok"):
        return ":test_tube: _Tests pass._"
    return f":x: _Tests fail (exit {result.get('code')})._"


def rework_note(result: dict) -> str:
    """What to hand back to the implementor, with the actual failure in it."""
    return ("Your change does not pass this project's tests. Fix it rather than "
            "adjusting the tests to suit it, unless a test is genuinely wrong -- "
            "and say so explicitly if you conclude that.\n\n"
            f"Exit status {result.get('code')}."
            + (f" What failed:\n\n```\n{result['failures']}\n```\n\n"
               if result.get("failures") else " ")
            + "Output tail:\n\n"
            f"```\n{result.get('output', '')}\n```")

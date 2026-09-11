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

import logging
import shlex
import subprocess

log = logging.getLogger("silkworm.verify")

#: A suite that has not finished in this long is not going to.
TIMEOUT_S = 1800

#: Enough of the tail to see what failed, in a Slack message.
OUTPUT_CHARS = 2000


def run(command: str, cwd, timeout: int = TIMEOUT_S) -> dict:
    """Run `command` in `cwd`. Returns {ran, ok, code, output}.

    `ran` is False when there was nothing to run -- which is different from
    failing, and callers must not treat the two the same.
    """
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
    return {"ran": True, "ok": proc.returncode == 0, "code": proc.returncode,
            "output": out[-OUTPUT_CHARS:]}


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
            f"Exit status {result.get('code')}. Output tail:\n\n"
            f"```\n{result.get('output', '')}\n```")

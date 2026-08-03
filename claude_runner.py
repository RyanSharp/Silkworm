"""Headless Claude Code invocation with stream-json parsing.

Runs `claude -p --output-format stream-json --verbose` as a subprocess and
surfaces events as they arrive: session init (session_id), tool activity
(for progress updates), and the final result (text + cost + duration).
"""

import json
import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("silkworm.runner")


class ClaudeError(Exception):
    pass


class ClaudeStopped(ClaudeError):
    """Turn was killed by an explicit !stop."""


class ClaudeTimeout(ClaudeError):
    """Turn exceeded its time budget and was killed.

    Distinct from a plain ClaudeError because it says nothing about whether the
    session is still valid — callers must not discard the session over one.
    """


@dataclass
class TurnResult:
    text: str = ""
    session_id: str | None = None
    cost_usd: float = 0.0
    duration_ms: int = 0
    num_turns: int = 0


@dataclass
class RunHandle:
    """Lets another thread stop a running turn."""
    proc: subprocess.Popen
    stopped: bool = field(default=False)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def stop(self, grace: float = 5.0) -> None:
        """Stop the turn, escalating to SIGKILL if SIGTERM is ignored.

        A child that ignores SIGTERM stays alive with the output pipe open, so
        the reader blocks forever and the thread's lock is never released --
        every later message in that thread queues behind it indefinitely. The
        escalation is what guarantees the turn actually ends.
        """
        with self._lock:
            self.stopped = True
        if not self._signal(signal.SIGTERM):
            return
        deadline = time.time() + grace
        while time.time() < deadline:
            if self._exited():
                return
            time.sleep(0.2)
        log.warning("pid %s ignored SIGTERM after %.0fs — sending SIGKILL",
                    self.proc.pid, grace)
        self._signal(signal.SIGKILL)

    def _signal(self, sig) -> bool:
        try:
            os.killpg(os.getpgid(self.proc.pid), sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def _exited(self) -> bool:
        try:
            return self.proc.poll() is not None
        except Exception:
            return False


def run_turn(
    prompt: str,
    *,
    binary: str,
    cwd,
    permission_args: list[str],
    session_id: str | None = None,
    model: str | None = None,
    append_system_prompt: str | None = None,
    extra_args: list[str] = (),
    env: dict | None = None,
    timeout: int = 900,
    on_init=None,      # fn(session_id)
    on_activity=None,  # fn(tool_name, tool_input)
    on_start=None,     # fn(RunHandle)
) -> TurnResult:
    cmd = [binary, "-p", "--output-format", "stream-json", "--verbose"]
    cmd += permission_args
    if model:
        cmd += ["--model", model]
    if session_id:
        cmd += ["--resume", session_id]
    if append_system_prompt:
        cmd += ["--append-system-prompt", append_system_prompt]
    cmd += list(extra_args)

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=cwd, env=env,
            start_new_session=True,  # own process group so stop/timeout kills children too
        )
    except FileNotFoundError:
        raise ClaudeError(f"`{binary}` not found — is Claude Code installed and on PATH?")

    handle = RunHandle(proc)
    if on_start:
        on_start(handle)

    timed_out = threading.Event()

    def _kill_on_timeout():
        timed_out.set()
        handle.stop()

    timer = threading.Timer(timeout, _kill_on_timeout)
    timer.start()
    timed_out.clear()

    result = TurnResult()
    saw_result = False
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")

            if etype == "system" and event.get("subtype") == "init":
                result.session_id = event.get("session_id")
                if on_init and result.session_id:
                    on_init(result.session_id)

            elif etype == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use" and on_activity:
                        on_activity(block.get("name", "tool"), block.get("input") or {})

            elif etype == "result":
                saw_result = True
                result.text = event.get("result") or ""
                result.session_id = event.get("session_id") or result.session_id
                result.cost_usd = event.get("total_cost_usd") or 0.0
                result.duration_ms = event.get("duration_ms") or 0
                result.num_turns = event.get("num_turns") or 0
                if event.get("is_error"):
                    raise ClaudeError(result.text or "Claude reported an error.")

        stderr = proc.stderr.read()
        proc.wait()
    finally:
        timer.cancel()
        # Cancelling the timer removes the child's only deadline, so if we are
        # leaving while it is still alive (any error path out of the read loop)
        # it would run forever with nobody reading it. Never abandon a child.
        if proc.poll() is None:
            log.warning("child %s still alive as the turn unwinds — stopping it", proc.pid)
            handle.stop()
        try:
            proc.stdout.close()
            proc.stderr.close()
        except Exception:
            pass

    if handle.stopped:
        if timed_out.is_set():
            raise ClaudeTimeout(f"Claude timed out after {timeout}s.")
        raise ClaudeStopped("Stopped by user.")

    if not saw_result:
        detail = (stderr or "").strip()[-1500:]
        raise ClaudeError(
            f"claude exited (code {proc.returncode}) without a result."
            + (f"\n```{detail}```" if detail else "")
        )

    if not result.text:
        result.text = "(no output)"
    return result

"""Boot the bot and the visualizer for real, and say whether they came up.

The suite lifts functions out of bot.py and runs them against fakes; nothing
in it ever imported bot.py as a program. So on 2026-10-26 a commit whose
module level registered `server.route("/ops", handle_ops)` three thousand
lines before `def handle_ops` passed every test, landed, and was deployed --
and bot.py raised NameError at import. Silkworm was down for 18 days.

This is the check that would have caught it: start `bot.py` and
`visualizer.py` as subprocesses, from the checkout under test, against a
throwaway state directory (SILKWORM_HOME), spare ports, and a stand-in Slack
on loopback (SILKWORM_OFFLINE + SILKWORM_SLACK_API) with tokens that are not
tokens. It passes only when the bot's /status answers and reports the
revision it booted, and the visualizer answers; an import error, a crash or a
timeout fails it with the tail of stderr. It runs as part of
`silkworm test`, so the landing gate and the deploy preflight both run it.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

#: How long the bot gets to answer /status. A cold import of slack_bolt plus
#: the stores takes a few seconds; a busy machine running the suite in
#: parallel can take several times that.
BOOT_TIMEOUT_S = 90.0
#: How much of a failed process's stderr to report.
TAIL_LINES = 25

#: Everything the stand-in Slack answers with, whatever the method. Enough
#: for auth.test (user_id, url) and for the history reads startup does.
FAKE_SLACK = {"ok": True, "user_id": "U0SMOKE", "user": "smoke", "bot_id": "B0SMOKE",
              "team_id": "T0SMOKE", "team": "smoke", "url": "https://smoke.invalid/",
              "messages": [], "channels": [], "members": [], "ts": "1.000001",
              "channel": {"id": "D0SMOKE"}, "has_more": False,
              "response_metadata": {"next_cursor": ""}}

#: Variables never passed to the booted processes: real credentials and
#: anything that would make them act as a turn.
_DROP_PREFIXES = ("SLACK_", "SILKWORM_", "CLAUDE_", "ANTHROPIC_", "GMAIL_", "VIZ_",
                  "SUPABASE", "GITHUB_", "GH_")


class FakeSlack:
    """Answers every Web API call with FAKE_SLACK, on loopback. Records the
    methods called, so a test can tell the bot really reached it."""

    def __init__(self):
        self.calls: list[str] = []
        calls = self.calls

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                calls.append(self.path.rsplit("/", 1)[-1].split("?")[0])
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                body = json.dumps(FAKE_SLACK).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/"
        threading.Thread(target=self.server.serve_forever, daemon=True,
                         name="smoke-slack").start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def spare_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tail(path: Path, n: int = TAIL_LINES) -> str:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def boot_env(home: Path, slack_url: str, bot_port: int, viz_port: int,
             base=None) -> dict:
    """The booted processes' environment: ours, minus every credential and
    turn variable, plus the smoke switches. HOME is the scratch home too, so
    nothing reads the real ~/.claude, keychain-backed config aside."""
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if not k.startswith(_DROP_PREFIXES)}
    env.update({
        "HOME": str(home),
        "SILKWORM_HOME": str(home),
        "SILKWORM_OFFLINE": "1",
        "SILKWORM_SLACK_API": slack_url,
        # Shaped so the SDKs accept them, and not tokens.
        "SLACK_BOT_TOKEN": "xoxb-smoke-not-a-token",
        "SLACK_APP_TOKEN": "xapp-smoke-not-a-token",
        "APPROVAL_PORT": str(bot_port),
        "SILKWORM_VIZ_PORT": str(viz_port),
        "SILKWORM_VAPID_DIR": str(home / "vapid"),
        # Nothing it starts may spend anything or reach out on a schedule.
        "CLAUDE_BIN": "/usr/bin/false",
        "NAMING_MODEL": "", "SUMMARY_MODEL": "",
        "HARVEST_INTERVAL_H": "0",
        "DIGEST_AT": "off", "OPERATOR_SWEEP_AT": "off", "HEALTH_LEAD": "off",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def _post(port: int, path: str, timeout: float = 2.0) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=b"{}",
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def _get_ok(port: int, path: str, timeout: float = 2.0) -> bool:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as resp:
        return resp.status == 200


def _stop(proc) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.wait(timeout=10)


def _wait(proc, ready, timeout: float, clock=time.monotonic):
    """Poll `ready()` until it returns something truthy, the process exits,
    or `timeout`. (answer, why-not)."""
    deadline = clock() + timeout
    last = ""
    while clock() < deadline:
        if proc.poll() is not None:
            return None, f"exited with status {proc.returncode} before answering"
        try:
            got = ready()
            if got:
                return got, ""
        except Exception as e:                      # noqa: BLE001 - not up yet
            last = f"{type(e).__name__}: {e}"
        time.sleep(0.25)
    return None, f"did not answer within {int(timeout)}s" + (f" ({last})" if last else "")


def boot(code_dir, *, python: str | None = None, timeout: float = BOOT_TIMEOUT_S,
         expect_sha: str = "", visualizer: bool = True) -> dict:
    """Boot `code_dir`'s bot.py (and visualizer.py) in a scratch home.

    {"ok", "why", "stderr", "status", "slack_calls"}. `why` and `stderr`
    (the tail of the failing process's stderr) say what went wrong; `status`
    is the bot's /status answer when it gave one. `expect_sha`, given, must be
    the revision the bot reports.
    """
    code_dir = Path(code_dir).resolve()
    python = python or sys.executable
    home = Path(tempfile.mkdtemp(prefix="silkworm-smoke-"))
    slack = FakeSlack()
    bot_port, viz_port = spare_port(), spare_port()
    env = boot_env(home, slack.url, bot_port, viz_port)
    # The visualizer reads the bot's port from its home's .env.
    (home / ".env").write_text(f"APPROVAL_PORT={bot_port}\n")
    out: dict = {"ok": False, "why": "", "stderr": "", "status": {},
                 "bot_port": bot_port, "viz_port": viz_port}
    bot = viz = None
    try:
        bot_err = home / "bot.err.log"
        with open(home / "bot.log", "wb") as so, open(bot_err, "wb") as se:
            bot = subprocess.Popen([python, str(code_dir / "bot.py")], cwd=str(home), env=env,
                                   stdin=subprocess.DEVNULL, stdout=so, stderr=se,
                                   start_new_session=True)

        def answered():
            st = _post(bot_port, "/status")
            return st if st.get("online") else None
        st, why = _wait(bot, answered, timeout)
        if not st:
            out.update(why=f"bot.py {why}", stderr=tail(bot_err))
            return out
        out["status"] = st
        rev = st.get("revision") or {}
        started = rev.get("started") or ""
        if not started:
            out.update(why="bot.py answered /status but reported no revision",
                       stderr=tail(bot_err))
            return out
        if expect_sha and started != expect_sha:
            out.update(why=f"bot.py reports revision {started[:8]}, expected {expect_sha[:8]}",
                       stderr=tail(bot_err))
            return out
        if visualizer:
            viz_err = home / "viz.err.log"
            with open(home / "viz.log", "wb") as so, open(viz_err, "wb") as se:
                viz = subprocess.Popen([python, str(code_dir / "visualizer.py")], cwd=str(home),
                                       env=env, stdin=subprocess.DEVNULL, stdout=so, stderr=se,
                                       start_new_session=True)
            ok, why = _wait(viz, lambda: _get_ok(viz_port, "/favicon.svg"), timeout)
            if not ok:
                out.update(why=f"visualizer.py {why}", stderr=tail(viz_err))
                return out
        if bot.poll() is not None:
            out.update(why=f"bot.py exited with status {bot.returncode} after answering",
                       stderr=tail(bot_err))
            return out
        out["ok"] = True
        return out
    finally:
        out["slack_calls"] = list(slack.calls)
        _stop(viz)
        _stop(bot)
        slack.close()
        shutil.rmtree(home, ignore_errors=True)


def describe(res: dict) -> str:
    if res.get("ok"):
        rev = (res.get("status") or {}).get("revision") or {}
        return f"booted: /status answered, running {(rev.get('started') or '')[:8]}"
    out = f"did not boot: {res.get('why') or 'unknown'}"
    if res.get("stderr"):
        out += "\n" + "\n".join("    " + line for line in res["stderr"].splitlines())
    return out


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent)
    r = boot(target)
    print(describe(r))
    sys.exit(0 if r["ok"] else 1)

"""Telling the owner something when the bot cannot.

Everything else that reaches you goes through the bot -- its Slack client,
its push notifier -- so the one thing that never arrived was "the bot is
down". This goes around it: chat.postMessage with the bot token from the
install's .env, to the home DM (SILKWORM_HOME_CHANNEL, else the most recently
active DM in sessions.json, as bot.home_channel picks), and a web push sent by
running push.py directly in the venv (it needs `cryptography`; this does not).

Stdlib only: the watchdog and `silkworm deploy` use it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

import envfile

SLACK_API = "https://slack.com/api/chat.postMessage"


def env_value(home, name: str) -> str:
    return (envfile.value(Path(home) / ".env", name) or "").strip()


def home_channel(home) -> str:
    chan = env_value(home, "SILKWORM_HOME_CHANNEL")
    if chan:
        return chan
    try:
        sessions = json.loads((Path(home) / "sessions.json").read_text() or "{}")
    except (OSError, ValueError):
        return ""
    dms = [k for k, v in sorted(sessions.items(),
                                key=lambda kv: -((kv[1] or {}).get("updated") or 0))
           if k.startswith("D")]
    return dms[0].split(":", 1)[0] if dms else ""


def slack_post(token: str, channel: str, text: str, thread_ts: str = "",
               url: str = SLACK_API, timeout: float = 15) -> tuple:
    """(ok, error). Never raises."""
    if not token or not channel:
        return False, "no bot token or no channel"
    body = {"channel": channel, "text": text[:3900]}
    if thread_ts:
        body["thread_ts"] = thread_ts
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ans = json.loads(resp.read() or b"{}")
    except Exception as e:                       # noqa: BLE001 - reported, not raised
        return False, f"{type(e).__name__}: {e}"
    return bool(ans.get("ok")), ans.get("error") or ""


def push_cmd(home, title: str, body: str, python: str = "") -> list:
    home = Path(home)
    py = python or str(home / ".venv" / "bin" / "python")
    if not Path(py).exists():
        py = sys.executable
    return [py, str(Path(__file__).resolve().parent / "push.py"), "alert",
            "--home", str(home), "--title", title, "--body", body]


def push_send(home, title: str, body: str, run=subprocess.run) -> tuple:
    """(ok, detail): push.py's direct send, in its own process."""
    try:
        r = run(push_cmd(home, title, body), capture_output=True, text=True, timeout=90)
    except Exception as e:                       # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    return r.returncode == 0, (r.stdout or r.stderr or "").strip()[-300:]


def owner(home, text: str, *, title: str = "Silkworm", thread: str = "",
          post=slack_post, push=push_send) -> dict:
    """Tell the owner, both ways. `thread` (channel:ts), given, is where the
    Slack message goes instead of the home DM. Returns what happened."""
    token = env_value(home, "SLACK_BOT_TOKEN")
    if thread and ":" in thread:
        channel, ts = thread.split(":", 1)
    else:
        channel, ts = home_channel(home), ""
    s_ok, s_err = post(token, channel, text, ts)
    first = " ".join(text.split())
    p_ok, p_detail = push(home, title, first[:180])
    return {"slack": s_ok, "slack_error": s_err, "push": p_ok, "push_detail": p_detail,
            "channel": channel}

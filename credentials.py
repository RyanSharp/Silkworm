"""How much life is left in the credentials headless turns run on.

Claude Code keeps OAuth credentials in the login Keychain when a process can
reach one and in `~/.claude/.credentials.json` when it cannot, and nothing
keeps the two in step. An interactive login refreshes only the file, so a
Keychain copy ages out unnoticed and every headless turn starts failing while
`claude` in a terminal keeps working -- which is what happened on 2026-08-30.

The refresh token also has a fixed expiry that does *not* roll forward when the
access token is refreshed (observed unchanged across a day and several
refreshes). So the credential dies on a schedule, and a restart cannot help:
the dead token is on disk, and restarting only re-reads the same file.

Everything here returns times and booleans, never a token. It feeds a status
line and a Slack warning, and a credential check that leaks the credential
would be a worse bug than the one it catches.
"""

import json
import logging
import subprocess
import time
from pathlib import Path

log = logging.getLogger("silkworm.credentials")

CREDS = Path.home() / ".claude" / ".credentials.json"

#: Warn this far ahead. A day is enough to act without being nagged for a week.
WARN_H = 24


def keychain_item_present() -> bool:
    """Whether the second credential store exists. Its existence is the risk."""
    try:
        return subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials"],
            capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def state(has_token: bool = False, path: Path | None = None, now: float | None = None) -> dict:
    """`mode` is one of token / missing / unreadable / oauth.

    A long-lived token short-circuits the question: it bypasses both stores, so
    neither drift nor refresh expiry applies.
    """
    if has_token:
        return {"mode": "token"}
    p = path or CREDS
    if not p.exists():
        return {"mode": "missing"}
    try:
        o = json.loads(p.read_text()).get("claudeAiOauth") or {}
        exp = float(o["refreshTokenExpiresAt"]) / 1000
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return {"mode": "unreadable"}
    left = exp - (time.time() if now is None else now)
    return {"mode": "oauth", "expires_at": exp, "hours_left": left / 3600}


def warning(st: dict) -> str:
    """The Slack message for a credential about to die, or '' if it is fine."""
    if st.get("mode") == "missing":
        return (":key: *Claude credentials are missing.* Every turn will fail until "
                "you log in on the host (`claude`) or set `CLAUDE_CODE_OAUTH_TOKEN`.")
    if st.get("mode") == "unreadable":
        return ":key: *Claude credentials are unreadable* — every turn will fail."
    if st.get("mode") != "oauth" or st["hours_left"] > WARN_H:
        return ""
    h = st["hours_left"]
    when = "have expired" if h <= 0 else f"expire in about {h:.0f}h"
    return (f":key: *Claude credentials {when}.* Turns will fail once they do, and "
            "*restarting will not help* — the expired token is on disk, so a restart "
            "just re-reads it.\n"
            "Fix it on the host with `claude setup-token`, then put the result in "
            "`CLAUDE_CODE_OAUTH_TOKEN` in Silkworm's `.env` and restart. "
            "Check it with `silkworm status`.")

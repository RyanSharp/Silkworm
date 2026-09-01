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


KEYCHAIN_SERVICE = "Claude Code-credentials"


def _keychain_read() -> dict | None:
    """The Keychain copy, or None if there is none or we cannot reach it.

    A process that cannot reach the login Keychain fails fast here (rc=36,
    "interaction not allowed"), which is exactly the asymmetry that lets the
    two stores drift in the first place.
    """
    try:
        r = subprocess.run(["security", "find-generic-password", "-w",
                            "-s", KEYCHAIN_SERVICE],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except Exception:
        return None


def keychain_item_present() -> bool:
    try:
        return subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
            capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def stores(path: Path | None = None) -> list[str]:
    """Which stores currently hold credentials.

    Two is the dangerous number, not one: the 2026-08-30 outage was a Keychain
    copy and a file copy drifting apart. One of either is fine, and which one
    it is has changed under us before.
    """
    found = []
    if (path or CREDS).exists():
        found.append("file")
    if keychain_item_present():
        found.append("keychain")
    return found


def state(has_token: bool = False, path: Path | None = None, now: float | None = None) -> dict:
    """`mode` is one of token / missing / unreadable / oauth.

    A long-lived token short-circuits the question: it bypasses both stores, so
    neither drift nor refresh expiry applies.
    """
    if has_token:
        return {"mode": "token", "stores": []}
    p = path or CREDS
    where = stores(p)
    raw, source = None, ""
    if p.exists():
        try:
            raw, source = json.loads(p.read_text()), "file"
        except (OSError, ValueError, json.JSONDecodeError):
            return {"mode": "unreadable", "stores": where, "source": "file"}
    elif "keychain" in where:
        raw, source = _keychain_read(), "keychain"
        if raw is None:
            # Present but unreadable from here -- which is its own diagnosis:
            # a process that cannot reach it has no credentials at all.
            return {"mode": "unreadable", "stores": where, "source": "keychain"}
    if raw is None:
        return {"mode": "missing", "stores": where, "source": ""}
    try:
        o = raw.get("claudeAiOauth") or {}
        exp = float(o["refreshTokenExpiresAt"]) / 1000
    except (ValueError, KeyError, AttributeError):
        return {"mode": "unreadable", "stores": where, "source": source}
    left = exp - (time.time() if now is None else now)
    return {"mode": "oauth", "stores": where, "source": source,
            "expires_at": exp, "hours_left": left / 3600}


def warning(st: dict) -> str:
    """The Slack message for a credential problem, or '' if there is none."""
    if st.get("mode") == "token":
        return ""
    if len(st.get("stores") or []) > 1:
        return (":key: *Claude credentials exist in two places* (the login Keychain "
                "and `~/.claude/.credentials.json`). Nothing keeps them in step, and "
                "the one a process uses depends on whether it can reach the Keychain "
                "— which is how every headless turn broke on 2026-08-30. Delete "
                "whichever is stale, or set `CLAUDE_CODE_OAUTH_TOKEN` to bypass both.")
    if st.get("mode") == "missing":
        return (":key: *Claude credentials are missing.* Every turn will fail until "
                "you log in on the host (`claude`) or set `CLAUDE_CODE_OAUTH_TOKEN`.")
    if st.get("mode") == "unreadable":
        return (":key: *Claude credentials are unreadable* from where the bot runs — "
                "every turn will fail.")
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

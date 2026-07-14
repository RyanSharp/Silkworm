#!/usr/bin/env python3
"""Global Claude Code hook: tell Silkworm when sessions start and end.

Installed into ~/.claude/settings.json (by install_hooks.py) for the
SessionStart and SessionEnd events. Fire-and-forget: 2s timeout, silent on
every failure, never writes to stdout (SessionStart stdout would be injected
into the session's context). Safe to leave installed when the bot is down.

Stdlib only.
"""

import json
import os
import sys
import urllib.request


def main() -> None:
    # Silkworm's own headless runs set this — those aren't terminal sessions.
    if os.environ.get("SILKWORM_BOT"):
        return
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return

    port = "8787"
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env_file) as f:
            for line in f:
                if line.strip().startswith("APPROVAL_PORT="):
                    port = line.split("=", 1)[1].strip()
    except OSError:
        pass

    body = json.dumps({
        "hook_event_name": payload.get("hook_event_name"),
        "session_id": payload.get("session_id"),
        "source": payload.get("source"),
        "reason": payload.get("reason"),
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/session-event", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass


if __name__ == "__main__":
    main()

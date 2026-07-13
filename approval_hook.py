#!/usr/bin/env python3
"""Claude Code PreToolUse hook: ask the Slack bot for permission.

Claude Code runs this before every tool call (when the bot is in
CLAUDE_APPROVAL_MODE=slack). It forwards the tool request to the bot's local
HTTP endpoint, which posts Approve/Deny buttons in the Slack thread and blocks
until someone clicks (or the timeout passes). Fails closed: any error denies.

Stdlib only — runs outside the bot's venv.
"""

import json
import os
import sys
import urllib.request


def respond(decision: str, reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        respond("deny", "approval hook could not parse input")

    port = os.environ.get("SLACK_BOT_APPROVAL_PORT")
    if not port:
        respond("deny", "SLACK_BOT_APPROVAL_PORT not set")

    timeout = float(os.environ.get("SLACK_BOT_APPROVAL_TIMEOUT", "300")) + 15
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/approve",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            answer = json.loads(resp.read())
    except Exception as e:
        respond("deny", f"approval service unreachable: {e}")

    decision = "allow" if answer.get("decision") == "allow" else "deny"
    respond(decision, answer.get("reason", "decided via Slack"))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Install Silkworm's session hooks into ~/.claude/settings.json (idempotent).

Adds SessionStart + SessionEnd command hooks pointing at session_hook.py so
the bot knows when a handed-off thread's terminal session opens or exits.
Existing settings are preserved; running twice changes nothing.
"""

import json
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"
HOOK_CMD = f'python3 "{Path(__file__).resolve().parent / "session_hook.py"}"'


def ensure(hooks: dict, event: str) -> bool:
    entries = hooks.setdefault(event, [])
    for entry in entries:
        for h in entry.get("hooks", []):
            if "session_hook.py" in h.get("command", ""):
                return False
    entries.append({"hooks": [{"type": "command", "command": HOOK_CMD, "timeout": 5}]})
    return True


def main() -> None:
    settings = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    hooks = settings.setdefault("hooks", {})
    changed = [ensure(hooks, "SessionStart"), ensure(hooks, "SessionEnd")]
    if any(changed):
        SETTINGS.parent.mkdir(exist_ok=True)
        SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
        print(f"installed session hooks into {SETTINGS}")
    else:
        print("session hooks already installed")


if __name__ == "__main__":
    main()

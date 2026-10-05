"""Read one setting from a .env file the way python-dotenv (the bot's reader) does.

Shared by bin/silkworm, visualizer.py and session_hook.py, which read .env
without loading dotenv. Each used to hand-roll its own parser, and the copies
drifted: one missed `export NAME=...`, two kept the quotes round a value.

Stdlib only, and no 3.10+ syntax: session_hook.py runs under the system
python3, which on macOS is 3.9.
"""

from __future__ import annotations

from pathlib import Path


def value(path: Path | str, name: str) -> str | None:
    """What the .env at `path` sets `name` to, or None if it doesn't (or the
    file can't be read). The last assignment wins, `export NAME=...` counts,
    and surrounding quotes are stripped."""
    val = None
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, sep, rest = line.partition("=")
            if sep and key.strip() == name:
                val = rest.strip().strip("\"'")
    except OSError:
        pass
    return val

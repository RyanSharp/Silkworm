"""Finding the `claude` process backing a session.

`pgrep -f` is the obvious tool and it does not work here: Silkworm passes a
large --append-system-prompt, and pgrep silently fails to match these processes
at all (on macOS it cannot even find them by name). Scanning `ps` output finds
them reliably, so that is what we use.

A substring match alone is not enough: a shell that merely mentions the session
id (and whose environment mentions ~/.claude) looks identical to the real
thing. So we require the executable itself to be `claude`, not just any command
line containing the word.
"""

import os
import subprocess


def session_pids(session_id: str) -> list[int]:
    """PIDs of `claude` processes running the given session."""
    if not session_id:
        return []
    try:
        out = subprocess.run(["ps", "-eww", "-o", "pid=,command="],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    me, pids = os.getpid(), []
    for line in out.splitlines():
        pid_s, _, cmd = line.strip().partition(" ")
        if not pid_s.isdigit() or int(pid_s) == me or session_id not in cmd:
            continue
        exe = cmd.split(" ", 1)[0]
        if os.path.basename(exe) != "claude":  # not a shell that merely mentions it
            continue
        pids.append(int(pid_s))
    return pids


def session_alive(session_id: str) -> bool:
    return bool(session_pids(session_id))

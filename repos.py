"""Git repository identity for learning scope.

A session's scope is the identity of the repo it works in — the normalized
`remote origin` URL (e.g. "github.com/RyanSharp/Silkworm"). That's stable across
worktrees, clones, and machines, so every workspace of the same repo shares the
same learnings. Directories with no git remote return "" (the caller falls back
to a path-based scope).
"""

import os
import re
import subprocess


def _git(cwd: str, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", cwd, *args],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def normalize(url: str) -> str:
    """git@github.com:owner/repo.git | https://github.com/owner/repo.git → github.com/owner/repo"""
    u = url.strip()
    u = re.sub(r"^\w+://", "", u)        # strip scheme
    u = re.sub(r"^[^@/]+@", "", u)       # strip user@
    u = u.replace(":", "/", 1)           # scp-style host:owner → host/owner
    u = re.sub(r"\.git$", "", u)
    return u.strip("/")


def identity(cwd) -> str:
    """Repo identity for a working directory, or '' if it has no git remote."""
    cwd = str(cwd)
    if not os.path.isdir(cwd):
        return ""
    url = _git(cwd, "config", "--get", "remote.origin.url")
    return normalize(url) if url else ""

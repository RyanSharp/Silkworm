"""Git-backed sharing for learnings.json.

If the directory holding learnings.json is a git repo with a remote, `sync()`
commits local changes, pulls, and pushes — so learnings are shared across
machines. Concurrent appends from two devices are reconciled by a union merge
(dedup by learning id), so sharing never produces a hand-resolved conflict.
"""

import json
import logging
import subprocess
from pathlib import Path

import jsonstore

log = logging.getLogger("silkworm.learnings_git")


def _git(repo: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=check)


def is_git_backed(learnings_file: Path) -> bool:
    repo = learnings_file.parent
    r = _git(repo, "rev-parse", "--is-inside-work-tree")
    return r.returncode == 0 and r.stdout.strip() == "true"


def _union(a: str, b: str) -> list:
    """Merge two learnings.json blobs, keeping every unique learning by id.

    Returns the records rather than the text of them, so the one caller writes
    them through jsonstore like every other writer of a state file.
    """
    def load(s):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return []
    merged, seen = [], set()
    for rec in load(a) + load(b):
        rid = rec.get("id")
        if rid and rid not in seen:
            seen.add(rid)
            merged.append(rec)
    merged.sort(key=lambda x: x.get("created", 0))
    return merged


def sync(learnings_file: Path) -> dict:
    repo = learnings_file.parent
    name = learnings_file.name
    if not is_git_backed(learnings_file):
        return {"ok": False, "error": f"{repo} is not a git repo — run: silkworm learnings init"}

    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"

    # 1. Commit any local changes so the tree is clean before we pull.
    _git(repo, "add", name)
    _git(repo, "commit", "-m", "learnings: local update")  # no-op if nothing staged

    has_remote = bool(_git(repo, "remote").stdout.strip())
    if not has_remote:
        return {"ok": True, "note": "committed locally (no remote configured)"}

    if _git(repo, "fetch", "origin").returncode != 0:
        return {"ok": False, "error": "git fetch failed — offline or no access"}

    # No upstream yet → first push.
    if _git(repo, "rev-parse", "--verify", f"origin/{branch}").returncode != 0:
        push = _git(repo, "push", "-u", "origin", branch)
        return {"ok": push.returncode == 0,
                "note": "pushed initial learnings" if push.returncode == 0 else push.stderr.strip()}

    merge = _git(repo, "merge", "--no-edit", f"origin/{branch}")
    if merge.returncode != 0:
        # Conflict — reconcile learnings.json by union, drop other conflicts to theirs.
        ours = _git(repo, "show", f":2:{name}").stdout
        theirs = _git(repo, "show", f":3:{name}").stdout
        if ours or theirs:
            # Through jsonstore, like every other write of a state file: this
            # one truncates learnings.json and refills it, and it is resolving
            # a conflict between two machines' copies, so it is the write with
            # the most in it to lose. It refreshes the backup too, which a bare
            # write_text left holding pre-merge records.
            jsonstore.save(learnings_file, _union(ours, theirs))
            _git(repo, "add", name)
        if _git(repo, "commit", "--no-edit").returncode != 0:
            _git(repo, "merge", "--abort")
            return {"ok": False, "error": "merge conflict outside learnings.json — resolve by hand"}
        log.info("learnings: union-merged with origin/%s", branch)

    push = _git(repo, "push", "origin", branch)
    if push.returncode != 0:
        return {"ok": False, "error": "git push failed: " + push.stderr.strip()[:200]}
    return {"ok": True, "note": f"synced with origin/{branch}"}


def init(learnings_file: Path, remote: str = "") -> dict:
    repo = learnings_file.parent
    repo.mkdir(parents=True, exist_ok=True)
    if not is_git_backed(learnings_file):
        _git(repo, "init")
        _git(repo, "checkout", "-B", "main")
    if remote:
        _git(repo, "remote", "remove", "origin")
        if _git(repo, "remote", "add", "origin", remote).returncode != 0:
            return {"ok": False, "error": "could not add remote"}
    if not learnings_file.exists():
        jsonstore.save(learnings_file, [])
    # The .prev/.corrupt/.tmp sidecars jsonstore.py keeps beside learnings.json
    # are local recovery copies, not shared state. Added one at a time rather
    # than only when the file is absent: this runs against directories that
    # were set up before those sidecars existed, and writing nothing there is
    # how a machine's backup copy ends up pushed to everyone else's.
    gi = repo / ".gitignore"
    have = gi.read_text().splitlines() if gi.exists() else []
    missing = [line for line in ("harvest_state.json", "*.json.prev",
                                 "*.json.corrupt", "*.json*.tmp.*")
               if line not in have]
    if missing:
        gi.write_text("".join(f"{line}\n" for line in have + missing))
    return {"ok": True, "dir": str(repo)}

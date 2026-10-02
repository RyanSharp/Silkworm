"""Git hooks that keep an implementor task off the base branch and the remote.

Landing is Silkworm's job: verification, then review, then merge.land, then
(if the project publishes) a push. On 2026-10-02 a Cadence implementor merged
its own commit into the checkout's main and pushed it to origin before either
gate had run. Nothing stopped it, because the only thing saying "don't" was
the prompt. These hooks make git say it.

How a hook knows who is calling: the bot exports ROLE_VAR and ID_VAR into the
environment of a *task turn's* claude subprocess (bot.claude_env), and every
git command the agent runs inherits them. The bot's own landing, verification
and releases run in the bot process, which never carries them, and neither
does anything you type -- so the guard is invisible to both.

Two hooks, installed once per repository into the common git dir (or
core.hooksPath), so every worktree of the repo shares them:

  pre-push              refuses every push from an implementor.
  reference-transaction refuses an implementor moving any ref but its own
                        branch (refs/heads/silkworm/<task id>), HEAD,
                        remote-tracking refs (so fetch works), and the
                        per-worktree bookkeeping a rebase or stash writes.
                        That is: no moving main, no other branch, no tags.

A hook already in place is not replaced: it is moved aside to
<name>.silkworm-chained and run by ours, with the same arguments and stdin.

This is a guard against a well-meaning agent doing what a goal seemed to ask
for, not a sandbox: `--no-verify` skips pre-push, and an agent determined to
can unset the variable. The role prompt says why it must not, and the
reference-transaction half cannot be skipped with a flag.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROLE_VAR = "SILKWORM_TASK_ROLE"
ID_VAR = "SILKWORM_TASK_ID"
#: The one role the guard constrains. A reviewer or ideator is read-only
#: already; an `assistant` task is a conversation the user is in.
GUARDED_ROLE = "implementor"

HOOKS = ("pre-push", "reference-transaction")
CHAINED_SUFFIX = ".silkworm-chained"
MARKER = "silkworm-guard"
#: reference-transaction appeared in git 2.28.
MIN_GIT = (2, 28)

_HEAD = f"""#!/bin/sh
# {MARKER}: installed by Silkworm (git_guard.py). Edits here are overwritten;
# a hook you had before lives on as "$0{CHAINED_SUFFIX}" and is run below.
chained="$0{CHAINED_SUFFIX}"
"""

PRE_PUSH = _HEAD + f"""
if [ "${{{ROLE_VAR}:-}}" = "{GUARDED_ROLE}" ]; then
    echo "silkworm: an implementor task may not push." >&2
    echo "silkworm: commit to your own branch (silkworm/${{{ID_VAR}:-<task id>}}) and" >&2
    echo "silkworm: report that it is ready to land -- landing and pushing are" >&2
    echo "silkworm: Silkworm's job, after verification and review." >&2
    exit 1
fi
if [ -x "$chained" ]; then
    exec "$chained" "$@"
fi
exit 0
"""

REFERENCE_TRANSACTION = _HEAD + f"""
input=$(cat)
if [ "$1" = "prepared" ] && [ "${{{ROLE_VAR}:-}}" = "{GUARDED_ROLE}" ]; then
    own="refs/heads/silkworm/${{{ID_VAR}:-}}"
    bad=""
    while read -r old new ref; do
        [ -n "$ref" ] || continue
        case "$ref" in
            "$own"|HEAD) ;;
            # fetch; and per-worktree bookkeeping a rebase, bisect or stash
            # writes, none of which moves anything another checkout reads.
            refs/remotes/*|refs/rewritten/*|refs/bisect/*|refs/worktree/*|refs/stash) ;;
            refs/*) bad="$bad $ref" ;;
            # Pseudo-refs (ORIG_HEAD, MERGE_HEAD, ...) are per-worktree.
            *) ;;
        esac
    done <<EOF
$input
EOF
    if [ -n "$bad" ]; then
        # Packing refs (git pack-refs, which gc runs) rewrites every ref as a
        # create in packed-refs plus a delete of the loose file -- the same
        # lines as a real move, but no ref changes value. Asked only here, on
        # the way to refusing, so ordinary updates never pay for a ps.
        case "$(ps -o args= -p "$PPID" 2>/dev/null)" in
            *pack-refs*) bad="" ;;
        esac
    fi
    if [ -n "$bad" ]; then
        echo "silkworm: an implementor task may only move its own branch ($own)." >&2
        echo "silkworm: refused:$bad" >&2
        echo "silkworm: do not merge into, reset or push the base branch, make other" >&2
        echo "silkworm: branches, or tag. Commit on your branch and report that it is" >&2
        echo "silkworm: ready to land; Silkworm lands it after verification and review." >&2
        exit 1
    fi
fi
if [ -x "$chained" ]; then
    if [ -n "$input" ]; then printf '%s\\n' "$input"; fi | "$chained" "$@"
    exit $?
fi
exit 0
"""

SCRIPTS = {"pre-push": PRE_PUSH, "reference-transaction": REFERENCE_TRANSACTION}


def env_for(role: str, task_id: str) -> dict:
    """What a task turn's subprocess env carries so the hooks can see it."""
    return {ROLE_VAR: role or "", ID_VAR: task_id or ""}


def strip(env: dict) -> dict:
    """`env` without the guard's variables. Everything that is not a task turn
    -- above all the bot's own landing -- must run without them."""
    for k in (ROLE_VAR, ID_VAR):
        env.pop(k, None)
    return env


def _git(repo, *args) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                           text=True, timeout=15, env=strip(dict(os.environ)))
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return p.returncode, p.stdout.strip()


def git_version() -> tuple[int, ...]:
    code, out = _git(".", "--version")
    nums = []
    for part in (out.split()[2] if code == 0 and len(out.split()) > 2 else "").split("."):
        if not part.isdigit():
            break
        nums.append(int(part))
    return tuple(nums)


def hooks_dir(repo) -> Path | None:
    """Where git looks for this repository's hooks, shared by its worktrees.

    core.hooksPath if it is set (relative to the main worktree, which is where
    a relative one is meant to point), else <common git dir>/hooks.
    """
    code, common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if code != 0 or not common:
        return None
    common_p = Path(common)
    code, hp = _git(repo, "config", "--get", "core.hooksPath")
    if code == 0 and hp:
        p = Path(os.path.expanduser(hp))
        if not p.is_absolute():
            base = common_p.parent if common_p.name == ".git" else common_p
            p = base / p
        return p
    return common_p / "hooks"


def _ours(path: Path) -> bool:
    try:
        return MARKER in path.read_text(errors="replace")[:400]
    except OSError:
        return False


def install(repo) -> dict:
    """Install (or refresh) both hooks for `repo`. Idempotent.

    Returns {"ok", "changed": [...], "chained": [...], "error"}. A hook that
    was already there and is not ours is moved to <name>.silkworm-chained and
    run by ours. If that name is taken too -- ours was replaced after it had
    chained something -- nothing is overwritten and it is reported instead.
    """
    if git_version() < MIN_GIT:
        return {"ok": False, "changed": [], "chained": [],
                "error": f"git {'.'.join(map(str, MIN_GIT))}+ is needed for "
                         "the reference-transaction hook"}
    d = hooks_dir(repo)
    if d is None:
        return {"ok": False, "changed": [], "chained": [],
                "error": f"{repo} is not a git repository"}
    changed, chained = [], []
    try:
        d.mkdir(parents=True, exist_ok=True)
        for name in HOOKS:
            path, want = d / name, SCRIPTS[name]
            if path.exists() or path.is_symlink():
                if _ours(path):
                    if path.read_text() == want and os.access(path, os.X_OK):
                        continue
                else:
                    aside = d / (name + CHAINED_SUFFIX)
                    if aside.exists() or aside.is_symlink():
                        return {"ok": False, "changed": changed, "chained": chained,
                                "error": f"{path} is not Silkworm's and {aside.name} "
                                         "already exists; refusing to overwrite either"}
                    os.rename(path, aside)
                    chained.append(name)
            tmp = d / f".{name}.silkworm-tmp"
            tmp.write_text(want)
            tmp.chmod(0o755)
            os.replace(tmp, path)
            changed.append(name)
    except OSError as e:
        return {"ok": False, "changed": changed, "chained": chained, "error": str(e)}
    return {"ok": True, "changed": changed, "chained": chained, "error": ""}


def state(repo) -> tuple[bool, str]:
    """Whether the guard is in force for `repo`, and if not, why not."""
    d = hooks_dir(repo)
    if d is None:
        return False, "not a git repository"
    missing = [n for n in HOOKS
               if not ((d / n).is_file() and (d / n).read_text(errors="replace") == SCRIPTS[n]
                       and os.access(d / n, os.X_OK))]
    if missing:
        return False, f"{', '.join(missing)} missing or out of date in {d}"
    if git_version() < MIN_GIT:
        return False, "git is too old to run reference-transaction"
    return True, str(d)


def ready_repos(records) -> list[tuple[str, str]]:
    """(slug, checkout) for every ready project with a repository."""
    import projects                  # late: keeps this module importable alone
    out = []
    for rec in records:
        if rec.get("archived") or projects.unready(rec):
            continue
        cwd = ((rec.get("scope") or {}).get("cwd") or "").strip()
        if cwd and Path(cwd).is_dir():
            out.append((rec.get("slug") or "", cwd))
    return out

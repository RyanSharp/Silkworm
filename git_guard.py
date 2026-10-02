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
                        Two commands write refs they do not choose and are
                        recognised by the git subcommand that runs them:
                        fetch may follow the remote's tags, and pack-refs
                        (gc) may rewrite refs it leaves at the same value.

Refused rather than guessed at, and reported by `silkworm status`: a relative
or in-tree core.hooksPath (each worktree would read its own copy, or the
install would dirty the base), and an existing hook that finds its work by its
own name ($0), which renaming would silently break.

A hook already in place is not replaced: it is moved aside to
<name>.silkworm-chained and run by ours, with the same arguments and stdin.

This is a guard against a well-meaning agent doing what a goal seemed to ask
for, not a sandbox: `--no-verify` skips pre-push, and an agent determined to
can unset the variable. The role prompt says why it must not, and the
reference-transaction half cannot be skipped with a flag.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
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
zero() {{ case "$1" in ""|*[!0]*) return 1 ;; esac; return 0; }}
# Whether every refused line leaves its ref's value as it was: a create in
# packed-refs of the value the ref already has, or a delete of a loose ref
# whose value packed-refs already holds. That is all packing does.
unmoved() {{
    packed="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)/packed-refs"
    while read -r old new ref; do
        case " $bad$tags " in *" $ref "*) ;; *) continue ;; esac
        if zero "$old" && ! zero "$new"; then
            [ "$(git rev-parse -q --verify "$ref" 2>/dev/null)" = "$new" ] || return 1
        elif ! zero "$old" && zero "$new"; then
            grep -qxF "$old $ref" "$packed" 2>/dev/null || return 1
        else
            return 1
        fi
    done <<EOF
$input
EOF
    return 0
}}
state="$1"                       # kept: the command lookup below reuses $@
if [ "$state" = "prepared" ] && [ "${{{ROLE_VAR}:-}}" = "{GUARDED_ROLE}" ]; then
    own="refs/heads/silkworm/${{{ID_VAR}:-}}"
    bad="" tags=""
    while read -r old new ref; do
        [ -n "$ref" ] || continue
        case "$ref" in
            "$own"|HEAD) ;;
            # fetch; and per-worktree bookkeeping a rebase, bisect or stash
            # writes, none of which moves anything another checkout reads.
            refs/remotes/*|refs/rewritten/*|refs/bisect/*|refs/worktree/*|refs/stash) ;;
            refs/tags/*) tags="$tags $ref" ;;
            refs/*) bad="$bad $ref" ;;
            # Pseudo-refs (ORIG_HEAD, MERGE_HEAD, ...) are per-worktree.
            *) ;;
        esac
    done <<EOF
$input
EOF
    if [ -n "$bad$tags" ]; then
        # Which git command this is, asked only on the way to refusing so an
        # ordinary update never pays for a ps: the first word after `git` and
        # its global options. Two commands write refs they do not choose:
        #   pack-refs (gc runs it) rewrites every ref as a create in
        #     packed-refs plus a delete of the loose file -- the same lines as
        #     a real move, though no ref changes value;
        #   fetch (and so pull) follows the remote's tags into refs/tags --
        #     the remote's, not the task's, and moving no branch.
        set -f                   # split the command line, but never glob it
        set -- $(ps -o args= -p "$PPID" 2>/dev/null)
        [ $# -gt 0 ] && shift
        while [ $# -gt 0 ]; do
            case "$1" in
                -C|-c|--git-dir|--work-tree|--namespace|--exec-path|--config-env|--attr-source) shift 2 ;;
                -*) shift ;;
                *) break ;;
            esac
        done
        case "${1:-}" in
            # Not on the name alone: ps joins argv with spaces, so
            # `git -c 'a.b=x pack-refs' update-ref ...` reads as pack-refs too.
            # Packing is recognisable by what it does -- every ref keeps its
            # value -- so that is checked as well.
            pack-refs) unmoved && bad="" tags="" ;;
            fetch) tags="" ;;
        esac
        bad="$bad$tags"
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
    if [ -n "$input" ]; then printf '%s\\n' "$input"; fi | "$chained" "$state"
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


def where(repo) -> tuple[Path | None, str]:
    """Where git looks for this repository's hooks, or why the guard cannot go
    anywhere every worktree would see it.

    <common git dir>/hooks, shared by every worktree -- or an absolute
    core.hooksPath outside the work tree. Two layouts are refused rather than
    guessed at. A relative core.hooksPath is resolved by git against the top
    of whichever worktree runs the hook, so each task's worktree reads its own
    copy and one install guards nothing but the main checkout. And a hooks
    directory inside the work tree is usually tracked (husky, .githooks), so
    writing into it dirties the checkout, and landing refuses a dirty base.
    """
    code, common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if code != 0 or not common:
        return None, f"{repo} is not a git repository"
    common_p = Path(common)
    code, hp = _git(repo, "config", "--get", "core.hooksPath")
    if code != 0 or not hp:
        return common_p / "hooks", ""
    p = Path(os.path.expanduser(hp))
    if not p.is_absolute():
        return None, (f"core.hooksPath is relative ({hp}), so every worktree reads "
                      "its own hooks; guard it by hand or make it absolute")
    top = common_p.parent if common_p.name == ".git" else common_p
    try:
        inside = p.resolve().is_relative_to(top.resolve())
    except OSError:
        inside = False
    if inside and common_p.name == ".git":
        return None, (f"core.hooksPath ({hp}) is inside the work tree; installing "
                      "there would dirty the checkout landing merges into")
    return p, ""


def hooks_dir(repo) -> Path | None:
    return where(repo)[0]


def _ours(path: Path) -> bool:
    try:
        return MARKER in path.read_text(errors="replace")[:400]
    except OSError:
        return False


#: A hook that finds its work by its own name or location -- husky's
#: `basename "$0"`, `$(dirname "$0")/husky.sh` -- stops working the moment
#: it is renamed to <name>.silkworm-chained, silently, for you as well as for
#: tasks. Such a hook is reported, not chained.
_SELF_REFERENCE = re.compile(r'\$\{?0\b|\$\{?BASH_SOURCE')

#: One install at a time in this process: the sweeper and the dashboard can
#: both reach a repo at once, and two installs interleaving between "is there
#: an aside?" and the rename would move the guard on top of your hook.
_LOCK = threading.Lock()


def _err(msg, changed=(), chained=()) -> dict:
    return {"ok": False, "changed": list(changed), "chained": list(chained), "error": msg}


def install(repo) -> dict:
    """Install (or refresh) both hooks for `repo`. Idempotent.

    Returns {"ok", "changed": [...], "chained": [...], "error"}. A hook that
    was already there and is not ours is moved to <name>.silkworm-chained and
    run by ours. Every hook is checked before anything is touched, and
    nothing is overwritten: an aside name already taken (ours was replaced
    after it chained something), or a hook that would break if renamed, is
    reported and leaves the repository as it was.
    """
    if git_version() < MIN_GIT:
        return _err(f"git {'.'.join(map(str, MIN_GIT))}+ is needed for "
                    "the reference-transaction hook")
    d, why = where(repo)
    if d is None:
        return _err(why)
    with _LOCK:
        to_chain = []
        for name in HOOKS:
            path = d / name
            if (path.exists() or path.is_symlink()) and not _ours(path):
                aside = d / (name + CHAINED_SUFFIX)
                if aside.exists() or aside.is_symlink():
                    return _err(f"{path} is not Silkworm's and {aside.name} already "
                                "exists; refusing to overwrite either")
                try:
                    text = path.read_text(errors="replace")
                except OSError as e:
                    return _err(f"cannot read {path}: {e}")
                if _SELF_REFERENCE.search(text):
                    return _err(f"{path} finds its work by its own name or location "
                                "($0), so renaming it to chain it would silently "
                                "disable it; merge the guard into it by hand")
                to_chain.append(name)
        changed, chained = [], []
        try:
            d.mkdir(parents=True, exist_ok=True)
            for name in HOOKS:
                path, want = d / name, SCRIPTS[name]
                if name in to_chain:
                    os.rename(path, d / (name + CHAINED_SUFFIX))
                    chained.append(name)
                elif (path.is_file() and path.read_text() == want
                        and os.access(path, os.X_OK)):
                    continue
                tmp = d / f".{name}.silkworm-tmp.{os.getpid()}.{threading.get_ident()}"
                tmp.write_text(want)
                tmp.chmod(0o755)
                os.replace(tmp, path)
                changed.append(name)
        except OSError as e:
            return _err(str(e), changed, chained)
    return {"ok": True, "changed": changed, "chained": chained, "error": ""}


def state(repo) -> tuple[bool, str]:
    """Whether the guard is in force for `repo`, and if not, why not."""
    d, why = where(repo)
    if d is None:
        return False, why
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

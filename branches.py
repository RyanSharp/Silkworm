"""Say out loud when finished work is sitting on a branch nobody merged.

A task that makes commits makes them on `silkworm/<task-id>`, in its own
worktree. The worktree is released when the turn ends; the branch outlives it.
That was the whole design -- and the only record that the branch existed was
one line in a Slack reply, which scrolls away.

So eight tasks completed, the board recorded eight `done`, and eight fixes sat
on eight branches that were never merged. None of those bugs were fixed in the
bot actually running. Two of the eight were the *same* fix, proposed on
different nights and implemented twice, because the first one never landed and
so the gap was still there for the next night's pass to find. That second
implementation cost a session and a reviewer to rediscover something already
written down.

Nothing here merges, deletes or pushes anything -- see DESIGN.md on why opening
pull requests is deliberately out of scope. This only answers the question the
system could not previously answer: *what is finished and not in the base?*

Merge state is asked of git rather than stored, because it changes without us:
you land a branch by hand and a stored flag would still say unmerged. What is
stored on the task is only what git cannot recover later -- the branch it used
and the base it was cut from.

Deliberately local: no fetch. A dashboard panel must not wait on the network,
and the cost of being slightly behind is naming a branch that someone else
already merged, which is a great deal better than staying silent about one
nobody did.
"""

import logging
import subprocess
from pathlib import Path

import tasks
import worktrees

log = logging.getLogger("silkworm.branches")

#: A task in one of these still owns its branch: it is working there now, or is
#: about to, or is waiting on a review that will. Every other state has stopped
#: moving on its own, which makes its commits either landed or stranded.
#:
#: Stated as the in-flight set rather than the finished one on purpose: a state
#: added later then errs towards being surveyed, and the failure mode of this
#: module is naming a branch too eagerly, not hiding one.
IN_FLIGHT = (tasks.PROPOSED, tasks.QUEUED, tasks.RUNNING, tasks.BLOCKED)


class _Failed:
    """What a git call that could not even start looks like to a caller."""
    returncode, stdout, stderr = 1, "", "git could not be run"


def _git(cwd, *args, timeout: int = 30):
    """Never raises. One unreadable or wedged repository must cost that
    repository's row, not the whole survey -- this runs behind a dashboard
    panel and inside the nightly goal, and neither may fail on it."""
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("git %s in %s: %s", args[0] if args else "?", cwd, e)
        return _Failed()


def name_for(task: dict) -> str:
    """The branch this task's commits would be on.

    Falls back to the convention when the record predates the field. Every
    task that ever ran in a worktree used `silkworm/<id>`, so the eight
    branches that prompted this are findable without a migration -- and a
    branch that is not there simply drops out of the survey.
    """
    return (task.get("branch") or "").strip() or \
        f"{worktrees.BRANCH_PREFIX}{task.get('id') or ''}"


def repo_for(task: dict) -> Path | None:
    """The checkout this task's branch lives in, if it has one.

    `scope.cwd` is where the task ran, which for a repo-backed project is the
    repository itself -- the worktree path is kept separately and is gone by
    now. A project with no repo (a trip, a kitchen) has a directory but no
    branches, so it is filtered out by asking the filesystem rather than by
    trusting `scope.repo`, which `!project` leaves empty.
    """
    scope = task.get("scope") or {}
    for candidate in (scope.get("repo"), scope.get("cwd")):
        if candidate and worktrees.is_repo(candidate):
            return Path(candidate)
    return None


def existing(repo) -> dict:
    """Every silkworm branch in this repo, as branch -> tip sha.

    One call for the whole repo. The alternative -- asking per task -- is a
    subprocess per record, and this runs behind a dashboard panel.
    """
    r = _git(repo, "for-each-ref", "--format=%(refname:short) %(objectname)",
             f"refs/heads/{worktrees.BRANCH_PREFIX}")
    if r.returncode != 0:
        log.warning("could not list branches in %s: %s", repo,
                    (r.stderr or "").strip()[-200:])
        return {}
    out = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    return out


def ahead(repo, base: str, branch: str) -> int:
    """Commits on `branch` that `base` does not have.

    This is the whole merge test. A branch already contained in the base has
    nothing ahead of it, so asking `git branch --merged` as well was a second
    way to compute the same answer -- and a second way no test could tell
    apart from the first, which is how it survived unnoticed.
    """
    r = _git(repo, "rev-list", "--count", f"{base}..{branch}")
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 0


def survey(records) -> list:
    """One row per stopped task whose branch still holds unmerged commits.

    Grouped by (repo, preferred base): the list of branches costs one call per
    group rather than one per task, and the base is resolved once. A project on
    a research branch and one on main are different groups, since "merged"
    means a different thing in each.

    Rows are newest-first: the useful list is "what did last night leave?".
    """
    groups: dict = {}
    for rec in records:
        if rec.get("state") in IN_FLIGHT:
            continue
        repo = repo_for(rec)
        if not repo:
            continue
        pref = ((rec.get("scope") or {}).get("branch") or "").strip()
        groups.setdefault((str(repo), pref), []).append(rec)

    rows = []
    for (repo, pref), group in groups.items():
        live = existing(repo)
        wanted = {name_for(r): r for r in group}
        present = {name: rec for name, rec in wanted.items() if name in live}
        if not present:
            continue
        base = worktrees.base_ref(repo, fetch=False, prefer=pref)
        shown = base_name(repo, base)
        for name, rec in present.items():
            count = ahead(repo, base, name)
            if not count:
                # Everything on it is already in the base -- it was merged, by
                # us or by hand -- or it never held anything. Either way there
                # is nothing to land and nothing to say.
                continue
            rows.append({
                "id": rec.get("id") or "",
                "title": rec.get("title") or "",
                "project": rec.get("project") or "",
                "state": rec.get("state") or "",
                "branch": name,
                "base": rec.get("base") or shown,
                "commits": count,
                "repo": repo,
                "head": live[name][:8],
                "thread": rec.get("thread") or "",
                "updated": rec.get("updated") or rec.get("created") or 0,
            })
    return sorted(rows, key=lambda r: -r["updated"])


def base_name(repo, ref: str) -> str:
    """The branch a base ref names.

    `origin/HEAD` is a symbolic ref: splitting it on "/" gives "HEAD" rather
    than the branch it points at, which has already caused one landing to be
    refused. Resolve it first, then take the last segment.
    """
    r = _git(repo, "rev-parse", "--abbrev-ref", ref)
    resolved = r.stdout.strip() if r.returncode == 0 else ""
    return (resolved or ref).split("/")[-1] or ref


def line(rows) -> str:
    """One line for `silkworm status` and the Slack-facing summary."""
    if not rows:
        return ""
    commits = sum(r["commits"] for r in rows)
    return (f"{len(rows)} finished task{'s' if len(rows) != 1 else ''} on "
            f"unmerged branch{'es' if len(rows) != 1 else ''} "
            f"({commits} commit{'s' if commits != 1 else ''})")

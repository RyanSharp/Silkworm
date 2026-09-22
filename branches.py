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


def ahead(repo, bases, branch: str) -> int:
    """Commits on `branch` that no copy of the base has.

    This is the whole merge test. A branch already contained in the base has
    nothing ahead of it, so asking `git branch --merged` as well was a second
    way to compute the same answer -- and a second way no test could tell
    apart from the first, which is how it survived unnoticed.

    `bases` is plural because a branch has two copies here, local and
    remote-tracking, and either one having the work means the work is not
    stranded. They drift in both directions: landing fast-forwards the local
    branch and does not push, and a pull request merged on the forge advances
    the remote one. Asking about a single ref got one of those two cases wrong
    whichever ref was chosen -- so ask about the commits no copy contains.
    """
    refs = (bases,) if isinstance(bases, str) else tuple(bases)
    if not refs:
        return 0
    r = _git(repo, "rev-list", "--count", branch, "--not", *refs)
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
        base, shown = base_for(repo, pref)
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
                # The base this was measured against, never the one stored on
                # the record. That field means "what it was cut from, as
                # resolved at the time", which is a different question and may
                # answer differently -- and a row that counts against one ref
                # while printing the name of another is the whole bug this
                # module had.
                "base": shown,
                "commits": count,
                "repo": repo,
                "head": live[name][:8],
                "thread": rec.get("thread") or "",
                "updated": rec.get("updated") or rec.get("created") or 0,
            })
    return sorted(rows, key=lambda r: -r["updated"])


def _full_ref(repo, ref: str) -> str:
    """Whatever `rev-parse --symbolic-full-name` makes of a ref.

    Often not a ref name at all, and both callers below are written to act only
    on the `refs/...` prefixes they recognise rather than on whatever comes
    back. It exits *zero with empty output* when a refname is ambiguous -- a
    branch and a tag sharing a name -- and returns the bare word "HEAD" when
    the checkout is detached. Treating either of those as an answer is how the
    naming below used to start guessing.
    """
    r = _git(repo, "rev-parse", "--symbolic-full-name", ref)
    return r.stdout.strip() if r.returncode == 0 else ""


def base_name(repo, ref: str) -> str:
    """The branch a base ref names.

    `origin/HEAD` is a symbolic ref: splitting it on "/" gives "HEAD" rather
    than the branch it points at, which has already caused one landing to be
    refused. So resolve it to a full ref name and strip only what qualifies
    it -- the remote it came from, or `refs/heads/`.

    Taking the last segment instead was a second bug of the same shape: a
    branch name may hold slashes, and the trader's base
    `origin/research/point-in-time-universe` was reported as `universe`, which
    names no branch anyone can look up or measure against. So when the ref
    cannot be resolved -- ambiguous, detached, a wedged repository -- it is
    named exactly as it was asked for rather than chopped into a guess. An
    unlovely name that resolves beats a tidy one that does not.
    """
    full = _full_ref(repo, ref)
    for prefix in ("refs/heads/", "refs/tags/"):
        if full.startswith(prefix):
            return full[len(prefix):]
    if full.startswith("refs/remotes/"):
        # refs/remotes/<remote>/<branch>, and a remote name holds no slash,
        # so one split takes the remote off and leaves the rest intact.
        rest = full[len("refs/remotes/"):]
        return rest.split("/", 1)[1] if "/" in rest else rest
    return ref


def base_for(repo, prefer: str = "") -> tuple[tuple, str]:
    """Every ref that is the base, and the one name they all go by.

    `worktrees.base_ref` answers a different question -- what to *cut* new work
    from -- and for that it rightly prefers `origin/main`: fresh origin is
    where a new branch should start. Asked instead what a finished branch has
    already reached, one ref is not enough, because the base has two copies and
    they drift apart in both directions.

    Landing fast-forwards the *local* branch and deliberately does not push, so
    origin falls one commit behind per landing. Measured rather than supposed:
    origin/main twelve landings behind local main, and three branches whose
    commits were every one of them on main announced as "3 finished tasks on
    unmerged branches (21 commits)" -- one of those branches being main's own
    tip. That number reaches the dashboard, `silkworm status`, and the nightly
    ideator, which was told those gaps were "fixed on those branches and not on
    the base you are reading" about code sitting on the base it was reading.

    Preferring the local copy instead would only have turned the error round:
    a branch merged on the forge lands on `origin/main` alone, and every task
    fetches origin when its worktree is made, so that case is no rarer. Both
    refs are the same branch under the same name, so the row can name it and
    measure against all of it -- and `ahead` counts what no copy contains.
    """
    ref = worktrees.base_ref(repo, fetch=False, prefer=prefer)
    name = base_name(repo, ref)
    full = _full_ref(repo, ref)
    refs = []
    local = f"refs/heads/{name}"
    if _git(repo, "rev-parse", "--verify", "--quiet", local).returncode == 0:
        refs.append(local)
    if full.startswith("refs/remotes/") and full not in refs:
        refs.append(full)
    # Nothing resolved -- a tag, a detached checkout, a repository git could
    # not be run against. The ref as asked for is then the only thing the name
    # can mean, so naming and measuring still agree.
    return tuple(refs) or (ref,), name


def line(rows) -> str:
    """One line for `silkworm status` and the Slack-facing summary."""
    if not rows:
        return ""
    commits = sum(r["commits"] for r in rows)
    return (f"{len(rows)} finished task{'s' if len(rows) != 1 else ''} on "
            f"unmerged branch{'es' if len(rows) != 1 else ''} "
            f"({commits} commit{'s' if commits != 1 else ''})")

"""Give a queued task its own checkout, so it cannot disturb yours.

A queued task used to run in the repository you work in. That meant it could
leave your working tree dirty, half-edited or on another branch, and it had to
queue behind any conversation about the same repo -- two agents in one checkout
fight over more than files, since one running `git checkout` moves the ground
under the other.

A worktree is a second working tree over the same object store: its own files
and its own branch, sharing history. So a task gets somewhere private to work,
your checkout is untouchable from it, and the two no longer serialise.

Deliberately *not* used for conversations. A worktree cannot see uncommitted
work in your main tree, so "fix the thing I'm working on" would find nothing
there. Isolation is right for self-contained work and wrong for iterating with
you -- so each task records that decision when it is made (`isolate` in
tasks.py). It is not read off `driver`: a restart hands an orphaned Slack
message to the queue runner so it is not lost, and being run by the runner must
not change where it runs.

Nothing here destroys work: a worktree holding uncommitted changes is left on
disk and reported rather than removed.
"""

import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

import discard

log = logging.getLogger("silkworm.worktrees")

#: Outside any repository, so it is never scanned, committed or walked by a
#: task that thinks it is looking at its own project.
ROOT = Path.home() / "workspace" / ".worktrees"

BRANCH_PREFIX = "silkworm/"
SEP = "--"          # repo name and task id both contain '-' and '_'


def _git(cwd, *args, timeout: int = 180):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout)


def is_repo(path) -> bool:
    """True for a repo root, including another worktree (.git is a file there)."""
    try:
        return (Path(path) / ".git").exists()
    except Exception:
        return False


def base_ref(repo, fetch: bool = True, prefer: str = "",
             fallback: str = "HEAD") -> str:
    """What to branch from: `prefer` if it exists, else the default branch.

    A project is not always working on main. The trader sits on a research
    branch eighteen commits ahead of it, and branching off main there would
    quietly drop all of that and build on the wrong baseline -- so a project
    can name its own base. Fresh main stays the default, per the usual rule
    about not stacking branches.

    Fetch is best-effort: being offline should mean branching from a slightly
    stale base, not failing to start the task at all.

    The remote-tracking ref is preferred, but only while it is actually the
    more advanced of the two -- see `_local_if_ahead`.

    `fallback` is what comes back when none of the candidates exist, and it is
    the caller's call because the two callers want opposite things. Starting a
    task from "HEAD" is a fine last resort -- somewhere to branch from beats
    not running at all. Landing onto it is not: see merge.land, which asks for
    "" so it can refuse instead of guessing.
    """
    if fetch:
        try:
            _git(repo, "fetch", "--quiet", "origin", timeout=60)
        except Exception:
            log.info("fetch failed for %s; branching from what is local", repo)
    candidates = []
    if prefer:
        candidates += [f"origin/{prefer}", prefer]
    candidates += ["origin/HEAD", "origin/main", "origin/master", "main", "master"]
    for ref in candidates:
        if _git(repo, "rev-parse", "--verify", "--quiet", ref).returncode == 0:
            return _local_if_ahead(repo, ref)
    return fallback


def _local_if_ahead(repo, ref: str) -> str:
    """`ref`, unless the local branch under it has commits the remote lacks.

    Preferring the remote is right when it is the more advanced of the two,
    which is the normal case: it is what everyone else can see. It is wrong
    when the local branch is ahead, and that happens routinely -- landing
    fast-forwards the local base, and publishing it is the project's choice, so
    a project that does not publish (or whose push was refused) has landed work
    that exists only here.

    Cutting the next task from the remote then hands it a baseline missing work
    that has already landed. Measured: origin twelve commits behind local main,
    and two tasks independently implementing the same fix because neither could
    see the other's landing.

    Strictly ahead only. If the two have diverged -- each holding commits the
    other does not -- the remote is still the shared truth, and branching from
    local would build on something nobody else has.
    """
    if not ref.startswith("origin/"):
        return ref
    # `origin/HEAD` is symbolic: it names `origin/main`, not a branch called
    # HEAD, so the local counterpart has to be read off what it resolves to.
    resolved = _git(repo, "rev-parse", "--abbrev-ref", ref).stdout.strip() or ref
    name = resolved.split("/", 1)[1] if resolved.startswith("origin/") else resolved
    if not name or name == "HEAD":
        return ref
    if _git(repo, "rev-parse", "--verify", "--quiet", name).returncode != 0:
        return ref
    if _git(repo, "merge-base", "--is-ancestor", resolved, name).returncode != 0:
        return ref        # diverged, or local is behind: the remote still wins
    if _git(repo, "rev-list", "--count",
            f"{resolved}..{name}").stdout.strip() in ("", "0"):
        return ref        # identical, so say it the usual way
    log.info("%s is ahead of %s; branching from the local branch", name, resolved)
    return name


def path_for(repo, task_id: str, label: str = "") -> Path:
    """Where a task's checkout goes. `label` says what it is for.

    The task id is always last: the sweep reads it back out of the directory
    name, so anything in front of it is decoration and anything behind it would
    be mistaken for it.
    """
    name = Path(repo).name + SEP + (label + SEP if label else "") + task_id
    return ROOT / name


#: `git worktree add` takes a repository-level lock while it writes refs, so
#: two workers starting at once make one of them fail. Creation is a second or
#: two; serialising just that costs nothing and keeps the tasks themselves
#: parallel. Observed directly: three concurrent tasks, two failed to get a
#: worktree and silently fell back to sharing the main checkout.
_create_locks: dict = {}
_create_guard = threading.Lock()


def _repo_create_lock(repo):
    with _create_guard:
        return _create_locks.setdefault(str(Path(repo).resolve()), threading.Lock())


def create(repo, task_id: str, fetch: bool = True, base: str = "") -> Path | None:
    """A fresh worktree for this task, or None if one could not be made.

    Returning None is a soft failure on purpose: the caller falls back to the
    main checkout, which is what happened before this existed.
    """
    repo = Path(repo)
    if not is_repo(repo):
        return None
    path = path_for(repo, task_id)
    if path.exists():
        return path
    ROOT.mkdir(parents=True, exist_ok=True)
    branch = f"{BRANCH_PREFIX}{task_id}"
    with _repo_create_lock(repo):
        base = base_ref(repo, fetch, prefer=base)
        r = _git(repo, "worktree", "add", "-b", branch, str(path), base)
        if r.returncode != 0:
            # `worktree add -b` creates the branch first and the working tree
            # second, so a failure at the second step leaves the branch behind
            # -- and a naive retry then fails forever with "a branch named X
            # already exists". Clear it before trying again.
            time.sleep(1.5)
            _git(repo, "worktree", "prune")
            # Through discard, because "the branch already exists" is one of
            # the reasons this step fails -- and then the branch being cleared
            # is not the half-made one, it is somebody's finished work.
            discard.drop(repo, branch)
            r = _git(repo, "worktree", "add", "-b", branch, str(path), base)
        if r.returncode != 0:
            # Still failed: do not leave a half-made branch lying around for a
            # later run to trip over.
            discard.drop(repo, branch)
    if r.returncode != 0:
        # Tail, not head: git puts "Preparing worktree..." progress on stderr,
        # so logging the first 300 characters captured that and hid the actual
        # error underneath it.
        log.warning("could not create a worktree for %s: %s",
                    repo, (r.stderr or "").strip()[-300:])
        return None
    log.info("worktree %s on %s (from %s)", path, branch, base)
    return path


def attach(repo, task_id: str, branch: str, label: str = "land") -> Path | None:
    """A checkout of an existing branch, for work that has already been done.

    Landing happens after the review, by which time the task's own worktree is
    long released -- but its branch survives. Reattaching the branch rather
    than creating one is what lets the work be rebased and retested later.

    The review needs exactly the same thing, and for a sharper reason: the work
    it has to read was committed inside a worktree that no longer exists, so
    without this it stands in the main checkout and reviews a tree the change
    never reached. `label` is only so that `git worktree list` says which of
    the two a directory is.
    """
    repo = Path(repo)
    if not is_repo(repo):
        return None
    if _git(repo, "rev-parse", "--verify", "--quiet", branch).returncode != 0:
        return None
    path = path_for(repo, task_id, label)
    if path.exists():
        return path
    ROOT.mkdir(parents=True, exist_ok=True)
    with _repo_create_lock(repo):
        r = _git(repo, "worktree", "add", str(path), branch)
    if r.returncode != 0:
        log.warning("could not attach %s for landing: %s",
                    branch, (r.stderr or "").strip()[-300:])
        return None
    return path


def main_repo(worktree) -> Path | None:
    """The checkout a worktree belongs to."""
    r = _git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if r.returncode != 0:
        return None
    common = Path(r.stdout.strip())          # .../repo/.git
    return common.parent if common.name == ".git" else None


def is_dirty(worktree) -> bool:
    r = _git(worktree, "status", "--porcelain")
    return r.returncode == 0 and bool(r.stdout.strip())


def branch_of(worktree) -> str:
    r = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else ""


def fork_point(repo, branch: str, base: str = "") -> str:
    """The commit `branch` was cut from, as a sha, or "" if it cannot be found.

    A review needs a fixed point to diff against, and the base *branch* is not
    quite one. The name has to resolve wherever the reviewer happens to be
    standing, and `origin/HEAD` is unset in plenty of repositories -- while a
    reviewer whose opening command errors is back to reading the summary it was
    asked not to trust. A sha always resolves.

    It is also the fork point rather than the base branch's tip, which matters
    because the base moves while a task works: `main..HEAD` read from the main
    checkout would credit this task with other people's commits.
    """
    ref = base_ref(repo, fetch=False, prefer=base)
    r = _git(repo, "merge-base", ref, branch)
    return r.stdout.strip() if r.returncode == 0 else ""


def commits_on(worktree, base: str = "") -> int:
    """How many commits the task actually made.

    The base has to be resolved rather than assumed: `origin/HEAD` is unset in
    plenty of repositories, and rev-list against a ref that does not exist
    fails silently into "no commits" -- which reads as "the task did nothing".
    """
    ref = base
    if not ref:
        repo = main_repo(worktree)
        ref = base_ref(repo, fetch=False) if repo else "HEAD"
    r = _git(worktree, "rev-list", "--count", f"{ref}..HEAD")
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 0


def release(worktree, delete_empty_branch: bool = True) -> tuple[bool, str]:
    """Tidy up after a task. Returns (removed, note for the reply).

    Uncommitted changes are never discarded -- the worktree is left where it is
    and named, because a task's unfinished work is still work.

    `delete_empty_branch` is what the caller knows about whose branch this is.
    The task's own executor knows the run is over and may tidy an empty branch
    away; the sweeper is guessing from a directory name and must not.
    """
    path = Path(worktree)
    if not path.exists():
        return True, ""
    branch = branch_of(path)
    if is_dirty(path):
        return False, (f"left in place with uncommitted changes: `{path}` "
                       f"(branch `{branch}`)")
    repo = main_repo(path)
    made = commits_on(path, base_ref(repo, fetch=False) if repo else "")
    if repo:
        r = _git(repo, "worktree", "remove", str(path))
        if r.returncode != 0:
            log.warning("worktree remove failed: %s", (r.stderr or "").strip()[:200])
            return False, f"could not be removed: `{path}`"
    else:
        shutil.rmtree(path, ignore_errors=True)
    if made:
        return True, f"branch `{branch}` ({made} commit{'s' if made != 1 else ''})"
    # A branch with nothing on it is not work, it is litter. Left alone they
    # accumulate one per run and make `git branch` useless. Two separate
    # things have to hold before one goes. The caller must be entitled to say
    # so -- the task's own executor knows the run is over, while the sweeper is
    # guessing from a directory name and must not. And discard checks that
    # claim against the refs rather than trusting the commit count above it:
    # `made` is counted from a base that has to be resolved, and a base that
    # resolves to nothing has read as "the task did nothing" before now.
    if delete_empty_branch and repo and branch.startswith(BRANCH_PREFIX):
        discard.drop(repo, branch)
    return True, ""


#: Nothing is swept until it has been sitting around this long. A genuinely
#: orphaned worktree is never urgent -- it costs some disk until the next pass
#: -- while a young one is very likely still being worked in. The sweep runs on
#: a guess (a task id parsed out of a directory name), and an hour of patience
#: is what stops that guess from costing anyone their afternoon.
MIN_AGE_S = 3600


def age_s(path) -> float:
    """How long ago this worktree was made, by the most recent evidence.

    Deliberately optimistic: whichever of the timestamps looks newest wins, so
    a tree the filesystem is unsure about is treated as young and left alone.
    """
    try:
        st = Path(path).stat()
    except OSError:
        return 0.0
    newest = max(st.st_mtime, st.st_ctime, getattr(st, "st_birthtime", 0))
    return max(0.0, time.time() - newest)


def sweep(keep: set, min_age_s: float = MIN_AGE_S) -> int:
    """Remove worktrees no live task owns. Never touches a dirty one.

    A restart orphans whatever was running, and an orphaned worktree is
    invisible: it costs disk and clutters `git worktree list` while looking
    like nothing at all.

    Three things hold the sweep off, because it is guessing and the cost of
    guessing wrong is somebody's uncommitted afternoon: `keep` (the caller
    knows a task still owns this), a dirty tree, and youth. And it never
    deletes the branch -- an agent that has just committed and is running a
    long test suite has a clean tree for minutes at a time, which is exactly
    when losing both the checkout and the branch costs the most.
    """
    if not ROOT.exists():
        return 0
    removed = 0
    for path in ROOT.iterdir():
        if not path.is_dir() or SEP not in path.name:
            continue
        # From the right. A labelled checkout is `<repo>--land--<id>` or
        # `<repo>--review--<id>`, and splitting from the front read the task id
        # as "land--<id>" -- which is in nobody's keep set, so a landing or a
        # review in progress became sweepable the moment it was an hour old.
        task_id = path.name.rsplit(SEP, 1)[1]
        if task_id in keep:
            continue
        if age_s(path) < min_age_s:
            continue
        if is_dirty(path):
            log.info("leaving orphaned worktree with uncommitted work: %s", path)
            continue
        ok, _ = release(path, delete_empty_branch=False)
        removed += int(ok)
        if ok:
            log.info("swept orphaned worktree %s", path)
    return removed

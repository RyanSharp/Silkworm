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

Nothing here merges or pushes anything -- see DESIGN.md on why opening pull
requests is deliberately out of scope. This mostly answers the question the
system could not previously answer: *what is finished and not in the base?* The
one thing it deletes is the other half of that answer -- a finished task's
branch that is already entirely in the base -- see `prune_merged`.

Merge state is asked of git rather than stored, because it changes without us:
you land a branch by hand and a stored flag would still say unmerged. What is
stored on the task is only what git cannot recover later -- the branch it used
and the base it was cut from.

Deliberately local: no fetch. A dashboard panel must not wait on the network,
and the cost of being slightly behind is naming a branch that someone else
already merged, which is a great deal better than staying silent about one
nobody did. Remote-tracking refs are read all the same -- they are on disk,
and a branch that was pushed and then lost its local copy is the case where
the commits survive and the row does not.
"""

import logging
import subprocess
from pathlib import Path

import discard
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


def _remotes(repo) -> list:
    """The remotes configured here, or none if git cannot be asked.

    Asked rather than pattern-matched out of the ref paths: `git remote add
    my/remote <url>` is accepted, so the first path segment under
    `refs/remotes/` is not reliably the remote's name.
    """
    r = _git(repo, "remote")
    return r.stdout.split() if r.returncode == 0 else []


#: The two places a branch can be, spelled once. A remote-tracking ref is a
#: copy of somebody's branch and not a branch you can check out, but it is
#: every bit as much a place finished commits are sitting.
_HEADS, _REMOTES = "refs/heads/", "refs/remotes/"


def existing(repo) -> dict:
    """Every silkworm branch in this repo, wherever its copies are.

    Keyed by branch name, so `silkworm/tsk_x` is one entry whether it is local,
    on a remote, or both. Each entry carries every ref that holds it, its tip,
    and whether a local branch of that name exists at all.

    Remotes are included because a branch with no local ref was invisible
    here, and this module's whole failure mode is meant to be naming a branch
    too eagerly rather than hiding one. A local ref is not permanent: a
    worktree release can recreate it from a stale base, a sweep or a hand can
    delete it, and branches in this repo have been observed reset to a
    pre-work commit between turns. Push first and any of those leaves the
    commits safe on origin and the branch gone from the survey --
    `origin/silkworm/tsk_e37a60256d`, one commit not in main, named by the
    dashboard, by `silkworm status` and by the nightly note in none of the
    three.

    Still one call for the whole repo. The alternative -- asking per task -- is
    a subprocess per record, and this runs behind a dashboard panel.
    """
    # One literal prefix per remote, rather than one `refs/remotes/*/...`
    # glob. Measured: for-each-ref matches a glob with WM_PATHNAME, so `*` is
    # a single path segment -- `origin/silkworm/a/b` and any remote whose own
    # name holds a slash both fell outside it, while the local pattern is a
    # literal prefix and matches however deep the name goes. That asymmetry
    # is a half-blind survey of exactly the kind this module is here to stop.
    # Longest first, so a remote called `up` cannot claim a ref belonging to
    # one called `up/stream`.
    known = sorted(_remotes(repo), key=len, reverse=True)
    pats = [f"{_HEADS}{worktrees.BRANCH_PREFIX}"]
    pats += [f"{_REMOTES}{name}/{worktrees.BRANCH_PREFIX}" for name in known]
    # And a one-segment glob besides, for refs left under `refs/remotes/` by a
    # remote that is no longer configured. Those hold commits too, and the rule
    # here is never to hide one; the name of a remote that does not exist can
    # only be guessed at, so it is guessed at from the path.
    pats.append(f"{_REMOTES}*/{worktrees.BRANCH_PREFIX}*")
    r = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)", *pats)
    if r.returncode != 0:
        log.warning("could not list branches in %s: %s", repo,
                    (r.stderr or "").strip()[-200:])
        return {}
    out: dict = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        ref, sha = parts
        if ref.startswith(_HEADS):
            name, remote = ref[len(_HEADS):], ""
        else:
            # The remote is taken from the pattern that asked for the ref
            # rather than guessed from the path, since `git remote add
            # my/remote <url>` is accepted and a partition on the first
            # slash would file its branches under "my".
            remote = next((n for n in known
                           if ref.startswith(f"{_REMOTES}{n}/")), "")
            if remote:
                name = ref[len(_REMOTES) + len(remote) + 1:]
            else:
                # No configured remote owns it. One segment is then the only
                # reading available, and a leftover is better named roughly
                # than dropped.
                remote, _, name = ref[len(_REMOTES):].partition("/")
                if not remote or not name:
                    continue
        if not name.startswith(worktrees.BRANCH_PREFIX):
            continue
        e = out.setdefault(name, {"refs": [], "sha": "", "remote": "",
                                  "local": False})
        e["refs"].append(ref)
        if remote:
            # Several remotes can hold the same branch, and the row shows one
            # name. Prefer origin, which is the one everything else here means
            # by "the remote"; otherwise whichever git listed first.
            if not e["remote"] or remote == "origin":
                e["remote"] = remote
        else:
            # The local copy is the one a landing would use, so it names the
            # tip when there is one; a remote-only branch has only the other.
            e["local"], e["sha"] = True, sha
        if not e["sha"]:
            e["sha"] = sha
    for e in out.values():
        e["refs"] = tuple(e["refs"])
    return out


def retired(repo) -> set:
    """Every (branch, short sha) a human deliberately threw away.

    `discard.py` tags a branch tip before deleting the branch, so that the work
    survives gc; the tag is named `discarded/<date>/<branch>-<short sha>`. That
    tag is the record of a decision -- this tip is not wanted -- and the survey
    has to honour it, because the local branch being gone is no longer enough
    to keep it quiet.

    It was exactly enough before this module read remotes, and stopped being so
    the moment it did. `git branch -D` removes `refs/heads/` only, the survey
    deliberately never pushes, and `worktrees.base_ref` fetches without
    `--prune` -- so `refs/remotes/origin/silkworm/<id>` is recreated on every
    fetch for as long as the branch is on origin. Without this, discarding a
    branch that had ever been pushed would put it back in the panel, back in
    `silkworm status`, and back into every nightly prompt for that project, for
    ever, with nothing in the product able to clear it. Measured: the one
    remote-only branch in this checkout, `origin/silkworm/tsk_e37a60256d`, is
    the tip of `discarded/2026-09-12/silkworm/tsk_e37a60256d-e3541be`.

    Matched on branch *and* tip, not on either alone: the same branch name gets
    reused by a re-run -- four of the fifteen names in the 2026-09-12 reset
    appear twice with divergent tips -- and a tip that later gained commits is
    not the tip anyone retired.

    A repository that cannot be asked yields an empty set and so hides nothing,
    which is the direction this module errs in everywhere else.
    """
    r = _git(repo, "for-each-ref", "--format=%(refname:short)",
             f"refs/tags/{discard.NAMESPACE}/")
    if r.returncode != 0:
        log.warning("could not list discarded tags in %s: %s", repo,
                    (r.stderr or "").strip()[-200:])
        return set()
    out = set()
    for name in r.stdout.split():
        rest = name[len(discard.NAMESPACE) + 1:]
        # discarded/<date>/<branch>-<sha7>: the date is one segment, the
        # branch may hold slashes and hyphens, and the sha is always last.
        rest = rest.split("/", 1)[1] if "/" in rest else rest
        branch, _, sha7 = rest.rpartition("-")
        if branch and sha7:
            out.add((branch, sha7))
    return out


def exists(repo, branch: str) -> bool:
    """Whether `branch` is a local branch in `repo`."""
    return _git(repo, "rev-parse", "--verify", "--quiet",
                f"refs/heads/{branch}").returncode == 0


def nothing_to_land(repo, branch: str, base: str) -> str:
    """Why a task's branch has nothing to land, or "" if it has something.

    A branch the executor deleted as empty, or one whose every commit the base
    already holds, is a task that found its job done -- not a landing git
    refused. Answered before a landing goes looking for a checkout, because the
    absence of a branch used to reach the user as "not landed (attach)", a
    refusal asking for a person, seven times in one day.

    Every copy of the branch counts, as it does in `survey`. Asking about the
    local ref alone meant a branch reset to the base while origin still held
    its commits showed N commits and a Land button on the unmerged panel, and
    Land then answered "everything on its branch is already on the base" --
    the panel and the button disagreeing about the same branch. A copy a human
    discarded on purpose (`retired`) is not work waiting, here as there.

    An unresolvable base says nothing either way: "" lets the landing run and
    refuse with its own named stage rather than be waved through here.
    """
    found = existing(repo).get(branch)
    gone = retired(repo)
    refs = [ref for ref in (found or {}).get("refs", ())
            if ref.startswith(_HEADS) or (branch, _tip(repo, ref)[:7]) not in gone]
    if not refs:
        return "its branch is gone: it made no commits the base did not already have"
    if not base:
        return ""
    bases = _both(base)
    counts = [ahead(repo, bases, ref) if ref.startswith(_HEADS)
              else _unlanded(repo, bases, ref) for ref in refs]
    if all(n == 0 for n in counts):
        return "everything on its branch is already on the base"
    return ""


def _both(base: str) -> list:
    """The base and its local name: either copy holding the work is enough."""
    return [base, base.split("/", 1)[1] if base.startswith("origin/") else base]


def _tip(repo, ref: str) -> str:
    r = _git(repo, "rev-parse", "--verify", "--quiet", ref)
    return r.stdout.strip() if r.returncode == 0 else ""


def _unlanded(repo, bases, ref: str) -> int | None:
    """Commits on a remote copy that no base holds, even as a rebased copy.

    A remote copy is usually a push from before the landing rebased the
    branch: its commits are in the base under other hashes, and `ahead`, which
    compares hashes, counts every one of them. The local branch is then
    deleted as landed, and the stale copy alone would make Land recreate the
    branch from it and land the same work a second time -- a rebase that
    either empties out into a "landed" record for nothing, or conflicts with
    the very change it already is. Patch-equivalence is the question that
    tells those commits apart from real work. None when git would not say.
    """
    counts = []
    for b in bases:
        r = _git(repo, "rev-list", "--count", "--cherry-pick", "--right-only",
                 "--no-merges", f"{b}...{ref}")
        if r.returncode != 0:
            continue                  # a base copy that does not exist says nothing
        try:
            counts.append(int(r.stdout.strip()))
        except ValueError:
            return None
    return min(counts) if counts else None


def restore_from_remote(repo, branch: str, base: str) -> str:
    """Point the local branch at a remote copy's work when it has none of its own.

    `nothing_to_land` counts every copy of a branch, so a local ref reset to
    the base -- or deleted -- while origin still holds the commits is work to
    land. But a landing checks out the *local* branch, and that one is empty:
    it would rebase nothing, test nothing new and report a clean landing of
    nothing. So before the landing attaches, the local ref is moved to the
    remote copy that holds the work.

    Only when the local copy has nothing of its own. A local branch with
    commits the base lacks is the work, and is landed as it always was; a
    stale remote copy beside it -- pushed before a rebase, say -- is ignored
    rather than allowed to replace it. Moving an empty local ref loses nothing:
    every commit on it is already in the base.

    Returns "" when the local branch is ready to land (moved or already
    right), else why it could not be made so -- which needs a person, since
    the commits are there and git would not hand them over.
    """
    found = existing(repo).get(branch)
    if not found:
        return ""
    bases = _both(base) if base else []
    local = f"{_HEADS}{branch}"
    if found["local"]:
        mine = ahead(repo, bases, local) if bases else None
        if mine != 0:
            return ""                 # its own work, or git would not say: leave it
    gone = retired(repo)
    tips = {}
    for ref in found["refs"]:
        if ref == local:
            continue
        sha = _tip(repo, ref)
        if not sha or (branch, sha[:7]) in gone:
            continue
        if bases:
            n = _unlanded(repo, bases, ref)
            if n is None:
                return f"could not tell whether {ref} holds work the base lacks"
            if n == 0:
                continue              # already landed, if under other hashes
        tips.setdefault(sha, ref)
    if not tips:
        return ""
    # Several remotes may hold it. One tip that contains every other is the
    # whole of the work; anything else is copies that disagree, and choosing
    # between them is not this function's call.
    best = next((sha for sha in tips
                 if all(_git(repo, "merge-base", "--is-ancestor", other, sha).returncode == 0
                        for other in tips)), "")
    if not best:
        return (f"its remote copies disagree ({', '.join(sorted(tips.values()))}) "
                "and the local branch has none of their work")
    # `git branch -f` refuses a branch checked out in any worktree, which is
    # the refusal wanted: moving a ref under someone's checkout would turn
    # their clean tree into a pile of apparent edits.
    r = _git(repo, "branch", "--no-track", *(["-f"] if found["local"] else []),
             branch, best)
    if r.returncode != 0:
        return (f"could not point {branch} at {tips[best]}: "
                + (r.stderr or "").strip()[-200:])
    log.info("restored %s from %s (%s) before landing", branch, tips[best], best[:8])
    return ""


def ahead(repo, bases, branch) -> int | None:
    """Commits on `branch` that no copy of the base has, or None if git would
    not say.

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

    `branch` is plural for the same reason and about the same two copies: a
    local ref reset to the base while origin still holds the commits is work
    that is sitting there, so the count is of everything on *any* copy of the
    branch and no copy of the base.

    None rather than a number when the call fails, because zero is the one
    answer that must never be invented here. `_git` never raises -- it returns
    a stub with empty stdout when the subprocess cannot run -- so a timeout,
    a wedged index or a missing git used to parse as "0 commits ahead", which
    `survey` reads as merged and drops. The one outcome this module exists to
    prevent, produced by a stopwatch. A caller that cannot show "unknown"
    should show the row anyway; it must not show nothing.
    """
    refs = (bases,) if isinstance(bases, str) else tuple(bases)
    tips = (branch,) if isinstance(branch, str) else tuple(branch)
    if not refs or not tips:
        return 0
    r = _git(repo, "rev-list", "--count", *tips, "--not", *refs)
    if r.returncode != 0:
        log.warning("could not count %s against %s in %s: %s", tips, refs,
                    repo, (r.stderr or "").strip()[-200:])
        return None
    try:
        return int(r.stdout.strip())
    except ValueError:
        log.warning("unreadable commit count for %s in %s: %r", tips, repo,
                    r.stdout[:80])
        return None


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
        gone = retired(repo)
        for name, rec in present.items():
            found = live[name]
            if (name, found["sha"][:7]) in gone:
                # Discarded on purpose, and its tip kept by a tag rather than
                # by a branch. Reading remotes brought these back from the
                # dead: nothing prunes the remote-tracking copy, so without
                # this the board could never reach empty again.
                continue
            count = ahead(repo, base, found["refs"])
            if count == 0:
                # Everything on it is already in the base -- it was merged, by
                # us or by hand -- or it never held anything. Either way there
                # is nothing to land and nothing to say.
                #
                # Compared against zero rather than tested for truth, because
                # `ahead` also answers None: git was asked and would not say.
                # Reading that as "merged" is how a timeout used to delete a
                # branch from the one list that names it, so an unknown count
                # keeps its row and travels as None for the callers to show.
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
                "head": found["sha"][:8],
                # Where the branch actually is. A row with no local copy is
                # still work sitting unmerged, but it is not a branch anyone
                # can check out by that name, and saying so is the difference
                # between a useful row and a confusing one.
                "local": found["local"],
                "remote": found["remote"],
                "thread": rec.get("thread") or "",
                "updated": rec.get("updated") or rec.get("created") or 0,
            })
    return sorted(rows, key=lambda r: -r["updated"])


def contained(repo, bases, branch: str) -> bool:
    """True only when git confirms every commit on `branch` is in some base.

    Not `ahead(...) == 0`. `ahead` now answers None rather than zero when git
    fails, but a deleter should not rest on a caller remembering to tell those
    apart: this asks a yes/no question whose only "yes" is git saying so.
    """
    refs = (bases,) if isinstance(bases, str) else tuple(bases)
    return any(_git(repo, "merge-base", "--is-ancestor", branch, ref).returncode == 0
               for ref in refs)


def prune_merged(records, skip=()) -> list:
    """Delete the branches of finished tasks that are already in the base.

    Landing removes its own branch: once it is in the base, the release
    after it counts no commits and drops it. Nothing removed a branch that
    reached the base any other way -- landed by hand, or by landing code from
    before it could tell a local base was ahead of origin -- so twenty-eight
    of them sat in this repository fully merged, burying the handful in
    `git branch` that still hold anything.

    Only `done` and `cancelled` tasks, which cannot move again. That is not
    quite the same as nobody coming back for the branch: Approve moves a task
    to `done` and *then* lands it, on another thread, for as long as the suite
    takes twice. `skip` is the caller's list of those -- a branch that goes in
    the middle of a landing whose post-merge tests then fail and reset the
    base is left with nothing holding it at all. A branch still checked out
    somewhere is refused by git itself. And the delete goes through
    `discard.drop`, so if the containment check were ever wrong the tip is
    tagged before it goes rather than lost. Returns the branches deleted.
    """
    groups: dict = {}
    for rec in records:
        if rec.get("state") not in tasks.TERMINAL or rec.get("id") in skip:
            continue
        repo = repo_for(rec)
        if not repo:
            continue
        pref = ((rec.get("scope") or {}).get("branch") or "").strip()
        groups.setdefault((str(repo), pref), []).append(rec)

    pruned = []
    for (repo, pref), group in groups.items():
        # One wedged repository costs its own branches, not every repository
        # after it on every pass: discard's git calls can raise.
        try:
            pruned += _prune_repo(repo, pref, group)
        except Exception:
            log.exception("could not prune merged branches in %s", repo)
    return pruned


def _prune_repo(repo, pref: str, group) -> list:
    live = existing(repo)
    # Local branches only. `existing` also reports copies that live solely on
    # a remote, which is right for a survey -- they are work sitting unmerged
    # -- and meaningless here: `discard.drop` deletes `refs/heads/`, and this
    # module never touches a remote.
    names = [n for n in {name_for(r) for r in group}
             if n in live and live[n]["local"]]
    if not names:
        return []
    base, _ = base_for(repo, pref)
    pruned = []
    for name in names:
        if not contained(repo, base, name):
            continue
        deleted, tag, msg = discard.drop(repo, name)
        if deleted:
            pruned.append(name)
            log.info("pruned merged branch %s in %s%s", name, repo,
                     f" (kept as {tag})" if tag else "")
        else:
            log.info("left merged branch %s in %s: %s", name, repo, msg)
    return pruned


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
    """One line for `silkworm status` and the Slack-facing summary.

    A row whose count is None is counted as a branch and not as commits: git
    refused to measure it, and quietly folding that in as a zero would make the
    summary agree with the bug it is reporting.
    """
    if not rows:
        return ""
    counted = [r.get("commits") for r in rows if r.get("commits") is not None]
    unknown = len(rows) - len(counted)
    size = []
    if counted or not unknown:
        total = sum(counted)
        size.append(f"{total} commit{'s' if total != 1 else ''}")
    if unknown:
        size.append(f"{unknown} unmeasured")
    return (f"{len(rows)} finished task{'s' if len(rows) != 1 else ''} on "
            f"unmerged branch{'es' if len(rows) != 1 else ''} "
            f"({', '.join(size)})")

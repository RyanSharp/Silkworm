"""Delete a branch without losing what was on it.

Fifteen branches were deleted on 2026-09-12 when the backlog was reset. What
survived was a file of their commit ids and a line saying they were "also in
the reflog for ~90 days". They were not. Deleting a branch deletes its reflog,
and these tips had never been HEAD in the main checkout -- the work happened in
worktrees, and a worktree's reflog goes with the worktree. Fourteen of the
fifteen were reachable from no ref at all, so git's own auto-gc would have
collected them from around 2026-09-24, silently, during some unrelated command.

Three proposals on the board still say to read those diffs before writing any
code, and two of the commits are reviewed work that exists nowhere else. All of
it was paid for once.

The lesson is narrow: writing a sha down does not keep a commit, only a ref
does. So deleting a branch that carries work tags the tip first, and the note
points at the tag rather than at a number. If the tag cannot be made, the
branch is not deleted.
"""

import logging
import subprocess
from datetime import date
from pathlib import Path

log = logging.getLogger("silkworm.discard")

#: One namespace, so `git tag -l 'discarded/*'` finds every rescue ever made.
NAMESPACE = "discarded"

#: Where the human-readable note goes. Ignored by git: it describes this
#: checkout's own history, and the tags are the durable half.
NOTE_DIR = ".discarded"


def _git(repo, *args, timeout: int = 60):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, timeout=timeout)


def tip(repo, branch: str) -> str:
    """The full sha a branch points at, or "" if there is no such branch."""
    r = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    return r.stdout.strip() if r.returncode == 0 else ""


def remote_tips(repo, branch: str) -> dict:
    """{remote-tracking ref: sha} for every remote copy of `branch`.

    A branch can outlive its local ref on a remote: pushed for a CI-gated
    landing, then deleted here by a drop or a sweep. `git fetch` without
    `--prune` recreates `refs/remotes/<remote>/<branch>` for as long as the
    remote has it, and the survey reads those -- so a drop that only knew
    `refs/heads/` answered "no branch" for exactly the copy still listed.
    """
    r = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes/")
    if r.returncode != 0:
        return {}
    out = {}
    for line in r.stdout.splitlines():
        ref, _, sha = line.partition(" ")
        # refs/remotes/<remote>/<branch>: a remote's name is one segment.
        rest = ref[len("refs/remotes/"):].split("/", 1)
        if len(rest) == 2 and rest[1] == branch and sha:
            out[ref] = sha.strip()
    return out


def held_elsewhere(repo, sha: str, ignore: str = "") -> list:
    """Refs other than `ignore` that already keep `sha` alive.

    This is the whole test for whether deleting something costs anything. A
    task branch that made no commits points at the base it was cut from, which
    origin/main holds anyway -- deleting that is tidying. A branch whose tip
    nothing else contains is the only thing standing between the work and the
    next gc.
    """
    r = _git(repo, "for-each-ref", "--contains", sha, "--format=%(refname)")
    if r.returncode != 0:
        return []
    skip = {f"refs/heads/{ignore}"} if ignore else set()
    # The branch's own remote-tracking copy does not count either. It is the
    # same branch, gone the moment a `fetch --prune` sees origin drop it, so
    # it is no guarantee against gc -- and the tag is also the record that
    # this tip was thrown away on purpose. `branches.retired` reads it to keep
    # a pushed branch's remote copy, which nothing here deletes, from coming
    # straight back as unmerged work; skipping the tag because origin had the
    # same tip left exactly those branches impossible to clear.
    same = f"/{ignore}" if ignore else None
    return [ref for ref in r.stdout.split() if ref not in skip
            and not (same and ref.startswith("refs/remotes/")
                     and ref.endswith(same))]


def _branch_holders(refs) -> list:
    """Of the refs holding a tip, the branches -- not tags.

    For a tip that also has a remote copy, the discarded/ tag is not only a
    rescue but the record that it was thrown away (`branches.retired`), and
    any other tag holding it is no such record. Measured: trader's
    `origin/silkworm/tsk_6e9eb16020` was dropped, its tip was already held by
    `recovery/tsk_6e9eb16020-50a6598`, so no discarded/ tag was written -- and
    the remote copy stayed on the unmerged survey with nothing able to clear
    it. A branch holding it (main, say) means it is not unmerged work at all.
    """
    # Nor another task's branch: one cut from this tip (or a rename of it)
    # is itself unmerged work, not evidence this tip reached anywhere.
    return [ref for ref in refs if not ref.startswith("refs/tags/")
            and "/silkworm/" not in ref]


def tag_for(branch: str, sha: str, when: str = "") -> str:
    """`discarded/<date>/<branch>-<short sha>`.

    The short sha is always there, not only when it is needed. Four of the
    fifteen branch names in the 2026-09-12 reset appear twice with divergent
    tips -- a task id reused its branch on a re-run -- so name-only tags would
    have collided and kept eight commits instead of twelve. A rule with an
    exception in it is the wrong rule for something read under pressure.
    """
    return f"{NAMESPACE}/{when or date.today().isoformat()}/{branch}-{sha[:7]}"


def preserve(repo, branch: str, sha: str = "", when: str = "",
             retire: bool = False) -> str:
    """Tag a branch tip so that deleting the branch cannot lose it.

    `retire`: the tip also has a remote copy, which nothing here deletes, so
    the tag is the record that keeps that copy off the survey; only another
    *branch* holding the tip then makes it unnecessary (`_branch_holders`).

    Returns the tag name, "" if nothing needed keeping, or raises if the tag
    could not be written -- the caller must not delete on a raise.
    """
    sha = sha or tip(repo, branch)
    if not sha:
        raise ValueError(f"no branch {branch} in {repo}")
    held = held_elsewhere(repo, sha, ignore=branch)
    if retire:
        held = _branch_holders(held)
    if held:
        return ""
    name = tag_for(branch, sha, when)
    if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/tags/{name}").returncode == 0:
        return name
    subj = _git(repo, "log", "-1", "--format=%s", sha).stdout.strip()
    made = _git(repo, "log", "-1", "--format=%ad", "--date=short", sha).stdout.strip()
    message = (f"{subj}\n\n"
               f"Tip of {branch}, committed {made}, deleted "
               f"{when or date.today().isoformat()}.\n\n"
               f"This tag is the only thing keeping it reachable: the branch is "
               f"gone and\nits reflog went with it.\n\n"
               f"Recover with: git checkout -b {branch} {name}")
    r = _git(repo, "tag", "-a", name, sha, "-m", message)
    if r.returncode != 0:
        raise RuntimeError(f"could not tag {branch}: {(r.stderr or '').strip()[-200:]}")
    return name


def drop(repo, branch: str, when: str = "", remote: bool = True) -> tuple:
    """Preserve then delete. Returns (deleted, tag, note).

    The order is the point. Tagging after the delete is a race with gc and a
    race with a crash; tagging first means the worst case is a tag for a branch
    that is still there, which is noise rather than loss.

    A branch that also -- or only -- has a remote copy is handled too. The
    remote copy is not deleted (nothing on a remote is a drop's to delete),
    but each remote tip is tagged as discarded, which is what keeps it off
    the unmerged survey (`branches.retired`). Remote-only, that tag is the
    whole of the drop, and `deleted` is True once it is written. The tag
    returned is the local tip's when there is one, else the first remote's.

    `remote=False` leaves remote copies out of it entirely: the local branch
    alone is tagged (as before) and deleted. For the merged-branch prune,
    which decides on the local branch only -- a local ref reset to the base
    while origin still holds work is contained, and retiring origin's copy
    there would hide that work from the survey with nobody deciding to.
    """
    sha = tip(repo, branch)
    remotes = remote_tips(repo, branch) if remote else {}
    if not sha and not remotes:
        return False, "", f"no branch {branch}"
    tags = []
    try:
        if sha:
            tags.append(preserve(repo, branch, sha, when, retire=bool(remotes)))
        for rsha in dict.fromkeys(remotes.values()):
            if rsha != sha:
                tags.append(preserve(repo, branch, rsha, when, retire=True))
    except Exception as exc:                     # noqa: BLE001 - never delete blind
        log.warning("not deleting %s: could not preserve it: %s", branch, exc)
        return False, "", f"kept {branch}: {exc}"
    tag = next((t for t in tags if t), "")
    where = ", ".join(sorted(r[len("refs/remotes/"):] for r in remotes))
    if not sha:
        return True, tag, (f"{where} -> {tag}; still on the remote, marked discarded"
                           if tag else f"{where} (nothing on it that a branch did not "
                                       "already hold)")
    r = _git(repo, "branch", "-D", branch)
    if r.returncode != 0:
        return False, tag, f"could not delete {branch}: {(r.stderr or '').strip()[-200:]}"
    return True, tag, (f"{branch} -> {tag}" if tag
                       else f"{branch} (nothing on it that a ref did not already hold)")


def note(repo, dropped, when: str = "") -> Path:
    """Write the recovery note, describing the tags rather than bare shas."""
    when = when or date.today().isoformat()
    out = Path(repo) / NOTE_DIR / f"{when}-reset.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Branches discarded {when}.",
             "#",
             "# Each one's tip is kept by the annotated tag beside it. A tag is a ref,",
             "# so gc will not collect these however long they sit. Do not rely on the",
             "# reflog: deleting a branch deletes its reflog, and work done in a",
             "# worktree leaves nothing in this checkout's reflog at all.",
             "#",
             "# Recover with: git checkout -b <branch> <tag>",
             "# List them with: git tag -l 'discarded/*'",
             ""]
    for branch, sha, tag in dropped:
        lines.append(f"{sha} {branch} {tag or '(already held by another ref)'}")
    out.write_text("\n".join(lines) + "\n")
    return out


def reset(repo, branches, when: str = "") -> list:
    """Discard a set of branches safely and write the note. Returns the notes."""
    when = when or date.today().isoformat()
    dropped, notes = [], []
    for branch in branches:
        sha = tip(repo, branch)
        deleted, tag, msg = drop(repo, branch, when)
        notes.append(msg)
        if deleted:
            dropped.append((branch, sha, tag))
    if dropped:
        note(repo, dropped, when)
    return notes

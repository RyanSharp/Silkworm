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
    return [ref for ref in r.stdout.split() if ref not in skip]


def tag_for(branch: str, sha: str, when: str = "") -> str:
    """`discarded/<date>/<branch>-<short sha>`.

    The short sha is always there, not only when it is needed. Four of the
    fifteen branch names in the 2026-09-12 reset appear twice with divergent
    tips -- a task id reused its branch on a re-run -- so name-only tags would
    have collided and kept eight commits instead of twelve. A rule with an
    exception in it is the wrong rule for something read under pressure.
    """
    return f"{NAMESPACE}/{when or date.today().isoformat()}/{branch}-{sha[:7]}"


def preserve(repo, branch: str, sha: str = "", when: str = "") -> str:
    """Tag a branch tip so that deleting the branch cannot lose it.

    Returns the tag name, "" if nothing needed keeping, or raises if the tag
    could not be written -- the caller must not delete on a raise.
    """
    sha = sha or tip(repo, branch)
    if not sha:
        raise ValueError(f"no branch {branch} in {repo}")
    if held_elsewhere(repo, sha, ignore=branch):
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


def drop(repo, branch: str, when: str = "") -> tuple:
    """Preserve then delete. Returns (deleted, tag, note).

    The order is the point. Tagging after the delete is a race with gc and a
    race with a crash; tagging first means the worst case is a tag for a branch
    that is still there, which is noise rather than loss.
    """
    sha = tip(repo, branch)
    if not sha:
        return False, "", f"no branch {branch}"
    try:
        tag = preserve(repo, branch, sha, when)
    except Exception as exc:                     # noqa: BLE001 - never delete blind
        log.warning("not deleting %s: could not preserve it: %s", branch, exc)
        return False, "", f"kept {branch}: {exc}"
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

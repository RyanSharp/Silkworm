"""Say out loud when a checkout is still holding work nobody has looked at.

worktrees.py will not destroy uncommitted work, and that is right: `sweep()`
skips a dirty tree and `release()` leaves it on disk rather than removing it.
Neither of them tells anybody. release()'s note goes into one Slack reply that
scrolls away, and sweep() logged a line at INFO -- 508 of them over six days,
for two trader checkouts, every half hour, to nobody.

This is branches.py's argument one level down. There it was: work that finished
and never reached the base is invisible because the only record was a Slack
line. Here it is: work that never even reached a commit is invisible for the
same reason, and it is in a worse place -- a branch survives a disk tidy and an
untracked file in a directory outside any repository does not.

Two shapes of held checkout, and today both end badly. A task still in
awaiting_approval or needs_input is in the sweeper's keep set, so its tree is
skipped before the dirty check is even reached: held indefinitely, in complete
silence. A task that has finished drops out of the keep set, reaches the dirty
check, and is logged for ever. Correctly never deleted either way, and never
surfaced either way.

Nothing here deletes, commits or moves anything. Its whole job is to answer
"which checkouts are holding work, whose, and where" -- for `silkworm status`,
for the dashboard, and for the one moment it matters most: approving or
dismissing a task is when its checkout stops being anybody's business.

Asked of git rather than stored, for branches.py's reason: you can go and
commit or delete those files yourself, and a stored flag would still say they
were there.
"""

import logging
import subprocess
from pathlib import Path

import tasks
import worktrees

log = logging.getLogger("silkworm.holding")

#: How many changed paths a row carries. Enough to tell a checkout holding a
#: script somebody wrote from one holding a virtualenv, which is the judgement
#: a reader has to make and the system must not make for them.
SHOW = 8


class _Failed:
    """What a git call that could not even start looks like to a caller."""
    returncode, stdout, stderr = 1, "", "git could not be run"


def _git(cwd, *args, timeout: int = 30):
    """Never raises. worktrees._git does, and deliberately -- it is called
    while a task is being set up, where a wedged repo should stop the task.
    This runs behind a dashboard panel and inside `silkworm status`, where one
    unreadable checkout must cost that row and nothing else."""
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("git %s in %s: %s", args[0] if args else "?", cwd, e)
        return _Failed()


def changes(worktree) -> list[str] | None:
    """Every uncommitted path in this checkout, or None if git could not say.

    The same question `worktrees.is_dirty` asks, keeping the answer. Porcelain
    collapses an untracked directory to a single entry instead of walking into
    it, which is what keeps this cheap against a checkout whose .venv holds
    forty thousand files -- and it is also the honest unit, because what a
    reader wants to know is "a virtualenv" rather than forty thousand names.

    None rather than an empty list when git fails, because "I could not read
    this" and "there is nothing in it" are the two answers this module exists
    to keep apart. A child killed mid-rebase leaves an index.lock behind,
    `git status` then exits non-zero, and reading that as clean would print
    the all-clear over a checkout full of half-finished work -- at the exact
    moment somebody is deciding whether to dismiss the task.
    """
    r = _git(worktree, "status", "--porcelain")
    if r.returncode != 0:
        return None
    return [line[3:].strip() for line in r.stdout.splitlines() if line[3:].strip()]


def branch_of(worktree) -> str:
    """The branch this checkout is on.

    worktrees.branch_of answers the same question and is allowed to raise. A
    survey that dies on one wedged checkout reports nothing at all, which is
    the silence this module exists to break -- so it is asked here, through
    the runner that cannot.
    """
    r = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else ""


def _row(path, rec: dict) -> dict | None:
    """A row for this checkout, or None when it is holding nothing.

    A checkout git could not read gets a row too, marked `unreadable`. The
    alternative is dropping it, which reads downstream as an all-clear -- the
    one direction this module must never fail in.

    Nothing else is asked of git until there is something to report: a clean
    checkout is the common case, and it costs one call.
    """
    files = changes(path)
    if files == []:
        return None
    unreadable = files is None
    files = files or []
    state = rec.get("state") or ""
    return {
        "id": rec.get("id") or worktrees.task_of(path),
        "title": rec.get("title") or "",
        "project": rec.get("project") or "",
        "state": state,
        # Which of the two failure modes this one is in. A terminal task's tree
        # is litter that cannot be tidied; a live one's is a promise nobody has
        # collected. The reader is owed the difference.
        "terminal": state in tasks.TERMINAL,
        "known": bool(rec),
        # git would not answer, so nothing here knows what is in there. Said
        # out loud rather than rounded down to "nothing".
        "unreadable": unreadable,
        "path": str(path),
        "name": Path(path).name,
        "branch": branch_of(path),
        "changes": len(files),
        "files": files[:SHOW],
        "thread": rec.get("thread") or "",
        "updated": rec.get("updated") or rec.get("created") or 0,
    }


def survey(records=()) -> list[dict]:
    """One row per checkout on disk that still holds uncommitted changes.

    Reads the directory rather than the board, because the case that matters
    most is the tree whose task nobody can name -- a record compacted away, a
    task id that was never written down. Those come back with an empty title
    and are reported anyway; being unattributable is a reason to say more about
    a held checkout, not less.

    A task that is running right now is skipped. Its tree is dirty because
    somebody is typing in it, and reporting work in progress as stuck work
    would put a row on the panel every time anything ran.

    Busiest first: of the twenty-one checkouts this was written against, twenty
    held only a .venv and a data directory and one held two scripts and a
    document somebody wrote.
    """
    if not worktrees.ROOT.exists():
        return []
    by_id = {r.get("id"): r for r in records if r.get("id")}
    rows = []
    for path in sorted(worktrees.ROOT.iterdir()):
        if not path.is_dir() or worktrees.SEP not in path.name:
            continue
        if not worktrees.is_repo(path):
            continue
        rec = by_id.get(worktrees.task_of(path)) or {}
        if rec.get("state") == tasks.RUNNING:
            continue
        row = _row(path, rec)
        if row:
            rows.append(row)
    # Unreadable first: it is the least certain row and the only one whose
    # size is unknown rather than small. Then busiest, then by name.
    return sorted(rows, key=lambda r: (not r["unreadable"], -r["changes"], r["name"]))


def for_task(task: dict) -> dict | None:
    """The checkout this one task is still holding, if it is holding one.

    Off the record rather than the directory listing: `scope["worktree"]` is
    where this task actually ran, and it survives the release -- a released
    checkout is simply not there any more, which is what the existence check
    reads. One task, one git call, so this can sit in the path that approves
    or dismisses something without slowing it down.
    """
    path = ((task.get("scope") or {}).get("worktree") or "").strip()
    if not path or not worktrees.is_repo(path):
        return None
    return _row(Path(path), task)


def line(rows) -> str:
    """One line for `silkworm status` and the Slack-facing summary."""
    if not rows:
        return ""
    blind = [r for r in rows if r["unreadable"]]
    held = [r for r in rows if not r["unreadable"]]
    paths = sum(r["changes"] for r in held)
    parts = []
    if held:
        parts.append(f"{len(held)} checkout{'s' if len(held) != 1 else ''} still "
                     f"holding uncommitted work "
                     f"({paths} path{'s' if paths != 1 else ''})")
    if blind:
        parts.append(f"{len(blind)} git could not read")
    return ", and ".join(parts)


def short(row) -> str:
    """Enough for a task's event log, which keeps fifty of these."""
    if not row:
        return ""
    what = ("git could not read it" if row["unreadable"] else
            f"{row['changes']} uncommitted path"
            f"{'s' if row['changes'] != 1 else ''}")
    return f"checkout left in place at {row['path']} ({what})"


def note(row) -> str:
    """What to say to the person at the moment the task leaves the board.

    Naming the files, not just the count: whether this is worth going and
    looking at is entirely a question of what is in there, and the system is in
    no position to judge that.
    """
    if not row:
        return ""
    if row["unreadable"]:
        # Not "it is holding nothing". Nothing here knows what is in there, and
        # saying so is the whole difference between a report and an all-clear.
        return (f":deciduous_tree: Its isolated checkout is left exactly where "
                f"it is — `{row['path']}` — and git would not say what is in "
                f"it, so it may be holding uncommitted work. Nothing here will "
                f"remove it; go and look.")
    shown = ", ".join(f"`{f}`" for f in row["files"][:4])
    more = (f" and {row['changes'] - 4} more" if row["changes"] > 4 else "")
    return (f":deciduous_tree: Its isolated checkout still holds "
            f"{row['changes']} uncommitted path"
            f"{'s' if row['changes'] != 1 else ''}, and is left exactly where "
            f"it is: `{row['path']}` on branch `{row['branch']}` — "
            f"{shown}{more}. Nothing here will remove it; commit what is worth "
            f"keeping, or delete the directory yourself.")

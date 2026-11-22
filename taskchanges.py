"""What a task changed in git, for its page: commits and files, read-only.

A task's page says what it did, and the only honest answer to that is git's.
Its branch (or, once landed, the exact range the landing recorded --
`landing.before..landing.head`) is read for its commits, one subject each,
and the files they touch with lines added and removed.

Every git call goes through the operator's vetted read-only command
(operator_role.git_command): no pager, and none of the programs a
repository's own config can name -- an external diff, a textconv driver,
fsmonitor, gpg, a lazy fetch -- whatever that config says. The arguments
are built here, never taken from a request, and still vetted
(operator_role.git_refusal); a ref that could read as an option is refused.

Not knowing is said, never drawn as "no changes": a call git refused or that
timed out leaves `error` set and the list it was filling as None.
"""

import re
import subprocess

import operator_role

#: Enough to read on a phone; the count says how many there were.
MAX_COMMITS = 50
MAX_FILES = 200
TIMEOUT_S = 20

#: Release tags (releases._RELEASE_TAG): <target>/v<major>.<minor>.<patch>.
_RELEASE_TAG = re.compile(r"^[a-z][a-z0-9-]*/v\d+\.\d+\.\d+$")


def _ok_ref(ref: str) -> bool:
    return bool(ref) and not ref.startswith("-") and "\n" not in ref and ".." not in ref


def git(repo, argv, run=subprocess.run, timeout: int = TIMEOUT_S):
    """(returncode, stdout) of a vetted read-only git call; (-1, "") if it
    could not run or was refused."""
    if operator_role.git_refusal(argv):
        return -1, ""
    try:
        r = run(operator_role.git_command(argv), cwd=str(repo), capture_output=True,
                text=True, errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""
    return r.returncode, r.stdout or ""


def _commits(repo, tips, nots, run):
    code, out = git(repo, ["log", "--no-color", "--format=%H%x1f%s%x1f%ct",
                           *tips, "--not", *nots, "--"], run)
    if code:
        return None
    rows = []
    for line in out.splitlines():
        sha, _, rest = line.partition("\x1f")
        subject, _, when = rest.partition("\x1f")
        if sha:
            rows.append({"sha": sha[:10], "subject": subject[:200],
                         "at": int(when) if when.isdigit() else None})
    return rows


def _files(repo, spec, run):
    code, out = git(repo, ["diff", "--no-color", "--numstat", "-M", *spec, "--"], run)
    if code:
        return None
    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        add, rem, path = parts
        rows.append({"path": path[:300],
                     # "-" is git's word for a binary file: no line counts.
                     "added": int(add) if add.isdigit() else None,
                     "removed": int(rem) if rem.isdigit() else None})
    return rows


def released_in(repo, head: str, run=subprocess.run) -> dict | None:
    """The first release tag that contains `head`: {tag, at}, or None."""
    if not _ok_ref(head):
        return None
    code, out = git(repo, ["for-each-ref", "--contains", head, "--sort=creatordate",
                           "--format=%(creatordate:unix) %(refname:short)", "refs/tags"], run)
    if code:
        return None
    for line in out.splitlines():
        when, _, name = line.strip().partition(" ")
        if when.isdigit() and _RELEASE_TAG.match(name):
            return {"tag": name, "at": int(when)}
    return None


def contains(repo, head: str, sha: str, run=subprocess.run) -> bool | None:
    """Whether commit `sha` has `head` in its history; None if git cannot say."""
    if not (_ok_ref(head) and _ok_ref(sha)):
        return None
    code, _ = git(repo, ["merge-base", "--is-ancestor", head, sha], run)
    return True if code == 0 else False if code == 1 else None


def read(repo, *, branch: str = "", bases=(), landing: dict | None = None, count: int = 0,
         run=subprocess.run) -> dict:
    """{branch, base, range, landed, commits, commit_count, files, error}.

    A landing that recorded its range is read over exactly that range; work
    not landed is the branch measured against every copy of its base (what no
    copy of the base has, as branches.ahead counts it)."""
    landing = landing or {}
    out = {"branch": branch, "base": str(landing.get("base") or ""), "range": "",
           "landed": False, "commits": None, "commit_count": None, "files": None,
           "error": ""}
    before, head = str(landing.get("before") or ""), str(landing.get("head") or "")
    if landing.get("landed") and _ok_ref(head) and not before:
        # A landing record that kept no `before` (none does once stored):
        # landing fast-forwards the base onto the rebased branch, so the
        # task's own commits -- `count`, as its checkout closed, else one --
        # are the top of the landed head.
        before = f"{head}~{max(1, int(count or 0))}"
    if landing.get("landed") and _ok_ref(before) and _ok_ref(head):
        short = before if "~" in before else before[:8]
        out.update(landed=True, range=f"{short.replace(head, head[:8])}..{head[:8]}")
        commits = _commits(repo, [head], [before], run)
        files = _files(repo, [before, head], run)
    elif branch and _ok_ref(branch) and bases and all(_ok_ref(b) for b in bases):
        code, _ = git(repo, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], run)
        if code:
            out["error"] = f"the branch {branch} is not here (landed, dropped, or never made)"
            return out
        out["range"] = f"{branch} against {bases[0]}"
        commits = _commits(repo, [f"refs/heads/{branch}"], list(bases), run)
        files = _files(repo, [f"{bases[0]}...refs/heads/{branch}"], run)
    else:
        out["error"] = "no branch or landed range to read"
        return out
    if commits is None or files is None:
        out["error"] = "git could not read the changes"
    if commits is not None:
        out["commit_count"] = len(commits)
        out["commits"] = commits[:MAX_COMMITS]
    if files is not None:
        out["file_count"] = len(files)
        out["added"] = sum(f["added"] or 0 for f in files)
        out["removed"] = sum(f["removed"] or 0 for f in files)
        out["files"] = files[:MAX_FILES]
    return out


def for_task(rec: dict, *, scope_for=None, own_repo=None, own_sha: str = "",
             run=subprocess.run) -> dict | None:
    """read() for one task record: its repository (its own scope, else its
    project's checkout), its landed range or its branch against its project's
    base now (branches.base_pref), the release tag that shipped it, and --
    for Silkworm's own code (`own_repo`, the checkout the bot runs from) --
    whether `own_sha`, the revision the bot booted from, has it. None when
    the task has no repository to read."""
    import branches
    import repos
    import worktrees
    repo = branches.repo_for(rec)
    if repo is None and rec.get("project") and scope_for is not None:
        cwd = (scope_for(rec["project"]) or {}).get("cwd")
        repo = cwd if cwd and worktrees.is_repo(cwd) else None
    if repo is None:
        return None
    result = rec.get("result") or {}
    landing = result.get("landing") or {}
    if not (rec.get("branch") or landing or result.get("landed") or rec.get("attempts")):
        # Never ran: there is no branch yet, and "the branch is not here"
        # would read as something lost.
        return {"branch": "", "base": "", "range": "", "landed": False, "commits": None,
                "commit_count": None, "files": None, "error": "", "not_run": True}
    bases: tuple = ()
    if not landing.get("landed"):
        try:
            bases, _name = branches.base_for(repo, branches.base_pref(rec, scope_for))
        except Exception:
            bases = ()
    out = read(repo, branch=branches.name_for(rec), bases=bases, landing=landing,
               count=rec.get("commits") or 0, run=run)
    head = str(landing.get("head") or result.get("landed") or "")
    if head:
        out["released"] = released_in(repo, head, run)
        if own_sha and own_repo is not None:
            mine = repos.identity(repo)
            if mine and mine == repos.identity(own_repo):
                out["deployed"] = contains(repo, head, own_sha, run)
    return out

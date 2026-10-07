"""Where projects live on disk: making one, finding one, and noticing one.

Three jobs, all about the same fact -- a project is a directory:

* `create` makes or adopts a project's directory in one step: the directory,
  a git repository in it, a starting CLAUDE.md, and the registration. A
  private GitHub repository is one more step it will take, but only when asked
  for in so many words -- publishing is outward, and it is never a default.
* `project_for` answers "which project is this directory part of", so a thread
  or task working inside a project's directory is filed under it without
  anyone saying so. Most specific wins.
* `unregistered` lists repositories sitting in the workspace that no project
  covers. Six projects once lived for weeks inside Silkworm's scratch folder
  with nothing filed against them, because nothing ever looked.

Pure of bot state: every store, path and command runner is passed in, so the
tests run against temporary directories and a fake `gh`.
"""

import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import projects
import repos

log = logging.getLogger("silkworm.workspaces")

#: Where `!project new <Name>` puts a project when no path is given.
ROOT = Path.home() / "workspace"

#: Directories under a scanned root that are never projects: archived work,
#: and task checkouts (each one a worktree of a project that is registered).
IGNORED = (".archive", ".worktrees")


# --- paths ---------------------------------------------------------------------

def _key(path) -> str:
    """A comparable form of a path: real, and case-folded where the disk is
    case-insensitive (macOS by default), so ~/workspace/Fathom and
    ~/workspace/fathom are the one directory they are on disk."""
    if not path:
        return ""                       # realpath("") is the process's cwd
    p = os.path.realpath(os.path.expanduser(str(path)))
    return p.casefold() if sys.platform == "darwin" else p


def _inside(child: str, parent: str) -> bool:
    """Both already _key()ed. A directory is inside itself."""
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def project_for(cwd, records, scratch=None) -> str:
    """The slug of the project whose directory holds `cwd`, or "".

    Most specific wins: a project at ~/workspace/x beats one at ~/workspace,
    because the deeper one is the one you are actually in. Archived projects
    are skipped -- new work is not filed into something put away.

    `scratch` is Silkworm's shared scratch folder. It sits inside Silkworm's
    own checkout, so without this every general conversation would be filed
    under Silkworm. A directory in the scratch folder only belongs to a
    project whose own directory is *below* it -- not one registered at the
    scratch folder itself, which `!project <name>` from a general thread
    does, and which would otherwise absorb every general conversation.
    """
    if not cwd:
        return ""
    here = _key(cwd)
    fence = _key(scratch) if scratch else ""
    best, depth = "", -1
    for rec in records:
        if rec.get("archived"):
            continue
        d = (rec.get("scope") or {}).get("cwd")
        if not d:
            continue
        there = _key(d)
        if not _inside(here, there):
            continue
        if fence and _inside(here, fence) and (there == fence or not _inside(there, fence)):
            continue
        if len(there) > depth:
            best, depth = rec["slug"], len(there)
    return best


# --- discovery -----------------------------------------------------------------

def is_repo_root(path) -> bool:
    """A directory that is itself the top of a git repository (or worktree)."""
    return (Path(path) / ".git").exists()


def unregistered(roots, records) -> list[str]:
    """Git repositories directly under any of `roots` that no project covers.

    Covered means a project lives in that directory or below it, or a project
    names the same remote (a second clone of a registered repo is that
    project's, not a new one). Archived projects still cover: put away is not
    unknown. Hidden directories -- .archive, .worktrees and the like -- are
    never scanned. Sorted, deduplicated, real paths.
    """
    dirs = [_key((r.get("scope") or {}).get("cwd")) for r in records
            if (r.get("scope") or {}).get("cwd")]
    remotes = {(r.get("scope") or {}).get("repo") for r in records} - {None, ""}
    found = {}
    for root in roots:
        try:
            children = sorted(Path(root).iterdir())
        except OSError:
            continue
        for child in children:
            if child.name.startswith(".") or child.name in IGNORED:
                continue
            try:
                if not child.is_dir() or not is_repo_root(child):
                    continue
            except OSError:
                continue
            k = _key(child)
            if any(_inside(d, k) for d in dirs):
                continue
            ident = repos.identity(child)
            if ident and ident in remotes:
                continue
            found.setdefault(k, os.path.realpath(child))
    return sorted(found.values())


# --- filing --------------------------------------------------------------------

def threads_to_file(threads: dict, records, scratch=None) -> dict:
    """{thread key: slug} for threads with no project whose directory is a
    project's. Never refiles a filed thread, nor one you unfiled on purpose
    (`!project none`), which is what makes running it again a no-op."""
    out = {}
    for key, entry in threads.items():
        if entry.get("project") or entry.get("unfiled"):
            continue
        slug = project_for(entry.get("cwd"), records, scratch)
        if slug:
            out[key] = slug
    return out


def tasks_to_file(tasks: dict, threads: dict, records, scratch=None,
                  settled=("done", "cancelled", "dismissed")) -> dict:
    """{task id: slug} for unfiled tasks that belong to a project by directory.

    A task run on a filed thread takes the thread's project; otherwise its own
    working directory decides. A reviewer is left alone -- it belongs to its
    parent's project, and is read that way everywhere.

    Only finished work and conversation turns are filed after the fact. A
    project decides whether work may run unsupervised (projects.unready), so
    giving an open, project-less implementor task a ready project would let
    it run, or land, without the person it was waiting on.
    """
    out = {}
    for tid, t in tasks.items():
        if t.get("project") or t.get("role") == "reviewer":
            continue
        if t.get("state") not in settled and t.get("role") != "assistant":
            continue
        thread = threads.get(t.get("thread") or "") or {}
        if thread.get("unfiled"):
            continue
        slug = thread.get("project") or project_for(
            (t.get("scope") or {}).get("cwd"), records, scratch)
        if slug:
            out[tid] = slug
    return out


# --- creation ------------------------------------------------------------------

class Refused(Exception):
    """Creating the project was refused; the message says why and what to do."""


def _run(cmd, cwd, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          timeout=timeout)


def github_name(name: str) -> str:
    """A GitHub repository name from a project name: "Silk Swing" -> Silk-Swing."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.") or "project"


def parse_new(arg: str) -> dict:
    """Read `<Name> [path] [--github] [--adopt] [-- purpose]`.

    The purpose is everything after a lone `--` (or an em dash, which is what
    a phone makes of one). The name may be quoted to hold spaces. Raises
    ValueError with the usage when there is no name.
    """
    # What a phone makes of quotes and a double dash.
    arg = (arg or "").translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"}))
    arg = re.sub(r"(?<!\S)[—–](?=[A-Za-z])", "--", arg)
    head, purpose = arg, ""
    m = re.search(r"(?:^|\s)(?:--|—|–)(?:\s|$)", arg)
    if m:
        head, purpose = arg[:m.start()], arg[m.end():].strip()
    try:
        words = shlex.split(head)
    except ValueError as e:
        raise ValueError(f"could not read that ({e})")
    flags = {w for w in words if w.startswith("--")}
    unknown = flags - {"--github", "--adopt"}
    if unknown:
        raise ValueError(f"unknown option {', '.join(sorted(unknown))}")
    rest = [w for w in words if not w.startswith("--")]
    if not rest:
        raise ValueError("usage: `!project new <Name> [path] [--github] [--adopt] -- <one-line purpose>`")
    if len(rest) > 2:
        raise ValueError("a name with spaces needs quotes, e.g. `!project new \"Silk Swing\"`")
    return {"name": rest[0], "path": rest[1] if len(rest) > 1 else "",
            "github": "--github" in flags, "adopt": "--adopt" in flags,
            "purpose": purpose}


def create(name: str, project_store, *, path: str = "", purpose: str = "",
           github: bool = False, adopt: bool = False, owner: str = "",
           root=None, run=_run, gh=None) -> dict:
    """Make or adopt a project's directory and register it, in one step.

    The directory is `path`, or `root`/<slug>. A new one is made; an existing
    one is adopted only when that is clearly meant -- you named its path, or
    said `adopt` -- because a bare name that happens to match a directory
    already there is as likely a collision as a wish. A name already taken by
    a project, or a directory another project already lives in, is refused.

    It is made a git repository if it is not one (with a first commit when it
    has anything in it), gets a CLAUDE.md if it has none, and is registered
    with its directory and, when it has a GitHub remote, its repo. Only with
    `github` does it also create a private GitHub repository and push to it;
    `gh` is the command runner for that (defaults to `run`), injectable so no
    test ever reaches GitHub.

    Returns what was done, for the caller to report. Raises Refused before
    touching anything when the request is refused; if a git step fails part
    way, what this call made is removed again before Refused is raised, so a
    retry is not refused for a directory it left behind.
    """
    gh = gh or run
    name = (name or "").strip()
    if not name:
        raise Refused("a project needs a name")
    slug = projects.slugify(name)
    if project_store.get(slug):
        raise Refused(f"there is already a project `{slug}` — file this thread "
                      f"under it with `!project {slug}`")
    named = bool(path)
    base = Path(root or ROOT)
    # A relative path is relative to the workspace, not to wherever the bot
    # happens to have been started.
    target = base / os.path.expanduser(path) if path else base / slug
    target = Path(os.path.abspath(target))
    existed = target.exists()
    if existed and not target.is_dir():
        raise Refused(f"{target} exists and is not a directory")
    if existed and not (named or adopt):
        raise Refused(f"{target} already exists — to make it this project, say so: "
                      f"`!project new {name} --adopt` (or give its path)")
    for other in project_store.all():
        if _key((other.get("scope") or {}).get("cwd") or "") == _key(target):
            raise Refused(f"{target} is already project `{other['slug']}`")
    repo_root = existed and is_repo_root(target)
    if not repo_root:
        # A new repository inside another one's working tree nests, and the
        # outer one then sees it as a pile of untracked files. Refused unless
        # the outer repository ignores the spot.
        outer = _git_top(target if existed else target.parent, run)
        if outer and not _ignored(outer, target, run):
            raise Refused(f"{target} is inside the git repository {outer}; a "
                          "repository there would nest in it. Pick a path outside it.")

    done = {"slug": slug, "title": name, "cwd": str(target), "made_dir": False,
            "initialised": False, "committed": False, "claude_md": False,
            "github_url": "", "repo": "", "notes": []}
    try:
        _make(target, name, purpose, existed, repo_root, done, run)
    except BaseException:
        _undo(target, existed, done)
        raise

    if github:
        url, note = github_repo(target, name, owner=owner, run=run, gh=gh)
        done["github_url"] = url
        if note:
            done["notes"].append(note)

    ident = repos.identity(target)
    scope = {"cwd": str(target)}
    if ident.startswith("github.com/"):
        scope["repo"] = ident
        done["repo"] = ident
        done["github_url"] = done["github_url"] or f"https://{ident}"
    rec = project_store.ensure(name, scope=scope)
    done["slug"] = rec["slug"]
    log.info("project %s %s at %s%s", rec["slug"], "made" if done["made_dir"] else "adopted",
             target, f" ({done['github_url']})" if done["github_url"] else "")
    return done


def _make(target, name, purpose, existed, repo_root, done, run) -> None:
    """The writing half of `create`, recording each step in `done` as it goes."""
    if not existed:
        target.mkdir(parents=True)
        done["made_dir"] = True
    md = target / "CLAUDE.md"
    if not md.exists():
        line = (purpose or "").strip()
        md.write_text(f"# {name}\n\n{line}\n" if line else f"# {name}\n")
        done["claude_md"] = True
        if not line:
            done["notes"].append("CLAUDE.md has only a title — add a line saying what this is for")
    if not repo_root:
        done["initialised"] = True          # before: a failed init can leave a .git
        _check(run(["git", "init", "-q"], target), "git init")
        _check(run(["git", "add", "-A"], target), "git add")
        if run(["git", "diff", "--cached", "--quiet"], target).returncode != 0:
            _check(run(["git", "commit", "-q", "-m", f"Start {name}"], target), "git commit")
            done["committed"] = True
    elif done["claude_md"]:
        done["notes"].append("CLAUDE.md was added to an existing repository and is not committed")


def _undo(target, existed, done) -> None:
    """Remove what a failed `create` made, and nothing that was already there."""
    try:
        if done["made_dir"]:
            shutil.rmtree(target, ignore_errors=True)
            return
        if done["initialised"]:
            shutil.rmtree(target / ".git", ignore_errors=True)
        if done["claude_md"]:
            (target / "CLAUDE.md").unlink(missing_ok=True)
    except OSError:
        log.exception("could not clear up after a failed create at %s", target)


def github_repo(target, name: str, *, owner: str = "", run=_run, gh=None) -> tuple:
    """Create a PRIVATE GitHub repository for `target` and push it.

    Only ever called because someone asked -- `--github`, the form's box, or
    `!project github create`. Returns (url, note): the URL on success, otherwise ""
    and why not. A directory that already has a remote, or nothing committed
    to push, is left alone.
    """
    gh = gh or run
    target = Path(target)
    have = repos.identity(target)
    if have:
        return "", f"not creating a GitHub repository: it already has a remote ({have})"
    if not is_repo_root(target) or not _has_commit(target, run):
        return "", "not creating a GitHub repository: there is nothing committed to push yet"
    full = f"{owner}/{github_name(name)}" if owner else github_name(name)
    r = gh(["gh", "repo", "create", full, "--private", f"--source={target}",
            "--remote=origin", "--push"], target, timeout=180)
    if r.returncode != 0:
        return "", ("GitHub repository not created: "
                    + ((r.stderr or r.stdout or "").strip()[-300:] or "gh failed"))
    urls = re.findall(r"https://github\.com/\S+", (r.stdout or "") + (r.stderr or ""))
    ident = repos.identity(target)
    url = urls[-1].rstrip(".") if urls else (f"https://{ident}" if ident else "")
    log.info("created private GitHub repository %s for %s", url or full, target)
    return url, ""


def _check(r, what: str) -> None:
    if r.returncode != 0:
        raise Refused(f"{what} failed: {(r.stderr or r.stdout or '').strip()[-300:]}")


def _git_top(path, run) -> str:
    p = Path(path)
    while not p.exists():
        p = p.parent
    r = run(["git", "rev-parse", "--show-toplevel"], p)
    return r.stdout.strip() if r.returncode == 0 else ""


def _ignored(top: str, target, run) -> bool:
    # Asked about a file inside it: a directory pattern ("inner/") does not
    # match a path that does not exist yet, but anything under it does.
    probe = Path(target) / ".silkworm-probe"
    return run(["git", "check-ignore", "-q", "--no-index", str(probe)], top).returncode == 0


def _has_commit(target, run) -> bool:
    return run(["git", "rev-parse", "--verify", "-q", "HEAD"], target).returncode == 0


def describe(done: dict) -> str:
    """One Slack-ready paragraph saying what `create` did."""
    bits = []
    bits.append("made" if done["made_dir"] else "adopted")
    if done["initialised"]:
        bits.append("git-initialised" + (" with a first commit" if done["committed"] else ""))
    if done["claude_md"]:
        bits.append("started its CLAUDE.md")
    text = (f":seedling: *{done['title']}* (`{done['slug']}`) — {', '.join(bits)} at "
            f"`{done['cwd']}`, and registered.")
    if done["github_url"]:
        text += f"\nGitHub: {done['github_url']}"
    for n in done["notes"]:
        text += f"\n• {n}"
    return text

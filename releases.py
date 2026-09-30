"""Releases: getting merged work out to where people use it.

Merging and releasing are different events. A task lands on the base branch
once it passes its tests and review, and for Silkworm and the trader that is
the end of it -- they run straight from their checkouts. An app is not like
that. A push to Cadence's main started an Xcode Cloud archive bound for
TestFlight, so every task that landed would have been a build; and a repo can
hold several things that ship separately -- the iOS app, its website, its
Supabase backend, one day an Android app -- each on its own schedule.

So a project describes its release targets in its own repository, in
`.silkworm/release.toml`, versioned with the code it ships:

    [targets.backend]
    paths    = ["supabase/"]
    ship     = "command"              # Silkworm runs the deploy here
    commands = ["~/.supabase/sb db push --yes"]
    preview  = ["~/.supabase/sb db push --dry-run"]

    [targets.ios]
    paths   = ["ios/"]
    ship    = "tag"                   # pushing the tag is the release; CI builds it
    version = { file = "ios/project.yml", key = "CFBundleShortVersionString" }
    after   = ["backend"]             # released first when it has anything pending

What is ready to release is what landed since the target's last release tag
and touches its paths. A release is recorded as a tag, `<target>/v<version>`,
so the history of what shipped when is in git, and the next version is the
last one bumped. A target with a version file has the file bumped and
committed as part of the release, because the store rejects a build whose
version is not higher than the last.

Deliberately not here: a release that happens on its own. Every release is a
person asking for one; see `bot.handle_release`.
"""

import logging
import os
import re
import subprocess
import tomllib
from pathlib import Path

log = logging.getLogger("silkworm.releases")

CONFIG = Path(".silkworm") / "release.toml"
SHIPS = ("tag", "command")
LEVELS = ("patch", "minor", "major")
#: First release of a target with no tag and no version file.
FIRST = (1, 0, 0)
COMMAND_TIMEOUT_S = 1800
OUTPUT_CHARS = 1500

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class ReleaseError(Exception):
    """A release that cannot go ahead, with the reason as its message."""


def _git(repo, *args, timeout: int = 120):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, timeout=timeout)


# --- configuration -------------------------------------------------------------

def load(repo) -> dict:
    """The project's release targets, name -> settings. Empty if it has none.

    Malformed configuration raises: a typo that silently dropped the backend
    from a release would be the worst way to find it.
    """
    path = Path(repo) / CONFIG
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        raw = tomllib.load(f).get("targets") or {}
    targets = {}
    for name, t in raw.items():
        if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
            raise ReleaseError(f"target name {name!r}: lowercase letters, digits and -")
        ship = t.get("ship")
        if ship not in SHIPS:
            raise ReleaseError(f"{name}: ship must be one of {SHIPS}, not {ship!r}")
        paths = t.get("paths") or []
        if not paths or not all(isinstance(p, str) and p for p in paths):
            raise ReleaseError(f"{name}: paths must list what belongs to it")
        if ship == "command" and not t.get("commands"):
            raise ReleaseError(f"{name}: a command release needs commands")
        version = t.get("version")
        if version and not (version.get("file") and version.get("key")):
            raise ReleaseError(f"{name}: version needs file and key")
        targets[name] = {
            "name": name, "paths": list(paths), "ship": ship,
            "commands": list(t.get("commands") or []),
            "preview": list(t.get("preview") or []),
            "env": {str(k): str(v) for k, v in (t.get("env") or {}).items()},
            "env_files": list(t.get("env_files") or []),
            "version": version or None,
            "after": list(t.get("after") or []),
        }
    for name, t in targets.items():
        for dep in t["after"]:
            if dep not in targets:
                raise ReleaseError(f"{name}: after names {dep!r}, which is not a target")
    order(targets, list(targets))           # raises on a cycle
    return targets


# --- versions ------------------------------------------------------------------

def parse(version: str) -> tuple | None:
    m = _SEMVER.match((version or "").strip().lstrip("v"))
    return tuple(int(x) for x in m.groups()) if m else None


def fmt(v: tuple) -> str:
    return ".".join(str(x) for x in v)


def bump(v: tuple, level: str) -> tuple:
    if level == "major":
        return (v[0] + 1, 0, 0)
    if level == "minor":
        return (v[0], v[1] + 1, 0)
    if level == "patch":
        return (v[0], v[1], v[2] + 1)
    raise ReleaseError(f"level must be one of {LEVELS} or a version like 1.2.0")


def tag_prefix(target: dict) -> str:
    return f"{target['name']}/v"


def last_release(repo, target: dict) -> tuple[str, tuple] | tuple[None, None]:
    """(tag, version) of the highest release of this target, by version not date."""
    prefix = tag_prefix(target)
    r = _git(repo, "tag", "--list", f"{prefix}*")
    best = (None, None)
    for tag in r.stdout.split():
        v = parse(tag[len(prefix):])
        if v and (best[1] is None or v > best[1]):
            best = (tag, v)
    return best


def file_version(repo, target: dict) -> tuple | None:
    """The version its version file declares, if it has one. Every occurrence
    of the key must agree -- an app and its extension shipping different
    versions is a store rejection -- so disagreement raises."""
    spec = target.get("version")
    if not spec:
        return None
    text = (Path(repo) / spec["file"]).read_text()
    found = {m for m in re.findall(
        rf'^\s*{re.escape(spec["key"])}:\s*"?([0-9.]+)"?\s*$', text, re.M)}
    if not found:
        raise ReleaseError(f"{spec['file']} has no {spec['key']}")
    if len(found) > 1:
        raise ReleaseError(f"{spec['file']} disagrees with itself: "
                           f"{spec['key']} is {', '.join(sorted(found))}")
    v = parse(found.pop())
    if not v:
        raise ReleaseError(f"{spec['key']} in {spec['file']} is not x.y.z")
    return v


def next_version(repo, target: dict, level: str) -> tuple:
    """The version this release will carry.

    Bumped from the higher of the last tag and the version file: the file can
    be ahead of the tags (bumped by hand, or released before tagging existed),
    and a release must never go backwards.
    """
    explicit = parse(level)
    _, tagged = last_release(repo, target)
    current = max([v for v in (tagged, file_version(repo, target)) if v], default=None)
    if explicit:
        if current and explicit <= current:
            raise ReleaseError(f"{fmt(explicit)} is not higher than {fmt(current)}")
        return explicit
    if current is None:
        return FIRST
    return bump(current, level)


def write_version(repo, target: dict, version: tuple) -> None:
    spec = target["version"]
    path = Path(repo) / spec["file"]
    text = path.read_text()
    new, n = re.subn(rf'^(\s*{re.escape(spec["key"])}:\s*)"?[0-9.]+"?(\s*)$',
                     lambda m: f'{m.group(1)}"{fmt(version)}"{m.group(2)}', text, flags=re.M)
    if not n:
        raise ReleaseError(f"{spec['file']} has no {spec['key']} to bump")
    path.write_text(new)


# --- what is pending -----------------------------------------------------------

def pending(repo, target: dict, base: str = "HEAD") -> list[str]:
    """Commits since this target's last release that touch its paths, newest
    first, as "sha subject". Everything that touches them, if never released."""
    tag, _ = last_release(repo, target)
    rng = f"{tag}..{base}" if tag else base
    r = _git(repo, "log", "--format=%h %s", rng, "--", *target["paths"])
    if r.returncode != 0:
        raise ReleaseError(f"could not read history: {r.stderr.strip()[-200:]}")
    return [line for line in r.stdout.splitlines() if line.strip()]


def order(targets: dict, names: list[str]) -> list[str]:
    """`names` plus whatever they come after, dependencies first. Raises on a
    cycle, which load() checks so a bad file fails on reading, not releasing."""
    out, seen, stack = [], set(), set()

    def visit(n):
        if n in seen:
            return
        if n in stack:
            raise ReleaseError(f"release order has a cycle through {n!r}")
        stack.add(n)
        for dep in targets[n]["after"]:
            visit(dep)
        stack.discard(n)
        seen.add(n)
        out.append(n)
    for n in names:
        if n not in targets:
            raise ReleaseError(f"no target {n!r}; this project has {', '.join(targets) or 'none'}")
        visit(n)
    return out


def plan(repo, names: list[str] | None = None, level: str = "patch") -> list[dict]:
    """What releasing would do, in order, without doing any of it.

    Asked for specific targets, a dependency is included only if it has
    something pending: releasing the app does not re-release an unchanged
    backend, but it does refuse to ship ahead of a backend change it may need.
    """
    targets = load(repo)
    wanted = names or [n for n in targets if pending(repo, targets[n])]
    steps = []
    for name in order(targets, wanted):
        t = targets[name]
        commits = pending(repo, t)
        if not commits and name not in (names or []):
            continue
        steps.append({"target": name, "ship": t["ship"], "commits": commits,
                      "version": fmt(next_version(repo, t, level)) if commits else None,
                      "tag": f"{tag_prefix(t)}{fmt(next_version(repo, t, level))}"
                             if commits else None})
    return steps


# --- doing it ------------------------------------------------------------------

def _env(target: dict) -> dict:
    env = dict(os.environ)
    for f in target["env_files"]:
        p = Path(os.path.expanduser(f))
        if not p.exists():
            raise ReleaseError(f"credentials file {f} is missing")
        for line in p.read_text().splitlines():
            m = re.match(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
            if m:
                env[m.group(1)] = m.group(2).strip("'\"")
    env.update(target["env"])
    return env


def _run(command: str, repo, env) -> dict:
    """One deploy command, in the repo, by the user's shell rules. The output
    is kept (tail only) because it is what says what a deploy did."""
    try:
        r = subprocess.run(["/bin/zsh", "-lc", os.path.expanduser(command)],
                           cwd=str(repo), env=env, capture_output=True, text=True,
                           timeout=COMMAND_TIMEOUT_S)
        out = (r.stdout + r.stderr)[-OUTPUT_CHARS:]
        return {"command": command, "ok": r.returncode == 0, "code": r.returncode,
                "output": out}
    except subprocess.TimeoutExpired:
        return {"command": command, "ok": False, "code": None,
                "output": f"timed out after {COMMAND_TIMEOUT_S}s"}


def preview(repo, name: str) -> list[dict]:
    """Run a target's preview commands (a migration dry run): what it would
    change, shown before anyone confirms something that cannot be undone."""
    t = load(repo)[name]
    return [_run(c, repo, _env(t)) for c in t["preview"]]


def ready(repo, base: str) -> str:
    """Why the checkout cannot release right now, or "".

    Released from the checkout itself, on the base branch, clean and not
    behind origin: a release is of what is on the base, not of whatever a
    working tree happens to hold.
    """
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != base:
        return f"the checkout is on {branch or 'a detached HEAD'}, not {base}"
    if _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return "the checkout has uncommitted changes"
    _git(repo, "fetch", "--quiet", "origin", timeout=60)
    behind = _git(repo, "rev-list", "--count", f"{base}..origin/{base}").stdout.strip()
    if behind and behind != "0":
        return (f"{base} is {behind} commit(s) behind origin -- pull first, so the "
                "release is of what everyone else sees")
    return ""


def release(repo, name: str, level: str = "patch", base: str = "main") -> dict:
    """Release one target. Returns a record of every step; never half-tags.

    Order is the point. A command target deploys first and is tagged only if
    every command succeeded, so a tag always means "this shipped". A tag
    target has its version bumped and committed, then the commit and the tag
    are pushed together -- the push is the release.
    """
    targets = load(repo)
    if name not in targets:
        raise ReleaseError(f"no target {name!r}")
    t = targets[name]
    why = ready(repo, base)
    if why:
        raise ReleaseError(why)
    commits = pending(repo, t)
    if not commits:
        raise ReleaseError(f"{name} has nothing to release since "
                           f"{last_release(repo, t)[0] or 'the start'}")
    version = next_version(repo, t, level)
    tag = f"{tag_prefix(t)}{fmt(version)}"
    record = {"target": name, "version": fmt(version), "tag": tag,
              "commits": commits, "steps": [], "released": False}

    if t["ship"] == "command":
        env = _env(t)
        for c in t["commands"]:
            step = _run(c, repo, env)
            record["steps"].append(step)
            if not step["ok"]:
                record["error"] = f"{c} failed (exit {step['code']}); nothing was tagged"
                return record

    if t["version"]:
        write_version(repo, t, version)
        _git(repo, "add", t["version"]["file"])
        c = _git(repo, "commit", "-q", "-m", f"Release {name} {fmt(version)}")
        if c.returncode != 0:
            _git(repo, "checkout", "--", t["version"]["file"])
            record["error"] = f"could not commit the version bump: {c.stderr.strip()[-200:]}"
            return record

    note = f"Release {name} {fmt(version)}\n\n" + "\n".join(commits[:100])
    tg = _git(repo, "tag", "-a", tag, "-m", note)
    if tg.returncode != 0:
        record["error"] = f"could not tag: {tg.stderr.strip()[-200:]}"
        return record
    refs = ([base] if t["version"] else []) + [f"refs/tags/{tag}"]
    p = _git(repo, "push", "--atomic", "origin", *refs, timeout=180)
    record["steps"].append({"command": "git push " + " ".join(refs),
                            "ok": p.returncode == 0, "code": p.returncode,
                            "output": (p.stdout + p.stderr)[-OUTPUT_CHARS:]})
    if p.returncode != 0:
        record["error"] = ("tagged locally but the push was refused; nothing reached "
                           "origin -- `git push --atomic origin "
                           + " ".join(refs) + "` from the checkout retries it")
        return record
    record["released"] = True
    return record

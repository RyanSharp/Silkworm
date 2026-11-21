"""GitHub CI as a landing gate: the rebased commit lands only once CI is green.

Why it exists. Landing proves a branch by running the project's test command
on the mini. For Cadence that is `make -C ios test`, and GitHub Actions then
ran the iOS "Tests" and the "Backend tests" workflows on a different runner,
after main had already moved and been pushed. In November 2026 that order let
three kinds of red reach main: AuthServiceTests hanging on an upsert on
c9bd1476, Backend tests red from its very first run, and a flaky
SupabaseSyncServicePullTests. Each was found after the fact, on a main that
everybody was already building on.

So for a project with `ci_gate` set, merge.land pushes the rebased commit to
origin as `silkworm/<task id>` -- never as the base -- and waits for the
required workflow runs on that exact commit before it fast-forwards anything.

What "required" means (`parse` below):

  "all"           every workflow whose push trigger fires for this change.
  "Tests, Backend tests"
                  just those, by `name:` or by file name.

A workflow that would not run for this change at all -- its `paths:` filter
matches no file the landing changes -- counts as passed, and the landing
record says so by name. One that *would* run once the change reaches the base
but is not triggered by the pushed branch name (`branches: ["*"]`, which does
not match a name with a slash in it) is refused rather than skipped: passing
it would be exactly the after-the-fact red this gate exists to stop, and the
fix is one line in the workflow (`"**"`).

Waiting polls `gh api` with a deadline. It runs in the bot's landing thread,
never inside a Claude turn, and the landing lets go of the checkout while it
waits (see `Gate.outside`), so a forty-minute macOS run does not lock a
project's threads out of their own directory.

Outcomes, as merge.land stages:

  ci          a required run failed: the workflow, the job, the failing step
              and the first failing test read off the job's log.
  ci-timeout  not finished by the deadline. Retryable: the pushed branch is
              kept, so landing again resumes waiting on the same runs.
  ci-config   the gate cannot be evaluated -- no GitHub remote, an unreadable
              workflow, a named workflow that does not exist, or one the
              branch name does not trigger. A person's fix, not a retry's.

The pushed branch is deleted from origin once the landing is over (landed, or
refused for any reason but a timeout) and when the task's branch is dropped.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import subprocess
import time
from pathlib import PurePosixPath

import verify

log = logging.getLogger("silkworm.ci")

#: The landing stage while it waits; the CLI, dashboard and digest show it as
#: "waiting for CI" rather than as a refusal or a plain "landing…".
WAITING = "ci-wait"
FAILED = "ci"
TIMEOUT = "ci-timeout"
CONFIG = "ci-config"
STAGES = (WAITING, FAILED, TIMEOUT, CONFIG)

#: How long a landing waits for CI. Cadence's macOS job takes ~20 minutes when
#: a runner is free; 45 leaves room for a queue without waiting all afternoon.
DEADLINE_S = 45 * 60
POLL_S = 30

#: The executable asked. A module attribute so tests can point it at a fake.
GH = "gh"
RUBY = "ruby"

PENDING = ("queued", "in_progress", "waiting", "requested", "pending")
#: Conclusions a run can pass with. `skipped` is every job skipped by its own
#: `if:` -- the workflow decided this change was not its business.
PASSING = ("success", "neutral", "skipped")

_STAMP = re.compile("^\ufeff?\\d{4}-\\d\\d-\\d\\dT[\\d:.]+Z ?")
_ANSI = re.compile("\x1b\\[[0-9;?]*[ -/]*[@-~]")


def label(stage: str) -> str:
    """How a landing stage reads to a person, where it differs from its key."""
    return {WAITING: "waiting for CI", FAILED: "CI failed",
            TIMEOUT: "CI timed out", CONFIG: "CI gate misconfigured"}.get(stage, stage)


# ---------------------------------------------------------------- the setting

def parse(value) -> str | list[str]:
    """The `ci_gate` setting: "" (off), "all", or a list of workflow names.

    Commas or newlines between names, since names hold spaces ("Backend
    tests"). "off", "none", "clear" and a blank turn it off.
    """
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        text = (value or "").strip()
        if text.lower() in ("", "off", "none", "clear", "false", "no"):
            return ""
        if text.lower() in ("all", "on", "true", "yes"):
            return "all"
        items = [t.strip() for t in re.split(r"[,\n]+", text)]
    items = [i for i in items if i]
    if not items:
        return ""
    if [i.lower() for i in items] == ["all"]:
        return "all"
    out: list[str] = []
    for i in items:
        if i not in out:
            out.append(i)
    return out


def describe(value) -> str:
    want = parse(value)
    if not want:
        return "off"
    return "all workflows that run" if want == "all" else ", ".join(want)


# ----------------------------------------------------------- GitHub's filters

def _regex(pattern: str) -> re.Pattern:
    """A GitHub Actions filter pattern as a regex.

    `*` is anything but "/", `**` is anything, `?` and `+` quantify the
    character before them (as in a regex), `[...]` is a class, and a
    backslash escapes. `**/` also matches no directory at all, so
    `docs/**/*.md` covers `docs/a.md`.
    """
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3; continue
        if pattern.startswith("**", i):
            out.append(".*"); i += 2; continue
        if c == "*":
            out.append("[^/]*")
        elif c in "?+":
            out.append(c)
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j < 0:
                out.append(re.escape(c))
            else:
                out.append(pattern[i:j + 1]); i = j + 1; continue
        elif c == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1])); i += 2; continue
        else:
            out.append(re.escape(c))
        i += 1
    try:
        return re.compile("".join(out) + r"\Z")
    except re.error:
        return re.compile(re.escape(pattern) + r"\Z")


def matches(patterns, name: str) -> bool:
    """Whether `name` passes an ordered filter list, `!` patterns negating.

    The last pattern that matches decides; nothing matching is a no.
    """
    keep = False
    for p in patterns or []:
        p = str(p)
        neg = p.startswith("!")
        if _regex(p[1:] if neg else p).match(name):
            keep = not neg
    return keep


def _as_list(v) -> list:
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)


def push_trigger(doc) -> tuple[bool, dict]:
    """(has a push trigger, its filters) from a parsed workflow."""
    if not isinstance(doc, dict):
        return False, {}
    # YAML 1.1 reads a bare `on` as true, so the key arrives as "true".
    on = doc.get("on", doc.get("true", doc.get(True)))
    if isinstance(on, str):
        on = {on: None}
    elif isinstance(on, list):
        on = {str(e): None for e in on}
    if not isinstance(on, dict) or "push" not in on:
        return False, {}
    cfg = on.get("push")
    return True, cfg if isinstance(cfg, dict) else {}


def fires(cfg: dict, branch: str, changed) -> tuple[bool, str]:
    """Whether a push of `branch` changing `changed` fires this push trigger.

    Returns (fires, why-not). Mirrors GitHub's rules: tags without branches
    means tag pushes only; branches and branches-ignore filter the name; paths
    and paths-ignore filter the files. (GitHub reads only the first 300
    changed files for paths; a change bigger than that is judged on all of
    them here, and may time out waiting for a run GitHub chose not to start.)
    """
    has_b, has_bi = "branches" in cfg, "branches-ignore" in cfg
    if not (has_b or has_bi) and ("tags" in cfg or "tags-ignore" in cfg):
        return False, "it runs on tag pushes only"
    if has_b and not matches(_as_list(cfg.get("branches")), branch):
        return False, f"its branches filter does not match {branch!r}"
    if has_bi and any(_regex(str(p)).match(branch)
                      for p in _as_list(cfg.get("branches-ignore"))):
        return False, f"its branches-ignore filter excludes {branch!r}"
    changed = list(changed or [])
    if "paths" in cfg:
        pats = _as_list(cfg.get("paths"))
        if not any(matches(pats, f) for f in changed):
            return False, "no changed file matches its paths filter"
    if "paths-ignore" in cfg:
        pats = [str(p) for p in _as_list(cfg.get("paths-ignore"))]
        if all(any(_regex(p).match(f) for p in pats) for f in changed):
            return False, "every changed file is in its paths-ignore"
    return True, ""


# ------------------------------------------------------------------- the repo

def _git(cwd, *args, timeout: int = 120):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout)


def slug_of(repo) -> str:
    """owner/name of origin on GitHub, or "" if origin is not on GitHub."""
    try:
        url = _git(repo, "remote", "get-url", "origin").stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""
    m = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else ""


_RUBY_PARSE = r"""
require "yaml"; require "json"
docs = JSON.parse(STDIN.read)
out = docs.map do |s|
  begin
    {"ok" => YAML.safe_load(s, aliases: true)}
  rescue Exception => e
    {"error" => e.message}
  end
end
STDOUT.write(JSON.dump(out))
"""


def _yaml(texts: list[str]) -> list[dict]:
    """Parse YAML documents: [{"ok": doc} | {"error": why}], one per text.

    Python here has no YAML library and the bot depends on nothing it does
    not need; Ruby ships with macOS and has one built in. One process for all
    the files.
    """
    if not texts:
        return []
    try:
        r = subprocess.run([RUBY, "-e", _RUBY_PARSE], input=json.dumps(texts),
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            got = json.loads(r.stdout)
            if isinstance(got, list) and len(got) == len(texts):
                return got
        why = (r.stderr or r.stdout or "no output").strip()[-200:]
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        why = str(exc)
    return [{"error": f"could not parse YAML ({why})"} for _ in texts]


def workflows(repo, commit: str) -> list[dict]:
    """The workflows at `commit`: [{path, name, push, cfg, error}].

    Read from the commit being landed, not the checkout: a branch that fixes a
    workflow's trigger is judged by the trigger it fixes it to.
    """
    r = _git(repo, "ls-tree", "--name-only", commit, ".github/workflows/")
    paths = [p for p in r.stdout.splitlines()
             if p.endswith((".yml", ".yaml"))] if r.returncode == 0 else []
    texts = []
    for p in paths:
        s = _git(repo, "show", f"{commit}:{p}")
        texts.append(s.stdout if s.returncode == 0 else "")
    out = []
    for p, parsed in zip(paths, _yaml(texts)):
        doc = parsed.get("ok")
        rec = {"path": p, "name": PurePosixPath(p).name, "push": False,
               "cfg": {}, "error": parsed.get("error") or ""}
        if isinstance(doc, dict):
            rec["name"] = str(doc.get("name") or p)
            rec["push"], rec["cfg"] = push_trigger(doc)
        elif not rec["error"]:
            rec["error"] = "not a workflow (no mapping at the top)"
        out.append(rec)
    return out


def changed_files(repo, before: str, commit: str) -> list[str]:
    r = _git(repo, "diff", "--name-only", "--no-renames", "-z", before, commit)
    return [p for p in r.stdout.split("\0") if p]


def plan(flows: list[dict], want, base: str, branch: str, changed) -> dict:
    """Which workflows to wait for, which are skipped, and what is wrong.

    {"wait": {path: name}, "skipped": [{name, why}], "problems": [str]}.
    """
    wait, skipped, problems = {}, [], []
    if want == "all":
        chosen = flows
    else:
        chosen = []
        for n in want:
            hit = [f for f in flows if n in (f["name"], f["path"],
                                             PurePosixPath(f["path"]).name)]
            if not hit:
                problems.append(f"no workflow is named {n!r} "
                                f"(have: {', '.join(f['name'] for f in flows) or 'none'})")
            chosen += [f for f in hit if f not in chosen]
    for f in chosen:
        if f["error"]:
            problems.append(f"{f['path']} could not be read: {f['error']}")
            continue
        if not f["push"]:
            if want != "all":
                problems.append(f"{f['name']!r} has no push trigger, so pushing "
                                f"{branch} cannot run it")
            continue
        on_branch, why_b = fires(f["cfg"], branch, changed)
        on_base, why_base = fires(f["cfg"], base, changed)
        if on_branch:
            wait[f["path"]] = f["name"]
        elif on_base:
            # It would run once the change is on the base, and nothing ran it
            # before: precisely the after-the-fact red this gate is for.
            problems.append(f"{f['name']!r} runs on pushes to {base} but not on "
                            f"{branch} ({why_b}); make its push branches match "
                            f"it, e.g. branches: [\"**\"]")
        else:
            skipped.append({"name": f["name"], "why": why_base or why_b})
    return {"wait": wait, "skipped": skipped, "problems": problems}


# ------------------------------------------------------------------ GitHub API

def gh_api(path: str, timeout: int = 60, raw: bool = False):
    """`gh api <path>`: parsed JSON (or text if `raw`). Raises RuntimeError.

    A raw read is a job log, and GitHub's own step echo colours it: gh refuses
    to print terminal escapes to anything but a terminal unless told to, so
    without the flag every log read failed and no test was ever named.
    """
    argv = [GH, "api", *(["--allow-escape-sequences"] if raw else []), path]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if raw and r.returncode != 0 and "unknown flag" in (r.stderr or ""):
            # An older gh, which never refused them in the first place.
            r = subprocess.run([GH, "api", path], capture_output=True, text=True,
                               timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"gh api {path}: {exc}") from exc
    if r.returncode != 0:
        raise RuntimeError(f"gh api {path}: {(r.stderr or r.stdout).strip()[-300:]}")
    if raw:
        return r.stdout
    try:
        return json.loads(r.stdout or "null")
    except ValueError as exc:
        raise RuntimeError(f"gh api {path}: not JSON ({exc})") from exc


def runs_for(slug: str, commit: str) -> list[dict]:
    got = gh_api(f"repos/{slug}/actions/runs?head_sha={commit}&event=push&per_page=100")
    runs = (got or {}).get("workflow_runs") or []
    return [r for r in runs if r.get("head_sha") == commit
            and (r.get("event") or "push") == "push"]


def _run_path(run: dict) -> str:
    return str(run.get("path") or "").split("@", 1)[0]


def _seen(run: dict) -> tuple:
    return (run.get("id"), run.get("run_attempt"), run.get("status"),
            run.get("conclusion"))


def verdict(runs: list[dict], stale=frozenset()) -> tuple[str, dict | None]:
    """One workflow's runs on one commit -> ("passed"|"pending"|"failed"|"none", run).

    A pass on this exact commit is a pass, whichever attempt it was: re-runs
    and a retried landing's second push all report against the same sha. A
    failure is not, when it is `stale` -- already there, exactly as it is,
    before this landing pushed: Land again after a flaky red would otherwise
    be refused by the very run it is retrying, before GitHub had even
    registered the new one. A stale run re-run on GitHub is new again.
    """
    runs = [r for r in runs if not (_seen(r) in stale and r.get("status") == "completed"
                                    and r.get("conclusion") not in PASSING)]
    if not runs:
        return "none", None
    runs = sorted(runs, key=lambda r: (r.get("id") or 0))
    for r in reversed(runs):
        if r.get("status") == "completed" and r.get("conclusion") in PASSING:
            return "passed", r
    for r in reversed(runs):
        if r.get("status") != "completed":
            return "pending", r
    return "failed", runs[-1]


def first_failure(slug: str, run: dict) -> dict:
    """{job, step, test, url} for a failed run, as much as can be read."""
    out = {"job": "", "step": "", "test": "", "url": run.get("html_url") or ""}
    try:
        jobs = (gh_api(f"repos/{slug}/actions/runs/{run.get('id')}/jobs?per_page=100")
                or {}).get("jobs") or []
    except RuntimeError as exc:
        log.warning("could not list the jobs of run %s: %s", run.get("id"), exc)
        return out
    bad = [j for j in jobs if j.get("conclusion") not in (None, *PASSING)]
    if not bad:
        return out
    job = bad[0]
    out["job"] = job.get("name") or ""
    out["url"] = job.get("html_url") or out["url"]
    steps = [s for s in job.get("steps") or []
             if s.get("conclusion") not in (None, *PASSING)]
    out["step"] = (steps[0].get("name") or "") if steps else ""
    try:
        text = gh_api(f"repos/{slug}/actions/jobs/{job.get('id')}/logs",
                      timeout=120, raw=True)
    except RuntimeError as exc:
        log.warning("could not read the log of job %s: %s", job.get("id"), exc)
        return out
    out["test"] = first_test(str(text))
    return out


def first_test(log_text: str) -> str:
    """The first failing test a job log names, else its first ##[error] line.

    xcodebuild run quietly (as Cadence's CI runs it) never prints "Test Case
    ... failed": it lists the failures under "Failing tests:", one per
    indented line, just before "** TEST FAILED **". That block first, then
    whatever verify recognises, then GitHub's own error annotation.
    """
    lines = [_ANSI.sub("", _STAMP.sub("", ln)) for ln in log_text.splitlines()]
    for i, ln in enumerate(lines):
        if ln.strip() == "Failing tests:":
            for nxt in lines[i + 1:]:
                if nxt[:1] in ("\t", " ") and nxt.strip():
                    return nxt.strip()[:200]
                break
    names = verify.failed_tests("\n".join(lines))
    if names:
        return names[0][:200]
    err = [ln for ln in lines if "##[error]" in ln]
    return err[0].split("##[error]", 1)[1].strip()[:200] if err else ""


# ------------------------------------------------------------------- the gate

class Gate:
    """One landing's CI gate. merge.land calls run() then, at the end, done().

    `outside` is a context-manager factory the caller supplies to step away
    from whatever it holds (the checkout's repo_guard) for the wait itself.
    `on_wait(commit, names)` is told once the wait starts, so the record can
    say "waiting for CI" while it does. `clock` and `sleep` are injected so a
    test can run the deadline without a real 45 minutes.
    """

    def __init__(self, want, push_as: str, *, slug: str = "",
                 deadline_s: float = DEADLINE_S, poll_s: float = POLL_S,
                 clock=time.monotonic, sleep=time.sleep, on_wait=None,
                 outside=None):
        self.want = parse(want)
        self.push_as = push_as
        self.slug = slug
        self.deadline_s, self.poll_s = deadline_s, poll_s
        self.clock, self.sleep = clock, sleep
        self.on_wait = on_wait
        self.outside = outside or contextlib.nullcontext
        self.pushed = ""                # the remote branch, once pushed

    def _out(self, ok: bool, stage: str, detail: str, **more) -> dict:
        return {"ok": ok, "stage": stage, "detail": detail,
                "pushed": self.pushed, **more}

    def run(self, worktree, repo, base: str, before: str, commit: str) -> dict:
        """Push `commit` as push_as and wait for the required runs on it."""
        if not self.want:
            return self._out(True, "", "the gate is off")
        if not self.push_as or self.push_as == base or not \
                self.push_as.startswith("silkworm/"):
            # Never the base, never anything but a task's own namespace.
            return self._out(False, CONFIG, f"refusing to push CI branch "
                                            f"{self.push_as!r} (base is {base!r})")
        slug = self.slug or slug_of(repo)
        if not slug:
            return self._out(False, CONFIG, "origin is not a GitHub repository, "
                                            "so its CI cannot gate this landing")
        flows = workflows(repo, commit)
        changed = changed_files(repo, before, commit)
        p = plan(flows, self.want, base, self.push_as, changed)
        skipped = p["skipped"]
        record = {"commit": commit, "branch": self.push_as, "skipped": skipped,
                  "required": sorted(p["wait"].values())}
        if p["problems"]:
            return self._out(False, CONFIG, "; ".join(p["problems"]), ci=record)
        if not p["wait"]:
            # Nothing this change touches has a workflow that runs on it.
            return self._out(True, "", self._skip_note(skipped)
                             or "no workflow runs for this change", ci=record)

        # What already ran on this commit, before this push: failures there
        # are a previous attempt's, not this one's (see verdict).
        try:
            stale = frozenset(_seen(r) for r in runs_for(slug, commit))
        except RuntimeError:
            stale = frozenset()
        # A copy left by an earlier attempt at another commit goes first. A
        # force-push over it makes GitHub filter paths on the difference
        # between the two -- the base's changes, not this task's -- so a
        # workflow filtered to the task's files would never start.
        there = remote_sha(repo, self.push_as)
        if there and there != commit:
            delete_remote(repo, self.push_as)
        # Force: a retried landing pushes again, and its rebase may have made
        # a different commit. Only ever this task's own branch name.
        try:
            push = _git(worktree, "push", "--force", "origin",
                        f"{commit}:refs/heads/{self.push_as}", timeout=300)
            pushed_ok = push.returncode == 0
            why = (push.stderr or push.stdout).strip()[-300:]
        except (subprocess.TimeoutExpired, OSError) as exc:
            pushed_ok, why = False, str(exc)
        if not pushed_ok:
            return self._out(False, TIMEOUT, f"could not push {self.push_as} for "
                                             f"CI, so nothing was merged: {why}",
                             ci=record, retryable=True)
        self.pushed = self.push_as

        if self.on_wait:
            try:
                self.on_wait(commit, sorted(p["wait"].values()))
            except Exception:
                log.exception("could not record that CI is being waited for")
        with self.outside():
            got = self._wait(slug, commit, p["wait"], stale)
        record.update({k: got[k] for k in ("passed", "pending") if k in got})
        if got["state"] == "passed":
            return self._out(True, "", self._pass_note(got["passed"], skipped),
                             ci=record)
        if got["state"] == "failed":
            f = got["failure"]
            record["failure"] = f
            bits = [f"{f['workflow']!r} failed on {commit[:8]}"]
            if f.get("job"):
                bits.append(f"job {f['job']!r}" + (f", step {f['step']!r}"
                                                   if f.get("step") else ""))
            if f.get("test"):
                bits.append(f"first failing test: {f['test']}")
            if f.get("url"):
                bits.append(f["url"])
            if self._red_on_base(slug, before, f.get("path", "")):
                record["base_red"] = True
                bits.append(f"it is red on {base} at {before[:8]} too")
            return self._out(False, FAILED, "; ".join(bits)
                             + ". Nothing was merged.", ci=record)
        waiting = ", ".join(got.get("pending") or []) or "the required runs"
        extra = f" (last error from gh: {got['error']})" if got.get("error") else ""
        mins = int(self.deadline_s // 60)
        return self._out(False, TIMEOUT,
                         f"CI had not finished on {commit[:8]} after {mins} min: "
                         f"still waiting for {waiting}{extra}. Nothing was merged; "
                         f"land again to resume waiting on the same runs.",
                         ci=record, retryable=True)

    @staticmethod
    def _red_on_base(slug: str, before: str, path: str) -> bool:
        """Whether the same workflow's latest run on the base commit failed:
        said with the refusal, since then the change may not be what broke it."""
        if not path:
            return False
        try:
            runs = [r for r in runs_for(slug, before) if _run_path(r) == path]
        except RuntimeError:
            return False
        return verdict(runs)[0] == "failed"

    def _wait(self, slug: str, commit: str, need: dict, stale=frozenset()) -> dict:
        start = self.clock()
        error = ""
        passed: list[str] = []
        pending = sorted(need.values())
        while True:
            try:
                runs = runs_for(slug, commit)
                error = ""
            except RuntimeError as exc:
                runs, error = None, str(exc)[-200:]
                log.warning("polling CI for %s: %s", commit[:8], error)
            if runs is not None:
                by_path: dict = {}
                for r in runs:
                    by_path.setdefault(_run_path(r), []).append(r)
                if self.want == "all":
                    # Whatever ran on this push counts too, predicted or not.
                    for path, rs in by_path.items():
                        need.setdefault(path, rs[-1].get("name") or path)
                passed, pending, failed = [], [], None
                for path, name in sorted(need.items(), key=lambda kv: kv[1]):
                    state, run = verdict(by_path.get(path, []), stale)
                    if state == "passed":
                        passed.append(name)
                    elif state == "failed" and failed is None:
                        failed = (name, run)
                    elif state in ("pending", "none"):
                        pending.append(name)
                if failed:
                    name, run = failed
                    return {"state": "failed", "passed": passed, "pending": pending,
                            "failure": {"workflow": name, "path": _run_path(run),
                                        **first_failure(slug, run)}}
                if not pending:
                    return {"state": "passed", "passed": passed}
            if self.clock() - start >= self.deadline_s:
                return {"state": "timeout", "passed": passed, "pending": pending,
                        "error": error}
            self.sleep(self.poll_s)

    @staticmethod
    def _skip_note(skipped) -> str:
        if not skipped:
            return ""
        return "not triggered by this change, so counted as passed: " + "; ".join(
            f"{s['name']} ({s['why']})" for s in skipped)

    def _pass_note(self, passed, skipped) -> str:
        note = "CI passed: " + ", ".join(passed)
        skip = self._skip_note(skipped)
        return note + (f"; {skip}" if skip else "")

    def done(self, repo) -> bool:
        """Delete the pushed branch from origin. True if there was none left."""
        if not self.pushed:
            return True
        ok = delete_remote(repo, self.pushed)
        if ok:
            self.pushed = ""
        return ok


def remote_sha(repo, branch: str) -> str:
    """The commit origin has `branch` on, or "" (absent, or unaskable)."""
    try:
        r = _git(repo, "ls-remote", "origin", f"refs/heads/{branch}")
    except (subprocess.TimeoutExpired, OSError):
        return ""
    out = r.stdout.split() if r.returncode == 0 else []
    return out[0] if out else ""


def delete_remote(repo, branch: str) -> bool:
    """Delete `branch` from origin, if it is a task's own and is there."""
    if not branch or not branch.startswith("silkworm/"):
        return False
    try:
        r = _git(repo, "push", "origin", "--delete", branch, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("could not delete %s from origin: %s", branch, exc)
        return False
    if r.returncode == 0:
        return True
    said = (r.stderr or r.stdout or "")
    if "remote ref does not exist" in said:
        # Gone already; drop the stale remote-tracking copy too.
        _git(repo, "update-ref", "-d", f"refs/remotes/origin/{branch}")
        return True
    log.warning("could not delete %s from origin: %s", branch, said.strip()[-200:])
    return False

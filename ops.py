"""Named operations on the mini: what the operator runs instead of telling you to.

On 2026-10-26 the operator handed the owner three shell steps it could not take
itself -- `silkworm tailscale on` (serve still pointed at the mini's old
Tailscale name after a reboot), reinstalling trader's com.traderjoe.pooldata
launchd job after its plist changed, and trader's one-off
`scripts/refetch_revisions.py ... --apply` after a close. "You should be able to
run these yourself."

It still gets no shell. This is a fixed registry, in code: each entry names its
project, the directory it runs in, the exact argv it runs (each argument it
takes validated, and nothing that looks like shell accepted at all), a timeout,
whether it changes anything, and what it is for. `silkworm ops run <name>` asks
the bot, which builds the argv from the entry (`plan`) and runs it with no shell
(`run`) -- so an argument can only ever be one argv element, in the place the
entry put it.

Who may run what (`refusal`):

* An op that only reads runs for anyone, any turn, the daily sweep included.
* One that changes something runs from inside a turn only on your word: the
  grant of a turn you started with your own Slack message in the operator's
  home thread (operator_role.Grants), with --notes saying what you asked for.
  A sweep, a wake-up, the web chat or any other turn has no grant and is
  refused. Your own terminal and the dashboard, which carry no turn at all,
  are you.
* An op marked `named` must also be named in that message -- trader's (they
  are its live configuration, which otherwise waits for `!approve`) and the
  one that exposes the dashboard on the tailnet.

Every run, and every refusal of a well-formed one, is appended to the ops log
(logs/ops.jsonl): who, on whose word, the argv of each step, exit codes and
the tail of the output.
"""

from __future__ import annotations

import contextlib
import json
import os
import plistlib
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

#: The characters an op argument may contain. Everything else -- spaces,
#: quotes, `;|&$<>()` and the rest -- is refused before any op reads it. No
#: shell ever sees an argument, but an argument that looks like shell is a
#: sign of someone reaching for one, and is never what an entry wants.
SAFE_ARG = re.compile(r"^[A-Za-z0-9._,:=@+/-]+$")
#: Per argument, and in all.
MAX_ARG = 64
MAX_ARGS = 60
#: How much of each step's output is kept, in the log and the reply.
TAIL_CHARS = 2000


class Refused(Exception):
    """The request names no op, or arguments the op does not take."""


@dataclass
class Step:
    argv: list
    #: A failure here does not stop the run (launchctl bootout of a job that
    #: is not loaded exits non-zero, and that is fine).
    may_fail: bool = False


@dataclass
class Plan:
    """One op, its arguments checked, and exactly what running it will do."""
    name: str
    project: str
    args: list
    cwd: str
    steps: list
    timeout: int
    read_only: bool
    #: Words any one of which, in your message, names this run. Empty: the op
    #: need not be named (Op.named is False).
    names: tuple = ()
    #: verify.project_lock's project: a test run shares the lock every other
    #: run of that project's suite takes.
    lock: str = ""


@dataclass
class Context:
    """What an op needs to know about this machine. The bot fills it in;
    tests fill it with fakes."""
    #: slug -> the project record (projects.json), or None.
    project: Callable[[str], dict | None]
    python: str
    silkworm_bin: str
    uid: int
    launch_agents: Path


@dataclass(frozen=True)
class Op:
    name: str
    project: str
    description: str
    usage: str
    timeout: int
    #: True: never changes anything. False: always does. None: it depends on
    #: the arguments (`writes`).
    read_only: bool | None
    build: Callable
    #: Changing what it changes needs your message to name it.
    named: bool = False
    writes: Callable[[list], bool] | None = None

    def mutating(self, args) -> bool:
        if self.read_only is None:
            return bool(self.writes and self.writes(list(args)))
        return not self.read_only


# --- the entries -----------------------------------------------------------------

def _no_args(name, args):
    if args:
        raise Refused(f"`{name}` takes no arguments")


def _project_rec(ctx: Context, slug: str) -> dict:
    rec = ctx.project(slug) or None
    if not rec:
        raise Refused(f"no project {slug!r}")
    return rec


def _cwd(rec: dict, slug: str) -> str:
    cwd = str(((rec or {}).get("scope") or {}).get("cwd") or "")
    if not cwd or not Path(cwd).is_dir():
        raise Refused(f"{slug} has no checkout on this machine ({cwd or 'none set'})")
    return cwd


def _tailscale(sub: str):
    def build(op, args, ctx):
        _no_args(op.name, args)
        return [Step([ctx.python, ctx.silkworm_bin, "tailscale", sub])], \
            str(Path(ctx.silkworm_bin).resolve().parent.parent)
    return build


#: Trader's launchd jobs, and only those, are its to reinstall.
LAUNCHD_PREFIX = "com.traderjoe."


def _git(cwd, *args) -> tuple:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                           timeout=30, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


#: A launchd job's label: the plist's file name, without .plist.
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*$")


def _launchd_reinstall(op, args, ctx):
    if len(args) != 1:
        raise Refused(f"usage: {op.name} {op.usage}")
    job = args[0][:-len(".plist")] if args[0].endswith(".plist") else args[0]
    if not _LABEL.match(job):
        raise Refused(f"`{args[0]}` is not a launchd job label (e.g. com.traderjoe.pooldata)")
    if not job.startswith(LAUNCHD_PREFIX):
        # Trader's own jobs only: a plist named for another (Silkworm's own
        # agent, say) would replace it.
        raise Refused(f"`{job}` is not one of trader's jobs ({LAUNCHD_PREFIX}…)")
    cwd = _cwd(_project_rec(ctx, op.project), op.project)
    if (Path(cwd) / "ops").is_symlink():
        raise Refused(f"{op.project}'s ops/ is a symlink; only its own directory counts")
    ops_dir = (Path(cwd) / "ops").resolve()
    src = ops_dir / f"{job}.plist"
    # Only a plist trader keeps in its own ops/: resolved, so a symlink there
    # pointing anywhere else is refused too.
    if not src.is_file() or src.resolve().parent != ops_dir:
        have = sorted(p.stem for p in ops_dir.glob("*.plist")) if ops_dir.is_dir() else []
        raise Refused(f"{op.project} has no ops/{job}.plist"
                      + (f"; it has: {', '.join(have)}" if have else ""))
    try:
        with open(src, "rb") as f:
            label = (plistlib.load(f) or {}).get("Label")
    except Exception as e:
        raise Refused(f"ops/{job}.plist does not parse as a plist: {e}") from e
    if label != job:
        raise Refused(f"ops/{job}.plist's Label is {label!r}, not {job!r}")
    # What is installed is what the project committed: not an edit sitting
    # in the checkout, which nobody reviewed.
    rel = f"ops/{job}.plist"
    if (_git(cwd, "ls-files", "--error-unmatch", "--", rel)[0] != 0
            or _git(cwd, "diff", "--quiet", "HEAD", "--", rel)[0] != 0):
        raise Refused(f"{rel} is not committed as it stands in {cwd}; only a committed, "
                      "unchanged plist is installed")
    dst = Path(ctx.launch_agents) / f"{job}.plist"
    if dst.is_symlink():
        raise Refused(f"{dst} is a symlink; refusing to write through it")
    domain = f"gui/{ctx.uid}"
    return [
        Step(["plutil", "-lint", str(src)]),
        Step(["cp", str(src), str(dst)]),
        Step(["launchctl", "bootout", f"{domain}/{job}"], may_fail=True),
        Step(["launchctl", "bootstrap", domain, str(dst)]),
    ], cwd


#: A ticker as trader's store spells them: BRK.A, BF-B, CRWD.
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9]{0,9}(?:[.-][A-Z0-9]{1,4})?$")


def _refetch(op, args, ctx):
    syms, flags, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a == "--symbols":
            i += 1
            while i < len(args) and not args[i].startswith("--"):
                s = args[i].upper()
                if not _SYMBOL.match(s):
                    raise Refused(f"`{args[i]}` is not a ticker")
                syms.append(s)
                i += 1
            continue
        if a in ("--dividends", "--apply"):
            if a not in flags:
                flags.append(a)
            i += 1
            continue
        raise Refused(f"`{a}` is not an option {op.name} passes on; usage: {op.name} {op.usage}")
    if not syms:
        raise Refused(f"say which symbols: {op.name} {op.usage}")
    cwd = _cwd(_project_rec(ctx, op.project), op.project)
    py = Path(cwd) / ".venv" / "bin" / "python3"
    if not py.exists():
        raise Refused(f"{op.project} has no .venv/bin/python3 in {cwd}")
    # In the order the script documents; its own guards stay: --apply refuses
    # while a session is open, takes update_data's lock and backs up first.
    argv = [str(py), "scripts/refetch_revisions.py", "--symbols", *dict.fromkeys(syms)]
    argv += [f for f in ("--dividends", "--apply") if f in flags]
    return [Step(argv)], cwd


_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _project_test(op, args, ctx):
    if len(args) != 1 or not _SLUG.match(args[0].lower()):
        raise Refused(f"usage: {op.name} {op.usage}")
    slug = args[0].lower()
    rec = _project_rec(ctx, slug)
    cmd = str(rec.get("test_cmd") or "").strip()
    if not cmd:
        raise Refused(f"{slug} has no test command set")
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        raise Refused(f"{slug}'s test command does not parse: {e}") from e
    # Split as verify.run splits it, and run without a shell, as it does.
    return [Step(argv)], _cwd(rec, slug)


#: The shortest last segment of a job label that counts as naming it on its
#: own ("pooldata" for com.traderjoe.pooldata): shorter ones are words a
#: message holds by chance.
SHORT_NAME_MIN = 6


def _names(op, args) -> tuple:
    """What naming this run in your message looks like: the op, or for a
    reinstall the job it acts on. Matched as whole words (`named`)."""
    if op.name == "trader-launchd-reinstall" and args:
        job = args[0][:-len(".plist")] if args[0].endswith(".plist") else args[0]
        short = job.rsplit(".", 1)[-1]
        return (op.name, job) + ((short,) if len(short) >= SHORT_NAME_MIN else ())
    if op.name == "trader-refetch-revisions":
        return (op.name, "refetch_revisions")
    return (op.name,)


def _norm(text: str) -> str:
    """Lower case, with hyphens, underscores and runs of space as one space:
    "tailscale-on", "tailscale on" and "Tailscale_On" are the same name."""
    return " ".join(re.sub(r"[-_\s]+", " ", str(text or "").lower()).split())


def named(names, text: str) -> bool:
    """Whether `text` holds one of `names` as a whole word or phrase -- not
    inside a longer word ("tailscale" is not "tailscale-on")."""
    said = _norm(text)
    return any(re.search(r"(?<![a-z0-9])" + re.escape(_norm(n)) + r"(?![a-z0-9])", said)
               for n in names if _norm(n))


REGISTRY: dict[str, Op] = {op.name: op for op in (
    Op("tailscale-status", "silkworm",
       "check the phone's way to the dashboard: Tailscale running, serve under this "
       "machine's current name, tailnet-only, Funnel off (`silkworm tailscale status`)",
       "", 60, True, _tailscale("status")),
    Op("tailscale-on", "silkworm",
       "serve the dashboard to the tailnet under this machine's current Tailscale name "
       "(`silkworm tailscale on`; tailnet-only, never Funnel), then check it",
       "", 120, False, _tailscale("on"), named=True),
    Op("trader-launchd-reinstall", "trader",
       "reinstall one of trader's launchd jobs from its ops/<job>.plist after it "
       "changed: plutil -lint, copy to ~/Library/LaunchAgents, launchctl bootout "
       "then bootstrap",
       "<job label, e.g. com.traderjoe.pooldata>", 120, False, _launchd_reinstall,
       named=True),
    Op("trader-refetch-revisions", "trader",
       "trader's scripts/refetch_revisions.py: a plan of Yahoo's revised bars for the "
       "symbols (writes nothing); with --apply it writes them (refused by the script "
       "while the market is open; takes update_data's lock and backs up first)",
       "--symbols SYM [SYM …] [--dividends] [--apply]", 1800, None, _refetch,
       named=True, writes=lambda args: "--apply" in args),
    Op("project-test", "",
       "run a project's own test command (its test_cmd) in its checkout, under the "
       "lock every run of that suite takes",
       "<project>", 1800, False, _project_test),
)}


def describe() -> list[dict]:
    return [{"name": op.name, "project": op.project, "usage": op.usage,
             "timeout": op.timeout,
             "read_only": ("with --apply it writes" if op.read_only is None
                           else op.read_only),
             "named": op.named, "description": op.description}
            for op in REGISTRY.values()]


def prompt_lines(bin_: str = "silkworm") -> str:
    """The ops, one line each, for the operator's prompts."""
    out = []
    for op in REGISTRY.values():
        kind = ("reads only" if op.read_only is True else
                "reads only; --apply writes" if op.read_only is None else "changes things")
        out.append(f"- `{op.name}{(' ' + op.usage) if op.usage else ''}` ({kind}): "
                   f"{op.description}")
    return "\n".join(out)


def arg_refusal(args) -> str:
    """Why these arguments are refused before any op reads them, or ""."""
    if len(args) > MAX_ARGS:
        return f"at most {MAX_ARGS} arguments"
    for a in args:
        if not isinstance(a, str) or not a:
            return "an empty argument"
        if len(a) > MAX_ARG:
            return f"`{a[:MAX_ARG]}…` is longer than {MAX_ARG} characters"
        if not SAFE_ARG.match(a):
            bad = "".join(sorted({c for c in a if not SAFE_ARG.match(c)}))
            return (f"`{a}` holds characters an op never takes ({bad!r}): no "
                    "shell runs here, and an argument is never shell")
    return ""


def lookup(name: str) -> Op:
    op = REGISTRY.get(str(name or ""))
    if not op:
        raise Refused(f"no op {name!r}; the ops are: {', '.join(REGISTRY)} "
                      "(`silkworm ops list`)")
    return op


def mutating(name: str, args) -> bool:
    """Whether running `name` with `args` would change anything. Unknown ops
    count as changing things."""
    op = REGISTRY.get(str(name or ""))
    return True if not op else op.mutating(args)


def plan(name: str, args, ctx: Context) -> Plan:
    """Exactly what running `name` with `args` would do. Raises Refused."""
    op = lookup(name)
    args = list(args or [])
    why = arg_refusal(args)
    if why:
        raise Refused(why)
    steps, cwd = op.build(op, args, ctx)
    project = op.project or (args[0].lower() if args else "")
    names = _names(op, args) if op.named else ()
    return Plan(name=op.name, project=project, args=args, cwd=str(cwd), steps=steps,
                timeout=op.timeout, read_only=not op.mutating(args), names=names,
                lock=project if op.name == "project-test" else "")


def refusal(p: Plan, *, in_turn: bool, grant: dict | None, notes: str) -> str:
    """Why this run may not go ahead, or "" if it may.

    `in_turn`: the request came from inside a turn (the CLI stamped it with a
    role or thread). Your terminal and the dashboard carry none: they are you.
    `grant`: the live grant of the turn you started in the operator's home
    thread (bot.user_grant), or None.
    """
    if p.read_only or not in_turn:
        return ""
    if not grant:
        return (f"refused: `{p.name}` changes something on this machine, so from "
                "inside a turn it runs only on the user's word -- a turn they started "
                "with their own Slack message in the operator's thread -- never in a "
                "sweep, a wake-up or any other turn. Ask them to tell you to run it "
                f"there, or to run `silkworm ops run {p.name}` themselves")
    if not str(notes or "").strip():
        return ("refused: acting on the user's word needs --notes quoting or "
                "paraphrasing what they asked for")
    if p.names:
        if not named(p.names, grant.get("text") or ""):
            what = ("trader's live configuration" if p.name.startswith("trader-")
                    else "what is exposed on the tailnet")
            return (f"refused: `{p.name}` changes {what}, so the user's message has to "
                    f"name it (one of: {', '.join(p.names)}); ask them to say so")
    return ""


# --- running ---------------------------------------------------------------------

_running: set = set()
_running_guard = threading.Lock()


def _scrub(text: str) -> str:
    try:
        import health_probes
        return health_probes.scrub(text)
    except Exception:
        return text


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _tail(text: str, n: int = TAIL_CHARS) -> str:
    # Colour codes (silkworm tailscale prints them) are noise in a Slack reply.
    text = _ANSI.sub("", text or "").strip()
    return text if len(text) <= n else "…" + text[-n:]


def run(p: Plan, *, env: dict | None = None, runner=None, clock=time.monotonic,
        log_path=None, record: dict | None = None) -> dict:
    """Run a plan's steps in order, without a shell, within its timeout.

    Stops at the first step that fails unless it `may_fail`. Returns (and logs)
    {op, args, cwd, steps: [{argv, exit, tail}], exit, tail, ok}. `exit` is
    None when a step could not start or ran out of time. One run of an op at a
    time: a second is refused while the first is still going.
    """
    runner = runner or subprocess.run
    key = (p.name, p.project)
    with _running_guard:
        if key in _running:
            raise Refused(f"`{p.name}` is already running for {p.project or 'this machine'}")
        _running.add(key)
    started, done, code = clock(), [], 0
    try:
        with contextlib.ExitStack() as held:
            try:
                held.enter_context(_project_lock(p.lock))
            except Exception as e:      # verify.Busy: the suite is running
                raise Refused(f"not run: {e}; try again when it has finished") from e
            for step in p.steps:
                left = p.timeout - (clock() - started)
                if left <= 0:
                    done.append({"argv": step.argv, "exit": None,
                                 "tail": f"not run: the op's {p.timeout}s were used up"})
                    code = None
                    break
                try:
                    r = runner(list(step.argv), cwd=p.cwd, env=env, capture_output=True,
                               text=True, timeout=left, stdin=subprocess.DEVNULL)
                    rc = r.returncode
                    out = ((r.stdout or "") + ("\n" if r.stdout and r.stderr else "")
                           + (r.stderr or ""))
                except subprocess.TimeoutExpired:
                    rc, out = None, f"timed out after {p.timeout}s"
                except (OSError, ValueError) as e:
                    rc, out = None, f"could not run it: {e}"
                done.append({"argv": list(step.argv), "exit": rc, "tail": _tail(_scrub(out))})
                if rc != 0 and not step.may_fail:
                    code = rc
                    break
    finally:
        with _running_guard:
            _running.discard(key)
    rec = dict(record or {})
    rec.update(at=time.time(), op=p.name, project=p.project, args=p.args, cwd=p.cwd,
               read_only=p.read_only, steps=done, exit=code, ok=code == 0,
               tail=done[-1]["tail"] if done else "",
               seconds=round(clock() - started, 1))
    if log_path:
        append_log(log_path, rec)
    return rec


def _project_lock(project: str):
    """The lock every run of `project`'s suite takes, without waiting for it:
    the CLI is waiting on this run's answer, not on another run's."""
    if not project:
        return contextlib.nullcontext()
    import verify
    return verify.project_lock(project, wait=False)


def append_log(path, rec: dict) -> None:
    """One JSON line per run or refusal. A log that cannot be written never
    costs the run its answer."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except OSError:
        pass


def read_log(path, n: int = 20) -> list[dict]:
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-max(1, n):]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def format_run(rec: dict) -> str:
    """A run as the CLI prints it, and the operator quotes it: each step's
    argv and exit code, then the tail of the last one's output."""
    lines = []
    for s in rec.get("steps") or []:
        code = "?" if s.get("exit") is None else s["exit"]
        lines.append(f"$ {shlex.join(s.get('argv') or [])}   [exit {code}]")
    tail = (rec.get("tail") or "").strip()
    if tail:
        lines.append(tail)
    return "\n".join(lines)

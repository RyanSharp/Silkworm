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
  one that exposes the dashboard on the tailnet -- unless it is an entry of
  the pending plan the operator showed you and your message approved it.

On 2026-11-15 "go with your recommendations, and run the trader ops" was
refused for all four of trader's ops, because it named none of them. So the
operator records the ops it proposes as a numbered plan (`ops plan`,
operator_role.State.set_plan), and a plain "yes", "do 2" or "all but 1" in
the message of yours that answers it runs exactly those entries (one that is
no answer -- "file it" -- leaves it waiting; a no spends it), each once, with exactly the
arguments you were shown (operator_role.plan_refusal; the words are read by
operator_role.plan_approval, deterministically, never by a model). A plan
lives a day, is replaced by the next, and is never answered by a sweep or a
wake-up, which carry no grant.

On 2026-11-14 two more of the owner's decisions needed shell steps: retiring
trader's cash-secured-put shadow job (com.traderjoe.shadowoptions) and switching
its momentum sleeve to the point-in-time universe (MOMENTUM_UNIVERSE=pit in
trader's .env). trader-launchd-retire and trader-env-set carry those out: the
first moves the plist to a dated backup, never deletes it; the second sets only
keys in ENV_KEYS, to values listed there, backs .env up first, and never prints
another line of it.

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
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

#: The characters an op argument may contain. Everything else -- spaces,
#: quotes, `;|&$<>()` and the rest -- is refused before any op reads it. No
#: shell ever sees an argument, but an argument that looks like shell is a
#: sign of someone reaching for one, and is never what an entry wants.
SAFE_ARG = re.compile(r"[A-Za-z0-9._,:=@+/-]+")   # fullmatch: `$` lets "x\n" through
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
    #: The step succeeds only if the command fails (exits non-zero): `launchctl
    #: print` of a job that should be gone.
    must_fail: bool = False
    #: Its output is not kept (`launchctl print` of a loaded job lists its
    #: environment, which can hold credentials): only its exit code.
    quiet: bool = False


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
    #: Words every one of which your message must also hold: trader-env-set's
    #: value, so "what is the momentum universe on?" names no change.
    also: tuple = ()
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
    if not _LABEL.fullmatch(job):
        raise Refused(f"`{args[0]}` is not a launchd job label (e.g. com.traderjoe.pooldata)")
    if not job.startswith(LAUNCHD_PREFIX):
        # Trader's own jobs only: a plist named for another (Silkworm's own
        # agent, say) would replace it.
        raise Refused(f"`{job}` is not one of trader's jobs ({LAUNCHD_PREFIX}…)")
    cwd = _cwd(_project_rec(ctx, op.project), op.project)
    retired = _retired_copies(cwd, job)
    if retired:
        # A job retired on your word stays retired: its source plist is still
        # committed in ops/, so a reinstall would quietly bring it back.
        # Undoing a retirement is moving its copy out of data/retired-launchd
        # by hand first -- a deliberate step, not a side effect.
        raise Refused(f"{job} was retired ({', '.join(retired)} in {RETIRED_DIR}); "
                      "move that out by hand first to bring it back")
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


def _job_arg(op, args) -> str:
    """The one job label a launchd op takes ("…plist" allowed), checked to be
    a label and one of trader's."""
    if len(args) != 1:
        raise Refused(f"usage: {op.name} {op.usage}")
    job = args[0][:-len(".plist")] if args[0].endswith(".plist") else args[0]
    if not _LABEL.fullmatch(job):
        raise Refused(f"`{args[0]}` is not a launchd job label (e.g. com.traderjoe.pooldata)")
    if not job.startswith(LAUNCHD_PREFIX):
        # Trader's own jobs only: retiring another (Silkworm's own agent,
        # say) is not trader's to do.
        raise Refused(f"`{job}` is not one of trader's jobs ({LAUNCHD_PREFIX}…)")
    return job


#: Where a retired job's plist goes, inside trader's checkout (data/ is
#: gitignored there): moved, never deleted. trader-launchd-reinstall refuses a
#: job with a copy here, so retiring is undone only by moving that copy out
#: by hand, then reinstalling.
RETIRED_DIR = "data/retired-launchd"


def _retired_copies(cwd, job: str) -> list:
    """The file names in trader's data/retired-launchd that retired `job`
    (`<job>.<date-time>[-n].plist`, as _launchd_retire names them)."""
    d = Path(cwd) / RETIRED_DIR
    if not d.is_dir():
        return []
    stamp = re.compile(re.escape(job) + r"\.\d{8}-\d{6}(?:-\d+)?\.plist")
    return sorted(p.name for p in d.iterdir() if stamp.fullmatch(p.name))


def _stamp(clock=time.time) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))


def _free(path: Path) -> Path:
    """`path`, or with -1, -2, ... before its suffix if something is there:
    never over an earlier backup when two runs fall in one second."""
    path, n = Path(path), 0
    cand = path
    while cand.exists() or cand.is_symlink():
        n += 1
        cand = path.with_name(f"{path.stem}-{n}{path.suffix}")
    return cand


def _launchd_retire(op, args, ctx):
    job = _job_arg(op, args)
    cwd = _cwd(_project_rec(ctx, op.project), op.project)
    agents = Path(ctx.launch_agents)
    src = agents / f"{job}.plist"
    if src.is_symlink() or not src.is_file():
        have = sorted(p.stem for p in agents.glob(f"{LAUNCHD_PREFIX}*.plist")
                      if p.is_file() and not p.is_symlink()) if agents.is_dir() else []
        raise Refused(f"no installed {job} ({src} is not a plain file)"
                      + (f"; installed: {', '.join(have)}" if have else ""))
    try:
        with open(src, "rb") as f:
            label = (plistlib.load(f) or {}).get("Label")
    except Exception as e:
        raise Refused(f"{src} does not parse as a plist: {e}") from e
    if label != job:
        # bootout and print would name another job, both "fail" as fine,
        # and the plist would move with the real job still loaded.
        raise Refused(f"{src}'s Label is {label!r}, not {job!r}; retire it by hand")
    data = Path(cwd) / "data"
    if not data.is_dir():
        raise Refused(f"{op.project} has no data/ in {cwd} to keep the retired plist in")
    retired = data.resolve() / "retired-launchd"
    if (data / "retired-launchd").is_symlink():
        raise Refused(f"{cwd}/{RETIRED_DIR} is a symlink; refusing to write through it")
    dst = _free(retired / f"{job}.{_stamp()}.plist")
    return [
        # May fail: a job already not loaded has nothing to boot out.
        Step(["launchctl", "bootout", f"gui/{ctx.uid}/{job}"], may_fail=True),
        # But it must be gone before its plist is: a job left loaded with its
        # plist moved away keeps running, unseen, until the next login.
        Step(["launchctl", "print", f"gui/{ctx.uid}/{job}"], must_fail=True, quiet=True),
        Step(["mkdir", "-p", str(retired)]),
        # -n: never over anything; the plan checked nothing is there.
        Step(["mv", "-n", str(src), str(dst)]),
        # mv -n exits 0 even when it did not move: make sure it did.
        Step(["test", "-f", str(dst)]),
    ], cwd


#: The keys of trader's .env an op may set, each with the values it may take.
#: Anything else in that file -- its credentials -- is never read out or
#: changed.
ENV_KEYS: dict = {
    # Which universe the momentum sleeve trades (trader/research/
    # deployed_universe.py): the hand-built list, or the point-in-time one.
    "MOMENTUM_UNIVERSE": ("static", "pit"),
}
#: Where .env backups go, inside trader's (gitignored) data/.
ENV_BACKUPS = "data/env-backups"


def _env_set(op, args, ctx):
    if len(args) != 2:
        raise Refused(f"usage: {op.name} {op.usage}")
    key, value = args
    if key not in ENV_KEYS:
        raise Refused(f"`{key}` is not a key {op.name} sets; it sets: "
                      + ", ".join(ENV_KEYS))
    if value not in ENV_KEYS[key]:
        raise Refused(f"`{value}` is not a value {key} takes: one of "
                      + ", ".join(ENV_KEYS[key]))
    cwd = _cwd(_project_rec(ctx, op.project), op.project)
    env = Path(cwd) / ".env"
    if env.is_symlink() or not env.is_file():
        raise Refused(f"{cwd}/.env is not a plain file")
    data = Path(cwd) / "data"
    if not data.is_dir():
        raise Refused(f"{op.project} has no data/ in {cwd} to back .env up into")
    if (data / "env-backups").is_symlink():
        raise Refused(f"{cwd}/{ENV_BACKUPS} is a symlink; refusing to write through it")
    backup = data.resolve() / "env-backups" / f"env.{_stamp()}.bak"
    return [Step([ctx.python, "-I", str(Path(__file__).resolve()), "env-set",
                  str(env), str(backup), key, value])], cwd


def env_set(env_path, backup_path, key: str, value: str) -> tuple:
    """Set `key` to `value` in the .env at env_path, after copying it (mode
    0600) to backup_path. Changes only that key's line, or adds one; every
    other byte stays. Returns (old value or None, the line printed). Never
    returns or prints any other line: the file holds secrets."""
    if key not in ENV_KEYS or value not in ENV_KEYS[key]:
        raise Refused(f"{key}={value} is not allowed")
    env_path, backup_path = Path(env_path), Path(backup_path)
    if env_path.is_symlink() or not env_path.is_file():
        raise Refused(f"{env_path} is not a plain file")
    raw = env_path.read_bytes()
    text = raw.decode("utf-8")
    # Lines end at \n and only there, as dotenv reads them. splitlines() also
    # splits at \f, a lone \r, \u2028 and the like -- rewriting one of those
    # "lines" would join the key's line to the next, which may be a secret.
    odd = set("\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029\ufeff") & set(text)
    if odd or re.search(r"\r(?!\n)", text):
        raise Refused(".env holds line breaks other than \\n (or a BOM); fix it by hand")
    lines = re.split(r"(?<=\n)", text)
    if lines and lines[-1] == "":
        lines.pop()
    pat = re.compile(r"^(\s*(?:export\s+)?)" + re.escape(key) + r"\s*=(.*?)(\r?\n)?$")
    hits = [i for i, line in enumerate(lines) if pat.match(line)]
    if len(hits) > 1:
        raise Refused(f"{key} is set on {len(hits)} lines of .env; fix that by hand")
    old = None
    if hits:
        m = pat.match(lines[hits[0]])
        old = m.group(2).split(" #", 1)[0].strip().strip("'\"")
    shown = old if old is None or (SAFE_ARG.fullmatch(old) and len(old) <= MAX_ARG) else "(unprintable)"
    if old == value:
        return old, f"{key}: already {value}; .env unchanged, nothing backed up"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = _free(backup_path)
    fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    if hits:
        m = pat.match(lines[hits[0]])
        lines[hits[0]] = f"{m.group(1)}{key}={value}{m.group(3) or ''}"
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={value}\n")
    tmp = env_path.with_name(f".env.ops-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                 env_path.stat().st_mode & 0o777)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write("".join(lines).encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, env_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return old, (f"{key}: {shown if old is not None else '(unset)'} -> {value} "
                 f"(backup: {backup_path}). It takes effect at the trading daemon's next "
                 "restart (the weekly Sunday restart); the daemon was not restarted")


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
                if not _SYMBOL.fullmatch(s):
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
    if len(args) != 1 or not _SLUG.fullmatch(args[0].lower()):
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
    if op.name == "trader-launchd-retire" and args:
        # Retiring is not undone by running it again: only the op or the full
        # label names it. A last part ("restart", "heartbeat") is a word said
        # in passing.
        job = args[0][:-len(".plist")] if args[0].endswith(".plist") else args[0]
        return (op.name, job)
    if op.name == "trader-launchd-reinstall" and args:
        job = args[0][:-len(".plist")] if args[0].endswith(".plist") else args[0]
        short = job.rsplit(".", 1)[-1]
        return (op.name, job) + ((short,) if len(short) >= SHORT_NAME_MIN else ())
    if op.name == "trader-env-set" and args:
        return (op.name, args[0])
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
       "then bootstrap (refused for a job retired into data/retired-launchd)",
       "<job label, e.g. com.traderjoe.pooldata>", 120, False, _launchd_reinstall,
       named=True),
    Op("trader-refetch-revisions", "trader",
       "trader's scripts/refetch_revisions.py: a plan of Yahoo's revised bars for the "
       "symbols (writes nothing); with --apply it writes them (refused by the script "
       "while the market is open; takes update_data's lock and backs up first)",
       "--symbols SYM [SYM …] [--dividends] [--apply]", 1800, None, _refetch,
       named=True, writes=lambda args: "--apply" in args),
    Op("trader-launchd-retire", "trader",
       "retire one of trader's installed launchd jobs: launchctl bootout, then move "
       "~/Library/LaunchAgents/<job>.plist to a dated backup in trader's "
       f"{RETIRED_DIR}/ (never deleted)",
       "<job label, e.g. com.traderjoe.shadowoptions>", 60, False, _launchd_retire,
       named=True),
    Op("trader-env-set", "trader",
       "set one allowed key of trader's .env (backed up first to "
       f"{ENV_BACKUPS}/; only that line changes, no other line is shown): "
       + "; ".join(f"{k} in {{{', '.join(v)}}}" for k, v in ENV_KEYS.items())
       + ". Takes effect at the trading daemon's next restart (the weekly Sunday "
       "restart); it does not restart the daemon",
       "<KEY> <VALUE>", 30, False, _env_set, named=True),
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
        if not SAFE_ARG.fullmatch(a):
            bad = "".join(sorted({c for c in a if not SAFE_ARG.fullmatch(c)}))
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
    # Setting a key needs its value said too.
    also = (args[1],) if op.named and op.name == "trader-env-set" else ()
    return Plan(name=op.name, project=project, args=args, cwd=str(cwd), steps=steps,
                timeout=op.timeout, read_only=not op.mutating(args), names=names, also=also,
                lock=project if op.name == "project-test" else "")


def refusal(p: Plan, *, in_turn: bool, grant: dict | None, notes: str,
            planned: bool = False) -> str:
    """Why this run may not go ahead, or "" if it may.

    `in_turn`: the request came from inside a turn (the CLI stamped it with a
    role or thread). Your terminal and the dashboard carry none: they are you.
    `grant`: the live grant of the turn you started in the operator's home
    thread (bot.user_grant), or None.
    `planned`: this exact run is an entry of the plan your message approved
    (operator_role.plan_refusal found nothing wrong), which names it for you:
    the plan showed you the op and its arguments, and you said yes.
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
    if planned:
        return ""
    if p.names:
        if not named(p.names, grant.get("text") or ""):
            what = ("trader's live configuration" if p.name.startswith("trader-")
                    else "what is exposed on the tailnet")
            return (f"refused: `{p.name}` changes {what}, so the user's message has to "
                    f"name it (one of: {', '.join(p.names)}); ask them to say so")
    if p.also and not all(named((a,), grant.get("text") or "") for a in p.also):
        return (f"refused: `{p.name}` changes trader's live configuration, so the user's "
                f"message has to say the value too ({', '.join(p.also)}); ask them to say so")
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
                if step.quiet and rc is not None:
                    out = "(output not kept)"
                done.append({"argv": list(step.argv), "exit": rc, "tail": _tail(_scrub(out))})
                failed = (rc is None or rc == 0) if step.must_fail else rc != 0
                if failed and not step.may_fail:
                    # A must_fail step that exited 0 is a failed run: exit 1.
                    code = (1 if rc == 0 else rc) if step.must_fail else rc
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


def _main(argv) -> int:
    """`python -I ops.py env-set <.env> <backup> <KEY> <VALUE>`: the one step
    trader-env-set runs. Prints only the changed key's old -> new."""
    if len(argv) != 5 or argv[0] != "env-set":
        print("usage: ops.py env-set <.env> <backup> <KEY> <VALUE>", file=sys.stderr)
        return 2
    try:
        _, line = env_set(*argv[1:])
    except (Refused, OSError, UnicodeDecodeError) as e:
        # The error names paths and the key, never a line of the file.
        print(f"refused: {type(e).__name__}: {e}"[:300], file=sys.stderr)
        return 1
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))

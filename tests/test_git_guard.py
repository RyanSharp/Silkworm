"""The implementor git guard.

Run by tests/run.py with every other tests/test_*.py; see tests/harness.py."""

from harness import *  # noqa: F401,F403


# --- the implementor git guard -------------------------------------------------
# On 2026-10-02 a Cadence implementor merged its own commit into the checkout's
# main and pushed it to origin before verification or review had run. The
# prompt said not to; nothing enforced it. These hooks make git refuse it, for
# an implementor's turn only -- the user and the bot's own landing must not
# notice they exist.

def test_implementor_git_guard():
    import git_guard as GG
    import merge as M
    print("\nimplementor git guard: git refuses what only landing may do")
    root = Path(tempfile.mkdtemp())
    origin, repo = root / "origin.git", root / "repo"
    tid = "tsk_guard1"
    clean = GG.strip(dict(os.environ))           # the user, the bot's landing
    agent = {**clean, **GG.env_for("implementor", tid)}

    def git(cwd, *a, env=None):
        return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a],
                              cwd=str(cwd), capture_output=True, text=True,
                              env=env if env is not None else clean)

    def sha(ref, cwd=repo):
        return git(cwd, "rev-parse", "-q", "--verify", ref).stdout.strip()

    check("this git has reference-transaction (2.28+)", GG.git_version() >= GG.MIN_GIT,
          str(GG.git_version()))
    git(root, "init", "-q", "--bare", "-b", "main", str(origin))
    git(root, "init", "-q", "-b", "main", str(repo))
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "a")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")

    # Hooks already in place, which must be chained rather than lost: each
    # records that it ran, and what it was given.
    hooks = repo / ".git" / "hooks"
    log_ = root / "chained.log"
    (hooks / "pre-push").write_text(f'#!/bin/sh\necho "pre-push $1" >> "{log_}"\ncat >/dev/null\n')
    (hooks / "reference-transaction").write_text(
        f'#!/bin/sh\nwhile read -r o n r; do echo "rt $1 $r" >> "{log_}"; done\n')
    for h in ("pre-push", "reference-transaction"):
        (hooks / h).chmod(0o755)
    before_rt = (hooks / "reference-transaction").read_text()

    res = GG.install(repo)
    check("install: both hooks go in", res["ok"] and sorted(res["changed"]) == sorted(GG.HOOKS),
          str(res))
    check("install: an existing hook is moved aside and chained, not replaced",
          sorted(res["chained"]) == sorted(GG.HOOKS)
          and (hooks / ("reference-transaction" + GG.CHAINED_SUFFIX)).read_text() == before_rt,
          str(res))
    again = GG.install(repo)
    check("install is idempotent: a second run changes nothing and chains nothing again",
          again["ok"] and not again["changed"] and not again["chained"]
          and (hooks / ("reference-transaction" + GG.CHAINED_SUFFIX)).read_text() == before_rt,
          str(again))
    check("status reads it as installed", GG.state(repo)[0], GG.state(repo)[1])

    wt = root / "wt"
    git(repo, "worktree", "add", "-q", "-b", f"silkworm/{tid}", str(wt), "main")
    log_.write_text("")

    # --- an implementor, in its own worktree ---------------------------------
    (wt / "b.txt").write_text("b\n")
    git(wt, "add", "-A", env=agent)
    r = git(wt, "commit", "-q", "-m", "b", env=agent)
    check("implementor: committing to its own branch works", r.returncode == 0, r.stderr[-200:])
    check("and the existing reference-transaction hook still ran (chained)",
          f"rt committed refs/heads/silkworm/{tid}" in log_.read_text(), log_.read_text()[-300:])
    r = git(wt, "commit", "-q", "--amend", "-m", "b2", env=agent)
    check("implementor: amending works", r.returncode == 0, r.stderr[-200:])
    (repo / "c.txt").write_text("c\n")
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "c")      # the user, on main
    r = git(wt, "rebase", "-q", "main", env=agent)
    check("implementor: rebasing its own branch onto the base works",
          r.returncode == 0 and (wt / "c.txt").exists(), r.stderr[-200:])
    r = git(wt, "fetch", "-q", "origin", env=agent)
    check("implementor: fetch works (remote-tracking refs are allowed)", r.returncode == 0,
          r.stderr[-200:])
    r = git(wt, "pack-refs", "--all", env=agent)
    r2 = git(wt, "gc", "-q", env=agent)
    check("implementor: pack-refs / gc work (they move no ref)",
          r.returncode == 0 and r2.returncode == 0, (r.stderr + r2.stderr)[-200:])

    main_at, origin_at = sha("main"), sha("main", origin)
    r = git(wt, "update-ref", "refs/heads/main", "HEAD", env=agent)
    check("implementor: moving main directly is refused",
          r.returncode != 0 and sha("main") == main_at and "silkworm" in r.stderr, r.stderr[-200:])
    r = git(repo, "merge", "-q", "--no-edit", f"silkworm/{tid}", env=agent)
    check("implementor: merging into main (in the main checkout) is refused",
          r.returncode != 0 and sha("main") == main_at, r.stderr[-200:])
    git(repo, "merge", "--abort")
    git(repo, "reset", "-q", "--hard", "main")
    r = git(wt, "push", "origin", "HEAD:main", env=agent)
    check("implementor: pushing to the remote's main is refused",
          r.returncode != 0 and sha("main", origin) == origin_at
          and "may not push" in r.stderr, r.stderr[-300:])
    r = git(wt, "push", "origin", f"silkworm/{tid}", env=agent)
    check("implementor: pushing even its own branch is refused",
          r.returncode != 0 and not sha(f"refs/heads/silkworm/{tid}", origin), r.stderr[-200:])
    r = git(wt, "tag", "v9", env=agent)
    check("implementor: tagging is refused", r.returncode != 0 and not sha("refs/tags/v9"),
          r.stderr[-200:])
    r = git(wt, "branch", "other", env=agent)
    check("implementor: making another branch is refused",
          r.returncode != 0 and not sha("refs/heads/other"), r.stderr[-200:])
    # A tag pushed to origin mid-task (release CI) is followed into refs/tags
    # by any fetch or pull. That is the remote's tag, not the task's.
    side = root / "side"
    git(root, "clone", "-q", str(origin), str(side))
    git(side, "commit", "-q", "--allow-empty", "-m", "elsewhere")
    git(side, "tag", "v7")
    git(side, "push", "-q", "origin", "HEAD:refs/heads/side", "v7")
    log_.write_text("")
    r = git(wt, "fetch", "origin", env=agent)
    check("implementor: fetch still works when origin has a new tag to follow",
          r.returncode == 0 and sha("refs/tags/v7"), r.stderr[-300:])
    check("and the chained hook was still handed the state, not the command line",
          "rt prepared refs/tags/v7" in log_.read_text(), log_.read_text()[-300:])
    git(repo, "tag", "-d", "v7")
    r = git(wt, "update-ref", "-m", "fix pack-refs", "refs/heads/main", "HEAD", env=agent)
    check("implementor: only pack-refs itself is exempt, not a command mentioning it",
          r.returncode != 0 and sha("main") == main_at, r.stderr[-200:])
    r = git(wt, "-c", "x.y=z pack-refs", "update-ref", "refs/heads/main", "HEAD", env=agent)
    check("implementor: a command disguised as pack-refs on ps's command line is refused",
          r.returncode != 0 and sha("main") == main_at, r.stderr[-200:])
    r = git(wt, "-c", "x.y=fetch", "update-ref", "refs/tags/v8", "HEAD", env=agent)
    check("implementor: and only fetch itself may follow tags, not a command mentioning it",
          r.returncode != 0 and not sha("refs/tags/v8"), r.stderr[-200:])
    other = {**clean, **GG.env_for("implementor", "tsk_someone_else")}
    tip = sha(f"refs/heads/silkworm/{tid}")
    (wt / "d.txt").write_text("d\n")
    git(wt, "add", "-A", env=other)
    r = git(wt, "commit", "-q", "-m", "d", env=other)
    check("implementor: another task's branch is not its own",
          r.returncode != 0 and sha(f"refs/heads/silkworm/{tid}") == tip, r.stderr[-200:])
    git(wt, "reset", "-q", "--hard", env=clean)
    for role in ("reviewer", "assistant"):
        r = git(wt, "tag", f"t-{role}", env={**clean, **GG.env_for(role, tid)})
        check(f"the guard is the implementor's only: a {role} is not refused",
              r.returncode == 0, r.stderr[-200:])
        git(repo, "tag", "-d", f"t-{role}")

    # --- the user, and the bot's own landing: untouched --------------------
    log_.write_text("")
    r = git(repo, "tag", "u1")
    check("unset: the user can tag", r.returncode == 0, r.stderr[-200:])
    r = git(repo, "branch", "scratch")
    check("unset: the user can make a branch", r.returncode == 0, r.stderr[-200:])
    r = git(repo, "push", "-q", "origin", "main")
    check("unset: the user can push, and the existing pre-push hook still ran",
          r.returncode == 0 and sha("main", origin) == sha("main")
          and "pre-push origin" in log_.read_text(), r.stderr[-200:] + log_.read_text()[-200:])
    # The real landing, with publish, as the bot runs it: in-process, with
    # whatever this process's environment is -- stripped, exactly as bot.py
    # strips its own at import. Run by an implementor, this suite inherits the
    # role, and that is the point of stripping it.
    saved = {k: os.environ.pop(k) for k in (GG.ROLE_VAR, GG.ID_VAR) if k in os.environ}
    try:
        lr = M.land(wt, repo, f"silkworm/{tid}", "main", lambda cwd: {"ok": True, "ran": True},
                    publish=True)
    finally:
        os.environ.update(saved)
    check("unset: Silkworm's own landing merges and publishes through the hooks",
          lr.get("landed") and lr.get("published") is True
          and sha("main") == sha(f"refs/heads/silkworm/{tid}") == sha("main", origin),
          f"{lr.get('stage')}: {str(lr.get('detail'))[:200]}")
    r = git(repo, "update-ref", "refs/heads/main", "main~1")
    check("unset: the user can move main", r.returncode == 0, r.stderr[-200:])

    # --- core.hooksPath, and a hook we must not clobber ----------------------
    r2 = root / "r2"
    git(root, "init", "-q", "-b", "main", str(r2))
    custom = root / "myhooks"
    custom.mkdir()
    (custom / "pre-push").write_text("#!/bin/sh\nexit 0\n"); (custom / "pre-push").chmod(0o755)
    git(r2, "config", "core.hooksPath", str(custom))
    res = GG.install(r2)
    check("core.hooksPath: installs where git will look, chaining what is there",
          res["ok"] and (custom / "reference-transaction").exists()
          and (custom / ("pre-push" + GG.CHAINED_SUFFIX)).read_text() == "#!/bin/sh\nexit 0\n"
          and not (r2 / ".git" / "hooks" / "pre-push").exists(), str(res))
    (custom / "pre-push").write_text("#!/bin/sh\necho someone-elses\n")
    res = GG.install(r2)
    check("a foreign hook with the chained name already taken is refused, not overwritten",
          not res["ok"] and (custom / "pre-push").read_text() == "#!/bin/sh\necho someone-elses\n"
          and (custom / ("pre-push" + GG.CHAINED_SUFFIX)).read_text() == "#!/bin/sh\nexit 0\n",
          str(res))
    check("and status reports it unguarded", not GG.state(r2)[0], GG.state(r2)[1])

    def fresh(name):
        r_ = root / name
        git(root, "init", "-q", "-b", "main", str(r_))
        git(r_, "commit", "-q", "--allow-empty", "-m", "a")
        return r_
    r3 = fresh("r3")
    (r3 / ".githooks").mkdir()
    git(r3, "config", "core.hooksPath", ".githooks")
    res = GG.install(r3)
    check("a relative core.hooksPath is refused (each worktree would read its own)",
          not res["ok"] and "relative" in res["error"]
          and not list((r3 / ".githooks").iterdir()) and not GG.state(r3)[0], str(res))
    git(r3, "config", "core.hooksPath", str(r3 / ".githooks"))
    res = GG.install(r3)
    check("a hooks directory inside the work tree is refused (it would dirty the base)",
          not res["ok"] and "work tree" in res["error"]
          and not list((r3 / ".githooks").iterdir()) and not GG.state(r3)[0], str(res))
    r4 = fresh("r4")
    husky = '#!/bin/sh\n. "$(dirname "$0")/husky.sh"\nrun "$(basename "$0")"\n'
    (r4 / ".git" / "hooks" / "reference-transaction").write_text(husky)
    res = GG.install(r4)
    check("a hook that dispatches on its own name ($0) is not renamed out from under itself",
          not res["ok"] and "$0" in res["error"]
          and (r4 / ".git" / "hooks" / "reference-transaction").read_text() == husky
          and not (r4 / ".git" / "hooks" / ("reference-transaction" + GG.CHAINED_SUFFIX)).exists()
          and not (r4 / ".git" / "hooks" / "pre-push").exists(), str(res))
    import threading as _th
    r5 = fresh("r5")
    mine = "#!/bin/sh\necho mine\n"
    (r5 / ".git" / "hooks" / "pre-push").write_text(mine)
    (r5 / ".git" / "hooks" / "pre-push").chmod(0o755)
    outs = []

    class Slow:                  # widens the check-then-rename window to 200ms
        def search(self, text):
            time.sleep(0.2)
            return None
    real_re, GG._SELF_REFERENCE = GG._SELF_REFERENCE, Slow()
    try:
        ts = [_th.Thread(target=lambda: outs.append(GG.install(r5))) for _ in range(4)]
        [t.start() for t in ts]; [t.join() for t in ts]
    finally:
        GG._SELF_REFERENCE = real_re
    check("concurrent installs chain the hook once and lose nothing",
          (r5 / ".git" / "hooks" / ("pre-push" + GG.CHAINED_SUFFIX)).read_text() == mine
          and GG.state(r5)[0] and sum(len(o["chained"]) for o in outs) == 1
          and not list((r5 / ".git" / "hooks").glob(".*silkworm-tmp*")), str(outs)[:300])

    # --- status --------------------------------------------------------------
    pj = root / "projects.json"
    pj.write_text(json.dumps({
        "ready": {"slug": "ready", "scope": {"cwd": str(repo)}, "test_cmd": "true",
                  "auto_merge": True},
        "loose": {"slug": "loose", "scope": {"cwd": str(r2)}, "test_cmd": "", "auto_merge": False},
    }))
    from importlib.machinery import SourceFileLoader
    from importlib.util import module_from_spec, spec_from_loader
    loader = SourceFileLoader("silkworm_cli", str(BASE / "bin" / "silkworm"))
    cli = module_from_spec(spec_from_loader("silkworm_cli", loader))
    loader.exec_module(cli)
    with contextlib.redirect_stdout(io.StringIO()) as out:
        rows = cli.check_git_guards(pj)
    check("status: reports the guard on each ready project, and only those",
          rows == [("ready", True, rows[0][2] if rows else "")]
          and "implementor git guard on ready" in out.getvalue(), out.getvalue())
    status_fn = next(n for n in ast.parse((BASE / "bin" / "silkworm").read_text()).body
                     if isinstance(n, ast.FunctionDef) and n.name == "do_status")
    check("status: `silkworm status` runs the guard check",
          any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
              and c.func.id == "check_git_guards" for c in ast.walk(status_fn)))
    (hooks / "pre-push").unlink()
    with contextlib.redirect_stdout(io.StringIO()):
        rows = cli.check_git_guards(pj)
    check("status: a missing hook reads as unguarded", rows and rows[0][1] is False, str(rows))


def test_researcher_git_guard():
    """A queued researcher runs unattended in its own worktree with write
    access, exactly as an implementor does, so git holds it to the same line:
    its own branch, and nothing else. It stays writable -- trader research
    writes caches and runs backtests -- so committing on its branch works."""
    import git_guard as GG
    print("\nresearcher git guard: an unattended research run cannot push, tag or move the base")
    root = Path(tempfile.mkdtemp())
    origin, repo, wt = root / "origin.git", root / "repo", root / "wt"
    tid = "tsk_research1"
    clean = GG.strip(dict(os.environ))
    agent = {**clean, **GG.env_for("researcher", tid)}

    def git(cwd, *a, env=None):
        return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a],
                              cwd=str(cwd), capture_output=True, text=True,
                              env=env if env is not None else clean)

    def sha(ref, cwd=repo):
        return git(cwd, "rev-parse", "-q", "--verify", ref).stdout.strip()

    check("the researcher is a guarded role, beside the implementor",
          {"researcher", "implementor"} <= set(getattr(GG, "GUARDED_ROLES", ())),
          str(getattr(GG, "GUARDED_ROLES", None)))
    git(root, "init", "-q", "--bare", "-b", "main", str(origin))
    git(root, "init", "-q", "-b", "main", str(repo))
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "a")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")
    res = GG.install(repo)
    check("install: both hooks go in", res["ok"], str(res))
    git(repo, "worktree", "add", "-q", "-b", f"silkworm/{tid}", str(wt), "main")
    main_at = sha("main")

    (wt / "cache.csv").write_text("x\n")
    git(wt, "add", "-A", env=agent)
    r = git(wt, "commit", "-q", "-m", "cache", env=agent)
    check("researcher: it is not read-only -- committing to its own branch works",
          r.returncode == 0, r.stderr[-200:])
    r = git(wt, "push", "-q", "origin", "HEAD:main", env=agent)
    check("researcher: pushing is refused",
          r.returncode != 0 and "may not push" in r.stderr
          and sha("main", origin) == main_at, r.stderr[-300:])
    r = git(wt, "push", "-q", "origin", f"silkworm/{tid}", env=agent)
    check("researcher: pushing even its own branch is refused",
          r.returncode != 0 and not sha(f"refs/heads/silkworm/{tid}", origin), r.stderr[-300:])
    r = git(wt, "update-ref", "refs/heads/main", "HEAD", env=agent)
    check("researcher: moving main is refused",
          r.returncode != 0 and sha("main") == main_at, r.stderr[-200:])
    r = git(repo, "merge", "-q", "--ff-only", f"silkworm/{tid}", env=agent)
    check("researcher: merging into main (in the main checkout) is refused",
          r.returncode != 0 and sha("main") == main_at, r.stderr[-200:])
    r = git(wt, "tag", "r1", env=agent)
    check("researcher: tagging is refused",
          r.returncode != 0 and not sha("refs/tags/r1"), r.stderr[-200:])
    r = git(wt, "branch", "elsewhere", env=agent)
    check("researcher: making another branch is refused",
          r.returncode != 0 and not sha("refs/heads/elsewhere"), r.stderr[-200:])
    r = git(wt, "fetch", "-q", "origin", env=agent)
    check("researcher: fetch still works", r.returncode == 0, r.stderr[-200:])
    r = git(repo, "tag", "u1")
    check("unset: the user is untouched", r.returncode == 0, r.stderr[-200:])


def test_unreviewed_roles_never_land():
    """A branch left by a role that is never reviewed is refused by Land,
    Approve and the landing gate under both -- not merged on a click with no
    reviewer having read it. A researcher's 'change nothing that needs
    merging' lived only in its prompt; if it committed anyway, its branch was
    listed as stranded work and Land merged it."""
    import roles
    import tasks as T
    import branches as B
    import merge as M
    print("\nunreviewed roles never land: Land, Approve and the gate refuse them")
    check("fixture: a researcher and an assistant are never reviewed; an implementor is",
          not roles.needs_review(roles.RESEARCHER) and not roles.needs_review("assistant")
          and roles.needs_review("implementor"))

    store = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    begun, recorded = [], []
    import contextlib
    ns = {"task_store": store, "tasks": T, "branches": B, "roles": roles,
          "discard": __import__("discard"), "log": logging.getLogger("test"),
          "holding": __import__("holding"), "tell_thread": lambda *a: None,
          "start_landing": lambda tid, approved=False: begun.append((tid, approved)) or True,
          "record_landing": lambda task, out: recorded.append(out),
          "repo_guard": lambda cwd: contextlib.nullcontext()}
    got = lift({"land_or_drop", "approve_task", "unreviewed_role"}, ns=ns)["__lifted__"]
    check("Land, Approve and the role gate were lifted out of bot.py",
          {"land_or_drop", "approve_task", "unreviewed_role"} <= got, str(sorted(got)))

    def finished(role):
        tid = store.create(f"{role} work", state=T.QUEUED, role=role,
                           scope={"cwd": "/nowhere"})["id"]
        store.transition(tid, T.RUNNING); store.transition(tid, T.DONE)
        return tid

    for role in (roles.RESEARCHER, "assistant"):
        tid = finished(role)
        r = ns["land_or_drop"]("land", {"id": tid})
        err = r.get("error") or ""
        check(f"Land refuses the branch of the {role} task, saying why and what to do instead",
              r.get("ok") is False and not begun and role in err
              and "Drop" in err and "implementor" in err, repr(r))
    begun.clear()
    tid = finished("implementor")
    r = ns["land_or_drop"]("land", {"id": tid})
    check("while an implementor's still lands", r.get("ok") and begun == [(tid, True)], repr(r))

    begun.clear()
    rid = store.create("researched", state=T.QUEUED, role=roles.RESEARCHER)["id"]
    store.transition(rid, T.RUNNING); store.transition(rid, T.AWAITING_APPROVAL)
    r = ns["approve_task"]({"id": rid, "by": "you"})
    check("Approve refuses a researcher's work, before closing or landing anything",
          r.get("ok") is False and "implementor" in (r.get("error") or "")
          and not begun and store.get(rid)["state"] == T.AWAITING_APPROVAL, repr(r))

    # The gate under both, and under auto-merge: a person asking does not
    # make up for a review that never happened.
    class Projects:
        def get(self, slug): return {"auto_merge": True, "test_cmd": "true"}
        def scope_for(self, slug): return {}
    attached = []

    class Worktrees:
        def base_ref(self, *a, **k): return "main"
        def attach(self, cwd, tid, branch): attached.append(branch); return None
    gate = {"project_store": Projects(), "worktrees": Worktrees(), "merge": M,
            "branches": B, "roles": roles, "log": logging.getLogger("test")}
    lift({"land_if_ready", "landing_enabled", "unreviewed_role"}, ns=gate, absent=LANDING_ROUTES)
    for approved in (False, True):
        r = gate["land_if_ready"]({"id": "tsk_r", "project": "p", "role": roles.RESEARCHER,
                                   "verified": True, "scope": {"cwd": "/nowhere"}},
                                  approved=approved)
        check(f"the landing gate refuses a researcher (approved={approved})",
              r.get("stage") == "unreviewed-role" and r.get("eligible") is False
              and not attached, repr(r))


def test_guard_env_reaches_task_turns_only():
    import git_guard as GG
    import roles as R_
    print("\nimplementor git guard: who carries the role")
    ns = lift("claude_env", git_guard=GG, os=os, APPROVAL_PORT=1,
                       CLAUDE_APPROVAL_MODE="skip", APPROVAL_TIMEOUT=1)
    saved = {k: os.environ.get(k) for k in (GG.ROLE_VAR, GG.ID_VAR)}
    # As if this process had itself been started from inside a task turn.
    os.environ[GG.ROLE_VAR], os.environ[GG.ID_VAR] = "implementor", "tsk_leak"
    try:
        task = ns["claude_env"]("C:1", 0, role="implementor", task_id="tsk_abc")
        slack = ns["claude_env"]("C:1")
        bare = ns["claude_env"]()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    check("a task turn's env names its role and id",
          task.get(GG.ROLE_VAR) == "implementor" and task.get(GG.ID_VAR) == "tsk_abc", str(
              {k: task.get(k) for k in (GG.ROLE_VAR, GG.ID_VAR)}))
    check("a Slack turn, and every helper run, carries neither -- not even inherited",
          all(GG.ROLE_VAR not in e and GG.ID_VAR not in e for e in (slack, bare)))

    tree = ast.parse((BASE / "bot.py").read_text())
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    def env_calls(fn):
        return [c for c in ast.walk(fns[fn]) if isinstance(c, ast.Call)
                and isinstance(c.func, ast.Name) and c.func.id == "claude_env"]
    ex = env_calls("execute_task")
    check("execute_task hands its task id to the turn's env",
          ex and all(any(k.arg == "task_id" and isinstance(k.value, ast.Name)
                         and k.value.id == "tid" for k in c.keywords) for c in ex), f"{len(ex)} call(s)")
    # Except the operator's own Slack turn, which is held to git_guard too:
    # only the branch of handle_prompt taken when the role is the operator's.
    op = [n for n in ast.walk(fns["handle_prompt"]) if isinstance(n, ast.IfExp)
          and ast.unparse(n.test) == "role_name == roles.OPERATOR"
          and isinstance(n.body, ast.Call) and getattr(n.body.func, "id", "") == "claude_env"]
    check("handle_prompt names a task id only for the operator's turn",
          len(op) == 1 and ast.unparse(op[0].orelse) == "claude_env(key)",
          str([ast.unparse(n) for n in op]))
    exempt = {id(n.body) for n in op}
    others = [c for f in fns if f != "execute_task" for c in env_calls(f) if id(c) not in exempt]
    check("and nothing else does: no other claude_env call names a task id",
          others and not any(k.arg == "task_id" for c in others for k in c.keywords))
    stripped = [n for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                and ast.unparse(n.value) == "git_guard.strip(os.environ)"]
    check("the bot strips the role from its own environment at import, so landing never has it",
          len(stripped) == 1)
    main = next(n for n in tree.body if isinstance(n, ast.If)
                and "__main__" in ast.unparse(n.test))
    check("the bot installs the guard at startup",
          any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
              and c.func.id == "install_git_guards" for c in ast.walk(main)))
    sweeper = fns["_worktree_sweeper"]
    check("and re-installs it on the sweeper's round, so a project that became ready is covered",
          any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
              and c.func.id == "install_git_guards" for c in ast.walk(sweeper)))
    for action in ("test_cmd=cmd", "auto_merge=on"):
        block = (BASE / "bot.py").read_text().split(f"project_store.ensure(slug, {action})", 1)
        check(f"setting {action.split('=')[0]} from the dashboard re-installs at once",
              len(block) == 2 and "install_git_guards(slug)" in block[1][:200])

    p = R_.IMPLEMENTOR_SYSTEM.lower()
    check("the implementor is told it must not push or move the base branch",
          "do not push" in p and "base branch" in p and "hook" in p)
    check("and that 'done when it is on main' is met by reporting it ready to land",
          "ready to land" in p and "on main" in p)


def test_conflicting_landings_go_back_to_their_implementor():
    """A branch that cannot catch up with its base goes back to the implementor
    that wrote it, with the conflict attached -- by either door.

    Of the nine Silkworm branches done but unmerged on 2026-10-02, four were
    held up only or mainly by being 27-37 commits behind and conflicting; each
    repair then came back as a separate proposal costing a decision, a session
    and a reviewer. Driven against a real repository with an origin remote, in
    which main and the task's branch genuinely conflict, through the real
    resolve_review, approve_task, land_or_drop, start_landing, land_if_ready
    and merge.land.
    """
    print("\nconflicting landings are sent back to their implementor")
    import contextlib
    import roles
    import tasks as T
    import worktrees as W
    import branches as B

    root = Path(tempfile.mkdtemp())
    origin, repo = root / "origin.git", root / "repo"
    saved_root, W.ROOT = W.ROOT, root / "wts"

    def git(cwd, *a):
        return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                               *a], cwd=str(cwd), capture_output=True, text=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    git(root, "clone", "-q", str(origin), str(repo))
    (repo / "shared.py").write_text("VALUE = 1\n")
    (repo / "other.py").write_text("x = 1\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "base")
    git(repo, "push", "-q", "origin", "main")

    store = T.TaskStore(root / "t.json")
    projects_ = {"ready": {"slug": "ready", "test_cmd": "true", "auto_merge": True},
                 "manual": {"slug": "manual", "test_cmd": "true"}}
    said, posted = [], []

    def task_state(tid, state, detail=""):
        try:
            store.transition(tid, state, detail)
        except T.InvalidTransition:
            pass

    class SyncThread:
        """start() runs the landing in place, so its outcome can be read."""
        def __init__(self, target, **kw):
            self.target = target
        def start(self):
            self.target()
    import threading as _th
    ns = {"task_store": store, "tasks": T, "roles": roles, "merge": __import__("merge"),
          "projects": __import__("projects"), "branches": B, "worktrees": W,
          "verify": __import__("verify"), "discard": __import__("discard"),
          "holding": __import__("holding"), "subprocess": subprocess, "time": time,
          "log": logging.getLogger("test"), "task_state": task_state,
          "threading": types.SimpleNamespace(Lock=_th.Lock, Thread=SyncThread),
          "project_store": types.SimpleNamespace(
              get=lambda slug: projects_.get(slug),
              scope_for=lambda slug: dict((projects_.get(slug) or {}).get("scope") or {})),
          "app": types.SimpleNamespace(client=types.SimpleNamespace(
              chat_postMessage=lambda **kw: posted.append(kw["text"]))),
          "tell_thread": lambda key, text: said.append((key, text)),
          "repo_guard": lambda *a, **k: contextlib.nullcontext(),
          "file_followups": lambda *a, **k: [], "REVISION": {"sha": ""},
          "BASE_DIR": root / "elsewhere", "Path": Path}
    got = lift({"resolve_review", "rework_flagged_review", "rework_conflict",
                    "rework_finished", "conflict_addendum", "send_back",
                    "review_addendum", "_unsupervised", "MAX_REVIEW_REWORKS",
                    "MAX_CONFLICT_REWORKS", "CONFLICT_STAGES", "land_and_record",
                    "land_if_ready", "landing_enabled", "record_landing",
                    "_is_own_checkout", "start_landing", "approve_task",
                    "land_or_drop", "LANDING_UNDERWAY", "_landing_now",
                    "_landing_guard"}, ns=ns,
               absent=LANDING_ROUTES + ('CLAUDE_CWD',))["__lifted__"]
    check("the gate, both landing doors and the rework were lifted out of bot.py",
          {"resolve_review", "rework_conflict", "rework_finished", "start_landing",
           "approve_task", "land_or_drop"} <= got, str(sorted(got)))

    def tip(ref):
        return git(repo, "rev-parse", "--verify", "-q", ref).stdout.strip()

    def worked(project, state=T.BLOCKED):
        """An implementor's branch off main, then main moving under it in the
        same lines -- a conflict nothing can resolve mechanically."""
        t = store.create("Make VALUE two", title="Value", project=project,
                         role="implementor", driver="queue", isolate=True,
                         thread="C1:1.0", scope={"cwd": str(repo), "branch": "main"})
        tid = t["id"]
        store.transition(tid, T.RUNNING, "claimed")
        wt = W.create(repo, tid, fetch=False, base="main")
        (wt / "shared.py").write_text(f"VALUE = 2  # {tid}\n")
        git(wt, "commit", "-qam", f"the work of {tid}")
        W.release(wt)
        n = len(store.all())
        (repo / "shared.py").write_text(f"VALUE = {100 + n}  # main moved on\n")
        git(repo, "commit", "-qam", f"main moves ({n})")
        git(repo, "push", "-q", "origin", "main")
        store.update(tid, branch=W.BRANCH_PREFIX + tid, verified=True, blocked_on=["rev"])
        store.transition(tid, T.BLOCKED, "awaiting review")
        if state != T.BLOCKED:
            store.update(tid, blocked_on=[])
            store.transition(tid, state, "parked")
        return tid

    def passing_review(tid):
        said.clear()
        rev = store.create("review it", role="reviewer", parent=tid)
        ns["resolve_review"](rev, "reviewer", '```json\n{"ok": true, "summary": '
                             '"fine", "findings": []}\n```', "C1", "1.0")
        return store.get(tid)

    try:
        # --- (a) a passing review whose landing conflicts ----------------------
        tid = worked("ready")
        main_before, work_before = tip("main"), tip(W.BRANCH_PREFIX + tid)
        t = passing_review(tid)
        check("a passing review whose landing conflicts is sent back, not parked",
              t["state"] == T.QUEUED and t.get("conflict_reworks") == 1,
              f"state {t['state']}, reworks {t.get('conflict_reworks')}")
        landing = (t.get("result") or {}).get("landing") or {}
        check("the refusal on the record names the conflicting files and the base",
              landing.get("stage") == "rebase" and landing.get("conflicts") == ["shared.py"]
              and landing.get("base") == "main" and landing.get("onto") == main_before,
              str(landing))
        check("and the goal it is sent back with carries them",
              "  shared.py" in t["goal"] and "git rebase main" in t["goal"]
              and main_before[:12] in t["goal"] and "CONFLICT" in t["goal"]
              and "run the full test suite" in t["goal"], t["goal"][-900:])
        check("on its own branch, untouched, and main untouched",
              tip(W.BRANCH_PREFIX + tid) == work_before and tip("main") == main_before)
        check("with the gates reset, so it is verified and reviewed again",
              t.get("blocked_on") == [] and t.get("verified") is None)

        # The rerun does not manage it: the second refusal is for a person.
        store.transition(tid, T.RUNNING, "claimed")
        store.update(tid, verified=True, blocked_on=["rev"])
        store.transition(tid, T.BLOCKED, "awaiting review")
        t = passing_review(tid)
        check("a second conflicting landing parks the task for a person",
              t["state"] == T.AWAITING_APPROVAL and t.get("conflict_reworks") == 1
              and not said, f"state {t['state']}, said {said}")

        # --- (b) Approve: done is terminal, so the work moves to a new task -----
        uid = worked("ready", T.AWAITING_APPROVAL)
        old = W.BRANCH_PREFIX + uid
        git(repo, "push", "-q", "origin", old)          # a remote copy, too
        work_tip, main_before = tip(old), tip("main")
        r = ns["approve_task"]({"id": uid, "by": "test"})
        u = store.get(uid)
        sid = ((u.get("result") or {}).get("landing") or {}).get("reworked_by") or ""
        s = store.get(sid) or {}
        check("approving closes the task, as it always has",
              r.get("ok") and u["state"] == T.DONE, str(r))
        check("and a conflicting landing is not stranded: a new task takes it over",
              s.get("state") == T.QUEUED and s.get("role") == "implementor"
              and s.get("driver") == "queue" and s.get("isolate") is True,
              f"reworked_by {sid!r}: {s.get('state')}")
        check("on the same commits, under the one name the guard lets it move",
              tip(W.BRANCH_PREFIX + sid) == work_tip and not tip(old)
              and s.get("branch") == W.BRANCH_PREFIX + sid,
              f"{W.BRANCH_PREFIX + sid} at {tip(W.BRANCH_PREFIX + sid)[:8]}, "
              f"{old} at {tip(old)[:8] or 'gone'}")
        check("told the base, the conflicting file and git's output",
              "  shared.py" in s.get("goal", "") and "git rebase main" in s["goal"]
              and "CONFLICT" in s["goal"] and s["goal"].startswith("Make VALUE two"),
              s.get("goal", "")[-600:])
        check("counted, in its own thread, pointing back at the original",
              s.get("conflict_reworks") == 1 and s.get("thread") == "C1:1.0"
              and s.get("source_ref") == uid and s.get("root") == uid)
        check("and the thread is told", any(sid in text for _, text in said), str(said))
        check("the original leaves the unmerged survey, remote copy and all",
              not [row for row in B.survey([store.get(uid)]) if row["id"] == uid],
              str(B.survey([store.get(uid)])))
        check("and Land on it refuses, naming who has the work",
              sid in (ns["land_or_drop"]("land", {"id": uid}).get("error") or ""))
        check("main untouched throughout", tip("main") == main_before)

        # The new task catches up, as an implementor would, and lands normally.
        here = W.attach(repo, sid, W.BRANCH_PREFIX + sid, label="")
        check("(its first run reattaches the branch execute_task looks for)", bool(here))
        store.transition(sid, T.RUNNING, "claimed")
        git(here, "rebase", "main")
        (here / "shared.py").write_text("VALUE = 2\n")
        git(here, "add", "shared.py")
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "-c", "core.editor=true", "rebase", "--continue"],
                       cwd=str(here), capture_output=True, text=True)
        W.release(here)
        store.update(sid, verified=True, blocked_on=["rev"])
        store.transition(sid, T.BLOCKED, "awaiting review")
        s = passing_review(sid)
        check("once caught up, it lands through the ordinary review path",
              s["state"] == T.DONE and (repo / "shared.py").read_text() == "VALUE = 2\n"
              and (s.get("result") or {}).get("landed") == tip("main"),
              f"{s['state']}: {((s.get('result') or {}).get('landing') or {})}")

        # --- the new task's own second conflict parks; approving it stops ------
        vid = worked("ready", T.AWAITING_APPROVAL)
        ns["approve_task"]({"id": vid, "by": "test"})
        wid = ((store.get(vid).get("result") or {}).get("landing") or {}).get("reworked_by")
        store.transition(wid, T.RUNNING, "claimed")      # and does nothing useful
        store.update(wid, verified=True, blocked_on=["rev"])
        store.transition(wid, T.BLOCKED, "awaiting review")
        w = passing_review(wid)
        check("the taken-over work's second conflict parks it for a person",
              w["state"] == T.AWAITING_APPROVAL, w["state"])
        before = len(store.all())
        ns["approve_task"]({"id": wid, "by": "test"})
        w = store.get(wid)
        check("and approving that is not sent round again",
              w["state"] == T.DONE and len(store.all()) == before
              and not ((w.get("result") or {}).get("landing") or {}).get("reworked_by")
              and tip(W.BRANCH_PREFIX + wid),
              f"{len(store.all()) - before} new task(s)")

        # --- Land on finished work takes the same route -------------------------
        xid = worked("ready", T.AWAITING_APPROVAL)
        store.transition(xid, T.DONE, "closed without landing")
        r = ns["land_or_drop"]("land", {"id": xid})
        yid = ((store.get(xid).get("result") or {}).get("landing") or {}).get("reworked_by")
        check("Land on a done task that conflicts hands it on too",
              r.get("ok") and yid and store.get(yid)["state"] == T.QUEUED
              and tip(W.BRANCH_PREFIX + yid), str(r))

        # --- a branch that cannot be taken over stays where it is ---------------
        real_run = subprocess.run
        def refusing(cmd, *a, **k):
            if cmd[:3] == ["git", "branch", "-m"]:
                return types.SimpleNamespace(returncode=128, stdout="", stderr="no")
            return real_run(cmd, *a, **k)
        ns["subprocess"] = types.SimpleNamespace(run=refusing)
        zid = worked("ready", T.AWAITING_APPROVAL)
        before, ztip = len(store.all()), tip(W.BRANCH_PREFIX + zid)
        try:
            ns["approve_task"]({"id": zid, "by": "test"})
        finally:
            ns["subprocess"] = subprocess
        zl = (store.get(zid).get("result") or {}).get("landing") or {}
        check("a rename git refuses files nothing and takes back the hand-over",
              len(store.all()) == before and tip(W.BRANCH_PREFIX + zid) == ztip
              and zl.get("stage") == "rebase" and not zl.get("reworked_by"), str(zl))

        # --- a project not taking unsupervised work: exactly as before -----------
        mid = worked("manual", T.AWAITING_APPROVAL)
        before, mtip = len(store.all()), tip(W.BRANCH_PREFIX + mid)
        ns["approve_task"]({"id": mid, "by": "test"})
        m = store.get(mid)
        check("on a project that does not take unsupervised work, Approve parks as today",
              m["state"] == T.DONE and len(store.all()) == before
              and tip(W.BRANCH_PREFIX + mid) == mtip
              and (m.get("result") or {}).get("landing", {}).get("stage") == "rebase")
    finally:
        W.ROOT = saved_root

    # --- and nothing else still calls the original stuck -----------------------
    import digest
    vz = (BASE / "visualizer.py").read_text()
    landjs = vz[vz.index("function landing(t)"):vz.index("function threadLink(")]
    probe = (landjs + "\nconst esc = s => String(s);\nprocess.stdout.write(landing("
             "{result: {landing: {eligible: true, landed: false, stage: 'rebase', "
             "branch: 'silkworm/tsk_h', reworked_by: 'tsk_n'}}}))")
    out = subprocess.run(["node", "-e", probe], capture_output=True, text=True)
    check("the dashboard says who has the work rather than 'waiting for you'",
          "handed to tsk_n" in out.stdout and "waiting for you" not in out.stdout,
          out.stdout or out.stderr[:300])
    handed = {"id": "tsk_h", "title": "Handed", "project": "ready", "state": T.DONE,
              "created": time.time(), "result": {"landing": {
                  "eligible": True, "landed": False, "stage": "rebase",
                  "reworked_by": "tsk_n", "at": time.time()}}}
    def refused(rec):
        return (digest.sections([rec], time.time()).get("ready") or {}).get("refused")
    plain = dict(handed, result={"landing": {
        k: v for k, v in handed["result"]["landing"].items() if k != "reworked_by"}})
    check("and the digest does not list it as refused",
          refused(plain) and not refused(handed), str((refused(plain), refused(handed))))

    # --- the reviewer is told staleness is not a finding -----------------------
    sysp = roles.REVIEWER_SYSTEM
    check("the review prompt says being behind the base is not a finding on its own",
          "behind the base branch is not a finding" in sysp
          and "do not ask for a rebase" in sysp.lower()
          and "clashes with what the base now does" in sysp, sysp[:400])


# python-dotenv, which the bot loads .env with, accepts `export NAME=...`.
# bin/silkworm read .env with a bare NAME= prefix match, so the same file set
# a token or port for the bot that the CLI never saw.

def test_cli_reads_exported_env_lines():
    print("\nbin/silkworm reads .env the way the bot does, export lines included")
    cli = _load_cli()
    root = Path(tempfile.mkdtemp())
    (root / ".env").write_text(
        "# VIZ_TOKEN=commented-out\n"
        "export VIZ_TOKEN='tok-1'\n"
        "export  APPROVAL_PORT=9911\n"
        "LEARNINGS_FILE=/tmp/first.json\n"
        "export LEARNINGS_FILE=\"/tmp/last.json\"\n"
        "EXPORTED_THING=x\n")
    cli.REPO = root
    names = ("VIZ_TOKEN", "APPROVAL_PORT", "LEARNINGS_FILE", "VIZ_BIND", "THING")
    saved = {n: os.environ.pop(n) for n in names if n in os.environ}
    try:
        check("an exported VIZ_TOKEN is read, quotes stripped",
              cli.env_setting("VIZ_TOKEN") == "tok-1", repr(cli.env_setting("VIZ_TOKEN")))
        check("an exported APPROVAL_PORT is the bot port",
              cli.bot_port() == "9911", cli.bot_port())
        check("the last assignment wins, as with dotenv",
              cli.learnings_file() == Path("/tmp/last.json"), str(cli.learnings_file()))
        check("env_has sees an exported variable",
              cli.env_has("APPROVAL_PORT"))
        check("a name is matched whole, not as a suffix of another",
              not cli.env_has("THING") and cli.env_file_value("THING") is None)
        check("an unset name still falls back to the default",
              cli.env_setting("VIZ_BIND") == "" and not cli.env_has("VIZ_BIND"))
    finally:
        os.environ.update(saved)


# session_hook.py and visualizer.py each hand-parsed APPROVAL_PORT from .env
# with split("=")[1].strip(), keeping the quotes: APPROVAL_PORT="9911" (which
# dotenv reads as 9911) gave them the port '"9911"', so the hook posted to a
# URL that can't exist and the dashboard lost the bot. All three readers now
# share envfile.value.

def test_every_env_reader_strips_quotes_like_dotenv():
    print("\n.env readers outside the bot agree with dotenv on quoted values")
    import http.server
    import shutil
    import subprocess
    import threading
    import envfile
    import visualizer as V
    root = Path(tempfile.mkdtemp())
    (root / ".env").write_text('export APPROVAL_PORT="9911"\nVIZ_TOKEN=\'tok\'\n')
    check("envfile strips double quotes", envfile.value(root / ".env", "APPROVAL_PORT") == "9911")
    check("envfile strips single quotes", envfile.value(root / ".env", "VIZ_TOKEN") == "tok")
    check("envfile: a missing file is None", envfile.value(root / "nope", "X") is None)
    saved = V.BASE_DIR
    try:
        V.BASE_DIR = root
        check("visualizer's bot port drops the quotes", V._bot_port() == "9911", V._bot_port())
    finally:
        V.BASE_DIR = saved
    cli = _load_cli()
    cli.REPO = root
    saved_env = os.environ.pop("APPROVAL_PORT", None)
    try:
        check("the CLI's bot port drops the quotes", cli.bot_port() == "9911", cli.bot_port())
    finally:
        if saved_env is not None:
            os.environ["APPROVAL_PORT"] = saved_env

    # The hook itself, run as Claude Code runs it: a script next to its .env,
    # under the system python3 (3.9 on macOS), posting to the configured port.
    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    hook_dir = Path(tempfile.mkdtemp())
    for f in ("session_hook.py", "envfile.py"):
        shutil.copy(BASE / f, hook_dir / f)
    (hook_dir / ".env").write_text(f'APPROVAL_PORT="{srv.server_address[1]}"\n')
    py = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
    env = {k: v for k, v in os.environ.items() if k != "SILKWORM_BOT"}
    r = subprocess.run([py, str(hook_dir / "session_hook.py")], env=env,
                       input='{"hook_event_name": "SessionStart", "session_id": "s1"}',
                       capture_output=True, text=True, timeout=10)
    srv.server_close()
    check("the hook runs cleanly under the system python", r.returncode == 0 and not r.stderr,
          r.stderr[-400:])
    check("the hook posts to the quoted port", [p for p, _ in got] == ["/session-event"], repr(got))

    # One reader: nothing outside envfile hand-splits APPROVAL_PORT lines.
    for f in ("session_hook.py", "visualizer.py", "bin/silkworm"):
        src = (BASE / f).read_text()
        check(f"{f} doesn't parse .env lines itself",
              'startswith("APPROVAL_PORT=")' not in src and 'split("=", 1)' not in src)

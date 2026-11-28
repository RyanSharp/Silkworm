"""Review and landing: the review gate, landing work, a red base, CI as the gate.

Run by tests/run.py with every other tests/test_*.py; see tests/harness.py."""

from harness import *  # noqa: F401,F403


# --- the review gate ----------------------------------------------------------

def test_review_gate():
    import roles
    import tasks as T
    print("\nreview gate")

    check("slack behaviour is untouched by roles", not roles.needs_review("assistant"))
    check("implementor output is gated", roles.needs_review("implementor"))
    check("a review is not itself reviewed", not roles.needs_review("reviewer"))
    check("reviewer starts a fresh session", roles.is_fresh("reviewer"))
    check("assistant resumes as before", not roles.is_fresh("assistant"))

    default = ["--dangerously-skip-permissions"]
    rp = roles.permission_args("reviewer", default)
    check("reviewer never gets full autonomy", "--dangerously-skip-permissions" not in rp)
    check("reviewer is read-only",
          "Edit" not in roles.REVIEWER_TOOLS and "Write" not in roles.REVIEWER_TOOLS)
    check("other roles keep the default args",
          roles.permission_args("implementor", default) == default)

    v = roles.parse_verdict('```json\n{"ok": true, "summary": "fine", "findings": []}\n```')
    check("a clean verdict parses", v["ok"] and v["parsed"])
    v = roles.parse_verdict('```json\n{"ok": false, "summary": "b", "findings": ["x"]}\n```')
    check("findings parse", not v["ok"] and v["findings"] == ["x"])
    v = roles.parse_verdict("Looks fine to me!")
    check("an unreadable verdict fails closed", not v["ok"] and not v["parsed"])
    v = roles.parse_verdict('```json\n{oops\n```')
    check("malformed json fails closed", not v["ok"] and not v["parsed"])
    v = roles.parse_verdict('```json\n{"ok":true,"findings":[]}\n```\n'
                            '```json\n{"ok":false,"summary":"no","findings":["x"]}\n```')
    check("the last verdict wins", not v["ok"])

    check("a blocked task can be resolved by its review",
          T.can(T.BLOCKED, T.DONE) and T.can(T.BLOCKED, T.AWAITING_APPROVAL))
    # A state that asks a question needs a way to answer it, or the task is
    # stuck on the board with no button that does anything.
    check("approving a flagged task completes it", T.can(T.AWAITING_APPROVAL, T.DONE))
    check("a flagged task can be sent back", T.can(T.AWAITING_APPROVAL, T.QUEUED))
    check("a task needing input can resume", T.can(T.NEEDS_INPUT, T.QUEUED))
    import re as _re
    vz = (BASE / "visualizer.py").read_text()
    js = _re.search(r"<script>(.*?)</script>", vz, _re.S).group(1)
    for state in ("proposed", "awaiting_approval", "needs_input", "failed"):
        check(f"the UI offers an action for {state}",
              f'=== "{state}"' in js,
              "a state that needs the user must have a button")
    check("the reviewer's findings are shown before you approve",
          "function review(t)" in js and "Review flagged" in js)

    # Sending work back is worth little if you cannot say why.
    check("send back asks for your own notes", "function sendBack(" in js)
    check("cancelling the prompt does not send it back", "notes === null" in js)
    bot = (BASE / "bot.py").read_text()
    rework = bot[bot.index('if action == "rework":'):bot.index('if action in ("accept"')]
    rework += bot[bot.index("def send_back("):bot.index("def rework_flagged_review(")]
    rework += bot[bot.index("def review_addendum("):bot.index("def send_back(")]
    check("the original goal is kept, not replaced",
          "task.get('goal', '')" in rework)
    check("reviewer findings are appended for the rerun", "findings" in rework)
    check("your notes are appended too", 'payload.get("notes")' in rework)
    check("your notes are attributed to you, not the reviewer",
          "From {payload.get('by'" in rework)
    check("a rework is handed to the runner", 'driver="queue"' in rework)

    src = (BASE / "bot.py").read_text()
    gate = src[src.index("def resolve_review("):src.index("def _task_scheduler(")]
    check("a review task is titled readably, not by its prompt",
          'title=f"Review: ' in gate)
    # Completes it -- or parks it on a step its run left for you; see
    # test_a_passing_review_still_leaves_the_step_with_you for the behaviour.
    check("passing review completes the parent", "tasks.ending(task_store.get(parent_id))" in gate)
    check("a flagged review asks the user", "tasks.AWAITING_APPROVAL" in gate)
    check("the implementor waits rather than self-certifying", "tasks.BLOCKED" in gate)
    exe = src[src.index("def execute_task("):src.index("def resolve_review(")]
    check("a fresh role does not resume the thread's session",
          "None if fresh else" in exe)
    check("a fresh role does not repoint the thread's session",
          "if not fresh:" in exe)

# --- the review must stand in the tree that holds the work --------------------
# Isolation and the review gate cancelled each other out. An isolated task
# commits inside its own worktree and nowhere else; the gate created the
# reviewer with the scope the *implementor* had been given, whose cwd is the
# main checkout, and that worktree had already been released. So the
# independent check read an unchanged tree and could only believe the summary
# it was told not to trust -- and a passing verdict is what lands work.

def test_review_sees_the_work():
    import roles
    import tasks as T
    import worktrees as W
    from tasks import TaskStore
    print("\nthe review stands where the work is")

    def git(cwd, *a):
        return subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True)

    root = Path(tempfile.mkdtemp())
    W.ROOT = root / "worktrees"
    repo = root / "repo"; repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("hello\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "base")

    st = TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    ns = lift("record_branch", "review_branch", "resolve_review",
                       roles=roles, tasks=T, task_store=st, worktrees=W,
                       branches=__import__("branches"),
                       project_store=types.SimpleNamespace(scope_for=lambda slug: {}),
                       task_state=lambda tid, state, detail="": st.transition(
                           tid, state, detail),
              absent=('CLAUDE_CWD', 'app', 'file_followups', 'land_and_record', 'rework_conflict',
                      'rework_flagged_review', 'tell_thread'))

    # An implementor's turn, for real: its own checkout, a commit in it, the
    # branch written down on the way out, and the checkout released.
    impl = st.create("add a fix", role="implementor", driver="queue", isolate=True,
                     project="p", scope={"cwd": str(repo)})
    tid = impl["id"]
    st.transition(tid, T.RUNNING)
    wt = W.create(repo, tid, fetch=False)
    st.update(tid, scope={**impl["scope"], "worktree": str(wt)})
    impl = st.get(tid)
    (wt / "fix.py").write_text("def fixed(): return 1\n")
    git(wt, "add", "-A"); git(wt, "commit", "-qm", "the work")
    ns["record_branch"](tid, wt, "")
    W.release(wt)
    check("the implementor's own checkout is gone by review time",
          not wt.exists(), "which is why the reviewer cannot simply inherit it")

    # The base branch moves on underneath, as it does in a real repository.
    (repo / "unrelated.txt").write_text("someone else\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "unrelated")

    # The real enqueue path.
    settled = ns["resolve_review"](impl, "implementor", "I added fix.py", "C1", "1.0")
    kids = [r for r in st.all().values() if r.get("parent") == tid]
    check("the gate enqueues a review and the task waits",
          settled and len(kids) == 1 and st.get(tid)["state"] == T.BLOCKED)
    child = kids[0]

    branch = ns["review_branch"](child)
    check("the review knows which branch to stand on",
          branch == f"{W.BRANCH_PREFIX}{tid}",
          "read off what the implementor left, which is the only record of it")

    # Now run the review's own turn through the real execute_task and look at
    # the directory it is actually handed. Snapshotted while the turn is
    # happening: the checkout is released before execute_task returns, so
    # looking afterwards finds nothing either way and would pass regardless.
    st.transition(child["id"], T.RUNNING, "claimed by the runner")
    seen = {}

    def run_turn(goal, **kw):
        here = Path(kw["cwd"])
        seen["cwd"] = here
        seen["has_work"] = (here / "fix.py").exists()
        seen["head"] = git(here, "log", "--format=%s", "-1").stdout.strip()
        return types.SimpleNamespace(text='{"ok": true, "summary": "fine"}',
                                     cost_usd=0.0, duration_ms=1, session_id="s")

    import logging
    import threading
    _bot_func("execute_task", tasks=T, task_store=st, store=tmp_store(),
              roles=roles, worktrees=W, Path=Path, run_turn=run_turn,
              review_branch=ns["review_branch"],
              record_branch=ns["record_branch"],
              OUTBOX_ROOT=root / "outbox", SILKWORM_BIN="/x/silkworm",
              permission_args=lambda: [], log=logging.getLogger("test"),
              task_thread=lambda t: ("C1", "1.0"),
              task_state=lambda tid, state, detail="": st.transition(tid, state, detail),
              _thread_lock=lambda key: threading.Lock(),
              repo_guard=lambda *a, **k: contextlib.nullcontext(),
              render_block=lambda _: "", chunk=lambda text: [text],
              to_mrkdwn=lambda text: text, resolve_review=lambda *a, **k: False,
              upload_outbox=lambda *a, **k: [], RUNNING={}, RUNNING_TASKS={},
              ClaudeStopped=ClaudeStopped, ClaudeError=ClaudeError,
              )(st.get(child["id"]))
    where = seen.get("cwd")

    # The point of the whole exercise.
    check("the reviewer's working directory contains the implementor's commits",
          bool(seen.get("has_work")) and seen.get("head") == "the work",
          f"the review ran in {where}, where the change does not exist")
    check("and it is not the main checkout",
          where != repo and not (repo / "fix.py").exists(),
          "a review sent there audits a tree the change never reached")
    check("the prompt it was given names that same directory",
          str(where) in child["goal"],
          "resolve_review predicts the path before the checkout exists; the "
          "two have to agree or the prompt names a directory nobody is in")
    check("and the checkout is released when the review's turn ends",
          not where.exists(),
          "the landing reattaches this branch and git refuses a held one")
    where = W.attach(repo, child.get("parent"), branch, label="review")

    # A branch name alone still leaves it guessing what in the tree is new.
    check("the prompt names the branch", branch in child["goal"])
    base = W.fork_point(repo, branch)
    check("the fork point is the commit the branch was cut from",
          base == git(repo, "rev-parse", "HEAD~1").stdout.strip(),
          "not the base branch's tip, which has moved since")
    check("and the prompt hands it over", base in child["goal"])

    # Every command offered has to be one this reviewer can actually run. A
    # missing base once produced `git log the base commit..HEAD`, which errors.
    prefixes = _re.findall(r"Bash\(([^)]*?):\*\)", roles.REVIEWER_TOOLS)

    def runnable(text, cwd):
        cmds = _re.findall(r"`(git [^`]*)`", text)
        if len(cmds) < 2:
            return False, "it offers no commands to start from"
        for cmd in cmds:
            if not any(cmd.startswith(p) for p in prefixes):
                return False, f"{cmd!r} is not in the reviewer's allowlist"
            r = subprocess.run(cmd.split(), cwd=str(cwd), capture_output=True, text=True)
            if r.returncode != 0 or not r.stdout.strip():
                return False, f"{cmd!r}: {(r.stderr or 'no output').strip()[:90]}"
        return True, ""

    ok, why = runnable(child["goal"], where)
    check("every command it is given is allowed, runs, and shows the work", ok, why)
    ok, why = runnable(roles.review_goal(child, "x", cwd=str(where),
                                         branch=branch, base=""), where)
    check("including when there is no fork point and it must fall back", ok, why)
    check("a placeholder never reaches the reviewer as a revision",
          "the base commit" not in roles.review_goal(child, "x", cwd=str(where),
                                                     branch=branch, base=""))

    # The branch under a review belongs to somebody else.
    W.release(where, delete_empty_branch=False)
    check("releasing the review's checkout leaves the branch alone",
          git(repo, "rev-parse", "--verify", "--quiet", branch).returncode == 0,
          "it is the only copy of the work, and the landing reattaches it")
    landing = W.attach(repo, tid, branch)
    check("and the landing can then have the branch", landing is not None,
          "git refuses a branch already checked out in another worktree")
    W.release(landing, delete_empty_branch=False)

    # Nothing between the two turns holds a directory open: the branch is
    # durable, so a review that never runs leaks nothing.
    check("the implementor's scope named a checkout that is now gone",
          not Path(impl["scope"]["worktree"]).exists(),
          "without this the next check passes whether or not anything strips it")
    check("the review's scope carries no released worktree",
          "worktree" not in (child.get("scope") or {}),
          "inherited verbatim, it points at a directory that no longer exists")

    src = (BASE / "bot.py").read_text()
    exe = src[src.index("def execute_task("):src.index("def verify_work(")]
    gate = src[src.index("def resolve_review("):src.index("def land_if_ready(")]
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "execute_task")
    # The review's checkout is the detached one; the other attach is a resumed
    # task getting its own branch back after a restart.
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "attach"
             and any(k.arg == "detach" for k in n.keywords)]
    check("execute_task checks out the branch under review",
          len(calls) == 1 and any(k.arg == "label" for k in calls[0].keywords),
          "without this the reviewer runs in whatever scope it inherited")
    check("and it is the branch review_branch names, not one guessed from an id",
          any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "review_branch"
              for n in ast.walk(fn)))
    check("what it borrows is never recorded as its own work",
          "if not borrowed:" in exe and "record_branch" in exe,
          "the implementor's commits would be filed against the review")
    dels = [n for n in ast.walk(fn) if isinstance(n, ast.keyword)
            and n.arg == "delete_empty_branch"]
    check("and is never deleted as an empty branch", len(dels) == 2 and all(
        isinstance(d.value, ast.UnaryOp) and isinstance(d.value.op, ast.Not)
        and getattr(d.value.operand, "id", "") == "borrowed" for d in dels),
        "release() deletes on a commit count that has read as zero before now")
    # Anchored on the release itself, not on how it is called: a change to the
    # arguments is a different fault, and this check should not claim it.
    released, acted = (exe.find("worktrees.release(worktree"),
                       exe.find("resolve_review("))
    check("the checkout goes before the verdict is acted on",
          released != -1 and acted != -1 and released < acted,
          "the landing reattaches the same branch and git refuses a held one")
    check("the branch is read from the record, not the dict in hand",
          "(task_store.get(tid) or task).get(\"branch\")" in gate,
          "record_branch wrote it to the store on the way out of the checkout; "
          "the record this turn has been carrying predates that")

    # --- a review that cannot reach the work must not certify it -------------
    # Carrying on in the main checkout is worse than not reviewing: the prompt
    # has already promised a checkout of the branch, so `git log <fork>..HEAD`
    # there reads whatever has landed on the base branch since -- other
    # people's commits, which it finds nothing wrong with, and a passing
    # verdict on those lands work nobody looked at.
    posted, ran = [], []

    class Client:
        def chat_postMessage(self, **kw):
            posted.append(kw["text"]); return {"ts": "1.1"}

    class Progress:
        ts = "1.1"
        def update(self, *a, **k): pass
        def finalize(self, text): posted.append(text)

    def review_turn(child_rec, **extra):
        return _bot_func(
            "execute_task", tasks=T, task_store=st, store=tmp_store(),
            roles=roles, worktrees=W, Path=Path,
            review_branch=ns["review_branch"], record_branch=ns["record_branch"],
            run_turn=lambda goal, **kw: (
                ran.append(Path(kw["cwd"])) or
                types.SimpleNamespace(text='{"ok": true, "summary": "fine"}',
                                      cost_usd=0.0, duration_ms=1, session_id="s")),
            OUTBOX_ROOT=root / "outbox", SILKWORM_BIN="/x/silkworm",
            permission_args=lambda: [], log=logging.getLogger("test"),
            task_thread=lambda t: ("C1", "1.0"),
            task_state=lambda tid_, state, detail="": st.transition(tid_, state, detail),
            _thread_lock=lambda key: threading.Lock(),
            repo_guard=lambda *a, **k: contextlib.nullcontext(),
            render_block=lambda _: "", chunk=lambda text: [text],
            to_mrkdwn=lambda text: text, resolve_review=lambda *a, **k: False,
            upload_outbox=lambda *a, **k: [], RUNNING={}, RUNNING_TASKS={},
            ClaudeStopped=ClaudeStopped, ClaudeError=ClaudeError,
            ProgressMessage=lambda *a, **k: Progress(), app=types.SimpleNamespace(
                client=Client()), **extra)(child_rec)

    gone = st.create("add another fix", role="implementor", driver="queue",
                     isolate=True, scope={"cwd": str(repo)})["id"]
    st.transition(gone, T.RUNNING)
    gwt = W.create(repo, gone, fetch=False)
    (gwt / "g.py").write_text("x\n")
    git(gwt, "add", "-A"); git(gwt, "commit", "-qm", "work that gets lost")
    ns["record_branch"](gone, gwt, "")
    W.release(gwt)
    ns["resolve_review"](st.get(gone), "implementor", "did it", "C1", "1.0")
    orphan = [r for r in st.all().values() if r.get("parent") == gone][0]
    # The branch goes -- dropped by hand, or set aside by a create() that
    # could not make its worktree any other way.
    git(repo, "branch", "-D", f"{W.BRANCH_PREFIX}{gone}")
    st.transition(orphan["id"], T.RUNNING)
    review_turn(st.get(orphan["id"]),
                refuse_review=lift(
                    "refuse_review", roles=roles, tasks=T, task_store=st,
                    log=logging.getLogger("test"),
                    task_state=lambda tid_, s_, d="": st.transition(tid_, s_, d),
                    app=types.SimpleNamespace(client=Client()))["refuse_review"])

    check("a review with no checkout does not run at all", ran == [],
          f"it ran in {ran}, which is not the tree it was told it was in")
    check("and returns no verdict", st.get(orphan["id"])["state"] == T.FAILED)
    check("the work it was gating waits for you instead",
          st.get(gone)["state"] == T.AWAITING_APPROVAL,
          "failing only the review strands its parent in blocked forever")
    check("and is not recorded as having passed",
          not ((st.get(gone).get("result") or {}).get("review") or {}).get("ok"),
          "a landing runs off that flag")
    check("and it says so rather than going quiet",
          any("could not check out" in t for t in posted), posted)

    # A directory that is not on the branch must not stand in for one.
    stale = W.path_for(repo, "tsk_stale", "review")
    stale.mkdir(parents=True)
    check("a stale directory is refused, not reused",
          W.attach(repo, "tsk_stale", branch, label="review") is None,
          "handed over as the checkout, a review would audit whatever is in it")
    stale.rmdir()

    # A sweep that cannot read the id back out of the path deletes it anyway.
    held = W.attach(repo, tid, branch, label="review")
    W.sweep(keep={tid}, min_age_s=0)
    check("a sweep keeps a checkout the waiting task still owns",
          held.exists() and (held / "fix.py").exists(),
          "the task id sits behind the label, so splitting from the front "
          "read it as 'review--<id>' and matched nothing in the keep set")
    # Cancelling is what makes this load-bearing: while the parent is merely
    # blocked it is non-terminal and kept anyway, so the check would pass on
    # its own and prove nothing.
    st.transition(tid, T.CANCELLED, "changed my mind mid-review")
    keeping = lift("live_worktree_tasks", tasks=T, task_store=st,
                            RUNNING_TASKS={child["id"]: object()}
                            )["live_worktree_tasks"]()
    check("a cancelled task is no longer kept on its own account",
          tid not in {t for t, r in st.all().items()
                      if r.get("state") not in T.TERMINAL})
    check("but its checkout survives while a review is standing in it",
          W.sweep(keep=keeping, min_age_s=0) == 0 and held.exists(),
          "the checkout is keyed by the task under review, so cancelling that "
          "task drops the directory its reviewer is currently in")
    W.sweep(keep=set(), min_age_s=0)
    check("and takes it once nothing owns it", not held.exists())

    # A failed release of a borrowed checkout is the one note worth keeping:
    # nothing else records that the branch is still held, and the landing will
    # refuse to reattach it for a reason stated nowhere.
    exe = (BASE / "bot.py").read_text()
    exe = exe[exe.index("def execute_task("):exe.index("def verify_work(")]
    check("a borrowed checkout that would not go is reported",
          "if not removed:" in exe and "still held" in exe,
          "suppressing the note suppressed the failure with it")

    # The path the prompt predicts is resolved the way execute_task resolves
    # it. A project with a base branch and no directory is a real shape.
    gate = (BASE / "bot.py").read_text()
    gate = gate[gate.index("def resolve_review("):gate.index("def land_if_ready(")]
    check("the review's directory falls back the way the turn's does",
          'scope.get("cwd") or CLAUDE_CWD' in gate,
          "empty cwd named the branch and then sent it to \"?\"")


# --- work is proven, not believed ------------------------------------------------
# The reviewer is read-only and cannot run anything, and said so in a real
# review: "could not run ./bin/silkworm test here, so the '567 passed' claim is
# unverified." Merging on that would be merging on an opinion.

def test_verification():
    import verify as V
    print("\nwork is verified by running the tests, not by asking")

    check("a passing command passes", V.run("true", "/tmp")["ok"])
    r = V.run("false", "/tmp")
    check("a failing command fails", r["ran"] and not r["ok"] and r["code"] == 1)
    check("output is captured for the failure message",
          "hello" in V.run("sh -c 'echo hello; exit 1'", "/tmp")["output"])

    none = V.run("", "/tmp")
    check("no command means it did not run", none["ran"] is False,
          "which is not the same as failing")
    missing = V.run("no-such-binary-xyz", "/tmp")
    check("an unrunnable command did not run either", missing["ran"] is False,
          "a missing binary is a configuration problem, not a failing test")
    check("and says which", "could not run" in missing["output"])
    slow = V.run("sleep 5", "/tmp", timeout=1)
    check("a suite that never finishes fails rather than hanging",
          slow["ran"] and not slow["ok"] and "did not finish" in slow["output"])

    check("a run that did not happen is never reported as passing",
          not none["ok"] and not missing["ok"])
    check("its summary says unverified, not failed",
          "Not verified" in V.summary(none), V.summary(none))
    check("a real failure says failed", ":x:" in V.summary(r))
    check("the rework note carries the actual output",
          "Output tail" in V.rework_note(r))
    check("and tells it not to bend the tests to fit",
          "rather than adjusting the tests" in V.rework_note(r))

    src = (BASE / "verify.py").read_text()
    check("verification is a subprocess, not an agent",
          "claude" not in src.lower() and "subprocess.run" in src,
          "a model in the loop could report a suite green that was not")

    bot = (BASE / "bot.py").read_text()
    ex = bot[bot.index("def execute_task"):bot.index("MAX_VERIFY_ATTEMPTS")]
    check("tests run before the reviewer is spent",
          ex.index("verify_work(task, cwd)") < ex.index("resolve_review("),
          "reviewing work that fails its own tests wastes a session")
    # Verification ran *after* the worktree was released, so it pointed at a
    # deleted directory. verify.run reported "could not run", the caller read
    # that as "nothing to verify", and every task passed unverified in silence.
    check("and before the checkout they ran in is released",
          ex.index("verify_work(task, cwd)") < ex.index("worktrees.release(worktree,"),
          "afterwards there is nothing left to test")
    rw = bot[bot.index('if action == "rework"'):bot.index('if action in ("accept"')]
    rw += bot[bot.index("def send_back("):bot.index("def rework_flagged_review(")]
    check("sending work back clears the previous review",
          "blocked_on=[]" in rw,
          "both the gate and verification are guarded by `not blocked_on`, so a "
          "stale id made a reworked task skip both and go straight to done")
    check("and clears the stale verdict with it", "verified=None" in rw)
    check("an unrun suite does not send work back",
          'if checked["ran"]:' in ex,
          "a project with no test command must not be treated as failing")
    sb = bot[bot.index("def send_back_for_tests"):bot.index("def resolve_review")]
    check("a failure goes back with the output attached",
          "verify.rework_note(result)" in sb)
    check("attempts are counted", "verify_attempts" in sb)
    check("and it stops asking after a few",
          "tasks.AWAITING_APPROVAL" in sb,
          "work that cannot pass is not one more session away from passing")

    import tasks as T
    # The first live run wedged here: send-back does running -> queued, which
    # was not a legal move, so task_state refused it and swallowed the refusal
    # by design. The task sat in `running` for ever.
    check("work can be sent back to be redone",
          T.QUEUED in T.TRANSITIONS[T.RUNNING],
          "without it the send-back is refused and the task never moves again")
    check("a refused transition is at least logged",
          "could not move to" in (BASE / "bot.py").read_text(),
          "it is swallowed so a reply is never lost, so the log is the only trace")
    check("the record says whether work was proven", "verified" in T.FIELDS)
    check("with None meaning not run, not False",
          T.default("verified") is None)


# --- landing work without a person -----------------------------------------------
# Eight branches accumulated off one base and none were merged. Two of them
# turned out to implement the same fix independently, each passing alone.

def test_landing():
    import merge as M, verify as V, worktrees as W
    import threading
    print("\nwork lands only once it has been shown to work")

    import projects as P_
    root = Path(tempfile.mkdtemp())
    repo = root / "repo"; repo.mkdir()
    def g(cwd, *a): return subprocess.run(["git", *a], cwd=str(cwd),
                                          capture_output=True, text=True)
    g(repo, "init", "-q", "-b", "main")
    g(repo, "config", "user.email", "t@t"); g(repo, "config", "user.name", "t")
    # Plain text, not Python: a two-line module lets git auto-resolve a
    # whole-file rewrite, and identical-length edits let a stale .pyc answer
    # for the new source. Both hid what this is trying to measure.
    (repo / "v.txt").write_text("1\n")
    (repo / "expect.txt").write_text("1\n")
    g(repo, "add", "-A"); g(repo, "commit", "-qm", "base")
    CMD = "sh -c 'test \"$(cat v.txt)\" = \"$(cat expect.txt)\"'"
    tests = lambda cwd: V.run(CMD, cwd)                # noqa: E731

    def branch(name, value):
        wt = root / name
        g(repo, "worktree", "add", "-q", "-b", name, str(wt), "main")
        (wt / "v.txt").write_text(f"{value}\n")
        (wt / "expect.txt").write_text(f"{value}\n")
        g(wt, "add", "-A")
        g(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name)
        return wt

    # Both cut from the same base *before* either lands -- which is the actual
    # situation, and the reason an earlier version of this test proved nothing:
    # a branch created after the first landing has nothing to conflict with.
    a = branch("a", 2)
    b = branch("b", 3)

    # Most projects never name a base. Passing the empty string straight to
    # `git rebase` gave "fatal: invalid upstream ''" and refused every landing
    # -- the first real one failed exactly there.
    z = branch("z", 5)
    check("an unnamed base resolves to the default branch",
          M.land(z, repo, "z", "", tests)["landed"],
          "scope.branch is None for most projects")
    g(repo, "reset", "--hard", "HEAD~1")


    check("a proven branch lands", M.land(a, repo, "a", "main", tests)["landed"])
    check("history stays linear",
          len(g(repo, "log", "--oneline").stdout.strip().splitlines()) == 2,
          "fast-forward only, so no merge commit resolves anything unreviewed")

    check("the second branch passes on its own", tests(b)["ok"])
    r = M.land(b, repo, "b", "main", tests)
    check("but is refused once the base has moved under it",
          not r["landed"] and r["stage"] == "rebase",
          f"got {r['stage']} — two agents fixing the same thing must not both land")

    # Running the suite litters the checkout it tested; that must not block the
    # next landing, which is what counting untracked files did.
    # A file inside it: git does not track empty directories, so mkdir alone
    # left this assertion testing nothing at all.
    (repo / "__pycache__").mkdir(exist_ok=True)
    (repo / "__pycache__" / "stale.pyc").write_bytes(b"\x00")
    c = branch("c", 2)
    (c / "note.txt").write_text("harmless\n")
    g(c, "add", "-A"); g(c, "-c", "user.email=t@t", "-c", "user.name=t",
                         "commit", "-qm", "note")
    check("build droppings do not block the next landing",
          M.land(c, repo, "c", "main", tests)["landed"],
          "the first landing's own test run left artefacts behind")

    # Uncommitted work in the base is someone's edit and must stop it.
    (repo / "v.txt").write_text("99\n")
    d = branch("d", 2)
    r = M.land(d, repo, "d", "main", tests)
    check("uncommitted work in the base stops a landing",
          not r["landed"] and r["stage"] == "base-dirty")
    g(repo, "checkout", "--", "v.txt")

    # And the safety net: passes in the worktree, breaks the base.
    before = g(repo, "rev-parse", "HEAD").stdout.strip()
    e = branch("e", 2)
    (e / "x.txt").write_text("x\n")
    g(e, "add", "-A"); g(e, "-c", "user.email=t@t", "-c", "user.name=t",
                         "commit", "-qm", "x")
    r = M.land(e, repo, "e", "main",
               lambda cwd: {"ran": True, "ok": str(cwd).endswith("/e"),
                            "code": 1, "output": "broke the base"})
    check("a merge that breaks the base is reverted",
          not r["landed"] and r["stage"] == "tests-after-merge")
    check("and the base is exactly where it was",
          g(repo, "rev-parse", "HEAD").stdout.strip() == before)

    # And with a real remote, where the resolved base is the symbolic ref
    # `origin/HEAD`: splitting that on "/" yields "HEAD", not the branch it
    # points at, so the checkout-is-on-the-base guard compared main against
    # HEAD and refused. A repo with no remote never exercises this.
    origin = root / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], capture_output=True)
    g(repo, "remote", "add", "origin", str(origin))
    g(repo, "push", "-q", "-u", "origin", "main")
    g(repo, "remote", "set-head", "origin", "main")
    check("origin/HEAD is resolved to its branch, not the word HEAD",
          g(repo, "rev-parse", "--abbrev-ref", "origin/HEAD").stdout.strip() == "origin/main",
          "this is what the guard has to cope with")
    y = branch("y", 6)
    r = M.land(y, repo, "y", "", tests)
    check("a landing works against a repo with a remote",
          r["landed"], f"refused at {r.get('stage')}: {str(r.get('detail'))[:70]}")

    # The lockout, which no fixture without a remote can show. Landing
    # fast-forwards the *local* base, and publishes only if the project asked
    # -- so after a landing the local base is normally ahead of origin. A
    # task's worktree is cut from `origin/HEAD`, meaning the next branch starts
    # where origin is, not where the base it will be merged into is. Rebasing
    # that branch onto origin/main is then a no-op leaving it without the
    # commit the base gained, and the fast-forward is impossible -- not just
    # for that branch, but for every branch from then on. Seen exactly this
    # way: local main ahead of origin and eight branches refusing at 'merge'.
    def branch_from(name, start):
        wt = root / name
        g(repo, "worktree", "add", "-q", "-b", name, str(wt), start)
        # Its own file. This is about which commit is the base, not about
        # conflicts -- touching v.txt would fail the rebase for a different
        # reason and the case would pass while proving nothing.
        (wt / f"{name}.txt").write_text(f"{name}\n")
        g(wt, "add", "-A")
        g(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name)
        return wt

    check("that landing left the local base ahead of origin",
          int(g(repo, "rev-list", "--count",
                "origin/main..main").stdout.strip()) == 1,
          "without this the rest of the case cannot arise")
    w = branch_from("w", "origin/main")
    check("and a branch cut from origin lacks what the base gained",
          g(repo, "merge-base", "--is-ancestor", "main", "w").returncode != 0,
          "this is how every task's worktree is cut")
    r = M.land(w, repo, "w", "", tests)
    check("a second branch lands even though it was cut from the old base",
          r["landed"],
          f"refused at {r.get('stage')}: {str(r.get('detail'))[:200]}")
    check("and the base fast-forwarded onto it",
          g(repo, "rev-parse", "HEAD").stdout.strip() ==
          g(repo, "rev-parse", "w").stdout.strip())

    # The other half of the same bug: what the *next* task is cut from.
    # `base_ref` preferred the remote-tracking ref unconditionally, which is
    # right while origin is the more advanced of the two and wrong the moment a
    # landing moves the local base past it -- the task then starts from a
    # baseline missing work that has already landed. That is how the same fix
    # came to be implemented twice, by two agents neither of whom could see the
    # other's landing.
    check("the local base is ahead of origin at this point",
          int(g(repo, "rev-list", "--count",
                "origin/main..main").stdout.strip()) > 0,
          "otherwise there is no difference for base_ref to prefer")
    check("a base ahead of its remote is what the next task is cut from",
          W.base_ref(repo, fetch=False) == "main",
          "cutting from origin/main hands the task a stale baseline")
    check("and the same when the project names its base",
          W.base_ref(repo, fetch=False, prefer="main") == "main")
    check("a worktree cut now actually contains the landed work",
          g(repo, "merge-base", "--is-ancestor", "main",
            W.base_ref(repo, fetch=False)).returncode == 0)

    # A base that *genuinely* moves between the proof and the merge is a
    # different failure and has to read as one. `--ff-only` refused both in the
    # same words, which is part of why the lockout above went unnoticed.
    u = branch_from("u", "main")
    def moves_base_mid_landing(cwd):
        if str(cwd) != str(repo):
            g(repo, "commit", "-q", "--allow-empty", "-m", "another landing")
        return tests(cwd)
    r = M.land(u, repo, "u", "", moves_base_mid_landing)
    check("a base that moves between the proof and the merge is named as that",
          not r["landed"] and r["stage"] == "base-moved",
          f"got {r.get('stage')}: {str(r.get('detail'))[:200]}")
    check("and the refusal names both commits rather than blaming git",
          "was at" in r["detail"] and "is at" in r["detail"],
          r["detail"][:200])

    # The fast-forward is the first step that moves the base, and the suite
    # after it can run for minutes. A restart in that window leaves the base
    # moved and never reset, so land checkpoints what the base was just before
    # -- and the checkpoint must be written while the base has not moved yet.
    ck = branch_from("ck", "main")
    seen = []
    def checkpoint(name, before):
        seen.append((name, before, g(repo, "rev-parse", "HEAD").stdout.strip()))
    r = M.land(ck, repo, "ck", "", tests, on_merge=checkpoint)
    check("a landing checkpoints the base once, just before it moves it",
          r["landed"] and len(seen) == 1 and seen[0][0] == "main"
          and seen[0][1] == seen[0][2] and r["head"] != seen[0][1],
          f"got {seen} / {r.get('stage')}")
    ck2 = branch_from("ck2", "main")
    at = g(repo, "rev-parse", "HEAD").stdout.strip()
    def cannot(name, before):
        raise OSError("disk full")
    r = M.land(ck2, repo, "ck2", "", tests, on_merge=cannot)
    check("a checkpoint that cannot be written refuses the merge",
          not r["landed"] and r["stage"] == "checkpoint"
          and g(repo, "rev-parse", "HEAD").stdout.strip() == at,
          f"got {r.get('stage')}")

    # Publishing. Landing moves the local branch; origin hears about it only if
    # the project asked. Off, the two drift -- survivable for landing now that
    # the base is the local commit, but not for the *next* task, whose worktree
    # is cut from origin and so cannot see what already landed. Not
    # hypothetical: two tasks independently implemented the same fix.
    check("publishing is off by default", P_.default("publish") is False,
          "pushing to a shared remote is the project's decision, not ours")
    check("and nothing so far has been published",
          int(g(repo, "rev-list", "--count",
                "origin/main..main").stdout.strip()) > 0,
          "the local base is ahead of origin, as an unpublished landing leaves it")

    pub = branch_from("pub", "main")
    r = M.land(pub, repo, "pub", "", tests, publish=True)
    check("a landing publishes when the project asks for it", r["landed"],
          f"refused at {r.get('stage')}: {str(r.get('detail'))[:200]}")
    check("and origin then agrees with the local base",
          g(repo, "rev-parse", "origin/main").stdout.strip() ==
          g(repo, "rev-parse", "main").stdout.strip(),
          "this is what lets the next task's worktree see landed work")
    check("and the result says so rather than just 'landed'",
          r.get("published") is True)
    check("as does the reply",
          "pushed to origin" in M.summary(r, "pub"),
          "'landed' meaning local-only is how origin drifted unnoticed")

    # A push that is refused -- someone else having pushed to the base is the
    # ordinary case. The landing stands, and says it was not pushed.
    #
    # Undoing it instead would throw away work that has passed the suite twice
    # because a remote was briefly out of reach, and the reason local-ahead
    # used to be worth undoing is gone: the merge targets this checkout's own
    # commit, and `base_ref` above cuts the next task from a local base that is
    # ahead of its remote. Neither the lockout nor the stale baseline survives.
    rival = root / "rival"
    g(repo, "worktree", "add", "-q", "--detach", str(rival), "origin/main")
    (rival / "rival.txt").write_text("rival\n")
    g(rival, "add", "-A")
    g(rival, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "rival")
    g(rival, "push", "-q", "origin", "HEAD:main")

    at_start = g(repo, "rev-parse", "HEAD").stdout.strip()
    nope = branch_from("nope", "main")
    r = M.land(nope, repo, "nope", "", tests, publish=True)
    check("a landing whose push is refused still lands",
          r["landed"], f"got {r.get('stage')}: {str(r.get('detail'))[:200]}")
    check("but does not claim to have published",
          r.get("published") is False,
          "saying 'landed' for local-only is how origin drifted unnoticed")
    check("and the base keeps the commit rather than the work being redone",
          g(repo, "rev-parse", "HEAD").stdout.strip() ==
          g(repo, "rev-parse", "nope").stdout.strip(),
          "it passed the suite twice; an unreachable remote is not a reason "
          "to pay for both runs again")
    check("and the base really did move, so that is not vacuously true",
          g(repo, "rev-parse", "HEAD").stdout.strip() != at_start)
    check("the reply says it is ahead of origin, and why",
          "local only, not pushed" in M.summary(r, "nope") and
          "could not be pushed" in M.summary(r, "nope"),
          M.summary(r, "nope")[:300])
    check("while the branch itself is untouched, so it can be retried",
          g(repo, "rev-parse", "--verify", "--quiet", "nope").returncode == 0)

    # Strictly ahead, though. The rejected push leaves each side holding
    # something the other does not, and a diverged remote is still the shared
    # truth -- branching from local there would build on something nobody else
    # has agreed to.
    check("the base and origin have diverged now",
          g(repo, "merge-base", "--is-ancestor", "origin/main",
            "main").returncode != 0,
          "otherwise the diverged case is not being exercised")
    check("a diverged base falls back to the remote",
          W.base_ref(repo, fetch=False) == "origin/HEAD",
          "ahead is not the same as diverged")

    # ...which is the one state that tells the two halves of the lockout fix
    # apart. While the base is merely *ahead*, `base_ref` returns the local
    # branch and the rebase target is the same commit either way, so the case
    # above passes with either half reverted. Diverged, `base_ref` rightly
    # names the remote -- and rebasing onto it would leave the branch off a
    # commit the base is not descended from, which `--ff-only` cannot take.
    # Only reading the commit off the checkout survives this.
    #
    # The divergence is built here rather than inherited from the refused push
    # above. Reverting the rebase target makes that push *succeed*, which puts
    # the two refs back in step -- so the case would have failed on its own
    # fixture instead of on its claim, and said nothing about the fix.
    g(repo, "commit", "-q", "--allow-empty", "-m", "only here")
    mine = root / "mine"
    g(repo, "worktree", "add", "-q", "--detach", str(mine), "origin/main")
    g(mine, "commit", "-q", "--allow-empty", "-m", "only on origin")
    g(mine, "push", "-q", "origin", "HEAD:main")
    g(repo, "fetch", "-q", "origin")
    check("the base and origin each hold what the other does not",
          g(repo, "merge-base", "--is-ancestor", "origin/main",
            "main").returncode != 0 and
          g(repo, "merge-base", "--is-ancestor", "main",
            "origin/main").returncode != 0,
          "this case needs a genuine divergence, not merely being ahead")
    check("and base_ref names the remote, as a diverged base should",
          W.base_ref(repo, fetch=False) == "origin/HEAD")
    dv = branch_from("dv", "main")
    check("the branch is not descended from the ref base_ref names",
          g(repo, "merge-base", "--is-ancestor", "origin/main", "dv").returncode != 0,
          "otherwise rebasing onto the remote would work and prove nothing")
    r = M.land(dv, repo, "dv", "", tests)
    check("a landing onto a diverged base still merges into the local commit",
          r["landed"], f"refused at {r.get('stage')}: {str(r.get('detail'))[:200]}")
    check("and the base is that branch, not the remote's tip",
          g(repo, "rev-parse", "HEAD").stdout.strip() ==
          g(repo, "rev-parse", "dv").stdout.strip())

    # A push can also fail *after* the remote accepted it, and a push that
    # times out may or may not have taken. Resetting then would drop a commit
    # that is already published -- a worse mess than the one this prevents --
    # so the outcome is settled by asking origin rather than by the exit code.
    # Back in step first -- origin is ahead after the rejection above, and a
    # push that fails for that ordinary reason would not exercise this at all.
    g(repo, "fetch", "-q", "origin")
    g(repo, "reset", "-q", "--hard", "origin/main")
    check("the base and origin start this case in step",
          g(repo, "rev-parse", "HEAD").stdout.strip() ==
          g(repo, "rev-parse", "origin/main").stdout.strip(),
          "otherwise the push fails for a different reason and proves nothing")
    ok = branch_from("ok", "main")
    real_push = M._git
    def push_lies(cwd, *a, **kw):
        out = real_push(cwd, *a, **kw)
        if a and a[0] == "push":
            real_push(cwd, *a, **kw)      # it really does reach origin
            out.returncode = 1            # and then reports otherwise
        return out
    M._git = push_lies
    try:
        r = M.land(ok, repo, "ok", "", tests, publish=True)
    finally:
        M._git = real_push
    check("a push that reports failure after origin took it still counts",
          r["landed"] and r.get("published") is True,
          f"got {r.get('stage')}: {str(r.get('detail'))[:200]}")
    check("and the base keeps the commit origin already has",
          g(repo, "rev-parse", "origin/main").stdout.strip() ==
          g(repo, "rev-parse", "main").stdout.strip(),
          "resetting here would drop a published commit")
    check("with the two in step, base_ref names the remote as usual",
          W.base_ref(repo, fetch=False) == "origin/HEAD",
          "the local branch is only preferred while it is actually ahead")

    # And a push that cannot even be run. This is the one step in `land` that
    # happens *after* the base has moved, so an exception escaping here would
    # leave the work merged while the caller recorded "landing errored" and
    # never wrote `result.landed` -- the only record that the commits reached
    # the base. The landing has to survive it.
    boom = branch_from("boom", "main")
    def push_explodes(cwd, *a, **kw):
        if a and a[0] in ("push", "ls-remote"):
            raise OSError("git is not on the path")
        return real_push(cwd, *a, **kw)
    M._git = push_explodes
    try:
        r = M.land(boom, repo, "boom", "", tests, publish=True)
    finally:
        M._git = real_push
    check("a push that cannot run does not take the landing down with it",
          r.get("landed") is True,
          f"got {r.get('stage')}: {str(r.get('detail'))[:200]}")
    check("it is reported unpublished, with the reason",
          r.get("published") is False and "could not be pushed" in r.get("detail", ""),
          str(r.get("detail"))[:200])
    check("and the base really does hold the work",
          g(repo, "rev-parse", "HEAD").stdout.strip() ==
          g(repo, "rev-parse", "boom").stdout.strip(),
          "otherwise the case is not exercising a landing that already merged")

    # The base's *name* is not only a label: publishing pushes it. Resolving it
    # by taking the last path segment made a base of `research/main` read as
    # `main`, which in a checkout parked on main passed the is-it-on-the-base
    # guard by comparing "main" against "main" -- and then merged and pushed
    # `main`, the wrong branch on a shared remote, reported as a clean landing.
    # Measured exactly that way. The survey's resolver had already been fixed
    # for slashed names; this is the second copy of it that had not.
    g(repo, "fetch", "-q", "origin")
    g(repo, "reset", "-q", "--hard", "origin/main")
    g(repo, "branch", "-f", "research/main", "HEAD~1")
    g(repo, "push", "-q", "-u", "origin", "research/main")
    was_main = g(repo, "rev-parse", "main").stdout.strip()
    was_slashed = g(repo, "rev-parse", "research/main").stdout.strip()
    check("the fixture's two branches really are different commits",
          was_main != was_slashed,
          "otherwise pushing the wrong one is indistinguishable")
    slashed = branch_from("slashed", "research/main")
    r = M.land(slashed, repo, "slashed", "research/main", tests, publish=True)
    check("a base whose name holds a slash is not chopped to its last segment",
          not r["landed"] and r["stage"] == "base-branch",
          f"got {r.get('stage')}/{r.get('base')}: {str(r.get('detail'))[:200]}")
    check("and the refusal names the base in full",
          "research/main" in r.get("detail", ""), str(r.get("detail"))[:200])
    check("so the branch that merely shares its last segment is untouched",
          g(repo, "rev-parse", "main").stdout.strip() == was_main and
          g(repo, "ls-remote", "origin",
            "refs/heads/main").stdout.split()[0] == was_main,
          "it would otherwise have been merged and pushed instead")

    g(repo, "remote", "remove", "origin")

    # And when there is genuinely nothing to resolve: no main, no master, no
    # remote to ask. base_ref's last resort is "HEAD", which is right for
    # *starting* a task and wrong for landing one -- HEAD resolves to whichever
    # branch the checkout is parked on, so the is-it-on-the-base guard compares
    # a name against itself and can never refuse, and `git rebase HEAD` rebases
    # the worktree onto itself. Measured before the fix: the work landed on a
    # branch called "wip", unrebased, and reported success.
    lone = root / "lone"; lone.mkdir()
    g(lone, "init", "-q", "-b", "wip")
    g(lone, "config", "user.email", "t@t"); g(lone, "config", "user.name", "t")
    (lone / "v.txt").write_text("1\n"); (lone / "expect.txt").write_text("1\n")
    g(lone, "add", "-A"); g(lone, "commit", "-qm", "base")
    check("the fixture really has no base to find",
          W.base_ref(lone, fetch=False) == "HEAD",
          "otherwise this measures nothing")
    lw = root / "lonework"
    g(lone, "worktree", "add", "-q", "-b", "lb", str(lw), "wip")
    (lw / "v.txt").write_text("2\n"); (lw / "expect.txt").write_text("2\n")
    g(lw, "add", "-A")
    g(lw, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "lb")
    at = g(lone, "rev-parse", "HEAD").stdout.strip()
    r = M.land(lw, lone, "lb", "", tests)
    check("an unresolvable base refuses rather than guessing one",
          not r["landed"] and r["stage"] == "base-unresolved",
          f"got {r.get('stage')} — it would land on whatever was checked out")
    check("and the checkout is untouched by the refusal",
          g(lone, "rev-parse", "HEAD").stdout.strip() == at)

    bot = (BASE / "bot.py").read_text()
    li = bot[bot.index("def landing_enabled"):bot.index("def run_email_ingest")]
    check("the gate names the branch the way the survey does",
          "branches.name_for(task)" in li and "BRANCH_PREFIX" not in li,
          "two ways to name it are two things that can disagree")
    # Opting in is still decided in one place. What it gates changed: an
    # unasked-for landing, not one a person approved.
    check("a project must opt in, decided in one place",
          li.count('"auto_merge"') == 1 and "if not landing_enabled(task) and not approved:" in li,
          "the gate and the board must not disagree about who lands its own work")
    check("and must be able to prove itself", 'proj.get("test_cmd")' in li,
          "landing on a project with no suite is merging on a guess")
    check("unverified work never lands", 'task.get("verified")' in li)
    check("landing takes the checkout guard",
          "with repo_guard(cwd):" in li,
          "it writes to the tree conversations share")
    check("the branch is reattached, since its worktree is long gone",
          "worktrees.attach(" in li)
    check("and the temporary checkout is always released",
          "finally:" in li and "worktrees.release(here)" in li)
    check("auto-merge is off by default", P_.default("auto_merge") is False)
    check("and the project's publishing choice reaches the landing",
          'publish=bool(proj.get("publish"))' in li,
          "otherwise the setting exists and does nothing")
    pub = bot[bot.index('if action == "publish"'):bot.index('if action == "ideate"')]
    check("publishing cannot be turned on without auto-merge",
          'rec.get("auto_merge")' in pub,
          "nothing would land for it to publish")


# (moved here: used only by this file's tests)
#: Words that share nothing, for fixtures that need many different goals.
_DISTINCT_WORDS = ("apple", "bridge", "candle", "dolphin", "engine", "forest",
                   "glacier", "harbor", "island", "jungle", "kettle", "lantern",
                   "meadow", "needle", "orchard", "pepper", "quarry", "river",
                   "saddle", "tunnel", "umbrella", "valley", "walnut", "yarrow",
                   "zephyr")
# --- reviewed work must not read as done while it sits on a branch ------------
# Five branches carrying eleven commits reached `done` with nothing on the
# board to say so. Two of them were the same fix, proposed on two different
# nights, because the first never landed and the gap was still there to find.

# A landing refused at tests-after-merge kept the last 600 characters of the
# suite's output. That output is stdout then stderr, so the tail was the
# suite's own logging -- a deliberate traceback, fixture chatter -- and seven
# refusals in four days never said which test failed. The refusal leads with
# the failing checks, read off the whole output, then the tail.
def test_a_refused_landing_names_the_failing_test():
    import merge as M, verify as V
    print("\na landing refused over failing tests says which ones")

    root = Path(tempfile.mkdtemp())
    repo = root / "repo"; repo.mkdir()
    def g(cwd, *a): return subprocess.run(["git", *a], cwd=str(cwd),
                                          capture_output=True, text=True)
    g(repo, "init", "-q", "-b", "main")
    g(repo, "config", "user.email", "t@t"); g(repo, "config", "user.name", "t")
    (repo / "v.txt").write_text("1\n")
    g(repo, "add", "-A"); g(repo, "commit", "-qm", "base")
    names = iter(range(100))

    def branch():
        name = f"b{next(names)}"
        wt = root / name
        g(repo, "worktree", "add", "-q", "-b", name, str(wt), "main")
        (wt / f"{name}.txt").write_text("x\n")
        g(wt, "add", "-A")
        g(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name)
        return wt, name

    noise = ("cost seeds: skipping unreadable task record junk\n"
             "Traceback (most recent call last):\n"
             + "  File \"turncost.py\", line 83, in _runs\n" * 30
             + "TypeError: 'int' object is not iterable\n")
    check("the fixture's noise alone overflows the old 600-character tail",
          len(noise) > 600, f"{len(noise)} chars")
    said = ("  ✔ something that passed\n"
            "  ✔ a check quoting '✘ not a failure' mid-line\n"
            "  ✔ quoting \"Test case 'Suite/foo()' failed on 'iPhone 17'\"\n"
            "  ✔ quoting 'XCTAssertEqual failed'\n"
            "Failed to install or launch the test runner\n"
            "  ✘ viz_up sends it too\n"
            "  ✔ something else\n\n"
            "2280 passed, 1 failed\n"
            "  FAILED: viz_up sends it too\n" + noise)

    before = g(repo, "rev-parse", "HEAD").stdout.strip()
    wt, name = branch()
    # Failing in the checkout only with the branch on it: a checkout that
    # fails without it too is the checkout's, not the branch's (merge.land 5b).
    def with_branch(cwd, name):
        return str(cwd) != str(repo) or not (repo / f"{name}.txt").exists()
    r = M.land(wt, repo, name, "main",
               lambda cwd: {"ran": True, "ok": with_branch(cwd, name),
                            "code": 1, "output": said})
    d = r.get("detail", "")
    check("refused after the merge, and put back",
          r["stage"] == "tests-after-merge"
          and g(repo, "rev-parse", "HEAD").stdout.strip() == before, r["stage"])
    check("the refusal names the failing check", "✘ viz_up sends it too" in d, d[:300])
    check("and the suite's summary", "2280 passed, 1 failed" in d, d[:300])
    check("ahead of the trailing output",
          0 <= d.find("viz_up sends it too") < d.find("not iterable"), d[:300])
    named = d.split("--- output tail ---")[0]
    check("a passing check that quotes a failure is not listed as one",
          "✔" not in named, named)
    check("nor is a launch error's 'Failed to install'",
          "Failed to install" not in named, named)
    shown = M.summary(r, name)
    check("the Slack summary keeps the reason and the failing check",
          "merging broke the base" in shown and "✘ viz_up sends it too" in shown,
          shown[:300])

    # The same before the merge, against the rebased branch.
    wt, name = branch()
    r = M.land(wt, repo, name, "main",
               lambda cwd: {"ran": True, "ok": False, "code": 1, "output": said})
    check("tests-after-rebase names it too",
          r["stage"] == "tests-after-rebase" and "✘ viz_up sends it too" in r["detail"],
          r.get("detail", "")[:300])

    # Through verify.run itself, whose output keeps only a tail: the failure is
    # on stdout and a long stderr follows it, so it is gone from `output`.
    script = root / "suite.py"
    script.write_text(
        "import sys\n"
        "print('  ✔ fine')\nprint('  ✘ the one that broke')\n"
        "print('\\n1 passed, 1 failed')\n"
        f"sys.stderr.write('noise line\\n' * {V.OUTPUT_CHARS})\n"
        "sys.exit(1)\n", encoding="utf-8")
    run = V.run(f"{sys.executable} {script}", root)
    check("verify.run's own tail has lost the failure",
          "the one that broke" not in run["output"])
    check("but it reads the failures off the whole output",
          "✘ the one that broke" in run.get("failures", "")
          and "1 passed, 1 failed" in run.get("failures", ""), run.get("failures"))
    wt, name = branch()
    r = M.land(wt, repo, name, "main",
               lambda cwd: {"ran": True, "ok": True} if with_branch(cwd, name) else run)
    check("and a landing refused on that run names it",
          "✘ the one that broke" in r.get("detail", ""), r.get("detail", "")[:300])
    check("so does the note sent back to the implementor",
          "✘ the one that broke" in V.rework_note(run))
    ok = V.run(f"{sys.executable} -c \"print('  ✘ quoted, but the run passed')\"", root)
    check("a passing run lists no failures", ok["ok"] and not ok.get("failures"))

    # A suite that never ran a test names nothing, and keeps saying why.
    wt, name = branch()
    r = M.land(wt, repo, name, "main",
               lambda cwd: {"ran": True, "ok": str(cwd) != str(repo), "code": 65,
                            "launch_failure": True,
                            "output": "Application failed preflight checks"})
    check("a launch failure still says what it was",
          r["stage"] == "tests-after-merge"
          and "failed preflight checks" in r["detail"], r.get("detail"))


def test_landing_is_visible():
    import logging
    import types
    import merge as M
    import tasks as T
    import worktrees as W
    print("\na task never reads as done while its commits sit on a branch")

    LOG = logging.getLogger("test")

    # The outcome has to distinguish "we never tried" from "git said no", or
    # the caller can only print it -- which is what it used to do.
    nap = getattr(M, "needs_a_person", None)
    check("a refused landing is distinguishable from one never attempted",
          bool(nap) and nap({"eligible": True, "landed": False, "stage": "rebase"})
          and not nap({"eligible": False, "landed": False, "stage": "not-enabled"})
          and not nap({"eligible": True, "landed": True, "stage": "done"}),
          "merge.needs_a_person is missing; the outcome is still just a sentence")

    # --- the gate itself ------------------------------------------------------
    class Projects:
        def scope_for(self, slug):
            return {}
        def __init__(self, rec):
            self.rec = rec
        def get(self, slug):
            return dict(self.rec)

    import branches as B
    gate = {"project_store": Projects({"auto_merge": True, "test_cmd": "true"}),
            "worktrees": W, "merge": M, "branches": B, "log": LOG}
    lift({"land_if_ready", "landing_enabled"}, ns=gate, absent=LANDING_ROUTES)
    t = {"id": "tsk_guard", "project": "p", "role": "implementor",
         "scope": {"cwd": "/nowhere"}}

    def land_if_ready(task):
        """The gate's answer, or a stand-in for a version that has no answer."""
        fn = gate.get("land_if_ready")
        try:
            return fn(task) if fn else "absent"
        except TypeError:
            return "it took a channel and returned a sentence"

    # send_back_for_tests parks work here after MAX_VERIFY_ATTEMPTS failures.
    # Approving that means "stop trying", not "merge it".
    r = land_if_ready({**t, "verified": False})
    check("work parked by failing tests is never merged by approving it",
          isinstance(r, dict) and not r.get("eligible") and r.get("stage") == "unverified",
          f"got {r!r}")
    gate["project_store"] = Projects({"auto_merge": True, "test_cmd": ""})
    r = land_if_ready({**t, "verified": True})
    check("a project with no suite cannot land on a guess",
          isinstance(r, dict) and not r.get("eligible")
          and r.get("stage") == "no-test-command", f"got {r!r}")
    gate["project_store"] = Projects({"auto_merge": False, "test_cmd": "true"})
    r = land_if_ready({**t, "verified": True})
    check("a project that never opted in is not a refusal anyone must chase",
          isinstance(r, dict) and not r.get("eligible")
          and r.get("stage") == "not-enabled", f"got {r!r}")

    # --- the refusal has somewhere durable to live ----------------------------
    store = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    rec_ns = {"task_store": store, "log": LOG, "time": time}
    lift({"record_landing"}, ns=rec_ns)
    record_landing = rec_ns.get("record_landing")

    task = store.create("do the thing", project="p")
    refused = {"eligible": True, "landed": False, "stage": "rebase",
               "branch": "silkworm/" + task["id"],
               "detail": "conflicts on tests/test_invariants.py"}
    if record_landing:
        record_landing(task, refused)
    landing = ((store.get(task["id"]).get("result") or {}).get("landing")) or {}
    check("a refusal is written to the record, not only posted to the thread",
          landing.get("stage") == "rebase" and landing.get("landed") is False,
          "result.landed is written on success only, so a refusal left no trace")
    check("and it names the branch that is still waiting",
          landing.get("branch") == "silkworm/" + task["id"])

    # A landing runs for minutes. Folding its outcome into the copy of the
    # record the caller took beforehand would drop whatever was written in
    # between -- the review verdict sits right there.
    stale = store.get(task["id"])
    store.update(task["id"], result={**(stale.get("result") or {}),
                                     "review": {"ok": True, "summary": "fine"}})
    if record_landing:
        record_landing(stale, {**refused, "stage": "attach"})
    after = (store.get(task["id"]).get("result")) or {}
    check("a landing outcome does not overwrite what was written beside it",
          (after.get("review") or {}).get("ok") is True
          and (after.get("landing") or {}).get("stage") == "attach",
          f"got {after!r}")

    quiet = store.create("do the thing", project="q")
    if record_landing:
        record_landing(quiet, {"eligible": False, "landed": False,
                               "stage": "not-enabled", "detail": ""})
    check("a project that does not land its own work gets no marker",
          bool(record_landing)
          and "landing" not in ((store.get(quiet["id"]).get("result")) or {}),
          "every task on every other project would carry a false warning")

    # --- approving flagged work goes through the same gate --------------------
    started = []
    app_ns = {"task_store": store, "tasks": T, "log": LOG,
              "holding": __import__("holding"),
              "start_landing": lambda tid, approved=False: bool(started.append((tid, approved)))}
    lift({"approve_task"}, ns=app_ns, absent=('tell_thread',))
    approve = app_ns.get("approve_task")

    def parked(state, detail=""):
        rec = store.create("implement it", project="p", role="implementor")
        store.transition(rec["id"], T.RUNNING)
        store.transition(rec["id"], state, detail)
        store.update(rec["id"], verified=True)
        return rec["id"]

    reviewed = parked(T.AWAITING_APPROVAL, "a review flagged this")
    if approve:
        approve({"id": reviewed, "by": "you"})
    # And says a person asked for it, which is what lets a project with
    # auto-merge off merge approved work at all.
    check("approving flagged work goes through the landing gate",
          bool(approve) and started == [(reviewed, True)],
          "approve maps straight to done; four reviewed commits stayed on a branch")
    check("and approving still completes the task",
          bool(approve) and store.get(reviewed)["state"] == T.DONE)

    started.clear()
    blocked = parked(T.BLOCKED, "awaiting review")
    if approve:
        approve({"id": blocked})
    check("approving a task still waiting on its review does not merge it",
          not started, "that would land work nobody has read")

    # --- and a review whose landing refused does not report success -----------
    outcome = {}
    posted = []
    moves = []

    def task_state(tid, state, detail=""):
        try:
            store.transition(tid, state, detail)
        except T.InvalidTransition as e:
            moves.append(str(e))

    import roles
    rev_ns = {"task_store": store, "tasks": T, "roles": roles, "merge": M,
              "log": LOG, "task_state": task_state,
              "app": types.SimpleNamespace(client=types.SimpleNamespace(
                  chat_postMessage=lambda **kw: posted.append(kw))),
              "land_and_record": lambda tid, ch, ts: outcome,
              # What main calls. Kept so main reaches the state decision and
              # this measures that decision rather than a NameError.
              "land_if_ready": lambda *a, **k: ":hand: _Not landed (rebase)._",
              # The real send-back gates, on a project that is not ready for
              # unsupervised work: these cases must route exactly as before.
              "projects": __import__("projects"), "branches": __import__("branches"),
              "project_store": types.SimpleNamespace(get=lambda slug: None,
                                                       scope_for=lambda slug: {}),
              "tell_thread": lambda key, text: posted.append({"text": text})}
    lift({"resolve_review", "rework_flagged_review", "rework_conflict",
              "send_back", "review_addendum", "_unsupervised", "MAX_REVIEW_REWORKS",
              "MAX_CONFLICT_REWORKS", "CONFLICT_STAGES"}, ns=rev_ns,
         absent=('CLAUDE_CWD', 'conflict_addendum', 'file_followups'))
    resolve_review = rev_ns.get("resolve_review")
    PASS = '```json\n{"ok": true, "summary": "fine", "findings": []}\n```'

    def reviewed_as(result):
        nonlocal outcome
        outcome = result
        pid = parked(T.BLOCKED, "awaiting review")
        if resolve_review:
            resolve_review({"id": "tsk_rev", "parent": pid}, "reviewer", PASS, "C", "1")
        return store.get(pid)

    stuck = reviewed_as({"eligible": True, "landed": False, "stage": "rebase",
                         "detail": "conflicts", "branch": "silkworm/x"})
    check("a passing review whose landing refused does not reach done",
          stuck["state"] == T.AWAITING_APPROVAL,
          f"state is {stuck['state']}; the commits are still only on the branch")
    check("and the board is told why it is waiting",
          "landing refused" in (stuck["events"][-1]["detail"] if stuck["events"] else ""),
          "otherwise it looks like the reviewer flagged something")

    done = reviewed_as({"eligible": True, "landed": True, "stage": "done",
                        "head": "abc1234", "branch": "silkworm/y"})
    check("a landing that succeeded still completes the task",
          done["state"] == T.DONE, f"state is {done['state']}")
    never = reviewed_as({"eligible": False, "landed": False,
                         "stage": "not-enabled", "detail": ""})
    check("and so does a project that never lands its own work",
          never["state"] == T.DONE, f"state is {never['state']}")
    check("no transition was refused along the way", not moves, f"refused: {moves}")

    # --- a landing killed mid-merge must not animate for ever ----------------
    # The landing runs on a daemon thread, which a restart ends without
    # unwinding. Its marker is durable and nothing else revisits it, so the row
    # would keep saying "landing…" about something that stopped days ago.
    import subprocess as _sp
    sweep_ns = {"task_store": store, "log": LOG, "subprocess": _sp, "time": time}
    lift({"record_landing", "clear_interrupted_landings", "LANDING_UNDERWAY",
              "_interrupted_landing_detail"}, ns=sweep_ns)
    sweep = sweep_ns.get("clear_interrupted_landings")
    mid = store.create("do the thing", project="p")
    store.update(mid["id"], result={"landing": {
        "eligible": True, "landed": False, "stage": "in-progress",
        "branch": "silkworm/" + mid["id"], "detail": "landing under way",
        "checkpointed": True}})
    settled = store.create("do the thing", project="p")
    store.update(settled["id"], result={"landing": {
        "eligible": True, "landed": True, "stage": "done", "head": "abc1234"}})
    cleared = sweep() if sweep else []
    after_mid = ((store.get(mid["id"]).get("result") or {}).get("landing")) or {}
    check("a landing a restart interrupted stops claiming to be under way",
          bool(sweep) and cleared == [mid["id"]]
          and after_mid.get("stage") == "interrupted",
          f"got {cleared} / {after_mid.get('stage')!r}")
    check("and, killed before the fast-forward, says nothing was merged",
          (after_mid.get("detail") or "").endswith("nothing was merged"),
          repr(after_mid.get("detail")))

    # A restart during merge.land's post-merge suite leaves the base
    # fast-forwarded and never reset. The checkpoint land writes just before
    # the fast-forward is what tells the two apart -- git alone cannot: an
    # empty branch is already an ancestor of its base.
    detail = sweep_ns.get("_interrupted_landing_detail")
    ask = (lambda task, landing: detail(task, landing)) if detail else (lambda *a: "")
    with tempfile.TemporaryDirectory() as td:
        def g(*a):
            return _sp.run(["git", *a], cwd=td, check=True, capture_output=True,
                           text=True).stdout.strip()
        def commit(m):
            g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
              "--allow-empty", "-m", m)
        g("init", "-q", "-b", "main")
        commit("base")
        before = g("rev-parse", "HEAD")
        g("checkout", "-q", "-b", "silkworm/landed")
        commit("work")
        g("checkout", "-q", "-b", "silkworm/empty", "main")
        g("checkout", "-q", "main")
        task = {"scope": {"cwd": td}}
        ckpt = {"checkpointed": True, "base": "main", "base_before": before}
        unmoved = ask(task, {**ckpt, "branch": "silkworm/empty"})
        g("merge", "-q", "--ff-only", "silkworm/landed")
        g("checkout", "-q", "silkworm/empty")      # a person parks it elsewhere
        merged = ask(task, {**ckpt, "branch": "silkworm/landed"})
        empty_after = ask(task, {**ckpt, "branch": "silkworm/empty"})
    old_marker = ask({"scope": {}}, {"branch": "silkworm/x"})
    nowhere = ask({"scope": {"cwd": "/nowhere/at/all"}},
                  {**ckpt, "branch": "silkworm/landed"})
    check("a landing killed after its fast-forward says the base moved",
          "was fast-forwarded onto main" in merged
          and "nothing was merged" not in merged, repr(merged))
    check("even with the checkout since parked on another branch",
          "was fast-forwarded" in merged, repr(merged))
    check("an empty branch at an unmoved base is not read as merged",
          unmoved.endswith("nothing was merged"), repr(unmoved))
    check("nor as merged when the base moved for some other reason",
          "may already have moved" in empty_after, repr(empty_after))
    check("a marker from a bot that never checkpointed claims neither",
          "may already have moved" in old_marker, repr(old_marker))
    check("and a checkout that cannot be asked claims neither",
          "may already have moved" in nowhere, repr(nowhere))
    sched = next(n for n in ast.parse((BASE / "bot.py").read_text()).body
                 if isinstance(n, ast.FunctionDef) and n.name == "_task_scheduler")
    check("and the sweep runs at startup, beside the one for interrupted turns",
          any(isinstance(c.func, ast.Name) and c.func.id == "clear_interrupted_landings"
              for c in ast.walk(sched) if isinstance(c, ast.Call)),
          "a sweep nothing calls leaves the marker exactly where it was")
    check("a landing that already finished is left alone",
          (((store.get(settled["id"]).get("result")) or {}).get("landing")
           or {}).get("stage") == "done")

    # --- and two approvals of one task cannot land beside each other ----------
    # Both would be handed the same land/<id> checkout by worktrees.attach, and
    # one releasing it while the other rebases inside it destroys the work.
    import threading as _th
    holding = _th.Event()
    release = _th.Event()
    threads_run = []
    start_ns = {"task_store": store, "tasks": T, "branches": B, "log": LOG,
                "threading": _th, "time": time,
                "landing_enabled": lambda task: True,
                "land_and_record": lambda tid, c, t, **kw: (threads_run.append(tid),
                                                      holding.set(),
                                                      release.wait(5))}
    lift({"start_landing", "record_landing", "LANDING_UNDERWAY",
              "_landing_now", "_landing_guard"}, ns=start_ns, absent=('rework_finished',))
    start_landing = start_ns.get("start_landing")
    slow = store.create("do the thing", project="p")
    first = start_landing(slow["id"]) if start_landing else False
    holding.wait(5)
    second = start_landing(slow["id"]) if start_landing else False
    release.set()
    check("only one landing per task may be in flight",
          first is True and second is False and threads_run == [slow["id"]],
          f"started {threads_run}")
    check("and the record says one is under way while it is",
          (((store.get(slow["id"]).get("result")) or {}).get("landing")
           or {}).get("stage") == "in-progress")

    # --- failing to even begin a landing is not a refused approval ------------
    # The transition already happened. Raising here makes the dashboard toast
    # "Not allowed" for work that is done, and the re-click finds a state that
    # no longer qualifies -- so no landing is ever attempted. The old silence,
    # reached through the new code.
    boom = {"task_store": store, "tasks": T, "log": LOG,
            "holding": __import__("holding"),
            "start_landing": lambda tid: (_ for _ in ()).throw(OSError("disk full"))}
    lift({"approve_task"}, ns=boom, absent=('tell_thread',))
    approve_boom = boom.get("approve_task")
    unlucky = parked(T.AWAITING_APPROVAL, "a review flagged this")
    try:
        answer = approve_boom({"id": unlucky}) if approve_boom else {}
    except Exception as e:
        # Fail as a named check rather than taking the rest of the case with it.
        answer = {"raised": repr(e)}
    check("a landing that cannot start is not reported as a refused approval",
          answer.get("ok") is True and store.get(unlucky)["state"] == T.DONE,
          f"got {answer!r}")

    # --- and the board actually renders each of those outcomes ---------------
    # Reading the marker into the page is not the same as it saying anything.
    # An earlier version returned "" for every refusal that never reached git,
    # so approving unverified work showed "landing…" and then nothing at all.
    import re as _re
    sys.argv = ["x"]
    import visualizer as _V
    js = _re.search(r"<script>(.*?)</script>", _V.PAGE, _re.S).group(1)
    fn = js[js.index("function landing(t)"):js.index("function taskButtons(")]
    check("only a task with no landing record renders nothing",
          "if (!l) return \"\";" in fn,
          "an outcome written and then not shown is worse than not writing it")
    check("and a refusal that never reached git still says so",
          "if (!l.eligible)" in fn and fn.count("not landed") >= 2)

    esc = _re.search(r"const esc = .*", js).group(0)
    harness = (esc + "\n" + fn + "\n"
               + "const out = JSON.parse(process.env.CASES).map(t => landing(t));\n"
               + "process.stdout.write(JSON.stringify(out));\n")
    cases = [{}, {"result": {"landing": {"eligible": True, "landed": True,
                                         "head": "abc1234def"}}},
             {"result": {"landing": {"eligible": True, "landed": False,
                                     "stage": "in-progress"}}},
             {"result": {"landing": {"eligible": True, "landed": False,
                                     "stage": "rebase", "branch": "silkworm/tsk_1",
                                     "detail": "conflicts"}}},
             {"result": {"landing": {"eligible": False, "landed": False,
                                     "stage": "unverified",
                                     "detail": "the change was never verified"}}}]
    try:
        run = subprocess.run(["node", "-e", harness], capture_output=True,
                             text=True, timeout=30,
                             env={**os.environ, "CASES": json.dumps(cases)})
        rendered = json.loads(run.stdout) if run.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        rendered = None                     # no node here; the checks above hold
    if rendered is None:
        print("    (node unavailable — landing() checked by contract only)")
    else:
        check("a task with no landing record renders nothing", rendered[0] == "")
        check("a landing that succeeded shows the commit",
              "abc1234" in rendered[1] and "not landed" not in rendered[1])
        check("one under way says so", "landing" in rendered[2].lower()
              and "not landed" not in rendered[2])
        check("a refusal git made names the branch still waiting",
              "silkworm/tsk_1" in rendered[3] and "waiting for you" in rendered[3]
              and "conflicts" in rendered[3])
        check("and one that never reached git still gives its reason",
              "never verified" in rendered[4] and "not landed" in rendered[4],
              "this rendered as nothing, so approving unverified work went quiet")


# --- a passing review must not swallow what it found --------------------------
# Every ok=true verdict on the board carried findings, and three of them were
# real defects. Routing on `ok` alone wrote them to a completed task, which is
# the one place nobody looks.

def test_review_followups():
    import roles
    import scoping
    from tasks import TaskStore
    import tasks as T
    print("\nreview followups")

    def verdict(**kw):
        return roles.parse_verdict("```json\n" + json.dumps(kw) + "\n```")

    v = verdict(ok=True, summary="fine", findings=["commits_ahead fails toward deletion"])
    check("a passing verdict's findings are not discarded",
          v["followups"] == ["commits_ahead fails toward deletion"],
          "this is the bug: ok=true plus findings used to go nowhere")
    check("and they do not become blocking", v["ok"] and v["findings"] == [])

    v = verdict(ok=False, summary="no", findings=["broken"], followups=["later"])
    check("a flagged verdict keeps its findings where they block",
          not v["ok"] and v["findings"] == ["broken"] and v["followups"] == ["later"])

    v = verdict(ok=True, summary="fine", findings=["a"], followups=["b"])
    check("promoted findings join the declared followups",
          v["followups"] == ["b", "a"])

    v = verdict(ok=True, summary="fine", unverified=["could not run the suite"])
    check("what the review could not check is not filed as work",
          v["unverified"] == ["could not run the suite"] and not v["followups"],
          "otherwise every completed task files a proposal and the board never empties")

    v = verdict(ok=True, summary="fine", findings="a single string")
    check("a string where a list belongs still survives",
          v["followups"] == ["a single string"])
    check("blank entries are dropped",
          verdict(ok=True, summary="s", followups=["", "  ", "real"])["followups"] == ["real"])
    check("a flood is bounded",
          len(verdict(ok=True, summary="s", followups=[f"f{i}" for i in range(50)])
              ["followups"]) == roles.MAX_ITEMS)
    for key in ("findings", "followups", "unverified"):
        check(f"an unreadable verdict still has {key}",
              roles.parse_verdict("nothing here")[key] == [])

    check("the reviewer is told where each list goes",
          all(k in roles.REVIEWER_SYSTEM for k in
              ("findings:", "followups:", "unverified:")))
    check("and told that passing with findings will not bury them",
          "discarded" in roles.REVIEWER_SYSTEM)

    # --- filing ---------------------------------------------------------------
    store = TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    ns = {"task_store": store, "scoping": scoping, "tasks": T, "log": logging.getLogger("t"),
          "dedup": __import__("dedup"), "branches": __import__("branches"),
          "project_store": types.SimpleNamespace(scope_for=lambda p: None,
                                                 get=lambda slug: None)}  # resolve_review asks about ci_gate
    file_followups = lift("file_followups", ns=ns)["file_followups"]

    parent = store.create("do the thing", title="Do the thing", project="silkworm",
                          scope={"cwd": "/repo", "worktree": "/tmp/wt-gone"})
    ids = file_followups(parent, ["rev-list failure reads as zero commits ahead"])
    check("a followup becomes a real task", len(ids) == 1)
    child = store.get(ids[0])
    check("it waits for a decision rather than running", child["state"] == T.PROPOSED,
          "queued would run unreviewed work; done would hide it again")
    check("which is a state the board surfaces", T.PROPOSED in T.NEEDS_ATTENTION)
    check("it says where it came from",
          child["source"] == "review" and child["source_ref"] == parent["id"])
    check("it inherits the project so it is not unfiled",
          child["project"] == "silkworm")
    check("it does not inherit the released worktree",
          "worktree" not in (child["scope"] or {}),
          "that path is gone the moment the implementor's turn ends")
    check("the finding itself is in the goal",
          "rev-list failure reads as zero" in child["goal"])
    check("and the rerun is told to confirm it is still true",
          "still true" in child["goal"])
    check("the title is readable in a list, not the whole finding",
          child["title"].startswith("From review: ") and len(child["title"]) <= 70)

    # Twenty different findings, not one finding twenty times: the same words
    # filed twice are now refused as a duplicate, which is not the cap.
    many = file_followups(parent, list(_DISTINCT_WORDS[:20]))
    check("filing is capped like any other unattended pass",
          len(many) == scoping.limit_for(propose=True))
    check("a followup that cannot be made into a valid goal is skipped, not filed",
          file_followups(parent, ["x" * (scoping.MAX_GOAL_CHARS + 1)]) == [],
          "better a lost note in the log than an unrunnable task on the board")

    # A review is the other unattended producer of proposals. Holding only the
    # nightly pass to the standing limit would leave a board too deep for the
    # ideator to touch quietly filling through this door instead -- while the
    # panel reported it paused, and told the user triage would restart it.
    # Under its own project: filling `silkworm` to the limit here would change
    # what the review gate below is filing against.
    deep = store.create("do the other thing", title="Other", project="backlogged",
                        scope={"cwd": "/repo"})
    while scoping.open_proposals(store.by_project("backlogged")) < scoping.max_open_proposals():
        store.create("Padding the board out to its standing limit",
                     project="backlogged", state=T.PROPOSED, role="implementor")
    at_limit = len(store.all())
    held: list = []
    check("a review files nothing onto a board already at its standing limit",
          file_followups(deep, ["something genuinely worth a decision"], None, held) == []
          and len(store.all()) == at_limit,
          "the nightly pass stops here; the other producer of proposals must too")
    check("and hands the refused finding back as held, not only to the log",
          held == ["something genuinely worth a decision"], str(held))
    check("and a project with room is unaffected by another's backlog",
          len(file_followups(parent, ["a finding on a project with room"])) == 1,
          "the limit is per project, like the nightly pass it mirrors")
    # One place left, so the batch has to stop partway rather than being
    # refused outright -- the case that tells a live count from one taken once.
    spare = next(t for t in store.by_project("backlogged")
                 if t["state"] == T.PROPOSED and t["goal"].startswith("Padding"))
    store.transition(spare["id"], T.CANCELLED, "dismissed")
    held, fates = [], {}
    batch = list(_DISTINCT_WORDS[20:25])
    got = file_followups(deep, batch, None, held, fates)
    check("and with one place left it files one, not the whole batch",
          len(got) == 1,
          "a count taken once for the batch would file all five past the limit")
    check("which leaves the board exactly full, never over",
          scoping.open_proposals(store.by_project("backlogged"))
          == scoping.max_open_proposals())
    check("the four the cap refused are handed back as held, in order",
          held == batch[1:], str(held))
    check("and each finding's fate is told apart",
          fates == {batch[0]: "filed", **{f: "held" for f in batch[1:]}}, str(fates))
    held = []
    file_followups(parent, ["x" * (scoping.MAX_GOAL_CHARS + 1)], None, held)
    check("a finding refused for another reason is not called held",
          held == [], "only the cap holds; an unfileable goal is a different thing")

    # The gate, with the board full: nothing it says may claim a filing.
    from unittest.mock import MagicMock
    impl = store.create("the backlogged implementor's job", title="Impl",
                        project="backlogged", role="implementor")
    rev = store.create("review it", role="reviewer", parent=impl["id"])
    app = MagicMock()
    resolve = _bot_func("resolve_review", task_store=store, tasks=T,
                        roles=__import__("roles"), app=app, time=__import__("time"),
                        file_followups=file_followups,
                        rework_flagged_review=lambda *a: False,
                        rework_conflict=lambda *a: False,
                        log=logging.getLogger("t"))
    capped = ["the cap should hold this finding back for later",
              "and this second one along with it, also for later"]
    resolve(dict(rev), "reviewer",
            '```json\n' + json.dumps({"ok": True, "summary": "fine",
                                       "followups": capped}) + '\n```', "C1", "1.0")
    review = (store.get(impl["id"]).get("result") or {}).get("review") or {}
    check("the gate records what the cap held on the verdict",
          review.get("held") == capped and review.get("filed") == []
          and review.get("held_at"), str(review))
    posted = " ".join(str(c.kwargs.get("text", ""))
                      for c in app.client.chat_postMessage.call_args_list)
    check("the thread says they were held by the cap, not filed or merely noted",
          "held back by the proposal cap" in posted
          and "Filed for you" not in posted and "Also noted" not in posted
          and all(f in posted for f in capped), posted[:400])

    # Partly filed: the "filed" heading must list only what was filed.
    store.transition(next(t for t in store.by_project("backlogged")
                          if t["state"] == T.PROPOSED
                          and t["goal"].startswith("Padding"))["id"],
                     T.CANCELLED, "dismissed")
    impl2 = store.create("another backlogged job", title="Impl2",
                         project="backlogged", role="implementor")
    rev2 = store.create("review it", role="reviewer", parent=impl2["id"])
    app.reset_mock()
    split = ["the filed one: a bounded retry for the landing push step",
             "the held one: rename the sweeper's keep set for clarity"]
    resolve(dict(rev2), "reviewer",
            '```json\n' + json.dumps({"ok": True, "summary": "fine",
                                       "followups": split}) + '\n```', "C1", "1.1")
    posted = " ".join(str(c.kwargs.get("text", ""))
                      for c in app.client.chat_postMessage.call_args_list)
    filed_part, _, held_part = posted.partition("held back by the proposal cap")
    review2 = (store.get(impl2["id"]).get("result") or {}).get("review") or {}
    check("partly filed: the filed heading lists only the filed finding",
          "Filed for you" in filed_part and split[0] in filed_part
          and split[1] not in filed_part and split[1] in held_part
          and review2.get("held") == [split[1]] and len(review2.get("filed") or []) == 1,
          posted[:500])

    # Filing raises after one finding is filed: its id is lost, but the
    # finding must still be said somewhere rather than under no heading.
    def files_then_raises(task, followups, duplicates=None, held=None, fates=None):
        fates[followups[0]] = "filed"
        raise RuntimeError("dedup blew up")
    boom = _bot_func("resolve_review", task_store=store, tasks=T,
                     roles=__import__("roles"), app=app, time=__import__("time"),
                     file_followups=files_then_raises,
                     rework_flagged_review=lambda *a: False,
                     rework_conflict=lambda *a: False,
                     log=logging.getLogger("t"))
    impl3 = store.create("a third backlogged job", title="Impl3",
                         project="backlogged", role="implementor")
    rev3 = store.create("review it", role="reviewer", parent=impl3["id"])
    app.reset_mock()
    lost = ["a finding filed just before the filing step raised"]
    boom(dict(rev3), "reviewer",
         '```json\n' + json.dumps({"ok": True, "summary": "fine",
                                    "followups": lost}) + '\n```', "C1", "1.2")
    posted = " ".join(str(c.kwargs.get("text", ""))
                      for c in app.client.chat_postMessage.call_args_list)
    check("a filing that raises midway still says every finding",
          "Also noted" in posted and lost[0] in posted, posted[:400])

    # --- routing --------------------------------------------------------------
    bot = (BASE / "bot.py").read_text()
    gate = bot[bot.index("def resolve_review("):bot.index("def land_if_ready(")]
    check("followups are filed from the review gate", "file_followups(parent" in gate)
    check("only once the verdict passes", 'verdict["ok"] and verdict["followups"]' in gate,
          "a flagged task already puts the whole verdict in front of the user")
    check("filing cannot cost the verdict", "log.exception(\"filing review followups"
          in gate)
    check("what was filed is recorded on the task", '"filed": filed' in gate)
    check("and said in the thread", "accept or dismiss" in gate)

    js = _re.search(r"<script>(.*?)</script>", (BASE / "visualizer.py").read_text(),
                    _re.S).group(1)
    rv = js[js.index("function review(t)"):js.index("function taskButtons(")]
    for key in ("findings", "followups", "unverified", "filed"):
        check(f"the dashboard shows the verdict's {key}", f"rv.{key}" in rv)

    # --- the gate itself, driven rather than read -----------------------------
    # The reported failure was end to end: a verdict came back ok with findings
    # and the task went to done with nothing anywhere. Run that exact input.
    posted = []
    gns = dict(ns)
    gns.update({
        "roles": roles, "task_store": store,
        "app": types.SimpleNamespace(client=types.SimpleNamespace(
            chat_postMessage=lambda **kw: posted.append(kw["text"]))),
        # Landing has its own cases in test_landing_is_visible; here it must
        # simply not stand between the verdict and where it routes.
        "merge": __import__("merge"),
        "land_and_record": lambda tid, c, th: {"eligible": True, "landed": True,
                                               "stage": "done", "head": "abc1234"},
        "task_state": lambda tid, st, why="": store.transition(tid, st, why),
        # Automatic send-backs have their own test; here nothing is sent back.
        "rework_flagged_review": lambda *a: False,
        "rework_conflict": lambda *a: False,
    })
    resolve = lift("resolve_review", ns=gns,
                   absent=('CLAUDE_CWD', 'tell_thread'))["resolve_review"]

    # Its own project: the checks above have filled silkworm's board to the
    # standing limit, and a full board refusing this finding is correct there.
    work = store.create("do it", title="Do it", project="gate-e2e", role="implementor",
                        scope={"cwd": "/repo"}, blocked_on=["rev1"])
    store.transition(work["id"], T.BLOCKED, "awaiting review")
    reviewer = store.create("review it", role="reviewer", parent=work["id"])
    resolve(store.get(reviewer["id"]), "reviewer",
            '```json\n{"ok": true, "summary": "fine", '
            '"findings": ["the guard fails toward deletion when rev-list errors"], '
            '"unverified": ["could not run the suite"]}\n```', "C1", "1.0")

    done = store.get(work["id"])
    check("a pass still completes the task silently", done["state"] == T.DONE)
    review_rec = (done.get("result") or {}).get("review") or {}
    check("its finding was filed, not written to a finished task",
          len(review_rec.get("filed") or []) == 1,
          "this is the end-to-end case that lost three real defects")
    filed_task = store.get(((review_rec.get("filed") or [""]) + [""])[0]) or {}
    check("and the filed task carries the finding",
          "fails toward deletion" in filed_task.get("goal", ""))
    check("and waits in a state the board surfaces",
          filed_task.get("state") == T.PROPOSED)
    check("what the review could not run was not filed as work anywhere",
          all("could not run the suite" not in t.get("goal", "")
              for t in gns["task_store"]._data.values()),
          "otherwise every completed task files a proposal and the board never empties")
    check("the thread was told", any("accept or dismiss" in m for m in posted))

    before = len(gns["task_store"]._data)
    flagged = store.create("do it too", title="Do it too", role="implementor",
                           scope={"cwd": "/repo"}, blocked_on=["rev2"])
    store.transition(flagged["id"], T.BLOCKED, "awaiting review")
    rev2 = store.create("review it", role="reviewer", parent=flagged["id"])
    resolve(store.get(rev2["id"]), "reviewer",
            '```json\n{"ok": false, "summary": "no", "findings": ["it is wrong"], '
            '"followups": ["and this too"]}\n```', "C1", "1.0")
    check("a flagged task still asks the user",
          store.get(flagged["id"])["state"] == T.AWAITING_APPROVAL)
    check("and nothing is filed behind its back",
          len(gns["task_store"]._data) == before + 2,
          "the whole verdict is already in front of them")


# --- a task with nothing to land is not a refused landing ----------------------
# Seven Silkworm tasks in a day read "not landed (attach)" -- a refusal, asking
# for a person -- when they had simply found their job already done. Their
# branches held nothing new, so the executor rightly deleted them as empty;
# the landing then went looking for a branch that was gone and reported the
# absence as git saying no.

def test_nothing_to_land_is_not_a_refusal():
    import logging
    import merge as M
    import worktrees as W
    import branches as B
    print("\na task with nothing to land is not a refused landing")

    repo = Path(tempfile.mkdtemp())
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
    g("init", "-q", "-b", "main")
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
    g("branch", "silkworm/tsk_empty")                  # on the base, nothing new
    g("checkout", "-q", "-b", "silkworm/tsk_work")
    (repo / "f.txt").write_text("work\n")
    g("add", "f.txt")
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "real work")
    g("checkout", "-q", "main")

    class Projects:
        def scope_for(self, slug):
            return {}
        def get(self, slug):
            return {"auto_merge": True, "test_cmd": "true"}
    attached = []
    class Worktrees:
        BRANCH_PREFIX = W.BRANCH_PREFIX
        def __getattr__(self, name):
            return getattr(W, name)
        def attach(self, cwd, tid, branch):
            attached.append(branch)
            return None                                # stop before git merges anything
    ns = {"project_store": Projects(), "worktrees": Worktrees(), "merge": M,
          "branches": B, "log": logging.getLogger("test")}
    lift({"land_if_ready", "landing_enabled"}, ns=ns, absent=LANDING_ROUTES)
    land = ns["land_if_ready"]
    base = {"project": "p", "role": "implementor", "verified": True,
            "scope": {"cwd": str(repo)}}

    for tid, why in (("tsk_gone", "its branch was tidied away as empty"),
                     ("tsk_empty", "everything on its branch is already on the base")):
        r = land({**base, "id": tid})
        check(f"{why}: nothing to land", r.get("stage") == "nothing-to-land"
              and not r.get("eligible"), f"got {r!r}")
        check(f"{why}: not a refusal anyone must chase", not M.needs_a_person(r))
    check("and neither went looking for a checkout",
          not any(b.endswith(("tsk_gone", "tsk_empty")) for b in attached), str(attached))
    r = land({**base, "id": "tsk_work"})
    check("a branch with real work still goes on to land",
          attached == ["silkworm/tsk_work"] and r.get("stage") == "attach", f"{attached} {r!r}")


# --- work left uncommitted goes back before it reaches review ------------------
# A Cadence implementor finished with three changes loose in its checkout. The
# suite would have tested them, but the reviewer checks the branch out -- and
# git refused, because the checkout still held it -- so "nothing was reviewed";
# it was approved, and closed with the work still on no branch at all.

def test_uncommitted_work_is_sent_back():
    import logging
    import types
    import worktrees as W
    import tasks as T
    print("\nwork left uncommitted goes back before it reaches review")

    repo = Path(tempfile.mkdtemp())
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
    g("init", "-q", "-b", "main")
    (repo / ".gitignore").write_text("build/\n")
    (repo / "a.txt").write_text("a\n")
    g("add", "-A"); g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")
    check("a clean checkout has nothing loose", W.uncommitted(repo) == [])
    (repo / "a.txt").write_text("changed\n")
    (repo / "new.txt").write_text("new\n")
    (repo / "build").mkdir(); (repo / "build" / "out.o").write_text("x")
    loose = sorted(W.uncommitted(repo))
    check("modified and new files are loose; ignored build output is not",
          loose == ["a.txt", "new.txt"], str(loose))

    store = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    posted = []
    client = types.SimpleNamespace(chat_postMessage=lambda **kw: posted.append(kw["text"]))
    ns = {"task_store": store, "tasks": T, "app": types.SimpleNamespace(client=client),
          "log": logging.getLogger("test"),
          "task_state": lambda tid, st, detail="": store.transition(tid, st, detail)}
    lift({"send_back_uncommitted", "MAX_COMMIT_ATTEMPTS"}, ns=ns)
    tid = store.create("implement it", state=T.QUEUED, role="implementor")["id"]
    store.transition(tid, T.RUNNING)
    check("first time: sent back", ns["send_back_uncommitted"](store.get(tid) | {"id": tid},
                                                               loose, "C", "1.1"))
    t = store.get(tid)
    check("requeued to try again", t["state"] == T.QUEUED)
    check("told which files, and why it matters",
          "new.txt" in t["goal"] and "uncommitted" in t["goal"] and "commit" in t["goal"].lower())
    store.transition(tid, T.RUNNING)
    ns["send_back_uncommitted"](store.get(tid) | {"id": tid}, loose, "C", "1.1")
    check("second time: stops and asks, rather than loop",
          store.get(tid)["state"] == T.AWAITING_APPROVAL and "Still uncommitted" in posted[-1])

    # Wired where it has to be: after the turn, before the suite and before
    # review -- found by the parse tree, not by searching the text.
    tree = ast.parse((BASE / "bot.py").read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "verify_work"
                      for c in ast.walk(n)) and n.name != "verify_work")
    def first(name):
        return min((c.lineno for c in ast.walk(fn) if isinstance(c, ast.Call)
                    and (getattr(c.func, "id", "") == name
                         or getattr(c.func, "attr", "") == name)), default=10**9)
    check("loose work is looked for before the suite runs",
          first("uncommitted") < first("verify_work"), fn.name)
    check("and sent back before review is asked for",
          first("send_back_uncommitted") < first("resolve_review"))
    guard = next((n for n in ast.walk(fn) if isinstance(n, ast.If)
                  and any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "verify_work"
                          for c in ast.walk(n))), None)
    check("the suite does not run over loose work",
          guard is not None and "loose" in ast.unparse(guard.test))


# --- a person can land work on any project -------------------------------------
# Auto-merge was the only door to the base. A project with it off could not
# merge anything at all: Approve closed the task and left the branch, and 59
# finished tasks piled up on branches with no button that would do anything.

def test_approval_lands_on_any_project():
    import logging
    import types
    import merge as M
    import worktrees as W
    import branches as B
    import discard as D
    import tasks as T
    print("\na person can land work on any project")

    repo = Path(tempfile.mkdtemp())
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
    g("init", "-q", "-b", "main")
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
    g("checkout", "-q", "-b", "silkworm/tsk_work")
    (repo / "f.txt").write_text("work\n"); g("add", "f.txt")
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "real work")
    g("checkout", "-q", "main")

    class Projects:
        def scope_for(self, slug):
            return {}
        def get(self, slug):
            return {"auto_merge": False, "test_cmd": "true"}     # auto-merge OFF
    attached = []
    class Worktrees:
        BRANCH_PREFIX = W.BRANCH_PREFIX
        def __getattr__(self, name):
            return getattr(W, name)
        def attach(self, cwd, tid, branch):
            attached.append(branch); return None
    ns = {"project_store": Projects(), "worktrees": Worktrees(), "merge": M,
          "branches": B, "log": logging.getLogger("test")}
    lift({"land_if_ready", "landing_enabled"}, ns=ns, absent=LANDING_ROUTES)
    land = ns["land_if_ready"]
    t = {"id": "tsk_work", "project": "p", "role": "implementor",
         "scope": {"cwd": str(repo)}}

    r = land({**t, "verified": True})
    check("unasked, auto-merge off still means no", r.get("stage") == "not-enabled")
    r = land({**t, "verified": True}, approved=True)
    check("approved, it goes on to land", attached == ["silkworm/tsk_work"], f"{r!r}")
    attached.clear()
    r = land({**t, "verified": False}, approved=True)
    check("work that failed its tests is never landed, even approved",
          r.get("stage") == "unverified" and not attached)
    r = land({**t, "verified": None}, approved=True)
    check("work never tested (no suite then) may be landed on request -- landing runs the suite",
          attached == ["silkworm/tsk_work"])

    # Land and Drop, on finished work only.
    store = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    begun, recorded = [], []
    import contextlib
    ns2 = {"task_store": store, "tasks": T, "branches": B, "discard": D,
           "log": logging.getLogger("test"),
           "start_landing": lambda tid, approved=False: begun.append((tid, approved)) or True,
           "record_landing": lambda task, out: recorded.append(out),
           "repo_guard": lambda cwd: contextlib.nullcontext()}
    lift({"land_or_drop"}, ns=ns2)
    lod = ns2.get("land_or_drop")
    check("there is a Land/Drop action", bool(lod))
    run = store.create("still going", state=T.QUEUED)["id"]
    store.transition(run, T.RUNNING)
    check("not on work still running", lod("land", {"id": run}).get("ok") is False and not begun)
    done = store.create("finished work", state=T.QUEUED, scope={"cwd": str(repo)},
                        role="implementor", branch="silkworm/tsk_work")["id"]
    store.transition(done, T.RUNNING); store.transition(done, T.DONE)
    r = lod("land", {"id": done})
    check("Land starts an approved landing", r.get("ok") and begun == [(done, True)], f"{r} {begun}")
    tip = g("rev-parse", "silkworm/tsk_work").stdout.strip()
    r = lod("drop", {"id": done})
    gone = g("rev-parse", "--verify", "-q", "refs/heads/silkworm/tsk_work").returncode != 0
    check("Drop deletes the branch", r.get("ok") and gone, f"{r}")
    tags = g("tag", "-l", "discarded/*").stdout.split()
    check("keeping what was on it under a tag",
          len(tags) == 1 and g("rev-parse", tags[0] + "^{commit}").stdout.strip() == tip, str(tags))
    check("and the record says it was dropped", recorded and recorded[-1]["stage"] == "dropped")

    js = (BASE / "visualizer.py").read_text()
    fn = js[js.index("async function renderUnmerged()"):]
    fn = fn[:fn.index("\n}\n")]
    check("the dashboard offers Land and Drop on each stranded branch",
          "'land'" in fn and "'drop'" in fn)


# --- unsupervised work needs a project that can prove and merge it -------------
# Ideation and queued implementor tasks run with nobody in the loop. On a
# project with no test command and no auto-merge, each one ended as a branch
# nothing tested and nothing merged -- sixty of them, across four projects.

def test_unsupervised_work_needs_a_ready_project():
    import logging
    import projects as P
    import tasks as T
    print("\nunsupervised work needs a project that can prove and merge it")
    check("ready: a test command and auto-merge",
          P.unready({"test_cmd": "make test", "auto_merge": True}) == "")
    check("no test command: not ready", "test command" in P.unready({"auto_merge": True}))
    check("no auto-merge: not ready", "auto-merge" in P.unready({"test_cmd": "x"}))
    check("no project at all: not ready", P.unready(None) != "")

    root = Path(tempfile.mkdtemp())
    ts, ps = T.TaskStore(root / "t.json"), P.ProjectStore(root / "p.json")
    ps.ensure("Cadence", test_cmd="make -C ios test", auto_merge=True)
    ps.ensure("Saga")
    told = []
    ns = {"projects": P, "project_store": ps, "tasks": T, "task_store": ts,
          "task_state": lambda tid, st, detail="": ts.transition(tid, st, detail),
          "tell_thread": lambda key, text: told.append(text)}
    lift({"hold_unsupervised"}, ns=ns)
    hold = ns["hold_unsupervised"]
    def claimed(project, role="implementor"):
        tid = ts.create("do it", project=project, role=role, state=T.QUEUED, driver="queue")["id"]
        ts.transition(tid, T.RUNNING)
        return dict(ts.get(tid), id=tid)
    t = claimed("saga")
    check("an implementor task for an unready project is not run", hold(t) is True)
    check("it waits on the board with the reason",
          ts.get(t["id"])["state"] == T.NEEDS_INPUT and "test command" in ts.get(t["id"])["events"][-1]["detail"])
    check("and its thread is told", told and "Not run" in told[-1])
    check("a task with no project is held too", hold(claimed("")) is True)
    check("a ready project's task runs", hold(claimed("cadence")) is False)
    check("investigation (assistant) is not held", hold(claimed("saga", "assistant")) is False)
    check("nor is a read-only review", hold(claimed("saga", "reviewer")) is False)

    # The gate sits where the queue starts work, so no door can route around it.
    tree = ast.parse((BASE / "bot.py").read_text())
    worker = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_task_worker")
    lines = {c.func.id: c.lineno for c in ast.walk(worker)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    check("the runner checks readiness before it runs anything",
          "hold_unsupervised" in lines and lines["hold_unsupervised"] < lines.get("execute_task", 0))

    # Ideation: refused when switched on, skipped when already on, and not
    # filed even if asked for directly.
    hp = {"projects": P, "project_store": ps, "log": logging.getLogger("test")}
    lift({"handle_projects"}, ns=hp, absent=PROJECT_ROUTES)
    r = hp["handle_projects"]({"action": "ideate", "slug": "saga", "at": "02:00"})
    check("ideation cannot be switched on for an unready project",
          r.get("ok") is False and "test command" in r.get("error", ""), str(r))
    r = hp["handle_projects"]({"action": "ideate", "slug": "cadence", "at": "02:00"})
    check("but can for a ready one", r.get("ok") is True, str(r))
    r = hp["handle_projects"]({"action": "ideate", "slug": "saga", "at": "off"})
    check("and can always be switched off", r.get("ok") is True, str(r))
    run_ideation, its, ips = _run_ideation_impl()
    ips.ensure("Saga", ideate_at="02:00")
    out = run_ideation("saga")
    check("a pass for an unready project files nothing", out.get("ok") is False and not its.all(), str(out))


# --- a review reads the commit, it does not take the branch --------------------
# Two Cadence reviews failed five times each: the implementor's checkout was
# kept (a generated file had changed in it) and so still held the branch, and
# git lets one checkout hold a branch. The review only reads; it never needed
# the branch, only the commit at its tip.

def test_review_does_not_take_the_branch():
    import worktrees as W
    print("\na review reads the commit, it does not take the branch")
    repo = Path(tempfile.mkdtemp()) / "r"
    g = lambda *a, cwd=repo: subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t",
                                             "-c", "user.name=t", *a], capture_output=True, text=True)
    repo.mkdir(); g("init", "-q", "-b", "main"); g("commit", "-q", "--allow-empty", "-m", "base")
    held = repo.parent / "held"
    g("worktree", "add", "-q", "-b", "silkworm/tsk_x", str(held))
    (held / "f.txt").write_text("work\n"); g("add", "f.txt", cwd=held)
    g("commit", "-q", "-m", "the work", cwd=held)
    (held / "scratch").write_text("left behind\n")           # kept: it is dirty
    old_root = W.ROOT
    W.ROOT = repo.parent / "wts"
    try:
        check("taking the branch is refused while another checkout holds it",
              W.attach(repo, "tsk_x", "silkworm/tsk_x", label="land") is None)
        here = W.attach(repo, "tsk_x", "silkworm/tsk_x", label="review", detach=True)
        tip = g("rev-parse", "silkworm/tsk_x").stdout.strip()
        check("a review gets the branch's commit regardless",
              here is not None and g("rev-parse", "HEAD", cwd=here).stdout.strip() == tip)
        check("and sees the committed work", here is not None and (here / "f.txt").exists())
        check("but not what was left uncommitted", here is not None and not (here / "scratch").exists())
        again = W.attach(repo, "tsk_x", "silkworm/tsk_x", label="review", detach=True)
        check("asked again, the same checkout is reused", again == here)
        g("commit", "-q", "--allow-empty", "-m", "more", cwd=held)
        moved = W.attach(repo, "tsk_x", "silkworm/tsk_x", label="review", detach=True)
        check("a checkout at an older commit is not reused as if current", moved is None
              or g("rev-parse", "HEAD", cwd=moved).stdout.strip()
              == g("rev-parse", "silkworm/tsk_x").stdout.strip())
    finally:
        W.ROOT = old_root
    calls = [c for c in ast.walk(ast.parse((BASE / "bot.py").read_text()))
             if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "attach"
             and any(k.arg == "label" and getattr(k.value, "value", "") == "review" for k in c.keywords)]
    check("the review asks for a detached checkout",
          calls and all(any(k.arg == "detach" and getattr(k.value, "value", None) is True
                            for k in c.keywords) for c in calls), f"{len(calls)} review attach call(s)")


# --- one run of a project's suite at a time -----------------------------------
# 2026-10-05: a Cadence landing's post-merge suite failed "Application failed
# preflight checks" (exit 65) while a Cadence implementor was being verified at
# the same moment. Cadence's suite pins one simulator, two runs fought over it,
# and the landing was refused and rolled back for a reason that had nothing to
# do with the code. Driven with real threads, real subprocesses, the real
# repo_guard and the real land_if_ready / verify_work / merge.land.

_PROBE = r'''
import os, sys, time
log, sleep = sys.argv[1], float(sys.argv[2])
if os.path.exists(".slow"):    # long enough to still be running when others start
    sleep = 3
with open(log, "a") as f:
    f.write(f"start {time.time()}\n")
time.sleep(sleep)
with open(log, "a") as f:
    f.write(f"end {time.time()}\n")
'''

_FLAKY = r'''
import sys
counter, mode = sys.argv[1], sys.argv[2]
try:
    n = int(open(counter).read())
except Exception:
    n = 0
open(counter, "w").write(str(n + 1))
LAUNCH = ('Testing failed:\n\tCadence encountered an error (Failed to install or '
          'launch the test runner. (Underlying Error: Simulator device failed to '
          'launch com.rtsharp.Cadence. The request was denied by service delegate '
          '(SBMainWorkspace) for reason: Busy ("Application failed preflight checks")')
if len(sys.argv) > 3:          # say when this attempt ran, for the lock check
    import time
    with open(sys.argv[3], "a") as f:
        f.write(f"flaky-{n} ")
    time.sleep(0.4)
if mode == "launch-once" and n == 0 or mode == "launch-always":
    print(LAUNCH)
    print("x" * 5000)          # pushes the launch error out of the kept tail
    print("** TEST FAILED **")
    sys.exit(65)
if mode == "test-fail":
    print("com.apple.CoreSimulator.SimDevice: booted")   # noise beside a real failure
    print("XCTAssertEqual failed: (\"1\") is not equal to (\"2\")")
    print("** TEST FAILED **")
    sys.exit(65)
print("** TEST SUCCEEDED **")
'''


def _suite_events(log):
    try:
        lines = Path(log).read_text().split()
    except FileNotFoundError:
        return []
    return [w for w in lines if w in ("start", "end")]


def _never_overlapped(events):
    return events and events == ["start", "end"] * (len(events) // 2)


def test_one_suite_run_per_project_at_a_time():
    import threading
    import verify as V
    print("\none run of a project's suite at a time")

    d = Path(tempfile.mkdtemp())
    probe = d / "probe.py"; probe.write_text(_PROBE)
    py = sys.executable

    def cmd(log, sleep=0.6):
        return f"{py} {probe} {log} {sleep}"

    waits = []
    class Catch(logging.Handler):
        def emit(self, rec):
            waits.append(rec.getMessage())
    vlog = logging.getLogger("silkworm.verify")
    handler = Catch(); vlog.addHandler(handler)
    saved_level = vlog.level; vlog.setLevel(logging.INFO)
    try:
        # Same project: the second run queues behind the first.
        log = d / "same.log"
        ts = [threading.Thread(target=V.run, args=(cmd(log), d),
                               kwargs={"project": "cadence"}, daemon=True)
              for _ in range(2)]
        for t in ts: t.start()
        for t in ts: t.join(20)
        ev = _suite_events(log)
        check("two runs of one project's suite never overlap",
              len(ev) == 4 and _never_overlapped(ev), f"got {ev}")
        check("and the one that waited says so in the log",
              any("waiting on another run" in m and "cadence" in m for m in waits),
              f"got {waits}")

        # Different projects: nothing shared, so nothing queued.
        log2 = d / "diff.log"
        ts = [threading.Thread(target=V.run, args=(cmd(log2), d),
                               kwargs={"project": p}, daemon=True)
              for p in ("cadence", "trader")]
        for t in ts: t.start()
        for t in ts: t.join(20)
        ev = _suite_events(log2)
        check("two different projects' suites still run at once",
              ev == ["start", "start", "end", "end"], f"got {ev}")
    finally:
        vlog.removeHandler(handler); vlog.setLevel(saved_level)

    # --- and a landing, holding repo_guard, cannot deadlock against it ----------
    # Three at once on one project: a verification in a task's own checkout,
    # an unisolated turn that holds the shared checkout's repo_guard and then
    # verifies, and a landing that takes repo_guard and runs the suite twice.
    import merge as M
    import worktrees as W
    import branches as B
    G = _repo_guard_impl()
    root = Path(tempfile.mkdtemp())
    saved_root, W.ROOT = W.ROOT, root / "wts"
    try:
        repo = root / "repo"; repo.mkdir()
        def git(*a):
            return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                                   "-c", "user.name=t", *a],
                                  capture_output=True, text=True)
        git("init", "-q", "-b", "main")
        git("commit", "-q", "--allow-empty", "-m", "base")
        git("checkout", "-q", "-b", "silkworm/tsk_lands")
        (repo / "work.txt").write_text("work\n")
        git("add", "-A"); git("commit", "-q", "-m", "the work")
        git("checkout", "-q", "main")
        elsewhere = root / "own-checkout"; elsewhere.mkdir()
        (elsewhere / ".slow").write_text("")

        log3 = root / "suite.log"
        proj = {"auto_merge": True, "test_cmd": cmd(log3, 0.5)}
        proj["scope"] = {"cwd": str(repo), "branch": "main"}
        ns = {"project_store": types.SimpleNamespace(
                  get=lambda slug: proj, scope_for=lambda slug: dict(proj["scope"])),
              "worktrees": W, "merge": M, "branches": B, "verify": V,
              "log": logging.getLogger("test"), "repo_guard": G.repo_guard,
              "record_landing": lambda *a, **k: None}
        got = lift({"land_if_ready", "landing_enabled", "verify_work",
                        "LANDING_UNDERWAY"}, ns=ns, absent=LANDING_ROUTES)["__lifted__"]
        check("the landing and the verification were lifted out of bot.py",
              {"land_if_ready", "verify_work"} <= got, str(sorted(got)))
        task = {"id": "tsk_lands", "project": "cadence", "role": "implementor",
                "verified": True,
                "scope": {"cwd": str(repo), "branch": "main"}}
        out = {}

        def verifying():
            out["own"] = ns["verify_work"]({"id": "tsk_v", "project": "cadence"},
                                           elsewhere)

        def unisolated_turn():
            with G.repo_guard(str(repo)):
                out["turn"] = ns["verify_work"]({"id": "tsk_u", "project": "cadence"},
                                                repo)

        def landing():
            out["land"] = ns["land_if_ready"](task)

        first = threading.Thread(target=verifying, daemon=True)
        first.start()
        deadline = time.time() + 10
        while not _suite_events(log3) and time.time() < deadline:
            time.sleep(0.02)                     # the suite is now running
        # The landing first, and only once it holds the checkout does the turn
        # start: the landing then waits on the suite's lock while holding
        # repo_guard, and the turn waits on repo_guard -- the shape a cycle
        # would need.
        rest = [threading.Thread(target=f, daemon=True)
                for f in (landing, unisolated_turn)]
        rest[0].start()
        guard = G._repo_lock(str(repo))
        while not guard.locked() and time.time() < deadline:
            time.sleep(0.01)
        rest[1].start()
        for t in [first, *rest]: t.join(30)
        stuck = [t for t in [first, *rest] if t.is_alive()]
        check("a landing inside repo_guard does not deadlock with waiting runs",
              not stuck, f"{len(stuck)} thread(s) still waiting after 30s")
        ev = _suite_events(log3)
        check("and none of the four runs for that project overlapped",
              len(ev) == 8 and _never_overlapped(ev), f"got {ev}")
        check("the landing still landed",
              (out.get("land") or {}).get("landed") is True, f"got {out.get('land')!r}")
    finally:
        W.ROOT = saved_root

    # The landing takes the lock only through verify.run, inside repo_guard.
    # Holding it around repo_guard instead is the inverted order that would
    # deadlock against an unisolated turn above.
    takes = [n.lineno for n in ast.walk(ast.parse((BASE / "bot.py").read_text()))
             if isinstance(n, ast.Call)
             and "project_lock" in (getattr(n.func, "attr", None),
                                    getattr(n.func, "id", None))]
    check("bot.py never takes a project's test lock except through verify.run",
          not takes, f"called at line(s) {takes}")


def test_a_simulator_that_would_not_launch_gets_one_more_go():
    import verify as V
    print("\na landing retries a suite that failed to launch, once")

    d = Path(tempfile.mkdtemp())
    flaky = d / "flaky.py"; flaky.write_text(_FLAKY)
    runs = iter(range(1000))

    def attempt(mode, **kw):
        counter = d / f"n{next(runs)}"
        r = V.run(f"{sys.executable} {flaky} {counter} {mode}", d,
                  project="cadence", **kw)
        return r, int(counter.read_text())

    r, n = attempt("launch-once", retry_launch=True)
    check("a launch failure is run once more, and the second run counts",
          r["ok"] and n == 2, f"ok={r['ok']} runs={n}")
    r, n = attempt("launch-always", retry_launch=True)
    check("but only once: a simulator that never comes up still refuses",
          not r["ok"] and n == 2, f"ok={r['ok']} runs={n}")
    r, n = attempt("test-fail", retry_launch=True)
    check("a real test failure is never retried",
          not r["ok"] and n == 1, f"ok={r['ok']} runs={n}")
    r, n = attempt("launch-once")
    check("and nothing is retried unless the caller asks",
          not r["ok"] and n == 1, f"ok={r['ok']} runs={n}")

    # The retry happens under the same hold as the first attempt: a run
    # queued behind it must not take the simulator in between, which is the
    # very collision the retry is there to ride out.
    import threading
    order = d / "order.log"
    counter = d / "held"
    first = threading.Thread(target=V.run, daemon=True, kwargs={
        "command": f"{sys.executable} {flaky} {counter} launch-once {order}",
        "cwd": d, "project": "cadence", "retry_launch": True})
    first.start()
    deadline = time.time() + 10
    while not order.exists() and time.time() < deadline:
        time.sleep(0.01)
    other = threading.Thread(target=V.run, daemon=True, kwargs={
        "command": f"{sys.executable} -c \"open('{order}', 'a').write('other ')\"",
        "cwd": d, "project": "cadence"})
    other.start(); first.join(20); other.join(20)
    got = order.read_text().split()
    check("a retry keeps the project's lock between its two attempts",
          got == ["flaky-0", "flaky-1", "other"], f"got {got}")

    for said in ("Application failed preflight checks",
                 "Unable to boot the Simulator.",
                 "CoreSimulatorService connection became invalid",
                 "Failed to install or launch the test runner"):
        r = V.run(f"{sys.executable} -c \"print('{said}'); raise SystemExit(65)\"", d)
        check(f"recognised as a launch failure: {said!r}", V.launch_failure(r))
    r = V.run(f"{sys.executable} -c \"print('Unable to boot'); raise SystemExit(0)\"", d)
    check("a passing run is never a launch failure", not V.launch_failure(r))
    r = V.run(f"{sys.executable} -c \"print('CoreSimulator: noise'); "
              f"print(\\\"Test Case '-[T t]' failed (0.1 seconds).\\\"); "
              f"raise SystemExit(65)\"", d)
    check("a run in which a test failed is not a launch failure, whatever else it says",
          r["ran"] and not r["ok"] and not V.launch_failure(r), r["output"][-200:])
    # The same for Swift Testing, whose failures read differently from
    # XCTest's: under xcodebuild, and under `swift test`.
    for said in ("Test case 'Suite/foo()' failed on 'iPhone 17' (0.1 seconds)",
                 "\u2718 Test foo() recorded an issue at T.swift:3:5: Expectation failed",
                 "\u2718 Test foo() failed after 0.002 seconds with 1 issue."):
        out = d / "swift-testing.txt"
        out.write_text(f"CoreSimulator: noise\n{said}\n", encoding="utf-8")
        r = V.run(f"{sys.executable} -c \"import sys; "
                  f"sys.stdout.write(open(sys.argv[1], encoding='utf-8').read()); "
                  f"raise SystemExit(65)\" {out}", d)
        check(f"a Swift Testing failure is not a launch failure: {said!r}",
              r["ran"] and not r["ok"] and not V.launch_failure(r), r["output"][-200:])
    # ...but a run summary alone is not evidence that any test ran.
    check("a bare run summary does not count as a test failing",
          not V.TEST_FAILURES.search("Test run with 0 tests failed after 0.001 seconds"))
    check("nor does xcodebuild's own 'Testing failed:' header",
          not V.TEST_FAILURES.search("Testing failed:\n\tApplication failed preflight checks"))

    # Wired where it matters: the landing retries, verification does not.
    import merge as M
    import worktrees as W
    import branches as B
    root = Path(tempfile.mkdtemp())
    saved_root, W.ROOT = W.ROOT, root / "wts"
    try:
        repo = root / "repo"; repo.mkdir()
        def git(*a):
            return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                                   "-c", "user.name=t", *a],
                                  capture_output=True, text=True)
        git("init", "-q", "-b", "main")
        git("commit", "-q", "--allow-empty", "-m", "base")
        git("checkout", "-q", "-b", "silkworm/tsk_flaky")
        (repo / "work.txt").write_text("work\n")
        git("add", "-A"); git("commit", "-q", "-m", "the work")
        git("checkout", "-q", "main")
        counter = root / "count"
        proj = {"auto_merge": True,
                "test_cmd": f"{sys.executable} {flaky} {counter} launch-once"}
        proj["scope"] = {"cwd": str(repo), "branch": "main"}
        ns = {"project_store": types.SimpleNamespace(
                  get=lambda slug: proj, scope_for=lambda slug: dict(proj["scope"])),
              "worktrees": W, "merge": M, "branches": B, "verify": V,
              "log": logging.getLogger("test"),
              "repo_guard": lambda *a, **k: contextlib.nullcontext(),
              "record_landing": lambda *a, **k: None}
        lift({"land_if_ready", "landing_enabled", "verify_work",
                  "LANDING_UNDERWAY"}, ns=ns, absent=LANDING_ROUTES)
        r = ns["land_if_ready"]({"id": "tsk_flaky", "project": "cadence",
                                 "role": "implementor", "verified": True,
                                 "scope": {"cwd": str(repo), "branch": "main"}})
        check("a landing whose simulator failed to launch once still lands",
              r.get("landed") is True, f"got {r.get('stage')}: {r.get('detail', '')[-200:]}")
        check("having run the suite three times: retry, then after the merge",
              counter.read_text() == "3", f"runs={counter.read_text()}")

        counter.unlink()
        r = ns["verify_work"]({"id": "tsk_v", "project": "cadence"}, repo)
        check("verifying an implementor's work does not retry",
              not r["ok"] and counter.read_text() == "1", f"runs={counter.read_text()}")
    finally:
        W.ROOT = saved_root


# --- a base that fails its own tests is noticed once, not per task --------------
# 2026-10-12: trader's main failed its own suite (a deliberate review-by date in
# ops/bar_integrity_baseline.json passed on 10-09). Every trader task then
# failed verification on that one test, was sent back, parked "never
# verified", and one agent spent a run proving the failure was not its own.

def _red_stores(slug="trader", test_cmd="pytest"):
    import projects as P
    import tasks as T
    d = Path(tempfile.mkdtemp())
    ps = P.ProjectStore(d / "p.json")
    ps.ensure(slug, test_cmd=test_cmd, auto_merge=True,
              scope={"cwd": str(d), "branch": "main"})
    return ps, T.TaskStore(d / "t.json")


def test_failed_tests_names_each_failure_once():
    print("\nfailing tests are read off a run by name, from every runner")
    import verify as V
    silk = ("  ✔ fine\n  ✘ the broken one — got 3\n  ✔ quoting '✘ not this'\n"
            "\n12 passed, 1 failed\n  FAILED: the broken one\n")
    check("Silkworm's runner: the check, once, without its detail",
          V.failed_tests(silk) == ["the broken one"], str(V.failed_tests(silk)))
    py = ("tests/test_bar_integrity.py F.\n=== short test summary info ===\n"
          "FAILED tests/test_bar_integrity.py::test_the_shipped_baseline - "
          "AssertionError: review_by 2026-10-09 passed\n"
          "==== 1 failed, 40 passed in 3.1s ====\n")
    check("pytest: the node id, without the assertion message",
          V.failed_tests(py) == ["tests/test_bar_integrity.py::test_the_shipped_baseline"],
          str(V.failed_tests(py)))
    xc = ("Test Case '-[AppTests.FooTests testBar]' failed (0.01 seconds).\n"
          "/x/FooTests.swift:9: error: -[AppTests.FooTests testBaz] : "
          "XCTAssertEqual failed: (\"1\") is not equal to (\"2\")\n"
          "✘ Test quux() recorded an issue at Q.swift:3:5\n"
          "Executed 9 tests, with 2 failures (0 unexpected)\n** TEST FAILED **\n")
    check("xcodebuild and Swift Testing: by test, tallies left out",
          V.failed_tests(xc) == ["-[AppTests.FooTests testBar]",
                                 "-[AppTests.FooTests testBaz]", "quux()"],
          str(V.failed_tests(xc)))
    check("a run that names nothing names nothing",
          V.failed_tests("Traceback ...\nImportError: no module named x\n") == [])
    ut = ("F.E\n======\nFAIL: test_b (t.T.test_b)\n------\nAssertionError\n"
          "ERROR: test_c (t.T)\n\nRan 3 tests in 0.001s\n\nFAILED (failures=1, errors=1)\n")
    check("unittest: each test, never its tally",
          V.failed_tests(ut) == ["t.T.test_b", "t.T.test_c"], str(V.failed_tests(ut)))
    check("unittest: two different failures are not the same '(failures=1)'",
          V.failed_tests("FAIL: test_a (t.T.test_a)\nFAILED (failures=1)\n")
          != V.failed_tests("FAIL: test_b (t.T.test_b)\nFAILED (failures=1)\n"))
    sw = ("✘ Test foo() recorded an issue at a.swift:3:4: Expectation failed\n"
          "✘ Test foo() failed after 0.1 seconds with 1 issue.\n"
          "✘ Suite X failed after 0.02 seconds with 1 issue.\n"
          "✘ Test run with 12 tests failed after 0.031 seconds with 1 issue.\n")
    check("swift test: the test, not the suite or run summary with its timing",
          V.failed_tests(sw) == ["foo()"], str(V.failed_tests(sw)))
    many = "".join(f"  ✘ check {i}\n" for i in range(40))
    check("never trimmed: a failure cut off would look like the base's",
          len(V.failed_tests(many)) == 40)


def test_only_failures_also_failing_on_base_are_the_bases():
    print("\nonly a task failing nothing but the base's own failures is held")
    import basered as B
    base = ["a", "b"]
    check("the same failures: the base's", B.only_failing_on_base(["a"], base))
    check("all of them: the base's", B.only_failing_on_base(["b", "a"], base))
    check("one more than the base fails: a real failure",
          not B.only_failing_on_base(["a", "c"], base),
          "holding it would hide a test this change broke")
    check("only others: a real failure", not B.only_failing_on_base(["c"], base))
    check("a run that named no test: real, nothing can be compared",
          not B.only_failing_on_base([], base))
    check("a base that named no test holds nothing",
          not B.only_failing_on_base(["a"], []))


def test_base_suite_runs_once_per_commit():
    print("\nthe base's suite runs at most once per base commit")
    import threading as _th
    import basered as B
    ps, _ = _red_stores()
    runs = []

    def run(sha):
        runs.append(sha)
        time.sleep(0.05)
        return {"ran": True, "ok": False, "code": 1, "output": "  ✘ t1\n",
                "failed": ["t1"]}

    first = B.check(ps, "trader", "a" * 40, run)
    again = B.check(ps, "trader", "a" * 40, run)
    check("red, with the failing test named",
          first and not first["ok"] and first["failed"] == ["t1"], str(first))
    check("asked twice, run once", runs == ["a" * 40], str(runs))
    check("and the second answer is the first", again == first)
    import projects as P
    check("cached on the project record, so a restart remembers it",
          "a" * 40 in (P.ProjectStore(ps._path).get("trader")["base_checks"]))
    B.check(ps, "trader", "b" * 40, run)
    check("a new base commit is a new question", runs == ["a" * 40, "b" * 40])

    out = []
    ts = [_th.Thread(target=lambda: out.append(B.check(ps, "trader", "c" * 40, run)))
          for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("three failures at once wait on one run", runs.count("c" * 40) == 1, str(runs))

    gone = B.check(ps, "trader", "d" * 40,
                   lambda s: {"ran": False, "ok": False, "output": "no checkout"})
    check("could not run: no answer", gone is None)
    check("and nothing cached, so the next failure asks again",
          B.cached(ps, "trader", "d" * 40) is None)
    launch = {"ran": True, "ok": False, "launch_failure": True, "output": "Unable to boot"}
    check("a suite that never launched is no answer either",
          B.check(ps, "trader", "e" * 40, lambda s: launch) is None
          and B.cached(ps, "trader", "e" * 40) is None)
    for i in range(B.MAX_CHECKS + 3):
        B.check(ps, "trader", f"{i:040d}", lambda s: {"ran": True, "ok": True})
    check("a handful kept, not every commit for ever",
          len(ps.get("trader")["base_checks"]) == B.MAX_CHECKS)


def test_failing_base_gets_one_attention_item():
    print("\na failing base is one needs-you item, however many tasks hit it")
    import basered as B
    import tasks as T
    ps, ts = _red_stores()
    red = {"sha": "a" * 40, "ok": False, "failed": ["t1"]}
    state, news = B.mark_red(ps, ts, "trader", red, "main")
    state2, news2 = B.mark_red(ps, ts, "trader", red, "main")
    items = [t for t in ts.all().values() if t.get("source") == B.SOURCE]
    check("one item", len(items) == 1, str(len(items)))
    check("in needs you", items and items[0]["state"] == T.NEEDS_INPUT
          and items[0]["state"] in T.NEEDS_ATTENTION)
    check("titled with the one line",
          items and items[0]["title"] == "trader main is failing: t1", items and items[0]["title"])
    check("news once, and only once", news and not news2)
    check("recorded on the project: tests, base sha, its item",
          ps.get("trader")["main_failing"]["tests"] == ["t1"]
          and ps.get("trader")["main_failing"]["sha"] == "a" * 40
          and ps.get("trader")["main_failing"]["attention"] == items[0]["id"])
    check("never run on its own", ts.claim(except_roles=()) is None)

    worse = {"sha": "b" * 40, "ok": False, "failed": ["t1", "t2"]}
    B.mark_red(ps, ts, "trader", worse, "main")
    items = [t for t in ts.all().values() if t.get("source") == B.SOURCE]
    check("a later commit still failing updates the same item",
          len(items) == 1 and items[0]["title"] == "trader main is failing: t1, t2",
          str([i["title"] for i in items]))
    check("and keeps when it started failing",
          ps.get("trader")["main_failing"]["since"] == state["since"])

    ts.transition(items[0]["id"], T.CANCELLED, "dismissed")
    B.mark_red(ps, ts, "trader", worse, "main")
    check("dismissed: not reopened for the same commit",
          len([t for t in ts.all().values() if t.get("source") == B.SOURCE]) == 1)
    B.mark_red(ps, ts, "trader", {"sha": "c" * 40, "ok": False, "failed": ["t1"]}, "main")
    opened = [t for t in ts.all().values() if t.get("source") == B.SOURCE
              and t["state"] == T.NEEDS_INPUT]
    check("but a later commit that still fails opens one again", len(opened) == 1)


def test_base_recovery_clears_state_and_closes_the_item():
    print("\na base that passes again clears the state and closes the item")
    import basered as B
    import tasks as T
    ps, ts = _red_stores()
    state, _ = B.mark_red(ps, ts, "trader", {"sha": "a" * 40, "failed": ["t1"]}, "main")
    t = ts.create("work", project="trader", role="implementor", driver="queue")
    B.hold(ts, t["id"], "verify", "a" * 40)
    other = ts.create("work", project="cadence", role="implementor", driver="queue")
    B.hold(ts, other["id"], "verify", "a" * 40)
    was, held = B.recover(ps, ts, "trader", "b" * 40)
    check("hands back what it cleared", was.get("sha") == "a" * 40)
    check("and this project's held tasks, no other's",
          [h["id"] for h in held] == [t["id"]], str([h["id"] for h in held]))
    check("the project is no longer failing", not ps.get("trader")["main_failing"]
          and B.failing(ps) == set())
    check("its item is closed", ts.get(state["attention"])["state"] == T.DONE)
    check("a second recovery is nothing", B.recover(ps, ts, "trader", "c" * 40) == ({}, []))


def test_runner_skips_implementors_of_a_failing_base():
    print("\nthe runner takes no implementor work where the base fails")
    import tasks as T
    d = Path(tempfile.mkdtemp())
    st = T.TaskStore(d / "t.json")
    impl = st.create("fix", project="trader", role="implementor", driver="queue")
    asst = st.create("look", project="trader", role="assistant", driver="queue")
    res = st.create("dig", project="trader", role="researcher", driver="queue")
    elsewhere = st.create("fix", project="cadence", role="implementor", driver="queue")
    got = []
    while True:
        r = st.claim(hold_implementors={"trader"})
        if not r:
            break
        got.append(r["id"])
    check("assistant and researcher work still runs",
          asst["id"] in got and res["id"] in got)
    check("another project's implementor still runs", elsewhere["id"] in got)
    check("the failing project's implementor stays queued",
          impl["id"] not in got and st.get(impl["id"])["state"] == T.QUEUED)
    check("and is claimed once the base is no longer failing",
          st.claim(hold_implementors=set())["id"] == impl["id"])
    src = (BASE / "bot.py").read_text()
    worker = src[src.index("def _task_worker("):src.index("#: How often to re-check for orphaned turns")]
    check("the task worker passes the failing projects to its claim",
          "hold_implementors=red" in worker and "basered.failing(project_store)" in worker)


def test_runner_lets_the_base_fix_through():
    import basered as B
    print("\nan implementor filed to fix a failing base is not held with the rest")
    import tasks as T
    ps, ts = _red_stores()
    state, _ = B.mark_red(ps, ts, "trader", {"sha": "a" * 40, "failed": ["t1"]}, "main")
    item = state["attention"]
    fix = ts.create("fix main", project="trader", role="implementor",
                    driver="queue", parent=item, root=item)
    # Shaped as bot.py files them: a landing conflict's catch-up rework and a
    # review's follow-up carry only the root, and no parent.
    redo = ts.create("catch up: fix main", project="trader", role="implementor",
                     driver="queue", source="rework", source_ref=fix["id"], root=item)
    followup = ts.create("tidy what the fix touched", project="trader",
                         role="implementor", driver="queue", source="review",
                         source_ref=fix["id"], root=item)
    redo2 = ts.create("catch up again: fix main", project="trader", role="implementor",
                      driver="queue", source="rework", source_ref=redo["id"], root=item)
    followup_redo = ts.create("catch up: tidy", project="trader", role="implementor",
                              driver="queue", source="rework",
                              source_ref=followup["id"], root=item)
    other = ts.create("unrelated", project="trader", role="implementor", driver="queue")
    research = ts.create("dig", project="trader", role="researcher", driver="queue")
    child = ts.create("next step", project="trader", role="implementor",
                      driver="queue", parent=research["id"], root=research["id"])
    ts.transition(research["id"], T.CANCELLED, "not the subject here")
    red = B.failing(ps)
    got = []
    while True:
        # Each finished as it is claimed, so nothing below waits on a running
        # parent: only the base-red hold can keep a task queued.
        r = ts.claim(hold_implementors=red, fixes_from={B.SOURCE})
        if not r:
            break
        got.append(r["id"])
        ts.transition(r["id"], T.DONE, "ran")
    check("the item's own child runs", fix["id"] in got)
    check("and its catch-up rework, by source_ref", redo["id"] in got)
    check("and a rework of that rework", redo2["id"] in got)
    ended = ts.create("fix the old spell", project="trader", role="implementor",
                      driver="queue", parent=item, root=item)
    ts.transition(item, T.CANCELLED, "the base passed; a new spell has its own item")
    check("a fix under an item that has ended is held like the rest",
          ts.claim(hold_implementors=red, fixes_from={B.SOURCE}) is None
          and ts.get(ended["id"])["state"] == T.QUEUED)
    check("a review follow-up on the fix stays held",
          followup["id"] not in got, str(got))
    check("and so does the follow-up's catch-up rework, though it shares the root",
          followup_redo["id"] not in got, str(got))
    check("other implementor work on the project stays held",
          other["id"] not in got and child["id"] not in got, str(got))
    ts2 = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    p = ts2.create("x", source=B.SOURCE, project="trader")
    k = ts2.create("fix", project="trader", role="implementor", driver="queue",
                   parent=p["id"])
    check("without fixes_from nothing is exempt",
          ts2.claim(hold_implementors={"trader"}) is None)
    check("with it, the fix is claimed",
          (ts2.claim(hold_implementors={"trader"}, fixes_from={B.SOURCE}) or {}).get("id")
          == k["id"])
    src = (BASE / "bot.py").read_text()
    worker = src[src.index("def _task_worker("):src.index("#: How often to re-check for orphaned turns")]
    check("the task worker exempts the base-red item's work",
          "fixes_from={basered.SOURCE}" in worker)


def test_filing_from_the_base_red_item_links_to_it():
    import basered as B
    print("\nwork filed from a failing base's item is filed under it")
    import tasks as T
    handle, budget, begin, ts, made = _file_task_impl()
    item = ts.create("trader main is failing", title="trader main is failing",
                     state=T.NEEDS_INPUT, role="assistant", driver="queue",
                     source=B.SOURCE, project="trader")
    ts.update(item["id"], thread="C1:111.1")
    goal = "Repair the expired baseline review-by date in trader"
    r = handle({"key": "C1:222.2", "goal": goal, "role": "implementor",
                "caller_task": item["id"]})
    t = ts.get(r.get("id") or "") or {}
    check("filed by the item's run: parent and project are the item's",
          r.get("ok") and t.get("parent") == item["id"] and t.get("root") == item["id"]
          and t.get("project") == "trader", str(r))
    check("still queued, as scoped work", t.get("state") == T.QUEUED)
    r = handle({"key": "C1:111.1", "goal": goal + " again", "role": "implementor"})
    t = ts.get(r.get("id") or "") or {}
    check("filed in the item's thread: linked too", t.get("parent") == item["id"], str(r))
    r = handle({"key": "C1:111.1", "goal": goal + " elsewhere", "role": "implementor",
                "project": "cadence"})
    t = ts.get(r.get("id") or "") or {}
    check("naming another project: not the base's fix",
          not t.get("parent") and t.get("project") == "cadence", str(t))
    r = handle({"key": "C1:333.3", "goal": goal + " from nowhere", "role": "implementor",
                "project": "trader"})
    t = ts.get(r.get("id") or "") or {}
    check("filed anywhere else: not linked", not t.get("parent"), str(t))


def _red_filing(slug="mondays"):
    """handle_file_task over real project and task stores, `slug`'s base red.

    The harness's project store is a stand-in that records what is ensured;
    the base-red lookups also want `get` and `all`, which come from a real
    store holding the red state. `requested_filing` is the grant check (not
    under test here): it vouches for any filing that asks."""
    import basered as B
    import tasks as T
    handle, budget, begin, _ts, made = _file_task_impl()
    ps, ts = _red_stores(slug)
    g = handle.__globals__
    g["task_store"] = ts
    g["project_store"] = types.SimpleNamespace(
        get=ps.get, all=ps.all, slug_for=g["project_store"].slug_for,
        ensure=lambda n, **kw: {"slug": n}, home=lambda n, create=False: None,
        scope_for=lambda n: None)
    g["requested_filing"] = lambda payload, key, role, parent, research: (
        {"by": "U_ME", "ts": "1.0", "notes": payload.get("notes") or "", "source": "Slack",
         "via": "slack", "at": time.time()}, "")
    state, _ = B.mark_red(ps, ts, slug, {"sha": "a" * 40, "failed": ["t1"]}, "main")
    ts.update(state["attention"], thread="D0:100.0")
    return handle, ps, ts, state["attention"]


def _claimed(ps, ts):
    import basered as B
    import tasks as T
    got = []
    while True:
        r = ts.claim(hold_implementors=B.failing(ps), fixes_from={B.SOURCE})
        if not r:
            return got
        got.append(r["id"])
        ts.transition(r["id"], T.DONE, "ran")


def test_a_base_fix_filed_from_the_operator_thread_runs():
    import tasks as T
    print("\na fix for a red base filed away from its item's thread is not held behind it")
    handle, ps, ts, item = _red_filing()
    # tsk_5c129269af, 2026-11-25: filed by the operator from its home thread
    # on the user's word, naming the base-red item in its goal -- and held
    # with every other Mondays implementor until the base it fixes passed.
    stuck = handle({"key": "D5:5.0", "goal": "Fix Mondays main, which fails its own "
                    f"tests ({item}): reproduce on clean main, then fix the cause",
                    "role": "implementor", "project": "mondays", "caller_role": "operator",
                    "requested_by_user": True, "notes": "user: go with your recommendations"})
    t = ts.get(stuck.get("id") or "") or {}
    check("naming the item in its goal: filed under it",
          stuck.get("ok") and t.get("parent") == item and t.get("root") == item
          and stuck.get("fixes") == item, str(stuck))
    flag = handle({"key": "D5:5.0", "goal": "Repair the push preference test on Mondays main",
                   "role": "implementor", "project": "mondays", "fixes": item})
    check("--fixes <item id>: filed under it",
          (ts.get(flag.get("id") or "") or {}).get("parent") == item, str(flag))
    base = handle({"key": "D5:5.0", "goal": "Make the Mondays store tests pass on main again",
                   "role": "implementor", "project": "mondays", "fixes": "base",
                   "requested_by_user": True, "notes": "user: fix main"})
    check("--fixes base, on the user's word: filed under the project's item",
          (ts.get(base.get("id") or "") or {}).get("parent") == item, str(base))
    other = handle({"key": "D5:5.0", "goal": "Add a share sheet to the Mondays issue view",
                    "role": "implementor", "project": "mondays"})
    aside = handle({"key": "D5:5.0", "goal": f"Add a share sheet to Mondays; {item} can wait",
                    "role": "implementor", "project": "mondays"})
    check("a goal merely mentioning the item, not on the user's word: not linked",
          aside.get("ok") and not (ts.get(aside["id"]) or {}).get("parent"), str(aside))
    loose = handle({"key": "D5:5.0", "goal": f"Fix the base {item} tracks, somewhere",
                    "role": "implementor", "requested_by_user": True, "notes": "user: fix it"})
    check("named in the goal with no project: not linked, nor moved into the red one",
          loose.get("ok") and not (ts.get(loose["id"]) or {}).get("parent")
          and (ts.get(loose["id"]) or {}).get("project") != "mondays", str(loose))
    check("unrelated work filed there: not linked",
          other.get("ok") and not (ts.get(other["id"]) or {}).get("parent"), str(other))
    bare = handle({"key": "D5:5.0", "goal": "Make the Mondays store tests pass on main",
                   "role": "implementor", "project": "mondays", "fixes": "base"})
    check("--fixes base not on the user's word: refused, nothing filed",
          not bare.get("ok") and "user's word" in (bare.get("error") or ""), str(bare))
    wrong = handle({"key": "D5:5.0", "goal": "Repair the Mondays push preference test",
                    "role": "implementor", "project": "mondays", "fixes": "tsk_nothere"})
    check("--fixes naming no open item: refused",
          not wrong.get("ok") and "no open item" in (wrong.get("error") or ""), str(wrong))
    elsewhere = handle({"key": "D5:5.0", "goal": f"Tidy cadence's settings, unlike {item}",
                        "role": "implementor", "project": "cadence"})
    check("naming the item under another project: not that base's fix",
          not (ts.get(elsewhere.get("id") or "") or {}).get("parent"), str(elsewhere))
    got = _claimed(ps, ts)
    check("the fixes run while the base is red",
          {stuck["id"], flag["id"], base["id"]} <= set(got), str(got))
    check("unrelated implementor work on the project stays held",
          other["id"] not in got and aside["id"] not in got
          and ts.get(other["id"])["state"] == T.QUEUED, str(got))
    check("another project's work is not held", elsewhere["id"] in got, str(got))
    ts.transition(item, T.CANCELLED, "dismissed")
    late = handle({"key": "D5:5.0", "goal": f"Fix Mondays main per {item}, once more",
                   "role": "implementor", "project": "mondays",
                   "requested_by_user": True, "notes": "user: once more"})
    check("naming an item that has ended links nothing",
          late.get("ok") and not (ts.get(late["id"]) or {}).get("parent"), str(late))


def test_the_operator_links_a_queued_task_as_the_base_fix():
    import basered as B
    import tasks as T
    print("\nqueued work filed before it could say so is linked as the base's fix")
    ps, ts = _red_stores("mondays")
    state, _ = B.mark_red(ps, ts, "mondays", {"sha": "a" * 40, "failed": ["t1"]}, "main")
    item = state["attention"]
    link = _bot_func("link_base_fix", task_store=ts, project_store=ps, basered=B,
                     tasks=T, time=time, log=logging.getLogger("test"))
    stuck = ts.create("Fix Mondays main", project="mondays", role="implementor",
                      driver="queue", source="scoped", filed_by="operator")
    other = ts.create("unrelated", project="mondays", role="implementor", driver="queue")
    check("the stuck case: held while unlinked", _claimed(ps, ts) == [])
    r = link({"id": stuck["id"], "notes": "user asked to fix main", "by": "operator"})
    t = ts.get(stuck["id"])
    check("linked under the item", r.get("ok") and t.get("parent") == item
          and t.get("root") == item, str(r))
    check("and says so in its events", any(e.get("kind") == "linked" and item in e.get("detail", "")
                                           for e in t.get("events") or []))
    check("again: already linked, still ok", link({"id": stuck["id"]}).get("ok"))
    check("naming another item: refused",
          not link({"id": other["id"], "item": "tsk_nope"}).get("ok")
          and not ts.get(other["id"]).get("parent"))
    prop = ts.create("an idea", project="mondays", role="implementor", driver="queue",
                     state=T.PROPOSED)
    check("a proposal: refused, it is the user's to accept",
          not link({"id": prop["id"]}).get("ok") and not ts.get(prop["id"]).get("parent"))
    green = ts.create("work", project="cadence", role="implementor", driver="queue")
    check("a project whose base is not red: refused", not link({"id": green["id"]}).get("ok"))
    research = ts.create("dig", project="mondays", role="researcher", driver="queue",
                         state=T.NEEDS_INPUT)
    under = ts.create("next", project="mondays", role="implementor", driver="queue",
                      parent=research["id"])
    check("already under another parent: refused",
          not link({"id": under["id"]}).get("ok")
          and ts.get(under["id"])["parent"] == research["id"])
    got = _claimed(ps, ts)
    check("the linked task runs; unrelated work stays held",
          stuck["id"] in got and other["id"] not in got, str(got))
    import operator_role as O
    check("the operator needs no approval to link (only queued work)",
          O.tasks_guard("fixes-base", {"id": stuck["id"]}, dict(stuck, state="queued")) is None)
    cli = _load_cli()
    pay = cli.parse_action("fixes-base", [stuck["id"], "--item", item, "--notes", "why"])
    check("`silkworm fixes-base` sends the action, the item and the notes",
          pay.get("action") == "fixes-base" and pay.get("item") == item
          and pay.get("id") == stuck["id"] and pay.get("notes") == "why", str(pay))
    src = (BASE / "bot.py").read_text()
    check("/tasks routes fixes-base to link_base_fix",
          'if action == "fixes-base":\n        return link_base_fix(payload)' in src)
    do_file_task, sent = _cli_file_task_impl()
    os.environ["SILKWORM_THREAD"] = "D5:5.0"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            do_file_task(["--fixes", item, "Repair", "the", "push", "test"])
    finally:
        os.environ.pop("SILKWORM_THREAD", None)
    check("`silkworm task --fixes` sends it, the goal alone as goal",
          sent and sent[-1][1].get("fixes") == item
          and sent[-1][1].get("goal") == "Repair the push test", str(sent))


def _base_glue(ps, ts, *, suite, base=("main", "b" * 40), moved=True, verify_ok=True):
    """ask_base and its neighbours, lifted out of bot.py, against real stores."""
    import basered as _br
    import merge as _merge
    import tasks as _T
    import verify as _V
    calls = {"posts": [], "suite": [], "landings": [], "reviews": [], "sent_back": [],
             "verified": [], "told": []}

    class Client:
        def chat_postMessage(self, **kw):
            calls["posts"].append(kw)
            return {"ts": f"{len(calls['posts'])}.0"}

    class SyncThread:
        def __init__(self, target, args=(), **kw):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    def run_suite(repo, slug, sha):
        calls["suite"].append(sha)
        return suite(sha)

    def state(tid, to, detail=""):
        try:
            ts.transition(tid, to, detail)
        except Exception:
            pass

    wt = types.SimpleNamespace(is_repo=lambda p: True, drop_scratch=lambda *a: True,
                               attach=lambda *a, **k: Path(tempfile.mkdtemp()))
    ns = lift(
        "ask_base", "hold_for_base", "hold_landing_for_base", "release_base",
        "reverify_held", "announce_red", "release_held",
        project_store=ps, task_store=ts, basered=_br, verify=_V, merge=_merge,
        tasks=_T, threading=types.SimpleNamespace(Thread=SyncThread),
        app=types.SimpleNamespace(client=Client()), home_channel=lambda: "DHOME",
        base_repo=lambda task: "/repo", current_base=lambda repo, task: base,
        run_base_suite=run_suite, base_moved_on=lambda repo, red, sha: moved,
        task_state=state, worktrees=wt,
        branches=types.SimpleNamespace(name_for=lambda t: "silkworm/" + t["id"]),
        start_landing=lambda tid, approved=False: calls["landings"].append((tid, approved)),
        verify_work=lambda task, cwd: (calls["verified"].append(task["id"]) or
                                       {"ran": True, "ok": verify_ok, "code": 0 if verify_ok else 1,
                                        "output": "" if verify_ok else "  ✘ t9\n",
                                        "failed": [] if verify_ok else ["t9"]}),
        resolve_review=lambda task, role, text, c, t: calls["reviews"].append(task["id"]),
        send_back_for_tests=lambda task, r, c, t: calls["sent_back"].append(task["id"]),
        tell_thread=lambda key, text: calls["told"].append(text),
        record_landing=lambda task, outcome: calls.setdefault("recorded", []).append(
            (task["id"], outcome)))
    return ns, calls


def _running(ts, project="trader"):
    import tasks as T
    t = ts.create("change", project=project, role="implementor", driver="queue",
                  thread="C1:1.0", isolate=True)
    ts.transition(t["id"], T.RUNNING, "claimed")
    ts.update(t["id"], result={"text": "did it"})
    return ts.get(t["id"])


def test_task_failing_only_the_bases_tests_is_held():
    print("\nverification failing only what the base fails holds the task")
    import tasks as T
    ps, ts = _red_stores()
    red = lambda sha: {"ran": True, "ok": False, "code": 1, "output": "  ✘ t1\n",
                       "failed": ["t1"]}
    ns, calls = _base_glue(ps, ts, suite=red)
    a, b = _running(ts), _running(ts)
    failing_t1 = {"ran": True, "ok": False, "code": 1, "output": "  ✘ t1\n", "failed": ["t1"]}
    check("held", ns["hold_for_base"](a, failing_t1, "C1", "1.0") is True)
    check("the second task too", ns["hold_for_base"](b, failing_t1, "C1", "1.0") is True)
    check("the base was run once for both", calls["suite"] == ["b" * 40], str(calls["suite"]))
    for t in (a, b):
        rec = ts.get(t["id"])
        check("parked blocked, on no task", rec["state"] == T.BLOCKED
              and not rec.get("blocked_on") and rec["held_base"]["kind"] == "verify")
        check("not counted as a failed attempt", not rec.get("verify_attempts"))
        check("not in needs you", rec["state"] not in T.NEEDS_ATTENTION)
    items = [t for t in ts.needs_attention() if t["project"] == "trader"]
    check("one needs-you item for the project, not one per task",
          len(items) == 1 and items[0]["title"] == "trader main is failing: t1",
          str([i["title"] for i in items]))
    home = [p for p in calls["posts"] if p.get("channel") == "DHOME"]
    check("announced once", len(home) == 1 and "trader main is failing: t1" in home[0]["text"])
    check("and the announcement is the item's thread",
          ts.get(items[0]["id"])["thread"] == "DHOME:1.0")

    c = _running(ts)
    extra = {"ran": True, "ok": False, "code": 1, "output": "  ✘ t1\n  ✘ t2\n",
             "failed": ["t1", "t2"]}
    check("a test the base passes: real, not held",
          ns["hold_for_base"](c, extra, "C1", "1.0") is False
          and ts.get(c["id"])["state"] == T.RUNNING and not ts.get(c["id"]).get("held_base"))
    d = _running(ts)
    crash = {"ran": True, "ok": False, "code": 1, "output": "Traceback\n", "failed": []}
    check("a run that names nothing: real", ns["hold_for_base"](d, crash, "C1", "1.0") is False)

    ps2, ts2 = _red_stores()
    ns2, calls2 = _base_glue(ps2, ts2, suite=lambda sha: {"ran": True, "ok": True})
    e = _running(ts2)
    check("a base that passes: the failure is the task's",
          ns2["hold_for_base"](e, failing_t1, "C1", "1.0") is False
          and not ps2.get("trader")["main_failing"] and not ts2.needs_attention())
    ns3, _ = _base_glue(ps2, ts2, suite=lambda sha: {"ran": False, "ok": False, "output": "x"})
    check("a base that cannot be asked: the failure is the task's",
          ns3["hold_for_base"](e, failing_t1, "C1", "1.0") is False)

    src = (BASE / "bot.py").read_text()
    body = src[src.index("def execute_task("):src.index("def verify_work(")]
    check("execute_task asks the base before sending work back",
          0 < body.index("hold_for_base(task, checked") < body.index("send_back_for_tests(task, checked"))


def test_landing_failing_only_the_bases_tests_is_held():
    print("\na landing failing only what the base fails is held, not refused")
    import merge as M
    ps, ts = _red_stores()
    ns, calls = _base_glue(ps, ts, suite=lambda sha: {
        "ran": True, "ok": False, "code": 1, "output": "  ✘ t1\n", "failed": ["t1"]})
    t = ts.create("change", project="trader", role="implementor", driver="queue")
    refused = {"eligible": True, "landed": False, "stage": "tests-after-rebase",
               "detail": "x", "branch": "b", "base": "main", "onto": "c" * 40,
               "failed": ["t1"]}
    out = ns["hold_landing_for_base"](t, refused, True)
    check("held at base-failing", out and out["stage"] == M.BASE_FAILING
          and out["eligible"] and not out["landed"], str(out))
    check("asked about the commit it was rebased onto", calls["suite"] == ["c" * 40])
    check("the task remembers it, and that it was approved",
          ts.get(t["id"])["held_base"] == {**ts.get(t["id"])["held_base"],
                                           "kind": "landing", "approved": True})
    check("needs no person: no rework, no awaiting-approval", not M.needs_a_person(out))
    check("and says it is held", "held" in M.summary(out, "b").lower())
    real = {**refused, "failed": ["t1", "t3"]}
    t2 = ts.create("change", project="trader", role="implementor", driver="queue")
    check("one more failure than the base: still a refusal",
          ns["hold_landing_for_base"](t2, real, False) is None
          and M.needs_a_person(real))
    src = (BASE / "bot.py").read_text()
    body = src[src.index("def land_if_ready("):src.index("def record_landing(")]
    check("land_if_ready asks after both test stages",
          "hold_landing_for_base(task, result, approved)" in body
          and '"tests-after-rebase", "tests-after-merge"' in body)
    import digest as D
    check("not a refusal in the digest", M.BASE_FAILING in D.NOT_REFUSALS)


def test_landing_refusals_carry_failures_and_base():
    print("\na landing refused on its tests says which, and on which base commit")
    import merge as M
    repo = Path(tempfile.mkdtemp())
    g = lambda *a, cwd=repo: subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t",
                                            "-c", "user.name=t", *a],
                                           capture_output=True, text=True, check=True)
    g("init", "-q", "-b", "main")
    g("commit", "-q", "--allow-empty", "-m", "base")
    base = g("rev-parse", "HEAD").stdout.strip()
    g("checkout", "-q", "-b", "silkworm/tsk_x")
    (repo / "a").write_text("a\n")
    g("add", "a")
    g("commit", "-q", "-m", "work")
    g("checkout", "-q", "main")
    wt = Path(tempfile.mkdtemp()) / "wt"
    g("worktree", "add", "-q", str(wt), "silkworm/tsk_x")
    bad = {"ok": False, "ran": True, "output": "  ✘ t1\n", "failed": ["t1"]}
    r = M.land(wt, repo, "silkworm/tsk_x", "main", lambda cwd: bad, generated=[])
    check("after the rebase: the failing tests and the commit",
          r["stage"] == "tests-after-rebase" and r.get("failed") == ["t1"]
          and r.get("onto") == base, str(r))
    # Bad in the checkout only with the branch's file there: failing without
    # it too would be the checkout's own failure (merge.CHECKOUT_FAILING).
    r = M.land(wt, repo, "silkworm/tsk_x", "main",
               lambda cwd: ({"ok": True} if Path(cwd) == wt or not (repo / "a").exists()
                            else bad), generated=[])
    check("after the merge too, with the base it was put back to",
          r["stage"] == "tests-after-merge" and r.get("failed") == ["t1"]
          and r.get("onto") == base and g("rev-parse", "HEAD").stdout.strip() == base, str(r))


def _checkout_rig():
    """A base repo, a git helper, and a way to cut a branch adding one file."""
    root = Path(tempfile.mkdtemp())
    repo = root / "repo"; repo.mkdir()
    def g(cwd, *a): return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=str(cwd),
        capture_output=True, text=True)
    g(repo, "init", "-q", "-b", "main")
    (repo / "v.txt").write_text("1\n")
    g(repo, "add", "-A"); g(repo, "commit", "-qm", "base")
    names = iter(range(100))

    def branch(add="doc.txt"):
        name = f"silkworm/tsk_c{next(names)}"
        wt = root / name.replace("/", "_")
        g(repo, "worktree", "add", "-q", "-b", name, str(wt), "main")
        (wt / add).write_text("a docstring, nothing else\n")
        g(wt, "add", "-A"); g(wt, "commit", "-qm", name)
        return wt, name
    return root, repo, g, branch


def test_a_failed_reset_after_merge_is_not_tested_or_called_put_back():
    import merge as M, verify as V
    print("\na landing whose reset back fails says so, and skips step 5b")
    root, repo, g, branch = _checkout_rig()
    # Fails in the checkout whenever the checkout has the branch's file -- with
    # or without the branch otherwise, which 5b would read as the checkout's.
    (repo / "live.txt").write_text("checkout only\n")
    CMD = ("sh -c 'if [ -f live.txt ]; then echo \"  ✘ test_live\"; exit 1; fi;"
           " echo \"  ✔ fine\"'")
    lock = repo / ".git" / "index.lock"
    seen = []

    def tests(cwd):
        seen.append(str(cwd))
        out = V.run(CMD, cwd)
        if str(cwd) == str(repo):
            lock.write_text("")             # a git process holding the index
        return out

    before = g(repo, "rev-parse", "HEAD").stdout.strip()
    wt, name = branch()
    try:
        r = M.land(wt, repo, name, "main", tests, generated=[])
        head = g(repo, "rev-parse", "HEAD").stdout.strip()
    finally:
        lock.unlink(missing_ok=True)
    check("refused as reset-failed, not held as the checkout's",
          r.get("stage") == M.RESET_FAILED and not r.get("landed"), str(r)[:300])
    check("the base really was left on the merged tree", head != before, head)
    check("the merged tree was not tested again as if it were the base",
          seen.count(str(repo)) == 1, str(seen))
    check("it says the base could not be put back, never that it was",
          "could NOT be put back" in r.get("detail", "")
          and "was put back" not in r.get("detail", "")
          and before[:8] in r["detail"] and head[:8] in r["detail"], r.get("detail"))
    check("naming the failed test, with the base it should be at",
          r.get("failed") == ["test_live"] and r.get("onto") == before)
    check("and it needs a person",
          M.needs_a_person({**r, "eligible": True}))


def test_a_failure_only_in_the_checkout_is_the_checkouts():
    import merge as M, verify as V
    print("\na test failing only in the project's checkout is not the branch's fault")
    root, repo, g, branch = _checkout_rig()
    # The trader case: a test reading something only the live checkout has.
    (repo / "live.txt").write_text("a stale paper report\n")
    CMD = ("sh -c 'if [ -f live.txt ]; then echo \"  ✘ test_entry_point_runs_end_to_end\";"
           " exit 1; fi; echo \"  ✔ fine\"'")
    seen = []

    def tests(cwd):
        seen.append((str(cwd), g(cwd, "rev-parse", "HEAD").stdout.strip()))
        return V.run(CMD, cwd)

    before = g(repo, "rev-parse", "HEAD").stdout.strip()
    wt, name = branch()
    r = M.land(wt, repo, name, "main", tests, generated=[])
    check("held at checkout-failing, not refused as breaking the base",
          r.get("stage") == M.CHECKOUT_FAILING and not r.get("landed"), str(r)[:300])
    check("the base is put back exactly where it was",
          g(repo, "rev-parse", "HEAD").stdout.strip() == before)
    check("it names the test, says the worktree's run passed, and blames the checkout",
          "test_entry_point_runs_end_to_end" in r.get("detail", "")
          and "worktree" in r["detail"] and "not to this branch" in r["detail"]
          and r.get("failed") == ["test_entry_point_runs_end_to_end"], r.get("detail"))
    check("with the base commit, for the one item to name",
          r.get("onto") == before and r.get("base") == "main")
    check("the third run was the checkout itself, back at the base",
          len(seen) == 3 and seen[2] == (str(repo), before), str(seen))
    check("it needs no person per task: the project's item speaks for it",
          not M.needs_a_person({**r, "eligible": True}))
    check("and its line says held, not 'waiting for you'",
          "held" in M.summary(r, name).lower()
          and "waiting for you" not in M.summary(r, name))
    import digest as D
    check("not a refusal in the digest", M.CHECKOUT_FAILING in D.NOT_REFUSALS)

    # A branch that really breaks the checkout: passes in its worktree, fails
    # with the checkout's files, and the checkout passes without it.
    (repo / "live.txt").unlink()
    (repo / "local.cfg").write_text("checkout only\n")
    CMD2 = ("sh -c 'if [ -f local.cfg ] && [ -f flag.txt ]; then "
            "echo \"  ✘ test_flag_with_config\"; exit 1; fi'")
    wt, name = branch(add="flag.txt")
    seen.clear()
    r = M.land(wt, repo, name, "main",
               lambda cwd: seen.append(str(cwd)) or V.run(CMD2, cwd), generated=[])
    check("a branch that really breaks the checkout is still refused at tests-after-merge",
          r.get("stage") == "tests-after-merge" and "merging broke the base" in r["detail"]
          and M.needs_a_person({**r, "eligible": True}), str(r)[:300])
    check("after asking the checkout without it", seen == [str(wt), str(repo), str(repo)],
          str(seen))

    # The checkout already fails one test; the branch adds a second there.
    CMD3 = ("sh -c 'echo \"  ✘ t_live\"; if [ -f flag2.txt ] && [ -f local.cfg ]; then "
            "echo \"  ✘ t_branch\"; fi; [ -f local.cfg ] && exit 1; exit 0'")
    wt, name = branch(add="flag2.txt")
    r = M.land(wt, repo, name, "main", lambda cwd: V.run(CMD3, cwd), generated=[])
    check("one more failure than the checkout's own is the branch's",
          r.get("stage") == "tests-after-merge"
          and set(r.get("failed") or []) == {"t_live", "t_branch"}, str(r)[:300])

    # Neither side naming a test: nothing to compare, so it stays the branch's.
    wt, name = branch()
    calls = []
    r = M.land(wt, repo, name, "main",
               lambda cwd: calls.append(1) or {"ran": True, "ok": len(calls) == 1,
                                                "code": 1, "output": "Traceback ..."},
               generated=[])
    check("an unnamed failure stays at tests-after-merge",
          r.get("stage") == "tests-after-merge" and len(calls) == 3, str(r)[:200])

    # The checkout could not be run at the base: unknown, so not moved off the branch.
    wt, name = branch()
    calls = []
    bad = {"ran": True, "ok": False, "code": 1, "output": "  ✘ t1\n", "failed": ["t1"]}
    r = M.land(wt, repo, name, "main",
               lambda cwd: (calls.append(1) or
                            [{"ran": True, "ok": True}, bad,
                             {"ran": True, "ok": False, "launch_failure": True,
                              "output": "  ✘ t1\n", "failed": ["t1"]}][min(len(calls), 3) - 1]),
               generated=[])
    check("a launch failure at the base does not clear the branch",
          r.get("stage") == "tests-after-merge", str(r)[:200])

    # A landing that passes pays nothing: two runs, as before.
    wt, name = branch()
    calls = []
    r = M.land(wt, repo, name, "main",
               lambda cwd: calls.append(1) or {"ran": True, "ok": True}, generated=[])
    check("a clean landing runs the suite exactly twice", r.get("landed") and len(calls) == 2,
          str(calls))


def test_the_checkout_rerun_happens_only_after_a_post_merge_failure():
    print("\nthe extra suite run in the checkout sits inside the post-merge failure")
    src = (BASE / "merge.py").read_text()
    tree = ast.parse(src)
    land = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "land")
    calls = [n for n in ast.walk(land) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "run_tests"]
    check("land runs the suite three times in its source, no more",
          len(calls) == 3, str(len(calls)))
    fail_ifs = [n for n in ast.walk(land) if isinstance(n, ast.If)
                and "after_merge.get('ok')" in ast.unparse(n.test)
                and ast.unparse(n.test).startswith("not ")]
    check("there is one post-merge failure branch", len(fail_ifs) == 1)
    inside = {id(c) for c in ast.walk(fail_ifs[0]) if isinstance(c, ast.Call)
              and isinstance(c.func, ast.Name) and c.func.id == "run_tests"}
    rerun = [c for c in calls if id(c) in inside]
    check("exactly one run is inside it, on the checkout",
          len(rerun) == 1 and ast.unparse(rerun[0].args[0]) == "repo",
          str([ast.unparse(c) for c in rerun]))
    resets = [n for n in ast.walk(fail_ifs[0]) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Name) and n.func.id == "_git"
              and any(isinstance(a, ast.Constant) and a.value == "reset" for a in n.args)]
    check("and after the reset back to the base",
          resets and all(r.lineno < rerun[0].lineno for r in resets))
    src_bot = (BASE / "bot.py").read_text()
    body = src_bot[src_bot.index("def land_if_ready("):src_bot.index("def record_landing(")]
    check("land_if_ready holds a checkout-failing landing for the checkout",
          "merge.CHECKOUT_FAILING" in body
          and "hold_landing_for_checkout(task, result, approved)" in body)
    fn = next(n for n in ast.parse(src_bot).body
              if isinstance(n, ast.FunctionDef) and n.name == "land_if_ready")
    landed = [n for n in ast.walk(fn) if isinstance(n, ast.If)
              and "result.get('landed')" in ast.unparse(n.test)
              and "checkout_failing" in ast.unparse(n.test)]
    check("and a landing that passes in a failing checkout unmarks it for the beat",
          len(landed) == 1 and any(
              isinstance(c, ast.Call) and ast.unparse(c.func) == "basered.clear_checkout"
              for c in ast.walk(landed[0].body[0]) if True), str(len(landed)))


def _checkout_glue(ps, ts, suite_ok=False):
    import threading
    import basered as _br
    import merge as _merge
    import tasks as _T
    calls = {"posts": [], "landings": [], "suite": [], "recorded": []}

    class Client:
        def chat_postMessage(self, **kw):
            calls["posts"].append(kw)
            return {"ts": f"{len(calls['posts'])}.0"}

    class SyncThread:
        def __init__(self, target, args=(), **kw):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    def run(cmd, cwd, **kw):
        calls["suite"].append(cwd)
        return {"ran": True, "ok": state["ok"], "output": "" if state["ok"] else "  ✘ t1\n"}
    state = {"ok": suite_ok}
    ns = lift(
        "hold_landing_for_checkout", "announce_checkout", "release_checkout_hold",
        "release_held_checkout", "_probe_checkout", "probe_failing_checkouts",
        project_store=ps, task_store=ts, basered=_br, merge=_merge, tasks=_T,
        threading=types.SimpleNamespace(Thread=SyncThread, Lock=threading.Lock),
        app=types.SimpleNamespace(client=Client()), home_channel=lambda: "DHOME",
        verify=types.SimpleNamespace(run=run), _repo_lock=lambda cwd: None,
        branches=types.SimpleNamespace(name_for=lambda t: "silkworm/" + t["id"]),
        start_landing=lambda tid, approved=False: calls["landings"].append((tid, approved)) or True,
        record_landing=lambda task, outcome: calls["recorded"].append((task["id"], outcome)),
        CHECKOUT_PROBE_S=3600, _checkout_probed_at={}, _base_probes=set(),
        _base_probes_guard=threading.Lock())
    return ns, calls, state


def test_a_failing_checkout_holds_landings_under_one_item():
    import merge as M
    import tasks as T
    import basered as B
    print("\na failing checkout holds its landings under one needs-you item")
    ps, ts = _red_stores()
    ns, calls, suite = _checkout_glue(ps, ts)
    held = {"eligible": True, "landed": False, "stage": M.CHECKOUT_FAILING,
            "branch": "b", "base": "main", "onto": "c" * 40,
            "failed": ["tests/test_paper_report.py::test_entry_point_runs_end_to_end"],
            "detail": "x"}
    ids = []
    for approved in (True, False, True):
        t = ts.create("change", project="trader", role="implementor", driver="queue",
                      state=T.AWAITING_APPROVAL)
        ids.append(t["id"])
        out = ns["hold_landing_for_checkout"](t, {**held, "branch": t["id"]}, approved)
        check("the outcome stays checkout-failing, needing no person",
              out["stage"] == M.CHECKOUT_FAILING and not M.needs_a_person(out))
        ts.update(t["id"], result={"landing": {"stage": M.CHECKOUT_FAILING}})
    items = [t for t in ts.all().values() if t.get("source") == B.CHECKOUT_SOURCE]
    check("three landings, one needs-you item for the project",
          len(items) == 1 and items[0]["state"] == T.NEEDS_INPUT
          and items[0]["project"] == "trader", str(len(items)))
    check("naming the test", "test_entry_point_runs_end_to_end" in items[0]["goal"])
    check("announced once", len(calls["posts"]) == 1, str(len(calls["posts"])))
    check("the project is marked", ps.get("trader")["checkout_failing"]["sha"] == "c" * 40)
    check("each task remembers it, and whether it was approved",
          [ts.get(i)["held_checkout"]["approved"] for i in ids] == [True, False, True])
    check("not mistaken for a failing base: implementor work is not paused",
          "trader" not in B.failing(ps) and not B.stranded(ps, ts))

    # The hourly look: still failing holds everything; and not again within the hour.
    ns["_checkout_probed_at"]["trader"] = 0
    ns["probe_failing_checkouts"](now=10_000)
    check("a still-failing checkout releases nothing",
          not calls["landings"] and len(calls["suite"]) == 1
          and "trader" in B.checkout_failing(ps))
    ns["probe_failing_checkouts"](now=10_100)
    check("and is not run again within the hour", len(calls["suite"]) == 1)

    # It passes: every held landing goes again, approved as it was; the item closes.
    t_gone = ts.get(ids[1])
    ts.transition(t_gone["id"], T.CANCELLED, "dropped")
    # Retried meanwhile: queued again with its old landing record and mark.
    retried = ts.create("change", project="trader", role="implementor", driver="queue",
                        state=T.QUEUED)
    B.hold_checkout(ts, retried["id"], "c" * 40, True)
    ts.update(retried["id"], result={"landing": {"stage": M.CHECKOUT_FAILING}})
    suite["ok"] = True
    ns["probe_failing_checkouts"](now=20_000)
    check("a passing checkout lands the held tasks again, approved as before",
          calls["landings"] == [(ids[0], True), (ids[2], True)], str(calls["landings"]))
    check("a task cancelled meanwhile only loses its mark",
          not ts.get(ids[1]).get("held_checkout"))
    check("and one retried meanwhile is not landed half-done",
          retried["id"] not in [x for x, _ in calls["landings"]]
          and not ts.get(retried["id"]).get("held_checkout"))
    check("every mark is gone and the project unmarked",
          not any(ts.get(i).get("held_checkout") for i in ids)
          and not ps.get("trader").get("checkout_failing"))
    check("and the item is closed", ts.get(items[0]["id"])["state"] in T.ENDED)

    # Closing the item is a person's word that it is fixed: no suite needed.
    t = ts.create("change", project="trader", role="implementor", driver="queue",
                  state=T.DONE)
    ns["hold_landing_for_checkout"](t, held, False)
    ts.update(t["id"], result={"landing": {"stage": M.CHECKOUT_FAILING}})
    item = ps.get("trader")["checkout_failing"]["attention"]
    check("a new failure opens a new item", item != items[0]["id"]
          and ts.get(item)["state"] == T.NEEDS_INPUT)
    ts.transition(item, T.DONE, "fixed it")
    before_suite = len(calls["suite"])
    ns["probe_failing_checkouts"](now=20_100)
    check("a closed item releases its landings without running the suite",
          calls["landings"][-1] == (t["id"], False) and len(calls["suite"]) == before_suite,
          str(calls["landings"]))

    # Stranded: held, but the project no longer marked (a crash part-way).
    t = ts.create("change", project="trader", role="implementor", driver="queue",
                  state=T.DONE)
    B.hold_checkout(ts, t["id"], "d" * 40, True)
    ts.update(t["id"], result={"landing": {"stage": M.CHECKOUT_FAILING}})
    ns["probe_failing_checkouts"](now=30_000)
    check("a held landing left over a released checkout is sent on",
          calls["landings"][-1] == (t["id"], True) and not ts.get(t["id"]).get("held_checkout"))

    # A hold that cannot be written must not leave the landing looking held.
    ns2, _, _ = _checkout_glue(ps, ts)
    ns2["basered"] = types.SimpleNamespace(
        mark_checkout=lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    t = ts.create("change", project="trader", role="implementor", driver="queue")
    out = ns2["hold_landing_for_checkout"](t, held, True)
    check("a hold that fails falls back to a refusal someone sees",
          out["stage"] == "tests-after-merge" and M.needs_a_person(out)
          and "could not be held" in out["detail"], str(out)[:200])
    src = (BASE / "bot.py").read_text()
    body = src[src.index("def execute_task("):]
    body = body[:body.index("progress = ProgressMessage(")]
    check("a task run again drops its checkout hold, as it drops its base hold",
          "held_checkout=None" in body)


def test_base_recovery_releases_held_work():
    print("\na base passing again releases what it held, and says so")
    import basered as B
    import tasks as T
    ps, ts = _red_stores()
    outcomes = {"a" * 40: {"ran": True, "ok": False, "code": 1, "output": "  ✘ t1\n",
                           "failed": ["t1"]},
                "b" * 40: {"ran": True, "ok": True, "code": 0, "output": ""}}
    ns, calls = _base_glue(ps, ts, suite=lambda sha: outcomes[sha], base=("main", "a" * 40))
    held_v = _running(ts)
    ns["hold_for_base"](held_v, {"ran": True, "ok": False, "failed": ["t1"],
                                 "output": "  ✘ t1\n"}, "C1", "1.0")
    lt = ts.create("landed", project="trader", role="implementor", driver="queue")
    ts.update(lt["id"], result={"landing": {"stage": "base-failing"}})
    B.hold(ts, lt["id"], "landing", "a" * 40, approved=True)
    item = ps.get("trader")["main_failing"]["attention"]

    ns2, calls2 = _base_glue(ps, ts, suite=lambda sha: outcomes[sha], base=("main", "b" * 40))
    probe = {"project": "trader", "scope": {}}
    out = ns2["ask_base"](probe, [], sha="b" * 40, base="main")
    check("the later commit passes", out == {"held": False, "state": None})
    check("the project is green", not ps.get("trader")["main_failing"])
    check("its item is closed", ts.get(item)["state"] == T.DONE)
    check("the held verification is verified again, then reviewed",
          calls2["verified"] == [held_v["id"]] and calls2["reviews"] == [held_v["id"]])
    check("and recorded as verified", ts.get(held_v["id"])["verified"] is True)
    check("the held landing is landed again, approved as before",
          calls2["landings"] == [(lt["id"], True)])
    check("neither is held any more",
          not ts.get(held_v["id"]).get("held_base") and not ts.get(lt["id"]).get("held_base"))
    check("and it is said",
          any("main is green again" in p.get("text", "") for p in calls2["posts"]))

    ps3, ts3 = _red_stores()
    ns3, calls3 = _base_glue(ps3, ts3, suite=lambda sha: outcomes[sha],
                             base=("main", "a" * 40), verify_ok=False)
    v = _running(ts3)
    ns3["hold_for_base"](v, {"ran": True, "ok": False, "failed": ["t1"],
                             "output": "  ✘ t1\n"}, "C1", "1.0")
    ns4, calls4 = _base_glue(ps3, ts3, suite=lambda sha: outcomes[sha],
                             base=("main", "b" * 40), verify_ok=False)
    ns4["ask_base"](probe, [], sha="b" * 40)
    check("failing again on a green base is a real failure, sent back",
          calls4["sent_back"] == [v["id"]] and not calls4["reviews"])

    ps5, ts5 = _red_stores()
    ns5, _ = _base_glue(ps5, ts5, suite=lambda sha: outcomes[sha], base=("main", "a" * 40))
    w = _running(ts5)
    ns5["hold_for_base"](w, {"ran": True, "ok": False, "failed": ["t1"],
                             "output": "  ✘ t1\n"}, "C1", "1.0")
    ns6, calls6 = _base_glue(ps5, ts5, suite=lambda sha: outcomes[sha], moved=False)
    ns6["ask_base"](probe, [], sha="b" * 40)
    check("a green answer about an older commit releases nothing",
          ps5.get("trader")["main_failing"] and ts5.get(w["id"])["held_base"]
          and not calls6["verified"])

    src = (BASE / "bot.py").read_text()
    sched = src[src.index("def _task_scheduler("):src.index("def hold_unsupervised(")]
    check("the scheduler watches failing bases for the fixing commit",
          "probe_failing_bases()" in sched)


def test_failing_base_is_one_line_on_board_and_digest():
    print("\na failing base is one line in the digest and on the dashboard")
    import basered as B
    import board as BD
    import digest as D
    ps, ts = _red_stores()
    B.mark_red(ps, ts, "trader", {"sha": "a" * 40, "failed": ["t1", "t2"]}, "main")
    for _ in range(3):
        t = ts.create("change", project="trader", role="implementor", driver="queue")
        ts.transition(t["id"], "running", "x")
        ts.transition(t["id"], "blocked", "held")
        B.hold(ts, t["id"], "verify", "a" * 40)
    now = time.time() + 3 * 86400            # long enough to read as stuck
    recs = list(ts.all().values())
    failing = {"trader": ps.get("trader")["main_failing"]}
    text = D.render(recs, now, failing=failing)
    check("the digest says it once",
          text.count("main is failing") == 1 and "t1, t2" in text, text)
    check("the held tasks are not listed as stuck", "stuck" not in text, text)
    check("nor the item counted as needs input as well", "needs input" not in text, text)
    check("without it, no line", "main is failing" not in D.render(recs, now))
    ov = BD.overview(ps.all(), recs)
    row = next(p for p in ov["projects"] if p["slug"] == "trader")
    check("the dashboard card carries the line",
          (row.get("main_failing") or {}).get("line") == "trader main is failing: t1, t2",
          str(row.get("main_failing")))
    check("and counts one thing needing you", row["needs"] == 1, str(row["needs"]))


def test_base_failing_without_naming_a_test_is_not_red():
    print("\na base that fails without naming a test does not pause the project")
    import tasks as T
    ps, ts = _red_stores()
    ns, calls = _base_glue(ps, ts, suite=lambda sha: {
        "ran": True, "ok": False, "code": None, "output": "tests did not finish within 1800s"})
    t = _running(ts)
    check("the task's failure stands as its own",
          ns["hold_for_base"](t, {"ran": True, "ok": False, "failed": ["t1"],
                                  "output": "  ✘ t1\n"}, "C1", "1.0") is False)
    check("and the project is not marked failing", not ps.get("trader")["main_failing"]
          and not [x for x in ts.needs_attention() if x.get("source") == "base-red"])


def test_held_work_is_never_stranded():
    print("\nheld work left over a green base is found and released")
    import basered as B
    import tasks as T
    ps, ts = _red_stores()
    ns, calls = _base_glue(ps, ts, suite=lambda sha: {"ran": True, "ok": True})
    # A restart part-way through a release: the project is green, and these
    # are still marked.
    v = _running(ts)
    ts.transition(v["id"], T.BLOCKED, "held")
    B.hold(ts, v["id"], "verify", "a" * 40)
    lt = ts.create("landed", project="trader", role="implementor", driver="queue")
    ts.update(lt["id"], result={"landing": {"stage": "base-failing"}})
    B.hold(ts, lt["id"], "landing", "a" * 40, approved=False)
    gone = ts.create("dropped", project="trader", role="implementor", driver="queue")
    ts.update(gone["id"], result={"landing": {"stage": "dropped"}})
    B.hold(ts, gone["id"], "landing", "a" * 40)
    moved = _running(ts)
    B.hold(ts, moved["id"], "verify", "a" * 40)            # retried by a person: running
    check("all four are found", {t["id"] for t in B.stranded(ps, ts)["trader"]}
          == {v["id"], lt["id"], gone["id"], moved["id"]})
    out = ns["release_held"]("trader")
    check("the held verification is verified again", calls["verified"] == [v["id"]])
    check("the held landing is landed again", calls["landings"] == [(lt["id"], False)])
    check("work since dropped or picked up again only loses the mark",
          gone["id"] not in out and moved["id"] not in out
          and not ts.get(gone["id"]).get("held_base")
          and not ts.get(moved["id"]).get("held_base"))
    check("nothing is left to find", not B.stranded(ps, ts))
    src = (BASE / "bot.py").read_text()
    body = src[src.index("def probe_failing_bases("):src.index("def _probe_base(")]
    check("the scheduler's beat looks for them", "basered.stranded(project_store, task_store)" in body
          and "release_held" in body)

    ps2, ts2 = _red_stores()
    ns2, calls2 = _base_glue(ps2, ts2, suite=lambda sha: {"ran": True, "ok": True})
    ns2["start_landing"] = lambda tid, approved=False: False
    lt2 = ts2.create("landed", project="trader", role="implementor", driver="queue")
    ts2.update(lt2["id"], result={"landing": {"stage": "base-failing"}})
    B.hold(ts2, lt2["id"], "landing", "a" * 40)
    ns2["release_held"]("trader")
    check("a landing that cannot be restarted says so on its record",
          [o["stage"] for _, o in calls2.get("recorded", [])] == ["held-released"])
    ps3, ts3 = _red_stores()
    ns3, calls3 = _base_glue(ps3, ts3, base=("main", "c" * 40), verify_ok=False,
                             suite=lambda sha: {"ran": True, "ok": False, "code": 1,
                                                "output": "  ✘ t9\n", "failed": ["t9"]})
    w = _running(ts3)
    ts3.transition(w["id"], T.BLOCKED, "held")
    B.hold(ts3, w["id"], "verify", "a" * 40)
    ns3["release_held"]("trader")
    check("red again at a later commit while releasing: held afresh, mark kept",
          ts3.get(w["id"])["state"] == T.BLOCKED
          and (ts3.get(w["id"]).get("held_base") or {}).get("sha") == "c" * 40
          and not calls3["sent_back"], str(ts3.get(w["id"]).get("held_base")))
    check("and the start-of-run clears a stale mark",
          'task_store.update(tid, held_base=None)' in
          src[src.index("def execute_task("):src.index("progress = ProgressMessage(app.client, channel, thread_ts)")])


def test_base_renamed_or_untested_releases_the_project():
    print("\na project whose base changed, or lost its tests, is not held for ever")
    import basered as B
    ps, ts = _red_stores()
    B.mark_red(ps, ts, "trader", {"sha": "a" * 40, "failed": ["t1"]}, "research/old")
    ns, calls = _base_glue(ps, ts, suite=lambda sha: {"ran": True, "ok": True}, moved=False)
    ns["ask_base"]({"project": "trader", "scope": {}}, [], sha="f" * 40, base="main")
    check("a green commit on the base it is named now releases it, ancestry or not",
          not ps.get("trader")["main_failing"])
    ps2, ts2 = _red_stores()
    st, _ = B.mark_red(ps2, ts2, "trader", {"sha": "a" * 40, "failed": ["t1"]}, "main")
    ts2.transition(st["attention"], "queued", "sent back")
    B.recover(ps2, ts2, "trader", "b" * 40)
    check("an item somebody queued is cancelled, not left to run on a green base",
          ts2.get(st["attention"])["state"] == "cancelled")
    src = (BASE / "bot.py").read_text()
    probe = src[src.index("def _probe_base("):src.index("def release_base(")]
    check("losing the test command lets it go", "release_base(slug" in probe
          and 'get("test_cmd")' in probe)
    guard = src[src.index("def take_step("):]
    guard = guard[:guard.index("def post(text)")]
    check("a held landing still blocks !release", "merge.BASE_FAILING" in guard)


def test_base_is_the_local_commit_a_landing_merges_into():
    print("\nthe base is asked about at the commit a landing would merge into")
    import branches as BR
    import worktrees as W
    up = Path(tempfile.mkdtemp())
    g = lambda d, *a: subprocess.run(["git", "-C", str(d), "-c", "user.email=t@t",
                                      "-c", "user.name=t", *a],
                                     capture_output=True, text=True, check=True).stdout.strip()
    g(up, "init", "-q", "-b", "main")
    g(up, "commit", "-q", "--allow-empty", "-m", "one")
    clone = Path(tempfile.mkdtemp()) / "c"
    subprocess.run(["git", "clone", "-q", str(up), str(clone)], check=True)
    local = g(clone, "rev-parse", "HEAD")
    g(up, "commit", "-q", "--allow-empty", "-m", "two, not pulled")
    g(clone, "fetch", "-q", "origin")
    ns = lift("current_base", worktrees=W, branches=BR, subprocess=subprocess,
                       task_base=lambda t: "main")
    name, sha = ns["current_base"](str(clone), {"project": "p"})
    check("origin ahead and not pulled: still the local commit",
          (name, sha) == ("main", local), f"{name} {sha[:8]} vs {local[:8]}")


# --- GitHub CI as the landing gate (ci.py) --------------------------------------
# Cadence landed on the mini's own `make -C ios test`, pushed main, and only
# then did GitHub run its workflows -- on another runner, after everybody was
# building on the result. Three kinds of red reached main that way in 2026-11.
# With `ci_gate` on, the rebased commit goes to origin as silkworm/<id>, never
# as main, and main moves only once GitHub's runs on that commit are green.

_FAKE_GH = r'''#!{python}
import json, subprocess, sys
state_path = {state!r}
with open(state_path) as f:
    st = json.load(f)
args = sys.argv[1:]
path = ([a for a in args[1:] if not a.startswith("-")] or [""])[0] if args[:1] == ["api"] else ""
def origin_refs():
    r = subprocess.run(["git", "--git-dir", st["origin"], "for-each-ref",
                        "--format=%(refname) %(objectname)"],
                       capture_output=True, text=True)
    return dict(l.split(" ", 1) for l in r.stdout.splitlines() if l)
call = {{"path": path, "refs": origin_refs()}}
st.setdefault("calls", []).append(call)
def save():
    with open(state_path, "w") as f:
        json.dump(st, f)
if st.get("down"):
    save(); sys.stderr.write("HTTP 502: Bad Gateway\n"); sys.exit(1)
if "/actions/runs?head_sha=" in path:
    sha = path.split("head_sha=", 1)[1].split("&", 1)[0]
    # GitHub only has runs for a commit something pushed: before the gate's
    # push this commit is nowhere on origin, so there is nothing yet.
    # Runs an earlier attempt left on this commit stay on GitHub after its
    # branch is deleted, finished and failed.
    old = [dict(r, head_sha=sha) for r in st.get("stale", [])]
    if sha not in call["refs"].values():
        save(); print(json.dumps({{"workflow_runs": old}})); sys.exit(0)
    call["gen"] = True
    st["polls"] = st.get("polls", 0) + 1
    runs = []
    for i, (wf, how) in enumerate(sorted(st.get("workflows", {{}}).items())):
        if st["polls"] < how.get("appear", 0):
            continue                  # GitHub has not registered this run yet
        concl = st.get("per_sha", {{}}).get(sha, how.get("conclusion"))
        done = how.get("after", 0) <= st["polls"] and concl != "pending"
        runs.append({{"id": 100 + i, "name": how["name"], "path": wf,
                      "head_sha": sha, "event": "push", "head_branch": st.get("branch", ""),
                      "status": "completed" if done else "in_progress",
                      "conclusion": concl if done else None, "run_attempt": 1,
                      "html_url": f"https://github.com/o/r/actions/runs/{{100 + i}}"}})
    runs += old
    save(); print(json.dumps({{"workflow_runs": runs}})); sys.exit(0)
if path.endswith("/jobs?per_page=100"):
    save()
    print(json.dumps({{"jobs": [
        {{"id": 7, "name": "lint", "conclusion": "success", "steps": []}},
        {{"id": 8, "name": "test", "conclusion": "failure",
         "html_url": "https://github.com/o/r/actions/runs/100/job/8",
         "steps": [{{"name": "Checkout", "conclusion": "success"}},
                   {{"name": "Run tests", "conclusion": "failure"}}]}}]}}))
    sys.exit(0)
if path.endswith("/jobs/8/logs"):
    save()
    if "--allow-escape-sequences" not in args:
        # What gh 2.97 says, writing a coloured log anywhere but a terminal.
        sys.stderr.write("the response contains terminal escape sequences; "
                         "pass --allow-escape-sequences to output it anyway\n")
        sys.exit(1)
    print("2026-11-21T10:00:00.0000000Z Test Case '-[CadenceTests.AuthServiceTests testUpsertHangs]' started.")
    print("2026-11-21T10:00:03.1000000Z Test Case '-[CadenceTests.AuthServiceTests testUpsertHangs]' failed (3.100 seconds).")
    print("2026-11-21T10:00:04.0000000Z \x1b[36;1mmake ios-test\x1b[0m")
    print("2026-11-21T10:00:04.0000000Z ##[error]Process completed with exit code 65.")
    sys.exit(0)
save(); sys.stderr.write(f"unexpected gh call: {{args}}\n"); sys.exit(2)
'''


def _ci_fixture(tests_branches='["**"]'):
    """A repo with a bare origin, two path-filtered workflows, and a fake gh.

    Returns a namespace: repo, origin, root, g (git), gh_state(), set_gh(),
    branch(name, files) -> worktree, tests (the local suite, always green).
    """
    import verify as V
    root = Path(tempfile.mkdtemp())
    repo, origin = root / "repo", root / "origin.git"
    def g(cwd, *a):
        return subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], capture_output=True)
    repo.mkdir()
    g(repo, "init", "-q", "-b", "main")
    g(repo, "config", "user.email", "t@t"); g(repo, "config", "user.name", "t")
    wf = repo / ".github" / "workflows"; wf.mkdir(parents=True)
    (wf / "tests.yml").write_text(
        "name: Tests\non:\n  push:\n    branches: " + tests_branches + "\n"
        "    paths: [\"ios/**\", \".github/workflows/tests.yml\"]\n"
        "jobs:\n  test:\n    runs-on: macos-26\n    steps:\n      - run: make test\n")
    (wf / "backend.yml").write_text(
        "name: Backend tests\n'on':\n  push:\n    branches: [\"**\"]\n"
        "    paths: [\"supabase/**\"]\n"
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make backend-test\n")
    (repo / "ios").mkdir(); (repo / "ios" / "a.txt").write_text("1\n")
    (repo / "README.md").write_text("r\n")
    g(repo, "add", "-A"); g(repo, "commit", "-qm", "base")
    g(repo, "remote", "add", "origin", str(origin))
    g(repo, "push", "-q", "-u", "origin", "main")
    state = root / "gh.json"
    state.write_text(json.dumps({"origin": str(origin), "workflows": {}}))
    gh = root / "gh"
    gh.write_text(_FAKE_GH.format(python=sys.executable, state=str(state)))
    gh.chmod(0o755)

    def gh_state():
        return json.loads(state.read_text())

    def set_gh(**kw):
        st = gh_state(); st.update(kw); st["calls"] = []; st["polls"] = 0
        state.write_text(json.dumps(st))

    def branch(name, files):
        wt = root / name.replace("/", "_")
        g(repo, "worktree", "add", "-q", "-b", name, str(wt), "main")
        for rel, text in files.items():
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text(text)
        g(wt, "add", "-A")
        g(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name)
        return wt

    tests = lambda cwd: V.run("true", cwd)       # noqa: E731
    return types.SimpleNamespace(root=root, repo=repo, origin=origin, g=g, gh=str(gh),
                                 gh_state=gh_state, set_gh=set_gh, branch=branch,
                                 tests=tests)


class _Clock:
    """Time that moves only when the gate sleeps: a 45-minute deadline in ms."""
    def __init__(self):
        self.t = 0.0
        self.slept = []
    def __call__(self):
        return self.t
    def sleep(self, s):
        self.slept.append(s); self.t += s


def _gate(fx, want, tid, **kw):
    import ci as C
    clock = _Clock()
    waits = []
    gate = C.Gate(want, f"silkworm/{tid}", slug="o/r", clock=clock, sleep=clock.sleep,
                  deadline_s=kw.pop("deadline_s", 600), poll_s=30,
                  on_wait=lambda commit, names: waits.append((commit, names)), **kw)
    return gate, clock, waits


def _origin_main(fx):
    return fx.g(fx.origin, "rev-parse", "refs/heads/main").stdout.strip()


def test_ci_gate_filters():
    import ci as C
    print("\nGitHub's workflow filters, as the CI gate reads them")
    check("'*' does not cross a slash, so it misses a silkworm/ branch",
          not C.matches(["*"], "silkworm/tsk_1") and C.matches(["*"], "main"))
    check("'**' matches a name with a slash",
          C.matches(["**"], "silkworm/tsk_1"))
    check("a later ! pattern excludes what an earlier one included",
          not C.matches(["**", "!silkworm/**"], "silkworm/x") and C.matches(["**", "!silkworm/**"], "main"))
    check("'ios/**' covers files at any depth under ios/",
          C.matches(["ios/**"], "ios/Cadence/App.swift") and not C.matches(["ios/**"], "supabase/x.ts"))
    check("'docs/**/*.md' covers docs/a.md too",
          C.matches(["docs/**/*.md"], "docs/a.md") and C.matches(["docs/**/*.md"], "docs/x/y.md"))
    cfg = {"branches": ["**"], "paths": ["ios/**"]}
    check("a path filter that no changed file matches does not fire",
          C.fires(cfg, "silkworm/t", ["README.md"]) == (False, "no changed file matches its paths filter"))
    check("one matching file is enough", C.fires(cfg, "silkworm/t", ["README.md", "ios/a"])[0])
    check("a change of over 300 files is still judged by its paths",
          not C.fires(cfg, "silkworm/t", [f"x{i}" for i in range(301)])[0])
    check("tags without branches is tag pushes only",
          not C.fires({"tags": ["v*"]}, "main", ["a"])[0])
    check("paths-ignore covering every file does not fire",
          not C.fires({"paths-ignore": ["docs/**"]}, "main", ["docs/a"])[0]
          and C.fires({"paths-ignore": ["docs/**"]}, "main", ["docs/a", "src/b"])[0])
    check("branches-ignore excludes by name",
          not C.fires({"branches-ignore": ["silkworm/**"]}, "silkworm/t", ["a"])[0])
    check("'on: push' as a bare string and as a list both count as a push trigger",
          C.push_trigger({"on": "push"})[0] and C.push_trigger({"true": ["push", "pull_request"]})[0]
          and not C.push_trigger({"on": {"pull_request": None}})[0])
    check("the setting: all, names, off",
          C.parse("all") == "all" and C.parse("Tests, Backend tests") == ["Tests", "Backend tests"]
          and C.parse("off") == "" and C.parse("") == "" and C.parse(["Tests"]) == ["Tests"])


def test_ci_gate_green_lands_and_main_waits_for_it():
    import merge as M
    print("\nwith a CI gate, main moves and is pushed only once CI is green")
    fx = _ci_fixture()
    fx.set_gh(workflows={".github/workflows/tests.yml": {"name": "Tests", "conclusion": "success",
                                                          "after": 3}})
    import ci as C
    C.GH = fx.gh
    try:
        before = _origin_main(fx)
        wt = fx.branch("silkworm/tsk_green", {"ios/a.txt": "2\n"})
        gate, clock, waits = _gate(fx, "all", "tsk_green")
        r = M.land(wt, fx.repo, "silkworm/tsk_green", "main", fx.tests, publish=True, ci_gate=gate)
        st = fx.gh_state()
        polls = [c for c in st["calls"] if c.get("gen")]
        check("a green run lands", r.get("landed"), f"{r.get('stage')}: {str(r.get('detail'))[:200]}")
        check("it polled until the run finished, sleeping on the injected clock",
              len(polls) == 3 and clock.slept == [30, 30])
        check("every poll saw the rebased commit on origin as silkworm/<id>",
              polls and all(c["refs"].get("refs/heads/silkworm/tsk_green") == r.get("head")
                            for c in polls))
        check("and main on origin not yet moved while CI ran",
              polls and all(c["refs"].get("refs/heads/main") == before for c in polls))
        check("main is pushed after green", _origin_main(fx) == r.get("head") and r.get("published"))
        check("the pushed silkworm/<id> branch is deleted from origin after landing",
              fx.g(fx.origin, "rev-parse", "--verify", "-q",
                   "refs/heads/silkworm/tsk_green").stdout.strip() == ""
              and (r.get("ci") or {}).get("deleted") is True and not r.get("ci_pushed"))
        check("the record says which workflows passed and which were not triggered",
              (r.get("ci") or {}).get("passed") == ["Tests"]
              and [s["name"] for s in (r.get("ci") or {}).get("skipped", [])] == ["Backend tests"])
        check("and the landing's detail says the skipped one counted as passed",
              "Backend tests (no changed file matches its paths filter)" in (r.get("detail") or "")
              and "counted as passed" in (r.get("detail") or ""), r.get("detail"))
        check("the wait was announced once, naming what it waits for",
              waits == [(r.get("head"), ["Tests"])])
        check("merge.summary names CI on the landed line",
              "CI passed: Tests" in M.summary({**r, "eligible": True}, "silkworm/tsk_green"))
    finally:
        C.GH = "gh"


def test_ci_gate_red_refuses_naming_the_failure():
    import merge as M
    import ci as C
    print("\na red CI run refuses the landing with the workflow, job and test named")
    fx = _ci_fixture()
    fx.set_gh(workflows={".github/workflows/tests.yml": {"name": "Tests", "conclusion": "failure",
                                                          "after": 1}})
    C.GH = fx.gh
    try:
        before, local = _origin_main(fx), fx.g(fx.repo, "rev-parse", "HEAD").stdout.strip()
        wt = fx.branch("silkworm/tsk_red", {"ios/a.txt": "3\n"})
        gate, _clock, _w = _gate(fx, "all", "tsk_red")
        r = M.land(wt, fx.repo, "silkworm/tsk_red", "main", fx.tests, publish=True, ci_gate=gate)
        d = r.get("detail") or ""
        check("refused at stage 'ci'", not r.get("landed") and r.get("stage") == "ci", r.get("stage"))
        check("naming the workflow, the failing job and step, and the first failing test",
              "'Tests' failed" in d and "job 'test'" in d and "step 'Run tests'" in d
              and "-[CadenceTests.AuthServiceTests testUpsertHangs]" in d, d)
        check("not the job that passed, nor the ##[error] line, when a test is named",
              "'lint'" not in d and "exit code 65" not in d)
        check("main untouched, locally and on origin",
              _origin_main(fx) == before and fx.g(fx.repo, "rev-parse", "HEAD").stdout.strip() == local)
        check("the pushed branch is cleaned up after a refusal",
              fx.g(fx.origin, "rev-parse", "--verify", "-q",
                   "refs/heads/silkworm/tsk_red").stdout.strip() == "")
        check("a red refusal is not marked retryable", not r.get("retryable"))
        check("it still needs a person", M.needs_a_person({**r, "eligible": True}))
    finally:
        C.GH = "gh"


def test_ci_gate_timeout_is_retryable():
    import merge as M
    import ci as C
    print("\nCI that does not finish by the deadline refuses as ci-timeout, and a retry resumes")
    fx = _ci_fixture()
    fx.set_gh(workflows={".github/workflows/tests.yml": {"name": "Tests", "conclusion": "pending"}})
    C.GH = fx.gh
    try:
        before = _origin_main(fx)
        wt = fx.branch("silkworm/tsk_slow", {"ios/a.txt": "4\n"})
        gate, clock, _w = _gate(fx, "all", "tsk_slow", deadline_s=300)
        r = M.land(wt, fx.repo, "silkworm/tsk_slow", "main", fx.tests, publish=True, ci_gate=gate)
        check("refused at stage 'ci-timeout'", r.get("stage") == "ci-timeout", r.get("stage"))
        check("marked retryable, and saying to land again", r.get("retryable")
              and "land again" in (r.get("detail") or ""))
        check("naming what it was still waiting for", "Tests" in (r.get("detail") or ""))
        check("it gave up at the deadline, not before and not long after",
              300 <= clock.t <= 330, clock.t)
        check("main untouched on origin", _origin_main(fx) == before)
        check("the pushed branch is kept, so a retry resumes the same runs",
              r.get("ci_pushed") == "silkworm/tsk_slow"
              and fx.g(fx.origin, "rev-parse", "--verify", "-q",
                       "refs/heads/silkworm/tsk_slow").stdout.strip() != "")
        check("merge.summary offers the retry", "Land again" in M.summary(
            {**r, "eligible": True}, "silkworm/tsk_slow"))
        # The runs finish; landing again picks them up on the same commit.
        fx.set_gh(workflows={".github/workflows/tests.yml": {"name": "Tests",
                                                              "conclusion": "success"}})
        gate2, _c, _w2 = _gate(fx, "all", "tsk_slow")
        wt2 = fx.root / "again"
        fx.g(fx.repo, "worktree", "remove", "--force", str(wt))
        fx.g(fx.repo, "worktree", "add", "-q", str(wt2), "silkworm/tsk_slow")
        r2 = M.land(wt2, fx.repo, "silkworm/tsk_slow", "main", fx.tests, publish=True, ci_gate=gate2)
        check("landing again after the timeout lands", r2.get("landed"), r2.get("stage"))
        check("and then the remote branch goes",
              fx.g(fx.origin, "rev-parse", "--verify", "-q",
                   "refs/heads/silkworm/tsk_slow").stdout.strip() == "")
        # gh unreachable throughout: a timeout too, saying why.
        fx.set_gh(down=True)
        wt3 = fx.branch("silkworm/tsk_down", {"ios/a.txt": "5\n"})
        gate3, _c3, _w3 = _gate(fx, "all", "tsk_down", deadline_s=60)
        r3 = M.land(wt3, fx.repo, "silkworm/tsk_down", "main", fx.tests, ci_gate=gate3)
        check("gh failing on every poll is a timeout that quotes gh's error",
              r3.get("stage") == "ci-timeout" and "502" in (r3.get("detail") or ""), r3.get("detail"))
    finally:
        C.GH = "gh"


def test_ci_gate_path_filtered_and_misconfigured():
    import merge as M
    import ci as C
    print("\npath-filtered workflows count as passed; untriggerable ones refuse")
    fx = _ci_fixture()
    fx.set_gh(workflows={})
    C.GH = fx.gh
    try:
        wt = fx.branch("silkworm/tsk_docs", {"README.md": "docs only\n"})
        gate, _c, waits = _gate(fx, "all", "tsk_docs")
        r = M.land(wt, fx.repo, "silkworm/tsk_docs", "main", fx.tests, publish=True, ci_gate=gate)
        check("a change no workflow's paths cover lands", r.get("landed"), r.get("stage"))
        check("without pushing a branch or asking GitHub anything",
              fx.gh_state().get("calls") == [] and not waits)
        check("and its record says both workflows were skipped, and why",
              sorted(s["name"] for s in (r.get("ci") or {}).get("skipped", []))
              == ["Backend tests", "Tests"] and "counted as passed" in (r.get("detail") or ""))

        # Named workflows: one that does not exist is a configuration problem.
        wt = fx.branch("silkworm/tsk_named", {"ios/a.txt": "6\n"})
        gate, _c, _w = _gate(fx, ["Tests", "Lint"], "tsk_named")
        r = M.land(wt, fx.repo, "silkworm/tsk_named", "main", fx.tests, ci_gate=gate)
        check("a named workflow that does not exist refuses as ci-config",
              r.get("stage") == "ci-config" and "'Lint'" in (r.get("detail") or ""), r.get("detail"))
    finally:
        C.GH = "gh"

    # Cadence's own tests.yml, branches: ["*"], which misses silkworm/<id>:
    # passing it would be the after-the-fact red all over again.
    fx = _ci_fixture(tests_branches='["*"]')
    fx.set_gh(workflows={})
    C.GH = fx.gh
    try:
        before = fx.g(fx.repo, "rev-parse", "HEAD").stdout.strip()
        wt = fx.branch("silkworm/tsk_star", {"ios/a.txt": "7\n"})
        gate, _c, _w = _gate(fx, "all", "tsk_star")
        r = M.land(wt, fx.repo, "silkworm/tsk_star", "main", fx.tests, ci_gate=gate)
        check("a workflow that would run on main but not on the pushed branch refuses",
              r.get("stage") == "ci-config" and "'Tests'" in (r.get("detail") or "")
              and '"**"' in (r.get("detail") or ""), r.get("detail"))
        check("and moves nothing", fx.g(fx.repo, "rev-parse", "HEAD").stdout.strip() == before
              and fx.gh_state().get("calls") == [])
        # The branch that fixes the trigger is judged by the trigger it fixes.
        fx.set_gh(workflows={".github/workflows/tests.yml": {"name": "Tests",
                                                              "conclusion": "success"}})
        yml = (fx.repo / ".github/workflows/tests.yml").read_text().replace('["*"]', '["**"]')
        wt = fx.branch("silkworm/tsk_fix", {".github/workflows/tests.yml": yml})
        gate, _c, _w = _gate(fx, "all", "tsk_fix")
        r = M.land(wt, fx.repo, "silkworm/tsk_fix", "main", fx.tests, ci_gate=gate)
        check("a branch fixing the trigger is judged by its own workflow file, and lands",
              r.get("landed") and (r.get("ci") or {}).get("passed") == ["Tests"], r.get("detail"))
    finally:
        C.GH = "gh"

    # Never the base: the gate refuses to push anything but silkworm/<id>.
    gate = C.Gate("all", "main", slug="o/r")
    out = gate.run(fx.repo, fx.repo, "main", "x", "y")
    check("the gate refuses to push the base as its CI branch",
          not out["ok"] and out["stage"] == "ci-config" and not gate.pushed)
    check("delete_remote refuses anything outside silkworm/",
          C.delete_remote(fx.repo, "main") is False)


def test_ci_gate_releases_the_checkout_while_waiting():
    import merge as M
    import ci as C
    import threading as _th
    print("\nthe CI wait lets go of the checkout and checks it again afterwards")
    fx = _ci_fixture()
    fx.set_gh(workflows={".github/workflows/tests.yml": {"name": "Tests", "conclusion": "success",
                                                          "after": 2}})
    C.GH = fx.gh
    try:
        lock = _th.Lock()
        held_during = []

        @contextlib.contextmanager
        def outside():
            lock.release()
            try:
                yield
            finally:
                lock.acquire()
        wt = fx.branch("silkworm/tsk_out", {"ios/a.txt": "8\n"})
        gate, clock, _w = _gate(fx, "all", "tsk_out", outside=outside)
        real_sleep = clock.sleep

        def sleep(s):
            held_during.append(lock.locked())
            # Someone edits a tracked file in the checkout meanwhile.
            (fx.repo / "README.md").write_text("edited while CI ran\n")
            real_sleep(s)
        gate.sleep = sleep
        with lock:
            r = M.land(wt, fx.repo, "silkworm/tsk_out", "main", fx.tests, ci_gate=gate)
        check("the checkout's lock is free while CI is polled", held_during == [False])
        check("and the checkout is asked again afterwards: edits made meanwhile refuse",
              r.get("stage") == "base-dirty" and not r.get("landed"), r.get("stage"))
        check("that refusal still cleans the remote branch up",
              fx.g(fx.origin, "rev-parse", "--verify", "-q",
                   "refs/heads/silkworm/tsk_out").stdout.strip() == "")
    finally:
        C.GH = "gh"


def test_ci_gate_wiring_in_the_bot():
    import ci as C
    print("\nthe bot builds the gate from the project, records the wait, and cleans up on drop")
    recorded = []
    task = {"id": "tsk_wire", "project": "cadence"}
    ns = lift("ci_gate_for", _repo_lock=lambda cwd: None,
              record_landing=lambda t, o: recorded.append(o),
              branches=types.SimpleNamespace(name_for=lambda t: "silkworm/tsk_wire"))
    check("no ci_gate, no gate", ns["ci_gate_for"](task, {"ci_gate": ""}, "/x") is None)
    gate = ns["ci_gate_for"](task, {"ci_gate": "all"}, "/x")
    check("ci_gate 'all' builds a gate pushing the task's own branch",
          isinstance(gate, C.Gate) and gate.want == "all" and gate.push_as == "silkworm/tsk_wire")
    gate.on_wait("abcdef1234", ["Tests"])
    check("the wait is recorded as its own landing stage, with the remote branch",
          recorded and recorded[-1]["stage"] == C.WAITING
          and recorded[-1].get("ci_pushed") == "silkworm/tsk_wire"
          and "waiting for Tests" in recorded[-1].get("detail", ""))
    src = (BASE / "bot.py").read_text()
    body = src[src.index("def land_if_ready("):src.index("def record_landing(")]
    check("land_if_ready passes the gate to merge.land, inside the landing serial lock",
          "ci_gate=gate" in body and "with landing_serial(cwd):\n            with repo_guard(cwd):" in body)
    keep = src[src.index("def record_landing("):src.index("def land_and_record(")]
    check("the landing record keeps the CI fields", all(f'"{k}"' in keep for k in
                                                       ("ci", "ci_pushed", "retryable")))
    rr = src[src.index("def resolve_review("):src.index("def landing_enabled(")]
    check("a CI-gated auto-merge lands on its own thread, not the review lane",
          "land_later" in rr and "start_landing(parent_id)" in rr)
    cl = src[src.index("def clear_interrupted_landings("):src.index("_email_lock")]
    check("a restart while waiting for CI is cleared like any interrupted landing",
          "ci.WAITING" in cl)
    drop = src[src.index("def land_or_drop("):src.index("def tidy_checkout(")] \
        if "def land_or_drop(" in src else ""
    check("dropping a task deletes the branch its CI gate pushed",
          "ci.delete_remote(repo, pushed)" in drop)
    import projects as P_
    check("cadence is seeded on, nothing else", P_.SEED_CI_GATE == {"cadence": "all"}
          and P_.default("ci_gate") == "")
    td = Path(tempfile.mkdtemp())
    (td / "p.json").write_text(json.dumps({"cadence": {"slug": "cadence"},
                                           "trader": {"slug": "trader"}}))
    ps = P_.ProjectStore(td / "p.json")
    check("an existing cadence record gets ci_gate on, others stay off",
          ps.get("cadence")["ci_gate"] == "all" and ps.get("trader")["ci_gate"] == "")
    (td / "q.json").write_text(json.dumps({"cadence": {"slug": "cadence", "ci_gate": ""}}))
    check("turned off, it stays off", P_.ProjectStore(td / "q.json").get("cadence")["ci_gate"] == "")


def test_ci_wait_is_its_own_stage_everywhere():
    import digest as Dg
    import merge as M
    import ci as C
    print("\n'waiting for CI' shows as its own landing stage")
    now = time.time()
    rec = {"id": "tsk_w", "title": "Waiting one", "project": "cadence", "state": "done",
           "role": "implementor", "created": now - 100,
           "result": {"landing": {"eligible": True, "landed": False, "stage": C.WAITING,
                                  "branch": "silkworm/tsk_w", "detail": "waiting for Tests",
                                  "at": now - 50}},
           "events": [{"kind": "done", "at": now - 60}]}
    out = Dg.sections([rec], now)
    check("the digest lists it under 'waiting for CI'",
          "Waiting one" in str(out.get("cadence", {}).get("ci")), out)
    check("and not as a refused landing", not out.get("cadence", {}).get("refused"))
    check("the digest's sections include it, with its label",
          ("ci", "waiting for CI", True) in Dg.ORDER)
    check("merge.summary says waiting for CI",
          "Waiting for CI" in M.summary({"eligible": True, "landed": False,
                                         "stage": C.WAITING, "detail": "x"}, "b"))
    check("ci.label names every CI stage", C.label(C.WAITING) == "waiting for CI"
          and C.label(C.TIMEOUT) == "CI timed out")
    cli = (BASE / "bin" / "silkworm").read_text()
    check("the CLI labels the stages the same way",
          '"ci-wait": "waiting for CI"' in cli and "landing_label(stage)" in cli)
    page = (BASE / "visualizer.py").read_text()
    check("the dashboard shows it as waiting for CI, not landing… or refused",
          'l.stage === "ci-wait"' in page and "waiting for CI…" in page
          and page.index('l.stage === "ci-wait"') < page.index('if (!l.eligible)'))


def test_ci_gate_branch_is_cleaned_up_on_drop_and_restart():
    import tasks as T, branches as B, discard as D
    print("\na dropped task takes its CI branch off origin; a restart clears the wait")
    fx = _ci_fixture()
    wt = fx.branch("silkworm/tsk_drop", {"ios/a.txt": "9\n"})
    fx.g(wt, "push", "-q", "origin", "silkworm/tsk_drop")
    fx.g(fx.repo, "worktree", "remove", "--force", str(wt))
    store = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    recorded = []
    ns = {"task_store": store, "tasks": T, "branches": B, "discard": D,
          "log": logging.getLogger("test"),
          "start_landing": lambda tid, approved=False: True,
          "record_landing": lambda task, out: recorded.append(out),
          "repo_guard": lambda cwd: contextlib.nullcontext()}
    lift({"land_or_drop"}, ns=ns)
    tid = store.create("gated work", state=T.QUEUED, scope={"cwd": str(fx.repo)},
                       role="implementor", branch="silkworm/tsk_drop")["id"]
    store.transition(tid, T.RUNNING); store.transition(tid, T.DONE)
    store.update(tid, result={"landing": {"eligible": True, "landed": False,
                                          "stage": "ci-timeout", "branch": "silkworm/tsk_drop",
                                          "ci_pushed": "silkworm/tsk_drop"}})
    on_origin = lambda: fx.g(fx.origin, "rev-parse", "--verify", "-q",   # noqa: E731
                             "refs/heads/silkworm/tsk_drop").stdout.strip()
    check("the fixture's CI branch is on origin to begin with", on_origin() != "")
    r = ns["land_or_drop"]("drop", {"id": tid})
    check("Drop deletes the branch the CI gate pushed from origin",
          r.get("ok") and on_origin() == "" and "deleted silkworm/tsk_drop from origin"
          in (r.get("note") or ""), r)
    # A task whose landing never pushed anything leaves origin alone.
    other = fx.branch("silkworm/tsk_keep", {"README.md": "k\n"})
    fx.g(other, "push", "-q", "origin", "silkworm/tsk_keep")
    fx.g(fx.repo, "worktree", "remove", "--force", str(other))
    tid2 = store.create("ungated", state=T.QUEUED, scope={"cwd": str(fx.repo)},
                        role="implementor", branch="silkworm/tsk_keep")["id"]
    store.transition(tid2, T.RUNNING); store.transition(tid2, T.DONE)
    ns["land_or_drop"]("drop", {"id": tid2})
    check("a branch the gate did not push is not deleted from origin",
          fx.g(fx.origin, "rev-parse", "--verify", "-q",
               "refs/heads/silkworm/tsk_keep").stdout.strip() != "")

    import subprocess as _sp
    sweep_ns = {"task_store": store, "log": logging.getLogger("test"), "subprocess": _sp,
                "time": time}
    lift({"record_landing", "clear_interrupted_landings", "LANDING_UNDERWAY",
          "_interrupted_landing_detail"}, ns=sweep_ns)
    w = store.create("waiting", project="cadence")["id"]
    store.update(w, result={"landing": {"eligible": True, "landed": False, "stage": "ci-wait",
                                        "branch": "silkworm/" + w, "checkpointed": True,
                                        "ci_pushed": "silkworm/" + w, "detail": "waiting"}})
    cleared = sweep_ns["clear_interrupted_landings"]()
    after = store.get(w)["result"]["landing"]
    check("a restart while waiting for CI is cleared, as nothing merged",
          w in cleared and after["stage"] == "interrupted"
          and after["detail"].endswith("nothing was merged"), after)
    check("keeping the CI branch's name so Drop can still remove it",
          after.get("ci_pushed") == "silkworm/" + w)


def test_ci_gate_retries_and_moving_parts():
    import merge as M
    import ci as C
    print("\nCI gate: retries see fresh runs, a moved branch refuses, the base's red is named")
    log = ("﻿2026-11-21T21:20:30.1Z \x1b[36;1mmake ios-test\x1b[0m\n"
           "2026-11-21T21:20:38.2Z Failing tests:\n"
           "2026-11-21T21:20:38.3Z \tNotificationSettingsViewModelTests.test_save()\n"
           "2026-11-21T21:20:38.3Z \n"
           "2026-11-21T21:20:38.4Z ** TEST FAILED **\n"
           "2026-11-21T21:20:44.0Z ##[error]Process completed with exit code 2.\n")
    check("xcodebuild's quiet 'Failing tests:' block names the test, past ANSI and stamps",
          C.first_test(log) == "NotificationSettingsViewModelTests.test_save()", C.first_test(log))
    check("with no test named, the ##[error] line stands in",
          C.first_test("2026-11-21T1:1:1Z ##[error]boom\n") == "boom")
    src = (BASE / "ci.py").read_text()
    check("job logs are asked for with --allow-escape-sequences, or gh refuses them",
          '"--allow-escape-sequences"' in src)

    fx = _ci_fixture()
    C.GH = fx.gh
    try:
        # A flaky red from an earlier attempt is already on this commit.
        stale = {"id": 50, "name": "Tests", "path": ".github/workflows/tests.yml",
                 "event": "push", "status": "completed", "conclusion": "failure",
                 "run_attempt": 1, "html_url": "u"}
        # And the retry's own run is not registered on the first poll, as on
        # GitHub, where it takes seconds after the push to appear.
        fx.set_gh(workflows={".github/workflows/tests.yml": {"name": "Tests",
                                                              "conclusion": "success",
                                                              "appear": 2, "after": 3}},
                  stale=[stale])
        wt = fx.branch("silkworm/tsk_flaky", {"ios/a.txt": "10\n"})
        gate, _c, _w = _gate(fx, "all", "tsk_flaky")
        r = M.land(wt, fx.repo, "silkworm/tsk_flaky", "main", fx.tests, ci_gate=gate)
        check("an earlier attempt's failed run does not refuse the retry; the new run decides",
              r.get("landed"), f"{r.get('stage')}: {str(r.get('detail'))[:160]}")
        check("a stale failed run re-run on GitHub counts again",
              C.verdict([dict(stale, status="in_progress", conclusion=None, run_attempt=2)],
                        frozenset({C._seen(stale)}))[0] == "pending")

        # The base is red on GitHub too: said with the refusal.
        fx.g(fx.repo, "push", "-q", "origin", "main")
        fx.set_gh(workflows={".github/workflows/tests.yml": {"name": "Tests",
                                                              "conclusion": "failure"}})
        wt = fx.branch("silkworm/tsk_both", {"ios/a.txt": "11\n"})
        gate, _c, _w = _gate(fx, "all", "tsk_both")
        r = M.land(wt, fx.repo, "silkworm/tsk_both", "main", fx.tests, ci_gate=gate)
        check("a failure that is red on the base as well says so",
              r.get("stage") == "ci" and "red on main" in (r.get("detail") or "")
              and (r.get("ci") or {}).get("base_red"), r.get("detail"))
        before = fx.g(fx.repo, "rev-parse", "HEAD").stdout.strip()
        fx.set_gh(workflows={".github/workflows/tests.yml": {"name": "Tests",
                                                              "conclusion": "failure"}},
                  per_sha={before: "success"})
        wt = fx.branch("silkworm/tsk_mine", {"ios/a.txt": "12\n"})
        gate, _c, _w = _gate(fx, "all", "tsk_mine")
        r = M.land(wt, fx.repo, "silkworm/tsk_mine", "main", fx.tests, ci_gate=gate)
        check("and one green on the base does not",
              r.get("stage") == "ci" and "red on main" not in (r.get("detail") or ""), r.get("detail"))

        # A copy left on origin at another commit is deleted before the push.
        fx.set_gh(workflows={".github/workflows/tests.yml": {"name": "Tests",
                                                              "conclusion": "success"}},
                  per_sha={})
        wt = fx.branch("silkworm/tsk_old", {"ios/a.txt": "13\n"})
        fx.g(wt, "push", "-q", "origin", f"{before}:refs/heads/silkworm/tsk_old")
        deleted = []
        real = C.delete_remote
        C.delete_remote = lambda repo, b: deleted.append(
            (b, C.remote_sha(repo, b))) or real(repo, b)
        try:
            gate, _c, _w = _gate(fx, "all", "tsk_old")
            r = M.land(wt, fx.repo, "silkworm/tsk_old", "main", fx.tests, ci_gate=gate)
        finally:
            C.delete_remote = real
        check("an earlier attempt's copy at another commit is deleted before pushing",
              r.get("landed") and deleted[:1] == [("silkworm/tsk_old", before)], deleted)

        # The branch moves while CI runs: what passed is not what would merge.
        wt = fx.branch("silkworm/tsk_moved", {"ios/a.txt": "14\n"})
        fx.set_gh(workflows={".github/workflows/tests.yml": {"name": "Tests",
                                                              "conclusion": "success", "after": 2}})
        gate, clock, _w = _gate(fx, "all", "tsk_moved")
        base_now = fx.g(fx.repo, "rev-parse", "HEAD").stdout.strip()

        def sleep(s):
            fx.g(fx.repo, "update-ref", "refs/heads/silkworm/tsk_moved", base_now)
            clock.t += s
        gate.sleep = sleep
        r = M.land(wt, fx.repo, "silkworm/tsk_moved", "main", fx.tests, ci_gate=gate)
        check("a branch moved while CI ran refuses rather than merging untested work",
              r.get("stage") == "branch-moved"
              and fx.g(fx.repo, "rev-parse", "HEAD").stdout.strip() == base_now, r.get("stage"))
    finally:
        C.GH = "gh"


def test_ci_gate_remembers_its_remote_branch():
    import tasks as T
    import ci as C
    print("\nthe branch a CI gate left on origin is remembered until it is deleted")
    store = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    ns = lift("record_landing", task_store=store)
    tid = store.create("gated", project="cadence")["id"]
    rec = lambda o: ns["record_landing"](store.get(tid), o)   # noqa: E731
    rec({"eligible": True, "landed": False, "stage": "ci-timeout", "ci_pushed": "silkworm/" + tid})
    rec({"eligible": True, "landed": False, "stage": "in-progress", "detail": "landing under way"})
    got = store.get(tid)["result"]["landing"]
    check("an outcome that does not mention it keeps the pushed branch",
          got.get("stage") == "in-progress" and got.get("ci_pushed") == "silkworm/" + tid, got)
    rec({"eligible": True, "landed": True, "stage": "done", "ci_pushed": ""})
    check("one that says it was deleted clears it",
          store.get(tid)["result"]["landing"].get("ci_pushed") == "")

    task = {"id": "tsk_seed", "result": {"landing": {"ci_pushed": "silkworm/tsk_seed"}}}
    ns2 = lift("ci_gate_for", _repo_lock=lambda cwd: None, record_landing=lambda t, o: None,
               branches=types.SimpleNamespace(name_for=lambda t: "silkworm/" + t["id"]))
    gate = ns2["ci_gate_for"](task, {"ci_gate": "all"}, "/x")
    check("the next landing's gate starts out owning that branch, so it cleans it up",
          gate.pushed == "silkworm/tsk_seed")
    other = ns2["ci_gate_for"]({"id": "tsk_o", "result": {"landing": {"ci_pushed": "silkworm/zzz"}}},
                               {"ci_gate": "all"}, "/x")
    check("but never a branch with another task's name", other.pushed == "")

    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    done = st.create("landed already", state=T.QUEUED)["id"]
    st.transition(done, T.RUNNING); st.transition(done, T.DONE)
    keep = lift("live_worktree_tasks", tasks=T, task_store=st, RUNNING_TASKS={},
                _landing_now={done})["live_worktree_tasks"]()
    check("a landing under way keeps its checkout from the sweeper, task done or not",
          done in keep)

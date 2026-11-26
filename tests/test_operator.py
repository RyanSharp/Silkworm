"""The operator: policy in code, and acting on the user's word.

Run by tests/run.py with every other tests/test_*.py; see tests/harness.py."""

from harness import *  # noqa: F401,F403


# --- the operator: a head agent held to a policy in code, not in its prompt -------
# The operator manages the board, routes work and deploys. What it may not do --
# accept, release, publish where publish is off, change trader's live config,
# delete anything not proven redundant -- is refused by the CLI and the bot,
# and lifted for one item only by an approval a person sends from Slack.

def _op_git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


def _op_repo():
    """A repo on main with branches cut from the same commit:

    `silkworm/tsk_same` -- two commits main then took as one squashed commit,
    after an unrelated change above them, as a rebase-and-squash landing
    does: new shas and different patches, so `git cherry` still lists both
    as unmerged though main holds every line; `silkworm/tsk_new` -- a change
    main has never seen; `silkworm/tsk_live` -- one that touches trader's
    live configuration; `silkworm/tsk_tight` -- main took its change, but
    edited the line right next to it too, which no merge can tell apart from
    a conflict.
    """
    repo = Path(tempfile.mkdtemp()) / "trader"
    repo.mkdir()
    for k in ("SILKWORM_TASK_ROLE", "SILKWORM_TASK_ID"):
        os.environ.pop(k, None)

    def edit(n, text):
        lines = (repo / "app.py").read_text().splitlines(True)
        lines[n - 1] = text + "\n"
        (repo / "app.py").write_text("".join(lines))

    _op_git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("".join(f"line {i}\n" for i in range(1, 41)))
    (repo / "scripts").mkdir()
    (repo / "scripts" / "paper_trade.py").write_text("LEVERAGE = 1.0\n")
    _op_git(repo, "add", "-A"); _op_git(repo, "commit", "-qm", "base")
    _op_git(repo, "branch", "fork")
    _op_git(repo, "checkout", "-qb", "silkworm/tsk_same")
    edit(20, "line 20 fixed"); _op_git(repo, "commit", "-qam", "fix line 20")
    edit(25, "line 25 fixed"); _op_git(repo, "commit", "-qam", "fix line 25")
    _op_git(repo, "checkout", "-qb", "silkworm/tsk_tight", "fork")
    edit(32, "line 32 fixed"); _op_git(repo, "commit", "-qam", "fix line 32")
    _op_git(repo, "checkout", "-q", "main")
    edit(2, "line 2 tidied"); _op_git(repo, "commit", "-qam", "tidy line 2")
    edit(20, "line 20 fixed"); edit(25, "line 25 fixed")
    _op_git(repo, "commit", "-qam", "fix lines 20 and 25 (squashed)")
    edit(31, "line 31 tidied"); edit(32, "line 32 fixed")
    _op_git(repo, "commit", "-qam", "fix line 32, and 31 beside it")
    _op_git(repo, "checkout", "-qb", "silkworm/tsk_new", "fork")
    (repo / "new.py").write_text("print('new')\n")
    _op_git(repo, "add", "-A"); _op_git(repo, "commit", "-qm", "new work")
    _op_git(repo, "checkout", "-qb", "silkworm/tsk_live", "fork")
    (repo / "scripts" / "paper_trade.py").write_text("LEVERAGE = 1.5\n")
    _op_git(repo, "commit", "-qam", "lever up")
    _op_git(repo, "checkout", "-q", "main")
    return repo


def test_operator_contained_detects_rebased_equivalents():
    import operator_role as O
    print("\nthe operator's drop rule: contained by content, rebased equivalents included")
    repo = _op_repo()
    cherry = _op_git(repo, "cherry", "main", "silkworm/tsk_same")
    check("the fixture is the hard case: git cherry still calls both commits unmerged",
          [ln[0] for ln in cherry.splitlines()] == ["+", "+"], cherry)
    ok, why = O.contained(repo, "silkworm/tsk_same", "main")
    check("a branch whose change main took squashed and rebased is contained", ok, why)
    ok, why = O.contained(repo, "silkworm/tsk_tight", "main")
    check("one whose neighbouring line main also changed is not proven, so it asks",
          not ok, why)
    ok, why = O.contained(repo, "silkworm/tsk_new", "main")
    check("a branch with a change main lacks is not", not ok, why)
    ok, why = O.contained(repo, "silkworm/tsk_gone", "main")
    check("a branch that does not exist is not proven anything", not ok, why)
    _op_git(repo, "branch", "silkworm/tsk_empty", "fork")
    ok, why = O.contained(repo, "silkworm/tsk_empty", "main")
    check("a branch with nothing of its own is contained", ok, why)
    hit = O.live_paths_touched(repo, "silkworm/tsk_live", "main", O.LIVE_CONFIG["trader"])
    check("a branch changing paper_trade.py touches trader's live config",
          hit == ["scripts/paper_trade.py"], str(hit))
    check("one that does not, does not",
          O.live_paths_touched(repo, "silkworm/tsk_new", "main", O.LIVE_CONFIG["trader"]) == [])
    check("git failing to say counts as touching it (fails closed)",
          O.live_paths_touched(repo, "silkworm/nope", "main", ("ops/",)) == ["ops/"])


def test_operator_accept_is_refused_until_approved_once():
    import tasks as T
    print("\nthe operator: accepting a proposal is refused, until you approve that one, once")
    bot = _operator_bot()
    st = bot.store
    a = st.create("Add an appearance preference to Settings", state=T.PROPOSED,
                  role="implementor", project="alpha")["id"]
    b = st.create("Cache the release plan per checkout", state=T.PROPOSED,
                  role="implementor", project="alpha")["id"]
    r = _op_cli(bot, "accept", a)
    check("the CLI refuses an operator's accept, exit 1, naming the approval",
          r.returncode == 1 and f"!approve accept {a}" in r.stderr
          and st.get(a)["state"] == T.PROPOSED, r.stdout + r.stderr)
    dead = types.SimpleNamespace(port=1, statefile=bot.statefile)
    r = _op_cli(dead, "accept", a)
    check("the CLI refuses it itself, before asking any bot (1, not 3 unreachable)",
          r.returncode == 1 and f"!approve accept {a}" in r.stderr, r.stdout + r.stderr)
    # The bot refuses it too, for a request that never went through this CLI.
    out = bot.ns["handle_tasks"]({"action": "accept", "id": a, "caller_role": "operator"})
    check("the bot refuses it on its own",
          not out["ok"] and out.get("refused") == "accept"
          and st.get(a)["state"] == T.PROPOSED, str(out))
    x = st.create("Show the sync error text in Settings", state=T.PROPOSED,
                  role="implementor", project="alpha")["id"]
    out = bot.ns["handle_tasks"]({"action": "accept", "id": x, "caller_thread": "D9:9.9"})
    check("a request from another thread with no role is not the operator's",
          out["ok"] and st.get(x)["state"] == T.QUEUED, str(out))
    bot.state.set_thread("D7:7.0")
    out = bot.ns["handle_tasks"]({"action": "accept", "id": a, "caller_thread": "D7:7.0"})
    check("its home thread alone is enough to be held to the policy",
          not out["ok"] and out.get("refused") == "accept", str(out))

    bot.state.approve("accept", b, by="U_ME")
    r = _op_cli(bot, "accept", a)
    check("an approval of a different item lifts nothing",
          r.returncode == 1 and st.get(a)["state"] == T.PROPOSED, r.stderr)
    bot.state.approve("accept", a, by="U_ME")
    r = _op_cli(bot, "accept", a)
    check("approved, it goes ahead, logged as the operator's",
          r.returncode == 0 and st.get(a)["state"] == T.QUEUED
          and "via operator" in st.get(a)["events"][-1]["detail"], r.stdout + r.stderr)
    check("and the approval is used up", not bot.state.has("accept", a))
    c = st.create("Retry the sync engine on 5xx", state=T.PROPOSED,
                  role="implementor", project="alpha")["id"]
    bot.state.approve("accept", c, by="U_ME", now=time.time() - O_TTL() - 1)
    r = _op_cli(bot, "accept", c)
    check("an approval older than a day has lapsed",
          r.returncode == 1 and st.get(c)["state"] == T.PROPOSED, r.stderr)

    r = _op_cli(bot, "accept", b, role="", thread="")
    check("the same command from you is unaffected",
          r.returncode == 0 and st.get(b)["state"] == T.QUEUED, r.stderr)

    try:
        out = bot.ns["handle_tasks"]({"action": "create", "goal": "Fix the drawdown halt marker "
                                      "so restarts keep it armed", "caller_role": "operator"})
    except Exception as e:          # got past the guard into the filing itself
        out = {"ok": True, "raised": repr(e)}
    check("filing straight into the queue is accepting, and refused",
          not out["ok"] and out.get("refused") == "accept", str(out))
    w = st.create("Tidy the sync retry loop in SyncEngine", role="implementor")["id"]
    st.transition(w, T.RUNNING); st.transition(w, T.AWAITING_APPROVAL)
    out = bot.ns["handle_tasks"]({"action": "approve", "id": w, "caller_role": "operator"})
    check("approving flagged work is the user's gate too",
          not out["ok"] and st.get(w)["state"] == T.AWAITING_APPROVAL, str(out))


def O_TTL():
    import operator_role
    return operator_role.APPROVAL_TTL_S


def test_operator_release_is_refused_until_approved():
    import tasks as T
    print("\nthe operator: releases are refused, until you approve that one")
    bot = _operator_bot()
    st = bot.store
    t = st.create("Cut a TestFlight build", role="implementor", project="cadence")["id"]
    st.transition(t, T.RUNNING); st.transition(t, T.NEEDS_INPUT)
    r = _op_cli(bot, "accept", t)            # warm-up: the CLI is up
    out = bot.ns["handle_tasks"]({"action": "release", "id": t, "caller_role": "operator"})
    check("taking a release step is refused", not out["ok"] and out.get("refused") == "release"
          and not bot.steps, str(out))
    bot.state.approve("release", t, by="U_ME")
    out = bot.ns["handle_tasks"]({"action": "release", "id": t, "caller_role": "operator"})
    check("approved, the release step is taken", out["ok"] and bot.steps == [t], str(out))
    out = bot.ns["handle_tasks"]({"action": "release", "id": t, "caller_role": "operator"})
    check("once", not out["ok"] and bot.steps == [t], str(out))
    # The /releases route asks the same guard.
    import operator_role as O
    check("/releases start is guarded, per project",
          O.releases_guard("start", {"slug": "Cadence"}) == ("release", "cadence"))
    check("its read-only plan is not", O.releases_guard("plan", {"slug": "cadence"}) is None)


def test_operator_publish_and_trader_settings_are_refused():
    import operator_role as O
    print("\nthe operator: publishing where it is off, and trader's settings, are refused")
    tmp = Path(tempfile.mkdtemp())
    state = O.State(tmp / "operator.json")
    refused = lift("operator_refused", operator_role=O, OPERATOR=state,
                            task_store=types.SimpleNamespace(get=lambda tid: None),
                   absent=('task_contained', 'task_live_paths'))["operator_refused"]
    me = {"caller_role": "operator"}
    out = refused("projects", "publish", {**me, "slug": "cadence", "on": True})
    check("switching publish on is refused", out and out["refused"] == "publish", str(out))
    check("switching it off is not", refused("projects", "publish",
                                             {**me, "slug": "cadence", "on": False}) is None)
    for action in ("auto-merge", "test-cmd", "base", "generated", "ideate", "brief", "archive"):
        out = refused("projects", action, {**me, "slug": "trader"})
        check(f"trader {action} is its live configuration, and refused",
              out and out["refused"] == "trader-config", str(out))
    check("reading trader is not", refused("projects", "get", {**me, "slug": "trader"}) is None)
    check("another project's settings are not",
          refused("projects", "auto-merge", {**me, "slug": "cadence"}) is None)
    check("and none of it applies to you",
          refused("projects", "publish", {"slug": "cadence", "on": True}) is None)
    state.approve("publish", "cadence", by="U_ME")
    check("approved, publish goes through once",
          refused("projects", "publish", {**me, "slug": "cadence", "on": True}) is None
          and refused("projects", "publish", {**me, "slug": "cadence", "on": True}))
    state.approve("trader-config", "trader", by="U_ME")
    check("approved, a trader setting goes through once",
          refused("projects", "auto-merge", {**me, "slug": "trader"}) is None
          and refused("projects", "auto-merge", {**me, "slug": "trader"}))
    # Every route the policy covers asks it first, before reading or changing.
    src = (BASE / "bot.py").read_text()
    tree = ast.parse(src)
    for fn, route in (("handle_tasks", "tasks"), ("handle_projects", "projects"),
                      ("handle_releases", "releases")):
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == fn)
        first_call = next((c for st_ in node.body[:4] for c in ast.walk(st_)
                           if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "operator_refused"),
                          None)
        check(f"{fn} asks operator_refused({route!r}, …) before anything else",
              first_call is not None and isinstance(first_call.args[0], ast.Constant)
              and first_call.args[0].value == route)
    import git_guard
    check("git refuses the operator's own pushes and ref moves (git_guard)",
          "operator" in git_guard.GUARDED_ROLES and "operator" in git_guard.PRE_PUSH)


def test_operator_drop_and_trader_landing_need_proof_or_approval():
    import tasks as T
    print("\nthe operator: it drops only what main holds, and lands nothing touching trader's live config")
    repo = _op_repo()
    bot = _operator_bot()
    st = bot.store

    def finished(tid_name, project="trader"):
        t = st.create(f"Work for {tid_name}", role="implementor", project=project,
                      scope={"cwd": str(repo), "repo": str(repo)})
        st.update(t["id"], branch=f"silkworm/{tid_name}")
        st.transition(t["id"], T.RUNNING); st.transition(t["id"], T.DONE)
        return t["id"]

    same, new, live = finished("tsk_same"), finished("tsk_new"), finished("tsk_live")
    r = _op_cli(bot, "drop", new)
    check("dropping a branch with work main lacks is refused, exit 1",
          r.returncode == 1 and f"!approve delete {new}" in r.stderr, r.stdout + r.stderr)
    r = _op_cli(bot, "drop", same)
    check("dropping one main took by rebase goes ahead", r.returncode == 0, r.stdout + r.stderr)
    r = _op_cli(bot, "operator", "contained", new)
    check("`operator contained` says why not, exit 1",
          r.returncode == 1 and "not contained" in r.stdout, r.stdout + r.stderr)
    r = _op_cli(bot, "operator", "contained", same)
    check("and proves the rebased one, exit 0",
          r.returncode == 0 and r.stdout.startswith("contained"), r.stdout + r.stderr)
    bot.state.approve("delete", new, by="U_ME")
    r = _op_cli(bot, "drop", new)
    check("approved, the drop goes ahead once", r.returncode == 0, r.stdout + r.stderr)

    r = _op_cli(bot, "land", live)
    check("landing a trader branch that changes paper_trade.py is refused",
          r.returncode == 1 and "trader-config" in r.stderr and live not in bot.landings,
          r.stdout + r.stderr)
    r = _op_cli(bot, "land", new)
    check("a trader branch that does not touch it lands", r.returncode == 0
          and new in bot.landings, r.stdout + r.stderr)
    r = _op_cli(bot, "land", live, role="", thread="")
    check("you can land it yourself", r.returncode == 0 and live in bot.landings, r.stderr)


def test_operator_approve_accept_does_not_land_trader_live_config():
    import tasks as T
    print("\nthe operator: an `!approve accept` of trader work does not land its live config")
    repo = _op_repo()
    bot = _operator_bot()
    st = bot.store

    def flagged(tid_name):
        t = st.create(f"Work for {tid_name}", role="implementor", project="trader",
                      scope={"cwd": str(repo), "repo": str(repo)})
        st.update(t["id"], branch=f"silkworm/{tid_name}")
        st.transition(t["id"], T.RUNNING); st.transition(t["id"], T.AWAITING_APPROVAL)
        return t["id"]

    live, new = flagged("tsk_live"), flagged("tsk_new")
    bot.state.approve("accept", live, by="U_ME")
    r = _op_cli(bot, "approve", live)
    check("approving reviewed trader work that changes paper_trade.py is refused, "
          "though accept was approved",
          r.returncode == 1 and "trader-config" in r.stderr and live not in bot.landings
          and st.get(live)["state"] == T.AWAITING_APPROVAL, r.stdout + r.stderr)
    check("and that accept approval is left unspent", bot.state.has("accept", live))
    bot.state.approve("accept", new, by="U_ME")
    r = _op_cli(bot, "approve", new)
    check("approving trader work that does not touch it still goes on an accept approval",
          r.returncode == 0 and new in bot.landings, r.stdout + r.stderr)
    bot.state.approve("trader-config", live, by="U_ME")
    r = _op_cli(bot, "approve", live)
    check("an !approve trader-config of it lands it, once",
          r.returncode == 0 and live in bot.landings
          and not bot.state.has("trader-config", live), r.stdout + r.stderr)
    cancelled = st.create("Work for tsk_live, dismissed", role="implementor", project="trader",
                          scope={"cwd": str(repo), "repo": str(repo)})["id"]
    st.update(cancelled, branch="silkworm/tsk_live")
    st.transition(cancelled, T.RUNNING); st.transition(cancelled, T.CANCELLED)
    bot.state.approve("accept", cancelled, by="U_ME")
    r = _op_cli(bot, "land", cancelled)
    check("nor does an accept approval land dismissed work that touches it",
          r.returncode == 1 and "trader-config" in r.stderr
          and cancelled not in bot.landings, r.stdout + r.stderr)


def test_operator_approvals_come_only_from_a_person_in_slack():
    import operator_role as O
    print("\n!approve: one item, one use, from an allowlisted person's own Slack message")
    check("parses guard and item", O.parse_approve("!approve delete tsk_ab12") == ("delete", "tsk_ab12", ""))
    check("bare lists", O.parse_approve("!approve") == ("", "", ""))
    check("an unknown guard is refused", O.parse_approve("!approve everything x")[2])
    check("a guard with no item is refused", O.parse_approve("!approve accept")[2])
    tmp = Path(tempfile.mkdtemp())
    state = O.State(tmp / "operator.json")
    ns = lift("approve_command", operator_role=O, OPERATOR=state,
                       BOT_USER_ID="UBOT", ALLOWED_USERS={"U_ME"},
                       fmt_age=lambda ts: "now")
    approve = ns["approve_command"]
    check("the web chat cannot approve",
          "web chat" in approve("!approve accept tsk_1", "U_ME", {"_web": True})
          and not state.approvals())
    check("the bot cannot approve", "Only" in approve("!approve accept tsk_1", "UBOT", {})
          and not state.approvals())
    check("someone off the allowlist cannot",
          "Only" in approve("!approve accept tsk_1", "U_STRANGER", {}) and not state.approvals())
    said = approve("!approve accept tsk_1", "U_ME", {})
    check("you can", ":white_check_mark:" in said and state.has("accept", "tsk_1"), said)
    check("and it is listed", "accept tsk_1" in approve("!approve", "U_ME", {}))
    ro = O.State(tmp / "operator.json", readonly=True)
    check("the CLI's read-only view sees it", ro.has("accept", "tsk_1"))
    try:
        ro.consume("accept", "tsk_1")
        wrote = True
    except PermissionError:
        wrote = False
    check("and cannot use it up or write one", not wrote and state.has("accept", "tsk_1"))
    src = (BASE / "bin" / "silkworm").read_text()
    check("no CLI path writes an approval", ".approve(" not in src)
    bot = (BASE / "bot.py").read_text()
    check("and no bot route does: only approve_command, from handle_prompt",
          bot.count("OPERATOR.approve(") == 1
          and "approve_command(text, user, event)" in bot)
    import roles
    tools = roles.permission_args("operator", ["--dangerously-skip-permissions"], bin="/s")
    check("the operator runs on an allowlist, without skip-permissions",
          tools[0] == "--allowedTools" and "--dangerously-skip-permissions" not in tools)
    check("which has no curl and no raw git",
          "curl" not in tools[1] and "Bash(git" not in tools[1]
          and "Bash(/s accept:*)" in tools[1]
          and "Bash(/s deploy --after-turn:*)" in tools[1], tools[1])


def test_operator_deploy_only_when_behind_and_idle():
    print("\nthe operator deploys only after its turn, with tests, when main is ahead and nothing runs")
    src = (BASE / "bin" / "silkworm").read_text()
    tree = ast.parse(src)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == "operator_deploy_refusal")
    ns = {"os": os}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "silkworm", "exec"), ns)
    refuse = ns["operator_deploy_refusal"]

    def call_for(state="stale", behind=3, running=()):
        def call(path, payload):
            if path == "/status":
                return {"online": True, "revision": {"state": state, "behind": behind}}
            return {"ok": True, "tasks": list(running)}
        return call

    os.environ["SILKWORM_THREAD"] = "D1:1.0"
    try:
        check("plain deploy is refused", "after-turn" in refuse(call_for(), False, False))
        check("skipping tests is refused", refuse(call_for(), True, True))
        check("nothing to deploy when current", "not ahead" in refuse(call_for("current", 0), True, False))
        check("a stale revision with no counted gap is not 'ahead'",
              refuse(call_for("stale", 0), True, False))
        check("another running task holds it",
              "running" in refuse(call_for(running=[{"id": "tsk_x", "thread": "C2:2.0"}]), True, False))
        check("its own turn does not", refuse(call_for(running=[{"id": "tsk_me", "thread": "D1:1.0"}]),
                                              True, False) == "")
        check("an unreachable bot refuses", refuse(lambda p, q: {}, True, False))
    finally:
        os.environ.pop("SILKWORM_THREAD", None)
    check("do_deploy asks it for the operator before anything else runs",
          src.index("operator_deploy_refusal(call, after_turn, skip_tests)")
          < src.index('print(f"{WORM} deploy {repo}"'))


def test_operator_context_is_live_and_within_budget():
    import operator_role as O
    import projects as P
    import tasks as T
    print("\nthe operator's context: rebuilt from live state, within its budget")
    tmp = Path(tempfile.mkdtemp())
    ps = P.ProjectStore(tmp / "projects.json")
    old_root = P.PROJECT_ROOT
    P.PROJECT_ROOT = tmp / "homes"
    try:
        ps.ensure("Kitchen")
        ps.set_brief("kitchen", "White oak cabinets; Golden Mist quartz; grout Bone.")
        ps.ensure("Trader", initial={"scope": {"cwd": "/w/trader", "repo": "/w/trader",
                                                "branch": "main"}},
                  auto_merge=True, test_cmd=".venv/bin/pytest -q", publish=False)
        st = T.TaskStore(tmp / "t.json")
        t = st.create("Fix the halt marker", role="implementor", project="trader")
        st.update(t["id"], result={"landing": {"stage": "conflict", "detail": "app.py",
                                               "at": time.time() - 60}})
        old = st.create("Ancient", role="implementor", project="trader")
        st.update(old["id"], result={"landing": {"stage": "landed", "landed": True,
                                                 "at": time.time() - 9 * 86400}})
        recs = [{**r, "_unready": P.unready(r)} for r in ps.all()]
        notes = "Waiting on Ryan re: Cadence 1.2 release.\n" * 20
        status = {"ok": True, "bot": {"revision": {"state": "stale", "behind": 2}},
                  "needs_you": {"cadence": {"needs_input": [{"id": "tsk_q"}]}}}
        text = O.context(recs, lambda r: ps.brief_for(r["slug"]), status,
                         O.recent_landings(st.all().values(), time.time()),
                         ["2026-10-12 deployed bfb3d8a"], {"cadence": {"sha": "abc"}},
                         {"cadence": ["cadence-v1.2.0"]}, notes)
        check("fits the budget", len(text) <= O.CONTEXT_MAX, str(len(text)))
        for want, why in (("Golden Mist", "a repo-less project's brief"),
                          ('"auto_merge": true', "settings"),
                          (".venv/bin/pytest -q", "test_cmd"),
                          ('"behind":2', "status --json, as JSON"),
                          ("conflict", "a recent landing"),
                          ("cadence-v1.2.0", "a release"),
                          ("deployed bfb3d8a", "a deploy"),
                          ('{"sha": "abc"}', "a failing base"),
                          (notes.strip(), "its notes, whole")):
            check(f"carries {why}", want in text, want)
        check("an old landing is not 'recent'", "Ancient" not in text and old["id"] not in text)
        check("and says it is live state", text.startswith("Live state"))

        huge = {"ok": True, "tasks": ["x" * 100] * 400}
        big = O.context(recs * 60, lambda r: "b" * 5000, huge, ["l" * 150] * 200,
                        ["d" * 200] * 50, {}, {}, notes, budget=6000)
        check("over budget, it still fits", len(big) <= 6000, str(len(big)))
        check("the notes survive the cut", notes.strip() in big)
        check("and the cut is said, not silent",
              "cut to fit" in big or "left out to fit" in big)
        check("a status that could not be read is said",
              "could not be read" in O.context([], lambda r: "", None, [], [], {}, {}, ""))
    finally:
        P.PROJECT_ROOT = old_root

    # The status section is the CLI's own status_summary, run in-process
    # against the bot's handlers rather than over HTTP to itself.
    def fake_tasks(p):
        if p.get("action") == "attention":
            return {"ok": True, "tasks": [{"id": "tsk_1", "project": "a",
                                           "state": "proposed", "title": "x"}]}
        return {"ok": True, "tasks": [], "unmerged": [], "watching": []}
    ns = lift("operator_status", "_cli_module", _CLI={},
                       SILKWORM_BIN=str(BASE / "bin" / "silkworm"), handle_tasks=fake_tasks,
                       handle_status=lambda p: {"online": True,
                                                "revision": {"state": "stale", "behind": 2}})
    got = ns["operator_status"]() or {}
    check("status --json is built in-process by the CLI's own code",
          got.get("ok") and got["bot"]["revision"]["behind"] == 2
          and got["needs_you"] == {"a": {"proposed": [{"id": "tsk_1", "title": "x",
                                                       "state": "proposed", "role": None}]}},
          str(got))
    ns = lift("operator_status", "_cli_module", _CLI={},
                       SILKWORM_BIN=str(BASE / "nonexistent"), handle_tasks=fake_tasks,
                       handle_status=lambda p: {})
    check("and when it cannot be, that is None, not an empty board",
          ns["operator_status"]() is None)

    nb = tmp / "notes.md"
    check("notes over 4KB are refused, not cut", O.write_notes(nb, "n" * 5000) and not nb.exists())
    check("notes within it are kept", O.write_notes(nb, "keep this") == ""
          and O.read_notes(nb).strip() == "keep this")
    bot = (BASE / "bot.py").read_text()
    for fn in ("handle_prompt", "execute_task"):
        body = bot[bot.index(f"def {fn}("):]
        body = body[:body.index("\ndef ", 10)]
        check(f"{fn} builds it fresh for each operator turn", "operator_note(" in body)


def test_operator_sweep_is_daily_after_the_digest_and_posts_once():
    import operator_role as O
    import tasks as T
    print("\nthe operator's sweep: once a day, after the digest, as one message")
    tmp = Path(tempfile.mkdtemp())
    state = O.State(tmp / "operator.json")
    morning = datetime(2026, 10, 13, 8, 45)
    check("no home thread, no sweep", not state.sweep_due(morning, "08:30", "2026-10-13"))
    state.set_thread("D1:1.0")
    check("not before its time", not state.sweep_due(datetime(2026, 10, 13, 8, 0), "08:30", "2026-10-13"))
    check("not before the digest has gone out", not state.sweep_due(morning, "08:30", "2026-10-12"))
    check("unless the digest is off", state.sweep_due(morning, "08:30", "", digest_on=False))
    check("due after both", state.sweep_due(morning, "08:30", "2026-10-13"))

    st = T.TaskStore(tmp / "t.json")
    ns = lift("file_operator_sweep", task_store=st, tasks=T, operator_role=O,
                       OPERATOR=state, BASE_DIR=tmp, datetime=datetime, SILKWORM_BIN="/s")
    filed = []
    for minute in range(0, 60, 5):           # the scheduler's beat, for an hour
        now = datetime(2026, 10, 13, 8, 30 + minute // 2)
        if state.sweep_due(now, "08:30", "2026-10-13"):
            filed.append(ns["file_operator_sweep"](now))
    check("filed exactly once that morning", len(filed) == 1, str(filed))
    rec = st.get(filed[0])
    check("as an operator wake-up on its thread, in the wake lane",
          rec["role"] == "operator" and T.is_wakeup(rec) and O.is_sweep(rec)
          and rec["thread"] == "D1:1.0" and rec["state"] == T.QUEUED, str(rec))
    check("its goal names the CLI by the path its allowlist matches",
          "/s operator contained" in rec["goal"] and "{bin}" not in rec["goal"], rec["goal"][:300])
    check("and again the next day", state.sweep_due(datetime(2026, 10, 14, 9, 0), "08:30", "2026-10-14"))

    # A digest that never posts must not silently cost the sweep too.
    late = O.State(tmp / "late.json")
    late.set_thread("D1:2.0")
    check("still waits for the digest within the grace",
          not late.sweep_due(datetime(2026, 10, 13, 11, 29), "08:30", "2026-10-12"))
    check("sweeps without it once the grace has passed",
          late.sweep_due(datetime(2026, 10, 13, 11, 30), "08:30", "2026-10-12"))
    check("and says the digest was missed",
          O.State.digest_missed(datetime(2026, 10, 13, 11, 30), "2026-10-12")
          and not O.State.digest_missed(datetime(2026, 10, 13, 11, 30), "2026-10-13")
          and not O.State.digest_missed(datetime(2026, 10, 13, 11, 30), "", digest_on=False))
    check("a late sweep time still falls back the same day, not never",
          late.sweep_due(datetime(2026, 10, 13, 23, 59), "22:30", "2026-10-12")
          and not late.sweep_due(datetime(2026, 10, 13, 23, 58), "22:30", "2026-10-12"))
    lst = T.TaskStore(tmp / "late-t.json")
    lns = lift("file_operator_sweep", task_store=lst, tasks=T, operator_role=O,
                        OPERATOR=late, BASE_DIR=tmp, datetime=datetime, SILKWORM_BIN="/s")
    lrec = lst.get(lns["file_operator_sweep"](datetime(2026, 10, 13, 11, 30), digest_missed=True))
    check("a sweep without the digest says so on the board and to the operator",
          "(no digest)" in lrec["title"] and "digest did not post" in lrec["goal"], str(lrec)[:300])
    check("and is not filed twice", not late.sweep_due(datetime(2026, 10, 13, 12, 0), "08:30", "2026-10-12"))
    bot = (BASE / "bot.py").read_text()
    body = bot[bot.index("def _operator_scheduler("):]
    body = body[:body.index("\ndef ", 10)]
    check("the scheduler logs a sweep that ran without the digest",
          "log.warning" in body and "digest_missed=missed" in body)

    class Client:
        def __init__(self, fail_update=False):
            self.calls, self.fail_update = [], fail_update

        def chat_update(self, **kw):
            self.calls.append(("update", kw))
            if self.fail_update:
                raise RuntimeError("message_not_found")

        def chat_postMessage(self, **kw):
            self.calls.append(("post", kw))

    long = "\n".join(f"- landed tsk_{i:04d}: something worth a line" for i in range(400)) \
        + "\n*Needs you:* approve the Cadence release?"
    c = Client()
    n = O.post_sweep(c, "D1", "1.0", "2.0", long, "\n_footer_", 3800)
    check("a long report is still one message", n == 1 and len(c.calls) == 1, str(len(c.calls)))
    body = c.calls[0][1]["text"]
    check("within Slack's limit", len(body) <= 3800, str(len(body)))
    check("keeping its question for you", "Needs you" in body and body.endswith("_footer_"))
    c = Client()
    O.post_sweep(c, "D1", "1.0", None, "short")
    check("with no placeholder, one post", [k for k, _ in c.calls] == ["post"])
    c = Client(fail_update=True)
    O.post_sweep(c, "D1", "1.0", "2.0", "short")
    check("a placeholder that cannot be edited costs no message, and adds no second",
          [k for k, _ in c.calls] == ["update", "post"])
    bot = (BASE / "bot.py").read_text()
    body = bot[bot.index("        if role_name == roles.OPERATOR and operator_role.is_sweep(task):\n"
                         "            # The sweep promises"):]
    body = body[:body.index("            return\n")]
    check("execute_task posts a sweep through post_sweep, then returns before chunking",
          "operator_role.post_sweep(" in body and "chunk(" not in body
          and "chat_postMessage" not in body)


def test_operator_has_no_raw_git_and_its_git_cannot_write():
    import operator_role as O
    import roles
    print("\nthe operator's git: no raw git on its allowlist, and `operator git` cannot write")
    tools = roles.permission_args("operator", [], bin="/s")[1]
    check("no Bash(git …) of any kind is allowlisted (git log --output writes any file)",
          "Bash(git" not in tools and "Bash(/s operator:*)" in tools, tools)
    for argv, why in ((["log", "--output=/tmp/x"], "--output"),
                      (["diff", "--outp=/tmp/x"], "an abbreviation of --output"),
                      (["show", "--ext-diff"], "an external diff"),
                      (["grep", "-Oevil", "x"], "grep's pager"),
                      (["push"], "a writing command"), (["worktree", "add", "x"], "worktree add"),
                      (["config", "core.fsmonitor", "x"], "config")):
        check(f"refuses {why}", O.git_refusal(argv), str(argv))
    for argv in (["log", "--oneline", "-3"], ["diff", "main...silkworm/x"],
                 ["worktree", "list"], ["status", "--short"]):
        check(f"allows {' '.join(argv)}", O.git_refusal(argv) == "", O.git_refusal(argv))
    repo = _op_repo()
    target = Path(tempfile.mkdtemp()) / "operator.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith("SILKWORM_")}
    env["SILKWORM_ROLE"] = "operator"
    r = subprocess.run([sys.executable, str(BASE / "bin" / "silkworm"), "operator", "git",
                        "--in", str(repo), "log", "-1", f"--output={target}"],
                       capture_output=True, text=True, env=env, timeout=60)
    check("through the CLI, --output is refused and nothing is written",
          r.returncode == 1 and not target.exists(), r.stdout + r.stderr)
    r = subprocess.run([sys.executable, str(BASE / "bin" / "silkworm"), "operator", "git",
                        "--in", str(repo), "log", "-1", "--format=%s"],
                       capture_output=True, text=True, env=env, timeout=60)
    check("and a read runs", r.returncode == 0 and r.stdout.strip() == "fix line 32, and 31 beside it",
          r.stdout + r.stderr)


def test_operator_git_runs_no_program_the_repository_config_names():
    import operator_role as O
    print("\nthe operator's git: a repo's own diff drivers and filters run nothing")
    base = Path(tempfile.mkdtemp())
    repo = base / "evil"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("SILKWORM_")}

    def git(*a):
        subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, env=env)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / ".gitattributes").write_text("*.txt diff=evil\n*.dat filter=evil\n")
    ran = base / "ran"
    git("config", "diff.evil.textconv", f"echo textconv >> {ran}; cat")
    git("config", "diff.evil.command", f"echo command >> {ran}; true")
    git("config", "filter.evil.smudge", f"echo smudge >> {ran}; cat")
    (repo / "f.txt").write_text("one\n")
    git("add", "f.txt", ".gitattributes")
    git("commit", "-qm", "one")
    (repo / "f.txt").write_text("two\n")
    git("commit", "-qam", "two")
    (repo / "f.txt").write_text("three\n")
    env["SILKWORM_ROLE"] = "operator"
    for argv in (["log", "-p"], ["show"], ["diff"], ["diff", "HEAD~1"],
                 ["blame", "f.txt"], ["grep", "-e", "three"]):
        if ran.exists():
            ran.unlink()
        r = subprocess.run([sys.executable, str(BASE / "bin" / "silkworm"), "operator", "git",
                            "--in", str(repo), *argv],
                           capture_output=True, text=True, env=env, timeout=60)
        check(f"`git {' '.join(argv)}` reads, running none of the repo's programs",
              r.returncode == 0 and not ran.exists(),
              (ran.read_text() if ran.exists() else "") + r.stdout + r.stderr)
    for argv in (["cat-file", "--filters", "HEAD:f.txt"], ["cat-file", "--filt", "HEAD:f.txt"],
                 ["log", "--remerge-diff"], ["log", "--diff-merges=remerge"],
                 ["show", "--diff-m", "r"], ["diff", "--submodule=diff"],
                 ["diff", "--sub=diff"]):
        check(f"refuses {' '.join(argv)} (runs the repo's programs)",
              O.git_refusal(argv), str(argv))
    for argv in (["diff", "--text"], ["log", "--diff-merges", "first-parent"],
                 ["diff", "--submodule=log"], ["diff", "--submodule"]):
        check(f"allows {' '.join(argv)}", O.git_refusal(argv) == "", O.git_refusal(argv))

    def operator(*argv, where=repo):
        if ran.exists():
            ran.unlink()
        return subprocess.run([sys.executable, str(BASE / "bin" / "silkworm"), "operator",
                               "git", "--in", str(where), *argv],
                              capture_output=True, text=True, env=env, timeout=60)

    # A signed commit, and the repo's own gpg.program -- %G? and showSignature.
    head = subprocess.run(["git", "cat-file", "commit", "HEAD"], cwd=repo, env=env,
                          capture_output=True, text=True, check=True).stdout
    hdr, _, msg = head.partition("\n\n")
    signed = (hdr + "\ngpgsig -----BEGIN PGP SIGNATURE-----\n \n x\n"
              " -----END PGP SIGNATURE-----\n\n" + msg)
    sha = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"], cwd=repo,
                         env=env, input=signed, capture_output=True, text=True,
                         check=True).stdout.strip()
    git("update-ref", "refs/heads/signed", sha)
    fake = base / "fakegpg"
    fake.write_text(f"#!/bin/sh\necho gpg >> {ran}\nexit 1\n")
    fake.chmod(0o755)
    git("config", "gpg.program", str(fake))
    git("config", "log.showSignature", "true")
    for argv in (["log", "-1", "--format=%G?", "signed"], ["log", "-1", "signed"],
                 ["for-each-ref", "--format=%(signature)", "refs/heads/signed"]):
        r = operator(*argv)
        check(f"`git {' '.join(argv)}` checks no signature with the repo's gpg.program",
              not ran.exists(), (ran.read_text() if ran.exists() else "") + r.stderr)

    # A conflicted merge, and the repo's own merge driver behind log.diffMerges.
    (repo / ".gitattributes").write_text("*.txt diff=evil merge=evil\n")
    git("commit", "-qam", "attrs")
    git("config", "merge.evil.driver", f"echo merge >> {ran}; false")
    git("checkout", "-qb", "side")
    (repo / "f.txt").write_text("side\n")
    git("commit", "-qam", "side")
    git("checkout", "-q", "-")
    (repo / "f.txt").write_text("main\n")
    git("commit", "-qam", "main")
    subprocess.run(["git", "merge", "-q", "side"], cwd=repo, env=env, capture_output=True)
    (repo / "f.txt").write_text("both\n")
    git("commit", "-qam", "merged")
    git("config", "log.diffMerges", "remerge")
    r = operator("log", "-m", "-p", "-1")
    check("`git log -m -p` with log.diffMerges=remerge runs no merge driver",
          r.returncode == 0 and not ran.exists(),
          (ran.read_text() if ran.exists() else "") + r.stderr)

    # A partial clone missing a blob: a lazy fetch would run core.sshCommand.
    lazy = base / "lazy"
    lazy.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=lazy, env=env, check=True)
    (lazy / "a.txt").write_text("hello\n")
    for a in (["add", "a.txt"], ["-c", "user.email=t@t", "-c", "user.name=t",
                                  "commit", "-qm", "a"]):
        subprocess.run(["git", *a], cwd=lazy, env=env, check=True)
    blob = subprocess.run(["git", "rev-parse", "HEAD:a.txt"], cwd=lazy, env=env,
                          capture_output=True, text=True, check=True).stdout.strip()
    (lazy / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    for k, v in (("extensions.partialClone", "origin"), ("remote.origin.promisor", "true"),
                 ("remote.origin.url", "ssh://example.invalid/x"),
                 ("core.sshCommand", f"echo ssh >> {ran}; false #")):
        subprocess.run(["git", "config", k, v], cwd=lazy, env=env, check=True)
    r = operator("show", "HEAD:a.txt", where=lazy)
    check("a missing blob in a partial clone is not lazily fetched with the repo's ssh",
          not ran.exists(), (ran.read_text() if ran.exists() else "") + r.stderr)
    check("the safe flags follow the subcommand, before the operator's own arguments",
          O.git_command(["log", "-p"])[-4:] == ["log", "--no-ext-diff", "--no-textconv", "-p"]
          and O.git_command(["status"])[-1:] == ["status"] and O.git_command([])[0] == "git")


def test_operator_cannot_queue_work_by_any_other_door():
    import operator_role as O
    import tasks as T
    print("\nthe operator: retry, rework and edit are not side doors into the queue")
    bot = _operator_bot()
    st = bot.store
    me = {"caller_role": "operator"}

    def ask(action, tid, **kw):
        return bot.ns["handle_tasks"]({"action": action, "id": tid, **me, **kw})

    prop = st.create("Add an appearance preference to Settings", state=T.PROPOSED,
                     role="implementor", project="alpha")["id"]
    for action, kw in (("retry", {}), ("rework", {"notes": "go"})):
        out = ask(action, prop, **kw)
        check(f"{action} on a proposal is accepting it, and refused",
              not out["ok"] and out.get("refused") == "accept"
              and st.get(prop)["state"] == T.PROPOSED, str(out))
    st.transition(prop, T.CANCELLED, "dismiss via ui")
    out = ask("retry", prop)
    check("retrying a proposal you dismissed is refused too (cancelled is final)",
          not out["ok"] and st.get(prop)["state"] == T.CANCELLED, str(out))
    q = st.create("Cache the release plan per checkout", role="implementor", project="alpha")["id"]
    out = ask("edit", q, goal="Rewrite the landing code from scratch, all of it")
    check("an edit that writes a new goal is refused", not out["ok"]
          and "Rewrite" not in st.get(q)["goal"], str(out))
    out = ask("edit", q, title="Release plan cache")
    check("an edit of the title is not", out["ok"], str(out))
    failed = st.create("Retry sync on 5xx", role="implementor", project="alpha")["id"]
    st.transition(failed, T.RUNNING); st.transition(failed, T.FAILED, "overloaded")
    out = ask("rework", failed, notes="also rewrite the settings screen")
    check("reworking work whose landing was not refused is a new goal, and refused",
          not out["ok"] and st.get(failed)["state"] == T.FAILED, str(out))
    out = ask("retry", failed)
    check("retrying failed work it was given is allowed",
          out["ok"] and st.get(failed)["state"] == T.QUEUED, str(out))
    clash = st.create("Fix the countdown", role="implementor", project="alpha")["id"]
    st.transition(clash, T.RUNNING); st.transition(clash, T.DONE)
    st.update(clash, result={"landing": {"eligible": True, "landed": False,
                                         "stage": "rebase", "detail": "conflict in a.py"}})
    check("reworking a landing git refused is its job",
          O.tasks_guard("rework", {"id": clash}, st.get(clash)) is None)
    flagged = st.create("Tidy the sync loop", role="implementor")["id"]
    st.transition(flagged, T.RUNNING); st.transition(flagged, T.AWAITING_APPROVAL)
    out = ask("dismiss", flagged)
    check("dismissing flagged work is refused (test_operator_cannot_decide_against_you)",
          not out["ok"] and st.get(flagged)["state"] == T.AWAITING_APPROVAL, str(out))
    out = ask("land", flagged)
    check("and landing it while it waits for you is refused",
          not out["ok"] and out.get("refused") == "accept" and flagged not in bot.landings,
          str(out))


def test_operator_cannot_decide_against_you():
    import operator_role as O
    import tasks as T
    print("\nthe operator: dismissing, closing, digging and cancelling what waits for you "
          "are refused too")
    bot = _operator_bot()
    st = bot.store
    stopped = []
    bot.ns["stop_task"] = lambda tid: stopped.append(tid) or True

    def refused_cli(r, tid, rec, state):
        return (r.returncode == 1 and f"!approve accept {tid}" in r.stderr
                and st.get(tid)["state"] == state
                and len(st.get(tid)["events"]) == len(rec["events"]))

    prop = st.create("Add an appearance preference to Settings", state=T.PROPOSED,
                     role="implementor", project="alpha")["id"]
    before = st.get(prop)
    r = _op_cli(bot, "dismiss", prop, "--by", "operator")
    check("dismissing a proposal you never saw is refused, and it stays proposed",
          refused_cli(r, prop, before, T.PROPOSED), r.stdout + r.stderr)
    ideated = st.create("Cache the release plan per checkout", state=T.PROPOSED,
                        role="implementor", project="alpha", filed_by="ideator")["id"]
    r = _op_cli(bot, "dismiss", ideated, "--by", "operator")
    check("so is one another run filed", refused_cli(r, ideated, st.get(ideated), T.PROPOSED),
          r.stdout + r.stderr)

    flagged = st.create("Tidy the sync retry loop", role="implementor", project="alpha")["id"]
    st.transition(flagged, T.RUNNING); st.transition(flagged, T.AWAITING_APPROVAL)
    before = st.get(flagged)
    r = _op_cli(bot, "dismiss", flagged, "--by", "operator")
    check("dismissing reviewed work waiting in awaiting_approval is refused",
          refused_cli(r, flagged, before, T.AWAITING_APPROVAL), r.stdout + r.stderr)

    found = st.create("Why does sync stall on cold launch?", role="researcher",
                      project="alpha")["id"]
    st.transition(found, T.RUNNING); st.transition(found, T.NEEDS_INPUT, "findings")
    before = st.get(found)
    for argv in (("dismiss", found), ("close", found), ("dig", found, "--notes", "and on iPad?")):
        r = _op_cli(bot, *argv, "--by", "operator")
        check(f"{argv[0]} on research's parked findings is refused, unread findings untouched",
              refused_cli(r, found, before, T.NEEDS_INPUT), r.stdout + r.stderr)
    for tid, rec, state in ((found, before, T.NEEDS_INPUT),
                            (flagged, st.get(flagged), T.AWAITING_APPROVAL)):
        r = _op_cli(bot, "retry", tid, "--by", "operator")
        check(f"retrying work waiting on you in {state} is refused (it would rerun unread)",
              refused_cli(r, tid, rec, state), r.stdout + r.stderr)
    dead = types.SimpleNamespace(port=1, statefile=bot.statefile)
    r = _op_cli(dead, "close", found)
    check("close is refused by the CLI itself, before asking a bot (1, not 3)",
          r.returncode == 1 and f"!approve accept {found}" in r.stderr, r.stdout + r.stderr)
    out = bot.ns["handle_tasks"]({"action": "close", "id": found, "caller_role": "operator"})
    check("and by the bot on its own", not out["ok"] and out.get("refused") == "accept"
          and st.get(found)["state"] == T.NEEDS_INPUT, str(out))

    work = st.create("Retry sync on 5xx", role="implementor", project="alpha")["id"]
    st.transition(work, T.RUNNING)
    for action in ("cancel", "dismiss"):
        out = bot.ns["handle_tasks"]({"action": action, "id": work, "caller_role": "operator"})
        check(f"{action} of a running implementor is refused, and its child is not killed",
              not out["ok"] and out.get("refused") == "accept"
              and st.get(work)["state"] == T.RUNNING and not stopped, str(out))
    waiting = st.create("Retry sync on 4xx", role="implementor", project="alpha")["id"]
    st.transition(waiting, T.RUNNING); st.transition(waiting, T.BLOCKED, "review")
    broke = st.create("Retry sync on timeouts", role="implementor", project="alpha")["id"]
    st.transition(broke, T.RUNNING); st.transition(broke, T.FAILED, "tests")
    accepted = st.create("Retry sync offline", state=T.PROPOSED, role="implementor",
                         project="alpha", filed_by=O.ROLE)["id"]
    st.transition(accepted, T.QUEUED)
    for tid, state in ((waiting, T.BLOCKED), (broke, T.FAILED), (accepted, T.QUEUED)):
        rec = st.get(tid)
        r = _op_cli(bot, "dismiss", tid, "--by", "operator")
        check(f"dismissing a {state} implementor is refused (cancelled is final)",
              refused_cli(r, tid, rec, state), r.stdout + r.stderr)
        out = bot.ns["handle_tasks"]({"action": "cancel", "id": tid, "caller_role": "operator"})
        check(f"so is cancelling it", not out["ok"] and out.get("refused") == "accept"
              and st.get(tid)["state"] == state, str(out))
    queued_own = st.create("Sweep the board later", role="operator")["id"]
    out = bot.ns["handle_tasks"]({"action": "cancel", "id": queued_own, "caller_role": "operator"})
    check("cancelling its own queued turn is its to do",
          out["ok"] and st.get(queued_own)["state"] == T.CANCELLED, str(out))
    own = st.create("Sweep the board", role="operator")["id"]
    st.transition(own, T.RUNNING)
    out = bot.ns["handle_tasks"]({"action": "cancel", "id": own, "caller_role": "operator"})
    check("cancelling its own running turn is still its to do",
          out["ok"] and st.get(own)["state"] == T.CANCELLED and stopped == [own], str(out))

    # Its own filings: stamped by the route that files them, then dismissable.
    bot.ns["project_store"] = types.SimpleNamespace(
        ensure=lambda n, **kw: {"slug": n}, home=lambda n, create=False: None,
        scope_for=lambda slug: {"branch": "main"}, get=lambda slug: None)
    out = bot.ns["handle_tasks"]({"action": "create", "state": "proposed", "project": "alpha",
                                  "goal": "Show the sync error text in Settings when a push fails",
                                  "scope": {"cwd": str(bot.tmp)}, "caller_role": "operator"})
    mine = (out.get("task") or {}).get("id", "")
    check("a proposal the operator files is recorded as its own",
          out["ok"] and st.get(mine)["filed_by"] == O.ROLE, str(out))
    r = _op_cli(bot, "dismiss", mine, "--by", "operator")
    check("and it may dismiss that one without asking",
          r.returncode == 0 and st.get(mine)["state"] == T.CANCELLED, r.stdout + r.stderr)
    out = bot.ns["handle_tasks"]({"action": "create", "state": "proposed", "project": "alpha",
                                  "goal": "Show the sync error text in Settings on a 4xx too",
                                  "scope": {"cwd": str(bot.tmp)}})
    check("one you file from the dashboard is nobody's run's",
          out["ok"] and st.get(out["task"]["id"])["filed_by"] == "", str(out))

    # An approval lifts each once.
    for tid, argv, after in ((prop, ("dismiss", prop), T.CANCELLED),
                             (flagged, ("dismiss", flagged), T.CANCELLED),
                             (found, ("close", found), T.DONE)):
        bot.state.approve("accept", tid, by="U_ME")
        r = _op_cli(bot, *argv, "--by", "operator")
        check(f"approved, {argv[0]} of {st.get(tid)['role']} work goes ahead once",
              r.returncode == 0 and st.get(tid)["state"] == after
              and not bot.state.has("accept", tid), r.stdout + r.stderr)
    found2 = st.create("Is the widget timeline too eager?", role="researcher",
                       project="alpha")["id"]
    st.transition(found2, T.RUNNING); st.transition(found2, T.NEEDS_INPUT, "findings")
    bot.state.approve("accept", found2, by="U_ME")
    r = _op_cli(bot, "dig", found2, "--notes", "check iOS 26 too", "--by", "operator")
    check("an approved dig goes ahead and uses the approval up",
          r.returncode == 0 and st.get(found2)["state"] != T.NEEDS_INPUT
          and not bot.state.has("accept", found2), r.stdout + r.stderr)
    bot.state.approve("accept", work, by="U_ME")
    out = bot.ns["handle_tasks"]({"action": "cancel", "id": work, "caller_role": "operator"})
    check("an approved cancel stops the running task",
          out["ok"] and st.get(work)["state"] == T.CANCELLED and work in stopped, str(out))

    r = _op_cli(bot, "dismiss", ideated, role="", thread="")
    check("your own CLI dismiss is unaffected",
          r.returncode == 0 and st.get(ideated)["state"] == T.CANCELLED, r.stdout + r.stderr)

    # `silkworm task` from a turn: the other filing route stamps the same way,
    # by the role the bot set or by the operator's home thread (D5:5.0 here).
    file_task, _filed, _begin, fts, _made = _file_task_impl()
    for key, role, want in (("C1:1.0", "operator", O.ROLE), ("D5:5.0", "", O.ROLE),
                            ("C1:2.0", "ideator", "ideator"), ("C1:3.0", "", "")):
        out = file_task({"key": key, "propose": True, "caller_role": role,
                         "goal": f"Add an appearance preference to Settings ({key})"})
        check(f"a filing from {role or 'no role'} on {key} is filed_by {want or 'nobody'}",
              out.get("ok") and fts.get(out["id"])["filed_by"] == want, str(out))


def test_operator_cancel_holds_to_the_state_its_guard_saw():
    import tasks as T
    print("\nthe operator: a queued task claimed after the guard looked is not cancelled "
          "or killed, approved or its own")
    bot = _operator_bot()
    st = bot.store
    stopped = []
    bot.ns["stop_task"] = lambda tid: stopped.append(tid) or True

    class Racing:
        """The store, with the runner claiming `tid` at a chosen moment: after
        the guard's read (`after_reads` gets of it), or just before the move."""
        def __init__(self, tid, after_reads=None, at_move=False):
            self.tid, self.after, self.at_move, self.reads = tid, after_reads, at_move, 0
        def __getattr__(self, name):
            return getattr(st, name)
        def _claim(self):
            if st.get(self.tid)["state"] == T.QUEUED:
                st.transition(self.tid, T.RUNNING, "claimed by the runner")
        def get(self, tid):
            rec = st.get(tid)
            if tid == self.tid:
                self.reads += 1
                if self.after is not None and self.reads == self.after:
                    self._claim()
            return rec
        def transition(self, tid, *a, **kw):
            if tid == self.tid and self.at_move:
                self._claim()
            return st.transition(tid, *a, **kw)

    for action in ("cancel", "dismiss"):
        for label, race in (("after the guard read it", dict(after_reads=1)),
                            ("just before the move", dict(at_move=True))):
            work = st.create(f"Retry sync on 5xx ({action}, {label})",
                             role="implementor", project="alpha")["id"]
            # Cancelling queued work needs your approval (7d32adb); you gave
            # it for a queued task, not for killing a running one.
            bot.state.approve("accept", work, by="U_ME")
            bot.ns["task_store"] = Racing(work, **race)
            stopped.clear()
            out = bot.ns["handle_tasks"]({"action": action, "id": work,
                                          "caller_role": "operator"})
            check(f"an approved {action} of a queued task claimed {label} is refused, "
                  "still running, its child not killed",
                  not out["ok"] and not out.get("refused")
                  and st.get(work)["state"] == T.RUNNING and not stopped, str(out))
    own = st.create("Sweep the board", role="operator")["id"]
    bot.ns["task_store"] = Racing(own, after_reads=1)
    stopped.clear()
    out = bot.ns["handle_tasks"]({"action": "cancel", "id": own, "caller_role": "operator"})
    check("so is a cancel of its own queued turn, claimed after the guard read it",
          not out["ok"] and st.get(own)["state"] == T.RUNNING and not stopped, str(out))
    bot.ns["task_store"] = st

    queued = st.create("Retry sync on 5xx (supplied)", role="implementor", project="alpha")["id"]
    out = bot.ns["handle_tasks"]({"action": "cancel", "id": queued,
                                  "_guard_state": T.RUNNING})
    check("a caller cannot supply the guard's state itself",
          out["ok"] and st.get(queued)["state"] == T.CANCELLED, str(out))
    work = st.create("Retry sync on 5xx (dashboard)", role="implementor", project="alpha")["id"]
    st.transition(work, T.RUNNING)
    stopped.clear()
    out = bot.ns["handle_tasks"]({"action": "cancel", "id": work})
    check("your own cancel of a running task still stops it",
          out["ok"] and st.get(work)["state"] == T.CANCELLED and stopped == [work], str(out))


def test_operator_drops_nothing_reverted_and_tidies_only_finished_task_checkouts():
    import operator_role as O
    import tasks as T
    import worktrees as W
    print("\nthe operator: a reverted change is not 'contained'; tidy is for finished tasks' checkouts")
    repo = _op_repo()
    _op_git(repo, "checkout", "-qb", "silkworm/tsk_rev", "fork")
    (repo / "rev.py").write_text("x = 1\n")
    _op_git(repo, "add", "-A"); _op_git(repo, "commit", "-qm", "add rev.py")
    _op_git(repo, "checkout", "-q", "main")
    _op_git(repo, "cherry-pick", "silkworm/tsk_rev")
    _op_git(repo, "revert", "--no-edit", "HEAD")
    cherry = _op_git(repo, "cherry", "main", "silkworm/tsk_rev")
    check("the fixture: git cherry calls it landed", cherry.startswith("-"), cherry)
    ok, why = O.contained(repo, "silkworm/tsk_rev", "main")
    check("but main no longer holds it, so it is not contained", not ok, why)

    root = Path(tempfile.mkdtemp())
    st = T.TaskStore(root / "t.json")
    ages = {}
    wt = types.SimpleNamespace(**{k: getattr(W, k) for k in dir(W) if not k.startswith("__")})
    wt.ROOT = root
    wt.age_s = lambda p: ages.get(Path(p).name, 99999)
    ns = lift("tidy_checkout", task_store=st, tasks=T, worktrees=wt,
                       live_worktree_tasks=lambda: set(),
                       repo_guard=lambda *a, **k: contextlib.nullcontext(), Path=Path)
    tidy = ns["tidy_checkout"]

    def checkout(tid, state=T.DONE):
        if state:
            t = st.create("w", role="implementor")
            st._data[tid] = {**st._data.pop(t["id"]), "id": tid, "state": state}
        path = root / f"trader--{tid}"
        _op_git(repo, "worktree", "add", "-q", "-b", f"silkworm/{tid}", str(path), "main")
        return path

    stray = checkout("feature", state=None)
    out = tidy({"path": str(stray)})
    check("a checkout no task owns is refused", not out["ok"] and stray.exists(), str(out))
    run = checkout("tsk_run", state=T.RUNNING)
    out = tidy({"path": str(run)})
    check("one a live task owns is refused", not out["ok"] and run.exists(), str(out))
    young = checkout("tsk_young")
    ages["trader--tsk_young"] = 5
    out = tidy({"path": str(young)})
    check("one touched within the hour is refused", not out["ok"] and young.exists(), str(out))
    held = checkout("tsk_held")
    (held / "notes.txt").write_text("somebody's work\n")
    out = tidy({"path": str(held)})
    check("one holding work is refused, naming it",
          not out["ok"] and "notes.txt" in out["error"] and held.exists(), str(out))
    clean = checkout("tsk_clean")
    out = tidy({"path": str(clean)})
    check("a finished task's clean checkout is removed", out["ok"] and not clean.exists(), str(out))
    out = tidy({"path": str(repo)})
    check("the repository itself is never a checkout to tidy",
          not out["ok"] and repo.exists(), str(out))


def test_operator_turns_do_not_hold_the_checkout_and_deploy_tests_run_clean():
    print("\nthe operator's turn holds no repo_guard; deploy's suite runs outside the turn")
    tree = ast.parse((BASE / "bot.py").read_text())
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for fn in ("handle_prompt", "execute_task"):
        guards = [n for n in ast.walk(fns[fn]) if isinstance(n, ast.Assign)
                  and ast.unparse(n.targets[0]) == "guard"]
        check(f"{fn}: the operator's turn takes no repo_guard",
              len(guards) == 1 and ast.unparse(guards[0].value).startswith(
                  "contextlib.nullcontext() if role_name == roles.OPERATOR else repo_guard("),
              str([ast.unparse(g) for g in guards]))
    src = (BASE / "bin" / "silkworm").read_text()
    node = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)
                and n.name == "run_suite")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(kw.get("env") or {})
        return types.SimpleNamespace(returncode=0)
    ns = {"venv_python": lambda: sys.executable, "sys": sys, "REPO": BASE, "os": os,
          "git_guard": __import__("git_guard"), "str": str,
          "operator_role": __import__("operator_role"),
          "subprocess": types.SimpleNamespace(run=fake_run)}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "silkworm", "exec"), ns)
    saved = {k: os.environ.get(k) for k in ("SILKWORM_ROLE", "SILKWORM_THREAD",
                                             "SILKWORM_TASK_ROLE", "SILKWORM_TASK_ID",
                                             "SILKWORM_USER_GRANT")}
    os.environ.update(SILKWORM_ROLE="operator", SILKWORM_THREAD="D1:1.0",
                      SILKWORM_TASK_ROLE="operator", SILKWORM_TASK_ID="tsk_x",
                      SILKWORM_USER_GRANT="f00d")
    try:
        ns["run_suite"]()
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    check("deploy's preflight suite runs without the turn's role, thread or guard",
          calls and not any(k in calls[0] for k in saved), str(sorted(calls[0])[:5]))


# `tailscale serve` proxies tailnet requests to the dashboard on 127.0.0.1, so
# without a gate every device on the tailnet -- a tagged server, a node shared
# in from someone else's tailnet -- would be the trusted loopback owner, able
# to relay prompts through /api/send. Proxied requests must carry the identity
# serve stamps on them, and it must be VIZ_TAILSCALE_LOGIN; plain loopback is
# untouched.
def test_viz_tailscale_identity_gate():
    import threading
    import urllib.error
    import urllib.request
    import visualizer as V
    print("\nthe dashboard admits only VIZ_TAILSCALE_LOGIN through tailscale serve")

    saved_env = os.environ.pop("VIZ_TAILSCALE_LOGIN", None)
    saved = (V.LOOPBACK, V.TOKEN, V.SESSIONS_FILE, V.ENV_FILE, V.bot_call)
    tmp = Path(tempfile.mkdtemp())
    (tmp / "sessions.json").write_text("{}")
    sent = []
    srv = None
    try:
        V.LOOPBACK, V.TOKEN = True, ""
        V.SESSIONS_FILE = tmp / "sessions.json"
        V.ENV_FILE = tmp / "no.env"           # nothing configured
        V.bot_call = lambda path, payload, timeout=1.0: sent.append(path) or {"ok": True}
        srv = V.make_server("127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]

        def call(path, headers=None, post=False):
            # As the page posts: JSON, unless a test says otherwise.
            headers = ({"Content-Type": "application/json"} if post else {}) | (headers or {})
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", headers=headers,
                data=b"{}" if post else None, method="POST" if post else "GET")
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()

        def via_serve(login=None, **extra):
            """Headers as tailscale serve sends them (ipn/ipnlocal/serve.go)."""
            h = {"X-Forwarded-For": "100.101.102.103",
                 "X-Forwarded-Host": "mini.tail1234.ts.net",
                 "X-Forwarded-Proto": "https"}
            if login is not None:
                h.update({"Tailscale-User-Login": login, "Tailscale-User-Name": "Owner",
                          "Tailscale-Headers-Info": "https://tailscale.com/s/serve-headers"})
            h.update(extra)
            return h

        gets, posts = _viz_routes("do_GET"), _viz_routes("do_POST")
        check("the route list is read from the code (has / and /api/send)",
              "/" in gets and "/api/send" in posts and len(posts) >= 8, str(posts))

        # Unset login: fail closed for anything proxied, loopback unchanged.
        code, body = call("/api/sessions", via_serve("owner@example.com"))
        check("login unset: a proxied request is refused, even with an identity",
              code == 403 and "VIZ_TAILSCALE_LOGIN is not set" in body, f"{code} {body}")
        code, _ = call("/api/send", via_serve("owner@example.com"), post=True)
        check("login unset: proxied /api/send is refused and never reaches the bot",
              code == 403 and not sent, f"{code} {sent}")
        code, _ = call("/api/sessions")
        check("login unset: unproxied loopback reads as before", code == 200)
        code, _ = call("/api/send", post=True)
        check("login unset: unproxied loopback still relays /api/send",
              code == 200 and "/web-message" in sent, f"{code} {sent}")
        sent.clear()

        os.environ["VIZ_TAILSCALE_LOGIN"] = "Owner@Example.com"
        code, _ = call("/api/sessions", via_serve("owner@example.com"))
        check("the owner's login is let in (case aside)", code == 200, str(code))
        code, _ = call("/api/send", via_serve("owner@example.com"), post=True)
        check("the owner's login can relay /api/send",
              code == 200 and "/web-message" in sent, f"{code} {sent}")
        sent.clear()
        code, _ = call("/favicon.svg", via_serve("=?utf-8?q?owner=40example.com?="))
        check("an RFC 2047-encoded login is decoded before comparing", code == 200, str(code))

        sent.clear()      # /api/sessions asks the bot for /status; start clean
        code, body = call("/api/sessions", via_serve("intruder@example.com"))
        check("a wrong login is refused with a plain reason",
              code == 403 and "intruder@example.com is not" in body, f"{code} {body}")
        refused = [p for p in gets if call(p, via_serve("intruder@example.com"))[0] != 403]
        refused += [p for p in posts
                    if call(p, via_serve("intruder@example.com"), post=True)[0] != 403]
        check("a wrong login is refused on every GET and POST route",
              not refused and not sent, f"let through: {refused} {sent}")

        code, body = call("/api/sessions", via_serve())
        check("a proxied request with no identity (a tagged device) is refused",
              code == 403 and "no Tailscale identity" in body, f"{code} {body}")
        code, _ = call("/api/send", via_serve(), post=True)
        check("...including /api/send, which never reaches the bot",
              code == 403 and not sent, f"{code} {sent}")
        for marker, value in (("X-Forwarded-For", "100.64.0.9"),
                              ("X-Forwarded-Host", "mini.tail1234.ts.net"),
                              ("Host", "mini.tail1234.ts.net")):
            code, _ = call("/api/sessions", {marker: value})
            check(f"{marker} alone marks a request as proxied", code == 403, str(code))
        code, body = call("/api/sessions", via_serve(None, **{
            "Tailscale-Funnel-Request": "?1"}))
        check("a Funnel request is refused", code == 403 and "Funnel" in body, body)

        # Serve stamps the owner's login on whatever page sent the request,
        # so a hostile page in the owner's browser must not post as them.
        sent.clear()
        for label, extra in (
                ("a text/plain POST (no preflight)", {"Content-Type": "text/plain"}),
                ("a form POST", {"Content-Type": "application/x-www-form-urlencoded"}),
                ("a cross-site POST", {"Sec-Fetch-Site": "cross-site"}),
                ("a same-site POST from another subdomain", {"Sec-Fetch-Site": "same-site"}),
                ("a POST from another origin", {"Origin": "https://evil.example"})):
            code, body = call("/api/tasks", via_serve("owner@example.com", **extra), post=True)
            check(f"the owner's login is not enough for {label}",
                  code == 403 and not sent, f"{code} {body} {sent}")
        code, _ = call("/api/tasks", via_serve("owner@example.com", **{
            "Origin": "https://mini.tail1234.ts.net", "Sec-Fetch-Site": "same-origin"}), post=True)
        check("the dashboard's own page posts as before",
              code == 200 and "/tasks" in sent, f"{code} {sent}")
        sent.clear()
        code, _ = call("/api/send", {"Content-Type": "text/plain"}, post=True)
        check("unproxied loopback POSTs are not held to the remote rules (unchanged)",
              code == 200 and "/web-message" in sent, f"{code} {sent}")
        sent.clear()

        code, _ = call("/api/sessions")
        check("login set: unproxied loopback reads as before", code == 200)
        code, _ = call("/api/send", post=True)
        check("login set: unproxied loopback still relays /api/send",
              code == 200 and "/web-message" in sent, f"{code} {sent}")
        sent.clear()

        # The installed service sees only PATH, so .env must be enough.
        os.environ.pop("VIZ_TAILSCALE_LOGIN")
        V.ENV_FILE = tmp / ".env"
        V.ENV_FILE.write_text('VIZ_TAILSCALE_LOGIN="owner@example.com"\n')
        code, _ = call("/api/sessions", via_serve("owner@example.com"))
        check("VIZ_TAILSCALE_LOGIN is read from .env, without a restart", code == 200, str(code))
        code, _ = call("/api/sessions", via_serve("intruder@example.com"))
        check("...and still refuses anyone else", code == 403, str(code))

        # Off loopback the token is still demanded on top of the identity.
        V.LOOPBACK, V.TOKEN = False, "s3cret"
        code, _ = call("/api/sessions", via_serve("intruder@example.com",
                                                  **{"X-Silkworm-Token": "s3cret"}))
        check("token mode: a valid token does not let a wrong login in", code == 403)
        code, _ = call("/api/sessions", via_serve("owner@example.com"))
        check("token mode: the right login still needs the token", code == 401, str(code))
    finally:
        V.LOOPBACK, V.TOKEN, V.SESSIONS_FILE, V.ENV_FILE, V.bot_call = saved
        if srv is not None:
            srv.shutdown(); srv.server_close()
        if saved_env is None:
            os.environ.pop("VIZ_TAILSCALE_LOGIN", None)
        else:
            os.environ["VIZ_TAILSCALE_LOGIN"] = saved_env


def test_viz_tailscale_gate_runs_before_everything():
    print("\nthe tailscale gate sits in _authed, ahead of the loopback shortcut")
    tree = ast.parse((BASE / "visualizer.py").read_text())
    handler = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
    fns = {n.name: n for n in handler.body if isinstance(n, ast.FunctionDef)}
    first = ast.unparse(fns["_authed"].body[1])     # body[0] is the docstring
    check("_authed asks tailnet_refusal first", "tailnet_refusal(self.headers)" in first, first)
    for name in ("do_GET", "do_POST"):
        stmts = [ast.unparse(s) for s in fns[name].body[:3]]
        check(f"{name} checks _authed before any route",
              any("if not self._authed(url)" in s for s in stmts), str(stmts))
    check("no other HTTP verb is handled (they would skip _authed)",
          sorted(n for n in fns if n.startswith("do_")) == ["do_GET", "do_POST"],
          str(sorted(fns)))


def test_silkworm_tailscale_subcommand():
    import importlib.machinery
    import importlib.util
    print("\nsilkworm tailscale: status, on and off; install steps when it is missing")
    loader = importlib.machinery.SourceFileLoader("silkworm_cli_ts", str(BASE / "bin" / "silkworm"))
    spec = importlib.util.spec_from_loader("silkworm_cli_ts", loader)
    cli = importlib.util.module_from_spec(spec)
    loader.exec_module(cli)

    tmp = Path(tempfile.mkdtemp())
    bindir = tmp / "bin"; bindir.mkdir()
    log, status_f, serve_f = tmp / "calls", tmp / "status.json", tmp / "serve.json"
    fake = bindir / "tailscale"
    fake.write_text(f"""#!/bin/sh
echo "$*" >> "{log}"
case "$*" in
  "status --json") cat "{status_f}" ;;
  "serve status --json") cat "{serve_f}" ;;
esac
exit 0
""")
    fake.chmod(0o755)
    saved_env = {k: os.environ.get(k) for k in ("PATH", "VIZ_TAILSCALE_LOGIN", "SILKWORM_VIZ_PORT")}
    dotenv = {}                                  # stands in for .env
    cli.env_file_value = lambda name: dotenv.get(name)
    saved_app = cli.tailnet.TAILSCALE_APP
    cli.tailnet.TAILSCALE_APP = tmp / "NoSuch.app" / "Tailscale"

    def run(*argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = cli.do_tailscale(list(argv))
        return rc, buf.getvalue()

    def world(login="owner@example.com", proxies=("http://127.0.0.1:8790",),
              funnel=False, state="Running", certs=True, **extra):
        status_f.write_text(json.dumps({
            "BackendState": state,
            "Self": {"UserID": 42, "DNSName": "mini.tail1234.ts.net."},
            "User": {"42": {"LoginName": login}},
            "CertDomains": ["mini.tail1234.ts.net"] if certs else None}))
        serve_f.write_text(json.dumps({
            "TCP": {"443": {"HTTPS": True}},
            "Web": {"mini.tail1234.ts.net:443": {"Handlers": {
                f"/{i}" if i else "/": {"Proxy": p} for i, p in enumerate(proxies)}}},
            "AllowFunnel": {"mini.tail1234.ts.net:443": True} if funnel else {},
            **extra}))

    try:
        os.environ["SILKWORM_VIZ_PORT"] = "8790"
        os.environ["PATH"] = "/usr/bin:/bin"          # no tailscale anywhere
        rc, out = run("status")
        check("not installed: status prints install steps and exits 0",
              rc == 0 and "not installed" in out and "HTTPS Certificates" in out
              and "MagicDNS" in out, out)
        rc, out = run("on")
        check("not installed: on prints the steps and exits 1, running nothing",
              rc == 1 and "not installed" in out and not log.exists(), out)

        os.environ["PATH"] = f"{bindir}:/usr/bin:/bin"
        dotenv["VIZ_TAILSCALE_LOGIN"] = "owner@example.com"
        world()
        rc, out = run("status")
        check("all set: status passes and shows the URL",
              rc == 0 and "https://mini.tail1234.ts.net/" in out and "✘" not in out, out)
        rc, out = run()
        check("bare `silkworm tailscale` is status", rc == 0, out)
        dotenv["VIZ_TAILSCALE_LOGIN"] = "someone@else.com"
        rc, out = run("status")
        check("a VIZ_TAILSCALE_LOGIN that is not this machine's login fails status",
              rc == 1 and "matches this machine's login" in out, out)
        dotenv.pop("VIZ_TAILSCALE_LOGIN")
        rc, out = run("status")
        check("an unset VIZ_TAILSCALE_LOGIN fails status", rc == 1 and "refuses" in out, out)
        os.environ["VIZ_TAILSCALE_LOGIN"] = "owner@example.com"
        rc, out = run("status")
        check("a login exported in the shell but not in .env (the service reads .env) fails",
              rc == 1 and "set in .env" in out, out)
        os.environ.pop("VIZ_TAILSCALE_LOGIN")
        dotenv["VIZ_TAILSCALE_LOGIN"] = "owner@example.com"
        for label, kw in (("serve pointing elsewhere", {"proxies": ("http://127.0.0.1:3000",)}),
                          ("serve also exposing the bot", {"proxies": (
                              "http://127.0.0.1:8790", "http://127.0.0.1:8787")}),
                          ("Funnel on", {"funnel": True}),
                          ("a raw TCP forward (no identity headers)", {"TCP": {
                              "8443": {"TCPForward": "127.0.0.1:8790"}}}),
                          ("a file handler", {"Web": {"mini.tail1234.ts.net:443": {"Handlers": {
                              "/": {"Proxy": "http://127.0.0.1:8790"}, "/f": {"Path": "/"}}}}}),
                          ("a foreground Funnel session", {"Foreground": {"abc": {
                              "AllowFunnel": {"mini.tail1234.ts.net:443": True}}}}),
                          ("a Service proxying the bot", {"Services": {"svc:bot": {"Web": {
                              "bot:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}}}}}}),
                          ("logged out", {"state": "NeedsLogin"}),
                          ("no HTTPS certificates", {"certs": False})):
            world(**kw)
            rc, out = run("status")
            check(f"{label} fails status", rc == 1, out)

        world()
        log.unlink()
        rc, out = run("on")
        calls = log.read_text().splitlines()
        check("on runs `tailscale serve --bg 8790`, then checks",
              rc == 0 and calls[0] == "serve --bg 8790", str(calls))
        rc, out = run("off")
        calls = log.read_text().splitlines()
        check("off runs `tailscale serve reset`", rc == 0 and calls[-1] == "serve reset", str(calls))
        check("nothing ever asks for Funnel", not any("funnel" in c for c in calls), str(calls))
        rc, out = run("funnel")
        check("an unknown subcommand is a usage error", rc == 2, out)
        src = (BASE / "bin" / "silkworm").read_text()
        check("main dispatches `tailscale` with the args after the subcommand",
              'sys.exit(do_tailscale(args[1:]))' in src)
        world(Web={"old-name.tail1234.ts.net:443": {"Handlers": {
            "/": {"Proxy": "http://127.0.0.1:8790"}}}})
        rc, out = run("status")
        check("serve under a name this machine no longer has fails, naming both",
              rc == 1 and "old-name.tail1234.ts.net" in out
              and "this machine's name (mini.tail1234.ts.net)" in out
              and "dashboard: https://" not in out, out)
    finally:
        cli.tailnet.TAILSCALE_APP = saved_app
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# The dashboard as a home-screen app (Tailscale plan, step 2): a manifest,
# icons drawn from logo.svg, and a service worker that never keeps the
# dashboard -- stale task state is worse than an offline error -- but explains,
# when the page cannot be reached, that Tailscale is probably off. Its one
# cache, that offline page, is named for the build, so a deploy replaces it.
def _png_header(data: bytes):
    """(width, height, colour type) of a PNG, or None if it is not one."""
    import struct
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    w, h, _depth, ctype = struct.unpack(">IIBB", data[16:26])
    return w, h, ctype


def test_viz_web_app_routes():
    import re
    import threading
    import urllib.error
    import urllib.request
    import visualizer as V
    print("\nthe dashboard serves a manifest, its icons and a service worker, behind the gate")

    saved_env = os.environ.pop("VIZ_TAILSCALE_LOGIN", None)
    saved = (V.LOOPBACK, V.TOKEN, V.SESSIONS_FILE, V.ENV_FILE, V.bot_call)
    tmp = Path(tempfile.mkdtemp())
    (tmp / "sessions.json").write_text("{}")
    srv = None
    try:
        V.LOOPBACK, V.TOKEN = True, ""
        V.SESSIONS_FILE = tmp / "sessions.json"
        V.ENV_FILE = tmp / "no.env"
        V.bot_call = lambda path, payload, timeout=1.0: {"ok": True}
        srv = V.make_server("127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]

        def get(path, headers=None):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, dict(r.headers), r.read()
            except urllib.error.HTTPError as e:
                return e.code, dict(e.headers), e.read()

        def via_serve(login):
            return {"X-Forwarded-For": "100.101.102.103",
                    "X-Forwarded-Host": "mini.tail1234.ts.net", "X-Forwarded-Proto": "https",
                    "Tailscale-User-Login": login}

        # The page points at all of it.
        head = V.PAGE[:V.PAGE.index("</head>")]
        manifest_href = re.search(r'<link rel="manifest" href="([^"]+)"', head)
        touch_href = re.search(r'<link rel="apple-touch-icon" href="([^"]+)"', head)
        check("the page links a manifest and an apple-touch-icon",
              bool(manifest_href and touch_href), head[-600:])
        check("the manifest link sends credentials (token mode keeps its cookie)",
              'crossorigin="use-credentials"' in head)
        check("the viewport reaches under the notch (viewport-fit=cover) for safe-area insets",
              "viewport-fit=cover" in head)
        check("standalone on iOS: apple-mobile-web-app-capable",
              '<meta name="apple-mobile-web-app-capable" content="yes">' in head)

        code, hdrs, body = get(manifest_href.group(1))
        check("the manifest is served as application/manifest+json",
              code == 200 and hdrs.get("Content-Type") == "application/manifest+json",
              f"{code} {hdrs.get('Content-Type')}")
        m = json.loads(body)
        bg = re.search(r"--bg:\s*(#[0-9A-Fa-f]{6})", V.PAGE).group(1)
        check("named Silkworm, standalone, starting at /",
              m.get("name") == "Silkworm" and m.get("display") == "standalone"
              and m.get("start_url") == "/", str(m))
        check("theme and background are the page's own --bg",
              m.get("theme_color") == bg and m.get("background_color") == bg
              and f'<meta name="theme-color" content="{bg}">' in head, f"{m} {bg}")
        sizes = sorted(int(i["sizes"].split("x")[0]) for i in m.get("icons", []))
        check("the manifest names 180, 192 and 512 px icons", sizes == [180, 192, 512], str(sizes))

        # Every icon the manifest names is served, at the size it claims.
        for icon in m["icons"]:
            code, hdrs, data = get(icon["src"])
            w, h = (int(x) for x in icon["sizes"].split("x"))
            check(f"{icon['src']} is served as a {icon['sizes']} PNG",
                  code == 200 and hdrs.get("Content-Type") == "image/png"
                  and (_png_header(data) or (0, 0))[:2] == (w, h) and icon["type"] == "image/png",
                  f"{code} {hdrs.get('Content-Type')} {_png_header(data)}")
        for path in (touch_href.group(1), "/apple-touch-icon.png",
                     "/apple-touch-icon-precomposed.png"):
            code, hdrs, data = get(path)
            check(f"{path}: 180px, flattened RGB with no alpha channel",
                  code == 200 and hdrs.get("Content-Type") == "image/png"
                  and _png_header(data) == (180, 180, 2), f"{code} {_png_header(data)}")

        code, hdrs, sw = get("/sw.js")
        check("/sw.js is served as JavaScript, at the root (scope /), never held stale",
              code == 200 and hdrs.get("Content-Type", "").startswith("text/javascript")
              and "no-cache" in hdrs.get("Cache-Control", ""),
              f"{code} {hdrs}")
        js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
        check("the page registers /sw.js at scope /",
              'navigator.serviceWorker.register("/sw.js", {scope: "/"})' in js)

        # Behind the step-1 gate, like every route.
        os.environ["VIZ_TAILSCALE_LOGIN"] = "owner@example.com"
        new = ["/manifest.webmanifest", "/sw.js", *V.ICONS]
        let_in = [p for p in new if get(p, via_serve("intruder@example.com"))[0] != 403]
        check("a wrong Tailscale login gets 403 on the manifest, worker and every icon",
              not let_in, str(let_in))
        refused = [p for p in new if get(p, via_serve("owner@example.com"))[0] != 200]
        check("the owner's login gets all of them", not refused, str(refused))
        routes = _viz_routes("do_GET")
        check("the gate tests' route list includes the new routes",
              all(p in routes for p in new), str(routes))
    finally:
        V.LOOPBACK, V.TOKEN, V.SESSIONS_FILE, V.ENV_FILE, V.bot_call = saved
        if srv is not None:
            srv.shutdown(); srv.server_close()
        if saved_env is None:
            os.environ.pop("VIZ_TAILSCALE_LOGIN", None)
        else:
            os.environ["VIZ_TAILSCALE_LOGIN"] = saved_env


def test_viz_icons_are_generated_from_the_logo():
    import subprocess as _sp
    print("\nthe committed icons are exactly what assets/make_icons.py draws from logo.svg")
    gen = BASE / "assets" / "make_icons.py"
    r = _sp.run([sys.executable, str(gen), "--check"], capture_output=True, text=True, timeout=120)
    check("make_icons.py --check: the PNGs match logo.svg", r.returncode == 0,
          (r.stdout + r.stderr)[-400:])
    sys.path.insert(0, str(gen.parent))
    try:
        import make_icons
    finally:
        sys.path.remove(str(gen.parent))
    logo = (gen.parent / "logo.svg").read_text()
    try:
        make_icons.shapes(logo.replace('<circle cx="118"', '<circle transform="scale(2)" cx="118"'))
        refused = False
    except ValueError:
        refused = True
    check("an SVG attribute the generator cannot draw is refused, not ignored", refused)
    import visualizer as V
    missing = [p for p, (name, _) in V.ICONS.items()
               if not (BASE / "assets" / "icons" / name).exists()]
    check("every icon visualizer serves is a committed file", not missing, str(missing))


def test_viz_service_worker_versions_its_cache():
    import shutil
    import subprocess as _sp
    import visualizer as V
    print("\nthe service worker is versioned by the build: pages network-first, one cache, "
          "the offline page, and a new build deletes the old one")
    src = V.SERVICE_WORKER_SRC
    sw = V.SERVICE_WORKER.decode()
    check("the worker served is stamped with this build",
          f'const VERSION = "{V.BUILD}";' in sw and "__BUILD__" not in sw, sw[:200])
    check("the offline page asks about Tailscale",
          "Can't reach Silkworm — is Tailscale connected?" in sw)
    node = shutil.which("node")
    if not node:
        print("  … node not installed — the worker's handlers checked by text only")
        return
    # Two builds' workers, one after the other, over one browser's caches --
    # as a deploy does it.
    harness = r"""
const store = new Map([["old-shell", new Map([["/", "a stale page"]])]]);
let netFails = false, cachesFail = false;
const NET = {network: true};
globalThis.fetch = async req => { if (netFails) throw new TypeError("Failed to fetch"); return NET; };
globalThis.caches = {
  keys: async () => [...store.keys()],
  delete: async k => store.delete(k),
  open: async k => { if (cachesFail) throw new Error("quota");
    if (!store.has(k)) store.set(k, new Map());
    const c = store.get(k);
    return {put: async (key, resp) => { c.set(key, resp); }}; },
  match: async (key, opts) => { if (cachesFail) throw new Error("quota");
    const c = store.get(opts && opts.cacheName); const r = c && c.get(key);
    return r ? r.clone() : undefined; },
};
function worker(text) {
  const listeners = {}, state = {skipped: false, claimed: false};
  const self = {addEventListener: (k, f) => { listeners[k] = f; },
                skipWaiting: async () => { state.skipped = true; },
                clients: {claim: async () => { state.claimed = true; }}};
  new Function("self", text)(self);
  return {listeners, state};
}
async function lifecycle(w) {
  const waits = [];
  w.listeners.install({waitUntil: p => waits.push(p)});
  await Promise.all(waits);
  w.listeners.activate({waitUntil: p => waits.push(p)});
  await Promise.all(waits);
}
async function dispatch(w, url, mode, method) {
  let answered = null;
  w.listeners.fetch({request: {url, mode, method: method || "GET"}, respondWith: p => { answered = p; }});
  return answered ? await Promise.resolve(answered) : null;
}
async function page(r) {
  return {status: r.status, type: r.headers.get("content-type"), body: await r.text()};
}
(async () => {
  const out = {};
  const A = worker(__A__), B = worker(__B__);
  await lifecycle(A);
  out.afterA = [...store.keys()];
  out.aState = A.state;
  out.onlineIsNetwork = (await dispatch(A, "/", "navigate")) === NET;
  out.cachedAfterOnline = [...(store.get("silkworm-aaaa") || new Map()).keys()];
  netFails = true;
  out.offlineA = await page(await dispatch(A, "/", "navigate"));
  const apis = [];
  for (const fail of [false, true]) {
    netFails = fail;
    for (const [u, mode, method] of [["/api/tasks", "cors", "POST"], ["/api/sessions", "same-origin"],
                                     ["/api/send", "same-origin", "POST"], ["/sw.js", "same-origin"],
                                     ["/icons/icon-192.png", "no-cors"]])
      apis.push(await dispatch(A, u, mode, method));
  }
  out.apisAnswered = apis.filter(a => a !== null).length;
  // The deploy: build B's worker installs and activates.
  netFails = false;
  await lifecycle(B);
  out.afterB = [...store.keys()];
  netFails = true;
  out.offlineB = await page(await dispatch(B, "/?thread=x", "navigate"));
  // No cache at all (private mode, quota): still the offline page.
  cachesFail = true;
  out.offlineNoCache = await page(await dispatch(B, "/", "navigate"));
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
""".replace("__A__", json.dumps(src.replace("__BUILD__", "aaaa"))) \
   .replace("__B__", json.dumps(src.replace("__BUILD__", "bbbb")))
    r = _sp.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    check("two builds' workers run under a stub ServiceWorker scope", r.returncode == 0,
          r.stderr[-600:])
    if r.returncode:
        return
    out = json.loads(r.stdout)
    check("installing build A takes over at once and leaves only its own cache "
          "(the old 'old-shell' is deleted)",
          out["afterA"] == ["silkworm-aaaa"] and out["aState"] == {"skipped": True, "claimed": True},
          str(out))
    check("an online page load is the network's own response, not a copy",
          out["onlineIsNetwork"] is True, str(out))
    check("the dashboard itself is never cached: the cache holds only the offline page",
          out["cachedAfterOnline"] == ["/__silkworm_offline__"], str(out["cachedAfterOnline"]))
    check("/api/*, actions, the worker and icons are never intercepted, online or off",
          out["apisAnswered"] == 0, str(out))
    check("a new build's worker deletes the old build's cache and keeps only its own",
          out["afterB"] == ["silkworm-bbbb"], str(out["afterB"]))
    for k in ("offlineA", "offlineB", "offlineNoCache"):
        off = out[k]
        check(f"offline ({k}), a page load gets a 503 HTML page about Tailscale",
              off["status"] == 503 and off["type"].startswith("text/html")
              and "is Tailscale connected?" in off["body"], str(off)[:300])


def test_viz_build_changes_with_the_code():
    import subprocess as _sp
    import visualizer as V
    print("\nthe build fingerprint is the served page and worker: change either and it changes")
    tmp = Path(tempfile.mkdtemp())
    try:
        src = (BASE / "visualizer.py").read_text()
        variants = {"same": src,
                    "page": src.replace("<title>Silkworm — Sessions</title>",
                                        "<title>Silkworm — Sessions!</title>", 1),
                    "worker": src.replace("// Silkworm dashboard service worker.",
                                          "// Silkworm dashboard service worker!", 1)}
        check("each variant really changes the code it names",
              variants["page"] != src and variants["worker"] != src
              and "Sessions!" not in V.SERVICE_WORKER_SRC)
        builds = {}
        for name, text in variants.items():
            d = tmp / name
            d.mkdir()
            (d / "visualizer.py").write_text(text)
            r = _sp.run([sys.executable, "-c",
                         "import sys; sys.path[:0] = [sys.argv[1], sys.argv[2]]; import visualizer as V;"
                         "print(V.BUILD, V.SERVICE_WORKER.decode().count(V.BUILD),"
                         " V.PAGE.count(V.BUILD))", str(d), str(BASE)],
                        capture_output=True, text=True, timeout=60, cwd=d)
            check(f"the {name} copy imports", r.returncode == 0, r.stderr[-400:])
            builds[name] = r.stdout.split()
        check("the same code is the same build (deterministic)",
              builds["same"][0] == V.BUILD, str(builds))
        check("a change to the page is a new build", builds["page"][0] != V.BUILD, str(builds))
        check("a change to the worker is a new build", builds["worker"][0] != V.BUILD, str(builds))
        check("the build is stamped into the worker and the page",
              all(int(b[1]) >= 1 and int(b[2]) >= 1 for b in builds.values()), str(builds))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_viz_page_is_revalidated_by_build():
    import threading
    import urllib.error
    import urllib.request
    import visualizer as V
    print("\nthe page is never held stale: no-cache, its ETag is the build, every answer names the build")
    saved = (V.LOOPBACK, V.TOKEN, V.SESSIONS_FILE, V.ENV_FILE, V.bot_call)
    saved_env = os.environ.pop("VIZ_TAILSCALE_LOGIN", None)
    tmp = Path(tempfile.mkdtemp())
    (tmp / "sessions.json").write_text("{}")
    srv = None
    try:
        V.LOOPBACK, V.TOKEN = True, ""
        V.SESSIONS_FILE, V.ENV_FILE = tmp / "sessions.json", tmp / "no.env"
        V.bot_call = lambda path, payload, timeout=1.0: {"ok": True}
        srv = V.make_server("127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]

        def get(path, headers=None):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, dict(r.headers), r.read()
            except urllib.error.HTTPError as e:
                return e.code, dict(e.headers), e.read()

        code, h, body = get("/")
        tag = f'"{V.BUILD}"'
        check("the page: 200, Cache-Control no-cache, ETag the build, X-Silkworm-Build",
              code == 200 and h.get("Cache-Control") == "no-cache" and h.get("ETag") == tag
              and h.get("X-Silkworm-Build") == V.BUILD, f"{code} {h}")
        check("the page's script carries the same build",
              f'const PAGE_BUILD = "{V.BUILD}";' in body.decode() and b"__BUILD__" not in body)
        code, h, body = get("/", {"If-None-Match": tag})
        check("revalidating the same build is a 304 with no body",
              code == 304 and body == b"" and h.get("ETag") == tag, f"{code} {h}")
        code, h, body = get("/", {"If-None-Match": '"some-older-build"'})
        check("an older build's ETag gets the new page", code == 200 and len(body) > 1000, str(code))
        code, h, _ = get("/sw.js")
        check("the worker is no-cache and names the build",
              code == 200 and "no-cache" in h.get("Cache-Control", "")
              and h.get("X-Silkworm-Build") == V.BUILD, f"{code} {h}")
        code, h, body = get("/api/build")
        check("/api/build answers the build, never cached",
              code == 200 and json.loads(body) == {"build": V.BUILD}
              and h.get("Cache-Control") == "no-store", f"{code} {h} {body[:100]}")
        code, h, _ = get("/api/sessions")
        etag = h.get("ETag")
        code, h, _ = get("/api/sessions", {"If-None-Match": etag})
        check("a 304 from a polled read still names the build",
              code == 304 and h.get("X-Silkworm-Build") == V.BUILD, f"{code} {h}")
        # Token mode: the answer to ?token= must carry its cookie, so it is
        # never a 304.
        V.LOOPBACK, V.TOKEN = False, "tok123"
        code, h, _ = get("/?token=tok123", {"If-None-Match": tag})
        check("?token= is answered in full, cookie and all, even when the build is held",
              code == 200 and "silkworm_token=tok123" in h.get("Set-Cookie", ""), f"{code} {h}")
    finally:
        V.LOOPBACK, V.TOKEN, V.SESSIONS_FILE, V.ENV_FILE, V.bot_call = saved
        if srv is not None:
            srv.shutdown(); srv.server_close()
        if saved_env is not None:
            os.environ["VIZ_TAILSCALE_LOGIN"] = saved_env


#: The page's own script, loaded in node over a small DOM that keeps each
#: element's state, a sessionStorage that outlives the "load" (it is passed in
#: and handed back), and a fetch that answers /api/build and /api/probe with
#: the build the "server" is serving. __JS__ is the script, __STATE__ the
#: load's inputs, __SCENARIO__ what happens after it loads.
UPDATE_HARNESS = r"""
process.on("unhandledRejection", () => {});
const STATE = __STATE__;
const els = {}, docListeners = {}, winListeners = {}, replaced = [], updates = [];
function mk(id) {
  const cls = new Set();
  return {id, style: {}, dataset: {}, value: "", children: [], placeholder: "", rows: 0,
    classList: {add: c => cls.add(c), remove: c => cls.delete(c), contains: c => cls.has(c),
                toggle(c, on) { (on === undefined ? !cls.has(c) : on) ? cls.add(c) : cls.delete(c); }},
    addEventListener(){}, removeEventListener(){}, querySelectorAll: () => [], querySelector: () => null,
    appendChild(){}, insertAdjacentHTML(){}, set innerHTML(v){}, get innerHTML(){ return ""; },
    scrollIntoView(){}, focus(){}, setAttribute(){}, getBoundingClientRect: () => ({top: 0, height: 0}),
    dispatchEvent(){}, scrollHeight: 0, scrollTop: 0, clientHeight: 0};
}
globalThis.document = {getElementById: id => els[id] || (els[id] = mk(id)),
  addEventListener: (k, f) => { (docListeners[k] = docListeners[k] || []).push(f); },
  querySelectorAll: () => [], querySelector: () => null, createElement: mk, hidden: false,
  body: mk("body"), documentElement: mk("html")};
globalThis.history = {state: null, replaceState(s) { this.state = s; }, pushState(s) { this.state = s; },
                      back(){}};
const store = new Map(Object.entries(STATE.session || {}));
globalThis.sessionStorage = {getItem: k => store.has(k) ? store.get(k) : null,
  setItem: (k, v) => { store.set(k, String(v)); }, removeItem: k => { store.delete(k); }};
globalThis.localStorage = {getItem: () => null, setItem(){}, removeItem(){}};
globalThis.location = {search: STATE.search || "", href: "http://x/" + (STATE.search || ""),
                       replace: u => { replaced.push(u); }, reload: () => { replaced.push("reload"); }};
globalThis.window = {addEventListener: (k, f) => { winListeners[k] = f; }, history: globalThis.history,
                     location: globalThis.location, innerHeight: 800, innerWidth: 390};
globalThis.matchMedia = q => ({matches: false, addEventListener(){}});
const swListeners = {};
Object.defineProperty(globalThis, "navigator", {configurable: true, value: {userAgent: "node",
  serviceWorker: {register: () => Promise.resolve(), addEventListener: (k, f) => { swListeners[k] = f; },
    getRegistration: () => Promise.resolve({update: () => { updates.push(1); return Promise.resolve(); }})}}});
// Timers never fire, except the update's settle delay, which runs when the
// scenario calls settleUpdate() -- so a scenario can act inside the window.
const settles = [];
globalThis.setInterval = () => 0; globalThis.clearTimeout = () => {};
globalThis.setTimeout = (f, ms) => { if (ms === __SETTLE__) settles.push(f); return 0; };
const settleUpdate = () => { while (settles.length) settles.shift()(); };
globalThis.requestAnimationFrame = () => 0;
let served = STATE.served;
const answer = url => ({status: 200,
  headers: {get: k => k === "X-Silkworm-Build" ? served : null},
  json: async () => url.startsWith("/api/build") ? {build: served} : {}});
globalThis.fetch = url => (url.startsWith("/api/build") || url.startsWith("/api/probe"))
  ? Promise.resolve(answer(url)) : new Promise(() => {});
const settle = () => new Promise(ok => setImmediate(ok));
(0, eval)(__JS__ + `
;globalThis.__p = {setActive: a => { active = a; }, getActive: () => active, applyUpdate,
                   readJSON, staged, build: PAGE_BUILD};`);
const P = globalThis.__p;
const fire = (k) => (docListeners[k] || []).forEach(f => f({}));
(async () => {
  const out = {};
  await settle();
  __SCENARIO__
  await settle(); await settle();
  settleUpdate();
  await settle();
  out.replaced = replaced;
  out.banner = els.updbar ? els.updbar.classList.contains("on") : false;
  out.reply = els.reply ? els.reply.value : "";
  out.session = Object.fromEntries(store);
  out.updates = updates.length;
  out.build = P.build;
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


def test_viz_page_reloads_onto_a_new_build():
    import re
    import shutil
    import subprocess as _sp
    import visualizer as V
    print("\na deploy reaches an open page: it notices the new build and reloads once onto it, "
          "or asks first when something is being typed -- and the draft survives")
    node = shutil.which("node")
    if not node:
        print("  … node not installed — skipped")
        return
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    OLD, NEW = V.BUILD, "f00dfacecafe0001"
    new_js = js.replace(f'const PAGE_BUILD = "{OLD}";', f'const PAGE_BUILD = "{NEW}";')
    settle_ms = re.search(r"const UPDATE_SETTLE_MS = (\d+);", js).group(1)
    check("the page's script has one PAGE_BUILD to stamp", new_js != js and new_js.count(NEW) == 1)

    def load(script, served, scenario, session=None, search=""):
        h = (UPDATE_HARNESS.replace("__JS__", json.dumps(script))
             .replace("__STATE__", json.dumps({"served": served, "session": session or {},
                                               "search": search}))
             .replace("__SCENARIO__", scenario)
             .replace("__SETTLE__", settle_ms))
        r = _sp.run([node, "-e", h], capture_output=True, text=True, timeout=60)
        check("the page's script runs in the update harness", r.returncode == 0, r.stderr[-800:])
        return json.loads(r.stdout) if r.returncode == 0 else None

    # Same build everywhere: nothing happens.
    out = load(js, OLD, 'fire("visibilitychange"); await P.readJSON("/api/probe");')
    if out is None:
        return
    check("the same build: no reload, no banner",
          out["replaced"] == [] and out["banner"] is False and out["build"] == OLD, str(out))

    # The deploy, noticed on coming back to the screen (iOS resumes the app).
    out = load(js, NEW, 'fire("visibilitychange");')
    check("back on screen after a deploy, an idle page with nothing typed reloads once, to /",
          out["replaced"] == ["/"] and out["banner"] is False, str(out))
    check("it asks the worker to update and remembers which build it reloaded for",
          out["updates"] >= 1 and out["session"].get("silkworm-updated-to") == NEW, str(out))
    # ... or by any polled read's header.
    out = load(js, NEW, 'await P.readJSON("/api/probe");')
    check("a polled read's X-Silkworm-Build is enough to notice it", out["replaced"] == ["/"], str(out))

    # Next load: the new code, served the new build -- no second reload.
    out = load(new_js, NEW, 'fire("visibilitychange"); await P.readJSON("/api/probe");',
               session={"silkworm-updated-to": NEW})
    check("the reloaded page runs the new build and does not reload again",
          out["build"] == NEW and out["replaced"] == [] and out["banner"] is False, str(out))
    check("and forgets the reload marker", "silkworm-updated-to" not in out["session"], str(out))

    # Reloaded for NEW and still served the old page: a banner, never a loop.
    out = load(js, NEW, 'fire("visibilitychange");', session={"silkworm-updated-to": NEW})
    check("reloaded once and still old: banner, not a reload loop",
          out["replaced"] == [] and out["banner"] is True, str(out))

    # Someone mid-message: a banner, and the draft goes with the reload.
    out = load(js, NEW, '''
      P.setActive({key: "C1:123.456"});
      document.getElementById("reply").value = "half a thought";
      fire("visibilitychange");
      await settle(); await settle(); settleUpdate();
      out.bannerFirst = document.getElementById("updbar").classList.contains("on");
      out.replacedFirst = replaced.slice();
      P.applyUpdate();''')
    check("a draft in the composer: the banner, no reload under it",
          out["bannerFirst"] is True and out["replacedFirst"] == [], str(out))
    check("tapping it reloads onto the same thread and keeps the draft",
          out["replaced"] == ["/?thread=C1%3A123.456"]
          and json.loads(out["session"].get("silkworm-draft", "{}"))
          == {"key": "C1:123.456", "text": "half a thought"}, str(out))
    carried = out["session"]
    out = load(new_js, NEW, "", session=carried, search="?thread=C1%3A123.456")
    check("after the reload the draft is back in the composer, once",
          out["reply"] == "half a thought" and "silkworm-draft" not in out["session"]
          and out["replaced"] == [], str(out))

    # Touched a moment ago, files picked, a form open: banner, not a reload.
    # Back from a notification tap: the build check and the worker's "open"
    # message race; the open lands inside the settle window -> banner, the task stays.
    out = load(js, NEW, '''
      fire("visibilitychange");
      await settle(); await settle();
      swListeners.message({data: {type: "open", url: "/?task=tsk_1"}});''')
    check("a notification tap landing while a reload waits: the banner, no reload",
          out["replaced"] == [] and out["banner"] is True, str(out))
    # A thread's notification opens no panel: the tap itself must count.
    out = load(js, NEW, '''
      fire("visibilitychange");
      await settle(); await settle();
      swListeners.message({data: {type: "open", url: "/?thread=C9%3A1.2"}});''')
    check("a thread notification landing while a reload waits: the banner, no reload",
          out["replaced"] == [] and out["banner"] is True, str(out))
    out = load(js, NEW, 'fire("visibilitychange"); await settle(); await P.readJSON("/api/probe");'
                        'await P.readJSON("/api/probe");')
    check("the worker is asked to update once per new build, not once per poll",
          out["updates"] == 1 and out["replaced"] == ["/"], str(out))
    for name, setup in (("touched", 'fire("pointerdown");'),
                        ("scrolled", 'fire("scroll");'),
                        ("board open", 'document.getElementById("boardmodal").style.display = "flex";'),
                        ("task panel open", 'document.getElementById("taskmodal").style.display = "flex";'),
                        ("files", 'P.staged.reply.push({name: "a.png", size: 1});'),
                        ("form", 'document.getElementById("fmodal").style.display = "flex";')):
        out = load(js, NEW, setup + ' fire("visibilitychange");')
        check(f"{name}: the banner, no reload", out["replaced"] == [] and out["banner"] is True, str(out))


def test_viz_phone_layout():
    import re
    import shutil
    import subprocess as _sp
    import visualizer as V
    print("\nthe dashboard fits a 390px phone: list and thread take turns, panels go full screen")
    css = re.search(r"<style>(.*?)</style>", V.PAGE, re.S).group(1)
    at = css.index("@media (max-width: 700px)")
    desktop, phone = css[:at], css[at:]

    # Any fixed width or minimum a phone cannot hold must be undone below 700px:
    # a phone rule naming the same selector must set the same property.
    prop_re = r"(?:^|;)\s*((?:min-)?width|grid-template-columns)\s*:\s*([^;]+)"

    def rules(text):
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
            sel = re.sub(r"/\*.*?\*/", "", sel, flags=re.S)
            for part in sel.split(","):
                yield " ".join(part.split()), body
    phone_sets = {(sel, prop) for sel, body in rules(phone[phone.index("{") + 1:])
                  for prop, _ in re.findall(prop_re, body)}
    too_wide = []
    for sel, body in rules(desktop):
        for prop, val in re.findall(prop_re, body):
            px = [int(n) for n in re.findall(r"(\d+)px", val)]
            if "min(" not in val and px and (max(px) >= 200 or prop == "grid-template-columns"):
                too_wide.append((sel, prop, val.strip()))
    unhandled = [w for w in too_wide if (w[0], w[1]) not in phone_sets]
    check("every desktop width of 200px+ (and every fixed grid) is overridden on a phone",
          len(too_wide) >= 5 and not unhandled, f"unhandled: {unhandled}; found {too_wide}")
    for rule in ("body.reading .listcol { display: none; }", "body.reading main { display: flex; }",
                 ".bcols { grid-template-columns: 1fr;", ".task { flex-wrap: wrap;"):
        check(f"phone rule present: {rule}", rule in phone)
    check("panels become the full screen on a phone",
          all(s in phone for s in ("#taskmodal .box", "#boardmodal .box", "#learnmodal .box",
                                   "#fmodal .fbox")) and "height: 100%" in phone)
    check("safe-area insets are respected (top, bottom, sides)",
          all(f"env(safe-area-inset-{s})" in css for s in ("top", "bottom", "left", "right")))
    field = re.search(r"--field-fs:\s*(\d+)px", phone)
    check("inputs are 16px on a phone, so focusing one does not zoom the page",
          field is not None and int(field.group(1)) >= 16
          and re.search(r"input, select, textarea[^{]*\{ font-size: var\(--field-fs\); \}", phone)
          is not None)
    tap = re.search(r"--tap:\s*(\d+)px", phone)
    check("touch targets are at least 44px on a phone", tap is not None and int(tap.group(1)) >= 44)
    check("on a phone the board shows one column, picked from tabs",
          ".bcols .bcol { display: none;" in phone and ".bpick { display: flex;" in phone
          and all(f'.bcols[data-show="{k}"] .bcol[data-col="{k}"]' in phone
                  for k in ("backlog", "running", "review", "needs", "done")))
    check("the kind filter is one sideways-scrolling row on a phone",
          re.search(r"#kindfilter \{\s*flex-wrap: nowrap; overflow-x: auto;", phone) is not None)
    check("the header is one row: search and the rest behind phone-only buttons",
          "header { position: relative; flex-wrap: nowrap;" in phone
          and 'onclick="toggleSearch()"' in V.PAGE and 'onclick="toggleMenu(event)"' in V.PAGE
          and '<div class="hmore" onclick="closeMenu()">' in V.PAGE)
    check("a laptop never sees the phone's controls",
          ".phoneonly, .bpick { display: none; }" in desktop and ".hmore { display: contents; }" in desktop)
    check("form buttons are pinned to the bottom of a phone sheet",
          re.search(r"\.tform \.acts \{ position: sticky; bottom: 0;", phone) is not None)
    for panel, fn in (("taskmodal", "toggleTasks()"), ("boardmodal", "toggleBoard()"),
                      ("learnmodal", "toggleLearn()")):
        start = V.PAGE.index(f'<div id="{panel}"')
        chunk = V.PAGE[start:start + 400]
        check(f"#{panel} has its own ✕ (a full-screen panel has no backdrop to tap)",
              f'class="ghost xclose" onclick="{fn}"' in chunk, chunk)
    check("the open thread has a way back to the list", 'onclick="closeSession()"' in V.PAGE)

    node = shutil.which("node")
    if not node:
        print("  … node not installed — showReading/closeSession checked by text only")
        return
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    harness = r"""
const classes = new Set(), pushed = [], winListeners = {};
let backs = 0, wide = false, registered = null;
const el = () => ({style: {}, classList: {add(){}, remove(){}, contains: () => false},
  addEventListener(){}, querySelectorAll: () => [], appendChild(){}, children: [],
  insertAdjacentHTML(){}, set innerHTML(v){}, get innerHTML(){ return ""; },
  scrollIntoView(){}, dataset: {}, value: ""});
globalThis.document = {getElementById: el, addEventListener(){}, querySelectorAll: () => [],
  createElement: el, hidden: false,
  body: {classList: {add: c => classes.add(c), remove: c => classes.delete(c),
                     contains: c => classes.has(c)}}};
// As after a reload with a thread open: the entry is still there.
let replaced = 0;
globalThis.history = {state: {reading: true},
  replaceState(s) { replaced++; this.state = s; },
  pushState(s) { pushed.push(s); this.state = s; },
  back() { backs++; this.state = null; winListeners.popstate(); }};
globalThis.window = {addEventListener: (k, f) => { winListeners[k] = f; }, history: globalThis.history,
                     location: {search: ""}};
globalThis.matchMedia = q => ({matches: !wide && q === "(max-width: 700px)"});
Object.defineProperty(globalThis, "navigator", {value: {serviceWorker: {
  register: (u, o) => { registered = [u, o]; return Promise.resolve(); }}}, configurable: true});
globalThis.localStorage = {getItem: () => null, setItem(){}};
globalThis.setInterval = () => 0; globalThis.setTimeout = () => 0;
// Never answers: the page's own loads stay pending, which is all this needs.
globalThis.fetch = () => new Promise(() => {});
(0, eval)(__JS__ + "\n;globalThis.__p = {showReading, closeSession, getActive: () => active, setActive: a => { active = a; }};");
const P = globalThis.__p, out = {};
out.staleCleared = replaced === 1 && history.state === null;
P.setActive({key: "k"});
P.showReading();
out.afterOpen = {reading: classes.has("reading"), pushed: pushed.length};
P.showReading();
out.reopenPushes = pushed.length;
P.closeSession();
out.afterBack = {reading: classes.has("reading"), backs, active: P.getActive()};
P.setActive({key: "k"}); P.showReading();
winListeners.popstate();
out.afterGesture = {reading: classes.has("reading"), active: P.getActive()};
wide = true; pushed.length = 0;
P.showReading(); out.widePushes = pushed.length;
out.registered = registered;
process.stdout.write(JSON.stringify(out));
""".replace("__JS__", json.dumps(js))
    r = _sp.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    check("the page's own script runs in the phone harness", r.returncode == 0, r.stderr[-600:])
    if r.returncode:
        return
    out = json.loads(r.stdout)
    check("opening a thread on a phone shows it and pushes one history entry",
          out["afterOpen"] == {"reading": True, "pushed": 1} and out["reopenPushes"] == 1, str(out))
    check("‹ Sessions goes back: list shown, nothing active (the transcript poll stops)",
          out["afterBack"] == {"reading": False, "backs": 1, "active": None}, str(out))
    check("the system back gesture (popstate) does the same",
          out["afterGesture"] == {"reading": False, "active": None}, str(out))
    check("a laptop-width window pushes no history", out["widePushes"] == 0, str(out))
    check("a reload with a thread's entry still in history clears it (no dead back step)",
          out["staleCleared"] is True, str(out))
    check("the service worker is registered at scope /",
          out["registered"] == ["/sw.js", {"scope": "/"}], str(out["registered"]))


CHAT_HARNESS = r"""
// A small DOM: elements hold their markup, children, classes and style.
function mk(id) {
  const cls = new Set();
  const e = {id, style: {}, dataset: {}, value: "", placeholder: "", children: [], _inner: "",
    scrollHeight: 0, scrollTop: 0, clientHeight: 0, parent: null,
    classList: {add: c => cls.add(c), remove: c => cls.delete(c), contains: c => cls.has(c),
                toggle: (c, on) => { if (on === undefined ? !cls.has(c) : on) cls.add(c); else cls.delete(c); }},
    addEventListener(){}, querySelectorAll: () => [], querySelector(sel) {
      return sel === ".empty" && this._inner.includes('class="empty"') ? {} : null; },
    insertAdjacentHTML(_, h) { this._inner += h; },
    appendChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1);
                     this.children.push(c); c.parent = this; },
    remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); },
    scrollIntoView(){}, focus(){}, attrs: {}, setAttribute(k, v) { this.attrs[k] = String(v); }};
  Object.defineProperty(e, "innerHTML", {get() { return this._inner; },
    set(v) { this._inner = v; this.children.length = 0; this._sets = (this._sets || 0) + 1; }});
  return e;
}
// An element's markup with its children's: a turn's line is made of elements.
const D = e => (e._inner || "") + (e.children || []).map(D).join("");
const els = {};
const byId = id => els[id] || (els[id] = mk(id));
const bodyCls = new Set();
let wide = false;
globalThis.document = {getElementById: byId, addEventListener(){}, querySelectorAll: () => [],
  querySelector: s => s === "main" ? byId("__main") : null,
  createElement: () => mk(""), hidden: false,
  body: {classList: {add: c => bodyCls.add(c), remove: c => bodyCls.delete(c), contains: c => bodyCls.has(c),
                     toggle: (c, on) => { if (on === undefined ? !bodyCls.has(c) : on) bodyCls.add(c); else bodyCls.delete(c); }}}};
globalThis.history = {state: null, replaceState(){}, pushState(){}, back(){}};
globalThis.window = {addEventListener(){}, history: globalThis.history, location: {search: ""}};
let reduce = false;
globalThis.matchMedia = q => ({matches: (!wide && q === "(max-width: 700px)")
                                       || (reduce && q === "(prefers-reduced-motion: reduce)")});
// Frames and timers are held, and run only when a check says so.
const frames = [], later = [];
globalThis.requestAnimationFrame = f => frames.push(f);
const runFrames = () => { while (frames.length) frames.shift()(); };
Object.defineProperty(globalThis, "navigator", {value: {}, configurable: true});
globalThis.localStorage = {getItem: () => null, setItem(){}};
globalThis.setInterval = () => 0; globalThis.setTimeout = (f, ms) => { later.push([f, ms]); return 0; };
globalThis.fetch = () => new Promise(() => {});
globalThis.getComputedStyle = () => ({lineHeight: "22px", paddingTop: "10px", paddingBottom: "10px",
                                      borderTopWidth: "1px", borderBottomWidth: "1px"});
(0, eval)(__JS__ + `
;globalThis.__p = {chatItems, toolsLabel, chatHtml, drawChat, toggleTools, chatOpen, chatEls,
  toggleCall, callOpen, fmtMs, placeFiles, drawerHtml, openDrawer, closeDrawer, drawerIsOpen,
  threadFiles, renderChips, addFiles, dropFile, staged, viewImage, closeViewer, viewerIsOpen,
  alertParts, renderAlerts, alertCount: () => alertList.length, defaultKind, setKind,
  kind: () => threadKind, chatHead, growReply, jumpTo, keepEnd, cardHtml, freshAges, transcriptHead,
  answeredAgo: s => readCache.set("/api/sessions", {etag: "x", data: {}, at: Date.now() - s * 1000}),
  forgetAnswer: () => readCache.delete("/api/sessions"),
  setLast: s => { lastSessions = s; }, getActive: () => active, setActive: a => { active = a; },
  setTview: t => { tview = t; }, drawLive, liveHtml, toEnd, atEnd, onScroll};`);
const P = globalThis.__p, out = {};
const T = (role, ...blocks) => ({role, ts: "2026-10-15T10:00:00Z", blocks});
const U = {in: 10, out: 20, cache_read: 900, cache_create: 5};
const msgs = [
  T("user", {type: "text", text: "please fix it"}),
  T("assistant", {type: "tool", name: "Bash", input: "ls"}),
  T("user", {type: "tool_result", text: "a b"}),
  Object.assign(T("assistant", {type: "thinking", text: "hm"}, {type: "tool", name: "Read", input: "x"}),
                {usage: U, model: "claude-opus-5-5"}),
  T("user", {type: "tool_result", text: "file"}),
  T("assistant", {type: "tool", name: "Bash", input: "make"}),
  T("user", {type: "tool_result", text: "ok"}),
  Object.assign(T("assistant", {type: "text", text: "Fixed **it**."}), {usage: U, model: "claude-opus-5-5"}),
  T("assistant", {type: "text", text: "One more:"}, {type: "tool", name: "Edit", input: "e"}),
  T("user", {type: "tool_result", text: "done"}),
  T("assistant", {type: "text", text: "All done."}),
];
const items = P.chatItems(msgs);
out.shape = items.map(i => i.kind === "bubble" ? i.role + ":" + i.text : "tools:" + i.tools + ":" + i.names.join("+"));
out.ids = items.map(i => i.id);
out.prefixIds = P.chatItems(msgs.slice(0, 6)).map(i => i.id);
out.labels = items.filter(i => i.kind === "tools").map(P.toolsLabel);
out.thinkingOnly = P.toolsLabel(P.chatItems([T("assistant", {type: "thinking", text: "x"})])[0]);
const fixed = items[2];
out.bubbleUsage = !!fixed.usage && fixed.model === "claude-opus-5-5";
out.groupUsage = items[1].usage.some(u => u.usage && u.model);
const firstTools = items[1];
P.chatEls.clear();
const pre = byId("tmsgsPre");
P.drawChat(pre, msgs, "s");
const toolDiv = P.chatEls.get(firstTools.id);
const closed = D(toolDiv);
out.closed = {pre: closed.includes("<pre>"), usage: closed.includes('class="usage"'),
              label: closed.includes("Bash ×2 · Read ×1 · Edit ×1")};
P.chatOpen.add(firstTools.id);
P.callOpen.add(firstTools.id + "/0");
P.drawChat(pre, msgs, "s");
const opened = D(toolDiv);
out.opened = {pre: opened.includes("<pre>ls</pre>"), usage: opened.includes('class="usage"'),
              bash: opened.includes('class="cname">Bash<'), result: opened.includes("<pre>a b</pre>")};
P.chatOpen.clear(); P.callOpen.clear(); P.chatEls.clear();
out.bubbleHtml = P.chatHtml(fixed);
out.plainBubble = P.chatHtml(items[0]);
// Keyed drawing: earlier items keep their elements as the thread grows, and
// an opened tools line stays open when redrawn.
const box = byId("tmsgs");
P.setActive({key: "k"});
P.setTview({key: "k", sid: "s", msgs: msgs.slice(0, 6), live: null});
P.drawChat(box, msgs.slice(0, 6), "s");
const firstEls = box.children.slice();
out.drawn1 = firstEls.length;
P.toggleTools(firstTools.id);
out.openAfterToggle = D(box.children[1]).includes('class="chain"');
P.drawChat(box, msgs, "s");
out.drawn2 = box.children.length;
out.kept = firstEls.every((e, i) => box.children[i] === e);
out.stillOpen = D(box.children[1]).includes('class="chain"') && D(box.children[1]).includes("Edit");
P.drawChat(box, [], "");
out.empty = box.innerHTML.includes("No messages yet");
// Alerts: one count for the laptop's bar and the phone's ⚠.
const now = Date.now() / 1000;
const sess = [{key: "a", title: "A", turn: {stalled: true, age_s: 900}},
              {key: "b", title: "B", turn: {stalled: true, age_s: 60}},
              {key: "c", title: "C", cost_flag: {ratio: 9, last: 1, median: 0.1}},
              {key: "d", title: "D"}];
out.alertsAll = P.alertParts(sess, {ready: true, connected: false, down_for: 30},
                             {state: "stale", message: "3 behind"}).length;
out.alertsNone = P.alertParts([{key: "d"}], {ready: true, connected: true}, {state: "current"}).length;
out.alertsTwo = P.alertParts(sess, {}, {}).length;
P.renderAlerts(sess, {ready: true, connected: false}, {state: "stale", message: "x"});
out.slot = byId("alertslot").innerHTML;
out.bar = (byId("alertbar").innerHTML.match(/class="alert /g) || []).length;
out.sheet = (byId("alertlist").innerHTML.match(/class="aitem"/g) || []).length;
P.renderAlerts([], {}, {state: "current"});
out.slotEmpty = byId("alertslot").innerHTML;
// The phone opens the list on Conversations, when there are any.
P.defaultKind([{kind: "task"}, {kind: "thread"}]); out.kindPhone = P.kind();
P.defaultKind([{kind: "task"}, {kind: "thread", hidden: true}]); out.kindNoConv = P.kind();
P.defaultKind([{kind: "thread"}]);                    // a phone: Conversations
wide = true; P.defaultKind([{kind: "thread"}]); out.kindWideBefore = P.kind();
wide = false;
P.defaultKind([{kind: "thread"}]); P.setKind("task"); P.defaultKind([{kind: "thread"}]);
out.kindChosen = P.kind();
// jumpTo: a thread the list filters out is opened anyway.
P.setActive(null);
P.setLast([{key: "hidden-task", kind: "task", title: "T"}]);
wide = true; P.jumpTo("hidden-task"); out.jumpedWide = (P.getActive() || {}).key || null;
wide = false;
P.jumpTo("hidden-task");
out.jumped = (P.getActive() || {}).key || null;
// The thread header.
out.headRun = P.chatHead({title: "A <b> title", project: "silkworm", turn: {age_s: 125, stalled: false}},
                         {running: true, turns: [{state: "queued"}, {state: "running"}]}, 3);
out.headIdle = P.chatHead({title: "", project: "", updated: now - 120}, {turns: []}, 0);
// A stalled turn: five hours in, silent for the last two. Every place it is
// shown says how long it has been silent, not how old it is.
const stuck = () => ({key: "s", title: "S", turn: {age_s: 5 * 3600, silent_s: 2 * 3600 + 60,
                                                   stalled: true, child_alive: true}});
out.stallCard = P.cardHtml(stuck());
out.stallHead = P.chatHead(stuck(), {turns: []}, 0);
out.stallAlert = P.alertParts([stuck()], {}, {}).join("");
try { out.stallBar = (P.transcriptHead(Object.assign(stuck(), {resume_cmd: "x", slack_link: "y"})).match(/<b>[^<]*Turn[^<]*<\/b>/) || [""])[0]; }
catch (e) { out.stallBar = "threw: " + e.message; }
out.stallOld = P.cardHtml({key: "o", turn: {age_s: 600, stalled: true, child_alive: false}});
// Between full answers the page counts the silence on, as it does the age.
const answer = {sessions: [stuck()]};
P.answeredAgo(3600); P.freshAges(answer);
out.stallLater = [answer.sessions[0].turn.age_s, answer.sessions[0].turn.silent_s];
P.forgetAnswer();
// Composer: grows to six lines, then scrolls.
const reply = byId("reply");
reply.scrollHeight = 400; P.growReply();
out.tall = [reply.style.height, reply.style.overflowY];
reply.scrollHeight = 42; P.growReply();
out.short = [reply.style.height, reply.style.overflowY];
out.placeholder = reply.placeholder;
// Following the newest message: at the end it follows; read further up it
// stays put and shows the pill.
const main = byId("__main");
Object.assign(main, {scrollHeight: 1000, clientHeight: 500, scrollTop: 500});
P.keepEnd(() => { main.scrollHeight = 1300; });
out.followed = [main.scrollTop, byId("jumppill").classList.contains("show")];
main.scrollTop = 100; P.onScroll();                  // the reader scrolls up
P.keepEnd(() => { main.scrollHeight = 1600; });
out.stayed = [main.scrollTop, byId("jumppill").classList.contains("show")];
P.keepEnd(() => {}, true);
out.forced = [main.scrollTop, byId("jumppill").classList.contains("show")];
// Following the end, the page grows by more than the slack somewhere a draw
// did not see (the stats above the thread): the next draw still follows.
main.scrollTop = main.scrollHeight - main.clientHeight;   // at the end, as a browser clamps it
P.onScroll();
// Following the end, a poll that changed nothing moves nothing -- a reader
// who opened something near the end is not scrolled past it.
main.scrollTop -= 30;                                    // still within the slack: pinned
const stillAt = main.scrollTop;
P.keepEnd(() => {});
out.unchangedPoll = main.scrollTop === stillAt;
P.onScroll();
main.scrollHeight += 400;
P.keepEnd(() => { main.scrollHeight += 50; });
out.grewBetween = [main.scrollTop === main.scrollHeight, byId("jumppill").classList.contains("show")];
// Tapping the pill glides down; the glide's own scroll events, short of the
// end, do not count as the reader scrolling up -- a poll meanwhile follows.
main.scrollTo = () => {};
Object.assign(main, {scrollHeight: 3000, clientHeight: 500, scrollTop: 100});
P.toEnd(true);
main.scrollTop = 1500; P.onScroll();                     // mid-glide
P.keepEnd(() => { main.scrollHeight = 3200; });
out.glide = [main.scrollTop, byId("jumppill").classList.contains("show")];
delete main.scrollTo;
// On a laptop too: the same chat, and the same pill when read further up.
wide = true;
byId("jumppill").classList.toggle("show", false);
Object.assign(main, {scrollHeight: 1000, clientHeight: 500, scrollTop: 100}); P.onScroll();
P.keepEnd(() => { main.scrollHeight = 1400; });
out.wideStayed = [main.scrollTop, byId("jumppill").classList.contains("show")];
Object.assign(main, {scrollTop: 900}); P.onScroll();
P.keepEnd(() => { main.scrollHeight = 1500; });
out.wideFollowed = [main.scrollTop, byId("jumppill").classList.contains("show")];
let smooth = null;
main.scrollTo = o => { smooth = o; };
byId("jumppill").classList.toggle("show", true);
P.toEnd(true);
out.pillTap = [smooth && smooth.behavior, byId("jumppill").classList.contains("show")];
reduce = true; smooth = null; P.toEnd(true);
out.pillTapReduced = [smooth, main.scrollTop];
reduce = false; delete main.scrollTo;
wide = false;
// The working indicator: while a turn runs, a reply-shaped bubble; its
// markup the same however long it has run, so a poll does not redraw it.
const tl = byId("tlive");
const sRun = {key: "k", title: "K", turn: {age_s: 58}};
P.setActive(sRun);
P.setTview({key: "k", sid: "s", msgs: [], live: {running: true, turns: [{ref: "r0", state: "running"}]}});
P.drawLive();
out.runningLive = tl.innerHTML;
const runningMarkup = P.liveHtml(sRun, {running: true, turns: []}, []);
out.runningSteady = runningMarkup === P.liveHtml(Object.assign({}, sRun, {turn: {age_s: 125}}),
                                                {running: true, turns: []}, []);
P.setTview({key: "k", sid: "s", msgs: [], live: {running: false, turns: [{ref: "r0", state: "done"}]}});
P.drawLive();
out.idleLive = tl.innerHTML;
// Motion: an item arriving after the first draw rises in; a tools line
// unfolds a frame after its detail is drawn, and folds before it is dropped.
const box2 = byId("tmsgs2");
P.chatEls.clear(); P.chatOpen.clear();
P.setTview({key: "k", sid: "s", msgs: msgs.slice(0, 2), live: null});
P.drawChat(box2, msgs.slice(0, 2), "s");
out.firstDrawEnter = box2.children.map(c => c.classList.contains("enter"));
P.drawChat(box2, msgs.slice(0, 8), "s");
out.laterEnter = box2.children.map(c => c.classList.contains("enter"));
// A grown thread appends only what is new: moving an element drops a
// selection or focus inside it.
let moved = [];
const realAppend = box2.appendChild;
box2.appendChild = function (c) { moved.push(this.children.indexOf(c)); return realAppend.call(this, c); };
P.drawChat(box2, msgs, "s");
out.appendedOnly = moved;
box2.appendChild = realAppend;
P.drawChat(box2, msgs.slice(0, 8), "s");                // back as it was for what follows
out.kinds = box2.children.map(c => c.classList.contains("ctools") ? "tools" : c.classList.contains("cbubble") ? "bubble" : "?");
const toolsId = P.chatItems(msgs.slice(0, 8))[1].id;
const toolsEl = () => P.chatEls.get(toolsId);
P.setTview({key: "k", sid: "s", msgs: msgs.slice(0, 8), live: null});
// toggleTools redraws #tmsgs; point the check at what it draws there
P.chatEls.clear(); P.drawChat(byId("tmsgs"), msgs.slice(0, 8), "s");
// Opening a line is reading it: the next poll that grows the thread does not
// scroll it away, and offers the pill instead.
Object.assign(main, {scrollHeight: 2000, clientHeight: 500, scrollTop: 1500}); P.onScroll();
P.toggleTools(toolsId);
main.scrollHeight = 2300;                                // the detail unfolded below
P.keepEnd(() => { main.scrollHeight = 2400; });
out.openedNotYanked = [main.scrollTop, byId("jumppill").classList.contains("show")];
P.toggleTools(toolsId); for (const [f] of later.splice(0)) f(); runFrames();
P.toggleTools(toolsId);
out.openBeforeFrame = [toolsEl().classList.contains("open"), D(toolsEl()).includes('class="chain"')];
runFrames();
out.openAfterFrame = toolsEl().classList.contains("open");
later.length = 0;
P.toggleTools(toolsId);
out.closing = [toolsEl().classList.contains("open"), D(toolsEl()).includes('class="chain"'),
               later.map(l => l[1])];
for (const [f] of later.splice(0)) f();
out.closed2 = D(toolsEl()).includes('class="chain"');
// Reduced motion: nothing rises; open and closed at once, no frame or timer waited on.
reduce = true;
const box3 = byId("tmsgs3");
P.chatEls.clear(); P.chatOpen.clear();
P.drawChat(box3, msgs.slice(0, 2), "s");
P.drawChat(box3, msgs.slice(0, 8), "s");
out.reducedEnter = box3.children.some(c => c.classList.contains("enter"));
P.chatEls.clear(); P.drawChat(byId("tmsgs"), msgs.slice(0, 8), "s");
frames.length = 0; later.length = 0;
P.toggleTools(toolsId);
out.reducedOpen = [toolsEl().classList.contains("open"), frames.length];
P.toggleTools(toolsId);
out.reducedClose = [toolsEl().classList.contains("open"), D(toolsEl()).includes('class="chain"'), later.length];
reduce = false;
// --- one line per turn, counting calls by tool, live and once done ---
reduce = false;
P.chatEls.clear(); P.chatOpen.clear(); P.callOpen.clear();
const At = (s, role, ...blocks) => ({role, ts: `2026-10-15T10:00:${String(s).padStart(2, "0")}.000Z`, blocks});
const turn = [
  At(0, "user", {type: "text", text: "look around"}),
  At(1, "assistant", {type: "tool", id: "r1", name: "Read", arg: "src/a.py", input: '{"file_path": "/x/src/a.py"}'},
                     {type: "tool", id: "g1", name: "Grep", arg: "TODO", input: '{"pattern": "TODO"}'}),
  At(2, "user", {type: "tool_result", for: "g1", text: "3 hits"}, {type: "tool_result", for: "r1", text: "print(1)"}),
  At(3, "assistant", {type: "text", text: "Now the tests."}),
  At(3, "assistant", {type: "tool", id: "b1", name: "Bash", arg: "make test", input: '{"command": "make test"}'}),
];
const tbox = byId("tmsgsTurn");
P.setActive({key: "k"});
P.setTview({key: "k", sid: "s", msgs: turn, live: {running: true, turns: []}});
P.drawChat(tbox, turn, "s");
const lineId = P.chatItems(turn)[1].id;
const lineEl = P.chatEls.get(lineId);
const userEl = tbox.children[0], saidEl = tbox.children[2];
const setsBefore = [userEl._sets, saidEl._sets];
out.liveLine = {text: lineEl._ctl._inner, live: lineEl.classList.contains("live"),
                items: tbox.children.length};
// More calls stream in while it runs: the same line counts them.
const turn2 = turn.concat([
  At(9, "user", {type: "tool_result", for: "b1", text: "FAILED", error: true}),
  At(10, "assistant", {type: "tool", id: "r2", name: "Read", arg: "tests/t.py", input: "{}"}),
  At(11, "user", {type: "tool_result", for: "r2", text: "def test(): ..."}),
  At(12, "assistant", {type: "tool", id: "b2", name: "Bash", arg: "make test", input: "{}"}),
]);
P.setTview({key: "k", sid: "s", msgs: turn2, live: {running: true, turns: []}});
P.drawChat(tbox, turn2, "s");
out.liveGrown = {text: lineEl._ctl._inner, same: P.chatEls.get(lineId) === lineEl,
                 items: tbox.children.length,
                 untouched: userEl._sets === setsBefore[0] && saidEl._sets === setsBefore[1]};
// Opened while it runs: the chain in order, the last call still spinning.
P.toggleTools(lineId); runFrames();
const chain = lineEl._detail._inner;
out.chainLive = {open: lineEl.classList.contains("open"),
                 aria: lineEl._ctl.attrs["aria-expanded"],
                 order: (chain.match(/class="cname">[^<]*</g) || []).map(x => x.slice(14, -1)),
                 args: (chain.match(/class="carg">[^<]*</g) || []).map(x => x.slice(13, -1)),
                 states: (chain.match(/class="step call (\w+)/g) || []).map(x => x.split(" ").pop()),
                 durs: (chain.match(/class="cdur">[^<]*</g) || []).map(x => x.slice(13, -1)),
                 noIO: !chain.includes('class="cio"')};
// A call opens onto its input and output, and closes again.
P.toggleCall(lineId, 2);
const io = lineEl._detail._inner;
out.callOpen = {input: io.includes("<pre>{&quot;command&quot;: &quot;make test&quot;}</pre>"),
                output: io.includes("<pre>FAILED</pre>"), err: io.includes('class="clbl">error<'),
                aria: io.includes('aria-expanded="true" data-k="' + lineId + '/2"')};
P.toggleCall(lineId, 2);
out.callClosed = !lineEl._detail._inner.includes('class="cio"');
// The turn ends: the last result and the reply arrive; the line stops
// spinning and says how long the turn took.
const turn3 = turn2.concat([
  At(20, "user", {type: "tool_result", for: "b2", text: "ok"}),
  At(21, "assistant", {type: "text", text: "Fixed."}),
]);
P.setTview({key: "k", sid: "s", msgs: turn3, live: {running: false, turns: []}});
P.drawChat(tbox, turn3, "s");
out.doneLine = {text: lineEl._ctl._inner, live: lineEl.classList.contains("live"),
                states: (lineEl._detail._inner.match(/class="step call (\w+)/g) || []).map(x => x.split(" ").pop())};
// Collapsed again, the chain is dropped once folded.
P.toggleTools(lineId); for (const [f] of later.splice(0)) f(); runFrames();
out.collapsed = {open: lineEl.classList.contains("open"), detail: lineEl._detail._inner,
                 aria: lineEl._ctl.attrs["aria-expanded"]};
// A turn that ended with a call never answered says so, rather than spin.
const lost = [At(0, "user", {type: "text", text: "go"}),
              At(1, "assistant", {type: "tool", id: "z", name: "Bash", arg: "sleep", input: "{}"})];
P.chatEls.clear(); P.chatOpen.clear();
P.setTview({key: "k", sid: "s", msgs: lost, live: {running: false, turns: []}});
const lbox = byId("tmsgsLost");
P.drawChat(lbox, lost, "s");
P.chatOpen.add(P.chatItems(lost)[1].id); P.drawChat(lbox, lost, "s");
out.lost = {line: lbox.children[1]._ctl._inner, state: /class="step call lost/.test(lbox.children[1]._detail._inner)};
// A new turn is a new line: the owner speaking again starts it.
const two = turn3.concat([At(30, "user", {type: "text", text: "and again"}),
                          At(31, "assistant", {type: "tool", id: "q", name: "Glob", input: "{}"})]);
// A result that comes with a note in the same message (an interruption)
// still finds its call; it does not start a turn.
const intr = [At(0, "user", {type: "text", text: "go"}),
              At(1, "assistant", {type: "tool", id: "i1", name: "Bash", input: "{}"}),
              At(2, "user", {type: "tool_result", for: "i1", text: "stopped"},
                            {type: "text", text: "[Request interrupted by user for tool use]"}),
              At(3, "assistant", {type: "tool", id: "i2", name: "Read", input: "{}"})];
const iItems = P.chatItems(intr).filter(i => i.kind === "tools");
out.interrupted = iItems.map(g => [P.toolsLabel(g, true), g.calls.map(c => c.done)]);
// The bot says a new turn runs before its message reaches the transcript:
// the last turn's line does not start spinning again.
P.chatEls.clear(); P.chatOpen.clear();
const nbox = byId("tmsgsNext");
const startedAfter = String(Date.parse("2026-10-15T10:01:00.000Z") / 1000);
P.setTview({key: "k", sid: "s", msgs: turn3, live: {running: true, turns: [{ref: startedAfter, state: "running"}]}});
P.drawChat(nbox, turn3, "s");
out.nextTurnOld = nbox.children[1].classList.contains("live");
const startedBefore = String(Date.parse("2026-10-15T10:00:00.500Z") / 1000);
P.setTview({key: "k", sid: "s", msgs: turn3, live: {running: true, turns: [{ref: startedBefore, state: "running"}]}});
P.drawChat(nbox, turn3, "s");
out.thisTurnLive = nbox.children[1].classList.contains("live");
out.perTurn = P.chatItems(two).filter(i => i.kind === "tools").map(i => P.toolsLabel(i, false));
out.fmt = [P.fmtMs(30), P.fmtMs(400), P.fmtMs(4200), P.fmtMs(42000), P.fmtMs(192000), P.fmtMs(3900000)];

// --- attachments: on their message, and in the drawer ---
P.chatEls.clear(); P.chatOpen.clear(); P.callOpen.clear();
const sec = s => Date.parse(`2026-10-15T10:00:${String(s).padStart(2, "0")}.000Z`) / 1000;
const note = "\n\n[The user attached 2 file(s), saved locally:\n- /up/1-shot.png (image/png, 12 KB) — an image; use Read to look at it\n- /up/1-notes.txt (text/plain)\nRead them if they are relevant to the request.]";
const fmsgs = [At(0, "user", {type: "text", text: "what is wrong here?" + note}),
               At(5, "assistant", {type: "text", text: "Here is the fixed one."}),
               At(9, "user", {type: "text", text: "thanks"})];
const files = [
  {name: "shot.png", path: "/up/1-shot.png", direction: "in", ts: sec(0) - 2, exists: true, image: true, size: 12000},
  {name: "notes.txt", path: "/up/1-notes.txt", direction: "in", ts: sec(0) - 2, exists: true, image: false, size: 900},
  {name: "fixed.png", path: "/art/2-fixed.png", direction: "out", ts: sec(7), exists: true, image: true, size: 50000},
  {name: "report.pdf", path: "/art/2-report.pdf", direction: "out", ts: sec(7), exists: true, image: false, size: 2400000},
  {name: "old.log", path: "/art/old.log", direction: "out", ts: sec(7) - 86400 * 30, exists: false, pruned: 1700000000, size: 10},
];
const fbox = byId("tmsgsFiles");
P.setTview({key: "k", sid: "s", msgs: fmsgs, live: null, files});
P.drawChat(fbox, fmsgs, "s", files);
const ub = fbox.children[0]._inner, ab = fbox.children[1]._inner, tb = fbox.children[2]._inner;
out.inline = {
  noteGone: !ub.includes("The user attached") && ub.includes("what is wrong here?"),
  thumb: /<button type="button" class="athumb" data-path="\/up\/1-shot.png"[^>]*onclick="viewImage\(this.dataset.path\)"/.test(ub)
         && ub.includes('src="/thumb?path=%2Fup%2F1-shot.png"') && ub.includes('loading="lazy"')
         && ub.includes('width="96" height="96"'),
  chip: ub.includes('<a class="fchip" href="/download?path=%2Fup%2F1-notes.txt" download="notes.txt"')
        && ub.includes('class="fsize">900 B<'),
  outThumb: ab.includes("/thumb?path=%2Fart%2F2-fixed.png"),
  outChip: ab.includes("report.pdf") && ab.includes("2.3 MB"),
  oldNowhere: !(ub + ab + tb).includes("old.log"),
  thanksBare: !tb.includes('class="atts"'),
};
// The viewer opens on the image, at full size, inline.
P.setActive({key: "k"});
P.viewImage("/art/2-fixed.png");
out.viewer = {open: P.viewerIsOpen(), src: byId("imgviewimg").src, name: byId("imgviewname")._inner};
P.closeViewer();
out.viewerClosed = P.viewerIsOpen();
// The header: Attachments (N), on a laptop and a phone.
out.headFiles = P.chatHead({title: "F"}, {turns: []}, 0, P.threadFiles().length);
out.toolbarFiles = P.transcriptHead({resume_cmd: "x", slack_link: "y"});
// The drawer: all of it, newest first, images a grid, the rest a list.
const many = [];
for (let i = 0; i < 24; i++)
  many.push({name: `s${i}.png`, path: `/art/s${i}.png`, direction: "out", ts: 1000 + i, exists: true, image: true, size: 1000});
many.push({name: "z.zip", path: "/art/z.zip", direction: "out", ts: 2000, exists: true, image: false, size: 5});
many.push({name: "gone.png", path: "/art/gone.png", direction: "in", ts: 500, exists: false, image: true, size: 5});
P.setTview({key: "k", sid: "s", msgs: fmsgs, live: null, files: many});
P.openDrawer();
const body = byId("drawerbody")._inner;
out.drawer = {open: P.drawerIsOpen(), title: byId("drawertitle")._inner,
              thumbs: (body.match(/class="athumb"/g) || []).length,
              listed: (body.match(/<li>/g) || []).length,
              firstImg: (body.match(/data-path="([^"]+)"/) || [])[1],
              lastImg: (body.match(/data-path="([^"]+)"/g) || []).pop(),
              zipFirst: body.indexOf("z.zip") < body.indexOf("gone.png"),
              lazy: (body.match(/loading="lazy"/g) || []).length};
P.closeDrawer();
out.drawerClosed = P.drawerIsOpen();
// The composer: only the files staged for the next message, removable.
P.addFiles([{name: "a.png", size: 2048, type: "image/png"}, {name: "b.txt", size: 10, type: "text/plain"}], "reply");
const chipsA = byId("chips")._inner;
P.dropFile("reply", 0);
out.composer = {before: (chipsA.match(/class="chip"/g) || []).length,
                names: chipsA.includes("a.png") && chipsA.includes("b.txt") && chipsA.includes("2 KB"),
                removable: chipsA.includes("onclick=\"dropFile('reply', 1)\"") && chipsA.includes('aria-label="remove b.txt"'),
                after: (byId("chips")._inner.match(/class="chip"/g) || []).length,
                thread: !chipsA.includes("shot.png")};
P.dropFile("reply", 0);
process.stdout.write(JSON.stringify(out));
"""


def test_viz_phone_chat():
    import re
    import shutil
    import subprocess as _sp
    import visualizer as V
    print("\non a phone the open thread is a chat: bubbles, folded tools, its own header, ⚠ count")
    css = re.search(r"<style>(.*?)</style>", V.PAGE, re.S).group(1)
    at = css.index("@media (max-width: 700px)")
    desktop, phone = css[:at], css[at:]
    check("a laptop never sees the phone's thread header, alert count or sheet",
          "#alertslot, #chathead, #alertsheet { display: none; }" in desktop)
    check("the new-messages pill is the laptop's too: shown by opacity, untappable while hidden",
          re.search(r"#jumppill \{[^}]*opacity: 0;[^}]*pointer-events: none;", desktop) is not None
          and re.search(r"#jumppill\.show \{[^}]*opacity: 1;[^}]*pointer-events: auto;", desktop) is not None)
    check("on a phone the alerts are never a bar above anything", "#alertbar { display: none; }" in phone)
    check("an open thread on a phone has the screen: no app header, its own sticky one",
          "body.reading header { display: none; }" in phone
          and re.search(r"body\.reading #chathead \{ display: flex;[^}]*position: sticky;", phone) is not None)
    check("the composer keeps above the keyboard (visual viewport height)",
          "body { height: var(--vvh, 100dvh); }" in phone and "visualViewport" in V.PAGE)
    check("the list header has ✏️ new thread on a phone",
          'onclick="newThreadForm()" title="new thread"' in V.PAGE)
    # Motion: transform and opacity only, and none of it under reduced motion.
    frames = dict(re.findall(r"@keyframes (\w+) \{(.*?\})\s*\}", css, re.S))
    props = {k: set(re.findall(r"([a-z-]+)\s*:", v)) for k, v in frames.items()}
    check("the chat's keyframes exist: rise and breathe", {"rise", "breathe"} <= set(frames), str(set(frames)))
    check("every keyframe moves only transform and opacity",
          all(v <= {"transform", "opacity"} for v in props.values()), str(props))
    rm = css.rfind("@media (prefers-reduced-motion: reduce)")
    tail = css[rm:]
    check("reduced motion turns every animation and transition off, last so it wins",
          rm > css.rfind("@media (max-width: 380px)") and "animation: none !important" in tail
          and "transition: none !important" in tail and "--t: 0ms" in tail, tail[:300])
    check("one shared set of chat measures, declared once at the root",
          desktop.count("--chat-fs:") == 1 and desktop.count("--bubble-r:") == 1
          and desktop.count("--t-fold:") == 1 and "var(--chat-fs)" in desktop)
    check("FOLD_MS in the script is the CSS's --t-fold",
          "--t-fold: 240ms" in desktop and "const FOLD_MS = 240;" in V.PAGE)
    check("code in a bubble scrolls inside itself, not the page",
          re.search(r"\.bubble pre \{[^}]*overflow-x: auto;", desktop) is not None
          and re.search(r"\.bubble \{[^}]*overflow-wrap: anywhere;", desktop) is not None)
    check("the composer is pinned to the bottom at every width, clear of the home indicator",
          re.search(r"#composer \{[^}]*position: sticky; bottom: 0;[^}]*env\(safe-area-inset-bottom\)",
                    desktop) is not None)
    check("Send gives way when pressed", "#composer .crow button.act:active { transform: scale(" in desktop)

    node = shutil.which("node")
    if not node:
        print("  … node not installed — the chat's script checked by text only")
        return
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    r = _sp.run([node, "-e", CHAT_HARNESS.replace("__JS__", json.dumps(js))],
                capture_output=True, text=True, timeout=60)
    check("the page's script runs in the chat harness", r.returncode == 0, r.stderr[-800:])
    if r.returncode:
        return
    o = json.loads(r.stdout)
    check("messages become bubbles; every tool call of a turn is one line, where the first ran",
          o["shape"] == ["user:please fix it", "tools:4:Bash+Read+Edit", "assistant:Fixed **it**.",
                         "assistant:One more:", "assistant:All done."], str(o["shape"]))
    check("an item's id is where it starts, so it is the same as the thread grows",
          o["ids"][:len(o["prefixIds"])] == o["prefixIds"] and o["prefixIds"] == ["0.0", "1.0"],
          f'{o["ids"]} vs {o["prefixIds"]}')
    check("the tools line counts the calls by tool, most first",
          o["labels"] == ["Bash ×2 · Read ×1 · Edit ×1"], str(o["labels"]))
    check("a run of only thinking says so", o["thinkingOnly"] == "💭 thinking", o["thinkingOnly"])
    check("a message's figures go with its words, else with its tools",
          o["bubbleUsage"] is True and o["groupUsage"] is True)
    check("folded, a tools line shows no detail and no figures",
          o["closed"] == {"pre": False, "usage": False, "label": True}, str(o["closed"]))
    check("opened, it shows each call -- one opened shows its input and result -- and the figures",
          all(o["opened"].values()), str(o["opened"]))
    check("a bubble's figures sit behind its time, the markdown rendered",
          '<details class="cstat"><summary>' in o["bubbleHtml"] and 'class="usage"' in o["bubbleHtml"]
          and "<b>it</b>" in o["bubbleHtml"] and "claude-opus-5-5" in o["bubbleHtml"], o["bubbleHtml"])
    check("a bubble with no figures is only its time",
          "<details" not in o["plainBubble"] and 'class="cmsg user"' in o["plainBubble"], o["plainBubble"])
    check("drawing a grown thread keeps the elements already drawn",
          o["drawn1"] == 2 and o["drawn2"] == 5 and o["kept"] is True, str(o))
    check("a tools line opened stays open, and still open as it collects more",
          o["openAfterToggle"] is True and o["stillOpen"] is True)
    check("a thread with nothing in it says so", o["empty"] is True)
    check("the alert count is one per alert: revision, Slack, stalled, cost",
          o["alertsAll"] == 4 and o["alertsNone"] == 0 and o["alertsTwo"] == 2,
          f'{o["alertsAll"]} {o["alertsNone"]} {o["alertsTwo"]}')
    check("the header's ⚠ shows that count, the bar and the sheet the same alerts",
          "⚠ 4" in o["slot"] and o["bar"] == 4 and o["sheet"] == 4, f'{o["slot"]!r} {o["bar"]} {o["sheet"]}')
    check("no alerts, no ⚠", o["slotEmpty"] == "", o["slotEmpty"])
    check("a phone's list opens on Conversations", o["kindPhone"] == "thread", o["kindPhone"])
    check("with no conversation to show, it opens on All", o["kindNoConv"] == "all", o["kindNoConv"])
    check("a laptop keeps All, also after the window was phone-narrow",
          o["kindWideBefore"] == "all", o["kindWideBefore"])
    check("a filter picked by hand is kept over the default", o["kindChosen"] == "task", o["kindChosen"])
    check("an alert's thread opens even when the list's filter hides it",
          o["jumped"] == "hidden-task", str(o["jumped"]))
    check("on a laptop jumpTo is as it was: only a card in the list opens",
          o["jumpedWide"] is None, str(o["jumpedWide"]))
    h = o["headRun"]
    check("the header: back, the title escaped, project, running time, queued, ⚠ and Stop",
          'onclick="closeSession()"' in h and "A &lt;b&gt; title" in h and "silkworm" in h
          and "running 2m" in h and "1 queued" in h and "⚠ 3" in h and "cmdSend('!stop')" in h, h)
    check("a stalled card says how long it has been silent, not how old it is",
          "stalled · silent 2h1" in o["stallCard"] and "5h" not in o["stallCard"], o["stallCard"])
    check("so does a stalled thread's header",
          "stalled · silent 2h1" in o["stallHead"] and "5h" not in o["stallHead"], o["stallHead"])
    check("and the alert bar's button for it",
          "silent 2h1" in o["stallAlert"] and "5h" not in o["stallAlert"], o["stallAlert"])
    check("the open thread's turn bar leads with the silence, then the age",
          "Turn stalled · silent 2h1 · 5h in" in o["stallBar"], o["stallBar"])
    check("an answer without silent_s still shows a number (its age)",
          "stalled · silent 10m" in o["stallOld"], o["stallOld"])
    check("between answers the silence counts on with the age",
          o["stallLater"] == [6 * 3600, 3 * 3600 + 60], o["stallLater"])
    hi = o["headIdle"]
    check("an idle thread's header has no Stop and no ⚠, and says untitled",
          "!stop" not in hi and "⚠" not in hi and "untitled" in hi, hi)
    check("the composer grows to six lines and then scrolls inside itself",
          o["tall"] == ["154px", "auto"] and o["short"] == ["44px", "hidden"], f'{o["tall"]} {o["short"]}')
    check("its placeholder on a phone is short enough to show whole",
          0 < len(o["placeholder"]) <= 24, o["placeholder"])
    check("read at the end, the chat follows a new message",
          o["followed"] == [1300, False], str(o["followed"]))
    check("read further up, it stays put and offers the way down",
          o["stayed"] == [100, True], str(o["stayed"]))
    check("what the owner just sent is brought into view", o["forced"] == [1600, False], str(o["forced"]))
    check("a reader following the end keeps following when the page grew between draws",
          o["grewBetween"] == [True, False], str(o["grewBetween"]))
    check("on a laptop, read further up, the chat stays put and shows the pill",
          o["wideStayed"] == [100, True], str(o["wideStayed"]))
    check("on a laptop, read at the end, it follows and the pill goes",
          o["wideFollowed"] == [1500, False], str(o["wideFollowed"]))
    check("tapping the pill scrolls smoothly down and hides it",
          o["pillTap"] == ["smooth", False], str(o["pillTap"]))
    check("under reduced motion the pill jumps straight down",
          o["pillTapReduced"][0] is None and o["pillTapReduced"][1] == 1500, str(o["pillTapReduced"]))
    rl = o["runningLive"]
    check("while a turn runs: a working bubble where the reply will be, announced, with Stop",
          'class="cmsg typing" role="status"' in rl and 'class="tdots"' in rl
          and "Working" in rl and "■ Stop" in rl, rl)
    check("its markup is the same however long the turn has run (a poll leaves it be)",
          o["runningSteady"] is True)
    check("following the end, a poll that changed nothing moves nothing", o["unchangedPoll"] is True)
    check("a tools line opened near the end is not scrolled away by the next poll; the pill is offered",
          o["openedNotYanked"] == [1500, True], str(o["openedNotYanked"]))
    check("mid-glide to the end, a poll still follows (the glide is not the reader scrolling up)",
          o["glide"] == [3200, False], str(o["glide"]))
    check("a grown thread appends only its new items, moving none already there",
          o["appendedOnly"] == [-1, -1], str(o["appendedOnly"]))
    check("the hidden pill is out of the tab order and the accessibility tree",
          re.search(r"#jumppill \{[^}]*visibility: hidden;", css) is not None
          and re.search(r"#jumppill\.show \{[^}]*visibility: visible;", css) is not None)
    check("the turn over, the indicator is gone", "typing" not in o["idleLive"] and "Working" not in o["idleLive"],
          o["idleLive"])
    check("the thread's first draw does not animate", o["firstDrawEnter"] == [False, False],
          str(o["firstDrawEnter"]))
    check("what arrives after it rises in, and only that",
          o["laterEnter"] == [False, False, True] and o["kinds"] == ["bubble", "tools", "bubble"],
          f'{o["laterEnter"]} {o["kinds"]}')
    check("a tools line's detail is drawn folded, then unfolds a frame later (height animates)",
          o["openBeforeFrame"] == [False, True] and o["openAfterFrame"] is True,
          f'{o["openBeforeFrame"]} {o["openAfterFrame"]}')
    check("closing folds it at once and drops the detail after the fold",
          o["closing"][0] is False and o["closing"][1] is True and o["closing"][2] == [240]
          and o["closed2"] is False, f'{o["closing"]} {o["closed2"]}')
    check("reduced motion: nothing rises in", o["reducedEnter"] is False)
    check("reduced motion: a tools line opens at once, waiting on no frame",
          o["reducedOpen"] == [True, 0], str(o["reducedOpen"]))
    check("reduced motion: and closes at once, its detail dropped with no timer",
          o["reducedClose"] == [False, False, 0], str(o["reducedClose"]))

    # One line per turn, counting calls by tool, live and once done.
    ll, lg = o["liveLine"], o["liveGrown"]
    check("while the turn runs its line spins and counts each tool, with no time yet",
          ll["live"] and 'class="spin"' in ll["text"] and "Read ×1 · Grep ×1 · Bash ×1" in ll["text"]
          and " · " not in ll["text"].split("Bash ×1")[1].split("<")[0], str(ll))
    check("calls streaming in update the same line's counts in place",
          "Read ×2 · Bash ×2 · Grep ×1" in lg["text"] and lg["same"] and lg["items"] == 3, str(lg))
    check("...rewriting nothing else in the thread", lg["untouched"] is True, str(lg))
    cl = o["chainLive"]
    check("tapped, it unfolds onto the turn's calls in the order they ran",
          cl["open"] and cl["aria"] == "true" and cl["order"] == ["Read", "Grep", "Bash", "Read", "Bash"]
          and cl["noIO"], str(cl))
    check("each call: a short argument, how it ended (the running one spins), its time",
          cl["args"] == ["src/a.py", "TODO", "make test", "tests/t.py", "make test"]
          and cl["states"] == ["ok", "ok", "err", "ok", "run"]
          and cl["durs"] == ["1.0s", "1.0s", "6.0s", "1.0s", ""], str(cl))
    check("a call opens onto its input and its output (an error says so)",
          all(o["callOpen"].values()), str(o["callOpen"]))
    check("and closes again", o["callClosed"] is True)
    dl = o["doneLine"]
    check("the turn over, the line stops spinning and says the turn's total time",
          not dl["live"] and "spin" not in dl["text"] and "Read ×2 · Bash ×2 · Grep ×1 · 19s" in dl["text"]
          and dl["states"] == ["ok", "ok", "err", "ok", "ok"], str(dl))
    check("tapped again it folds back, its chain dropped",
          o["collapsed"] == {"open": False, "detail": "", "aria": "false"}, str(o["collapsed"]))
    check("a call a finished turn never answered shows no result, not a spinner",
          o["lost"]["state"] and "spin" not in o["lost"]["line"], str(o["lost"]))
    check("the owner speaking again starts the next turn's line",
          o["perTurn"] == ["Read ×2 · Bash ×2 · Grep ×1 · 19s", "Glob ×1"], str(o["perTurn"]))
    check("a result sharing a message with a note (an interruption) finds its call; one line",
          o["interrupted"] == [["Bash ×1 · Read ×1", [True, False]]], str(o["interrupted"]))
    check("a new turn not yet in the transcript does not set the last turn's line spinning",
          o["nextTurnOld"] is False and o["thisTurnLive"] is True,
          f'{o["nextTurnOld"]} {o["thisTurnLive"]}')
    check("times are short: <0.1s, 0.4s, 4.2s, 42s, 3m 12s, 1h 5m",
          o["fmt"] == ["<0.1s", "0.4s", "4.2s", "42s", "3m 12s", "1h 5m"], str(o["fmt"]))
    check("the spinner turns by transform, and is still under reduced motion",
          re.search(r"\.spin \{[^}]*animation: spin", css) is not None and "spin" in frames)
    # Attachments: on their message, in the drawer, and the composer's own.
    check("files sit on the message they came with: images lazy, fixed-size thumbnails, others chips",
          all(o["inline"].values()), str(o["inline"]))
    check("an image opens the viewer at full size, inline, and closes",
          o["viewer"]["open"] and o["viewer"]["src"] == "/download?inline=1&path=%2Fart%2F2-fixed.png"
          and "2 / 2" in o["viewer"]["name"] and o["viewerClosed"] is False, str(o["viewer"]))
    check("the thread's header offers Attachments (N), on a phone and a laptop",
          'aria-label="Attachments (5)">📎 5<' in o["headFiles"]
          and "📎 Attachments (5)" in o["toolbarFiles"], o["headFiles"][-300:])
    dr = o["drawer"]
    check("the drawer holds every file, newest first: images a grid, the rest a list",
          dr["open"] and dr["title"] == "📎 Attachments (26)" and dr["thumbs"] == 24
          and dr["listed"] == 2 and dr["firstImg"] == "/art/s23.png"
          and dr["lastImg"] == 'data-path="/art/s0.png"' and dr["zipFirst"] and dr["lazy"] == 24, str(dr))
    check("and closes", o["drawerClosed"] is False)
    check("the composer shows only the files staged for the next message, each removable",
          o["composer"] == {"before": 2, "names": True, "removable": True, "after": 1, "thread": True},
          str(o["composer"]))
    check("thumbnails are fixed squares, faded in by opacity (no layout shift)",
          re.search(r"\.athumb \{ width: 84px; height: 84px;", css) is not None
          and re.search(r"\.athumb img \{[^}]*object-fit: cover; opacity: 0;", css) is not None)
    check("the drawer slides by transform, the viewer fades by opacity",
          re.search(r"#attdrawer \.box \{[^}]*transform: translateX\(100%\)", css) is not None
          and re.search(r"#imgview \{[^}]*opacity: 0;", css) is not None)
    check("the thread's file list no longer sits under the chat", 'id="tfiles"' not in V.PAGE)


# --- the operator acts on your word, in a turn you started ---------------------
# "I should just be able to tell you" (2026-10-21, 10-23/24): a decision stated
# in plain words in the operator's thread is carried out, without `!approve`.


def _say(bot, text, turn=None):
    """Open a grant as handle_prompt does for your Slack message."""
    return bot.ns["OPERATOR_GRANTS"].open(HOME, "1761300000.000100", "U_ME", text,
                                          turn or bot.turn)


def _decided(st, tid, ts="1761300000.000100"):
    rec = st.get(tid) or {}
    ev = [e for e in rec.get("events") or [] if e.get("kind") == "decided"]
    return ((rec.get("result") or {}).get("decided_by") == "user via operator"
            and (rec.get("result") or {}).get("decided_ts") == ts
            and bool(ev) and ts in ev[-1]["detail"])


def test_operator_on_your_word_accepts_and_approves():
    import tasks as T
    print("\nthe operator, on your word: accept and approve in a turn you started; "
          "refused in a sweep")
    bot = _said_bot()
    st = bot.store
    a = _proposal(st, "Add an appearance preference to Settings")
    b = _proposal(st, "Cache the release plan per checkout")
    g = _say(bot, f"accept {a} and {b}")
    r = _as_sweep(bot, "accept", a, "--notes", "user said accept it")
    check("a sweep's accept is still refused, naming !approve",
          r.returncode == 1 and "!approve accept" in r.stderr
          and st.get(a)["state"] == T.PROPOSED, r.stdout + r.stderr)
    r = _as_user(bot, g, "accept", a, "--notes", "you said: accept the appearance one")
    check("in your turn, accept goes ahead", r.returncode == 0
          and st.get(a)["state"] == T.QUEUED, r.stdout + r.stderr)
    check("and records your message and decided_by", _decided(st, a), st.get(a))
    out = bot.ns["handle_tasks"]({"action": "accept", "id": b, "caller_role": "operator",
                                  "caller_thread": HOME, "caller_grant": g,
                                  "caller_task": "tsk_some_wakeup", "notes": "accept"})
    check("a grant sent from another turn (a wake-up's) lifts nothing",
          not out["ok"] and out.get("refused") == "accept"
          and st.get(b)["state"] == T.PROPOSED, str(out))
    w = st.create("Tidy the sync retry loop in SyncEngine", role="implementor")["id"]
    st.transition(w, T.RUNNING); st.transition(w, T.AWAITING_APPROVAL)
    out = bot.ns["handle_tasks"]({"action": "approve", "id": w, "caller_role": "operator",
                                  "caller_thread": HOME, "by": "operator"})
    check("a sweep's approve is refused", not out["ok"]
          and st.get(w)["state"] == T.AWAITING_APPROVAL, str(out))
    out = bot.ns["handle_tasks"]({"action": "approve", "id": w, "caller_role": "operator",
                                  "caller_thread": HOME, "caller_grant": g,
                                  "caller_task": bot.turn, "by": "operator",
                                  "notes": "approve the retry loop tidy"})
    check("in your turn, approve goes ahead",
          out["ok"] and st.get(w)["state"] != T.AWAITING_APPROVAL and _decided(st, w), str(out))
    w2 = st.create("Tidy the sync backoff", role="implementor")["id"]
    st.transition(w2, T.RUNNING); st.transition(w2, T.AWAITING_APPROVAL)
    r = _as_sweep(bot, "approve", w2, "--notes", "approve it")
    check("`silkworm approve` from a sweep is refused",
          r.returncode == 1 and st.get(w2)["state"] == T.AWAITING_APPROVAL, r.stdout + r.stderr)
    r = _as_user(bot, g, "approve", w2, "--notes", "you said approve the backoff tidy")
    check("and goes ahead in your turn", r.returncode == 0
          and st.get(w2)["state"] != T.AWAITING_APPROVAL and _decided(st, w2),
          r.stdout + r.stderr)
    away = bot.ns["OPERATOR_GRANTS"].open("D9:9.0", "1761300000.000200", "U_ME",
                                          f"accept {b}", bot.turn)
    out = bot.ns["handle_tasks"]({"action": "accept", "id": b, "caller_role": "operator",
                                  "caller_thread": "D9:9.0", "caller_grant": away,
                                  "caller_task": bot.turn, "notes": "accept"})
    check("a grant from a thread that is not the operator's home lifts nothing",
          not out["ok"] and st.get(b)["state"] == T.PROPOSED, str(out))
    bot.ns["OPERATOR_GRANTS"].close(g)
    r = _as_user(bot, g, "accept", b, "--notes", "accept it")
    check("once your turn has ended its grant is gone",
          r.returncode == 1 and st.get(b)["state"] == T.PROPOSED, r.stdout + r.stderr)


def test_operator_on_your_word_dismisses_and_cancels():
    import tasks as T
    print("\nthe operator, on your word: dismiss and cancel; a running task only if you named it")
    bot = _said_bot()
    st = bot.store
    stopped = []
    bot.ns["stop_task"] = lambda tid: stopped.append(tid) or True
    prop = _proposal(st, "Add an appearance preference to Settings")
    st.update(prop, branch="silkworm/" + prop)
    queued = st.create("Retry sync on 5xx", role="implementor", project="alpha")["id"]
    run_named = st.create("Retry sync on 4xx", role="implementor", project="alpha")["id"]
    st.transition(run_named, T.RUNNING)
    run_other = st.create("Retry sync offline", role="implementor", project="alpha")["id"]
    st.transition(run_other, T.RUNNING)
    g = _say(bot, f"dismiss {prop}, cancel {queued} and stop {run_named}; leave the rest")
    for tid in (prop, queued):
        r = _as_sweep(bot, "dismiss", tid, "--notes", "the user said so")
        check(f"a sweep's dismiss of {st.get(tid)['state']} work is refused",
              r.returncode == 1 and st.get(tid)["state"] != T.CANCELLED, r.stdout + r.stderr)
    r = _as_user(bot, g, "dismiss", prop, "--notes", "you said dismiss the appearance one")
    check("in your turn, dismiss goes ahead and is recorded",
          r.returncode == 0 and st.get(prop)["state"] == T.CANCELLED and _decided(st, prop),
          r.stdout + r.stderr)
    check("and deletes no branch", bot.dropped == [], bot.dropped)
    me = {"caller_role": "operator", "caller_thread": HOME, "caller_grant": g,
          "caller_task": bot.turn, "by": "operator", "notes": "you said cancel these"}
    out = bot.ns["handle_tasks"]({"action": "cancel", "id": queued, "caller_role": "operator",
                                  "caller_thread": HOME, "by": "operator", "notes": "x"})
    check("a sweep's cancel of queued work is refused",
          not out["ok"] and st.get(queued)["state"] == T.QUEUED, str(out))
    r = _as_user(bot, g, "cancel", queued, "--notes", f"you said cancel {queued}")
    check("in your turn, `silkworm cancel` goes ahead", r.returncode == 0
          and st.get(queued)["state"] == T.CANCELLED and _decided(st, queued),
          r.stdout + r.stderr)
    out = bot.ns["handle_tasks"]({**me, "action": "cancel", "id": run_other})
    check("a running task your message did not name is not cancelled, nor its child killed",
          not out["ok"] and st.get(run_other)["state"] == T.RUNNING
          and run_other not in stopped and "name it" in out["error"], str(out))
    out = bot.ns["handle_tasks"]({**me, "action": "cancel", "id": run_named})
    check("the running task you named is cancelled and stopped",
          out["ok"] and st.get(run_named)["state"] == T.CANCELLED
          and stopped == [run_named], str(out))


def test_operator_on_your_word_closes_and_digs():
    import tasks as T
    print("\nthe operator, on your word: close and dig on research's findings")
    bot = _said_bot()
    st = bot.store

    def parked(q):
        t = st.create(q, role="researcher", project="alpha")["id"]
        st.transition(t, T.RUNNING); st.transition(t, T.NEEDS_INPUT, "findings")
        return t
    shut, deeper = parked("Why does sync stall on cold launch?"), parked("Is the widget too eager?")
    g = _say(bot, f"close {shut}; on {deeper} dig into iOS 26 too")
    r = _as_sweep(bot, "close", shut, "--notes", "close it")
    check("a sweep's close is refused", r.returncode == 1
          and st.get(shut)["state"] == T.NEEDS_INPUT, r.stdout + r.stderr)
    r = _as_sweep(bot, "dig", deeper, "--notes", "and iOS 26?")
    check("a sweep's dig is refused", r.returncode == 1
          and st.get(deeper)["state"] == T.NEEDS_INPUT, r.stdout + r.stderr)
    r = _as_user(bot, g, "close", shut, "--notes", "you said close the cold-launch findings")
    check("in your turn, close goes ahead", r.returncode == 0
          and st.get(shut)["state"] == T.DONE and _decided(st, shut), r.stdout + r.stderr)
    r = _as_user(bot, g, "dig", deeper, "--notes", "you asked: check iOS 26 too")
    check("in your turn, dig goes ahead", r.returncode == 0
          and st.get(deeper)["state"] != T.NEEDS_INPUT and _decided(st, deeper),
          r.stdout + r.stderr)


def test_operator_on_your_word_reworks_lands_edits_and_drops():
    import tasks as T
    print("\nthe operator, on your word: rework, land, edit and drop")
    repo = _op_repo()
    bot = _said_bot()
    st = bot.store

    def finished(name, state=T.DONE):
        t = st.create(f"Work for {name}", role="implementor", project="alpha",
                      scope={"cwd": str(repo), "repo": str(repo)})["id"]
        st.update(t, branch=f"silkworm/{name}")
        st.transition(t, T.RUNNING); st.transition(t, state)
        return t
    redo = finished("tsk_same", T.AWAITING_APPROVAL)
    land = finished("tsk_new", T.CANCELLED)          # dismissed from review
    drop = finished("tsk_new")
    unrun = st.create("Retry sync on 5xx", role="implementor", project="alpha")["id"]
    g = _say(bot, f"send {redo} back to handle iPad, land {land}, reword {unrun}, drop {drop}")
    for argv in (("rework", redo, "--notes", "handle iPad"), ("land", land),
                 ("edit", unrun, "--goal", "Retry sync on 5xx and 429"), ("drop", drop)):
        before = st.get(argv[1])
        r = _as_sweep(bot, *argv)
        check(f"a sweep's {argv[0]} is refused",
              r.returncode == 1 and st.get(argv[1])["state"] == before["state"]
              and st.get(argv[1])["goal"] == before["goal"], r.stdout + r.stderr)
    check("and nothing was landed or dropped", not bot.landings and not bot.dropped)
    r = _as_user(bot, g, "rework", redo, "--notes", "you said: handle iPad too")
    check("in your turn, rework goes ahead", r.returncode == 0
          and st.get(redo)["state"] == T.QUEUED and _decided(st, redo), r.stdout + r.stderr)
    r = _as_user(bot, g, "land", land, "--notes", f"you said land {land}")
    check("in your turn, landing work dismissed from review goes ahead",
          r.returncode == 0 and land in bot.landings and _decided(st, land), r.stdout + r.stderr)
    r = _as_user(bot, g, "edit", unrun, "--goal", "Retry sync on 5xx and 429",
                 "--notes", "you said include 429")
    check("in your turn, an edit of the goal goes ahead",
          r.returncode == 0 and st.get(unrun)["goal"] == "Retry sync on 5xx and 429"
          and _decided(st, unrun), r.stdout + r.stderr)
    r = _as_user(bot, g, "drop", drop, "--notes", f"you said drop {drop}")
    check("in your turn, dropping an unproven branch goes ahead",
          r.returncode == 0 and bot.dropped and _decided(st, drop), r.stdout + r.stderr)


def test_operator_on_your_word_files_and_takes_a_release_step():
    import tasks as T
    print("\nthe operator, on your word: filing straight to the queue, and a release step")
    bot = _said_bot()
    st = bot.store
    bot.ns["project_store"] = types.SimpleNamespace(
        ensure=lambda n, **kw: {"slug": n}, home=lambda n, create=False: None,
        scope_for=lambda slug: {"branch": "main"}, get=lambda slug: None)
    step = st.create("Cut a TestFlight build", role="implementor", project="cadence")["id"]
    st.transition(step, T.RUNNING); st.transition(step, T.NEEDS_INPUT)
    g = _say(bot, f"queue the 429 retry work, and release {step}")
    sweep = {"caller_role": "operator", "caller_thread": HOME, "by": "operator",
             "notes": "the user wants it"}
    mine = {**sweep, "caller_grant": g, "caller_task": bot.turn,
            "notes": "you said queue the 429 retry"}
    filing = {"action": "create", "project": "alpha", "scope": {"cwd": str(bot.tmp)},
              "goal": "Retry sync on 429 with the server's Retry-After"}
    out = bot.ns["handle_tasks"]({**sweep, **filing})
    check("a sweep filing straight into the queue is refused",
          not out["ok"] and out.get("refused") == "accept", str(out))
    out = bot.ns["handle_tasks"]({**mine, **filing})
    made = (out.get("task") or {}).get("id", "")
    check("in your turn it is filed queued, and records who decided",
          out["ok"] and (st.get(made) or {}).get("state") == T.QUEUED
          and _decided(st, made), str(out))
    out = bot.ns["handle_tasks"]({**sweep, "action": "release", "id": step})
    check("a sweep's release step is refused",
          not out["ok"] and out.get("refused") == "release" and not bot.steps, str(out))
    out = bot.ns["handle_tasks"]({**mine, "action": "release", "id": step,
                                  "notes": "you said release it"})
    check("in your turn it is taken", out["ok"] and bot.steps == [step] and _decided(st, step),
          str(out))


def test_operator_on_your_word_needs_notes():
    import tasks as T
    print("\nthe operator, on your word: never without --notes saying what you decided")
    bot = _said_bot()
    st = bot.store
    a = _proposal(st, "Add an appearance preference to Settings")
    g = _say(bot, f"accept {a}")
    r = _as_user(bot, g, "accept", a, "--notes", "   ")
    check("blank --notes is refused by the CLI as a usage error",
          r.returncode == 2 and st.get(a)["state"] == T.PROPOSED, r.stdout + r.stderr)
    r = _as_user(bot, g, "accept", a)
    check("no --notes is refused, and says why",
          r.returncode == 1 and "--notes" in r.stderr and st.get(a)["state"] == T.PROPOSED,
          r.stdout + r.stderr)
    dead = types.SimpleNamespace(port=1, statefile=bot.statefile, turn=bot.turn)
    r = _as_user(dead, g, "accept", a)
    check("the CLI refuses that itself, before asking any bot (1, not 3)",
          r.returncode == 1 and "--notes" in r.stderr, r.stdout + r.stderr)
    out = bot.ns["handle_tasks"]({"action": "accept", "id": a, "caller_role": "operator",
                                  "caller_thread": HOME, "caller_grant": g,
                                  "caller_task": bot.turn, "notes": " \n "})
    check("and by the bot, for a request that never went through this CLI",
          not out["ok"] and "--notes" in out["error"] and st.get(a)["state"] == T.PROPOSED,
          str(out))
    r = _as_user(bot, g, "accept", a, "--notes", "you said accept it")
    said = [e for e in st.get(a)["events"] if e["kind"] == "decided"]
    check("with them, it goes ahead and keeps them",
          r.returncode == 0 and said and "you said accept it" in said[-1]["detail"],
          r.stdout + r.stderr)


def test_operator_on_your_word_never_publishes_or_touches_trader():
    print("\nthe operator, on your word in general: publishing where it is off and trader's "
          "live config stay refused unless the item itself is named or planned")
    import tasks as T
    repo = _op_repo()
    bot = _said_bot()
    st = bot.store
    g = _say(bot, "turn on everything for cadence, set trader's base, and land the trader fix")
    me = {"caller_role": "operator", "caller_thread": HOME, "caller_grant": g,
          "caller_task": bot.turn, "by": "operator", "notes": "you said so"}
    refused = bot.ns["operator_refused"]
    out = refused("projects", "publish", {**me, "slug": "cadence", "on": True})
    check("publish where it is off is refused, telling it to record a plan, not to ask "
          "for !approve", out and out["refused"] == "publish"
          and "ops plan --publish cadence" in out["error"] and "!approve" not in out["error"],
          str(out))
    out = refused("projects", "base", {**me, "slug": "trader"})
    check("trader's settings are refused", out and out["refused"] == "trader-config", str(out))
    out = refused("projects", "base", {**me, "slug": "trader", "notes": ""})
    check("saying what does lift them (`!approve`), not asking for --notes that cannot",
          out and "!approve trader-config trader" in out["error"]
          and "--notes" not in out["error"], str(out))
    live = st.create("Work for tsk_live", role="implementor", project="trader",
                     scope={"cwd": str(repo), "repo": str(repo)})["id"]
    st.update(live, branch="silkworm/tsk_live")
    st.transition(live, T.RUNNING); st.transition(live, T.CANCELLED)
    r = _as_user(bot, g, "land", live, "--notes", "you said land the trader fix")
    check("landing dismissed trader work that touches its live config is refused",
          r.returncode == 1 and "trader-config" in r.stderr and live not in bot.landings,
          r.stdout + r.stderr)
    flagged = st.create("Work for tsk_live again", role="implementor", project="trader",
                        scope={"cwd": str(repo), "repo": str(repo)})["id"]
    st.update(flagged, branch="silkworm/tsk_live")
    st.transition(flagged, T.RUNNING); st.transition(flagged, T.AWAITING_APPROVAL)
    r = _as_user(bot, g, "approve", flagged, "--notes", "you said approve the trader fix")
    check("so is approving it, which lands it too",
          r.returncode == 1 and "trader-config" in r.stderr and flagged not in bot.landings
          and st.get(flagged)["state"] == T.AWAITING_APPROVAL, r.stdout + r.stderr)
    bot.state.approve("accept", live, by="U_ME")
    r = _as_user(bot, g, "land", live, "--notes", "you said land the trader fix")
    check("a left-over accept approval does not lift it either",
          r.returncode == 1 and "trader-config" in r.stderr and live not in bot.landings
          and bot.state.has("accept", live), r.stdout + r.stderr)
    bot.state.approve("trader-config", live, by="U_ME")
    r = _as_user(bot, g, "land", live, "--notes", "you said land the trader fix")
    check("an !approve trader-config of it does, once", r.returncode == 0
          and live in bot.landings and not bot.state.has("trader-config", live),
          r.stdout + r.stderr)
    bot.state.approve("trader-config", "trader", by="U_ME")
    check("an !approve still lifts it, as before",
          refused("projects", "base", {**me, "slug": "trader"}) is None)


# --- trader-config and publish on your word: a plan entry, or naming it ---------
# 2026-11-19: tsk_b921b84c73 and tsk_e8758becd0 waited on a typed
# `!approve trader-config`. Now a yes to the plan entry, or a message naming
# the landing, lifts it for that one item, once -- from Slack or the dashboard.


class _Projects:
    """A project store with a publish switch, for /projects publish."""

    def __init__(self, recs):
        self.recs = {r["slug"]: dict(r) for r in recs}

    def get(self, slug):
        r = self.recs.get(slug)
        return dict(r) if r else None

    def ensure(self, slug, **kw):
        self.recs[slug].update(kw)
        return dict(self.recs[slug])

    def scope_for(self, slug):
        return {"branch": "main"}


def _lift_bot(repo):
    """_said_bot also serving /ops (plans) and /projects (publish), as shipped."""
    import ops as OPS
    bot = _said_bot()
    bot.projects = _Projects([
        {"slug": "cadence", "auto_merge": True, "publish": False},
        {"slug": "trader", "auto_merge": True, "publish": False}])
    shared = {k: v for k, v in bot.ns.items() if not k.startswith("__")}
    bot.told = []
    shared.update(project_store=bot.projects, ops=OPS, re=_re,
                  tell_thread=lambda key, text: bot.told.append((key, text)),
                  install_git_guards=lambda slug: None,
                  ops_context=lambda: OPS.Context(
                      project=lambda slug: None, python=sys.executable, silkworm_bin="/s",
                      uid=os.getuid(), launch_agents=Path(tempfile.mkdtemp())))
    ns2 = lift("handle_ops", "record_ops_plan", "guarded_plan_entry",
                        "handle_projects", **shared, absent=PROJECT_ROUTES + ('OPS_LOG',))
    bot.ns2 = ns2
    bot.server.route("/ops", ns2["handle_ops"])
    bot.server.route("/projects", ns2["handle_projects"])
    bot.repo = repo
    return bot


def _live_task(bot, branch="silkworm/tsk_live", state=None, title="Lever up"):
    """A trader task whose branch changes scripts/paper_trade.py."""
    import tasks as T
    st = bot.store
    tid = st.create(f"{title} ({branch})", role="implementor", project="trader",
                    scope={"cwd": str(bot.repo), "repo": str(bot.repo)})["id"]
    st.update(tid, branch=branch)
    st.transition(tid, T.RUNNING); st.transition(tid, state or T.DONE)
    return tid


def _answer(bot, text, via=None):
    """Your message's turn, answering the live plan as handle_prompt does."""
    ts = f"{time.time() + 1:.6f}"
    return bot.ns["OPERATOR_GRANTS"].open(HOME, ts, "U_ME", text, bot.turn,
                                          plan=bot.state.answer_plan(ts, text), via=via), ts


def _plan(bot, *argv):
    return _op_cli(bot, "ops", "plan", *argv, "--by", "operator", thread=HOME,
                   task="tsk_sweep")


def test_operator_trader_landing_lifts_on_a_yes_to_its_plan_entry():
    import tasks as T
    print("\nthe operator: a trader live-config landing lifts on your yes to its plan entry, once")
    repo = _op_repo()
    _op_git(repo, "branch", "silkworm/tsk_live2", "silkworm/tsk_live")
    bot = _lift_bot(repo)
    st = bot.store
    live, other = _live_task(bot), _live_task(bot, "silkworm/tsk_live2", title="Lever more")
    plain = _live_task(bot, "silkworm/tsk_new", title="New work")
    g0, _ = _answer(bot, "yes")
    r = _as_user(bot, g0, "land", live, "--notes", "you said yes")
    check("with no plan, your 'yes' does not land it; the refusal says to record a plan",
          r.returncode == 1 and live not in bot.landings and "trader-config" in r.stderr
          and f"ops plan --land {live}" in r.stderr and "!approve" not in r.stderr,
          r.stdout + r.stderr)
    r = _as_sweep(bot, "land", live, "--notes", "the user wants it")
    check("a sweep is refused the same way", r.returncode == 1 and live not in bot.landings
          and f"ops plan --land {live}" in r.stderr, r.stdout + r.stderr)
    r = _plan(bot, "--land", plain)
    check("a landing that touches no live config needs no plan entry",
          r.returncode == 1 and "needs no plan entry" in r.stderr, r.stdout + r.stderr)
    r = _plan(bot, "--land", "tsk_nosuch")
    check("nor can a task that does not exist be planned",
          r.returncode == 1 and "no task tsk_nosuch" in r.stderr, r.stdout + r.stderr)
    r = _plan(bot, "--land", live)
    shown = (f"1. `land {live}` -- needs trader-config: lands silkworm/tsk_live into main; "
             "changes trader's live config: scripts/paper_trade.py")
    check("a sweep records it, showing the guard and the live files it changes",
          r.returncode == 0 and shown in r.stdout, r.stdout + r.stderr)
    check("and Silkworm itself posts the entries to the thread, not only the operator",
          bot.told and bot.told[-1][0] == HOME and shown in bot.told[-1][1], bot.told)
    r = _as_sweep(bot, "land", live, "--notes", "planned")
    check("the sweep that recorded it still cannot land it",
          r.returncode == 1 and live not in bot.landings, r.stdout + r.stderr)
    g, ts = _answer(bot, "yes")
    r = _as_user(bot, g, "land", other, "--notes", "you said yes")
    check("your yes lifts nothing for a task not in the plan",
          r.returncode == 1 and other not in bot.landings
          and "ops plan --land" in r.stderr, r.stdout + r.stderr)
    r = _as_user(bot, g, "land", live)
    check("--notes are still needed", r.returncode == 1 and live not in bot.landings
          and "--notes" in r.stderr, r.stdout + r.stderr)
    r = _as_user(bot, g, "land", live, "--notes", "you said yes")
    res = (st.get(live) or {}).get("result") or {}
    lift = res.get("decided_lift") or {}
    ev = [e for e in st.get(live)["events"] if e["kind"] == "decided"]
    check("after your yes, the planned landing goes ahead",
          r.returncode == 0 and bot.landings.count(live) == 1, r.stdout + r.stderr)
    check("and the task records the plan id, the entry, your message and where it came from",
          lift.get("how") == "plan" and lift.get("plan_id", "").startswith("plan_")
          and lift.get("entry") == 1 and lift.get("ts") == ts and lift.get("guard")
          == "trader-config" and "Slack message" in lift.get("via", "")
          and res.get("decided_ts") == ts and ev and lift["plan_id"] in ev[-1]["detail"], res)
    check("which compaction keeps", "decided_lift" in T.RESULT_KEEPS)
    r = _as_user(bot, g, "land", live, "--notes", "you said yes")
    check("and only once: the same yes does not land it again",
          r.returncode == 1 and bot.landings.count(live) == 1
          and "already been carried out" in r.stderr, r.stdout + r.stderr)

    # "do 2" lifts entry 2 only.
    two = _live_task(bot, "silkworm/tsk_live2", state=T.AWAITING_APPROVAL,
                     title="Lever again")
    r = _plan(bot, "--land", other, "--approve", two)
    check("a plan can name several, land and approve alike",
          r.returncode == 0 and f"2. `approve {two}` -- needs trader-config" in r.stdout,
          r.stdout + r.stderr)
    g2, _ = _answer(bot, "do 2")
    r = _as_user(bot, g2, "land", other, "--notes", "you said do 2")
    check("'do 2' does not lift entry 1", r.returncode == 1 and other not in bot.landings,
          r.stdout + r.stderr)
    out = bot.ns["handle_tasks"]({"action": "land", "id": two, "caller_role": "operator",
                                  "caller_thread": HOME, "caller_grant": g2,
                                  "caller_task": bot.turn, "notes": "you said do 2"})
    check("an approve entry does not lift a land of it", not out["ok"]
          and two not in bot.landings, str(out))
    r = _as_user(bot, g2, "approve", two, "--notes", "you said do 2")
    check("and lifts entry 2 for its approve", r.returncode == 0 and two in bot.landings,
          r.stdout + r.stderr)

    # A landing that never starts gives its entry back.
    r = _plan(bot, "--land", other)
    g3, _ = _answer(bot, "go ahead")
    keep = bot.ns["start_landing"]
    bot.ns["start_landing"] = lambda tid, **k: False
    r = _as_user(bot, g3, "land", other, "--notes", "you said go ahead")
    check("a landing that could not start is refused", r.returncode == 1
          and other not in bot.landings, r.stdout + r.stderr)
    check("and the entry is still unused", not ((bot.state.plan() or {}).get("entries") or [{"used": 1}])[0]["used"],
          bot.state.plan())
    bot.ns["start_landing"] = keep
    r = _as_user(bot, g3, "land", other, "--notes", "you said go ahead")
    check("so the same yes lands it when it can", r.returncode == 0
          and other in bot.landings, r.stdout + r.stderr)
    g4, _ = _answer(bot, f"land {plain}")
    check("an entry for another turn's message is not this one's",
          not bot.ns["handle_tasks"]({"action": "land", "id": live, "caller_role": "operator",
                                      "caller_thread": HOME, "caller_grant": g4,
                                      "caller_task": bot.turn, "notes": "x"})["ok"])


def test_operator_trader_landing_lifts_when_your_message_names_it():
    import tasks as T
    print("\nthe operator: a trader live-config landing lifts when your message names it, "
          "from Slack or the dashboard, once")
    repo = _op_repo()
    bot = _lift_bot(repo)
    st = bot.store
    a, b = _live_task(bot), _live_task(bot, title="Lever twice")
    rev = _live_task(bot, state=T.AWAITING_APPROVAL, title="Lever reviewed")
    via = {"via": "dashboard", "login": "me@example.com", "node": "phone", "peer": "100.64.0.9"}
    g, ts = _answer(bot, f"land {a}", via=via)
    r = _as_user(bot, g, "land", b, "--notes", f"you said land {a}")
    check("a message naming one landing lifts nothing for another",
          r.returncode == 1 and b not in bot.landings, r.stdout + r.stderr)
    r = _as_user(bot, g, "land", a, "--notes", f"you said land {a}")
    res = st.get(a)["result"]
    check("a dashboard message naming it lands it", r.returncode == 0 and a in bot.landings,
          r.stdout + r.stderr)
    check("recording that the message named it, and where it came from",
          res.get("decided_lift", {}).get("how") == "named"
          and "dashboard message" in res.get("decided_via", "")
          and "me@example.com" in res.get("decided_via", "") and res.get("decided_ts") == ts,
          res)
    r = _as_user(bot, g, "land", a, "--notes", f"you said land {a}")
    check("once", r.returncode == 1 and bot.landings.count(a) == 1, r.stdout + r.stderr)
    g2, _ = _answer(bot, f"don't land {b} yet")
    r = _as_user(bot, g2, "land", b, "--notes", "x")
    check("'don't land' names nothing", r.returncode == 1 and b not in bot.landings,
          r.stdout + r.stderr)
    g3, _ = _answer(bot, f"should we land {b}?")
    r = _as_user(bot, g3, "land", b, "--notes", "x")
    check("nor does a question", r.returncode == 1 and b not in bot.landings,
          r.stdout + r.stderr)
    g4, _ = _answer(bot, f"approve {rev}")
    r = _as_user(bot, g4, "approve", rev, "--notes", f"you said approve {rev}")
    check("naming reviewed work approves and lands it (its accept on your word too)",
          r.returncode == 0 and rev in bot.landings
          and st.get(rev)["state"] != T.AWAITING_APPROVAL, r.stdout + r.stderr)
    out = bot.ns["handle_tasks"]({"action": "land", "id": b, "caller_role": "operator",
                                  "caller_thread": HOME, "caller_task": "tsk_wake",
                                  "notes": f"land {b}"})
    check("a wake-up, with no grant, is refused", not out["ok"]
          and out["refused"] == "trader-config" and b not in bot.landings, str(out))
    bot.state.approve("trader-config", b, by="U_ME")
    r = _as_sweep(bot, "land", b, "--notes", "approved")
    check("`!approve trader-config` keeps working", r.returncode == 0 and b in bot.landings,
          r.stdout + r.stderr)


def test_operator_publish_lifts_on_your_word_for_that_project():
    print("\nthe operator: publish on lifts on a yes to its plan entry, or your message "
          "naming it; never a sweep")
    bot = _lift_bot(_op_repo())
    pub = lambda: bot.projects.recs["cadence"]["publish"]
    r = _op_cli(bot, "publish", "cadence", "on", "--by", "operator", "--notes", "x",
                thread=HOME, task="tsk_sweep")
    check("a sweep turning publish on is refused, saying to record a plan",
          r.returncode == 1 and not pub() and "ops plan --publish cadence" in r.stderr,
          r.stdout + r.stderr)
    r = _plan(bot, "--publish", "trader")
    check("trader's publish is its live config: not plannable as publish",
          r.returncode == 1 and "trader-config" in r.stderr, r.stdout + r.stderr)
    r = _plan(bot, "tailscale-on", "--publish", "cadence")
    check("publish is planned beside an op, numbered after it",
          r.returncode == 0 and "1. `tailscale-on`" in r.stdout
          and "2. `publish cadence` -- needs publish: turns publish on for cadence"
          in r.stdout, r.stdout + r.stderr)
    g, ts = _answer(bot, "do 1")
    r = _as_user(bot, g, "publish", "cadence", "on", "--notes", "you said do 1")
    check("'do 1' does not lift entry 2", r.returncode == 1 and not pub(), r.stdout + r.stderr)
    r = _plan(bot, "--publish", "cadence")
    g, ts = _answer(bot, "yes")
    r = _as_user(bot, g, "publish", "cadence", "on", "--notes", "you said yes")
    check("your yes to it turns publish on", r.returncode == 0 and pub()
          and "on the user's word" in r.stdout, r.stdout + r.stderr)
    bot.projects.recs["cadence"]["publish"] = False
    r = _as_user(bot, g, "publish", "cadence", "on", "--notes", "you said yes")
    check("once", r.returncode == 1 and not pub(), r.stdout + r.stderr)
    g2, _ = _answer(bot, "turn publish on for cadence")
    r = _as_user(bot, g2, "publish", "cadence", "on", "--notes", "you said turn it on")
    check("a message naming it turns it on", r.returncode == 0 and pub(), r.stdout + r.stderr)
    bot.projects.recs["cadence"]["publish"] = False
    r = _as_user(bot, g2, "publish", "cadence", "on", "--notes", "you said turn it on")
    check("also once", r.returncode == 1 and not pub(), r.stdout + r.stderr)
    r = _as_sweep(bot, "publish", "cadence", "off", "--notes", "x")
    check("turning it off was never guarded", r.returncode == 0, r.stdout + r.stderr)
    bot.projects.recs["cadence"]["auto_merge"] = False
    _plan(bot, "--publish", "cadence")
    g3, _ = _answer(bot, "yes")
    r = _as_user(bot, g3, "publish", "cadence", "on", "--notes", "you said yes")
    check("a publish the switch refuses (auto-merge off) does not happen",
          r.returncode == 1 and not pub() and "auto-merge" in r.stderr, r.stdout + r.stderr)
    check("and gives its entry back", not ((bot.state.plan() or {}).get("entries") or [{"used": 1}])[0]["used"],
          bot.state.plan())


def test_guard_lift_rules():
    import operator_role as O
    print("\nguard_lift: a plan entry your answer approved, or your message naming it")
    yes = lambda t: [O.names_action(x, "trader-config", "tsk_ab12") for x in t]
    check("names: a clause that is only the verb, the items, and asking or joining",
          yes(["land tsk_ab12", "please `land tsk_ab12`.", "land tsk_x and tsk_ab12",
               "land tsk_x, tsk_ab12", "Go ahead and merge tsk_ab12 👍",
               "approve tsk_ab12 please", "thanks. land tsk_ab12"]) == [True] * 7)
    no = ["don't land tsk_ab12", "never land tsk_ab12", "land tsk_ab12?",
          "land tsk_x but not tsk_ab12", "dismiss tsk_ab12", "land tsk_ab123",
          "tsk_ab12 looks fine", "land tsk_x. close tsk_ab12",
          "I don't think we should land tsk_ab12 yet", "I'm not sure we want to land tsk_ab12",
          "do not, under any circumstances, land tsk_ab12", "Should I land tsk_ab12",
          "what would happen if we land tsk_ab12",
          "the landing of tsk_ab12 failed last night, what happened",
          "merge conflicts in tsk_ab12 look bad",
          "land tsk_cd34 and investigate tsk_ab12",
          "land tsk_cd34 and explain why tsk_ab12 is risky",
          "land tsk_ab12 once the backtest from tsk_cd34 passes",
          "> land tsk_ab12\nwhy did you suggest this"]
    said = yes(no)
    check("not: a no, a question, any other word in the clause, a quote",
          said == [False] * len(no), [t for t, y in zip(no, said) if y])
    check("land names a landing, approve an approval: not each other",
          not O.names_action("land tsk_ab12", "trader-config", "tsk_ab12", "approve")
          and not O.names_action("approve tsk_ab12", "trader-config", "tsk_ab12", "land")
          and O.names_action("merge tsk_ab12", "trader-config", "tsk_ab12", "land"))
    check("publish: 'publish cadence', 'turn publish on for cadence'",
          O.names_action("turn publish on for cadence", "publish", "cadence")
          and O.names_action("publish cadence", "publish", "cadence")
          and not O.names_action("land cadence", "publish", "cadence")
          and not O.names_action("publishing cadence broke yesterday", "publish", "cadence")
          and not O.names_action("publish alpha and cadence", "publish", "alpha")
          and not O.names_action("publish cadence", "trader-config", "cadence"))
    plan = {"id": "plan_x", "answered": "5.0",
            "entries": [{"n": 1, "name": "land", "args": ["tsk_ab12"], "guard": "trader-config"},
                        {"n": 2, "name": "publish", "args": ["cadence"], "guard": "publish"}]}
    g = {"ts": "5.0", "user": "U_ME", "text": "yes", "plan": {"id": "plan_x"}, "via": "slack"}
    lift, why = O.guard_lift(g, "trader-config", "land", "tsk_ab12", plan)
    check("a yes to entry 1 lifts it", lift and lift["entry"] == 1 and lift["how"] == "plan",
          why)
    check("for its own action only", not O.guard_lift(g, "trader-config", "approve",
                                                     "tsk_ab12", plan)[0])
    check("not for publish of it, an action of another guard",
          not O.guard_lift(g, "trader-config", "publish", "tsk_ab12", plan)[0])
    check("nor a sweep (no grant)", not O.guard_lift(None, "trader-config", "land",
                                                    "tsk_ab12", plan)[0])
    check("nor once the plan was answered by another message",
          not O.guard_lift(g, "trader-config", "land", "tsk_ab12",
                           dict(plan, answered="6.0"))[0])
    check("nor once replaced", not O.guard_lift(g, "trader-config", "land", "tsk_ab12",
                                                dict(plan, id="plan_y"))[0])
    used = dict(plan, entries=[dict(plan["entries"][0], used=1.0)])
    check("nor once used", not O.guard_lift(g, "trader-config", "land", "tsk_ab12", used)[0])
    g2 = dict(g, text="do 2")
    check("'do 2' lifts publish cadence, not entry 1",
          O.guard_lift(g2, "publish", "publish", "cadence", plan)[0]
          and not O.guard_lift(g2, "trader-config", "land", "tsk_ab12", plan)[0])
    check("naming trader does not lift its live config for a project setting",
          not O.guard_lift(dict(g, text="land trader"), "trader-config", "publish",
                           "trader", plan)[0])
    check("a project setting of trader stays !approve-only",
          not O.guard_lift(dict(g, text="set base trader"), "trader-config", "base",
                           "trader", plan)[0]
          and "!approve trader-config trader" in O.refusal("trader-config", "trader", "base"))
    tmp = Path(tempfile.mkdtemp())
    st = O.State(tmp / "operator.json")
    check("a named lift is claimed once per message",
          st.use_said_lift("5.0", "publish", "cadence")
          and not st.use_said_lift("5.0", "publish", "cadence")
          and st.use_said_lift("6.0", "publish", "cadence"))
    st.release_said_lift("5.0", "publish", "cadence")
    check("and given back if its action did not happen",
          st.use_said_lift("5.0", "publish", "cadence"))
    check("the prompts no longer send the user to type !approve",
          "never ask them to type `!approve`" in O.SWEEP_NOTE
          and "Never ask them to type `!approve`" in __import__("roles").OPERATOR_SYSTEM)


def test_operator_grant_is_made_only_for_your_own_message():
    import operator_role as O
    print("\nthe operator's grant: made for your Slack message's turn in its thread, and no other")
    ns = lift("user_started", BOT_USER_ID="UBOT", ALLOWED_USERS={"U_ME"})
    started = ns["user_started"]
    check("your Slack message starts a turn of yours", started({"ts": "1.0"}, "U_ME"))
    check("the web chat does not, unless the dashboard vouched it was you",
          not started({"_web": True}, "U_ME"))
    check("nor the bot, another app, or someone off the allowlist",
          not started({}, "UBOT") and not started({"bot_id": "B1"}, "U_ME")
          and not started({}, "U_STRANGER") and not started({}, ""))
    gs = O.Grants()
    t = gs.open("D1:1.0", "5.0", "U_ME", "accept tsk_1", "tsk_turn")
    check("a grant answers only for its own thread and turn",
          gs.get(t, "D1:1.0", "tsk_turn") and not gs.get(t, "D2:2.0", "tsk_turn")
          and not gs.get(t, "D1:1.0", "tsk_sweep") and not gs.get("", "D1:1.0", "tsk_turn")
          and not gs.get("f" * 32, "D1:1.0", "tsk_turn"))
    gs.close(t)
    check("and not after it is closed", not gs.get(t, "D1:1.0", "tsk_turn"))
    src = (BASE / "bot.py").read_text()
    tree = ast.parse(src)
    opens = [n for f in tree.body if isinstance(f, ast.FunctionDef)
             for n in ast.walk(f) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "open"
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "OPERATOR_GRANTS"]
    owners = {f.name for f in tree.body if isinstance(f, ast.FunctionDef)
              for n in ast.walk(f) if n in opens}
    check("only handle_prompt makes one -- never the queue runner, a sweep or a route",
          len(opens) == 1 and owners == {"handle_prompt"}, owners)
    hp = next(f for f in tree.body if isinstance(f, ast.FunctionDef) and f.name == "handle_prompt")
    seg = ast.get_source_segment(src, hp)
    check("for the operator's thread, on user_started, and closed when the turn ends",
          "role_name == roles.OPERATOR and user_started(event, user)" in seg
          and "OPERATOR_GRANTS.close(grant)" in seg)
    ce = next(f for f in tree.body if isinstance(f, ast.FunctionDef) and f.name == "claude_env")
    check("no other turn's environment inherits one",
          "env.pop(operator_role.GRANT_VAR, None)" in ast.get_source_segment(src, ce))


def test_operator_your_word_outlives_the_run_it_queued():
    import costs, tasks as T
    print("\nwho decided survives the run it queued, and compaction")
    tmp = Path(tempfile.mkdtemp())
    st = T.TaskStore(tmp / "t.json")
    t = st.create("Retry sync on 429", role="implementor")["id"]
    st.record_decision(t, "accept decided by U_ME (Slack message 9.1) via operator: yes",
                       "user via operator", "9.1")
    st.transition(t, T.RUNNING)
    st.update(t, result={"text": "done", **costs.add_run(st.get(t), 0.5)})
    res = st.get(t)["result"]
    check("a run rewriting result keeps decided_by and its message",
          res.get("decided_by") == "user via operator" and res.get("decided_ts") == "9.1", res)
    check("compaction keeps them", {"decided_by", "decided_ts"} <= set(T.RESULT_KEEPS))


def test_operator_release_cli_is_the_release_button():
    import operator_role as O
    import tasks as T
    from localserver import LocalServer
    import socket as _socket
    print("\n`silkworm release`: the Release button's plan-then-start, on your word only")
    fx = _release_fixture()
    tmp = Path(tempfile.mkdtemp())
    state = O.State(tmp / "operator.json")
    state.set_thread(HOME)
    grants = O.Grants()
    fx.ns.update(operator_role=O, OPERATOR=state, OPERATOR_GRANTS=grants,
                 task_store=T.TaskStore(tmp / "t.json"))
    lift({"operator_refused", "user_grant", "task_contained", "task_live_paths"}, ns=fx.ns)
    asked = []
    hr = fx.ns["handle_releases"]

    def route(payload):
        asked.append(payload.get("action"))
        return hr(payload)
    with _socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = LocalServer(port)
    server.route("/releases", route)
    server.start()
    bot = types.SimpleNamespace(port=port, statefile=tmp / "operator.json")
    tags = lambda: sorted(fx.g("tag", "-l").stdout.split())

    r = _op_cli(bot, "release", "alpha", "backend", "--notes", "release it", thread=HOME,
                task="tsk_sweep")
    check("a sweep's release is refused by the CLI before asking", r.returncode == 1
          and "!approve release alpha" in r.stderr and not asked, r.stdout + r.stderr)
    out = hr({"action": "start", "slug": "alpha", "caller_role": "operator",
              "caller_thread": HOME, "confirm": "x", "notes": "release"})
    check("and by the bot", not out["ok"] and out.get("refused") == "release", str(out))
    g = grants.open(HOME, "1761300000.000100", "U_ME", "release alpha's backend", "tsk_turn")
    r = _op_cli(bot, "release", "alpha", "backend", "--level", "major", "--notes", "x",
                thread=HOME, grant=g, task="tsk_turn")
    check("major is not a CLI release (one target, read first: !release or the button)",
          r.returncode == 2, r.stdout + r.stderr)
    r = _op_cli(bot, "release", "alpha", "backend", thread=HOME, grant=g, task="tsk_turn")
    check("without --notes it is refused", r.returncode == 1 and "--notes" in r.stderr
          and not asked, r.stdout + r.stderr)

    fx.ns["DRAIN"].start(600, "deploy")
    r = _op_cli(bot, "release", "alpha", "backend", "--notes", "you said release backend",
                thread=HOME, grant=g, task="tsk_turn")
    check("while a deploy drains it is refused, as the button is",
          r.returncode == 1 and "draining" in r.stderr and tags() == [], r.stdout + r.stderr)
    fx.ns["DRAIN"].stop()
    fx.ns["_releasing"].add("alpha")
    r = _op_cli(bot, "release", "alpha", "backend", "--notes", "you said release backend",
                thread=HOME, grant=g, task="tsk_turn")
    check("while one runs it is refused, by the same _releasing lock",
          r.returncode == 1 and "already running" in r.stderr, r.stdout + r.stderr)
    fx.ns["_releasing"].discard("alpha")
    asked.clear()
    r = _op_cli(bot, "release", "alpha", "backend", "--notes", "you said release backend",
                thread=HOME, grant=g, task="tsk_turn")
    check("in your turn it starts", r.returncode == 0 and "started" in r.stdout,
          r.stdout + r.stderr)
    check("as the button does: the plan, then a start carrying its confirmation",
          asked == ["plan", "start"], asked)
    st = _wait_release(hr, "alpha")
    check("and releases what was planned, through start_release and run_release",
          st["run"] and st["run"]["released"] == ["backend/v1.0.0"]
          and "backend/v1.0.0" in tags(), st)
    check("its thread says it was on your word, with your message",
          "for U_ME (Slack message 1761300000.000100)" in (st["run"] or {}).get("by", ""),
          st["run"])
    src = (BASE / "bin" / "silkworm").read_text()
    check("`release` is a CLI subcommand on the operator's allowlist",
          'elif cmd == "release":' in src
          and "Bash(/s release:*)" in __import__("roles").permission_args(
              "operator", [], bin="/s")[1])


def test_operator_prompts_say_to_do_what_you_decided():
    import operator_role as O, roles
    print("\nthe operator's prompts: in your turn do what you decided; in a sweep, nothing lifts")
    text = roles.system_prompt(O.ROLE, bin="/s")
    check("the home-thread brief says to carry out what the user decided",
          "When the user decides, do it" in text and "--notes" in text
          and "/s release <project>" in text, text)
    check("report each action with its id, never decide alone, ask when unclear",
          "with its\ntask id" not in text and "task id" in text
          and "Never decide anything on your own judgement" in text
          and "ask before acting" in text)
    check("and that a sweep or wake-up lifts nothing",
          "your daily sweep, a wake-up" in text)
    check("the sweep keeps its 'Not yours' list and says nothing said earlier lifts it",
          "Not yours" in O.SWEEP_GOAL and "nothing the user said earlier lifts them" in O.SWEEP_GOAL)

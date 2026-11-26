"""Named ops and plans of ops.

Run by tests/run.py with every other tests/test_*.py; see tests/harness.py."""

from harness import *  # noqa: F401,F403


# --- named ops: the operator runs what it used to hand you ---------------------
# 2026-10-26: "You should be able to run these yourself." `silkworm tailscale
# on`, reinstalling trader's pooldata launchd job, and trader's one-off refetch
# with --apply were all handed to the owner as shell steps. Now they are named
# entries in ops.py, run by the bot from their exact argv, on your word.


def _ops_turn(w, text, turn="tsk_turn_mine"):
    """Open the grant handle_prompt opens for your own Slack message."""
    return w.grants.open("D1:1.0", "1761400000.000100", "U_ME", text, turn)


def _ops_as_sweep(w, *argv):
    return _ops_cli(w, "run", *argv, "--by", "operator", task="tsk_sweep")


def _ops_ask(w, payload):
    """handle_ops called directly, as the route would; a handler that raises
    reads as not done (the server answers {}), and fails a check cleanly."""
    try:
        return w.ns["handle_ops"](payload)
    except Exception as e:
        return {"ok": None, "error": f"raised {e!r}"}


def test_ops_each_runs_its_exact_argv():
    import ops as OPS
    print("\nnamed ops: each builds exactly the argv its entry says, and runs it with no shell")
    w = _ops_world()
    py, sw = sys.executable, str(BASE / "bin" / "silkworm")
    p = OPS.plan("tailscale-status", [], w.ctx)
    check("tailscale-status is `silkworm tailscale status`, reading only",
          [s.argv for s in p.steps] == [[py, sw, "tailscale", "status"]] and p.read_only,
          [s.argv for s in p.steps])
    p = OPS.plan("tailscale-on", [], w.ctx)
    check("tailscale-on is `silkworm tailscale on` -- serve, never funnel -- and changes things",
          [s.argv for s in p.steps] == [[py, sw, "tailscale", "on"]] and not p.read_only)
    src = str((w.trader / "ops" / "com.traderjoe.pooldata.plist").resolve())
    dst = str(w.agents / "com.traderjoe.pooldata.plist")
    p = OPS.plan("trader-launchd-reinstall", ["com.traderjoe.pooldata"], w.ctx)
    check("trader-launchd-reinstall lints, copies, boots out and bootstraps that plist",
          [s.argv for s in p.steps] == [
              ["plutil", "-lint", src], ["cp", src, dst],
              ["launchctl", "bootout", "gui/501/com.traderjoe.pooldata"],
              ["launchctl", "bootstrap", "gui/501", dst]]
          and [s.may_fail for s in p.steps] == [False, False, True, False]
          and p.cwd == str(w.trader) and not p.read_only, [s.argv for s in p.steps])
    tpy = str(w.trader / ".venv" / "bin" / "python3")
    p = OPS.plan("trader-refetch-revisions",
                 ["--apply", "--symbols", "BRK.A", "cat", "GLW", "--dividends"], w.ctx)
    check("trader-refetch-revisions passes --symbols, --dividends and --apply to the script",
          [s.argv for s in p.steps] == [[tpy, "scripts/refetch_revisions.py", "--symbols",
                                         "BRK.A", "CAT", "GLW", "--dividends", "--apply"]]
          and p.cwd == str(w.trader) and not p.read_only, [s.argv for s in p.steps])
    p = OPS.plan("trader-refetch-revisions", ["--symbols", "CRWD"], w.ctx)
    check("without --apply the refetch is a plan, and reads only",
          p.read_only and [s.argv for s in p.steps]
          == [[tpy, "scripts/refetch_revisions.py", "--symbols", "CRWD"]])
    p = OPS.plan("project-test", ["alpha"], w.ctx)
    check("project-test runs the project's test_cmd, split as verify splits it, under its "
          "lock -- in the live checkout, so it counts as changing things",
          [s.argv for s in p.steps] == [[f"{w.fakebin}/runtests", "--quick"]]
          and p.cwd == w.projects["alpha"]["scope"]["cwd"] and p.lock == "alpha"
          and not p.read_only)
    seen = []
    rec = OPS.run(OPS.plan("project-test", ["alpha"], w.ctx),
                  runner=lambda argv, **kw: seen.append((argv, kw)) or types.SimpleNamespace(
                      returncode=0, stdout="ok", stderr=""))
    check("and runs each step's argv as a list, never through a shell",
          seen and seen[0][0] == [f"{w.fakebin}/runtests", "--quick"]
          and "shell" not in seen[0][1] and seen[0][1]["cwd"] == p.cwd and rec["ok"], seen)
    tree = ast.parse((BASE / "ops.py").read_text())
    shells = [k for n in ast.walk(tree) if isinstance(n, ast.Call)
              for k in n.keywords if k.arg == "shell"]
    check("ops.py passes shell= nowhere", not shells)
    real = w.real_context()
    check("the bot's context is this machine's: its uid, its LaunchAgents, its silkworm",
          real.uid == os.getuid() and real.launch_agents == Path.home() / "Library" / "LaunchAgents"
          and real.silkworm_bin == str(BASE / "bin" / "silkworm"))


def test_ops_refuse_unknown_ops_bad_args_and_shell():
    import ops as OPS
    print("\nnamed ops: an unknown op, arguments it does not take, and shell are refused")
    w = _ops_world()

    def refused(name, args):
        try:
            OPS.plan(name, args, w.ctx)
        except OPS.Refused as e:
            return str(e)
        return ""

    check("an op that is not in the registry", "no op 'rm'" in refused("rm", ["-rf", "/"]))
    for bad in ("a;b", "a|b", "a&b", "$(id)", "`id`", "a b", "a>b", "a<b", "x'y", 'x"y',
                "a\nb", "~/x", "*", "a\\b", "(x)", "{x}"):
        check(f"shell metacharacters in {bad!r}", "never shell" in refused(
            "project-test", [bad]), refused("project-test", [bad]))
    check("an empty argument", "empty" in refused("project-test", [""]))
    check("tailscale ops take no arguments",
          "takes no arguments" in refused("tailscale-on", ["--funnel"])
          and "takes no arguments" in refused("tailscale-status", ["x"]))
    check("a job label that walks out of ops/",
          "not a launchd job label" in refused("trader-launchd-reinstall", ["../../etc/evil"]))
    check("a plist trader's ops/ does not have, naming what it does have",
          "no ops/com.traderjoe.nope.plist" in refused("trader-launchd-reinstall",
                                                       ["com.traderjoe.nope"])
          and "com.traderjoe.pooldata" in refused("trader-launchd-reinstall",
                                                  ["com.traderjoe.nope"]))
    check("one that is a symlink out of ops/",
          "no ops/com.traderjoe.link.plist" in refused("trader-launchd-reinstall",
                                                       ["com.traderjoe.link"]))
    check("one whose Label is not its name",
          "Label is 'com.traderjoe.other'" in refused("trader-launchd-reinstall",
                                                     ["com.traderjoe.mislabel"]))
    check("a job that is not trader's (com.traderjoe.*)",
          "not one of trader's jobs" in refused("trader-launchd-reinstall",
                                                ["com.silkworm.bot"]))
    plist = w.trader / "ops" / "com.traderjoe.pooldata.plist"
    good = plist.read_bytes()
    plist.write_bytes(good.replace(b"/bin/true", b"/tmp/anything"))
    check("a plist edited in the checkout and not committed",
          "not committed as it stands" in refused("trader-launchd-reinstall",
                                                  ["com.traderjoe.pooldata"]))
    plist.write_bytes(good)
    import plistlib
    with open(w.trader / "ops" / "com.traderjoe.fresh.plist", "wb") as f:
        plistlib.dump({"Label": "com.traderjoe.fresh"}, f)
    check("or one never committed at all",
          "not committed as it stands" in refused("trader-launchd-reinstall",
                                                  ["com.traderjoe.fresh"]))
    retired = w.trader / "data" / "retired-launchd"
    retired.mkdir(parents=True, exist_ok=True)
    gone = retired / "com.traderjoe.pooldata.20261115-093000-1.plist"
    gone.write_bytes(good)
    (retired / "com.traderjoe.pool.20261115-093000.plist").write_bytes(good)
    msg = refused("trader-launchd-reinstall", ["com.traderjoe.pooldata"])
    check("a job retired into data/retired-launchd, though its plist is still in ops/: "
          "a retired job stays retired", "was retired" in msg and gone.name in msg, msg)
    gone.unlink()
    check("and once that copy is moved out, it reinstalls -- another job's retired "
          "copy, whose name only starts the same, does not count",
          OPS.plan("trader-launchd-reinstall", ["com.traderjoe.pooldata"], w.ctx).steps)
    (w.agents / "com.traderjoe.pooldata.plist").symlink_to(w.tmp / "victim")
    check("a destination that is a symlink is not written through",
          "symlink" in refused("trader-launchd-reinstall", ["com.traderjoe.pooldata"]))
    (w.agents / "com.traderjoe.pooldata.plist").unlink()
    elsewhere = w.tmp / "trader2"
    elsewhere.mkdir()
    shutil.copytree(w.trader, elsewhere / "t", symlinks=True)
    shutil.rmtree(elsewhere / "t" / "ops")
    (elsewhere / "t" / "ops").symlink_to(w.trader / "ops")
    w.projects["trader"]["scope"]["cwd"] = str(elsewhere / "t")
    check("nor a checkout whose ops/ is a symlink",
          "symlink" in refused("trader-launchd-reinstall", ["com.traderjoe.pooldata"]))
    w.projects["trader"]["scope"]["cwd"] = str(w.trader)
    check("reinstall takes exactly one job",
          refused("trader-launchd-reinstall", []) and refused(
              "trader-launchd-reinstall", ["com.traderjoe.pooldata", "x"]))
    check("a refetch without symbols", "say which symbols" in refused(
        "trader-refetch-revisions", ["--apply"]))
    check("a refetch option it does not pass on",
          "not an option" in refused("trader-refetch-revisions", ["--symbols", "CAT", "--start",
                                                                  "2020-01-01"])
          and "not an option" in refused("trader-refetch-revisions", ["CAT"]))
    check("a symbol that is not a ticker",
          "not a ticker" in refused("trader-refetch-revisions", ["--symbols", "../x"])
          and "not a ticker" in refused("trader-refetch-revisions", ["--symbols", "A.B.C"]))
    check("project-test of an unknown project, or one with no test command",
          "no project 'nope'" in refused("project-test", ["nope"])
          and "no test command" in refused("project-test", ["beta"])
          and refused("project-test", []))
    with _fake_path(w):
        port, w.port = w.port, 1      # no bot: only the CLI can refuse
        r = _ops_cli(w, "run", "project-test", "alpha;reboot", role="", thread="")
        check("the CLI refuses shell before asking, exit 1",
              r.returncode == 1 and "never shell" in r.stderr, r.stderr)
        r = _ops_cli(w, "run", "nope", role="", thread="")
        check("and an unknown op", r.returncode == 1 and "no op 'nope'" in r.stderr)
        w.port = port
        out = w.ns["handle_ops"]({"action": "run", "name": "project-test",
                                  "args": ["alpha", "$(id)"]})
        check("the bot refuses them again on its own", not out["ok"]
              and out.get("refused") == "args", out)
        out = w.ns["handle_ops"]({"action": "run", "name": "project-test", "args": "alpha"})
        check("args that are not a list", not out["ok"] and "list" in out["error"], out)
        r = _ops_cli(w, "run", "trader-refetch-revisions", "--symbols", "CAT", "--notes",
                     "--apply", role="", thread="")
        check("--notes never swallows an option (--apply): a usage error",
              r.returncode == 2, r.stdout + r.stderr)
    check("nothing ran for any of it", _calls(w) == [], _calls(w))


def test_ops_run_through_the_bot_with_fakes():
    print("\nnamed ops: run by the bot, each step's argv, exit code and output in the "
          "reply and the ops log")
    w = _ops_world()
    with _fake_path(w):
        r = _ops_cli(w, "run", "tailscale-status", role="", thread="")
        check("tailscale-status asks tailscale, never serve or funnel",
              any(c.startswith("tailscale status") for c in _calls(w))
              and not any("serve --bg" in c or "funnel" in c for c in _calls(w)),
              _calls(w))
        check("its reply quotes the command and exit code",
              "tailscale status" in r.stdout and "[exit" in r.stdout, r.stdout + r.stderr)
        w.calls.write_text("")
        g = _ops_turn(w, "the phone can't reach the dashboard -- run tailscale on")
        r = _ops_as_user(w, g, "tailscale-on", "--notes", "user: run tailscale on")
        serves = [c for c in _calls(w) if c.startswith("tailscale serve")
                  and not c.startswith("tailscale serve status")]
        check("tailscale-on serves the dashboard's port in the background, and nothing else",
              serves == [f"tailscale serve --bg {os.environ.get('SILKWORM_VIZ_PORT', '8790')}"]
              and not any("funnel" in c for c in _calls(w)), _calls(w))
        w.calls.write_text("")
        g = _ops_turn(w, "reinstall the pooldata job now that the fix landed")
        r = _ops_as_user(w, g, "trader-launchd-reinstall", "com.traderjoe.pooldata",
                         "--notes", "user: reinstall pooldata")
        check("the reinstall runs plutil, cp and launchctl, in order",
              [c.split()[0] + " " + c.split()[1] for c in _calls(w)]
              == ["plutil -lint", "cp " + str((w.trader / "ops").resolve()
                                              / "com.traderjoe.pooldata.plist"),
                  "launchctl bootout", "launchctl bootstrap"]
              and r.returncode == 0, _calls(w) + [r.stdout, r.stderr])
        check("and the plist is in LaunchAgents",
              (w.agents / "com.traderjoe.pooldata.plist").read_bytes()
              == (w.trader / "ops" / "com.traderjoe.pooldata.plist").read_bytes())
        check("the reply has each step's command and exit code",
              r.stdout.count("[exit 0]") == 4 and "launchctl bootstrap gui/501" in r.stdout,
              r.stdout)
    with _fake_path(w, FAKE_BOOTOUT="113"):
        w.calls.write_text("")
        g = _ops_turn(w, "reinstall com.traderjoe.pooldata")
        r = _ops_as_user(w, g, "trader-launchd-reinstall", "com.traderjoe.pooldata",
                         "--notes", "user: reinstall it")
        check("a job that was not loaded (bootout fails) still gets bootstrapped",
              r.returncode == 0 and _calls(w)[-1].startswith("launchctl bootstrap"),
              _calls(w) + [r.stdout, r.stderr])
    with _fake_path(w, FAKE_BOOTSTRAP="5"):
        g = _ops_turn(w, "reinstall com.traderjoe.pooldata")
        r = _ops_as_user(w, g, "trader-launchd-reinstall", "com.traderjoe.pooldata",
                         "--notes", "user: reinstall it")
        check("a bootstrap that fails is a failed run, exit 1, saying exit 5",
              r.returncode == 1 and "[exit 5]" in r.stdout and "failed" in r.stderr,
              r.stdout + r.stderr)
    with _fake_path(w):
        w.calls.write_text("")
        g = _ops_turn(w, "after the close, run refetch_revisions for BRK.A and CAT with "
                         "--dividends --apply")
        r = _ops_as_user(w, g, "trader-refetch-revisions", "--symbols", "BRK.A", "CAT",
                         "--dividends", "--apply", "--notes", "user: refetch, apply")
        check("the refetch runs trader's python on the script with exactly those arguments",
              _calls(w) == ["python3 scripts/refetch_revisions.py --symbols BRK.A CAT "
                            "--dividends --apply"]
              and "plan: 3 symbols revised" in r.stdout and r.returncode == 0,
              _calls(w) + [r.stdout, r.stderr])
        r = _ops_cli(w, "run", "project-test", "alpha", role="", thread="")
        check("project-test runs the suite and quotes its tail",
              r.returncode == 0 and "12 passed, 0 failed" in r.stdout, r.stdout + r.stderr)
    with _fake_path(w, FAKE_TESTS="1"):
        r = _ops_cli(w, "run", "project-test", "alpha", role="", thread="")
        check("a failing suite is exit 1, its exit code said",
              r.returncode == 1 and "[exit 1]" in r.stdout, r.stdout + r.stderr)
    import ops as OPS
    runs = OPS.read_log(w.log, 50)
    re_ = next((x for x in runs if x.get("op") == "trader-refetch-revisions"), {})
    check("the ops log has each run: argv, exit code, tail, who and on whose word",
          re_.get("steps") and re_["steps"][0]["argv"][1:] == [
              "scripts/refetch_revisions.py", "--symbols", "BRK.A", "CAT", "--dividends",
              "--apply"]
          and re_.get("exit") == 0 and "plan: 3 symbols" in re_.get("tail", "")
          and re_.get("decided_by") == "U_ME" and re_.get("decided_ts") == "1761400000.000100"
          and re_.get("notes") == "user: refetch, apply" and re_.get("by") == "operator", re_)
    r = _ops_cli(w, "log", "-n", "50", role="", thread="")
    check("`silkworm ops log` reads it back, with the bot not asked",
          "trader-refetch-revisions --symbols BRK.A CAT" in r.stdout
          and "for U_ME (Slack message 1761400000.000100)" in r.stdout, r.stdout)
    r = _ops_cli(w, "list", "--json", role="", thread="")
    names = [d["name"] for d in json.loads(r.stdout)]
    check("`silkworm ops list` names every op", names == list(OPS.REGISTRY), names)


def test_ops_that_change_things_need_your_word():
    import ops as OPS
    print("\nnamed ops: reading is always allowed; changing needs your word, never a sweep's")
    w = _ops_world()
    with _fake_path(w):
        r = _ops_as_sweep(w, "tailscale-status")
        check("a read-only op runs in a sweep", r.returncode == 0 or "[exit" in r.stdout,
              r.stdout + r.stderr)
        r = _ops_as_sweep(w, "trader-refetch-revisions", "--symbols", "CAT")
        check("and so does a refetch plan (no --apply)",
              r.returncode == 0 and "plan: 3 symbols" in r.stdout, r.stdout + r.stderr)
        w.calls.write_text("")
        for argv in (["tailscale-on", "--notes", "x"],
                     ["trader-launchd-reinstall", "com.traderjoe.pooldata", "--notes", "x"],
                     ["trader-refetch-revisions", "--symbols", "CAT", "--apply",
                      "--notes", "x"]):
            r = _ops_as_sweep(w, *argv)
            check(f"a sweep's {argv[0]}{' --apply' if '--apply' in argv else ''} is refused",
                  r.returncode == 1 and "never in a sweep" in r.stderr, r.stderr)
        for argv in (["tailscale-on"], ["trader-refetch-revisions", "--symbols", "CAT",
                                        "--apply"]):
            out = _ops_ask(w, {"action": "run", "name": argv[0], "args": argv[1:],
                                      "caller_role": "operator", "caller_thread": "D1:1.0",
                                      "caller_task": "tsk_sweep", "notes": "x"})
            check(f"and refused by the bot itself ({argv[0]})",
                  not out["ok"] and out.get("refused") == "grant", out)
        g = _ops_turn(w, "run tailscale on please")
        out = _ops_ask(w, {"action": "run", "name": "tailscale-on", "args": [],
                                  "caller_role": "operator", "caller_thread": "D1:1.0",
                                  "caller_grant": g, "caller_task": "tsk_a_wakeup",
                                  "notes": "x"})
        check("your grant, sent from another turn (a wake-up's), lifts nothing",
              not out["ok"] and out.get("refused") == "grant", out)
        out = _ops_ask(w, {"action": "run", "name": "tailscale-on", "args": [],
                                  "caller_role": "assistant", "caller_thread": "C9:9.0",
                                  "caller_task": "tsk_x", "notes": "x"})
        check("nor does any other turn -- an assistant's -- run one", not out["ok"], out)
        check("none of those ran anything", not any(
            c.startswith(("tailscale serve", "launchctl", "cp ", "python3")) for c in _calls(w)),
            _calls(w))
        r = _ops_as_user(w, g, "tailscale-on")
        check("in your turn, without --notes, refused", r.returncode == 1
              and "--notes" in r.stderr, r.stderr)
        out = _ops_ask(w, {"action": "run", "name": "tailscale-on", "args": [],
                                  "caller_role": "operator", "caller_thread": "D1:1.0",
                                  "caller_grant": g, "caller_task": "tsk_turn_mine"})
        check("and by the bot", not out["ok"] and "--notes" in out["error"], out)
        g2 = _ops_turn(w, "fix the dashboard on my phone")
        r = _ops_as_user(w, g2, "tailscale-on", "--notes", "user: fix the dashboard")
        check("tailscale-on only when your message named it (tailscale)",
              r.returncode == 1 and "has to name it" in r.stderr, r.stderr)
        g3 = _ops_turn(w, "reinstall the dataupdate job")
        r = _ops_as_user(w, g3, "trader-launchd-reinstall", "com.traderjoe.pooldata",
                         "--notes", "user: reinstall")
        check("a trader op only when your message named it -- another job is not this one",
              r.returncode == 1 and "trader's live configuration" in r.stderr, r.stderr)
        for text in ("is tailscale still broken? just look", "check the tailscale-only setup"):
            g6 = _ops_turn(w, text)
            r = _ops_as_user(w, g6, "tailscale-on", "--notes", "x")
            check(f"tailscale-on is not named by {text!r}",
                  r.returncode == 1 and "has to name it" in r.stderr, r.stderr)
        g6 = _ops_turn(w, "refetch CAT --apply")
        r = _ops_as_user(w, g6, "trader-refetch-revisions", "--symbols", "CAT", "--apply",
                         "--notes", "x")
        check("a bare 'refetch' does not name trader-refetch-revisions",
              r.returncode == 1 and "has to name it" in r.stderr, r.stderr)
        import ops as _O
        check("whole words: a short last segment never names a job, the full label does",
              _O.named(("trader-launchd-reinstall", "com.traderjoe.a"), "do a reinstall")
              is False
              and "a" not in _O._names(_O.REGISTRY["trader-launchd-reinstall"],
                                       ["com.traderjoe.a"])
              and _O.named(("com.traderjoe.a",), "reinstall com.traderjoe.a please")
              and _O.named(("trader-refetch-revisions", "refetch_revisions"),
                           "run the refetch revisions script")
              and not _O.named(("pooldata",), "the pooldatafeed job"))
        r = _ops_as_sweep(w, "project-test", "alpha")
        check("project-test (a suite in the live checkout) is refused in a sweep",
              r.returncode == 1 and "never in a sweep" in r.stderr, r.stderr)
        g7 = _ops_turn(w, "run alpha's tests")
        r = _ops_as_user(w, g7, "project-test", "alpha", "--notes", "user: run alpha's tests")
        check("and runs on your word, without having to name the op",
              r.returncode == 0 and "12 passed" in r.stdout, r.stdout + r.stderr)
        g4 = _ops_turn(w, "accept tsk_1 and land tsk_2")
        r = _ops_as_user(w, g4, "trader-refetch-revisions", "--symbols", "CAT", "--apply",
                         "--notes", "x")
        check("a refetch --apply your message never mentioned is refused",
              r.returncode == 1 and "has to name it" in r.stderr, r.stderr)
        check("still nothing ran", not any(
            c.startswith(("tailscale serve", "launchctl", "cp ", "python3")) for c in _calls(w)),
            _calls(w))
        g5 = _ops_turn(w, "run tailscale-on")
        r = _ops_as_user(w, g5, "tailscale-on", "--notes", "user: run tailscale-on")
        check("named, with notes, in your turn: it runs", any(
            c.startswith("tailscale serve --bg") for c in _calls(w)), r.stdout + r.stderr)
        w.grants.close(g5)
        w.calls.write_text("")
        r = _ops_as_user(w, g5, "tailscale-on", "--notes", "user: run tailscale-on")
        check("once your turn has ended its grant runs nothing",
              r.returncode == 1 and not _calls(w), r.stderr)
        r = _ops_cli(w, "run", "tailscale-on", role="", thread="")
        check("your own terminal (no turn) runs it", any(
            c.startswith("tailscale serve --bg") for c in _calls(w)), r.stdout + r.stderr)
    refusals = [x for x in OPS.read_log(w.log, 100) if x.get("refused")]
    check("each refusal is in the ops log, with who asked",
          len(refusals) >= 6 and all(x.get("by") and x.get("op") for x in refusals),
          len(refusals))
    # The CLI's half alone: with no bot to ask, a sweep's mutating op is
    # refused (1), not "could not reach the bot" (3).
    env_bot = w.port
    w.port = 1
    r = _ops_as_sweep(w, "tailscale-on", "--notes", "x")
    check("the CLI refuses a sweep's mutating op before asking the bot",
          r.returncode == 1 and "never in a sweep" in r.stderr, r.stderr)
    r = _ops_as_user(w, "f" * 32, "tailscale-on")
    check("and one with a grant but no --notes", r.returncode == 1 and "--notes" in r.stderr,
          r.stderr)
    w.port = env_bot


def test_ops_one_run_at_a_time_and_within_its_timeout():
    import ops as OPS
    print("\nnamed ops: one run of an op at a time, and each within its timeout")
    w = _ops_world()
    p = OPS.plan("project-test", ["alpha"], w.ctx)
    p.timeout = 1

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
    rec = OPS.run(p, runner=slow)
    check("a step out of time is a failed run with no exit code, saying so",
          not rec["ok"] and rec["exit"] is None and "timed out" in rec["tail"], rec)
    entered, release = __import__("threading").Event(), __import__("threading").Event()

    def held(argv, **kw):
        entered.set()
        release.wait(5)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    p = OPS.plan("tailscale-status", [], w.ctx)
    t = __import__("threading").Thread(target=OPS.run, args=(p,), kwargs={"runner": held})
    t.start()
    entered.wait(5)
    try:
        OPS.run(OPS.plan("tailscale-status", [], w.ctx), runner=held)
        second = ""
    except OPS.Refused as e:
        second = str(e)
    release.set()
    t.join(5)
    check("a second run while the first is going is refused", "already running" in second,
          second)
    rec = OPS.run(OPS.plan("tailscale-status", [], w.ctx),
                  runner=lambda a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    check("and runs once the first is done", rec["ok"], rec)
    import verify
    ran = []
    said = []

    def attempt():
        try:
            OPS.run(OPS.plan("project-test", ["alpha"], w.ctx),
                    runner=lambda a, **k: ran.append(a) or types.SimpleNamespace(
                        returncode=0, stdout="", stderr=""))
            said.append("")
        except OPS.Refused as e:
            said.append(str(e))
    # In a thread, bounded: an op that waited on the lock would otherwise
    # hang this test rather than fail it.
    with verify.project_lock("alpha"):
        t = __import__("threading").Thread(target=attempt, daemon=True)
        t.start()
        t.join(5)
        busy = said[0] if said else "it waited on the lock"
        ran_while_held = list(ran)
    t.join(5)
    ran = ran_while_held
    check("project-test while that suite is running (a verify, a landing) is refused at once,"
          " not left waiting past the CLI's patience", "already running" in busy and not ran,
          busy)
    rec = OPS.run(OPS.plan("project-test", ["alpha"], w.ctx),
                  runner=lambda a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    with verify.project_lock("alpha", wait=False):
        released = True
    check("and the lock is let go after a run", rec["ok"] and released)
    rec = OPS.run(OPS.plan("tailscale-status", [], w.ctx),
                  runner=lambda a, **k: types.SimpleNamespace(
                      returncode=0, stdout="key nodekey:" + "0123456789abcdef", stderr=""))
    check("output is scrubbed of credentials", "0123456789abcdef" not in rec["tail"], rec["tail"])


def _no_secrets(w, *texts):
    """None of the fake .env's secrets in any of `texts`, the ops log or the
    calls log."""
    hay = "\n".join([*texts, w.log.read_text() if w.log.exists() else "",
                     w.calls.read_text()])
    return not any(x in hay for x in w.secrets)


def test_ops_trader_retire_and_env_set_run_their_exact_steps():
    import ops as OPS
    import stat as _stat
    print("\nnamed ops: trader-launchd-retire and trader-env-set run exactly their steps")
    w = _ops_world()
    job = "com.traderjoe.shadowoptions"
    src = w.agents / f"{job}.plist"
    p = OPS.plan("trader-launchd-retire", [job], w.ctx)
    retired = (w.trader / "data").resolve() / "retired-launchd"
    dst = p.steps[-1].argv[-1]
    check("retire: bootout (may fail), print (must fail, output not kept), mkdir, mv -n, test",
          [s.argv for s in p.steps] == [
              ["launchctl", "bootout", f"gui/501/{job}"],
              ["launchctl", "print", f"gui/501/{job}"],
              ["mkdir", "-p", str(retired)],
              ["mv", "-n", str(src), dst], ["test", "-f", dst]]
          and [(s.may_fail, s.must_fail, s.quiet) for s in p.steps]
          == [(True, False, False), (False, True, True), (False, False, False),
              (False, False, False), (False, False, False)]
          and _re.fullmatch(_re.escape(str(retired / job)) + r"\.\d{8}-\d{6}\.plist", dst)
          and p.cwd == str(w.trader) and not p.read_only, [s.argv for s in p.steps])
    check("and is named only by the op or the full label -- never a last part like "
          "'restart' or 'heartbeat', words said in passing",
          p.names == ("trader-launchd-retire", job)
          and OPS.plan("trader-launchd-retire", ["com.traderjoe.stocktwits"], w.ctx).names
          == ("trader-launchd-retire", "com.traderjoe.stocktwits"), p.names)
    p = OPS.plan("trader-env-set", ["MOMENTUM_UNIVERSE", "pit"], w.ctx)
    check("env-set: one step, the ops.py helper under -I, with the .env, a dated backup "
          "in data/env-backups, the key and the value",
          len(p.steps) == 1 and p.steps[0].argv[:4] == [
              sys.executable, "-I", str((BASE / "ops.py").resolve()), "env-set"]
          and p.steps[0].argv[4] == str(w.trader / ".env")
          and _re.fullmatch(_re.escape(str((w.trader / "data").resolve() / "env-backups"))
                           + r"/env\.\d{8}-\d{6}\.bak", p.steps[0].argv[5])
          and p.steps[0].argv[6:] == ["MOMENTUM_UNIVERSE", "pit"]
          and not p.read_only and p.names == ("trader-env-set", "MOMENTUM_UNIVERSE")
          and p.also == ("pit",),
          [s.argv for s in p.steps])
    with _fake_path(w):
        g = _ops_turn(w, "lets address all 5 of these: retire com.traderjoe.shadowoptions")
        r = _ops_as_user(w, g, "trader-launchd-retire", job, "--notes", "user: retire it")
        check("through the bot: launchctl bootout, print, then mkdir, mv, test, in order",
              [" ".join(c.split()[:2]) for c in _calls(w)]
              == ["launchctl bootout", "launchctl print", "mkdir -p", "mv -n",
                  f"test -f"] and r.returncode == 0, _calls(w) + [r.stdout, r.stderr])
        moved = sorted(retired.glob(f"{job}.*.plist"))
        check("the plist is gone from LaunchAgents and kept, byte for byte, in "
              "data/retired-launchd",
              not src.exists() and len(moved) == 1
              and plistlib_load(moved[0]).get("Label") == job, moved)
        check("the other jobs are untouched", (w.agents / "com.traderjoe.stocktwits.plist")
              .exists() and (w.agents / "com.silkworm.bot.plist").exists())
        check("the reply has each step and exit code",
              r.stdout.count("[exit") == 5 and "launchctl bootout gui/501/" + job in r.stdout,
              r.stdout)
    with _fake_path(w, FAKE_BOOTOUT="3"):
        w.calls.write_text("")
        g = _ops_turn(w, "retire com.traderjoe.stocktwits")
        r = _ops_as_user(w, g, "trader-launchd-retire", "com.traderjoe.stocktwits",
                         "--notes", "user: retire stocktwits")
        check("a job that was not loaded (bootout fails) is still retired",
              r.returncode == 0 and not (w.agents / "com.traderjoe.stocktwits.plist").exists(),
              r.stdout + r.stderr)
    import plistlib
    with open(w.agents / "com.traderjoe.stocktwits.plist", "wb") as f:
        plistlib.dump({"Label": "com.traderjoe.stocktwits"}, f)
    leak = "tok" + os.urandom(10).hex()
    with _fake_path(w, FAKE_BOOTOUT="5", FAKE_PRINT="0", FAKE_PRINT_LEAK=leak):
        w.calls.write_text("")
        g = _ops_turn(w, "retire com.traderjoe.stocktwits")
        r = _ops_as_user(w, g, "trader-launchd-retire", "com.traderjoe.stocktwits",
                         "--notes", "user: retire stocktwits")
        check("a job still loaded after bootout is not moved: a failed run, exit 1",
              r.returncode == 1 and (w.agents / "com.traderjoe.stocktwits.plist").exists()
              and not any(c.startswith(("mkdir", "mv")) for c in _calls(w)),
              _calls(w) + [r.stdout, r.stderr])
        check("and launchctl print's output (a job's environment) is not kept",
              leak not in r.stdout + r.stderr + w.log.read_text()
              and "(output not kept)" in r.stdout, r.stdout + r.stderr)
    env = w.trader / ".env"
    with _fake_path(w):
        w.calls.write_text("")
        g = _ops_turn(w, "switch the momentum universe to pit")
        r = _ops_as_user(w, g, "trader-env-set", "MOMENTUM_UNIVERSE", "pit",
                         "--notes", "user: MOMENTUM_UNIVERSE=pit")
        text = env.read_text()
        check("env-set adds a missing key as one line at the end, every other byte as it was",
              r.returncode == 0 and text == w.env_text + "MOMENTUM_UNIVERSE=pit\n",
              r.stdout + r.stderr)
        backups = sorted((w.trader / "data" / "env-backups").iterdir())
        check("after backing the old file up, mode 0600",
              len(backups) == 1 and backups[0].read_text() == w.env_text
              and _stat.S_IMODE(backups[0].stat().st_mode) == 0o600
              and _stat.S_IMODE(env.stat().st_mode) == 0o600, backups)
        check("it reports old -> new, and that it waits for the daemon's next restart",
              "MOMENTUM_UNIVERSE: (unset) -> pit" in r.stdout
              and "next restart (the weekly Sunday restart)" in r.stdout
              and "not restarted" in r.stdout, r.stdout)
        check("restarting nothing: no launchctl, no other program",
              _calls(w) == [], _calls(w))
        g = _ops_turn(w, "actually set trader-env-set MOMENTUM_UNIVERSE back to static")
        r = _ops_as_user(w, g, "trader-env-set", "MOMENTUM_UNIVERSE", "static",
                         "--notes", "user: back to static")
        check("a key already there is changed in place: pit -> static, still one line",
              r.returncode == 0 and "MOMENTUM_UNIVERSE: pit -> static" in r.stdout
              and env.read_text() == w.env_text + "MOMENTUM_UNIVERSE=static\n",
              r.stdout + r.stderr)
        r = _ops_as_user(w, g, "trader-env-set", "MOMENTUM_UNIVERSE", "static",
                         "--notes", "user: back to static")
        check("setting the value it has changes and backs up nothing",
              r.returncode == 0 and "already static" in r.stdout
              and len(list((w.trader / "data" / "env-backups").iterdir())) == 2, r.stdout)
        g = _ops_turn(w, "set MOMENTUM_UNIVERSE to pit")
        env.write_bytes(w.env_text.replace("ALERT_CHANNEL=C123\n",
                                           "export MOMENTUM_UNIVERSE=\"static\"  # was\r\n"
                                           "ALERT_CHANNEL=C123\n").encode())
        r = _ops_as_user(w, g, "trader-env-set", "MOMENTUM_UNIVERSE", "pit",
                         "--notes", "user: pit")
        check("an `export KEY=\"value\"  # comment` line keeps its export and line ending",
              r.returncode == 0 and "static -> pit" in r.stdout
              and env.read_bytes() == w.env_text.replace(
                  "ALERT_CHANNEL=C123\n", "export MOMENTUM_UNIVERSE=pit\r\nALERT_CHANNEL=C123\n"
              ).encode(), repr(env.read_bytes()))
        twice = w.env_text + "MOMENTUM_UNIVERSE=static\nMOMENTUM_UNIVERSE=pit\n"
        env.write_text(twice)
        r = _ops_as_user(w, g, "trader-env-set", "MOMENTUM_UNIVERSE", "pit",
                         "--notes", "user: pit")
        check("a key set on two lines is refused, the file untouched",
              r.returncode == 1 and "2 lines" in r.stdout + r.stderr
              and env.read_text() == twice, r.stdout + r.stderr)
        for name, odd in (("a form feed", "\x0c"), ("a lone carriage return", "\r"),
                          ("a line separator", "\u2028")):
            body = (w.env_text + f"MOMENTUM_UNIVERSE=static{odd}SECRET_TWO=" + w.secrets[0]
                    + "\n").encode()
            env.write_bytes(body)
            r = _ops_as_user(w, g, "trader-env-set", "MOMENTUM_UNIVERSE", "pit",
                             "--notes", "user: pit")
            check(f"a .env with {name} inside a line is refused, not joined to the next "
                  "line (a secret's)", r.returncode == 1 and "line breaks" in r.stdout + r.stderr
                  and env.read_bytes() == body, r.stdout + r.stderr)
        body = ("\ufeff" + "MOMENTUM_UNIVERSE=static\n" + w.env_text).encode()
        env.write_bytes(body)
        r = _ops_as_user(w, g, "trader-env-set", "MOMENTUM_UNIVERSE", "pit",
                         "--notes", "user: pit")
        check("so is one starting with a BOM (its first key would be missed, then added "
              "twice)", r.returncode == 1 and env.read_bytes() == body, r.stdout + r.stderr)
        check("no secret of the .env in any reply, the ops log or the calls",
              _no_secrets(w, r.stdout, r.stderr), "")
    runs = OPS.read_log(w.log, 50)
    check("the ops log has the runs, each step's argv and exit code",
          any(x.get("op") == "trader-env-set" and x.get("ok") for x in runs)
          and any(x.get("op") == "trader-launchd-retire" and x.get("ok") for x in runs))


def plistlib_load(path):
    import plistlib
    with open(path, "rb") as f:
        return plistlib.load(f)


def test_ops_trader_retire_and_env_set_refuse_what_they_do_not_take():
    import ops as OPS
    print("\nnamed ops: retire and env-set refuse unknown jobs, keys, values, shell, and "
          "anyone's word but yours")
    w = _ops_world()

    def refused(name, args):
        try:
            OPS.plan(name, args, w.ctx)
        except OPS.Refused as e:
            return str(e)
        except Exception as e:          # fails the check, not the whole test
            return f"raised {e!r}"
        return ""

    R = "trader-launchd-retire"
    check("a job that is not trader's", "not one of trader's jobs" in refused(
        R, ["com.silkworm.bot"]))
    check("one of trader's that is not installed, naming what is",
          "no installed com.traderjoe.nope" in refused(R, ["com.traderjoe.nope"])
          and "com.traderjoe.shadowoptions" in refused(R, ["com.traderjoe.nope"]))
    check("a label that walks out of LaunchAgents",
          "not a launchd job label" in refused(R, ["../com.traderjoe.x"])
          and "not a launchd job label" in refused(R, ["com.traderjoe..x"]))
    check("exactly one job", refused(R, []) and refused(
        R, ["com.traderjoe.shadowoptions", "com.traderjoe.stocktwits"]))
    (w.agents / "com.traderjoe.link.plist").symlink_to(w.agents / "com.traderjoe.stocktwits.plist")
    check("an installed plist that is a symlink",
          "no installed com.traderjoe.link" in refused(R, ["com.traderjoe.link"]))
    import plistlib
    with open(w.agents / "com.traderjoe.mislabel.plist", "wb") as f:
        plistlib.dump({"Label": "com.traderjoe.other"}, f)
    check("an installed plist whose Label is another job's",
          "Label is 'com.traderjoe.other'" in refused(R, ["com.traderjoe.mislabel"]))
    (w.agents / "com.traderjoe.junk.plist").write_bytes(b"not a plist")
    check("or one that does not parse", "does not parse" in refused(R, ["com.traderjoe.junk"]))
    (w.trader / "data" / "retired-launchd").symlink_to(w.tmp)
    check("a retired-launchd that is a symlink",
          "symlink" in refused(R, ["com.traderjoe.shadowoptions"]))
    (w.trader / "data" / "retired-launchd").unlink()
    E = "trader-env-set"
    for key in ("BROKER_KEY", "IB_PASSWORD", "momentum_universe", "PATH"):
        check(f"a key not on the list ({key})", "not a key" in refused(E, [key, "pit"]))
    for value in ("PIT", "dynamic", "", "static,pit", "pit=1"):
        why = refused(E, ["MOMENTUM_UNIVERSE", value])
        check(f"a value MOMENTUM_UNIVERSE does not take ({value!r})",
              "not a value" in why or "empty" in why, why)
    for bad in ("pit;id", "$(id)", "pit x", "`id`", "pit\n", "pit|x"):
        check(f"shell in a value ({bad!r})", "never shell" in refused(
            E, ["MOMENTUM_UNIVERSE", bad]))
        check(f"or in a label ({bad!r})", "never shell" in refused(R, [bad]))
    check("one key and one value", refused(E, ["MOMENTUM_UNIVERSE"])
          and refused(E, ["MOMENTUM_UNIVERSE", "pit", "x"]))
    check("the helper itself refuses a key or value off the list",
          "not allowed" in _refusal_of(lambda: OPS.env_set(w.trader / ".env", w.tmp / "b",
                                                       "BROKER_KEY", "x"))
          and "not allowed" in _refusal_of(lambda: OPS.env_set(
              w.trader / ".env", w.tmp / "b", "MOMENTUM_UNIVERSE", "dynamic"))
          and (w.trader / ".env").read_text() == w.env_text)
    (w.trader / "data" / "env-backups").symlink_to(w.tmp)
    check("an env-backups that is a symlink", "symlink" in refused(
        E, ["MOMENTUM_UNIVERSE", "pit"]))
    (w.trader / "data" / "env-backups").unlink()
    real = w.trader / ".env"
    moved = w.tmp / "real.env"
    real.rename(moved)
    real.symlink_to(moved)
    check("a .env that is a symlink", "not a plain file" in refused(
        E, ["MOMENTUM_UNIVERSE", "pit"]))
    r = subprocess.run([sys.executable, "-I", str(BASE / "ops.py"), "env-set", str(real),
                        str(w.tmp / "b2"), "MOMENTUM_UNIVERSE", "pit"],
                       capture_output=True, text=True)
    check("and the helper refuses it too, writing nothing",
          r.returncode == 1 and moved.read_text() == w.env_text
          and not (w.tmp / "b2").exists(), r.stderr)
    real.unlink()
    moved.rename(real)
    with _fake_path(w):
        w.calls.write_text("")
        for argv in (["trader-launchd-retire", "com.traderjoe.shadowoptions"],
                     ["trader-env-set", "MOMENTUM_UNIVERSE", "pit"]):
            r = _ops_as_sweep(w, *argv, "--notes", "x")
            check(f"a sweep's {argv[0]} is refused", r.returncode == 1
                  and "never in a sweep" in r.stderr, r.stderr)
            out = _ops_ask(w, {"action": "run", "name": argv[0], "args": argv[1:],
                               "caller_role": "operator", "caller_thread": "D1:1.0",
                               "caller_task": "tsk_sweep", "notes": "x"})
            check(f"and by the bot itself ({argv[0]})",
                  not out["ok"] and out.get("refused") == "grant", out)
            g = _ops_turn(w, "do the five things from this morning")
            r = _ops_as_user(w, g, *argv, "--notes", "user: do them")
            check(f"your turn, but a message that never names {argv[0]}, its job or key",
                  r.returncode == 1 and "trader's live configuration" in r.stderr
                  and "has to name it" in r.stderr, r.stderr)
            g = _ops_turn(w, f"run {argv[0]} please")
            r = _ops_as_user(w, g, *argv)
            check(f"named, but without --notes ({argv[0]})",
                  r.returncode == 1 and "--notes" in r.stderr, r.stderr)
        g = _ops_turn(w, "retire the stocktwits job")
        r = _ops_as_user(w, g, "trader-launchd-retire", "com.traderjoe.shadowoptions",
                         "--notes", "user: retire stocktwits")
        check("naming another job does not name this one", r.returncode == 1
              and "has to name it" in r.stderr, r.stderr)
        for text in ("retire shadowoptions", "restart the heartbeat and shadowoptions jobs"):
            g = _ops_turn(w, text)
            r = _ops_as_user(w, g, "trader-launchd-retire", "com.traderjoe.shadowoptions",
                             "--notes", "user: retire")
            check(f"a job's last part alone does not name a retire ({text!r})",
                  r.returncode == 1 and "has to name it" in r.stderr, r.stderr)
        for text in ("what is the momentum universe on now?",
                     "set MOMENTUM_UNIVERSE to static"):
            g = _ops_turn(w, text)
            r = _ops_as_user(w, g, "trader-env-set", "MOMENTUM_UNIVERSE", "pit",
                             "--notes", "user: pit")
            check(f"the key without the value set does not name it ({text!r})",
                  r.returncode == 1 and "say the value too" in r.stderr, r.stderr)
        g = _ops_turn(w, "set the universe to pit")
        r = _ops_as_user(w, g, "trader-env-set", "MOMENTUM_UNIVERSE", "pit",
                         "--notes", "user: pit")
        check("'universe' alone does not name MOMENTUM_UNIVERSE", r.returncode == 1
              and "has to name it" in r.stderr, r.stderr)
        out = _ops_ask(w, {"action": "run", "name": "trader-env-set",
                           "args": ["MOMENTUM_UNIVERSE", "pit"], "caller_role": "assistant",
                           "caller_thread": "C9:9.0", "caller_task": "tsk_x", "notes": "x"})
        check("nor does any other turn's (an assistant's)", not out["ok"], out)
        check("none of it ran anything or touched a file",
              not _calls(w) and (w.trader / ".env").read_text() == w.env_text
              and (w.agents / "com.traderjoe.shadowoptions.plist").exists()
              and not (w.trader / "data" / "env-backups").exists(), _calls(w))
        r = _ops_cli(w, "run", "trader-env-set", "MOMENTUM_UNIVERSE", "pit", role="",
                     thread="")
        check("your own terminal (no turn) runs it", r.returncode == 0
              and "(unset) -> pit" in r.stdout, r.stdout + r.stderr)
        check("no secret in any of it", _no_secrets(w, r.stdout, r.stderr))


def _refusal_of(fn) -> str:
    try:
        fn()
    except Exception as e:
        return str(e)
    return ""


def test_ops_on_the_operators_allowlist_and_in_its_prompts():
    import ops as OPS
    import operator_role as O
    import roles
    print("\nnamed ops: on the operator's allowlist by exact prefix, and in both its prompts")
    args = roles.permission_args(O.ROLE, ["--dangerously-skip-permissions"], bin="/abs/silkworm")
    tools = args[args.index("--allowedTools") + 1].split(") ")
    tools = [t if t.endswith(")") else t + ")" for t in tools]
    check("`ops run`, `ops list` and `ops log` at the CLI's absolute path",
          {"Bash(/abs/silkworm ops run:*)", "Bash(/abs/silkworm ops list:*)",
           "Bash(/abs/silkworm ops log:*)"} <= set(tools), tools)
    check("and no wider ops entry, raw tailscale, launchctl or shell",
          not any(t in tools for t in ("Bash(/abs/silkworm ops:*)", "Bash")) and not any(
              ("tailscale" in t or "launchctl" in t or "plutil" in t or "python" in t)
              for t in tools), tools)
    text = roles.system_prompt(O.ROLE, bin="/s")
    check("its prompt lists every op", all(f"`{n}" in text for n in OPS.REGISTRY))
    check("and says to run them rather than hand the user a shell step",
          "Commands on this machine: never hand the user a shell step you have an op for"
          in text
          and "/s ops run <name>" in text)
    check("only on the user's word for one that changes things, never a sweep",
          "never\nin a sweep" not in text and "never in a sweep or a wake-up" in text
          and "only when their message named it" in text)
    goal = O.SWEEP_GOAL.format(bin="/s")
    check("the sweep may read, and asks before changing anything",
          "/s ops run tailscale-status" in goal and "is never run in a sweep" in goal
          and all(n in goal for n, op in OPS.REGISTRY.items() if op.read_only is False)
          and "record the ones you propose as a plan (`/s ops plan" in goal
          and 'a plain "yes" in this thread runs them' in goal
          and "never ask the user to run a command themselves" in goal, goal)
    check("`ops plan` is on its allowlist, and its prompt says to record and number a plan",
          "Bash(/abs/silkworm ops plan:*)" in tools
          and "/s ops plan <op> [args] [--op <op> [args]]… --by operator" in text
          and "--plan-entry N" in text and 'plain "yes"' in text, text)
    check("and to file what the user asked for with --requested-by-user, not propose+accept",
          "/s task --requested-by-user --notes" in text
          and "file it with `task --propose`, then `accept" not in text, text)


# --- your word in plain words: a numbered plan of ops, and filing past the cap ------
# 2026-11-15: "go with your recommendations, and run the trader ops" was refused
# for all four of trader's ops -- the message named none of them -- and work you
# had explicitly asked for was refused by MAX_OPEN_PROPOSALS. "I don't like
# having to send these exact commands."

def test_plan_approval_reads_plain_words():
    import operator_role as O
    print("\nyour answer to a plan: plain words, read by rules, failing closed")
    cases = {
        "yes": {1, 2, 3}, "Yes!": {1, 2, 3}, "go": {1, 2, 3}, "do it": {1, 2, 3},
        "run them": {1, 2, 3}, "ok": {1, 2, 3}, "go ahead": {1, 2, 3}, "lgtm": {1, 2, 3},
        ":+1:": {1, 2, 3}, "sure, run them all": {1, 2, 3},
        "can you run them?": {1, 2, 3}, "sounds good": {1, 2, 3},
        "do 1 and 3": {1, 3}, "do 2": {2}, "yes, run 2": {2}, "2": {2}, "1 and 3": {1, 3},
        "1-2": {1, 2}, "yes 1, 3": {1, 3}, "go ahead with 2": {2}, "just 2": {2},
        "yes to 1": {1}, "all but 2": {1, 3}, "yes except 1 and 3": {2},
        "yes but not 2": {1, 3}, "do it, all but #3": {1, 2},
    }
    words = O.plan_words([{"name": "trader-env-set", "args": ["MOMENTUM_UNIVERSE", "pit"]},
                          {"name": "tailscale-on", "args": []}])
    # The words of the plan's own ops may name what is approved -- the very
    # message refused on 2026-11-15.
    cases.update({"go with your recommendations, and run the trader ops": {1, 2, 3},
                  "yes, set the momentum universe to pit": {1, 2, 3}})
    for text, want in cases.items():
        got, why = O.plan_approval(text, 3, words)
        check(f"{text!r} approves {sorted(want)}", set(got) == want and not why, (got, why))
    check("without the plan's words, 'trader ops' is more than a yes",
          not O.plan_approval("go with your recommendations, and run the trader ops", 3)[0])
    for text in ("what does 2 do?", "which one is 2", "is 2 safe", "no", "not yet",
                 "wait", "hold off on 2", "don't", "do it later", "thanks", "",
                 "do 5", "all but 4", "yes, 0", "do 3-9", "all but 1, 2 and 3",
                 "yes? what is 2", "hmm, 2", "what if we run 2", "should I do 2",
                 # Saying more than yes is not a yes (found in review).
                 "all except the trader ones", "all but the last one", "do the first two",
                 "do it in 2 hours", "go for it tomorrow", "run them once I am back",
                 "sure, but first explain 2", "ok so 3 is the risky one",
                 "yes, file a task for the login bug", "ok thanks", "approve 1, 2 is wrong",
                 "do 1. 2 seems wrong", "the 3 failing checks are fine, yes run them",
                 "skip 2", "all", "the plan", "run them?", "do 2?", "yes?",
                 "nope", "never", "do not run them", "no 2", "do it, then wait"):
        got, why = O.plan_approval(text, 3, words)
        check(f"{text!r} approves nothing, and says why", not got and why, (got, why))
    check("no plan, nothing approved", not O.plan_approval("yes", 0)[0])
    live = {"id": "plan_a", "answered": "1.0", "entries": [
        {"n": 1, "name": "tailscale-on", "args": [], "used": 1.0},
        {"n": 2, "name": "trader-env-set", "args": ["MOMENTUM_UNIVERSE", "pit"], "used": None}]}
    grant = {"text": "yes", "ts": "1.0", "plan": {"id": "plan_a", "entries": live["entries"]}}
    check("plan_refusal: an entry that ran is refused on its own",
          "already run" in O.plan_refusal(grant, 1, "tailscale-on", [], live))
    check("plan_refusal: the other runs, exactly as planned",
          O.plan_refusal(grant, 2, "trader-env-set", ["MOMENTUM_UNIVERSE", "pit"], live) == "")
    check("plan_refusal: not with another value",
          "only exactly" in O.plan_refusal(grant, 2, "trader-env-set",
                                           ["MOMENTUM_UNIVERSE", "static"], live))
    check("plan_refusal: a grant from another plan is refused",
          "replaced" in O.plan_refusal(dict(grant, plan={"id": "plan_b"}), 2, "trader-env-set",
                                       ["MOMENTUM_UNIVERSE", "pit"], live))
    check("plan_refusal: a bad entry number is refused, not raised",
          "entry number" in O.plan_refusal(grant, "x", "tailscale-on", [], live))
    check("plan_refusal: a plan another message answered is refused",
          "answered by another" in O.plan_refusal(
              dict(grant, ts="2.0"), 2, "trader-env-set", ["MOMENTUM_UNIVERSE", "pit"],
              live))


def test_plan_approval_reads_phone_typing():
    import operator_role as O
    print("\nyour answer to a plan as a phone types it: curly quotes, ellipses, a lead-in")
    # The plan answered on 2026-11-16 (plan_7ca85c43df) was trader's ops.
    words = O.plan_words([
        {"name": "trader-launchd-retire", "args": ["com.trader.momentum"]},
        {"name": "trader-env-set", "args": ["MOMENTUM_UNIVERSE", "pit"]},
        {"name": "trader-launchd-reinstall", "args": []},
        {"name": "trader-refetch-revisions", "args": ["--apply"]},
        {"name": "tailscale-on", "args": []}])
    yes5 = {1, 2, 3, 4, 5}
    cases = {
        # The exact message refused on 2026-11-16.
        "for trader\u2026 \u201cyes\u201d": yes5,
        "\u201cyes\u201d": yes5, "\u2018yes\u2019": yes5, '"yes"': yes5, "Yes.": yes5,
        "YES!!": yes5, "yes \U0001f44d": yes5, "yes \U0001f44d\U0001f3fd": yes5,
        "\U0001f44d": yes5, "\u2705": yes5, "yes \u2714\ufe0f": yes5, "yes \U0001f64f": yes5,
        "  yes\u00a0 ": yes5, "yes;": yes5, "yes\u2026": yes5, "go ahead \u2014 run them": yes5,
        "for trader: do 1 and 3": {1, 3}, "trader: do 1 and 3": {1, 3},
        "re the ops: yes": yes5, "re the ops, yes": yes5, "about the trader ops \u2014 yes": yes5,
        "for the plan: all but 2": {1, 3, 4, 5}, "for trader\u2026 do 1\u20133": {1, 2, 3},
        "all but 5\u2026": {1, 2, 3, 4}, "all but 5.": {1, 2, 3, 4},
        "yes but not 4": {1, 2, 3, 5}, "Yes \u2014 but not 4": {1, 2, 3, 5},
        "do 1 \u2013 3": {1, 2, 3}, "yes \u2014 except 2": {1, 3, 4, 5},
        "yes :pray:": yes5, "yes 2\ufe0f\u20e3": {2}, "yes: 2": {2}, "2: yes": {2},
    }
    for text, want in cases.items():
        got, why = O.plan_approval(text, 5, words)
        check(f"{text!r} approves {sorted(want)}", set(got) == want and not why, (got, why))
    refusals = (
        "yes?", "\u201cyes?\u201d", "yes\uff1f", "yes \u2753", "\U0001f914", "what does 2 do",
        "what does 2 do\u2026", "yes but not 4 yet", "yes but 4 later", "yes if 2 passes",
        "no", "No.", "nope", "not yet", "Not yet\u2026", "don't", "don\u2019t",
        "don\u2019t run 2", "\U0001f44e", "\u274c", "yes, but wait", "hold off \u2014 not yet",
        "do 7", "yes to 6", "all but 9", "for trader: do 6",
        # A lead-in naming something that is not this plan is not a lead-in.
        "for cadence: yes", "re mondays: yes", "re 2: yes", "for the other plan, yes but",
        "about tomorrow: yes", "trader? yes",
        # Numbers joined by dots are a range, a list or a decimal: not guessed.
        "do 1.2", "1\u20263", "do 1. 2 seems wrong",
        "it\u2019s fine, run them", "\u201cyes\u201d she said no",
        # A dash before a number may mean "minus": not read as a list (found
        # in review -- "yes all - 2" had approved only 2).
        "yes all - 2", "yes, all \u2014 2", "everything \u2013 2",
        # ...nor stripped through as a lead-in's end (found in review: these
        # had approved only 2).
        "the plan - 2", "ops - 2", "trader ops - 2", "for trader - 2",
        "the plan \u2014 2", "re the ops \u2013 2", "trader -- 2",
        # Slack shortcodes that say no or ask, and ones we cannot read.
        "yes :x:", "yes :-1:", "yes :thumbsdown:", "yes :no_entry:", "yes :raised_hand:",
        "yes :question:", "yes :thinking_face:", "yes :eyes:", "yes :skull:",
    )
    for text in refusals:
        got, why = O.plan_approval(text, 5, words)
        check(f"{text!r} approves nothing, and says why", not got and why, (got, why))
    check("a negation is named as one",
          "says no" in O.plan_approval("not yet", 5, words)[1]
          and "says no" in O.plan_approval("don\u2019t", 5, words)[1])
    # The normaliser alone, pinned so each rule is seen to do its part.
    for raw, want in {
            "for trader\u2026 \u201cyes\u201d": "for trader... yes",
            "\u2018yes\u2019": "yes", "don\u2019t": "don't", "Yes.": "yes", "yes!!!": "yes",
            "yes \U0001f44d\U0001f3fb": "yes yes", "ok \u2705": "ok yes", "do 1\u20133": "do 1-3",
            "do 1 \u2014 3": "do 1-3", "go \u2014 run": "go , run", "yes - run": "yes , run",
            "  YES \u00a0 ": "yes", "yes\u2026": "yes", "1\u20263": "1...3",
            "yes \U0001f64f": "yes", "\u274c": "no", "yes\u200b": "yes",
            "is that ok?": "is that ok?"}.items():
        got = O.normalise_answer(raw)
        check(f"normalise_answer({raw!r}) == {want!r}", got == want, got)
    for raw, want in {
            "for trader: yes": "yes", "re the ops, do 2": "do 2", "trader: yes": "yes",
            "for the trader ops... yes": "yes", "about the plan , yes": "yes",
            "trader ops yes": "trader ops yes", "for cadence: yes": "for cadence: yes",
            "re 2: yes": "re 2: yes", "for trader:": "for trader:",
            "the plan - 2": "the plan - 2", "ops - 2": "ops - 2",
            "re the ops": "re the ops"}.items():
        got = O.strip_lead_in(raw, words)
        check(f"strip_lead_in({raw!r}) == {want!r}", got == want, got)
    check("Slack's no and question shortcodes are read as such, not as unknown words",
          "says no" in O.plan_approval("yes :x:", 5, words)[1]
          and "says no" in O.plan_approval("yes :thumbsdown:", 5, words)[1]
          and "question" in O.plan_approval("yes :thinking_face:", 5, words)[1])
    check("a refusal word in an op's name is never a lead-in",
          not O.plan_approval("hold: yes", 2, O.plan_words([{"name": "trader-hold"}]))[0])
    check("with no plan words, only the plan itself is a subject",
          O.strip_lead_in("re the ops: yes", ()) == "yes"
          and O.strip_lead_in("for trader: yes", ()) == "for trader: yes")


def test_ops_plan_runs_on_a_plain_yes():
    import ops as OPS
    import operator_role as O
    print("\nnamed ops: a plan the operator showed you runs on 'yes' or 'do 2', exactly as shown")
    w = _ops_world()
    PLAN = ("trader-env-set", "MOMENTUM_UNIVERSE", "pit", "--op", "tailscale-on",
            "--op", "trader-launchd-retire", "com.traderjoe.shadowoptions")
    with _fake_path(w):
        r = _plan_record(w, *PLAN)
        check("a sweep records a plan, numbered as the reply shows it",
              r.returncode == 0 and "1. `trader-env-set MOMENTUM_UNIVERSE pit`" in r.stdout
              and "2. `tailscale-on`" in r.stdout
              and "3. `trader-launchd-retire com.traderjoe.shadowoptions`" in r.stdout
              and "plain 'yes'" in r.stdout, r.stdout + r.stderr)
        r = _ops_cli(w, "plan", task="tsk_sweep")
        check("and `ops plan` alone shows the pending one",
              r.returncode == 0 and "pending plan plan_" in r.stdout and "2. `tailscale-on`"
              in r.stdout, r.stdout + r.stderr)
        r = _plan_run(w, "", 1, "trader-env-set", "MOMENTUM_UNIVERSE", "pit")
        check("the sweep itself cannot run an entry (no grant)",
              r.returncode == 1 and "never in a sweep" in r.stderr
              and _env_value(w, "MOMENTUM_UNIVERSE") is None, r.stderr)
        out = _ops_ask(w, {"action": "run", "name": "trader-env-set",
                           "args": ["MOMENTUM_UNIVERSE", "pit"], "plan_entry": 1,
                           "caller_role": "operator", "caller_thread": "D1:1.0",
                           "caller_task": "tsk_sweep", "notes": "x"})
        check("nor through the bot", not out["ok"], out)

        g = _plan_turn(w, "yes")
        r = _plan_run(w, g, 1, "trader-env-set", "MOMENTUM_UNIVERSE", "pit")
        check("'yes' runs entry 1 -- trader's .env, though the message named no op",
              r.returncode == 0 and _env_value(w, "MOMENTUM_UNIVERSE") == "pit",
              r.stdout + r.stderr)
        w.calls.write_text("")
        r = _plan_run(w, g, 2, "tailscale-on")
        check("and entry 2 (its own checks fail against the fakes; it ran)", any(
            c.startswith("tailscale serve --bg") for c in _calls(w)), r.stdout + r.stderr)
        w.calls.write_text("")
        r = _plan_run(w, g, 2, "tailscale-on")
        check("an entry runs once", r.returncode == 1 and "already run" in r.stderr
              and not _calls(w), r.stderr)
        r = _plan_run(w, g, 3, "trader-launchd-retire", "com.traderjoe.stocktwits")
        check("args can't differ from the plan's",
              r.returncode == 1 and "only exactly what the user was shown" in r.stderr
              and (w.agents / "com.traderjoe.stocktwits.plist").exists(), r.stderr)
        r = _plan_run(w, g, 3, "trader-launchd-reinstall", "com.traderjoe.pooldata")
        check("nor the op", r.returncode == 1 and "not `trader-launchd-reinstall" in r.stderr,
              r.stderr)
        r = _ops_as_user(w, g, "trader-launchd-reinstall", "com.traderjoe.pooldata",
                         "--notes", "user: yes")
        check("an op not in the plan still has to be named",
              r.returncode == 1 and "has to name it" in r.stderr, r.stderr)
        r = _plan_run(w, g, 7, "tailscale-on")
        check("and there is no entry 7", r.returncode == 1 and "no entry 7" in r.stderr, r.stderr)
        r = _plan_run(w, g, 3, "trader-launchd-retire", "com.traderjoe.shadowoptions",
                      notes="")
        check("--notes still needed", r.returncode != 0, r.stderr)
        r = _plan_run(w, g, 3, "trader-launchd-retire", "com.traderjoe.shadowoptions")
        check("entry 3, exactly as planned, runs",
              r.returncode == 0 and not (w.agents / "com.traderjoe.shadowoptions.plist").exists(),
              r.stdout + r.stderr)
        runs = [x for x in OPS.read_log(w.log, 100) if x.get("plan_entry") and not x.get("refused")]
        check("each run records its plan id, entry and your message's ts",
              len(runs) == 3 and all(x.get("plan_id", "").startswith("plan_")
                                     and x.get("decided_ts") and x.get("decided_by") == "U_ME"
                                     for x in runs)
              and [x["plan_entry"] for x in runs] == [1, 2, 3], runs)
        check("a plan whose every entry ran is no longer pending", w.state.plan() is None)

        # 'do 2': that one only.
        (w.trader / ".env").write_text(w.env_text)
        _plan_record(w, "trader-env-set", "MOMENTUM_UNIVERSE", "pit", "--op",
                     "trader-env-set", "MOMENTUM_UNIVERSE", "static")
        g = _plan_turn(w, "do 2")
        r = _plan_run(w, g, 1, "trader-env-set", "MOMENTUM_UNIVERSE", "pit")
        check("'do 2' does not run entry 1",
              r.returncode == 1 and "does not approve entry 1" in r.stderr
              and _env_value(w, "MOMENTUM_UNIVERSE") is None, r.stderr)
        r = _plan_run(w, g, 2, "trader-env-set", "MOMENTUM_UNIVERSE", "static")
        check("and runs entry 2", r.returncode == 0
              and _env_value(w, "MOMENTUM_UNIVERSE") == "static", r.stdout + r.stderr)

        # Not an approval.
        _plan_record(w, "tailscale-on")
        w.calls.write_text("")
        g = _plan_turn(w, "what does 1 do?")
        r = _plan_run(w, g, 1, "tailscale-on")
        check("a question approves nothing: it is no answer to the plan",
              r.returncode == 1 and "no answer to it" in r.stderr and not _calls(w), r.stderr)
        g = _plan_turn(w, "ok, yes")
        r = _plan_run(w, g, 1, "tailscale-on")
        check("a question is no answer, so the plan waits: the 'yes' after it runs it "
              "(its own checks fail against the fakes; it ran)", any(c.startswith("tailscale serve --bg")
                                        for c in _calls(w)), r.stdout + r.stderr)
        _plan_record(w, "tailscale-on", "--op", "tailscale-on")
        w.calls.write_text("")
        g = _plan_turn(w, "do 1")
        g = _plan_turn(w, "ok, yes")
        r = _plan_run(w, g, 2, "tailscale-on")
        check("only the first message that answers a plan spends it: a later 'yes' is "
              "no answer", r.returncode == 1 and "no plan was waiting" in r.stderr
              and not _calls(w), r.stderr)
        _plan_record(w, "tailscale-on")
        g = _plan_turn(w, "no, hold off")
        r = _plan_run(w, g, 1, "tailscale-on")
        check("nor does a no", r.returncode == 1 and not _calls(w), r.stderr)
        _plan_record(w, "tailscale-on")
        ts_ = f"{time.time() + 1:.6f}"
        first = w.state.answer_plan(ts_, "yes")
        check("the answering message's own turn, asked again (a retry), still gets it",
              first and (w.state.answer_plan(ts_, "yes") or {}).get("id") == first["id"]
              and w.state.answer_plan(f"{time.time() + 2:.6f}", "yes") is None)

        # A message sent before the plan was recorded answers none.
        _plan_record(w, "tailscale-on")
        g = _plan_turn(w, "yes", ts=f"{time.time() - 60:.6f}")
        r = _plan_run(w, g, 1, "tailscale-on")
        check("a message sent before the plan existed is no answer to it",
              r.returncode == 1 and "no plan was waiting" in r.stderr
              and not _calls(w), r.stderr)

        # Replaced, then expired.
        _plan_record(w, "tailscale-on")
        g = _plan_turn(w, "yes")
        _plan_record(w, "tailscale-on")
        r = _plan_run(w, g, 1, "tailscale-on")
        check("a plan replaced after you answered runs nothing",
              r.returncode == 1 and "expired or been replaced" in r.stderr
              and not _calls(w), r.stderr)
        g = _plan_turn(w, "yes")
        data = json.loads(w.statefile.read_text())
        data["plan"]["at"] -= O.PLAN_TTL_S + 5
        w.statefile.write_text(json.dumps(data))
        r = _plan_run(w, g, 1, "tailscale-on")
        check("an expired plan runs nothing",
              r.returncode == 1 and "expired or been replaced" in r.stderr
              and not _calls(w), r.stderr)
        g = _plan_turn(w, "yes")
        check("and a message after it expired carries no plan", (w.grants.get(
            g, "D1:1.0", "tsk_turn_mine") or {}).get("plan") is None)
        check("plan_before reads a day-old plan as gone",
              w.state.plan_before(f"{time.time() + 1:.6f}") is None)

        # Who may record one, and what.
        r = _ops_cli(w, "plan", "tailscale-on", "--by", "x", role="assistant",
                     thread="C9:9.0", task="tsk_x")
        check("only an operator turn in its home thread records a plan",
              r.returncode == 1 and "home thread" in r.stderr, r.stderr)
        r = _ops_cli(w, "plan", "tailscale-on", "--by", "operator", thread="C9:9.0",
                     task="tsk_x")
        check("not even an operator turn in another thread",
              r.returncode == 1 and "home thread" in r.stderr, r.stderr)
        r = _plan_record(w, "tailscale-status")
        check("an op that only reads is not planned", r.returncode == 1
              and "only reads" in r.stderr, r.stderr)
        r = _plan_record(w, "tailscale-on", "--op", "trader-env-set", "MOMENTUM_UNIVERSE", "x")
        check("each entry is checked as a run would be", r.returncode == 1
              and "entry 2" in r.stderr, r.stderr)
        r = _plan_record(w, "no-such-op")
        check("an unknown op is refused", r.returncode == 1 and "no op" in r.stderr, r.stderr)
        r = _plan_record(w, "tailscale-on", "--op", "trader-launchd-reinstall", "a;b")
        check("and shell is refused before the bot is asked", r.returncode == 1, r.stderr)
        out = w.ns["record_ops_plan"]({"entries": [{"name": "tailscale-on", "args": []}] * 11,
                                       "caller_role": "operator", "caller_thread": "D1:1.0"})
        check(f"at most {O.MAX_PLAN} ops in a plan", not out["ok"], out)


def test_plan_answer_reads_answers_only():
    import operator_role as O
    print("\nplans: only a message that answers the plan (yes, entries, or no) spends it")
    E = [{"name": "trader-env-set", "args": ["MOMENTUM_UNIVERSE", "pit"]},
         {"name": "tailscale-on", "args": []},
         {"name": "trader-launchd-retire", "args": ["com.traderjoe.stocktwits"]}]
    for t in ("yes", "go", "do 1 and 3", "all but 2", "2", "for trader… “yes”", "👍"):
        check(f"{t!r} approves: an answer", O.plan_answer(t, E) == "approve")
    for t in ("no", "No.", "nope", "not now", "not yet", "skip it", "cancel the plan",
              "no thanks", "don't run them", "do not do it", "hold off", "nah, not today",
              "👎", ":x:", "for trader: no", "never mind", "forget it", "cancel the trader ops",
              "re the ops: no", "regarding trader: skip it",
              # found in review: each had left the plan runnable by a later "ok"
              "do not", "No. Do not.", "absolutely not", "okay no", "ok, cancel", "no go",
              "no way", "hell no", "nope, not doing that", "no, leave it", "let's not",
              "don't bother", "not interested", "dont run anything",
              "not yet, ask me tomorrow", "nah I'll do it myself", "no, don't run 3",
              "do NOT run 1", "wait", "wait, what does 3 do?", "no?", "not 3", "skip 2",
              "no, do it", "later today we'll see", "stop the trader daemon",
              "yes but not 4 yet", "won't need those"):
        check(f"{t!r} says no: it spends the plan", O.plan_answer(t, E) == "decline", t)
    for t in ("file it", "what does 3 do?", "thanks", "ok thanks", "", "   ",
              "file a task for the trader bars", "can you look at cadence",
              "how long does 1 take?", "what's the momentum universe now"):
        check(f"{t!r} is no answer: the plan waits", O.plan_answer(t, E) == "", t)
    check("an empty plan is never answered", O.plan_answer("yes", []) == "")
    # Once a message has passed the plan by, a bare acknowledgement is about
    # that message's reply, not the plan.
    for t in ("ok", "okay", "sure", "k", "fine", "sounds good", "ok!", "👍 sure", "👍",
              "👍🏽", ":+1:", "✅", "for trader: ok"):
        check(f"after a passed-by message, {t!r} no longer answers",
              O.plan_answer(t, E, passed=True) == "" and (
                  t == "sounds good" or O.plan_answer(t, E) == "approve"), t)
    for t in ("yes", "go", "run them", "do 1 and 3", "ok, do 2", "2", "yes please",
              "ok yes", "yes 👍", "👍 do 2"):
        check(f"but {t!r} still does", O.plan_answer(t, E, passed=True) == "approve", t)
    check("and a no still spends it", O.plan_answer("ok no", E, passed=True) == "decline")
    st = O.State(Path(tempfile.mkdtemp()) / "operator.json")
    st.set_thread("D1:1.0")
    st.set_plan(E, by="operator", thread="D1:1.0")
    t1, t2, t3 = (f"{time.time() + i:.6f}" for i in (1, 2, 3))
    check("'file it' gets no plan and leaves it unanswered",
          st.answer_plan(t1, "file it") is None and not st.plan().get("answered")
          and st.plan().get("passed") == [t1])
    check("and the 'ok' after it (about the filing) gets none either",
          st.answer_plan(f"{time.time() + 1.5:.6f}", "ok") is None
          and not st.plan().get("answered"))
    got = st.answer_plan(t2, "yes")
    check("the 'yes' after it answers it", got and got.get("answered") == t2
          and st.plan().get("answered") == t2)
    check("and is the plan's only answer", st.answer_plan(t3, "yes") is None
          and st.answer_plan(t1, "yes") is None)
    st.set_plan(E, by="operator", thread="D1:1.0")
    check("a no answers it too, so a yes after it gets nothing",
          st.answer_plan(f"{time.time() + 4:.6f}", "no") is not None
          and st.answer_plan(f"{time.time() + 5:.6f}", "yes") is None)


def test_ops_plan_waits_through_an_unrelated_message():
    import ops as OPS
    import operator_role as O
    print("\nplans: an unrelated message between the plan and your yes leaves it waiting")
    w = _ops_world()
    PLAN = ("trader-env-set", "MOMENTUM_UNIVERSE", "pit", "--op", "tailscale-on",
            "--op", "trader-launchd-retire", "com.traderjoe.stocktwits")
    plist = w.agents / "com.traderjoe.stocktwits.plist"
    tailscale = lambda: any(c.startswith("tailscale serve --bg") for c in _calls(w))

    def reset():
        (w.trader / ".env").write_text(w.env_text)
        if not plist.exists():
            import plistlib
            with open(plist, "wb") as f:
                plistlib.dump({"Label": "com.traderjoe.stocktwits",
                               "ProgramArguments": ["/bin/true"]}, f)
        w.calls.write_text("")

    def ran():
        return (_env_value(w, "MOMENTUM_UNIVERSE") == "pit", tailscale(), not plist.exists())

    def run_all(g):
        _plan_run(w, g, 1, "trader-env-set", "MOMENTUM_UNIVERSE", "pit")
        _plan_run(w, g, 2, "tailscale-on")
        _plan_run(w, g, 3, "trader-launchd-retire", "com.traderjoe.stocktwits")
        return ran()

    with _fake_path(w):
        # 2026-11-17: 'file it', then 'yes'.
        reset()
        _plan_record(w, *PLAN)
        g = _plan_turn(w, "file it")
        r = _plan_run(w, g, 1, "trader-env-set", "MOMENTUM_UNIVERSE", "pit")
        check("'file it' runs nothing: it is no answer to the plan",
              r.returncode == 1 and "no answer to it" in r.stderr and ran() == (False,) * 3,
              r.stderr)
        g = _plan_turn(w, "ok")
        check("nor does the 'ok' that acknowledges the filing", run_all(g) == (False,) * 3,
              ran())
        g = _plan_turn(w, "yes")
        check("and the 'yes' after it runs the whole plan", run_all(g) == (True,) * 3, ran())
        runs = [x for x in OPS.read_log(w.log, 100) if x.get("plan_entry")
                and not x.get("refused")]
        yes_ts = (w.grants.get(g, "D1:1.0", "tsk_turn_mine") or {}).get("ts")
        check("each run records the plan id, its entry and the yes's ts",
              [x["plan_entry"] for x in runs] == [1, 2, 3]
              and all(x.get("plan_id", "").startswith("plan_") and x.get("decided_ts") == yes_ts
                      for x in runs), runs)

        # 'what does 3 do?', then 'do 1 and 3'.
        reset()
        _plan_record(w, *PLAN)
        g = _plan_turn(w, "what does 3 do?")
        r = _plan_run(w, g, 3, "trader-launchd-retire", "com.traderjoe.stocktwits")
        check("a question runs nothing", r.returncode == 1 and plist.exists(), r.stderr)
        g = _plan_turn(w, "do 1 and 3")
        check("and 'do 1 and 3' after it runs 1 and 3, not 2",
              run_all(g) == (True, False, True), ran())

        # 'no', then 'yes'.
        reset()
        _plan_record(w, *PLAN)
        g = _plan_turn(w, "no")
        check("'no' runs nothing", run_all(g) == (False,) * 3, ran())
        g = _plan_turn(w, "yes")
        r = _plan_run(w, g, 2, "tailscale-on")
        check("and spends the plan: the 'yes' after it runs nothing",
              run_all(g) == (False,) * 3 and "no plan was waiting" in r.stderr, r.stderr)

        # A new plan replaces the waiting one.
        reset()
        _plan_record(w, *PLAN)
        old = w.state.plan()["id"]
        g = _plan_turn(w, "file it")
        _plan_record(w, "tailscale-on")
        g = _plan_turn(w, "yes")
        r = _plan_run(w, g, 1, "trader-env-set", "MOMENTUM_UNIVERSE", "pit")
        check("a new plan replaces the waiting one: its entry 1 is the new plan's",
              w.state.plan() is not None and w.state.plan()["id"] != old
              and r.returncode == 1 and "is `tailscale-on`" in r.stderr
              and _env_value(w, "MOMENTUM_UNIVERSE") is None, r.stderr)
        r = _plan_run(w, g, 1, "tailscale-on")
        check("and the yes runs the new plan (its checks fail against the fakes; it ran)",
              tailscale(),
              r.stdout + r.stderr)

        # After a day, 'yes' is refused.
        reset()
        _plan_record(w, *PLAN)
        g = _plan_turn(w, "file it")
        data = json.loads(w.statefile.read_text())
        data["plan"]["at"] -= O.PLAN_TTL_S + 5
        w.statefile.write_text(json.dumps(data))
        g = _plan_turn(w, "yes")
        r = _plan_run(w, g, 1, "trader-env-set", "MOMENTUM_UNIVERSE", "pit")
        check("a plan left waiting still expires after a day: 'yes' then runs nothing",
              run_all(g) == (False,) * 3 and "no plan was waiting" in r.stderr, r.stderr)

        # A sweep never runs it, waiting or not.
        reset()
        _plan_record(w, *PLAN)
        _plan_turn(w, "file it")
        r = _plan_run(w, "", 1, "trader-env-set", "MOMENTUM_UNIVERSE", "pit", turn="tsk_sweep")
        out = _ops_ask(w, {"action": "run", "name": "tailscale-on", "args": [],
                           "plan_entry": 2, "caller_role": "operator",
                           "caller_thread": "D1:1.0", "caller_task": "tsk_sweep",
                           "notes": "x"})
        check("a sweep turn never runs a waiting plan", r.returncode == 1 and not out["ok"]
              and ran() == (False,) * 3, r.stderr + str(out))
        g = _plan_turn(w, "yes")
        check("and it is still there for your yes", run_all(g) == (True,) * 3, ran())


def test_requested_filing_passes_a_full_cap_and_a_sweep_does_not():
    import operator_role as O
    import scoping as S
    import tasks as T
    print("\nfiling: work you asked for passes a full proposal cap; a sweep's does not")
    w = _requested_world()
    HOME = "D5:5.0"
    goal = "Rebuild trader's momentum universe point-in-time, as the user asked today"

    def file(**kw):
        payload = {"key": HOME, "goal": goal, "project": "alpha",
                   "caller_role": "operator", "caller_task": "tsk_turn", **kw}
        try:
            return w.file(payload)
        except Exception as e:
            return {"ok": None, "error": f"raised {e!r}"}

    check("the board is full", S.backlog_full(S.open_proposals(w.ts.by_project("alpha"))))
    out = file(propose=True, caller_task="tsk_sweep")
    check("a sweep's filing is refused by the cap", not out["ok"]
          and "standing limit" in out["error"], out)
    out = file(requested_by_user=True, notes="user: please rebuild it",
               caller_task="tsk_sweep")
    check("and claiming the user asked, with no grant, is refused",
          not out["ok"] and "turn the user started" in out["error"], out)
    g = w.grants.open(HOME, "1763200000.000100", "U_ME", "file the universe rebuild", "tsk_turn")
    out = file(requested_by_user=True, caller_grant=g, notes="  ")
    check("in your turn, without --notes, refused", not out["ok"]
          and "--notes" in out["error"], out)
    out = file(requested_by_user=True, caller_grant=g, notes="user: please rebuild it",
               caller_task="tsk_other")
    check("your grant from another turn lifts nothing", not out["ok"], out)
    out = file(requested_by_user=True, caller_grant=g, notes="user: x",
               health="alpha/ci")
    check("nor files a health fix", not out["ok"]
          and "not a health fix" in out["error"], out)
    out = w.file({"key": "C9:9.0", "goal": goal, "project": "alpha", "caller_role": "assistant",
                  "requested_by_user": True, "caller_grant": g, "notes": "x"})
    check("only the operator files on your request", not out["ok"]
          and "--requested-by-user is the operator's" in out["error"], out)
    out = file(requested_by_user=True, caller_grant=g,
               notes="user: please rebuild the whole momentum universe tonight")
    check("in your turn, notes claiming words you never wrote are refused",
          not out["ok"] and "quoting the user's own message" in out["error"], out)
    before = len(w.ts.by_project("alpha"))
    check("none of those filed anything", before == S.max_open_proposals())
    out = file(requested_by_user=True, caller_grant=g,
               notes="user: 'file the universe rebuild'")
    check("your explicit request passes the full cap, straight to the queue",
          out.get("ok") and out.get("state") == T.QUEUED and out.get("requested_by_user"), out)
    rec = w.ts.get(out.get("id") or "") or {}
    rq = rec.get("requested_by_user") or {}
    check("the task records the exemption: whose word, which message, what they said",
          rq.get("by") == "U_ME" and rq.get("ts") == "1763200000.000100"
          and rq.get("notes") == "user: 'file the universe rebuild'"
          and rq.get("open_proposals") == S.max_open_proposals()
          and (rec.get("result") or {}).get("decided_by") == O.DECIDED_BY
          and any("filed on the request of U_ME" in e.get("detail", "")
                  for e in rec.get("events") or []), rec)
    out = file(requested_by_user=True, caller_grant=g, propose=True,
               goal="A proposal: review a universe rebuild plan first, before anything runs",
               notes="as a proposal -- user: 'file the universe rebuild'")
    check("asked for as a proposal, it is one, past the cap",
          out.get("ok") and out.get("state") == T.PROPOSED, out)
    out = file(requested_by_user=True, caller_grant=g,
               goal="Raise the leverage cap on the live momentum sleeve to 1.5x",
               notes="user: 'file the universe rebuild'")
    check("your real words beside a goal you never asked for are refused",
          not out["ok"] and "the goal must be about" in out["error"], out)
    out = file(propose=True, goal="Another idea of its own, while the board is full")
    check("in the same turn, its own idea is still capped",
          not out["ok"] and "standing limit" in out["error"], out)
    w.grants.close(g)
    out = file(requested_by_user=True, caller_grant=g, notes="user: x",
               goal="One more after the turn has ended, which must be refused")
    check("once your turn has ended, refused", not out["ok"], out)


def test_request_quote_refusal_needs_the_users_own_words():
    import operator_role as O
    print("\nfiling on your request: --notes must quote your message, not invent it")
    said = "Please file the universe rebuild for trader, it's overdue."
    ok = lambda n: O.request_quote_refusal(n, said) == ""
    check("the whole message, case and punctuation aside", ok("please FILE the universe rebuild, for trader"))
    check("after a leading label", ok("user: the universe rebuild for trader"))
    check("a quoted part among paraphrase", ok('they want it now: "file the universe rebuild"'))
    check("curly quotes too", ok("asked \u201cuniverse rebuild for trader\u201d today"))
    check("an apostrophe in the quote", ok("user: trader, it\u2019s overdue"))
    check("words never said are refused", not ok("user: rebuild the momentum universe tonight"))
    check("a quote too short to be the request is refused", not ok("user: 'the universe'"))
    check("scattered words of the message are not a quote", not ok("user: trader universe file rebuild"))
    check("a short message may be quoted whole", O.request_quote_refusal("user: 'do it'", "do it") == "")
    check("but not by a fragment of it",
          O.request_quote_refusal("user: 'do'", "do it now") != "")
    check("an empty message quotes nothing", O.request_quote_refusal("user: x", "") != "")


def test_request_goal_refusal_ties_the_goal_to_your_message():
    import operator_role as O
    print("\nfiling on your request: the goal must be about what you asked, not just beside your quote")
    said = "Please file a task to fix the flaky pull tests in cadence"
    ok = lambda g: O.request_goal_refusal(g, said) == ""
    check("a goal naming what you asked passes",
          ok("Fix SupabaseSyncServicePullTests flakiness in Cadence's pull tests"))
    check("inflections count as the same word", ok("Make the flakiness in Cadence go away"))
    check("an unrelated goal is refused", not ok("Raise trader's leverage cap to 1.5x"))
    check("one shared word is not enough", not ok("Rewrite the cadence onboarding screens"))
    check("the asking and filing words tie nothing",
          not ok("Please file a task to do the thing you want"))
    check("a message of one content word needs only that word",
          O.request_goal_refusal("Ship the release notes for mondays", "do mondays") == "")
    check("a message with no content words leaves it to the quote rule",
          O.request_goal_refusal("anything at all", "yes please do it") == "")
    check("the refusal says why", "shares 0" in O.request_goal_refusal("Raise leverage", said))
    g = O.request_goal_refusal
    check("short words match their plurals", g("Fixes bugs in the login", "fix the bug in login") == "")
    check("two-letter acronyms count", g("Make CI less slow", "CI is slow") == ""
          and g("Make the build less slow", "CI is slow") != "")
    goal = "Rebuild the momentum universe point-in-time"
    for m in ("go ahead and file it", "sounds good, file the first one",
              "perfect, the recommended one please", "yep, the second suggested option"):
        check(f"saying yes or pointing is not words the goal must repeat: {m!r}", g(goal, m) == "")
    for m in ("yes, he approves the proposals", "the one he recommends", "she suggests it",
              "approving both proposals", "agrees with the recommendations"):
        check(f"nor are other forms of those words: {m!r}", g(goal, m) == "")
    check("filler's forms do not swallow real words that merely start alike",
          O._content(O._words("performance record recovery format approach preview"))
          == {"perf", "reco", "form", "appr", "prev"})
    check("nor do short filler words' look-alikes",
          {"not", "cool", "min", "good", "will", "last"}
          <= O._content(O._words("noted cooling mining goods willing lasting")))
    check("but the fix named beside them still is",
          g(goal, "sure, file the suggested fix") != ""
          and g("Fix the universe rebuild", "sure, file the suggested fix") == "")
    check("a contraction's pieces are not words of the request",
          "isn" not in O._content(O._words("it isn't working, the importer"))
          and g("Get the importer working again", "it isn't working, the importer") == "")


def test_operator_role_binds_each_module_name_once():
    import ast
    print("\noperator_role: no module-level name is bound twice (a second _FILLER silently replaced the first)")
    tree = ast.parse((BASE / "operator_role.py").read_text())
    seen, twice = set(), []
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            targets = [ast.Name(id=node.name)]
        for t in targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    (twice.append(n.id) if n.id in seen else seen.add(n.id))
    check("each name once", not twice, twice)


def test_cli_task_requested_by_user_sends_notes():
    print("\n`silkworm task --requested-by-user` sends the request and refuses without --notes")
    cli_file_task, sent = _cli_file_task_impl()
    saved = {k: os.environ.get(k) for k in ("SILKWORM_THREAD",)}
    os.environ["SILKWORM_THREAD"] = "D5:5.0"
    try:
        cli_file_task(["--requested-by-user", "--notes", "user: do it", "rebuild the universe"])
        check("it sends requested_by_user and the notes, and the goal alone as goal",
              sent and sent[-1][1].get("requested_by_user") is True
              and sent[-1][1].get("notes") == "user: do it"
              and sent[-1][1].get("goal") == "rebuild the universe", sent)
        n = len(sent)
        try:
            cli_file_task(["--requested-by-user", "rebuild the universe"])
            code = 0
        except SystemExit as e:
            code = e.code
        check("without --notes it asks nothing and exits 1", code == 1 and len(sent) == n)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_your_turn_carries_the_plan_you_answered():
    print("\nyour turn's grant carries the pending plan recorded before your message")
    tree = ast.parse((BASE / "bot.py").read_text())
    opens = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "open"
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "OPERATOR_GRANTS"]
    plan_kw = [k for c in opens for k in c.keywords if k.arg == "plan"]
    check("handle_prompt opens your grant with plan=pending_plan(msg_ts, text)",
          len(opens) == 1 and len(plan_kw) == 1 and isinstance(plan_kw[0].value, ast.Call)
          and getattr(plan_kw[0].value.func, "id", "") == "pending_plan"
          and [getattr(a, "id", "") for a in plan_kw[0].value.args] == ["msg_ts", "text"],
          ast.dump(plan_kw[0]) if plan_kw else "no plan= keyword")
    import operator_role as O
    ns = lift("pending_plan", OPERATOR=types.SimpleNamespace(
        answer_plan=lambda ts, text: (_ for _ in ()).throw(OSError("unreadable"))))
    check("a plan that cannot be read costs your turn nothing: no plan, no raise",
          ns["pending_plan"]("1.0", "yes") is None)
    st = O.State(Path(tempfile.mkdtemp()) / "operator.json")
    st.set_thread("D1:1.0")
    st.set_plan([{"name": "tailscale-on", "args": []}], by="operator", thread="D1:1.0")
    ns = lift("pending_plan", OPERATOR=st)
    check("and a readable one is attached", (ns["pending_plan"](f"{time.time() + 1:.6f}", "yes")
                                             or {}).get("entries", [{}])[0].get("name")
          == "tailscale-on")
    st.set_thread("D2:2.0")
    check("a plan from a thread that is no longer its home is not pending", st.plan() is None)


def test_operator_state_writes_hold_one_lock():
    print("\noperator.json: every read-modify-write holds the one lock")
    tree = ast.parse((BASE / "operator_role.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "State")
    bad = []
    for fn in cls.body:
        if not isinstance(fn, ast.FunctionDef) or fn.name == "_save":
            continue
        saves = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
                 and isinstance(c.func, ast.Attribute) and c.func.attr == "_save"]
        locked = [w for w in ast.walk(fn) if isinstance(w, ast.With) and any(
            getattr(i.context_expr, "id", "") == "_STATE_LOCK" for i in w.items)]
        inside = {id(c) for w in locked for c in ast.walk(w)}
        if any(id(c) not in inside for c in saves):
            bad.append(fn.name)
    check("no State method saves outside _STATE_LOCK (a stale copy would undo a run "
          "entry's mark, and it would run twice)", not bad, bad)
    import operator_role as O
    check("and it is one lock for every State, not one each",
          isinstance(O._STATE_LOCK, type(__import__("threading").RLock())))


# Shapes GitHub secret scanning (and its partners) recognise. Kept a little
# looser than the real patterns so a fake that *nearly* matches is caught here
# rather than by an alert on the public repo. Each regex is written so that
# its own source text never matches itself.
_SECRET_SHAPES = {
    "Tailscale key": r"tskey-[A-Za-z0-9]+-[A-Za-z0-9_-]{4,}",
    "Tailscale node key": r"nodekey:[0-9a-f]{16,}",
    "Anthropic key": r"sk-ant-[a-z]+[0-9]{2}-[A-Za-z0-9_-]{4,}",
    "Slack token": r"xox[abposre]-[0-9]{3,}-[A-Za-z0-9-]+",
    "Slack app token": r"xapp-[0-9]-[A-Z0-9]+-[0-9]+-[a-f0-9]+",
    "Slack webhook": r"hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+",
    "Supabase secret key": r"sb_secret_[A-Za-z0-9_-]{8,}",
    "Supabase access token": r"sbp_[a-f0-9]{32,}",
    "JWT": r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{8,}",
    "GitHub token": r"gh[pousr]_[A-Za-z0-9]{20,}",
    "GitHub fine-grained token": r"github_pat_[A-Za-z0-9_]{20,}",
    "AWS access key id": r"(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[0-9A-Z]{16}",
    "AWS secret key": r"aws_?secret_?access_?key[\"' :=]+[A-Za-z0-9/+]{40}",
    "private key": r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----",
}


def _secret_shaped_literals(root):
    """(path, line, kind) for every secret-shaped run of text in root's tracked files."""
    import re as _re
    shapes = {k: _re.compile(v, _re.IGNORECASE if k == "AWS secret key" else 0)
              for k, v in _SECRET_SHAPES.items()}
    names = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True,
                           check=True).stdout.decode().split("\0")
    hits = []
    for name in filter(None, names):
        path = Path(root) / name
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue                       # binary: icons and the like
        for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            hits += [(name, n, k) for k, rx in shapes.items() if rx.search(line)]
    return hits, len(names)


def test_no_secret_shaped_literal_in_tracked_source():
    print("\nsecret scanning: no tracked file carries a literal a scanner would flag")
    # 2e3ef9e put a fake 'tskey-auth-…' in this file; it was fake, but it was
    # the shape of a real one, and GitHub raised an alert on the public repo.
    # Fakes are assembled at runtime ("tskey-" + "auth-…") so the source holds
    # no contiguous match.
    hits, scanned = _secret_shaped_literals(BASE)
    check("the scan actually looked at the tree", scanned > 20, scanned)
    check("no secret-shaped literal anywhere in tracked source", not hits, hits[:20])

    # And it catches one: each shape, planted in a scratch repo, is found.
    plants = {
        "Tailscale key": "tskey-" + "auth-kAbCdEf123CNTRL-" + "z" * 12,
        "Tailscale node key": "nodekey:" + "9c" * 32,
        "Anthropic key": "sk-ant-" + "api03-" + "Q" * 40,
        "Slack token": "xoxb-" + "123456789012-123456789012-" + "a" * 24,
        "Slack app token": "xapp-" + "1-A0123ABCD-1234567890-" + "ab" * 32,
        "Slack webhook": "https://hooks.slack" + ".com/services/T0123ABCD/B0123ABCD/" + "x" * 24,
        "Supabase secret key": "sb_secret_" + "k" * 24,
        "Supabase access token": "sbp_" + "f00dfeed" * 5,
        "JWT": ".".join(("eyJ" + "hbGciOiJIUzI1NiJ9", "eyJ" + "zdWIiOiIxIn0", "c" * 30)),
        "GitHub token": "ghp_" + "A1" * 18,
        "GitHub fine-grained token": "github_pat_" + "B2" * 20,
        "AWS access key id": "AKIA" + "ABCDEFGHIJ234567",
        "AWS secret key": "aws_secret_access_key = " + "wJalrXUtnFEMI/K7MDENG/" + "b" * 18,
        "private key": "-----BEGIN " + "RSA PRIVATE KEY-----",
    }
    check("every shape has a plant that exercises it", set(plants) == set(_SECRET_SHAPES),
          set(_SECRET_SHAPES) ^ set(plants))
    scratch = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", str(scratch)], check=True)
    for i, (kind, text) in enumerate(plants.items()):
        (scratch / f"f{i}.py").write_text(f"x = {text!r}\n")
    (scratch / "blob.bin").write_bytes(b"\0" + plants["Tailscale key"].encode())
    (scratch / "split.py").write_text('x = "tskey-" + "auth-kAbCdEf123CNTRL-" + "z" * 12\n')
    subprocess.run(["git", "-C", str(scratch), "add", "-A"], check=True)
    found, _ = _secret_shaped_literals(scratch)
    got = {k for _, _, k in found}
    check("each planted literal is caught", got >= set(plants), set(plants) - got)
    check("binaries are skipped, and a fake assembled at runtime is not a hit",
          not [f for f, _, _ in found if f in ("blob.bin", "split.py")], found)
    shutil.rmtree(scratch, ignore_errors=True)


def _review_pair(st, T, parent_state, review_state=None):
    """An implementor blocked on its review, the review then failing the way
    refuse_review fails it -- which releases the parent to awaiting_approval
    (it has a result). Then the parent is moved to `parent_state`."""
    p = st.create("implement the thing", role="implementor", driver="queue")
    st.transition(p["id"], T.RUNNING)
    st.update(p["id"], result={"text": "did it"})
    r = st.create("review it", role="reviewer", driver="queue", parent=p["id"],
                  title="Review: thing")
    st.update(p["id"], blocked_on=[r["id"]])
    st.transition(p["id"], T.BLOCKED, f"awaiting review {r['id']}")
    st.transition(r["id"], T.RUNNING)
    if review_state:
        st.transition(r["id"], review_state, "could not check out the branch",
                      _release=False)
    else:
        st.transition(r["id"], T.FAILED,
                      "could not check out silkworm/x, so nothing was reviewed")
    if parent_state and st.get(p["id"])["state"] != parent_state:
        st.transition(p["id"], parent_state, "moved on")
    return p["id"], r["id"]


def test_a_review_closes_with_its_task():
    """tsk_0051063496: a failed review of a task later cancelled sat in Needs
    you, offering Retry, for three weeks -- and was retried twice into the
    same failure. Ending a task closes its reviews; Retry is refused, and not
    drawn, on a review its task no longer waits on; startup sweeps the rest."""
    import tasks as T
    import board as B
    import home as H
    from tasks import TaskStore
    print("\na review closes with the task it reviews")
    st = TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    handle = _bot_func("handle_tasks", task_store=st, tasks=T,
                       log=logging.getLogger("test"))

    # (1) parent cancelled after its review failed: the review is closed.
    pid, rid = _review_pair(st, T, T.AWAITING_APPROVAL)
    check("a failed review releases its parent to awaiting_approval",
          st.get(pid)["state"] == T.AWAITING_APPROVAL, st.get(pid)["state"])
    st.transition(pid, T.CANCELLED, "dismiss via ui")
    rec = TaskStore(st._path).get(rid)
    check("cancelling the parent cancels its failed review, on disk",
          rec["state"] == T.CANCELLED, rec["state"])
    check("with a reason that names the parent and its state",
          pid in rec["events"][-1]["detail"]
          and "cancelled" in rec["events"][-1]["detail"], rec["events"][-1])
    r = handle({"action": "retry", "id": rid})
    check("and it can no longer be retried", not r["ok"]
          and st.get(rid)["state"] == T.CANCELLED, r)

    # A review the runner claims between being listed and being closed is
    # running: it must not be labelled cancelled while its agent carries on.
    p = st.create("work", role="implementor", driver="queue")
    q = st.create("review", role="reviewer", driver="queue", parent=p["id"])
    real_transition = st.transition
    raced = {"done": False}

    def claims_first(tid, to_state, *a, **kw):
        if tid == q["id"] and to_state == T.CANCELLED and not raced["done"]:
            raced["done"] = True
            st.claim(only_roles={"reviewer"})
        return real_transition(tid, to_state, *a, **kw)
    st.transition = claims_first
    try:
        real_transition(p["id"], T.CANCELLED, "cancel via ui")
    finally:
        st.transition = real_transition
    check("a review claimed while its task was being closed is left running",
          raced["done"] and st.get(q["id"])["state"] == T.RUNNING,
          (raced, st.get(q["id"])["state"]))

    # Queued and blocked reviews close too; a running one is left to finish.
    for rstate in (T.QUEUED, T.BLOCKED):
        p = st.create("work", role="implementor", driver="queue")
        q = st.create("review", role="reviewer", driver="queue", parent=p["id"])
        if rstate == T.BLOCKED:
            st.transition(q["id"], T.BLOCKED, "quota")
        st.transition(p["id"], T.CANCELLED, "cancel via ui")
        check(f"a {rstate} review of a cancelled task is cancelled",
              st.get(q["id"])["state"] == T.CANCELLED, st.get(q["id"])["state"])
    p = st.create("work", role="implementor", driver="queue")
    q = st.create("review", role="reviewer", driver="queue", parent=p["id"])
    st.transition(q["id"], T.RUNNING)
    st.transition(p["id"], T.CANCELLED, "cancel via ui")
    check("a running review is left to finish",
          st.get(q["id"])["state"] == T.RUNNING, st.get(q["id"])["state"])
    # Somebody else's review is not touched.
    p1 = st.create("one", role="implementor", driver="queue")
    p2 = st.create("two", role="implementor", driver="queue")
    q2 = st.create("review two", role="reviewer", driver="queue", parent=p2["id"])
    st.transition(p1["id"], T.CANCELLED, "cancel")
    check("only the ended task's own reviews close",
          st.get(q2["id"])["state"] == T.QUEUED, st.get(q2["id"])["state"])
    # A save that fails while closing a review does not undo the parent's move.
    p = st.create("work", role="implementor", driver="queue")
    q = st.create("review", role="reviewer", driver="queue", parent=p["id"])
    real = st._save
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("disk full")
        real()
    st._save = flaky
    try:
        moved = st.transition(p["id"], T.CANCELLED, "cancel")
        raised = False
    except OSError:
        raised = True
    st._save = real
    check("a review that cannot be closed does not fail its parent's move",
          not raised and moved["state"] == T.CANCELLED
          and st.get(q["id"])["state"] == T.QUEUED,
          (raised, st.get(q["id"])["state"]))

    # (3) the startup sweep closes what ended before the rule, or across a crash.
    sweep_review = q["id"]
    closed = st.close_orphaned_reviews()
    check("the startup sweep closes a review left open by an ended task",
          sweep_review in closed
          and TaskStore(st._path).get(sweep_review)["state"] == T.CANCELLED, closed)
    pid_s, rid_s = _review_pair(st, T, T.AWAITING_APPROVAL)
    # As tasks.json held it on 2026-11-15: parent cancelled, review failed.
    st._data[pid_s]["state"] = T.CANCELLED
    st._save()
    check("(fixture: before the sweep it is still failed, in Needs you)",
          st.get(rid_s)["state"] == T.FAILED
          and "cancelled" in T.no_retry(st.get(rid_s), st.all()))
    closed = st.close_orphaned_reviews()
    check("and the sweep closes it", rid_s in closed
          and st.get(rid_s)["state"] == T.CANCELLED, closed)
    check("a second sweep finds nothing", st.close_orphaned_reviews() == [])
    sched = next(n for n in ast.parse((BASE / "bot.py").read_text()).body
                 if isinstance(n, ast.FunctionDef) and n.name == "_task_scheduler")
    loop = next(n for n in ast.walk(sched) if isinstance(n, ast.While))
    called = {c.func.attr for c in ast.walk(loop) if isinstance(c, ast.Call)
              and isinstance(c.func, ast.Attribute)}
    check("the scheduler runs the sweep on its beat, from startup",
          "close_orphaned_reviews" in called, called)
    pid_live, rid_live = _review_pair(st, T, T.AWAITING_APPROVAL)
    st.close_orphaned_reviews()
    check("the sweep leaves a failed review its task still waits on",
          st.get(rid_live)["state"] == T.FAILED)

    # (2) Retry: allowed while the parent waits, refused once it has moved on.
    r = handle({"action": "retry", "id": rid_live})
    check("a failed review whose task awaits approval can be retried",
          r["ok"] and st.get(rid_live)["state"] == T.QUEUED, r)
    pid_b, rid_b = _review_pair(st, T, None, review_state=T.FAILED)
    check("(fixture: the parent is still blocked on its review)",
          st.get(pid_b)["state"] == T.BLOCKED
          and rid_b in st.get(pid_b)["blocked_on"], st.get(pid_b))
    check("Retry is drawn on it", not T.no_retry(st.get(rid_b), st.all()))
    r = handle({"action": "retry", "id": rid_b})
    check("a failed review whose task is still blocked on it can be retried",
          r["ok"] and st.get(rid_b)["state"] == T.QUEUED, r)

    pid_f, rid_f = _review_pair(st, T, T.QUEUED)       # sent back for rework
    why = T.no_retry(st.get(rid_f), st.all())
    check("a review whose task has moved on is not offered Retry",
          pid_f in why and "queued" in why, why)
    r = handle({"action": "retry", "id": rid_f})
    check("and Retry on it is refused, saying why",
          not r["ok"] and why in r.get("error", "")
          and st.get(rid_f)["state"] == T.FAILED, r)
    pid_n, rid_n = _review_pair(st, T, T.AWAITING_APPROVAL)
    newer = st.create("review again", role="reviewer", driver="queue",
                      parent=pid_n)
    st._data[newer["id"]]["created"] = st.get(rid_n)["created"] + 1
    check("nor one superseded by a newer review of the same task",
          newer["id"] in T.no_retry(st.get(rid_n), st.all())
          and not handle({"action": "retry", "id": rid_n})["ok"])
    # Blocked, but on a later review than this one: the old one is history.
    pid_o, rid_o = _review_pair(st, T, None, review_state=T.FAILED)
    later = st.create("review again", role="reviewer", driver="queue", parent=pid_o)
    st.update(pid_o, blocked_on=[later["id"]])
    why_o = T.no_retry(st.get(rid_o), st.all())
    r = handle({"action": "retry", "id": rid_o})
    check("nor one whose task is blocked on a different review",
          "different review" in why_o and not r["ok"]
          and st.get(rid_o)["state"] == T.FAILED, (why_o, r))
    plain = st.create("not a review", role="implementor", driver="queue")
    st.transition(plain["id"], T.RUNNING)
    st.transition(plain["id"], T.FAILED, "boom")
    check("an ordinary failed task is retried as before",
          handle({"action": "retry", "id": plain["id"]})["ok"])

    # The boards draw by the same rule.
    items = T.mark_no_retry([dict(r, id=i) for i, r in st.all().items()])
    marked = {t["id"]: t for t in items}
    acts = lambda t: [b[1] for b in H.buttons_for(t)]
    check("Slack board: no Retry on the moved-on review, Dismiss still there",
          "retry" not in acts(marked[rid_f]) and "dismiss" in acts(marked[rid_f]),
          acts(marked[rid_f]))
    pid_w, rid_w = _review_pair(st, T, T.AWAITING_APPROVAL)
    items = T.mark_no_retry([dict(r, id=i) for i, r in st.all().items()])
    marked = {t["id"]: t for t in items}
    check("Slack board: Retry on a review still waited on",
          "retry" in acts(marked[rid_w]), acts(marked[rid_w]))
    def offers(view, tid):
        flat = json.dumps(view)
        return (f'"home_retry", "value": "{tid}|' in flat
                or f'"retry|{tid}|' in flat)
    view = H.render(items, now=time.time(), max_attention=500)
    check("the rendered board offers Retry on one and not the other, and shows both",
          offers(view, rid_w) and not offers(view, rid_f)
          and rid_f in json.dumps(view))
    check("dashboard rows and cards carry the same verdict",
          why and B.panel_row(dict(st.get(rid_f), no_retry=why))["no_retry"] == why
          and B.card(st.get(rid_f), by_id=st.all())["no_retry"] == why
          and B.detail(st.get(rid_f), st.all().values())["no_retry"] == why
          and B.card(st.get(rid_w), by_id=st.all())["no_retry"] == "")
    # bot._with_costs is what the dashboard's task list is built from.
    wc = _bot_func("_with_costs", task_store=st, tasks=T, board=B,
                   research=__import__("research"),
                   slacklinks=__import__("slacklinks"))
    rows = {t["id"]: t for t in wc([st.get(rid_f), st.get(rid_w)])}
    check("the dashboard list marks them the same way",
          why and rows[rid_f]["no_retry"] == why and rows[rid_w]["no_retry"] == "", rows)

    import visualizer as V
    import re as _re
    js = _re.search(r"<script>(.*?)</script>", V.PAGE, _re.S).group(1)
    fn = js[js.index("function taskButtons(t)"):js.index("async function renderTasks()")]
    esc = _re.search(r"const esc = .*", js).group(0)
    harness = (esc + "\n" + fn + "\nprocess.stdout.write(JSON.stringify(["
               'taskButtons({id: "tsk_1", state: "failed", role: "reviewer", no_retry: "x is cancelled"}),'
               'taskButtons({id: "tsk_2", state: "failed", role: "reviewer", no_retry: ""})]));\n')
    try:
        run = subprocess.run(["node", "-e", harness], capture_output=True, text=True,
                             timeout=30)
        out = json.loads(run.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        out = None
    if out is None:
        print("    (node unavailable — dashboard checked by contract only)")
        check("dashboard taskButtons reads no_retry", "t.no_retry" in fn)
    else:
        check("dashboard: no Retry when the review is not waited on, and why",
              "'retry'" not in out[0] and "x is cancelled" in out[0]
              and "'dismiss'" in out[0], out[0])
        check("dashboard: Retry when it is", "'retry')\">Retry" in out[1], out[1])

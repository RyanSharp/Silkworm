"""Restart, deploy, boot and releases.

Run by tests/run.py with every other tests/test_*.py; see tests/harness.py."""

from harness import *  # noqa: F401,F403


# (moved here: used only by this file's tests)
def _board_view(board) -> dict:
    """What the board shows now, as the view a redraw would post."""
    return {"blocks": board.blocks()}
# --- a landed fix is not a running fix ---------------------------------------
# Silkworm auto-merges reviewed work onto its own main and nothing restarts it,
# so the commit sat in the tree while the live process kept serving the
# revision it booted with. Six days of landings were invisible from outside:
# startup logged the workspace and the turn limits but never the revision, and
# the only way to answer "is the running bot the code I am reading" was to
# probe for a behaviour difference and infer backwards.

def test_revision_drift():
    import revision as R
    print("\nthe running revision is reported, and drift from it is visible")

    here, there = "a" * 40, "b" * 40
    same = R.drift({"sha": here, "branch": "main"}, {"sha": here, "branch": "main"})
    check("a matching sha is current", same["state"] == "current")
    check("and is not reported as behind anything", same["behind"] == 0)

    moved = R.drift({"sha": here, "branch": "main"},
                    {"sha": there, "branch": "main"}, 6)
    check("a moved checkout is stale", moved["state"] == "stale", moved["state"])
    check("and says how far behind", moved["behind"] == 6)
    check("the message names both revisions and the gap",
          "6 commits behind" in R.describe(moved)
          and here[:8] in R.describe(moved) and there[:8] in R.describe(moved),
          R.describe(moved))
    check("one commit is not pluralised",
          "1 commit behind" in R.describe(
              R.drift({"sha": here}, {"sha": there}, 1)))

    # Not knowing must never read as all-clear. A missing git, a directory that
    # is not a repo, or a bot too old to report the field would otherwise every
    # one of them come back "current" -- the same silence this check exists to
    # break, told more confidently.
    for label, started, head in (
            ("no startup revision", {}, {"sha": here}),
            ("no revision on disk", {"sha": here}, {}),
            ("neither", {}, {})):
        d = R.drift(started, head)
        check(f"{label} is unknown, not current", d["state"] == "unknown", d["state"])

    # `behind` only counts when git could count. A rebase or a dropped branch
    # leaves the startup commit unreachable, and a guess of 0 would claim "up
    # to date" about a revision it could not even find.
    unreachable = R.drift({"sha": here}, {"sha": there}, -1)
    check("an uncountable gap is still stale", unreachable["state"] == "stale")
    check("but claims no count", unreachable["behind"] == 0)
    check("and says so without a number",
          "behind" in R.describe(unreachable) and "-1" not in R.describe(unreachable),
          R.describe(unreachable))

    # A process that booted from a modified tree is running code that was never
    # any commit -- and if those edits were then reverted, code that exists
    # nowhere on disk. A matching sha is the weakest evidence here, not the
    # strongest, so it must not come back "current".
    dirty = R.drift({"sha": here, "dirty": True}, {"sha": here})
    check("a dirty boot is carried through", dirty["dirty"] is True)
    check("and is unknown, not current", dirty["state"] == "unknown", dirty["state"])
    check("named apart from having no revision at all",
          dirty["why"] == "dirty-boot"
          and R.drift({}, {})["why"] == "no-revision")
    check("with a message that says why it cannot be checked",
          "uncommitted" in R.describe(dirty) and "cannot be checked" in R.describe(dirty),
          R.describe(dirty))
    check("a clean match is still plainly current",
          R.drift({"sha": here}, {"sha": here})["state"] == "current")

    # The two git calls are separate processes, so a commit landing between
    # them pairs an unchanged sha with a nonzero count. The sha is the answer.
    raced = R.drift({"sha": here}, {"sha": here}, 2)
    check("a count against an unchanged sha is not a gap",
          raced["state"] == "current" and raced["behind"] == 0,
          f"{raced['state']} / {raced['behind']}")

    # Against a real repository, end to end.
    repo = Path(tempfile.mkdtemp()) / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t"); run("config", "user.name", "t")
    (repo / "f").write_text("one")
    run("add", "."); run("commit", "-qm", "one")
    first = R.of(repo)
    check("a real checkout resolves to a sha and a branch",
          len(first["sha"]) == 40 and first["branch"] == "main", str(first))
    check("a clean tree is not dirty", first["dirty"] is False)

    (repo / "f").write_text("two")
    run("commit", "-qam", "two")
    (repo / "f").write_text("three")
    run("commit", "-qam", "three")
    check("git counts what has landed since", R.behind(repo, first["sha"]) == 2)
    live = R.state(repo, first)
    check("the state route calls that stale", live["state"] == "stale", live["message"])
    check("with the real distance", live["behind"] == 2, live["message"])
    check("and a message to show", bool(live["message"]))
    check("the same revision is current",
          R.state(repo, R.of(repo))["state"] == "current")

    # The dashboard polls /status every five seconds and this is two git
    # subprocesses, so the answer is memoised whole -- both halves or neither,
    # or a fresh HEAD gets paired with a count taken against the previous one.
    (repo / "f").write_text("four")
    run("commit", "-qam", "four")
    check("a polled answer is served from cache, not re-resolved",
          R.state(repo, first)["behind"] == 2, "it spawned git again")
    check("and a zero TTL always re-resolves",
          R.state(repo, first, ttl=0)["behind"] == 3,
          "the cache must be a cache, not a one-shot")

    # Detached HEAD reports the literal string rather than a name, and "on
    # HEAD" in a startup log is not a branch anybody can go and look at.
    run("checkout", "-q", "--detach")
    detached = R.of(repo)
    check("a detached HEAD reports no branch rather than the word HEAD",
          detached["branch"] == "", detached["branch"])
    check("and still compares against the checkout",
          R.state(repo, detached, ttl=0)["state"] == "current")
    run("checkout", "-q", "main")

    (repo / "untracked").write_text("x")
    R._cache.clear()
    check("an uncommitted change makes the checkout dirty", R.of(repo)["dirty"] is True)
    check("and booting from one is unknown end to end",
          R.state(repo, R.of(repo), ttl=0)["why"] == "dirty-boot")

    # Somewhere that is not a repository at all.
    R._cache.clear()
    outside = Path(tempfile.mkdtemp())
    check("a non-repo resolves to nothing, not to a sha", R.of(outside)["sha"] == "")
    check("and that is unknown rather than current",
          R.state(outside, first)["state"] == "unknown")
    check("counting from an unknown sha gives no number", R.behind(repo, "") == -1)
    check("counting from a sha this repo never had gives no number",
          R.behind(repo, "c" * 40) == -1)

    # The wiring: the bot must record the revision at boot and report it, and
    # the two status surfaces must read it rather than asking git themselves --
    # which would describe the checkout instead of the process.
    src = (BASE / "bot.py").read_text()
    tree = ast.parse(src)
    assigns = [n for n in tree.body if isinstance(n, ast.Assign)
               and any(getattr(t, "id", "") == "REVISION" for t in n.targets)]
    check("bot.py resolves its revision once, at import", len(assigns) == 1)
    # CODE_DIR, not BASE_DIR: since the pinned release checkout (live.py) the
    # code runs from one tree and the install (state, landings) is another.
    check("from the checkout it was loaded from, not the workspace",
          assigns and "CODE_DIR" in ast.dump(assigns[0].value)
          and "revision" in ast.dump(assigns[0].value))
    check("startup logs it", 'log.info("revision: %s%s on %s%s"' in src)
    check("/status reports it",
          "revision.state(BASE_DIR, REVISION)" in src)
    # Asserted on the branch, not on the text: `_is_own_checkout(cwd)` also
    # appears in the function's own `def` line, so a substring check passes
    # happily with the call site replaced by `if False`.
    landing = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "land_and_record")
    guarded = [n for n in ast.walk(landing) if isinstance(n, ast.If)
               and any(isinstance(c, ast.Call)
                       and getattr(c.func, "id", "") == "_is_own_checkout"
                       for c in ast.walk(n.test))]
    check("landing checks whether it landed into Silkworm's own checkout",
          len(guarded) == 1, f"found {len(guarded)} such branches")
    check("and adds to the reply rather than replacing it",
          guarded and any(isinstance(n, ast.AugAssign) for n in guarded[0].body),
          "the shipit line must survive")
    check("saying the running bot is still the old one",
          guarded and "silkworm deploy" in ast.dump(guarded[0]),
          "the warning has to name the remedy")

    # And the predicate itself, run rather than read.
    own = lift("_is_own_checkout", Path=Path, BASE_DIR=BASE)["_is_own_checkout"]
    check("its own checkout is recognised", own(str(BASE)) is True)
    check("through a non-canonical path", own(str(BASE / "tests" / "..")) is True)
    check("another project's checkout is not", own(str(BASE / "tests")) is False)
    check("and a path that cannot be resolved is not", own("") is False)

    cli = (BASE / "bin" / "silkworm").read_text()
    # Scoped to do_status: `deploy` resolves HEAD itself, to compare with
    # what the bot reports -- status must not.
    status_src = ast.get_source_segment(cli, next(
        n for n in ast.walk(ast.parse(cli))
        if isinstance(n, ast.FunctionDef) and n.name == "do_status"))
    check("silkworm status reads the bot's revision, not git",
          'bot.get("revision")' in status_src and "rev-parse" not in status_src
          and "git_out" not in status_src)
    # Asserted on the call, not on the text: the sentence can be present while
    # being handed to a check that passes, which is the failure it warns about.
    status = next(n for n in ast.walk(ast.parse(cli))
                  if isinstance(n, ast.FunctionDef) and n.name == "do_status")
    unanswered = [n for n in ast.walk(status)
                  if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "check"
                  and any("too old to report" in a.value or "could not resolve" in a.value
                          for a in ast.walk(n) if isinstance(a, ast.Constant)
                          and isinstance(a.value, str))]
    check("both ways of not knowing the revision are checks in silkworm status",
          len(unanswered) == 2, f"found {len(unanswered)}")
    # Subscripting the payload would turn a version-skewed bot -- the exact
    # situation this check exists for -- into a traceback that takes the rest
    # of `silkworm status` down with it, credential checks included.
    revision_reads = [n for n in ast.walk(status)
                      if isinstance(n, ast.Subscript)
                      and getattr(n.value, "id", "") == "rev"]
    check("silkworm status never subscripts the revision payload",
          not revision_reads,
          "a bot reporting a field it does not fill must not crash the check")
    check("and each one fails rather than passes",
          all(isinstance(c.args[1], ast.Constant) and c.args[1].value is False
              for c in unanswered),
          "a bot that cannot say what it is running is not a bot that is up to date")

    viz = (BASE / "visualizer.py").read_text()
    check("the dashboard passes it to the alert bar",
          "renderAlerts(data.sessions, data.slack, data.revision)" in viz)
    check("and alerts on anything that is not current",
          'revision.state !== "current"' in viz)


# --- releases: merged is not released -----------------------------------------
# A push to Cadence's main started an Xcode Cloud archive bound for TestFlight,
# so every task that landed would have been a build. Merging and releasing are
# separate events, and a repo holds several things that ship separately.

def test_release_effects_count_things_not_files():
    # A confirmation that says "6 in supabase/functions" when one function
    # changed six files overstates what reaches production. An edge function
    # is its folder, a migration its file; elsewhere it is files, said so.
    import releases as R
    print("\nreleases: a confirmation counts functions and migrations, not files")
    root = Path(tempfile.mkdtemp())
    repo = root / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                                   "-c", "user.name=t", *a], capture_output=True, text=True)
    def commit(*paths):
        for path in paths:
            p = repo / path; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(path)
        g("add", "-A"); g("commit", "-q", "-m", "c")
    (repo / ".silkworm").mkdir()
    (repo / ".silkworm" / "release.toml").write_text(
        '[targets.backend]\npaths = ["supabase/"]\nship = "command"\ncommands = ["true"]\n'
        '[targets.ios]\npaths = ["ios/"]\nship = "tag"\n')
    commit("supabase/functions/a/index.ts", "supabase/functions/a/deno.json",
           "supabase/functions/a/lib/x.ts", "supabase/functions/b/index.ts",
           "supabase/functions/_shared/jws.ts", "supabase/functions/_shared/apple.ts",
           "supabase/migrations/0001.sql", "supabase/migrations/0002.sql",
           "ios/App/A.swift", "ios/App/Views/B.swift", "ios/App/Views/C.swift")
    targets = R.load(repo)
    got = R.changed(repo, targets["backend"])
    words = [R.describe(d, t) for d, t in got.items()]
    check("four files over two functions are two edge functions, the shared folder named",
          "2 edge functions and shared code (_shared) in supabase/functions" in words, words)
    check("migrations are counted one per file", "2 migrations in supabase/migrations" in words, words)
    check("one function, one migration: singular",
          R.describe("supabase/functions", {"a"}) == "1 edge function in supabase/functions"
          and R.describe("supabase/migrations", {"1.sql"}) == "1 migration in supabase/migrations")
    check("only shared code changed: no function counted",
          R.describe("supabase/functions", {"_shared"}) == "shared code (_shared) in supabase/functions")
    # A file loose in supabase/functions is config beside the functions, not
    # one of them: named, never counted.
    commit("supabase/functions/import_map.json", "supabase/functions/deno.json")
    loose = [R.describe(d, t) for d, t in R.changed(repo, targets["backend"]).items()]
    check("a loose file beside the functions is named, not counted as a function",
          "2 edge functions and shared code (_shared) and deno.json and import_map.json"
          " in supabase/functions" in loose, loose)
    check("only a loose file changed: no function counted",
          R.describe("supabase/functions", {R.LOOSE + "deno.json"})
          == "deno.json in supabase/functions")
    g("reset", "-q", "--hard", "HEAD~1")
    ios = [R.describe(d, t) for d, t in R.changed(repo, targets["ios"]).items()]
    check("a directory of no known kind counts files, and says files",
          ios == ["3 files in ios/App"], ios)
    step = {"commits": ["x c"], "version": (1, 0, 0), "tag": "backend/v1.0.0"}
    eff = R.effects(repo, targets["backend"], step, "main")
    check("the confirmation's first line reads in things",
          eff and "(2 edge functions and shared code (_shared) in supabase/functions, "
                  "2 migrations in supabase/migrations)" in eff[0], eff)


def test_releases():
    import releases as R
    print("\nreleases: merged is not released")
    root = Path(tempfile.mkdtemp())
    origin, repo = root / "origin.git", root / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], capture_output=True)
    g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                                   "-c", "user.name=t", *a], capture_output=True, text=True)
    def commit(path, text, msg):
        p = repo / path; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)
        g("add", path); g("commit", "-q", "-m", msg)
    (repo / ".silkworm").mkdir()
    (repo / ".silkworm" / "release.toml").write_text('''
[targets.backend]
paths = ["supabase/"]
ship = "command"
commands = ["echo deployed-backend > .deploy-log"]
preview = ["echo would-apply-0001"]

[targets.ios]
paths = ["ios/"]
ship = "tag"
version = { file = "ios/project.yml", key = "CFBundleShortVersionString" }
after = ["backend"]

[targets.web]
paths = ["web/"]
ship = "command"
commands = ["exit 3"]
''')
    commit(".silkworm/release.toml", (repo / ".silkworm/release.toml").read_text(), "config")
    commit("ios/project.yml", 'targets:\n  App:\n    CFBundleShortVersionString: "1.1.0"\n'
                              '  Widget:\n    CFBundleShortVersionString: "1.1.0"\n', "ios")
    commit("supabase/migrations/0001.sql", "select 1;\n", "migration")
    commit("web/index.html", "hi\n", "site")
    g("push", "-q", "origin", "main")

    targets = R.load(repo)
    check("targets are read from the project's own repo", sorted(targets) == ["backend", "ios", "web"])
    check("pending is per target, by path",
          [c.split(" ", 1)[1] for c in R.pending(repo, targets["ios"])] == ["ios"]
          and [c.split(" ", 1)[1] for c in R.pending(repo, targets["web"])] == ["site"])
    check("the next version bumps what the file says", R.fmt(R.next_version(repo, targets["ios"], "minor")) == "1.2.0")
    check("a target never released and unversioned starts at 1.0.0",
          R.fmt(R.next_version(repo, targets["backend"], "patch")) == "1.0.0")
    steps = R.plan(repo, ["ios"], "minor")
    check("releasing the app releases a pending backend first",
          [s["target"] for s in steps] == ["backend", "ios"], str(steps))
    check("and says what each carries", len(steps) == 2 and steps[1]["tag"] == "ios/v1.2.0"
          and steps[1]["commits"])

    r = R.release(repo, "backend")
    check("a command target deploys, then is tagged", r["released"] and r["tag"] == "backend/v1.0.0"
          and (repo / ".deploy-log").read_text().strip() == "deployed-backend", str(r.get("error")))
    remote_tags = subprocess.run(["git", "-C", str(origin), "tag"], capture_output=True, text=True).stdout.split()
    check("and the tag reached origin", "backend/v1.0.0" in remote_tags)
    check("after which it has nothing pending", R.pending(repo, targets["backend"]) == [])
    (repo / ".deploy-log").unlink()

    r = R.release(repo, "web")
    check("a failed deploy is not tagged: a tag always means it shipped",
          not r["released"] and "failed" in r.get("error", "")
          and not g("tag", "-l", "web/*").stdout.strip())

    r = R.release(repo, "ios", "minor")
    check("a tag target bumps every occurrence of its version",
          r["released"] and (repo / "ios/project.yml").read_text().count('"1.2.0"') == 2, str(r.get("error")))
    log_ = subprocess.run(["git", "-C", str(origin), "log", "-1", "--format=%s", "main"],
                          capture_output=True, text=True).stdout.strip()
    check("and pushes the bump and the tag together", log_ == "Release ios 1.2.0"
          and "ios/v1.2.0" in subprocess.run(["git", "-C", str(origin), "tag"],
                                             capture_output=True, text=True).stdout.split())
    try:
        R.release(repo, "ios")
        check("nothing pending, nothing released", False)
    except R.ReleaseError as e:
        check("nothing pending, nothing released", "nothing to release" in str(e))

    commit("ios/a.swift", "x\n", "more ios")
    (repo / "ios/a.swift").write_text("dirty\n")
    try:
        R.release(repo, "ios")
        check("never from a dirty checkout", False)
    except R.ReleaseError as e:
        check("never from a dirty checkout", "uncommitted" in str(e))
    g("checkout", "--", "ios/a.swift")
    other = root / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], capture_output=True)
    (other / "README").write_text("x\n")
    subprocess.run(["git", "-C", str(other), "-c", "user.email=t@t", "-c", "user.name=t", "add", "README"])
    subprocess.run(["git", "-C", str(other), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "elsewhere"])
    subprocess.run(["git", "-C", str(other), "push", "-q", "origin", "main"], capture_output=True)
    try:
        R.release(repo, "ios")
        check("never while behind origin", False)
    except R.ReleaseError as e:
        check("never while behind origin", "behind origin" in str(e))
    try:
        R.next_version(repo, targets["ios"], "1.0.0")
        check("a release never goes backwards", False)
    except R.ReleaseError:
        check("a release never goes backwards", True)
    check("the preview runs without releasing",
          R.preview(repo, "backend")[0]["output"].strip() == "would-apply-0001")

    bad = root / "bad"; (bad / ".silkworm").mkdir(parents=True)
    for cfg, why in (('[targets.a]\npaths=["a/"]\nship="tag"\nafter=["b"]\n[targets.b]\npaths=["b/"]\nship="tag"\nafter=["a"]\n', "a cycle"),
                     ('[targets.a]\npaths=["a/"]\nship="deploy"\n', "an unknown ship"),
                     ('[targets.a]\npaths=["a/"]\nship="tag"\nafter=["nope"]\n', "a missing dependency")):
        (bad / ".silkworm/release.toml").write_text(cfg)
        try:
            R.load(bad); check(f"a config with {why} is refused on reading", False)
        except R.ReleaseError:
            check(f"a config with {why} is refused on reading", True)


def test_release_command():
    import logging
    import contextlib
    import threading
    import releases as R
    print("\n!release: plan first, then release in order, stopping at a failure")
    root = Path(tempfile.mkdtemp())
    origin, repo = root / "origin.git", root / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], capture_output=True)
    g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                                   "-c", "user.name=t", *a], capture_output=True, text=True)
    def commit(path, text, msg):
        p = repo / path; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)
        g("add", path); g("commit", "-q", "-m", msg)
    commit(".silkworm/release.toml", '''
[targets.backend]
paths = ["supabase/"]
ship = "command"
commands = ["exit 7"]
[targets.ios]
paths = ["ios/"]
ship = "tag"
after = ["backend"]
''', "config")
    commit("supabase/0001.sql", "x\n", "migration")
    commit("ios/app.swift", "x\n", "app")
    g("push", "-q", "origin", "main")

    class Projects:
        def scope_for(self, slug):
            return {"cwd": str(repo)} if slug == "cadence" else {}
    import worktrees as W
    ns = {"releases": R, "project_store": Projects(), "worktrees": W, "threading": threading,
          "log": logging.getLogger("test"),
          "repo_guard": lambda cwd: contextlib.nullcontext(), "DRAIN": __import__("drain").Drain()}
    ns["board"] = __import__("board")
    lift({"release_command", "release_plan", "run_release", "_releasing", "_releasing_guard",
              "_board_releases", "release_checkout"}, ns=ns, absent=('RELEASE_STARTED',))
    cmd = ns["release_command"]
    check("no arguments: usage", "Usage" in cmd("", print))
    check("an unknown project is named", "no repository" in cmd("nope", print))
    check("a bad level is refused before anything runs", "not patch" in cmd("cadence ios sideways", print))

    posted = []
    done = ns["run_release"]("cadence", repo, "main", ["ios"], "minor", posted.append)
    check("a failing dependency stops the release before the app",
          not g("tag", "-l").stdout.strip() and any("backend" in p and "Stopping" in p for p in posted),
          str(posted))
    check("and the app was not tagged", not any("ios" in p and "released as" in p for p in posted))
    check("the release is marked finished even when it fails", not ns["_releasing"])

    ns["_releasing"].add("cadence")
    check("one release per project at a time", "already running" in cmd("cadence all", print))
    ns["_releasing"].discard("cadence")

    (repo / ".silkworm/release.toml").write_text((repo / ".silkworm/release.toml").read_text()
                                                  .replace('commands = ["exit 7"]', 'commands = ["true"]'))
    g("commit", "-qam", "fix the deploy"); g("push", "-q", "origin", "main")
    posted.clear()
    ns["run_release"]("cadence", repo, "main", ["ios"], "minor", posted.append)
    tags = g("tag", "-l").stdout.split()
    check("fixed, the backend goes first and then the app",
          tags == ["backend/v1.0.0", "ios/v1.0.0"] or sorted(tags) == ["backend/v1.0.0", "ios/v1.0.0"],
          f"{tags} {posted}")
    check("each one reported", sum("released as" in p for p in posted) == 2, str(posted))
    posted.clear()
    ns["run_release"]("cadence", repo, "main", ["ios"], "patch", posted.append)
    check("asked for something with nothing pending, it says so", any("nothing to release" in p for p in posted),
          str(posted))
    posted.clear()
    ns["release_plan"]("cadence", repo, posted.append)
    check("the plan says when there is nothing left", posted and "nothing since" in posted[0], str(posted))


def test_releases_ship_only_what_is_published():
    import os as _os
    import time as _t
    import releases as R
    print("\nreleases ship only what origin has, and a failure leaves nothing behind")
    root = Path(tempfile.mkdtemp())
    origin, repo = root / "origin.git", root / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], capture_output=True)
    g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                                   "-c", "user.name=t", *a], capture_output=True, text=True)
    def commit(path, text, msg):
        p = repo / path; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)
        g("add", path); g("commit", "-q", "-m", msg)
    secret_file = root / "creds.env"
    secret_file.write_text('export DEPLOY_TOKEN="tok_live_supersecret123" # a note\n')
    commit(".silkworm/release.toml", f'''
[targets.backend]
paths = ["supabase/"]
ship = "command"
env_files = ["{secret_file}"]
commands = ["echo token=$DEPLOY_TOKEN; echo slack=${{SLACK_BOT_TOKEN:-unset}}; exit 1"]
[targets.ios]
paths = ["ios/"]
ship = "tag"
version = {{ file = "ios/project.yml", key = "CFBundleShortVersionString" }}
''', "config")
    commit("ios/project.yml", 'CFBundleShortVersionString: "1.0.0"\n', "ios")
    commit("supabase/0001.sql", "x\n", "migration")
    g("push", "-q", "origin", "main")

    (repo / "supabase/stray.sql").write_text("drop table everything;\n")
    try:
        R.release(repo, "backend"); ok = False
    except R.ReleaseError as e:
        ok = "untracked" in str(e) or "uncommitted" in str(e)
    check("an untracked file in the checkout blocks a release", ok)
    (repo / "supabase/stray.sql").unlink()

    commit("ios/local.swift", "x\n", "not pushed")
    try:
        R.release(repo, "ios"); ok = False
    except R.ReleaseError as e:
        ok = "ahead" in str(e) or "push" in str(e)
    check("unpushed commits block a release: ship only what origin has", ok)
    g("push", "-q", "origin", "main")

    _os.environ["SLACK_BOT_TOKEN"] = "xoxb-" + "should-not-reach-a-deploy"
    try:
        r = R.release(repo, "backend")
    finally:
        _os.environ.pop("SLACK_BOT_TOKEN", None)
    out = r["steps"][0]["output"]
    check("a quoted value with a trailing comment is read correctly",
          "supersecret" not in out and "token=***" in out, out)
    check("the bot's own tokens do not reach a deploy", "slack=unset" in out, out)

    # A refused push must not leave a local tag or bump behind to trip the retry.
    subprocess.run(["git", "-C", str(origin), "config", "receive.denyNonFastForwards", "true"])
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n"); hook.chmod(0o755)
    head = g("rev-parse", "HEAD").stdout.strip()
    r = R.release(repo, "ios")
    check("a refused push is reported", not r["released"])
    check("and leaves no local tag", not g("tag", "-l", "ios/*").stdout.strip())
    check("and no local bump commit", g("rev-parse", "HEAD").stdout.strip() == head
          and 'CFBundleShortVersionString: "1.0.0"' in (repo / "ios/project.yml").read_text())
    hook.unlink()
    check("so the retry works", R.release(repo, "ios")["released"])

    # A command that outlives its timeout is killed, children and all. The
    # marker is unique to this run and matched exactly (never pgrep -f, which
    # matches any process whose command line merely mentions it), and the
    # release runs under a deadline: a kill that misses the child leaves it
    # holding the output pipe, and waiting on that would hang the suite
    # rather than fail it.
    import random as _r
    import threading as _th
    secs = str(_r.randint(300000, 399999))
    commit(".silkworm/release.toml", (repo / ".silkworm/release.toml").read_text()
           .replace('commands = ["echo token', f'commands = ["sleep {secs}; echo token'), "slow")
    g("push", "-q", "origin", "main")
    def sleepers():
        out = subprocess.run(["ps", "-ax", "-o", "pid=,command="], capture_output=True, text=True).stdout
        return [l.split(None, 1)[0] for l in out.splitlines()
                if l.split(None, 1)[1:] == [f"sleep {secs}"]]
    old = R.COMMAND_TIMEOUT_S
    R.COMMAND_TIMEOUT_S = 1
    box = {}
    worker = _th.Thread(target=lambda: box.setdefault("r", R.release(repo, "backend")), daemon=True)
    try:
        worker.start(); worker.join(20)
    finally:
        R.COMMAND_TIMEOUT_S = old
    _t.sleep(0.5)
    alive = sleepers()
    for pid in alive:
        subprocess.run(["kill", "-9", pid])
    check("a timed-out deploy is killed, not left running",
          not worker.is_alive() and not alive and not box.get("r", {}).get("released"),
          f"still running: {alive}" if alive else "the release never returned")

    steps_err = None
    try:
        R.plan(repo, None, "2.0.0")
    except R.ReleaseError as e:
        steps_err = str(e)
    check("an explicit version cannot apply to every target at once", bool(steps_err))


def test_releases_look_past_generated_files():
    import releases as R
    print("\nreleases: a base checkout holding only its generated files can still release")
    # Review of tsk_26cb94d8a6: Cadence's builds rewrite both Info.plists in
    # the base checkout, so `ready` refused every Cadence release exactly as
    # landings had refused before worktrees.changes learned about them.
    root = Path(tempfile.mkdtemp())
    origin, repo = root / "origin.git", root / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], capture_output=True)
    g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                                   "-c", "user.name=t", *a], capture_output=True, text=True)
    def commit(path, text, msg):
        p = repo / path; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)
        g("add", path); g("commit", "-q", "-m", msg)
    commit(".silkworm/release.toml", '''
[targets.ios]
paths = ["ios/"]
ship = "tag"
version = { file = "ios/project.yml", key = "CFBundleShortVersionString" }
''', "config")
    commit("ios/project.yml", 'CFBundleShortVersionString: "1.0.0"\n', "ios")
    commit("ios/Cadence/Info.plist", "<plist/>\n", "plist")
    commit("ios/a.swift", "x\n", "code")
    g("push", "-q", "origin", "main")
    gen = ["ios/Cadence/Info.plist"]
    plist = repo / "ios/Cadence/Info.plist"
    injected = "<plist>SUPABASE_KEY=sb_secret_" + "local</plist>\n"
    plist.write_text(injected)
    g("add", "ios/Cadence/Info.plist")        # staged, the worst case

    check("without the generated list the plist still blocks",
          "Info.plist" in R.ready(repo, "main", generated=[]))
    check("with it the checkout is ready", R.ready(repo, "main", generated=gen) == "",
          R.ready(repo, "main", generated=gen))
    (repo / "ios/new.swift").write_text("y\n")
    check("an untracked file still blocks", "new.swift" in R.ready(repo, "main", generated=gen))
    (repo / "ios/new.swift").unlink()

    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n"); hook.chmod(0o755)
    head = g("rev-parse", "HEAD").stdout.strip()
    r = R.release(repo, "ios", generated=gen)
    check("a refused push is undone", not r["released"]
          and g("rev-parse", "HEAD").stdout.strip() == head, r.get("error"))
    check("and the undo leaves the regenerated plist as the build wrote it",
          plist.read_text() == injected)
    hook.unlink()

    r = R.release(repo, "ios", generated=gen)
    check("the retry releases", r["released"], r.get("error"))
    shipped = g("show", "--name-only", "--format=", "HEAD").stdout.split()
    check("the bump commit holds only the version file", shipped == ["ios/project.yml"], shipped)
    check("the injected keys never reached origin",
          "sb_secret" not in subprocess.run(["git", "-C", str(origin), "show", "main:ios/Cadence/Info.plist"],
                                            capture_output=True, text=True).stdout)
    check("and the plist is still there for the next local build", plist.read_text() == injected)


# --- restart reports what is serving, not what launchctl accepted --------------
# `silkworm restart` printed "restarted" as soon as launchctl returned. A
# `silkworm status` 25s later then called both services unreachable while they
# were still binding -- and a service that never came up read exactly the same
# as one that had.

def test_restart_waits_for_ports():
    import importlib.machinery
    import importlib.util
    import socket
    import threading
    print("\nrestart waits until the services answer")

    loader = importlib.machinery.SourceFileLoader("silkworm_cli", str(BASE / "bin" / "silkworm"))
    spec = importlib.util.spec_from_loader("silkworm_cli", loader)
    cli = importlib.util.module_from_spec(spec)
    loader.exec_module(cli)

    def free_port():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return str(s.getsockname()[1])

    def listen_after(port, delay, hold):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        def run():
            time.sleep(delay)
            srv.bind(("127.0.0.1", int(port)))
            srv.listen()
            time.sleep(hold)
            srv.close()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    class Fake(cli.LaunchdManager):
        UP_TIMEOUT = 4.0
        def __init__(self, ports):
            self.ports = ports
        def _probes(self):
            return {f"com.silkworm.{n}": ((lambda p=p: cli.port_listening(p)), p)
                    for n, p in self.ports.items()}

    slow, never = free_port(), free_port()
    listen_after(slow, 1.5, 10)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        t0 = time.monotonic()
        ok = Fake({"bot": slow})._report_up("restarted")
        took = time.monotonic() - t0
    check("a service that binds late is waited for, not called down",
          ok and 1.0 < took < 4.0 and "answering on :" + slow in out.getvalue(),
          f"ok={ok} took={took:.1f}s {out.getvalue()!r}")

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ok = Fake({"bot": slow, "viz": never})._report_up("restarted")
    text = out.getvalue()
    check("one that never binds is reported failed, by name and port",
          not ok and "com.silkworm.viz restarted but not answering on :" + never in text
          and "viz.err.log" in text, text)
    check("and the one that did bind is still reported up",
          "com.silkworm.bot restarted, answering on :" + slow in text, text)

    # The old process must be gone before probing, or it answers for the new one.
    old = free_port()
    listen_after(old, 0, 1.5)
    while not cli.port_listening(old):
        time.sleep(0.05)
    t0 = time.monotonic()
    held = Fake({"bot": old})._wait_ports_free(timeout=5)
    took = time.monotonic() - t0
    check("probing waits for the old listener to let go",
          0.5 < took < 4.0 and held == [] and not cli.port_listening(old),
          f"took={took:.1f}s held={held}")

    # A holder that never lets go would answer the probe while the new copy
    # crash-loops on bind: that is a failed restart, not a successful one.
    stuck = free_port()
    listen_after(stuck, 0, 10)
    while not cli.port_listening(stuck):
        time.sleep(0.05)
    agents = Path(tempfile.mkdtemp())
    class Held(Fake):
        AGENTS = agents
        SERVICES = {"com.silkworm.bot": ["bot.py"]}
        started = []
        def _stop(self, label): pass
        def _free_ports(self): pass
        def _start(self, plist): self.started.append(plist.stem)
        def _wait_ports_free(self, timeout=15.0):
            return super()._wait_ports_free(timeout=1)
    (agents / "com.silkworm.bot.plist").write_text("")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ok = Held({"bot": stuck}).restart()
    check("a port still held after the wait fails the restart",
          ok is False and f":{stuck} is still held" in out.getvalue()
          and "answering" not in out.getvalue(), out.getvalue())
    check("the services are still started for launchd to retry",
          Held.started == ["com.silkworm.bot"])

    # The visualizer's readiness probe must be cheap: /api/stats re-reads
    # every transcript and took 1.3-1.5s live, against a 2s probe timeout.
    probes = cli.LaunchdManager()._probes()
    check("the visualizer is probed on a static route, not /api/stats",
          probes["com.silkworm.viz"][0] is cli.viz_serving
          and "/favicon.svg" in (BASE / "bin" / "silkworm").read_text()
          .split("def viz_serving", 1)[1].split("def ", 1)[0])
    # `silkworm status` probes the same way: /api/stats takes about two
    # seconds on the live sessions.json, against a two-second timeout, so a
    # status check on it reported a healthy dashboard down.
    import ast
    status = next(n for n in ast.walk(ast.parse((BASE / "bin" / "silkworm").read_text()))
                  if isinstance(n, ast.FunctionDef) and n.name == "do_status")
    probed = {a.func.id for c in ast.walk(status) if isinstance(c, ast.Call)
              and getattr(c.func, "id", None) == "check" and len(c.args) > 1
              for a in [c.args[1]] if isinstance(a, ast.Call) and isinstance(a.func, ast.Name)}
    check("silkworm status probes the visualizer with viz_serving",
          "viz_serving" in probed, sorted(probed))
    check("nothing probes /api/stats for liveness any more",
          '"/api/stats"' not in (BASE / "bin" / "silkworm").read_text())

    # And the CLI's exit status carries the result, so scripts can trust it.
    class Stub:
        def __init__(self, ok): self.ok = ok
        def restart(self): return self.ok
        def install(self): return self.ok
    real, argv = cli.service_manager, sys.argv
    try:
        for cmd in ("restart", "install"):
            for ok, want in ((True, 0), (False, 1)):
                cli.service_manager = lambda ok=ok: Stub(ok)
                sys.argv = ["silkworm", cmd]
                try:
                    cli.main()
                    code = 0
                except SystemExit as e:
                    code = e.code
                check(f"silkworm {cmd} exits {want} when services are {'up' if ok else 'down'}",
                      code == want, f"exit {code}")
    finally:
        cli.service_manager, sys.argv = real, argv


# `silkworm restart` waits for the visualizer to answer its probe. Off
# loopback the visualizer demands VIZ_TOKEN on every route, favicon included,
# and a specific-address bind refuses 127.0.0.1 -- so a probe that knocked on
# 127.0.0.1 without the token called a healthy visualizer down for 90s.
def test_viz_probe_off_loopback():
    import importlib.machinery
    import importlib.util
    import socket
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    import visualizer as V
    print("\nthe visualizer probe works when VIZ_BIND is not loopback")

    loader = importlib.machinery.SourceFileLoader("silkworm_cli", str(BASE / "bin" / "silkworm"))
    spec = importlib.util.spec_from_loader("silkworm_cli", loader)
    cli = importlib.util.module_from_spec(spec)
    loader.exec_module(cli)

    class V6(ThreadingHTTPServer):
        address_family = socket.AF_INET6

    def serve(cls, host):
        srv = cls((host, 0), V.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    saved = {k: os.environ.get(k) for k in ("VIZ_BIND", "VIZ_TOKEN", "SILKWORM_VIZ_PORT")}
    loopback, token, sessions = V.LOOPBACK, V.TOKEN, V.SESSIONS_FILE
    servers = []
    try:
        V.LOOPBACK, V.TOKEN = False, "s3cret-token"
        # No threads, so nothing here reads the live sessions.json.
        empty = Path(tempfile.mkdtemp()) / "sessions.json"
        empty.write_text("{}")
        V.SESSIONS_FILE = empty
        os.environ["VIZ_TOKEN"] = "s3cret-token"

        # A wildcard-style bind: reachable on 127.0.0.1, but every route 401s.
        srv = serve(ThreadingHTTPServer, "127.0.0.1"); servers.append(srv)
        os.environ["SILKWORM_VIZ_PORT"] = str(srv.server_address[1])
        os.environ["VIZ_BIND"] = "0.0.0.0"
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{srv.server_address[1]}/favicon.svg", timeout=2)
            unauth = 200
        except urllib.error.HTTPError as e:
            unauth = e.code
        check("the fixture really demands the token (bare GET is 401)", unauth == 401)
        check("viz_serving sends the token and sees it up", cli.viz_serving() is True)
        os.environ["VIZ_TOKEN"] = "wrong"
        check("a wrong token is not mistaken for up", cli.viz_serving() is False)
        os.environ["VIZ_TOKEN"] = "s3cret-token"

        class Waiter(cli.LaunchdManager):
            def _probes(self):
                return {"com.silkworm.viz": (cli.viz_serving, cli.viz_port())}

        # A specific-address bind: only that address answers. ::1 stands in
        # for a LAN address -- a host whose 127.0.0.1 refuses the port.
        try:
            srv6 = serve(V6, "::1"); servers.append(srv6)
        except OSError:
            srv6 = None
        check("the ::1 stand-in for a LAN address could bind", srv6 is not None)
        if srv6 is not None:
            port6 = str(srv6.server_address[1])
            os.environ["SILKWORM_VIZ_PORT"] = port6
            os.environ["VIZ_BIND"] = "::1"
            check("the fixture refuses 127.0.0.1", not cli.port_listening(port6))
            check("viz_serving knocks on the bound address", cli.viz_serving() is True)
            check("the port-free wait sees the bound address held",
                  Waiter()._wait_ports_free(timeout=1.0) == [port6])

        # The installed service never sees VIZ_BIND (the plist passes only
        # PATH; visualizer.py never loads .env), so a VIZ_BIND in .env or the
        # shell must not stop the probe finding it on loopback.
        V.LOOPBACK, V.TOKEN = True, ""
        lo = serve(ThreadingHTTPServer, "127.0.0.1"); servers.append(lo)
        os.environ["SILKWORM_VIZ_PORT"] = str(lo.server_address[1])
        os.environ["VIZ_BIND"] = "::1"
        check("a configured address the service never saw: still up on loopback",
              cli.viz_serving() is True)
        check("...and the port-free wait still sees loopback held",
              Waiter()._wait_ports_free(timeout=1.0) == [str(lo.server_address[1])])
    finally:
        V.LOOPBACK, V.TOKEN, V.SESSIONS_FILE = loopback, token, sessions
        for srv in servers:
            srv.shutdown(); srv.server_close()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ThreadingHTTPServer is IPv4-only: VIZ_BIND=::1 -- which LOOPBACK counts as
# loopback -- or any IPv6 address crashed the visualizer at startup with
# gaierror, and a crashed visualizer is one launchd keeps respawning.
def test_viz_binds_ipv6():
    import socket
    import threading
    import urllib.request
    import visualizer as V
    print("\nthe visualizer can bind an IPv6 VIZ_BIND")

    def answers(host, port):
        netloc = f"[{host}]" if ":" in host else host
        try:
            with urllib.request.urlopen(f"http://{netloc}:{port}/favicon.svg", timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    loopback, token, getfqdn = V.LOOPBACK, V.TOKEN, socket.getfqdn
    servers, started = [], []
    try:
        V.LOOPBACK, V.TOKEN = True, ""
        # HTTPServer.server_bind's getfqdn took 35s here on a stalled
        # resolver; make_server must not wait on DNS to start.
        def no_dns(*a):
            raise AssertionError("make_server called getfqdn")
        socket.getfqdn = no_dns
        for bind in ("::1", "::"):
            try:
                srv = V.make_server(bind, 0); servers.append(srv)
            except OSError as e:
                check(f"make_server binds {bind}", False, repr(e))
                continue
            check(f"make_server binds {bind}", True)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            started.append(srv)
            port = srv.server_address[1]
            check(f"{bind}: answers on [::1]", answers("::1", port))
            if bind == "::":
                # bin/silkworm probes a wildcard bind on 127.0.0.1 only.
                check(":: also answers IPv4 127.0.0.1 (V6ONLY cleared)",
                      answers("127.0.0.1", port))
                check(":: has IPV6_V6ONLY off",
                      srv.socket.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 0)
            else:
                check("::1 stays v6-only (refuses 127.0.0.1)",
                      not answers("127.0.0.1", port))
        srv = V.make_server("127.0.0.1", 0); servers.append(srv)
        check("an IPv4 bind keeps a plain AF_INET server",
              srv.address_family == socket.AF_INET)
    except AssertionError as e:
        check("make_server starts without a DNS lookup", False, str(e))
    finally:
        V.LOOPBACK, V.TOKEN, socket.getfqdn = loopback, token, getfqdn
        for srv in started:
            srv.shutdown()
        for srv in servers:
            srv.server_close()
    cli = _load_cli()
    saved = os.environ.get("VIZ_BIND")
    try:
        os.environ["VIZ_BIND"] = "::1"
        check("silkworm shows an IPv6 VIZ_BIND bracketed", cli.viz_host() == "[::1]")
        os.environ["VIZ_BIND"] = "10.0.0.5"
        check("and an IPv4 one bare", cli.viz_host() == "10.0.0.5")
    finally:
        if saved is None:
            os.environ.pop("VIZ_BIND", None)
        else:
            os.environ["VIZ_BIND"] = saved
    check("__main__ builds its server through make_server",
          "server = make_server(BIND, PORT)" in (BASE / "visualizer.py").read_text())


# --- deploy drains instead of killing --------------------------------------------
# The bot restarted 13 times in 10 days (2026-09-18..10-01) to load new code.
# Every restart killed the turn running at the time -- nine runs ended exit
# 143 -- and an interrupted queue task started over, re-spending its session.


def test_drain_lapses_and_holds_the_runner():
    import drain as D
    import retry as R
    import tasks as T
    from tasks import TaskStore
    print("\ndraining stops the runner claiming, and lapses on its deadline")

    d = D.Drain()
    check("a new drain is not draining", d.remaining() == 0)
    t0 = 1_000_000.0
    d.start(600, now=t0)
    check("a drain holds until its deadline",
          d.remaining(now=t0 + 599) > 0 and d.remaining(now=t0 + 601) == 0,
          f"{d.remaining(now=t0 + 599)} / {d.remaining(now=t0 + 601)}")
    d.start(10 * 86400, now=t0)
    check("however long it is asked for, it is capped",
          d.remaining(now=t0 + D.CAP_S + 1) == 0)
    d.start(600)
    check("stop lifts it", d.stop() and d.remaining() == 0)

    # The real worker, over a real store, held by a real drain.
    class Idle(BaseException):
        pass
    root = Path(tempfile.mkdtemp())
    st = TaskStore(root / "t.json")
    tid = st.create("queued work", driver="queue", source="ui",
                    scope={"cwd": str(root)})["id"]
    ran = []
    def idle(_s):
        raise Idle()
    dr = D.Drain()
    worker = _bot_func("_task_worker", task_store=st,
                       execute_task=lambda task: ran.append(task["id"]),
                       hold_unsupervised=lambda task: False, lane_filter=lambda lane: {},
                       RUNNER_HOLD=R.Hold(), DRAIN=dr, TASK_POLL_S=5,
                       log=logging.getLogger("test"),
                       time=types.SimpleNamespace(sleep=idle, time=time.time))
    def one_pass():
        try:
            worker(0)
        except Idle:
            pass
    dr.start(3600)
    one_pass()
    check("the runner claims nothing while draining",
          not ran and st.get(tid)["state"] == T.QUEUED, f"{ran} {st.get(tid)['state']}")
    # A deploy that died part-way: its drain's time is up, nobody stopped it.
    dr.start(60, now=time.time() - 120)
    one_pass()
    check("and claims again once the drain's deadline passes, unstopped",
          ran == [tid], str(ran))

    # The route: what a restart would kill, any driver, plus live landings.
    for title, driver in (("a slack turn", "inline"), ("a queued task", "queue")):
        x = st.create(title, driver=driver, source="ui", scope={"cwd": str(root)})
        st.transition(x["id"], T.RUNNING, "go")
    st.create("still queued", driver="queue", source="ui", scope={"cwd": str(root)})
    dr2 = D.Drain()
    handle = _bot_func("handle_drain", DRAIN=dr2, drain=D, task_store=st,
                       _landing_now={"tsk_landing"}, RUNNING_TASKS={},
                       REVISION={"sha": "abc"})
    r = handle({"action": "start", "seconds": 900})
    titles = sorted(row["title"] for row in r["running"])
    check("/drain start drains, with the deadline asked for",
          r["ok"] and r["draining"] and 0 < dr2.remaining() <= 900, str(r)[:200])
    check("every running task counts, Slack conversations included",
          {"a slack turn", "a queued task"} <= set(titles) and "still queued" not in titles,
          str(titles))
    check("a landing in flight counts too",
          any(row["kind"] == "landing" and row["id"] == "tsk_landing" for row in r["running"]))
    check("a running record no child holds is marked, not hidden",
          all(row["live"] is False for row in r["running"] if row["kind"] == "task"))
    r = handle({"action": "stop"})
    check("/drain stop lifts it", r["ok"] and not r["draining"] and dr2.remaining() == 0)
    check("an unknown action is refused, not read as status",
          handle({"action": "pause"})["ok"] is False)


class _FakeBot:
    """The bot's HTTP API as `silkworm deploy` sees it, on a fake clock."""

    def __init__(self, clock, running_until=0.0, head="h" * 40, drain_ok=True,
                 connected=True, boots=None):
        self.clock, self.running_until = clock, running_until
        self.head, self.drain_ok, self.connected = head, drain_ok, connected
        self.boots = boots or head
        self.draining, self.drain_calls, self.restarted_at = False, [], None
        self.gaps = []          # (from, to) windows with nothing running

    def running(self):
        t = self.clock()
        if self.restarted_at is not None:
            return []
        if any(a <= t < b for a, b in self.gaps):
            return []
        if t < self.running_until:
            return [{"id": "tsk_busy", "kind": "task", "title": "long turn",
                     "driver": "queue", "live": True}]
        return []

    def __call__(self, path, payload):
        if path == "/status":
            if self.restarted_at is not None:
                return {"online": True, "slack": {"connected": self.connected},
                        "revision": {"started": self.boots}}
            return {"online": True, "slack": {"connected": True},
                    "revision": {"started": "old"}}
        if path == "/drain":
            if not self.drain_ok:
                return {}
            self.drain_calls.append(dict(payload))
            if payload.get("action") == "start":
                self.draining = True
            elif payload.get("action") == "stop":
                self.draining = False
            return {"ok": True, "draining": self.draining, "running": self.running()}
        return {}


def _deploy(cli, argv, bot, clock, restarts, *, git=None, suite=lambda: 0):
    class Mgr:
        def restart(self):
            restarts.append(clock())
            bot.restarted_at = clock()
            return True
    def fake_git(repo, *args):
        if args[:1] == ("rev-parse",) and "--abbrev-ref" in args:
            return 0, "main"
        if args[:1] == ("rev-parse",):
            return 0, bot.head
        if args[:1] == ("status",):
            return 0, ""
        return 1, ""
    t = [0.0]
    def sleep(s):
        t[0] += s
        # A wait that never ends must fail the test, not hang the suite.
        if t[0] > 86400:
            raise RuntimeError("deploy waited a simulated day without ending")
    clock.__dict__["t"] = t
    out = io.StringIO()
    saved = os.environ.pop("SILKWORM_THREAD", None)
    try:
        with contextlib.redirect_stdout(out):
            code = cli.do_deploy(argv, repo=Path("/nonexistent"), call=bot,
                                 manager=Mgr(), git=git or fake_git, suite=suite,
                                 clock=lambda: t[0], sleep=sleep, confirm_s=10)
    finally:
        if saved is not None:
            os.environ["SILKWORM_THREAD"] = saved
    return code, out.getvalue(), t


def test_deploy_waits_then_restarts_onto_head():
    print("\nsilkworm deploy drains, waits for quiet, restarts, confirms")
    cli = _load_cli()

    class Clock:
        def __call__(self):
            return self.t[0]
    clock = Clock(); clock.t = [0.0]

    # Busy for ten minutes, then quiet.
    restarts = []
    bot = _FakeBot(clock, running_until=600)
    code, out, t = _deploy(cli, [], bot, clock, restarts)
    check("a deploy waits out the running turn, then restarts and succeeds",
          code == 0 and len(restarts) == 1, f"code={code} restarts={restarts}\n{out}")
    check("it restarts only after 30s with nothing running",
          restarts and restarts[0] >= 600 + cli.DEPLOY_QUIET_S, str(restarts))
    starts = [c for c in bot.drain_calls if c.get("action") == "start"]
    check("the drain asked for outlasts the wait, and is bounded",
          starts and cli.DEPLOY_MAX_WAIT_S < starts[0]["seconds"]
          <= cli.DEPLOY_MAX_WAIT_S + cli.DEPLOY_DRAIN_MARGIN_S, str(starts))
    check("it says what it is waiting on while it waits",
          "waiting on 1 running" in out and "tsk_busy" in out, out)
    check("each confirmation step is reported",
          "connected to slack" in out and "the checkout's HEAD" in out, out)

    # A gap shorter than the quiet window is not quiet.
    restarts = []
    bot = _FakeBot(clock, running_until=600)
    bot.gaps = [(100, 120)]
    _deploy(cli, [], bot, clock, restarts)
    check("a 20s lull between turns does not count as quiet",
          restarts and restarts[0] >= 600 + cli.DEPLOY_QUIET_S, str(restarts))

    # Never quiet: give up, do not restart, lift the drain, say what was running.
    restarts = []
    bot = _FakeBot(clock, running_until=10 ** 9)
    code, out, t = _deploy(cli, ["--max-wait", "10m"], bot, clock, restarts)
    check("still busy at --max-wait: no restart, and a failing exit",
          code != 0 and not restarts, f"code={code} restarts={restarts}")
    check("it gives up at the max wait, not before or long after",
          600 <= t[0] < 700, f"{t[0]}")
    check("the drain is lifted when it gives up",
          bot.drain_calls[-1].get("action") == "stop" and not bot.draining,
          str(bot.drain_calls[-1:]))
    check("and it names what was still running",
          "still busy" in out and "tsk_busy" in out.split("still busy", 1)[1], out)

    # --now: no wait.
    restarts = []
    bot = _FakeBot(clock, running_until=10 ** 9)
    code, out, t = _deploy(cli, ["--now"], bot, clock, restarts)
    check("--now restarts without waiting, naming what it interrupts",
          code == 0 and restarts == [0.0] and "tsk_busy" in out, f"{code} {restarts}\n{out}")

    # Confirmation is checked, not assumed.
    for label, kw in (("on another revision", {"boots": "b" * 40}),
                      ("not connected to Slack", {"connected": False})):
        restarts = []
        bot = _FakeBot(clock, **kw)
        code, out, t = _deploy(cli, [], bot, clock, restarts)
        check(f"a bot that comes back {label} fails the deploy",
              code != 0 and restarts, f"code={code}\n{out}")

    # A restart that fails can leave the old bot running: it must not stay
    # drained for the next two hours.
    bot = _FakeBot(clock)
    class Broken:
        def restart(self):
            return False
    t = [0.0]
    saved = os.environ.pop("SILKWORM_THREAD", None)
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.do_deploy(["--now"], repo=Path("/nonexistent"), call=bot,
                                 manager=Broken(), suite=lambda: 0,
                                 git=lambda repo, *a: ((0, "main") if "--abbrev-ref" in a else
                                                       (0, bot.head) if a[0] == "rev-parse" else
                                                       (0, "") if a[0] == "status" else (1, "")),
                                 clock=lambda: t[0],
                                 sleep=lambda s: t.__setitem__(0, t[0] + s))
    finally:
        if saved is not None:
            os.environ["SILKWORM_THREAD"] = saved
    check("a failed restart lifts the drain and fails the deploy",
          code != 0 and bot.drain_calls[-1].get("action") == "stop" and not bot.draining,
          f"{code} {bot.drain_calls}\n{out.getvalue()}")

    # A bot too old to drain is not quietly restarted under running work.
    restarts = []
    bot = _FakeBot(clock, running_until=10 ** 9, drain_ok=False)
    code, out, t = _deploy(cli, [], bot, clock, restarts)
    check("a bot that cannot drain is refused without --now",
          code != 0 and not restarts and "--now" in out, out)

    # From inside a turn it would wait on itself and then kill itself. The
    # same deploy outside a turn goes ahead, so the refusal is the turn's doing.
    saved = os.environ.get("SILKWORM_THREAD")
    try:
        for inside in (False, True):
            restarts = []
            bot = _FakeBot(clock)
            os.environ.pop("SILKWORM_THREAD", None)
            if inside:
                os.environ["SILKWORM_THREAD"] = "C1:1.0"
            class Mgr:
                def restart(self):
                    restarts.append(1)
                    bot.restarted_at = 0
                    return True
            def fake_git(repo, *args):
                if "--abbrev-ref" in args:
                    return 0, "main"
                return (0, bot.head) if args[:1] == ("rev-parse",) else (
                    (0, "") if args[:1] == ("status",) else (1, ""))
            out = io.StringIO()
            t = [0.0]
            with contextlib.redirect_stdout(out):
                code = cli.do_deploy([], repo=Path("/nonexistent"), call=bot,
                                     manager=Mgr(), suite=lambda: 0, git=fake_git,
                                     clock=lambda: t[0],
                                     sleep=lambda s: t.__setitem__(0, t[0] + s),
                                     confirm_s=10)
            if inside:
                check("deploy refuses to run from inside a Silkworm turn",
                      code != 0 and not bot.drain_calls and not restarts, out.getvalue())
            else:
                check("(the same deploy outside a turn goes ahead)",
                      code == 0 and restarts, out.getvalue())
    finally:
        if saved is None:
            os.environ.pop("SILKWORM_THREAD", None)
        else:
            os.environ["SILKWORM_THREAD"] = saved

    # And the CLI dispatches it.
    real, argv = cli.do_deploy, sys.argv
    try:
        cli.do_deploy = lambda a: 7 if a == ["--now"] else 0
        sys.argv = ["silkworm", "deploy", "--now"]
        try:
            cli.main(); code = 0
        except SystemExit as e:
            code = e.code
        check("`silkworm deploy` exits with do_deploy's status, args after the subcommand",
              code == 7, f"exit {code}")
    finally:
        cli.do_deploy, sys.argv = real, argv


def test_deploy_preflight_refuses_unfit_checkouts():
    print("\nsilkworm deploy refuses a checkout it should not become")
    cli = _load_cli()
    root = Path(tempfile.mkdtemp())
    repo = root / "repo"; repo.mkdir()
    def git(*a): return subprocess.run(["git", *a], cwd=str(repo),
                                       capture_output=True, text=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (repo / "a.txt").write_text("a\n")
    git("add", "-A"); git("commit", "-qm", "base")

    ran = []
    def suite(rc):
        def run():
            ran.append(rc)
            return rc
        return run
    def pre(skip, rc):
        with contextlib.redirect_stdout(io.StringIO()):
            return cli.deploy_preflight(repo, skip, suite=suite(rc))

    ok = pre(False, 0)
    check("a clean main that passes its tests may deploy", ok == [] and ran == [0], str(ok))
    (repo / "loose.txt").write_text("untracked is fine\n")
    check("untracked files do not count as uncommitted", pre(True, 0) == [])

    ran.clear()
    bad = pre(False, 1)
    check("a failing suite refuses", any("test suite fails" in p for p in bad), str(bad))
    ran.clear()
    check("--skip-tests does not run it", pre(True, 1) == [] and ran == [])

    (repo / "a.txt").write_text("edited\n")
    bad = pre(True, 0)
    check("uncommitted tracked changes refuse", any("uncommitted" in p for p in bad), str(bad))
    git("checkout", "-q", "--", "a.txt")

    git("checkout", "-qb", "feature")
    bad = pre(True, 0)
    check("a checkout off its base branch refuses",
          any("not main" in p for p in bad), str(bad))
    ran.clear()
    pre(False, 0)
    check("and does not spend the suite on a refusal", ran == [])

    # The full command stops at preflight: nothing drained, nothing restarted.
    calls = []
    saved = os.environ.pop("SILKWORM_THREAD", None)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.do_deploy([], repo=repo, call=lambda p, b: calls.append(p) or {},
                                 manager=types.SimpleNamespace(
                                     restart=lambda: calls.append("restart")),
                                 suite=suite(0))
    finally:
        if saved is not None:
            os.environ["SILKWORM_THREAD"] = saved
    check("a refused deploy touches neither the bot nor the services",
          code != 0 and calls == [], str(calls))

    check("durations parse as the usage says",
          (cli.parse_duration("90"), cli.parse_duration("15m"), cli.parse_duration("2h"))
          == (90, 900, 7200))


# --- deploy --after-turn: a turn deploys without waiting on or killing itself -----
# Until this, a turn that wanted to deploy hand-rolled a double fork with the
# SILKWORM_* variables stripped. Plain `deploy` from a turn waited on itself
# and the restart then killed the turn running it.

def _after_turn_git(head):
    def git(repo, *args):
        if "--abbrev-ref" in args:
            return 0, "main"
        if args[:1] == ("rev-parse",):
            return 0, head[0] if isinstance(head, list) else head
        if args[:1] == ("status",):
            return 0, ""
        return 1, ""
    return git


def _in_turn_env(thread="C1:1.0", task="tsk_me"):
    saved = {k: os.environ.get(k) for k in
             ("SILKWORM_THREAD", "SILKWORM_TASK_ID", "SILKWORM_ROLE", "SILKWORM_TASK_ROLE")}
    os.environ["SILKWORM_THREAD"] = thread
    os.environ["SILKWORM_ROLE"] = "assistant"
    if task:
        os.environ["SILKWORM_TASK_ID"] = task
        os.environ["SILKWORM_TASK_ROLE"] = "implementor"
    else:
        os.environ.pop("SILKWORM_TASK_ID", None)
    def restore():
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return restore


def test_deploy_after_turn_checks_in_the_foreground_and_hands_off_once():
    print("\nsilkworm deploy --after-turn: checks now, hands off, one deploy at a time")
    cli = _load_cli()
    root = Path(tempfile.mkdtemp())
    lock = root / "deploy.lock"
    log_ = root / "deploy.log"
    head = ["a" * 40]
    handed, calls = [], []
    live = {4242}

    def handoff(argv, env, log_path):
        handed.append((list(argv), dict(env), log_path))
        return 4242

    def run(argv, suite=lambda: 0):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.do_deploy(argv, repo=Path("/nonexistent"),
                                 call=lambda p, b: calls.append(p) or {},
                                 manager=types.SimpleNamespace(
                                     restart=lambda: calls.append("restart")),
                                 git=_after_turn_git(head), suite=suite,
                                 handoff=handoff, lock_path=lock, log_path=log_,
                                 alive=lambda pid: pid in live)
        return code, out.getvalue()

    restore = _in_turn_env()
    try:
        # Plain deploy still refuses in a turn, and says what to use instead.
        code, out = run([])
        check("plain deploy refuses inside a turn, pointing at --after-turn",
              code == 1 and "--after-turn" in out and not calls and not handed, out)
        code, out = run(["--now", "--after-turn"])
        check("--now with --after-turn is a usage error", code == 2 and not handed, out)

        # Preflight runs here and its failure is this command's failure.
        code, out = run(["--after-turn"], suite=lambda: 1)
        check("a failing suite fails --after-turn at once, non-zero, nothing handed off",
              code == 1 and "test suite fails" in out and not handed and not calls, out)
        check("and a refused check does not leave the one-deploy lock held",
              not lock.exists())

        code, out = run(["--after-turn"])
        check("a passing check hands off and exits 0",
              code == 0 and len(handed) == 1, f"{code} {handed}\n{out}")
        check("it touches neither the bot nor the services itself",
              calls == [], str(calls))
        argv, env, lp = handed[0]
        check("the handoff is told the HEAD, the thread and the task to wait for",
              cli.AFTER_TURN_CMD in argv and argv[argv.index("--head") + 1] == head[0]
              and argv[argv.index("--thread") + 1] == "C1:1.0"
              and argv[argv.index("--task") + 1] == "tsk_me", str(argv))
        check("and gets none of the turn's SILKWORM_* variables",
              not [k for k in env if k.startswith("SILKWORM_")
                   and k not in ("SILKWORM_BOT_PORT", "SILKWORM_VIZ_PORT")],
              str(sorted(k for k in env if k.startswith("SILKWORM_"))))
        check("it logs to the known file", lp == log_ and str(log_) in out, out)
        held = json.loads(lock.read_text())
        check("the lock names the detached process, not the CLI that exits",
              held.get("pid") == 4242 and held.get("head") == head[0], str(held))

        # One at a time.
        code, out = run(["--after-turn"])
        check("a second --after-turn while one is pending reports it, starts nothing",
              code == 0 and len(handed) == 1 and "already pending" in out
              and "4242" in out, out)
        head[0] = "b" * 40
        code, out = run(["--after-turn"])
        check("pending for an older HEAD: says so and fails, still starts nothing",
              code == 1 and len(handed) == 1 and "aaaaaaaa" in out, out)

        # A lock whose process is gone is a deploy that died: taken over.
        live.clear()
        code, out = run(["--after-turn"])
        check("a stale lock does not block the next deploy",
              code == 0 and len(handed) == 2, f"{code} {len(handed)}\n{out}")
    finally:
        restore()


def test_deploy_after_turn_handoff_is_nobodys_child():
    print("\nthe --after-turn handoff is reparented to PID 1, in its own session")
    cli = _load_cli()
    root = Path(tempfile.mkdtemp())
    report, log_ = root / "report.json", root / "handoff.log"
    script = (
        "import json, os, sys, time\n"
        "eof = os.read(0, 1) == b''\n"
        "null = os.fstat(0).st_rdev == os.stat(os.devnull).st_rdev\n"
        "print('hello from the handoff', flush=True)\n"
        f"open({str(report)!r} + '.tmp', 'w').write(json.dumps({{"
        "'pid': os.getpid(), 'ppid': os.getppid(), 'sid': os.getsid(0),"
        "'pgid': os.getpgid(0), 'eof': eof, 'null': null,"
        "'thread': os.environ.get('SILKWORM_THREAD'),"
        "'port': os.environ.get('SILKWORM_BOT_PORT')}))\n"
        f"os.replace({str(report)!r} + '.tmp', {str(report)!r})\n"
        "time.sleep(30)\n")
    env = cli.handoff_env({**os.environ, "SILKWORM_THREAD": "C1:1.0",
                           "SILKWORM_TASK_ID": "tsk_me", "SILKWORM_BOT_PORT": "9999"})
    pid = cli.detach([sys.executable, "-c", script], env, log_, cwd=root)
    try:
        deadline = time.time() + 15
        while not report.exists() and time.time() < deadline:
            time.sleep(0.1)
        got = json.loads(report.read_text()) if report.exists() else {}
        check("detach returns the pid of the process that runs", got.get("pid") == pid,
              f"{pid} {got}")
        check("its parent is PID 1, not this process", got.get("ppid") == 1, str(got))
        # Walk its ancestry as the OS sees it now: this process is not in it.
        chain, p = [], pid
        while p > 1 and len(chain) < 50:
            r = subprocess.run(["ps", "-o", "ppid=", "-p", str(p)],
                               capture_output=True, text=True)
            p = int(r.stdout.strip() or 1)
            chain.append(p)
        check("the caller is not among its ancestors",
              os.getpid() not in chain and chain[:1] == [1], str(chain))
        # The session is the one setsid made for the first child, which has
        # exited -- so the process that runs leads nothing and can never take
        # a controlling terminal.
        check("it runs in a session of its own, outside the caller's process group",
              got.get("sid") not in (None, os.getsid(0), pid)
              and got.get("pgid") == got.get("sid") and got.get("pgid") != os.getpgid(0),
              str(got))
        check("its stdin is /dev/null, closed to the caller",
              got.get("eof") and got.get("null"), str(got))
        check("the turn's variables are gone, the bot's port kept",
              got.get("thread") is None and got.get("port") == "9999", str(got))
        check("its output goes to the log",
              "hello from the handoff" in log_.read_text(), log_.read_text())
    finally:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


class _TurnBot(_FakeBot):
    """_FakeBot with rows chosen per moment, a /tell route, and a bot that
    can fail to come back."""

    def __init__(self, clock, rows, *, tell_ok=True, down_after=False, **kw):
        super().__init__(clock, **kw)
        self.rows, self.tell_ok, self.down_after = rows, tell_ok, down_after
        self.told = []

    def running(self):
        if self.restarted_at is not None:
            return []
        return self.rows(self.clock())

    def __call__(self, path, payload):
        if self.down_after and self.restarted_at is not None:
            return {}
        if path == "/tell":
            self.told.append(dict(payload))
            return {"ok": True} if self.tell_ok else {}
        return super().__call__(path, payload)


def _after_turn(cli, bot, clock, *, head="h" * 40, thread="C1:1.0", task="tsk_me",
                max_wait=600, manager=None, outbox=None, caller_s=None, git=None):
    root = Path(tempfile.mkdtemp())
    lock = root / "deploy.lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "head": head}))
    restarts = []
    class Mgr:
        def restart(self):
            restarts.append(clock.t[0])
            bot.restarted_at = clock.t[0]
            return True
    t = clock.t
    def sleep(s):
        t[0] += s
        if t[0] > 2 * 86400:
            raise RuntimeError("the after-turn deploy waited two simulated days")
    out = io.StringIO()
    kw = {} if caller_s is None else {"caller_s": caller_s}
    with contextlib.redirect_stdout(out):
        code = cli.after_turn_run(head, thread, task, max_wait, call=bot,
                                  manager=manager or Mgr(), lock_path=lock,
                                  log_path=root / "deploy.log",
                                  outbox=outbox or root / "outbox",
                                  clock=lambda: t[0], sleep=sleep, confirm_s=10,
                                  repo=Path("/nonexistent"),
                                  git=git or _after_turn_git(head), **kw)
    return code, out.getvalue(), restarts, lock, root


def test_deploy_after_turn_waits_for_its_own_turn_then_deploys():
    print("\nthe detached deploy waits for the turn that asked, then for quiet")
    cli = _load_cli()

    class Clock:
        def __call__(self):
            return self.t[0]
    def fresh():
        c = Clock(); c.t = [0.0]
        return c

    me = lambda live=True: {"id": "tsk_me", "kind": "task", "title": "the asking turn",
                            "thread": "C1:1.0", "driver": "queue", "live": live}
    # The asking turn runs three hours -- far past --max-wait (10m). Its time
    # is not other work's: the deploy still happens, once it ends.
    clock = fresh()
    bot = _TurnBot(clock, lambda t: [me()] if t < 3 * 3600 else [])
    code, out, restarts, lock, _ = _after_turn(cli, bot, clock)
    check("a long asking turn is waited out, not counted against --max-wait",
          code == 0 and restarts and restarts[0] >= 3 * 3600 + cli.DEPLOY_QUIET_S,
          f"{code} {restarts}\n{out[-600:]}")
    starts = [c for c in bot.drain_calls if c.get("action") == "start"]
    check("it drains only once the asking turn has ended",
          starts and all(c.get("action") == "status" for c in
                         bot.drain_calls[:bot.drain_calls.index(starts[0])]), str(bot.drain_calls[:3]))
    check("success is posted to the asking thread with the revision",
          bot.told and bot.told[-1]["key"] == "C1:1.0" and "h" * 8 in bot.told[-1]["text"]
          and "Deployed" in bot.told[-1]["text"], str(bot.told))
    check("and the one-deploy lock is let go", not lock.exists())

    # Its process has gone but its record still says running (the bot catching
    # up, or an orphan): that must not hold the deploy forever.
    clock = fresh()
    bot = _TurnBot(clock, lambda t: [me(live=t < 120)])
    code, out, restarts, _, _ = _after_turn(cli, bot, clock)
    check("a record no live process holds is not waited on forever",
          code == 0 and restarts and restarts[0] < 600, f"{code} {restarts}\n{out[-600:]}")

    # Inline Slack turns have no task id in their environment: found by thread.
    clock = fresh()
    bot = _TurnBot(clock, lambda t: [{**me(), "id": "tsk_inline"}] if t < 900 else [])
    code, out, restarts, _, _ = _after_turn(cli, bot, clock, task="")
    check("an asking turn is found by its thread when it has no task id",
          code == 0 and restarts and restarts[0] >= 900 + cli.DEPLOY_QUIET_S, f"{restarts}")

    # Other work is still waited on as any deploy would, including a later turn
    # on the same thread -- that is not the asking turn.
    other = {"id": "tsk_other", "kind": "task", "title": "someone else's turn",
             "thread": "C1:1.0", "driver": "inline", "live": True}
    clock = fresh()
    bot = _TurnBot(clock, lambda t: ([me()] if t < 60 else [])
                   + ([other] if 70 <= t < 1200 else []))
    code, out, restarts, _, _ = _after_turn(cli, bot, clock, max_wait=3600)
    check("a later turn on the same thread is other work, waited on",
          code == 0 and restarts and restarts[0] >= 1200 + cli.DEPLOY_QUIET_S, f"{restarts}")

    # The asking turn that never ends: bounded, said, nothing restarted.
    clock = fresh()
    bot = _TurnBot(clock, lambda t: [me()])
    code, out, restarts, lock, _ = _after_turn(cli, bot, clock, caller_s=3600)
    check("an asking turn that never ends is given up on, not restarted under",
          code == 1 and not restarts and bot.told and ":x:" in bot.told[-1]["text"]
          and "calling turn" in bot.told[-1]["text"], f"{code} {restarts} {bot.told}")
    check("(and the lock is still let go)", not lock.exists())


def test_deploy_after_turn_failure_is_reported_not_silent():
    print("\nthe detached deploy says when it could not confirm, wherever it can")
    cli = _load_cli()

    class Clock:
        def __call__(self):
            return self.t[0]
    def fresh():
        c = Clock(); c.t = [0.0]
        return c
    gone = lambda t: []

    # Comes back on another revision: the deploy did not do what was asked.
    clock = fresh()
    bot = _TurnBot(clock, gone, boots="b" * 40)
    code, out, restarts, lock, root = _after_turn(cli, bot, clock)
    told = bot.told[-1]["text"] if bot.told else ""
    check("a restart onto another revision fails, and the thread is told why",
          code == 1 and restarts and ":x:" in told and "bbbbbbbb" in told
          and "hhhhhhhh" in told, f"{code} {told}\n{out[-400:]}")
    check("the failure names the log", str(root / "deploy.log") in told, told)
    check("it never claims success", "Deployed" not in told)

    # Never comes back: no bot to post through, so the thread's outbox gets it.
    clock = fresh()
    outbox = Path(tempfile.mkdtemp())
    bot = _TurnBot(clock, gone, down_after=True)
    code, out, restarts, lock, root = _after_turn(cli, bot, clock, outbox=outbox)
    notes = list((outbox / "C1__1.0").glob("deploy-*.txt"))
    text = notes[0].read_text() if notes else ""
    check("a bot that never returns: the failure is left in the thread's outbox",
          code == 1 and notes and "never answered" in text
          and str(root / "deploy.log") in text, f"{code} {notes}\n{out[-400:]}")
    check("(and the lock is let go)", not lock.exists())

    # A crash inside the deploy is a failure, said, not a silent exit.
    clock = fresh()
    bot = _TurnBot(clock, gone)
    def boom():
        raise OSError("launchctl went away")
    code, out, restarts, lock, _ = _after_turn(
        cli, bot, clock, manager=types.SimpleNamespace(restart=boom))
    told = bot.told[-1]["text"] if bot.told else ""
    check("an exception mid-deploy is reported with its reason",
          code == 1 and "launchctl went away" in told and not lock.exists(), told)

    # Still busy at --max-wait: reported with what was running.
    clock = fresh()
    bot = _TurnBot(clock, lambda t: [{"id": "tsk_long", "kind": "task", "title": "long one",
                                      "thread": "C9:9.9", "live": True}])
    code, out, restarts, lock, _ = _after_turn(cli, bot, clock, max_wait=600)
    told = bot.told[-1]["text"] if bot.told else ""
    check("busy past --max-wait: no restart, the thread is told what held it",
          code == 1 and not restarts and "tsk_long" in told, told)


def test_deploy_after_turn_rechecks_and_lets_go_when_stopped():
    print("\nthe detached deploy restarts only onto what was checked, and a stop lifts its drain")
    cli = _load_cli()

    class Clock:
        def __call__(self):
            return self.t[0]
    def fresh():
        c = Clock(); c.t = [0.0]
        return c
    gone = lambda t: []

    # A commit (or a landing) after the checks: not restarted onto.
    clock = fresh()
    bot = _TurnBot(clock, gone)
    code, out, restarts, lock, _ = _after_turn(cli, bot, clock,
                                               git=_after_turn_git("c" * 40))
    told = bot.told[-1]["text"] if bot.told else ""
    check("HEAD moved after the checks: no restart, and the thread is told",
          code == 1 and not restarts and "moved" in told and "cccccccc" in told, told)
    check("(the drain it took is lifted)",
          bot.drain_calls and bot.drain_calls[-1].get("action") == "stop", str(bot.drain_calls[-2:]))
    def dirty(repo, *a):
        return (0, " M bot.py") if a[:1] == ("status",) else _after_turn_git("h" * 40)(repo, *a)
    clock = fresh()
    bot = _TurnBot(clock, gone)
    code, out, restarts, _, _ = _after_turn(cli, bot, clock, git=dirty)
    check("tracked files changed after the checks: no restart",
          code == 1 and not restarts and bot.told and "changed" in bot.told[-1]["text"],
          str(bot.told))

    # Stopped mid-wait (SIGTERM becomes SystemExit): drain lifted, thread told.
    clock = fresh()
    bot = _TurnBot(clock, lambda t: [{"id": "tsk_x", "kind": "task", "title": "other",
                                      "thread": "C9:9.9", "live": True}])
    root = Path(tempfile.mkdtemp())
    lock = root / "deploy.lock"
    lock.write_text(json.dumps({"pid": os.getpid()}))
    def sleep(_s):
        clock.t[0] += 5
        if clock.t[0] > 300:
            raise SystemExit(143)
    stopped = False
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            cli.after_turn_run("h" * 40, "C1:1.0", "tsk_me", 3600, call=bot,
                               manager=types.SimpleNamespace(restart=lambda: True),
                               lock_path=lock, log_path=root / "l", outbox=root / "o",
                               clock=clock, sleep=sleep, confirm_s=10,
                               repo=Path("/nonexistent"), git=_after_turn_git("h" * 40))
        except SystemExit:
            stopped = True
    check("a stop mid-wait lifts the drain rather than leaving it for hours",
          stopped and bot.drain_calls[-1].get("action") == "stop" and not bot.draining,
          str(bot.drain_calls[-2:]))
    check("and still tells the thread, and lets go of the lock",
          bot.told and "stopped" in bot.told[-1]["text"] and not lock.exists(), str(bot.told))


def test_one_deploy_lock_is_shared_and_race_free():
    print("\nterminal deploys and after-turn deploys share one lock, taken by one caller")
    import threading
    cli = _load_cli()
    root = Path(tempfile.mkdtemp())

    # Plain deploy from a terminal while an after-turn deploy is pending.
    lock = root / "a.lock"
    lock.write_text(json.dumps({"pid": 4242, "head": "a" * 40, "stage": "waiting"}))
    restarts = []
    saved = os.environ.pop("SILKWORM_THREAD", None)
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.do_deploy([], repo=Path("/nonexistent"), call=lambda p, b: {},
                                 manager=types.SimpleNamespace(
                                     restart=lambda: restarts.append(1) or True),
                                 git=_after_turn_git("a" * 40), suite=lambda: 0,
                                 lock_path=lock, alive=lambda pid: pid == 4242)
        check("a terminal deploy under a pending one refuses and names it",
              code == 1 and not restarts and "4242" in out.getvalue(), out.getvalue())
        lock.unlink()
        bot = _FakeBot(lambda: 0.0, head="a" * 40)
        seen = []
        class Mgr:
            def restart(self):
                seen.append(json.loads(lock.read_text()).get("pid"))
                bot.restarted_at = 0
                return True
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.do_deploy(["--now"], repo=Path("/nonexistent"), call=bot,
                                 manager=Mgr(), git=_after_turn_git("a" * 40),
                                 suite=lambda: 0, lock_path=lock,
                                 clock=lambda: 0.0, sleep=lambda s: None, confirm_s=0)
        check("a terminal deploy holds the lock while it runs, and lets go after",
              code == 0 and seen == [os.getpid()] and not lock.exists(), f"{code} {seen}")
    finally:
        if saved is not None:
            os.environ["SILKWORM_THREAD"] = saved

    # Many callers find the same stale lock at once: exactly one takes it.
    for round_ in range(5):
        lock = root / f"race{round_}.lock"
        lock.write_text(json.dumps({"pid": 1, "head": "dead"}))
        won, gate = [], threading.Barrier(8)
        def go(n):
            gate.wait()
            if cli.take_deploy_lock(lock, {"pid": 1000 + n}, alive=lambda pid: pid != 1) is None:
                won.append(n)
        th = [threading.Thread(target=go, args=(n,)) for n in range(8)]
        for x in th:
            x.start()
        for x in th:
            x.join()
        if len(won) != 1:
            break
    check("eight callers racing over one stale lock: exactly one wins",
          len(won) == 1 and json.loads(lock.read_text())["pid"] == 1000 + won[0], str(won))


def test_bot_tell_route_and_drain_rows_carry_threads():
    print("\nthe bot can post a line to a thread for a detached deploy")
    import drain as D
    from unittest.mock import MagicMock
    app = MagicMock()
    tell = _bot_func("handle_tell", app=app, re=_re, log=logging.getLogger("test"))
    r = tell({"key": "C1:1.0", "text": "deployed"})
    kw = app.client.chat_postMessage.call_args.kwargs if app.client.chat_postMessage.called else {}
    check("/tell posts in the thread named",
          r == {"ok": True} and kw.get("channel") == "C1" and kw.get("thread_ts") == "1.0"
          and kw.get("text") == "deployed", f"{r} {kw}")
    check("a malformed key or empty text is refused",
          not tell({"key": "nope", "text": "x"})["ok"] and not tell({"key": "C1:1.0"})["ok"])
    app.client.chat_postMessage.side_effect = RuntimeError("slack down")
    r = tell({"key": "C1:1.0", "text": "deployed"})
    check("a post that fails says so, so the caller can use the outbox", r["ok"] is False, str(r))
    check("/tell is routed", 'server.route("/tell", handle_tell)' in (BASE / "bot.py").read_text())

    rows = D.busy([{"id": "tsk_a", "state": "running", "thread": "C1:1.0"}])
    check("what a restart would kill carries each task's thread",
          rows and rows[0].get("thread") == "C1:1.0", str(rows))


# --- releasing from the dashboard and the Slack board -----------------------------
# `!release` was the only way to ship, typed. These are the same release behind
# one confirmed click: a plan with its previews, the exact production effects,
# and a start that carries the fingerprint of what was confirmed.


def test_release_route():
    print("\n/releases: plan, preview, a confirmed start, and every refusal")
    fx = _release_fixture()
    hr, g = fx.ns["handle_releases"], fx.g
    tags = lambda: sorted(g("tag", "-l").stdout.split())

    p = hr({"action": "plan", "slug": "alpha"})
    check("the plan answers", p["ok"], p.get("error"))
    check("every target, in dependency order",
          [t["target"] for t in p["targets"]] == ["backend", "ios", "web"], p.get("targets"))
    rows = {t["target"]: t for t in p["targets"]}
    check("with what is pending and its first commits",
          rows["backend"]["pending"] == 3 and rows["backend"]["commits"][0].endswith("a function")
          and rows["ios"]["pending"] == 1)
    check("and the version each level would give",
          rows["ios"]["versions"] == {"patch": "1.1.1", "minor": "1.2.0", "major": "2.0.0"}
          and rows["backend"]["versions"]["patch"] == "1.0.0", rows["ios"]["versions"])
    check("everything pending, dependencies first",
          [s["target"] for s in p["steps"]] == ["backend", "ios", "web"])
    eff = "\n".join(p["effects"])
    check("the effects name every command that will run",
          f"backend: runs `echo deployed >> {fx.log}`" in p["effects"]
          and "web: runs `exit 3`" in p["effects"], eff)
    check("and how much of what ships, by directory",
          "2 migrations in supabase/migrations" in eff
          and "1 edge function in supabase/functions" in eff, eff)
    check("and every ref pushed, with the project's own description",
          "ios: pushes tag ios/v1.1.1 to origin — the push is the release" in p["effects"]
          and "CFBundleShortVersionString to 1.1.1 in ios/project.yml" in eff
          and "ios: CI builds the tag for TestFlight" in p["effects"], eff)
    check("a ready plan carries a fingerprint and no refusal", p["confirm"] and p["blocked"] == "")
    check("planning ran nothing", not fx.log.exists() and tags() == [])

    pv = hr({"action": "preview", "slug": "alpha", "target": "backend"})
    check("a preview runs the target's preview",
          pv["ok"] and pv["steps"][0]["output"].strip() == "would-apply-0002", pv)
    check("and never its deploy", not fx.log.exists())
    check("a target it does not have is refused",
          not hr({"action": "preview", "slug": "alpha", "target": "nope"})["ok"])
    check("an unknown project is refused", not hr({"action": "plan", "slug": "nope"})["ok"])

    # Levels: major and exact versions are for one target on its own.
    two = hr({"action": "plan", "slug": "alpha", "targets": ["backend", "ios"],
              "levels": {"ios": "major"}})
    check("major alongside another target is refused", not two["ok"] and "on its own" in two["error"],
          two)
    check("so is an exact version", not hr({"action": "plan", "slug": "alpha",
          "levels": {"ios": "3.0.0"}})["ok"])
    check("a bad level is refused", not hr({"action": "plan", "slug": "alpha", "targets": ["ios"],
          "levels": {"ios": "sideways"}})["ok"])
    one = hr({"action": "plan", "slug": "alpha", "targets": ["ios"], "levels": {"ios": "major"}})
    st = {s["target"]: s for s in one.get("steps", [])}
    check("major for one target: it gets it, its dependency a patch",
          one["ok"] and st["ios"]["version"] == "2.0.0" and st["backend"]["level"] == "patch"
          and "web" not in st, one)
    check("a different choice is a different fingerprint", one["confirm"] != p["confirm"])

    # The confirmation is required, and it must be the plan's own.
    r = hr({"action": "start", "slug": "alpha"})
    check("no confirmation, no release", not r["ok"] and "confirmation" in r["error"], r)
    r = hr({"action": "start", "slug": "alpha", "confirm": "0" * 20})
    check("a confirmation of something else, no release", not r["ok"] and "changed" in r["error"], r)
    r = hr({"action": "start", "slug": "alpha", "confirm": one["confirm"]})
    check("the confirmation of another selection, no release", not r["ok"], r)
    r = hr({"action": "start", "slug": "alpha", "confirm": p["confirm"], "by": "slack:U_STEPH"})
    check("off the allowlist, no release", not r["ok"] and "allowlist" in r["error"], r)
    check("and none of those ran or tagged anything", not fx.log.exists() and tags() == []
          and not fx.ns["_releasing"] and not fx.ns["_release_runs"])

    # Refusals, with the reason.
    (fx.repo / "supabase/stray.sql").write_text("drop table x;\n")
    b = hr({"action": "plan", "slug": "alpha"})
    check("a dirty checkout is refused, with the reason",
          "untracked" in b["blocked"] and not b["confirm"], b.get("blocked"))
    r = hr({"action": "start", "slug": "alpha", "confirm": p["confirm"]})
    check("and cannot be started", not r["ok"] and "untracked" in r["error"])
    (fx.repo / "supabase/stray.sql").unlink()
    fx.commit("ios/local.swift", "x\n", "not pushed", push=False)
    b = hr({"action": "plan", "slug": "alpha"})
    check("ahead of origin is refused", "origin does not" in b["blocked"], b.get("blocked"))
    g("push", "-q", "origin", "main")
    other = fx.root / "other"
    subprocess.run(["git", "clone", "-q", str(fx.origin), str(other)], capture_output=True)
    (other / "README").write_text("x\n")
    for a in (["add", "README"], ["commit", "-qm", "elsewhere"], ["push", "-q", "origin", "main"]):
        subprocess.run(["git", "-C", str(other), "-c", "user.email=t@t", "-c", "user.name=t", *a],
                       capture_output=True)
    b = hr({"action": "plan", "slug": "alpha"})
    check("behind origin is refused", "behind origin" in b["blocked"], b.get("blocked"))
    g("pull", "-q", "--ff-only", "origin", "main")
    fresh = hr({"action": "plan", "slug": "alpha"})
    check("ready again once level with origin", fresh["blocked"] == "" and fresh["confirm"])
    r = hr({"action": "start", "slug": "alpha", "confirm": p["confirm"]})
    check("work landing after a confirmation voids it",
          not r["ok"] and "changed" in r["error"] and r["offer"]["confirm"] == fresh["confirm"], r)
    fx.ns["_releasing"].add("alpha")
    b = hr({"action": "plan", "slug": "alpha"})
    check("one release per project at a time",
          "already running" in b["blocked"] and not hr({"action": "start", "slug": "alpha",
                                                        "confirm": fresh["confirm"]})["ok"])
    check("and no preview while it runs",
          not hr({"action": "preview", "slug": "alpha", "target": "backend"})["ok"])
    fx.ns["_releasing"].discard("alpha")
    check("list offers it while something is ready",
          [x["slug"] for x in hr({"action": "list"})["projects"]] == ["alpha"])

    # A confirmed release: backend, then ios, then web fails and stops it.
    fx.ns["_board_releases"].get("alpha", lambda: "cached before the release")
    r = hr({"action": "start", "slug": "alpha", "confirm": fresh["confirm"], "by": "slack:U_ME"})
    check("confirmed, it starts", r["ok"], r)
    st = _wait_release(hr, "alpha")
    run = st["run"]
    check("it finishes", run and run["finished"], st)
    check("in order, stopping at the failure",
          run["released"] == ["backend/v1.0.0", "ios/v1.1.1"] and run["ok"] is False
          and any("web" in l and "Stopping" in l for l in run["lines"]), run["lines"])
    check("the deploy ran once", fx.log.read_text().count("deployed") == 1)
    check("the tags are on origin", {"backend/v1.0.0", "ios/v1.1.1"} <= set(
        subprocess.run(["git", "-C", str(fx.origin), "tag"], capture_output=True, text=True).stdout.split()))
    check("progress opened a thread and posted under it",
          fx.posts[0][1] is None and "Release" in fx.posts[0][2]
          and all(t == "9.1" for _, t, _ in fx.posts[1:]) and len(fx.posts) >= 4, fx.posts)
    check("starting with exactly what it confirmed",
          "confirmed by slack:U_ME" in fx.posts[1][2] and "ios: pushes tag ios/v1.1.1" in fx.posts[1][2])
    check("and is free again afterwards", not fx.ns["_releasing"])
    check("and the board's cached plans are dropped, so it stops offering Release…",
          fx.ns["_board_releases"].get("alpha", lambda: "fresh") == "fresh")
    tree = ast.parse((BASE / "bot.py").read_text())
    sr = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "start_release")
    go = next(n for n in ast.walk(sr) if isinstance(n, ast.FunctionDef) and n.name == "go")
    frees = [c for c in ast.walk(go) if isinstance(c, ast.Call)
             and isinstance(c.func, ast.Attribute) and c.func.attr == "discard"]
    check("the running release is freed once, by run_release, never again by its starter "
          "(a second discard would free a release begun in between)", not frees)
    nothing = hr({"action": "plan", "slug": "alpha", "targets": ["backend"]})
    check("nothing pending is refused, with the reason",
          "nothing is pending for backend" in nothing["blocked"], nothing.get("blocked"))

    # The confirmation is checked again under the guard, at the moment it ships.
    fx.commit("supabase/migrations/0003.sql", "x\n", "migration three")
    posted = []
    fx.ns["run_release"]("alpha", fx.repo, "main", ["backend"], "patch", posted.append,
                         expect="not-the-plan")
    check("a release whose plan moved under it ships nothing",
          "backend/v1.0.1" not in tags() and any("changed after it was confirmed" in x for x in posted),
          posted)


def test_no_release_starts_while_a_deploy_drains():
    import drain as D
    import home
    import tasks as T
    print("\na deploy waits for a release in flight, and no release starts while it drains")

    # A release in flight is something a restart would kill.
    rows = D.busy([], releasing={"alpha"})
    check("drain.busy reports a release in flight",
          [(r["id"], r["kind"]) for r in rows] == [("alpha", "release")], str(rows))
    handle = _bot_func("handle_drain", DRAIN=D.Drain(), drain=D,
                       task_store=types.SimpleNamespace(all=lambda: {}),
                       _landing_now=set(), RUNNING_TASKS={}, _releasing={"alpha"},
                       REVISION={"sha": "abc"})
    r = handle({"action": "status"})
    check("the /drain answer names it, so deploy and idle restart wait on it",
          any(row["kind"] == "release" and row["id"] == "alpha" for row in r["running"]), str(r))

    def settle(fx):
        end = time.time() + 60
        while fx.ns["_releasing"] and time.time() < end:
            time.sleep(0.05)

    # !release
    fx = _release_fixture()
    fx.ns["DRAIN"].start(600, why="deploy")
    said = []
    reply = fx.ns["release_command"]("alpha ios", said.append)
    check("!release is refused while a deploy drains",
          "draining" in reply and not reply.startswith(":rocket:")
          and not fx.ns["_releasing"] and not fx.g("tag", "-l").stdout.strip(), reply)
    fx.ns["DRAIN"].stop()
    reply = fx.ns["release_command"]("alpha ios", said.append)
    settle(fx)
    check("!release starts once the drain is lifted",
          reply.startswith(":rocket:") and "ios/v1.1.1" in fx.g("tag", "-l").stdout, reply)

    # The dashboard's Release: handle_releases start
    fx = _release_fixture()
    hr = fx.ns["handle_releases"]
    confirm = hr({"action": "plan", "slug": "alpha", "targets": ["ios"]})["confirm"]
    fx.ns["DRAIN"].start(600, why="deploy")
    r = hr({"action": "start", "slug": "alpha", "targets": ["ios"], "confirm": confirm})
    check("the dashboard's release is refused while a deploy drains",
          not r["ok"] and "draining" in r["error"] and not fx.ns["_releasing"]
          and not fx.ns["_release_runs"], r)
    fx.ns["DRAIN"].stop()
    r = hr({"action": "start", "slug": "alpha", "targets": ["ios"], "confirm": confirm})
    st = _wait_release(hr, "alpha")
    check("the dashboard's release starts once the drain is lifted",
          r["ok"] and st["run"]["released"] == ["backend/v1.0.0", "ios/v1.1.1"], (r, st))

    # The Slack board's Release
    fx = _release_fixture()
    hr = fx.ns["handle_releases"]
    hub = _board_for(T.TaskStore(fx.root / "t.json"), lambda p: {"ok": True},
                     {"U_ME"}, release_call=hr)
    hub._spawn = lambda fn: fn()
    hub.publish = lambda *a, **k: None
    meta = {"slug": "alpha", "level": "patch",
            "confirm": hr({"action": "plan", "slug": "alpha"})["confirm"]}
    view = {"private_metadata": json.dumps(meta)}
    fx.ns["DRAIN"].start(600, why="deploy")
    hub.on_release(lambda **k: None, {"user": {"id": "U_ME"}}, None, view)
    check("the board's release is refused while a deploy drains",
          "Couldn't release" in hub._notices["U_ME"][0] and "draining" in hub._notices["U_ME"][0]
          and not fx.ns["_release_runs"] and not fx.log.exists(), hub._notices)
    fx.ns["DRAIN"].stop()
    hub.on_release(lambda **k: None, {"user": {"id": "U_ME"}}, None, view)
    _wait_release(hr, "alpha")
    check("the board's release starts once the drain is lifted",
          "Releasing" in hub._notices["U_ME"][0] and fx.log.exists(), hub._notices)

    # A run's needs_user Release step
    fx = _release_fixture()
    st = T.TaskStore(fx.root / "t.json")
    told = []
    ns = lift("handle_tasks", task_store=st, tasks=T,
                       holding=__import__("holding"), task_state=_task_state_for(st),
                       release_command=fx.ns["release_command"], RELEASE_STARTED=":rocket:",
                       LANDING_UNDERWAY="in-progress", merge=__import__("merge"),
                       roles=__import__("roles"),
                       tell_thread=lambda key, text: told.append(text),
                       stop_task=lambda tid: False, start_landing=lambda tid, **k: None,
              absent=TASK_ROUTES)
    t = st.create("Ship it", role="implementor", project="alpha", thread="C1:1.0")
    st.transition(t["id"], T.RUNNING)
    st.update(t["id"], needs_user={"action": "!release alpha ios", "why": "",
                                   "release": "alpha ios"})
    st.transition(t["id"], T.NEEDS_INPUT, "yours")
    fx.ns["DRAIN"].start(600, why="deploy")
    r = ns["handle_tasks"]({"action": "release", "id": t["id"], "by": "you"})
    check("a needs_user Release step is refused while a deploy drains",
          not r["ok"] and "draining" in r["error"] and not fx.ns["_releasing"]
          and st.get(t["id"])["state"] == T.NEEDS_INPUT, r)
    fx.ns["DRAIN"].stop()
    r = ns["handle_tasks"]({"action": "release", "id": t["id"], "by": "you"})
    settle(fx)
    check("the Release step runs once the drain is lifted, and closes the task",
          r["ok"] and st.get(t["id"])["state"] == T.DONE, (r, st.get(t["id"])["state"]))


def test_release_from_the_slack_board():
    import home
    print("\nthe Slack board: Release… opens the confirmation, confirming runs it")
    fx = _release_fixture()
    hr = fx.ns["handle_releases"]

    class Slack:
        def __init__(self):
            self.calls = []
        def views_open(self, trigger_id, view):
            self.calls.append(("open", view)); return {"view": {"id": "V1"}}
        def views_update(self, view_id, view):
            self.calls.append(("update", view_id, view))
        def chat_postEphemeral(self, **kw):
            self.calls.append(("ephemeral", kw))

    import tasks as T
    store = T.TaskStore(fx.root / "t.json")
    hub = _board_for(store, lambda p: {"ok": True}, {"U_ME"},
                     releasable=lambda: hr({"action": "list"})["projects"], release_call=hr)
    hub._spawn = lambda fn: fn()
    hub.publish = lambda *a, **k: None          # the redraw is test_board_channel's
    view = _board_view(hub)
    dumped = json.dumps(view)
    check("a project with something to release is on the board",
          "Ready to release" in dumped and '"release|alpha|"' in dumped
          and "backend 3" in dumped, dumped[-600:])
    rb = home.release_blocks([{"slug": f"p{i}", "title": "P", "targets": []} for i in range(9)])
    check("with a menu entry Release…, the rest counted",
          sum(1 for x in rb if x.get("accessory")) == home.MAX_RELEASES
          and "4 more" in json.dumps(rb))
    big = home.render([store.create(f"t{i}", state=T.PROPOSED) for i in range(60)], now=time.time(),
                      max_blocks=50, max_attention=40,
                      releasable=[{"slug": f"p{i}", "title": "P", "targets": []} for i in range(9)])
    check("and the board still fits a message", len(big["blocks"]) <= 50
          and "Ready to release" in json.dumps(big), len(big["blocks"]))

    menu = lambda user, value: {"user": {"id": user}, "trigger_id": "T1",
                                "actions": [{"selected_option": {"value": value}}]}
    s = Slack()
    hub.on_menu(lambda **k: None, menu("U_STEPH", "release|alpha|"), s)
    check("off the allowlist, no window and no plan", not [c for c in s.calls if c[0] in ("open", "update")])
    s = Slack()
    hub.on_menu(lambda **k: None, menu("U_ME", "release|alpha|"), s)
    opened, updated = s.calls[0], s.calls[1]
    check("a window opens at once, with nothing to submit yet",
          opened[0] == "open" and "submit" not in opened[1] and "Working out" in json.dumps(opened[1]))
    v = updated[2]
    text = json.dumps(v)
    check("then the plan fills it, in order",
          updated[:2] == ("update", "V1") and text.find("*backend*") < text.find("*ios*") < text.find("*web*"))
    check("with what reaches production", "What will reach production" in text
          and "pushes tag ios/v1.1.1" in text and "echo deployed" in text, text[:400])
    check("and the preview's output", "would-apply-0002" in text)
    meta = json.loads(v["private_metadata"])
    check("and a Release button carrying the plan's fingerprint and level",
          v["submit"]["text"] == "Release" and meta == {"slug": "alpha", "level": "patch",
          "confirm": hr({"action": "plan", "slug": "alpha"})["confirm"]})
    picker = [e for b in v["blocks"] if b.get("block_id") == "release_level"
              for e in b["elements"]]
    check("with a level picker: patch or minor for everything, patch chosen",
          len(picker) == 1 and picker[0]["action_id"] == home.RELEASE_LEVEL_ACTION
          and [o["value"] for o in picker[0]["options"]] == ["patch", "minor"]
          and picker[0]["initial_option"]["value"] == "patch", picker)
    check("the board registers the picker",
          "app.action(RELEASE_LEVEL_ACTION)(home.on_release_level)" in (BASE / "home.py").read_text())

    # Picking minor re-plans the window at minor, without re-running previews.
    asked = []
    spy = lambda p: asked.append(p) or hr(p)
    hub.release_call = spy
    pick = lambda user, lvl: {"user": {"id": user}, "view": {"id": "V1", "private_metadata": v["private_metadata"]},
                              "actions": [{"selected_option": {"value": lvl}}]}
    s = Slack()
    hub.on_release_level(lambda **k: None, pick("U_STEPH", "minor"), s)
    check("off the allowlist, picking a level does nothing", not s.calls and not asked)
    hub.on_release_level(lambda **k: None, pick("U_ME", "major"), s)
    check("major is not offered for everything, and is ignored", not s.calls and not asked)
    hub.on_release_level(lambda **k: None, pick("U_ME", "minor"), s)
    hub.release_call = hr
    mv = s.calls[-1][2] if s.calls else {}
    mtext = json.dumps(mv)
    mmeta = json.loads(mv.get("private_metadata") or "{}")
    minor_plan = hr({"action": "plan", "slug": "alpha", "level": "minor"})
    check("picking minor redraws the same window at minor",
          s.calls and s.calls[-1][:2] == ("update", "V1") and "ios/v1.2.0" in mtext
          and "(minor)" in mtext and "ios/v1.1.1" not in mtext, mtext[:500])
    check("its Release carries the minor plan's fingerprint, not patch's",
          mmeta == {"slug": "alpha", "level": "minor", "confirm": minor_plan["confirm"]}
          and minor_plan["confirm"] != meta["confirm"], mmeta)
    check("the previews are kept, not run again",
          [p["action"] for p in asked] == ["plan"] and "would-apply-0002" in mtext, asked)
    check("and the picker now shows minor",
          any(e.get("initial_option", {}).get("value") == "minor"
              for b in mv.get("blocks", []) if b.get("block_id") == "release_level"
              for e in b["elements"]))
    stale = hr({"action": "start", "slug": "alpha", "level": "minor", "confirm": meta["confirm"],
                "by": "slack:U_ME"})
    check("a patch window's fingerprint cannot start a minor release",
          not stale["ok"] and "changed" in stale["error"] and not fx.log.exists(), stale)
    check("the route refuses a level it does not know",
          not hr({"action": "plan", "slug": "alpha", "level": "huge"})["ok"])
    check("and major for everything at once",
          not hr({"action": "plan", "slug": "alpha", "level": "major"})["ok"])
    check("nothing ran in showing it", not fx.log.exists() and not fx.g("tag", "-l").stdout.strip())
    s = Slack()
    hub.on_menu(lambda **k: None, menu("U_ME", "preview|alpha|"), s)
    check("Preview only… is the same window with nothing to submit",
          "would-apply-0002" in json.dumps(s.calls[-1]) and "submit" not in s.calls[-1][2])

    body = lambda user: {"user": {"id": user}}
    view_ = lambda m: {"private_metadata": json.dumps(m)}
    s = Slack()
    hub.on_release(lambda **k: None, body("U_STEPH"), s, view_(meta))
    check("confirmed by someone off the allowlist, nothing is released",
          not fx.log.exists() and not fx.ns["_release_runs"])
    # The board's own check, not only the route's: with a route that would
    # accept anything, an outsider's click still never reaches it.
    asked = []
    lax = _board_for(store, lambda p: {"ok": True}, {"U_ME"},
                     release_call=lambda p: asked.append(p) or {"ok": True})
    lax._spawn = lambda fn: fn()
    lax.publish = lambda *a, **k: None
    lax.on_release(lambda **k: None, body("U_STEPH"), Slack(), view_(meta))
    lax.on_menu(lambda **k: None, menu("U_STEPH", "release|alpha|"), Slack())
    check("the board itself refuses an outsider before asking the route", asked == [], asked)
    hub.on_release(lambda **k: None, body("U_ME"), s, view_({"slug": "alpha", "confirm": ""}))
    check("a window without the plan's fingerprint releases nothing",
          not fx.log.exists() and "Couldn't release" in hub._notices["U_ME"][0], hub._notices)
    (fx.repo / "stray").write_text("x")
    s = Slack()
    hub.on_menu(lambda **k: None, menu("U_ME", "release|alpha|"), s)
    blocked = s.calls[-1][2]
    check("a checkout that cannot release says why and offers no Release",
          "Can't release" in json.dumps(blocked) and "untracked" in json.dumps(blocked)
          and "submit" not in blocked)
    (fx.repo / "stray").unlink()
    racing = {"private_metadata": json.dumps(meta), "state": {"values": {"release_level": {
        home.RELEASE_LEVEL_ACTION: {"selected_option": {"value": "minor"}}}}}}
    hub.on_release(lambda **k: None, body("U_ME"), Slack(), racing)
    check("released while the picker reads minor but the plan is still patch's, nothing ships",
          not fx.log.exists() and not fx.ns["_release_runs"]
          and "still working out the minor" in hub._notices["U_ME"][0], hub._notices)
    # A preview is reused only while its target's pending commits are the same.
    ran = []
    hub.release_call = lambda p: ran.append(p["action"]) or hr(p)
    hub.fill_release(Slack(), "V9", "U_ME", "alpha", True, "minor",
                     {"backend": (("old", ()), {"ok": True, "steps": []})})
    check("a preview of what was pending before runs again", ran == ["plan", "preview"], ran)
    ran.clear()
    hub.fill_release(Slack(), "V9", "U_ME", "alpha", True, "patch", hub._previews["V9"])
    check("one of what is still pending is reused", ran == ["plan"], ran)
    hub.release_call = hr
    hub.on_release(lambda **k: None, body("U_ME"), s, view_(mmeta))
    check("confirmed, it releases", "Releasing" in hub._notices["U_ME"][0], hub._notices)
    st = _wait_release(hr, "alpha")
    check("and runs to the end, at the level the window showed",
          st["run"]["released"] == ["backend/v1.0.0", "ios/v1.2.0"]
          and st["run"]["by"] == "slack:U_ME", st["run"])
    check("the board registers the window's submit", "app.view(RELEASE_CALLBACK)(home.on_release)"
          in (BASE / "home.py").read_text())
    bot = (BASE / "bot.py").read_text()
    check("the bot wires the board to the route",
          "releasable=releasable_projects, release_call=handle_releases" in bot
          and 'server.route("/releases", handle_releases)' in bot)


RELEASE_DRIVER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
function el(id) {
  return {id, innerHTML: "", textContent: "", value: "", title: "", className: "",
          style: {}, disabled: false, checked: false, dataset: {}, children: [],
          classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
          appendChild() {}, removeChild() {}, remove() {}, addEventListener() {},
          insertAdjacentHTML(_, h) { this.innerHTML += h; }, focus() {}, scrollIntoView() {},
          querySelector() { return null; }, querySelectorAll() { return []; }};
}
const els = {};
const byId = id => els[id] || (els[id] = el(id));
// Redrawing replaces the elements inside, as a browser would.
const box = el("fbox");
let boxHtml = "";
Object.defineProperty(box, "innerHTML", {
  get() { return boxHtml; },
  set(h) { boxHtml = h; for (const m of h.matchAll(/id="([^"]+)"/g)) delete els[m[1]]; }});
els.fbox = box;
globalThis.document = {getElementById: byId, createElement: () => el("new"),
                       querySelector: () => null, querySelectorAll: () => [],
                       addEventListener() {}, body: el("body")};
globalThis.window = {addEventListener() {}, location: {search: ""}};
globalThis.localStorage = {getItem: () => null, setItem() {}};
globalThis.setInterval = () => 0;
const timers = [];
globalThis.setTimeout = (fn) => { timers.push(fn); return 0; };
globalThis.confirm = () => true;
globalThis.prompt = () => null;
const calls = [];
const replies = {};
const PLAN = {ok: true, slug: "alpha", base: "main", blocked: "", running: false, confirm: "fp-all",
  targets: [
    {target: "backend", ship: "command", after: [], last: "", pending: 2, preview: ["sb db push --dry-run"],
     commits: ["a1 migration two", "a0 migration one"], versions: {patch: "1.0.0", minor: "1.0.0", major: "1.0.0"}},
    {target: "ios", ship: "tag", after: ["backend"], last: "ios/v1.1.0", pending: 1, preview: [],
     commits: ["b1 app change"], versions: {patch: "1.1.1", minor: "1.2.0", major: "2.0.0"}},
    {target: "web", ship: "command", after: [], last: "web/v1.0.0", pending: 0, preview: [],
     commits: [], versions: {patch: "1.0.1", minor: "1.1.0", major: "2.0.0"}}],
  steps: [{target: "backend", level: "patch", version: "1.0.0", tag: "backend/v1.0.0", commits: 2},
          {target: "ios", level: "patch", version: "1.1.1", tag: "ios/v1.1.1", commits: 1}],
  effects: ["backend: runs `sb db push --yes`", "ios: pushes tag ios/v1.1.1 to origin — the push is the release"]};
globalThis.fetch = async (url, opts) => {
  const body = opts && opts.body ? JSON.parse(opts.body) : {};
  let out = {ok: true};
  if (url.startsWith("/api/sessions")) out = {bot_online: true, sessions: [], slack: {}};
  else if (url.startsWith("/api/stats")) out = {total_cost: 0, cache_rate: null, threads: 0, days: [], models: []};
  else {
    calls.push({url, ...body});
    const key = `${url}:${body.action}`;
    if (replies[key]) out = replies[key](body);
    else if (key === "/api/releases:plan") out = PLAN;
    else if (key === "/api/releases:preview") out = {ok: true, target: body.target,
      steps: [{command: "sb db push --dry-run", ok: true, code: 0, output: "Would push 0002_x.sql"}]};
    else if (key === "/api/releases:start") out = {ok: true, run: {lines: []}};
    else if (key === "/api/releases:status") out = {ok: true, running: false, run: {finished: 1, ok: true,
      released: ["backend/v1.0.0", "ios/v1.1.1"], lines: [":white_check_mark: *backend* released"]}};
    else if (body.action === "overview") out = {ok: true, projects: [], archived: []};
  }
  return {json: async () => out, text: async () => ""};
};
(0, eval)(src + `
;toast = m => {};
globalThis.__b = {releaseForm, releaseChanged, confirmRelease, startRelease, closeModal, modalIsOpen,
  projectCard, boardHead, pollRelease, drawReleasePlan, get form() { return boardForm; }};`);
const tick = async () => { for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r)); };
const of = (action) => calls.filter(c => c.url === "/api/releases" && c.action === action);
(async () => {
  const B = globalThis.__b, out = {};
  out.card = B.projectCard({slug: "alpha", title: "Alpha", counts: {},
    release: {targets: [{target: "ios", commits: 1, version: "1.1.1"}]}});
  out.cardNone = B.projectCard({slug: "beta", title: "Beta", counts: {}, release: null});
  out.cardBroken = B.projectCard({slug: "gamma", title: "G", counts: {}, release: {error: "bad", targets: []}});
  out.head = B.boardHead({slug: "alpha", title: "Alpha", unready: "", release: true});
  out.headNone = B.boardHead({slug: "beta", title: "Beta", unready: "", release: false});

  const opening = B.releaseForm("alpha");
  out.loading = byId("fbox").innerHTML;
  await opening; await tick();
  out.plan = byId("fbox").innerHTML;
  out.previews = of("preview").map(c => c.target);
  out.previewShown = byId("fbox").innerHTML.includes("Would push 0002_x.sql")
    || (els["rpv-backend"] || {}).innerHTML;

  // Release… asks; nothing has started yet.
  out.startedEarly = (await B.startRelease(), of("start").length);
  B.confirmRelease();
  out.confirm = byId("fbox").innerHTML;
  out.startsBeforeYes = of("start").length;
  // Untouched, Yes asks for what the first plan was asked for: everything pending.
  await B.startRelease(); await tick();
  out.firstStart = of("start").slice(-1)[0];
  await B.releaseForm("alpha"); await tick();
  B.confirmRelease();

  // Back, choose ios alone at major: a new plan from the route.
  B.drawReleasePlan();
  byId("rsel-backend").checked = false; byId("rsel-ios").checked = true;
  byId("rlvl-ios").value = "major";
  replies["/api/releases:plan"] = b => Object.assign({}, PLAN, {confirm: "fp-ios-major",
    steps: [{target: "backend", level: "patch", version: "1.0.0", tag: "backend/v1.0.0", commits: 2},
            {target: "ios", level: "major", version: "2.0.0", tag: "ios/v2.0.0", commits: 1}]});
  await B.releaseChanged(); await tick();
  out.singlePlan = of("plan").slice(-1)[0];
  out.singleHtml = byId("fbox").innerHTML;
  // An exact version: the box appears, and nothing is confirmable until it is typed.
  const plansBefore = of("plan").length;
  byId("rsel-backend").checked = false; byId("rsel-ios").checked = true;
  byId("rlvl-ios").value = "version";
  await B.releaseChanged(); await tick();
  out.versionBox = {html: byId("fbox").innerHTML, err: byId("tferr").textContent,
                    asked: of("plan").length - plansBefore, confirm: B.form.offer.confirm};
  byId("rsel-backend").checked = false; byId("rsel-ios").checked = true;
  byId("rlvl-ios").value = "version"; byId("rver-ios").value = "3.0.0";
  await B.releaseChanged(); await tick();
  out.versionPlan = of("plan").slice(-1)[0];
  // Two targets with major: coerced to patch before asking.
  byId("rsel-backend").checked = true; byId("rsel-ios").checked = true;
  byId("rlvl-ios").value = "major"; byId("rlvl-backend").value = "patch";
  delete replies["/api/releases:plan"];
  await B.releaseChanged(); await tick();
  out.twoPlan = of("plan").slice(-1)[0];
  out.twoHtml = byId("fbox").innerHTML;

  B.confirmRelease();
  await B.startRelease(); await tick();
  out.start = of("start").slice(-1)[0];
  out.running = byId("fbox").innerHTML + byId("rprog").innerHTML;
  out.status = of("status").length;

  // A start refused because the plan moved: back to the new plan, with the reason.
  await B.releaseForm("alpha"); await tick();
  replies["/api/releases:start"] = () => ({ok: false, error: "what would ship has changed since it was confirmed; look again",
    offer: Object.assign({}, PLAN, {confirm: "fp-new"})});
  B.confirmRelease(); await B.startRelease(); await tick();
  out.moved = {html: byId("fbox").innerHTML, err: byId("tferr").textContent, confirm: B.form.offer.confirm};
  delete replies["/api/releases:start"];

  // Blocked: the reason, and no way forward.
  replies["/api/releases:plan"] = () => Object.assign({}, PLAN, {blocked: "the checkout has uncommitted or untracked files: x", confirm: ""});
  await B.releaseForm("alpha"); await tick();
  out.blocked = byId("fbox").innerHTML;
  const before = of("start").length;
  B.confirmRelease(); await B.startRelease();
  out.blockedStarts = of("start").length - before;
  // Running, but started by !release: the plan says so; no stale progress.
  replies["/api/releases:plan"] = () => Object.assign({}, PLAN, {running: true, confirm: "",
    blocked: "a release of alpha is already running"});
  replies["/api/releases:status"] = () => ({ok: true, running: true,
    run: {finished: 5, ok: true, released: ["old/v1.0.0"], lines: ["days ago"]}});
  const pv = of("preview").length;
  await B.releaseForm("alpha"); await tick();
  out.elsewhere = {html: byId("fbox").innerHTML, previews: of("preview").length - pv};
  replies["/api/releases:status"] = () => ({ok: true, running: true, run: {finished: null, lines: ["going"]}});
  await B.releaseForm("alpha"); await tick();
  out.ownRunning = byId("fbox").innerHTML + (els["rprog"] || {innerHTML: ""}).innerHTML;
  delete replies["/api/releases:status"];
  B.closeModal();
  out.closed = B.modalIsOpen();
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_release_modal_in_the_page():
    import re
    sys.argv = ["x"]
    import visualizer as V
    print("\nthe dashboard's Release… modal, driven through the page's own javascript")
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    defined = set(re.findall(r"(?:async\s+)?function\s+([A-Za-z_]\w*)", js))
    for name in ("releaseForm", "releaseChanged", "confirmRelease", "startRelease",
                 "pollRelease", "runPreview", "releaseCall", "releaseButton"):
        check(f"{name}() is defined", name in defined)
    vis = (BASE / "visualizer.py").read_text()
    check("the page's /api/releases reaches the bot's /releases",
          'url.path == "/api/releases"' in vis and 'bot_call("/releases"' in vis)
    node = shutil.which("node")
    if not node:
        print("  … node not installed — cannot run the page's own javascript")
        return
    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    (d / "drive.js").write_text(RELEASE_DRIVER)
    p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js")],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        check("the release modal's javascript runs", False, p.stderr.strip()[-800:])
        return
    o = json.loads(p.stdout)
    check("a project card with release targets offers Release…",
          "releaseForm('alpha')" in o["card"] and "releaseForm" not in o["cardNone"]
          and "releaseForm" not in o["cardBroken"])
    check("so does its board header", "releaseForm('alpha')" in o["head"]
          and "releaseForm" not in o["headNone"])
    check("it opens with a spinner while the plan is worked out", "working out" in o["loading"])
    pl = o["plan"]
    check("the plan: per target what is pending, and its first commits",
          "<b>backend</b> · 2 changes" in pl and "a1 migration two" in pl
          and "<b>web</b> · nothing pending" in pl, pl[:600])
    check("a level per target, with the version each gives",
          'id="rlvl-backend"' in pl and "minor → 1.2.0" in pl)
    check("no major while releasing more than one target",
          "major →" not in pl and "exact version" not in pl)
    check("the dependency order", "1. <b>backend</b>" in pl and "2. <b>ios</b>" in pl)
    check("previews run on opening, only for targets that have one",
          o["previews"] == ["backend"] and o["previewShown"], o["previews"])
    check("Release… asks first: nothing starts before the confirmation",
          o["startedEarly"] == 0 and o["startsBeforeYes"] == 0)
    c = o["confirm"]
    check("the confirmation lists exactly what reaches production",
          "runs `sb db push --yes`" in c and "pushes tag ios/v1.1.1" in c
          and ">Yes, release</button>" in c)
    check("one target alone asks the route for its plan at major",
          o["singlePlan"] == {"url": "/api/releases", "action": "plan", "slug": "alpha",
                              "targets": ["ios"], "levels": {"ios": "major"}}, o["singlePlan"])
    check("and may offer major", "major → 2.0.0" in o["singleHtml"] and "ios/v2.0.0" in o["singleHtml"])
    check("major is dropped to a patch when another target joins",
          o["twoPlan"]["levels"] == {"backend": "patch", "ios": "patch"}, o["twoPlan"])
    s = o["start"]
    check("Yes sends the plan's fingerprint and the selection",
          s == {"url": "/api/releases", "action": "start", "slug": "alpha", "confirm": "fp-all",
                "by": "dashboard", "targets": ["backend", "ios"],
                "levels": {"backend": "patch", "ios": "patch"}}, s)
    check("then progress is polled and shown",
          o["status"] >= 1 and "✓ released backend/v1.0.0, ios/v1.1.1" in o["running"], o["running"][-300:])
    check("a plan that moved goes back to the plan with the reason",
          "has changed" in o["moved"]["err"] and o["moved"]["confirm"] == "fp-new"
          and 'id="rgo"' in o["moved"]["html"])
    check("a blocked plan says why and cannot be released",
          "Can&#39;t release" in o["blocked"] or "Can't release" in o["blocked"])
    check("its Release… is disabled, and nothing can start",
          'onclick="confirmRelease()" disabled' in o["blocked"] and o["blockedStarts"] == 0)
    check("untouched, Yes starts what the first plan was asked for (no targets: all pending)",
          o["firstStart"] == {"url": "/api/releases", "action": "start", "slug": "alpha",
                              "confirm": "fp-all", "by": "dashboard"}, o["firstStart"])
    vb = o["versionBox"]
    check("exact version keeps its choice and shows the box",
          'id="rver-ios"' in vb["html"] and 'value="version" selected' in vb["html"], vb["html"][-500:])
    check("and nothing is confirmable until a version is typed",
          vb["asked"] == 0 and not vb["confirm"] and "exact version" in vb["err"], vb)
    check("typed, the route is asked for it",
          o["versionPlan"]["levels"] == {"ios": "3.0.0"} and o["versionPlan"]["targets"] == ["ios"],
          o["versionPlan"])
    check("a release started elsewhere shows the plan saying so, not stale progress",
          "already running" in o["elsewhere"]["html"] and "days ago" not in o["elsewhere"]["html"]
          and o["elsewhere"]["previews"] == 0, o["elsewhere"]["html"][-300:])
    check("its own release still running reopens on its progress", "going" in o["ownRunning"])
    check("Cancel closes it", o["closed"] is False)


# --- boot, promotion and rollback: Silkworm must not go down unnoticed again --------
# 2026-10-26: 793f119 registered server.route("/ops", handle_ops) three thousand
# lines above `def handle_ops`. The suite passed (it lifts functions; nothing
# booted bot.py), deploy restarted onto it, bot.py raised NameError at import,
# and the deploy's failure note went to outbox/ because the bot is the sender.
# Down 18.5 days, until bot.py was hand-patched in the live tree.

#: The commits each _code_copy made, oldest first, by directory.
_SHAS: dict = {}


def _code_copy(apply=None, extra_commits=()) -> Path:
    """This checkout's code (tracked and new files, as they are on disk) in a
    fresh git repo, committed. `apply(dir)` edits it before the first commit;
    each of `extra_commits` is (message, apply) committed after, in order."""
    d = Path(tempfile.mkdtemp(prefix="sw-code-"))
    names = subprocess.run(["git", "-C", str(BASE), "ls-files", "-co", "--exclude-standard"],
                           capture_output=True, text=True, check=True).stdout.splitlines()
    for n in names:
        src = BASE / n
        if src.is_file() and not n.startswith(("tests/", "assets/screens")):
            (d / n).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, d / n)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    env = {k: v for k, v in env.items() if not k.startswith("SILKWORM_")}

    def commit(msg):
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", msg, "--no-verify"],
                       check=True, env=env)
        return subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()

    subprocess.run(["git", "-C", str(d), "init", "-q", "-b", "main"], check=True, env=env)
    if apply:
        apply(d)
    shas = [commit("good")]
    for msg, fn in extra_commits:
        fn(d)
        shas.append(commit(msg))
    _SHAS[d] = shas
    return d


def _the_793f119_ordering(d: Path) -> None:
    """Put bot.py back the way 793f119 had it: the /ops route registered at
    module level, its handler defined far below."""
    p = d / "bot.py"
    s = p.read_text()
    start = s.index("#: Every run of a named op (ops.py)")
    end = s.index("server = LocalServer(APPROVAL_PORT)")
    block, s = s[start:end], s[:start] + s[end:]
    at = s.index("_CLI = {}\n")
    p.write_text(s[:at] + block + s[at:])


def _raise_at_import(d: Path) -> None:
    p = d / "bot.py"
    s = p.read_text()
    at = s.index("server = LocalServer(APPROVAL_PORT)")
    p.write_text(s[:at] + 'raise RuntimeError("smoke: this revision cannot boot")\n' + s[at:])


def test_bot_and_visualizer_boot_for_real():
    import smoke
    print("\nbot.py and visualizer.py boot, as processes, from this checkout")
    r = smoke.boot(BASE)
    check("bot.py answers /status and the visualizer answers", r["ok"], smoke.describe(r))
    head = subprocess.run(["git", "-C", str(BASE), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    rev = (r.get("status") or {}).get("revision") or {}
    check("and reports the revision it booted", rev.get("started") == head, json.dumps(rev))
    check("Slack was the loopback stand-in, which it really called",
          "auth.test" in r.get("slack_calls", []), r.get("slack_calls"))
    st = r.get("status") or {}
    check("/status says where the code runs from and when it booted",
          st.get("code_dir") == str(BASE) and st.get("pid") and st.get("booted"),
          {k: st.get(k) for k in ("code_dir", "pid", "booted")})
    env = smoke.boot_env(Path("/tmp/x"), "http://127.0.0.1:1/api/", 1, 2,
                         base={"SLACK_BOT_TOKEN": "xoxb-" + "real", "CLAUDE_CODE_OAUTH_TOKEN": "sk",
                               "SILKWORM_THREAD": "C1:1", "PATH": "/bin"})
    check("no real token or turn variable reaches the booted processes",
          env["SLACK_BOT_TOKEN"] == "xoxb-" + "smoke-not-a-token"
          and "CLAUDE_CODE_OAUTH_TOKEN" not in env and "SILKWORM_THREAD" not in env
          and env["SILKWORM_OFFLINE"] == "1" and env["PATH"] == "/bin", env)


def test_boot_smoke_fails_on_the_793f119_ordering():
    import smoke
    print("\nthe boot smoke test fails on the code that took Silkworm down")
    bad = _code_copy(_the_793f119_ordering)
    r = smoke.boot(bad, timeout=60)
    check("it does not boot", not r["ok"], smoke.describe(r))
    check("and says why: bot.py died before answering",
          "bot.py exited" in r["why"], r["why"])
    check("with the NameError from stderr",
          "NameError" in r["stderr"] and "handle_ops" in r["stderr"], r["stderr"][-400:])
    good = _code_copy()
    r = smoke.boot(good, timeout=60, expect_sha=_SHAS[good][0])
    check("the same tree with the fix boots, at the expected revision", r["ok"],
          smoke.describe(r))
    r = smoke.boot(good, timeout=60, expect_sha="f" * 40)
    check("a different revision than expected fails", not r["ok"] and "expected" in r["why"],
          r["why"])
    # The switch cannot point a real install's token anywhere but loopback.
    env = smoke.boot_env(Path(tempfile.mkdtemp()), "https://evil.example/api/", 1, 2)
    p = subprocess.run([sys.executable, str(good / "bot.py")], env=env, cwd=env["HOME"],
                       capture_output=True, text=True, timeout=60)
    check("SILKWORM_OFFLINE refuses a stand-in that is not on loopback",
          p.returncode != 0 and "loopback" in p.stderr, p.stderr[-300:])


def _late_names(src: str) -> list:
    """Names a module's top level reads before anything has bound them --
    793f119's bug, found without running anything. Function and lambda bodies
    run later and are skipped; comprehension variables are their own."""
    import builtins
    tree = ast.parse(src)
    bound = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
    out = []

    def binds(node):
        names = set()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return {node.name}
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return {(a.asname or a.name).split(".")[0] for a in node.names}
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                names.add(n.id)
            elif isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                names.add(n.name)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                names |= {(a.asname or a.name).split(".")[0] for a in n.names}
            elif isinstance(n, ast.ExceptHandler) and n.name:
                names.add(n.name)
        return names

    def evaluated(node):
        stack, seen = [node], []
        while stack:
            n = stack.pop()
            if isinstance(n, ast.Lambda):
                stack.extend(n.args.defaults)
                continue
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                stack.extend(n.decorator_list + n.args.defaults
                             + [d for d in n.args.kw_defaults if d])
                continue
            if isinstance(n, ast.ClassDef):
                stack.extend(n.decorator_list + n.bases)
                continue
            if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                local = {t.id for g in n.generators for t in ast.walk(g.target)
                         if isinstance(t, ast.Name)}
                seen += [x for x in evaluated_all(n) if x.id not in local]
                continue
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                seen.append(n)
            stack.extend(ast.iter_child_nodes(n))
        return seen

    def evaluated_all(comp):
        out_ = []
        for child in ast.iter_child_nodes(comp):
            out_ += evaluated(child)
        return out_

    for stmt in tree.body:
        if isinstance(stmt, ast.If) and "__main__" in ast.unparse(stmt.test):
            continue
        local = binds(stmt) if isinstance(stmt, (ast.For, ast.With, ast.If, ast.Try)) else set()
        out += [(n.lineno, n.id) for n in evaluated(stmt)
                if n.id not in bound and n.id not in local]
        bound |= binds(stmt)
    return out


def test_module_level_reads_nothing_defined_below_it():
    print("\nbot.py and visualizer.py read nothing at import that is defined further down")
    for name in ("bot.py", "visualizer.py"):
        late = _late_names((BASE / name).read_text())
        check(f"{name}: every name its top level uses is bound above it", not late, late[:5])
    bad = _code_copy(_the_793f119_ordering)
    late = _late_names((bad / "bot.py").read_text())
    check("793f119's ordering is caught statically too",
          [n for _, n in late] == ["handle_ops"], late)
    check("a name used inside a function body is not (it runs later)",
          not _late_names("def f():\n    return g()\ndef g():\n    return 1\n"))
    check("nor a comprehension's own variable",
          not _late_names("xs = [1]\nys = [x * 2 for x in xs]\n"))


class _ProcManager:
    """launchd, for a test: restart() stops the bot and starts bot.py from the
    pinned checkout, logs where launchd would put them, and waits for it to
    answer (as LaunchdManager._report_up does). Slack is the stand-in."""

    def __init__(self, home: Path, port: int, slack_url: str, wait: float = 40):
        import smoke
        self.home, self.port, self.wait = home, port, wait
        self.env = smoke.boot_env(home, slack_url, port, smoke.spare_port())
        self.proc = None
        self.started = []

    def restart(self) -> bool:
        import live as L
        import smoke
        smoke._stop(self.proc)
        logs = self.home / "logs"
        logs.mkdir(exist_ok=True)
        code = L.code_dir(self.home)
        with open(logs / "bot.log", "ab") as so, open(logs / "bot.err.log", "ab") as se:
            self.proc = subprocess.Popen([sys.executable, str(code / "bot.py")],
                                         cwd=str(self.home), env=self.env, stdout=so, stderr=se,
                                         stdin=subprocess.DEVNULL, start_new_session=True)
        self.started.append(L.head(code))
        try:
            smoke._wait(self.proc, lambda: smoke._post(self.port, "/status").get("online"),
                        self.wait)
        except Exception:
            pass
        return self.proc.poll() is None and self._answers()

    def _answers(self) -> bool:
        import smoke
        try:
            return bool(smoke._post(self.port, "/status").get("online"))
        except Exception:
            return False

    def stop(self):
        import smoke
        smoke._stop(self.proc)


def test_a_promotion_that_does_not_boot_is_rolled_back():
    import live as L
    import outages as O
    import smoke
    cli = _load_cli()
    print("\na deploy onto a revision that raises at import rolls itself back")
    home = _code_copy(extra_commits=[("bad", _raise_at_import)])
    good, bad = _SHAS[home]
    old_live = os.environ.get("SILKWORM_LIVE_DIR")
    os.environ["SILKWORM_LIVE_DIR"] = str(Path(tempfile.mkdtemp()) / "live")
    slack = smoke.FakeSlack()
    port = smoke.spare_port()
    mgr = _ProcManager(home, port, slack.url)

    def call(path, payload):
        try:
            ans = smoke._post(port, path, timeout=5) if path == "/status" else {}
        except Exception:
            return {}
        # Offline on purpose (no Socket Mode): a boot that answered counts
        # as connected here; the revision it reports is the real one.
        if ans.get("online"):
            ans.setdefault("slack", {})["connected"] = True
        return ans
    try:
        ok, _ = L.checkout(home, good, why="test")
        check("the good revision is pinned and boots", ok and mgr.restart(), L.read(home))
        L.mark_good(home, good, "test")
        real = L.Pin(home)
        ok, detail = real.boot(bad, timeout=60)
        check("the real clean-checkout boot refuses the bad commit before any restart",
              not ok and "cannot boot" in detail and mgr.started == [good], detail[-300:])
        ok, detail = real.boot(good, timeout=60)
        check("and passes the good one", ok, detail)

        class PassesCleanBoot(L.Pin):
            # One that boots alone and fails on the real port (its config,
            # its port, its Slack): the rollback is the guard for that.
            def boot(self, sha, timeout=90):
                return True, "booted"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code, why = cli.deploy_onto(call, bad, now=True, max_wait=60, manager=mgr,
                                        pin=PassesCleanBoot(home), confirm_s=15)
        text = out.getvalue()
        check("the deploy fails", code == 1, why)
        check("and says both outcomes: the bad one did not come up, the good one is back",
              f"{bad[:8]} did not come up" in why
              and f"rolled back to {good[:8]}, which is answering again" in why, why)
        check("the bad revision was really started, then the good one again",
              mgr.started[-2:] == [bad, good], mgr.started)
        st = call("/status", {})
        check("the old revision is answering again",
              (st.get("revision") or {}).get("started") == good, json.dumps(st.get("revision")))
        rec = L.read(home)
        check("the pin is back on it, still the last known good",
              rec.get("live") == good and rec.get("last_good") == good, rec)
        rows = O.load(home)
        check("the outage is recorded: ended, why, and what was rolled back",
              len(rows) == 1 and rows[0]["end"] and rows[0]["seen_by"] == "deploy"
              and "smoke: this revision cannot boot" in rows[0]["cause"]
              and rows[0]["rolled_back"] == {**rows[0]["rolled_back"], "from": bad,
                                             "to": good, "ok": True}, rows)
        check("the terminal shows the traceback", "cannot boot" in text, text[-500:])
    finally:
        mgr.stop()
        slack.close()
        if old_live is None:
            os.environ.pop("SILKWORM_LIVE_DIR", None)
        else:
            os.environ["SILKWORM_LIVE_DIR"] = old_live


class _Pin:
    def __init__(self, good="g" * 40, fail_checkout=(), fail_boot=()):
        self.good, self.fail_checkout = good, set(fail_checkout)
        self.fail_boot = set(fail_boot)
        self.checked, self.marked, self.outages, self.booted = [], [], [], []

    def boot(self, sha, timeout=90):
        self.booted.append(sha)
        return (sha not in self.fail_boot), "did not boot: bot.py exited\n    NameError: x"

    def checkout(self, sha, why=""):
        self.checked.append(sha)
        return (sha not in self.fail_checkout), "/live"

    def last_good(self):
        return self.good

    def mark_good(self, sha, by="deploy"):
        self.marked.append(sha)

    def record_outage(self, entry):
        self.outages.append(entry)

    def stderr_tail(self, n=20):
        return "Traceback\nNameError: boom"


def test_deploy_rollback_paths():
    cli = _load_cli()
    print("\ndeploy: promote, confirm, or roll back -- every branch")
    new, good = "n" * 40, "g" * 40

    def bot(running_after, before=good):
        state = {"running": before, "drained": False, "stops": 0}

        def call(path, payload):
            if path == "/status":
                if state["running"] is None:
                    return {}
                return {"online": True, "slack": {"connected": True},
                        "revision": {"started": state["running"]}}
            if path == "/drain":
                if payload.get("action") == "start":
                    state["drained"] = True
                if payload.get("action") == "stop":
                    state["stops"] += 1
                return {"ok": True, "draining": state["drained"], "running": []}
            return {}
        seq = list(running_after)

        def restart():
            state["running"] = seq.pop(0) if seq else None
            return state["running"] is not None
        return call, types.SimpleNamespace(restart=restart), state

    def run(running_after, pin):
        call, mgr, state = bot(running_after)
        with contextlib.redirect_stdout(io.StringIO()):
            code, why = cli.deploy_onto(call, new, now=True, max_wait=60, manager=mgr, pin=pin,
                                        confirm_s=0, sleep=lambda s: None)
        return code, why, state

    pin = _Pin()
    code, why, _ = run([new], pin)
    check("a revision that comes up is promoted and becomes last known good",
          code == 0 and pin.checked == [new] and pin.marked == [new] and not pin.outages, why)

    pin = _Pin()
    code, why, _ = run([None, good], pin)
    check("one that never answers is rolled back to the last known good, which answers",
          code == 1 and pin.checked == [new, good] and "answering again" in why
          and pin.outages and pin.outages[0]["end"], why)

    pin = _Pin()
    code, why, _ = run([good, good], pin)
    check("one that answers but runs the wrong revision is rolled back too",
          code == 1 and pin.checked == [new, good] and "rolled back" in why, why)

    pin = _Pin()
    code, why, _ = run([None, None], pin)
    check("a rollback that fails too is reported as down, and the outage left open",
          code == 1 and "failed too" in why and "Silkworm is down" in why
          and pin.outages and pin.outages[0]["end"] is None, why)

    pin = _Pin(good="")
    call, mgr, _ = bot([None], before="")
    with contextlib.redirect_stdout(io.StringIO()):
        code, why = cli.deploy_onto(call, new, now=True, max_wait=60, manager=mgr, pin=pin,
                                    confirm_s=0, sleep=lambda s: None)
    check("with nothing known good it says so rather than inventing a target",
          code == 1 and "no known good revision" in why and pin.checked == [new]
          and pin.outages and pin.outages[0]["end"] is None, why)
    # (the running revision -- `good` from /status -- would have been the
    # target; this bot reports none once restart has taken it down, so the
    # deploy read it before restarting: covered below.)
    pin = _Pin(good="")
    call, mgr, _ = bot([None, good])
    with contextlib.redirect_stdout(io.StringIO()):
        code, why = cli.deploy_onto(call, new, now=True, max_wait=60, manager=mgr, pin=pin,
                                    confirm_s=0, sleep=lambda s: None)
    check("with no record, what was running before the restart is the fallback",
          pin.checked == [new, good] and "answering again" in why, why)

    pin = _Pin(good="")
    call, mgr, _ = bot([None])
    dirty = lambda path, payload: (dict(call(path, payload), revision={
        "started": good, "dirty": True}) if path == "/status" and call(path, payload) else
        call(path, payload))
    with contextlib.redirect_stdout(io.StringIO()):
        code, why = cli.deploy_onto(dirty, new, now=True, max_wait=60, manager=mgr, pin=pin,
                                    confirm_s=0, sleep=lambda s: None)
    check("but never a revision that booted from a dirty tree: its commit is not what ran",
          pin.checked == [new] and "no known good revision" in why, why)

    pin = _Pin(fail_boot=[new])
    code, why, state = run([new], pin)
    check("a commit that does not boot on its own is never pinned or restarted onto",
          code == 1 and "does not boot" in why and pin.booted == [new] and not pin.checked
          and state["running"] == good and state["stops"] == 1, why)

    pin = _Pin(fail_checkout=[new])
    code, why, state = run([new], pin)
    check("a pin that cannot be checked out restarts nothing and lifts the drain",
          code == 1 and "could not pin" in why and state["stops"] == 1
          and state["running"] == good, why)

    # The real entry point promotes through the install's pin; a test that
    # injects only a manager gets none, so it never touches a real checkout.
    src = (BASE / "bin" / "silkworm").read_text()
    check("do_deploy pins only when it is driving the real services",
          "if pin is None and manager is None:\n        pin = live.Pin(repo)" in src)
    check("the detached deploy pins too, and can alert directly",
          "pin=live.Pin(REPO)," in src and "alerts.owner(" in src.split(
              "def do_after_turn_detached", 1)[1])


def _wd_world(home, status_seq, *, installed=True, deploying=False):
    import watchdog as W

    class World(W.World):
        def __init__(self):
            super().__init__(home)
            self.t = 1_000_000.0
            self.calls, self.alerts, self.checkouts = [], [], []
            self.seq = list(status_seq)

        def now(self):
            return self.t

        def status(self):
            return self.seq.pop(0) if self.seq else None

        def launchctl(self, *args):
            self.calls.append(args)
            if args[0] == "print":
                return (0 if installed else 113), ""
            return 0, ""

        def deploying(self):
            return deploying

        def checkout(self, sha):
            self.checkouts.append(sha)
            import live as L
            rec = L.read(home)
            rec["live"] = sha
            L.write(home, rec)
            return True, "/live"

        def stderr_tail(self, n=20):
            return "Traceback (most recent call last):\nNameError: name 'handle_ops' is not defined"

        def alert(self, text):
            self.alerts.append(text)
            return {"slack": True, "push": True}

        def log(self, line):
            pass
    return World()


def test_watchdog_rolls_back_once_and_alerts():
    import live as L
    import outages as O
    import watchdog as W
    print("\nthe watchdog: down for N checks -> outage, one rollback, one alert")
    home = Path(tempfile.mkdtemp())
    live_dir = Path(tempfile.mkdtemp()) / "live"
    (live_dir / ".git").mkdir(parents=True)
    (live_dir / "bot.py").write_text("")
    old_live = os.environ.get("SILKWORM_LIVE_DIR")
    os.environ["SILKWORM_LIVE_DIR"] = str(live_dir)
    try:
        bad, good = "b" * 40, "a" * 40
        L.write(home, {"live": bad, "last_good": good})
        up = {"online": True, "pid": 7, "revision": {"started": good}}
        w = _wd_world(home, [up, None, None, None, None, None])
        r = [W.check(home, w, n=3)]
        check("an answering bot is healthy", r[0]["state"] == "up" and not w.alerts, r)
        for _ in range(2):
            w.t += 120
            r.append(W.check(home, w, n=3))
        check("one or two missed checks are only suspicion -- nothing done",
              [x["state"] for x in r[1:]] == ["suspect", "suspect"]
              and not w.checkouts and not w.alerts and not O.load(home), r)
        w.t += 120
        r.append(W.check(home, w, n=3))
        kick = [c for c in w.calls if c[0] == "kickstart"]
        check("the third rolls back to the last known good and restarts both services",
              w.checkouts == [good] and len(kick) == 2
              and all(c[1] == "-k" for c in kick), (w.checkouts, w.calls))
        rows = O.load(home)
        check("and records the outage from the first failed check, with the traceback",
              len(rows) == 1 and rows[0]["start"] == 1_000_120.0 and rows[0]["end"] is None
              and "handle_ops" in rows[0]["cause"]
              and rows[0]["rolled_back"]["to"] == good, rows)
        check("and alerts the owner, saying what it did",
              len(w.alerts) == 1 and "Silkworm is down" in w.alerts[0]
              and f"→ {good[:8]}" in w.alerts[0] and "handle_ops" in w.alerts[0], w.alerts)
        for _ in range(4):
            w.t += 120
            W.check(home, w, n=3)
        check("still down: never a second rollback, and no repeat alert inside the window",
              w.checkouts == [good] and len(w.alerts) == 1, (w.checkouts, w.alerts))
        w.t += W.REALERT_S
        w.seq = [None]
        W.check(home, w, n=3)
        check("past the window it says so again (still no second rollback)",
              len(w.alerts) == 2 and w.checkouts == [good])
        w.t += 120
        w.seq = [dict(up, pid=8)]
        res = W.check(home, w, n=3)
        rows = O.load(home)
        check("back up: the outage is closed and the owner told",
              res["state"] == "up" and rows[-1]["end"] == w.t and len(w.alerts) == 3
              and "answering again" in w.alerts[-1], (rows, w.alerts))
        st = W._load(home)
        check("and its counters reset for the next outage",
              not st.get("down") and not st.get("rolled_back") and not st.get("alerted_at"), st)

        # Already on the last known good: nothing to roll back to.
        w2 = _wd_world(home, [None, None, None])
        for _ in range(3):
            w2.t += 120
            W.check(home, w2, n=3)
        check("down on the last known good itself: alerts, does not roll back",
              not w2.checkouts and len(w2.alerts) == 1
              and "is the last known good revision already" in w2.alerts[0], w2.alerts)
        O.close(home, end=w2.t, how="test")
        W._save(home, {})

        # A deploy in progress owns its own rollback.
        L.write(home, {"live": bad, "last_good": good})
        w3 = _wd_world(home, [None] * 3, deploying=True)
        for _ in range(3):
            w3.t += 120
            res = W.check(home, w3, n=3)
        check("while a deploy holds its lock it leaves the bot to the deploy",
              res["state"] == "deploy-running" and not w3.checkouts and not w3.alerts, res)
        W._save(home, {})

        # Uninstalled is not down.
        w4 = _wd_world(home, [None] * 5, installed=False)
        for _ in range(5):
            res = W.check(home, w4, n=3)
        check("with the services not loaded there is nothing to watch",
              res["state"] == "not-installed" and not w4.alerts and not w4.checkouts)

        # A rollback whose checkout fails leaves the pin where it was: the
        # once-per-outage rule, not the "already there" one, stops a retry.
        W._save(home, {})
        L.write(home, {"live": bad, "last_good": good})
        w6 = _wd_world(home, [None] * 6)
        w6.checkout = lambda sha: (w6.checkouts.append(sha), (False, "git said no"))[1]
        gap, W.ROLLBACK_GAP_S = W.ROLLBACK_GAP_S, 0     # this rule alone, not the day's gap
        try:
            for _ in range(6):
                w6.t += 120
                W.check(home, w6, n=3)
        finally:
            W.ROLLBACK_GAP_S = gap
        check("a rollback that failed is not retried every check",
              w6.checkouts == [good] and "that failed" in w6.alerts[0], (w6.checkouts, w6.alerts))
        O.close(home, end=w6.t, how="test")

        # Up again, then down again within the day: still only one rollback.
        w7 = _wd_world(home, [up] + [None] * 3)
        w7.t = w6.t + 120
        for _ in range(4):
            W.check(home, w7, n=3)
            w7.t += 120
        check("a second outage within a day of a rollback does not roll back again",
              not w7.checkouts and "within the last day" in w7.alerts[-1], w7.alerts)
        O.close(home, end=w7.t, how="test")

        # A revision that passed its deploy (so last_good is it) and fails
        # later: back to the one good before it.
        W._save(home, {})
        older = "c" * 40
        L.write(home, {"live": good, "last_good": good, "previous_good": older})
        w8 = _wd_world(home, [None] * 3)
        w8.t = w7.t + 2 * W.ROLLBACK_GAP_S
        for _ in range(3):
            w8.t += 120
            W.check(home, w8, n=3)
        check("one that passed its deploy and failed later goes back to the one before it",
              w8.checkouts == [older], (w8.checkouts, w8.alerts))
        O.close(home, end=w8.t, how="test")
        L.write(home, {"live": bad, "last_good": good})

        # A crash loop: it answers, but a new process every check.
        W._save(home, {})
        loop = [dict(up, pid=p) for p in (10, 11, 12, 13)]
        w5 = _wd_world(home, loop)
        for _ in range(4):
            w5.t += 120
            res = W.check(home, w5, n=3)
        check("a bot restarting every check is treated as down",
              res["state"] == "down" and "crash loop" in res["what"] and w5.checkouts == [good],
              res)
    finally:
        if old_live is None:
            os.environ.pop("SILKWORM_LIVE_DIR", None)
        else:
            os.environ["SILKWORM_LIVE_DIR"] = old_live


def test_watchdog_alerts_reach_slack_and_push_without_the_bot():
    import alerts as A
    import smoke
    import watchdog as W
    print("\nthe watchdog's alert goes straight to Slack and the phone")
    home = Path(tempfile.mkdtemp())
    (home / ".env").write_text("SLACK_BOT_TOKEN=xoxb-" + "from-env-file\n")
    (home / "sessions.json").write_text(json.dumps(
        {"D0OLD:1.0": {"updated": 1}, "D0NEW:2.0": {"updated": 5}, "C0CHAN:3.0": {"updated": 9}}))
    check("the home DM is the most recently active DM, as the bot picks it",
          A.home_channel(home) == "D0NEW")
    (home / ".env").write_text("SLACK_BOT_TOKEN=xoxb-" + "from-env-file\nSILKWORM_HOME_CHANNEL=D0SET\n")
    check("unless SILKWORM_HOME_CHANNEL names one", A.home_channel(home) == "D0SET")

    # chat.postMessage, for real, against a loopback stand-in.
    got = []
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading as _t

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            got.append((self.path, self.headers.get("Authorization"),
                        json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    _t.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/api/chat.postMessage"
    pushed = []
    res = A.owner(home, "Silkworm is down", post=lambda tok, ch, text, ts="": A.slack_post(
        tok, ch, text, ts, url=url), push=lambda h, t, b: pushed.append((t, b)) or (True, "1"))
    srv.shutdown()
    check("it posts with the bot token from the install's .env, to the home DM",
          res["slack"] and got and got[0][1] == "Bearer xoxb-" + "from-env-file"
          and got[0][2]["channel"] == "D0SET" and got[0][2]["text"] == "Silkworm is down", got)
    check("and pushes to the phone", pushed == [("Silkworm", "Silkworm is down")], pushed)
    res = A.owner(home, "x", thread="C9:1.5", post=lambda tok, ch, text, ts="": (
        got.append((ch, ts)) or (True, "")), push=lambda *a: (False, ""))
    check("a deploy's note goes to its own thread instead", got[-1] == ("C9", "1.5"))
    check("a Slack post that cannot connect is reported, not raised",
          A.slack_post("xoxb-" + "x", "D1", "t", url=f"http://127.0.0.1:{smoke.spare_port()}/x")[0]
          is False)

    ran = []
    ok, _ = A.push_send(home, "T", "B", run=lambda argv, **k: ran.append(argv) or
                        types.SimpleNamespace(returncode=0, stdout="sent 1", stderr=""))
    check("the push is push.py's own direct send, in its own process",
          ok and ran[0][1].endswith("push.py") and ran[0][2:] ==
          ["alert", "--home", str(home), "--title", "T", "--body", "B"], ran)

    # push.py alert, for real: one subscription, a fake push service.
    import push
    vapid = Path(tempfile.mkdtemp())
    old = os.environ.get("SILKWORM_VAPID_DIR")
    os.environ["SILKWORM_VAPID_DIR"] = str(vapid)
    try:
        push.ensure_keys(vapid)
        priv, sub = _ua_subscription()
        push.PushStore(home / "push.json").add(sub)
        sent = []
        with contextlib.redirect_stdout(io.StringIO()):
            rc = push.alert_main(["--home", str(home), "--title", "Silkworm is down",
                                  "--body", "rolled back"],
                                 sender=lambda ep, body, auth, urg: sent.append(body) or 201)
        check("push.py alert sends to every subscribed device without the bot",
              rc == 0 and len(sent) == 1 and json.loads(
                  _ua_decrypt(sent[0], priv, sub["keys"]["auth"]))["title"] == "Silkworm is down")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = push.alert_main(["--home", str(Path(tempfile.mkdtemp())), "--body", "x"])
        check("with no device it says so and exits non-zero", rc == 1)
    finally:
        if old is None:
            os.environ.pop("SILKWORM_VAPID_DIR", None)
        else:
            os.environ["SILKWORM_VAPID_DIR"] = old

    # The real World's launchctl calls, with launchctl faked.
    calls = []

    class Fake(W.World):
        def launchctl(self, *a):
            calls.append(a)
            return 0, ""
    w = Fake(home)
    w.restart()
    w.installed()
    check("a rollback kickstarts -k the bot and the visualizer",
          [c[:2] for c in calls[:2]] == [("kickstart", "-k")] * 2
          and calls[0][2].endswith("/com.silkworm.bot") and calls[1][2].endswith("/com.silkworm.viz"),
          calls)
    check("and 'installed' is launchctl print of the bot's job",
          calls[2][0] == "print" and calls[2][1].endswith("/com.silkworm.bot"), calls)
    lock = home / "logs" / "deploy-after-turn.lock"
    lock.parent.mkdir(exist_ok=True)
    lock.write_text(json.dumps({"pid": os.getpid()}))
    check("a deploy lock held by a live process is a deploy running", w.deploying())
    lock.write_text(json.dumps({"pid": 2 ** 22 + 12345}))
    check("one whose process is gone is not", not w.deploying())


def test_watchdog_is_its_own_launchd_job():
    cli = _load_cli()
    print("\nthe watchdog is a StartInterval job of its own, and the services run the pin")
    mgr = cli.LaunchdManager()
    wd = mgr._watchdog_plist()
    check("StartInterval, not KeepAlive", "<key>StartInterval</key><integer>120</integer>" in wd
          and "KeepAlive" not in wd, wd)
    check("it runs watchdog.py from the install, not from the pinned checkout",
          f"<string>{cli.REPO / 'watchdog.py'}</string>" in wd)
    bot_plist = mgr._plist("com.silkworm.bot", "bot.py")
    check("the bot's job is told where the install is (SILKWORM_HOME)",
          f"<key>SILKWORM_HOME</key><string>{cli.REPO}</string>" in bot_plist)
    import live as L
    check("and runs the pinned checkout once there is one",
          f"<string>{L.code_dir(cli.REPO) / 'bot.py'}</string>" in bot_plist)
    if shutil.which("plutil"):
        d = Path(tempfile.mkdtemp())
        ok = True
        for name, text in (("w.plist", wd), ("b.plist", bot_plist)):
            (d / name).write_text(text)
            ok &= subprocess.run(["plutil", "-lint", str(d / name)],
                                 capture_output=True).returncode == 0
        check("both are valid plists", ok)
    src = (BASE / "watchdog.py").read_text()
    tree = ast.parse(src)
    imported = ({a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
                 for a in n.names}
                | {n.module.split(".")[0] for n in ast.walk(tree)
                   if isinstance(n, ast.ImportFrom) and n.module})
    allowed = set(sys.stdlib_module_names) | {"alerts", "live", "outages", "jsonstore",
                                               "envfile", "smoke", "__future__"}
    check("it imports only the stdlib and this checkout's stdlib-only modules",
          imported <= allowed, imported - allowed)
    for mod in ("alerts", "live", "outages", "jsonstore", "envfile", "smoke"):
        src = (BASE / f"{mod}.py").read_text()
        imp = {n.names[0].name.split(".")[0] for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.Import)} | {n.module.split(".")[0] for n in
                                                ast.walk(ast.parse(src))
                                                if isinstance(n, ast.ImportFrom) and n.module}
        check(f"{mod}.py needs nothing outside the stdlib", imp <= allowed, imp - allowed)
    check("bot.py is not where it lives", "import watchdog" not in
          (BASE / "bot.py").read_text())


def test_pinned_checkout_moves_only_by_promotion():
    import live as L
    print("\nthe pinned checkout: created, moved, cleaned, read back")
    home = _code_copy(extra_commits=[("two", lambda d: (d / "NEWFILE").write_text("x"))])
    one, two = _SHAS[home]
    old = os.environ.get("SILKWORM_LIVE_DIR")
    pin_dir = Path(tempfile.mkdtemp()) / "live"
    os.environ["SILKWORM_LIVE_DIR"] = str(pin_dir)
    try:
        check("before any promotion the install runs from itself",
              L.code_dir(home) == home and not L.read(home))
        ok, d = L.checkout(home, one)
        check("the first checkout creates it, detached at the revision",
              ok and L.head(pin_dir) == one and L.code_dir(home) == pin_dir, d)
        (pin_dir / "stray.py").write_text("edit")
        (pin_dir / "bot.py").write_text("hand patched")
        ok, _ = L.checkout(home, two)
        check("moving it discards hand edits and strays, so what runs is the commit",
              ok and L.head(pin_dir) == two and not (pin_dir / "stray.py").exists()
              and (pin_dir / "bot.py").read_text() == (home / "bot.py").read_text())
        st = subprocess.run(["git", "-C", str(pin_dir), "status", "--porcelain"],
                            capture_output=True, text=True).stdout
        check("and it is clean", st == "", st)
        ok, why = L.checkout(home, "e" * 40)
        check("an unknown revision is refused and the pin stays put",
              not ok and L.head(pin_dir) == two and L.read(home)["live"] == two, why)

        def lying_git(cwd, *args, timeout=0):
            # Says yes to the checkout, and HEAD is still where it was.
            return (0, two, "") if args[:1] == ("rev-parse",) else (0, "", "")
        ok, why = L.checkout(home, one, git=lying_git)
        check("a checkout git claims but HEAD does not show is refused, unrecorded",
              not ok and "not " + one[:8] in why and L.read(home)["live"] == two, why)
        L.mark_good(home, one)
        L.mark_good(home, two)
        rec = L.read(home)
        check("last known good and the one before it are recorded",
              rec["last_good"] == two and rec["previous_good"] == one
              and [h["sha"] for h in rec["history"]] == [one, two], rec)
        main_head = subprocess.run(["git", "-C", str(home), "rev-parse", "--abbrev-ref", "HEAD"],
                                   capture_output=True, text=True).stdout.strip()
        check("the install's own checkout is untouched (still on main)", main_head == "main")
    finally:
        if old is None:
            os.environ.pop("SILKWORM_LIVE_DIR", None)
        else:
            os.environ["SILKWORM_LIVE_DIR"] = old


def test_outages_reach_status_digest_and_health():
    import digest as D
    import outages as O
    cli = _load_cli()
    print("\noutages: recorded by whoever saw them, reported once the bot is back")
    home = Path(tempfile.mkdtemp())
    now = time.time()
    O.open_(home, start=now - 3 * 86400, cause="NameError: x xoxb-" + "123456789-abc",
            seen_by="watchdog", revision="b" * 40)
    again = O.open_(home, start=now, cause="other", seen_by="watchdog")
    check("one outage, however many see it", len(O.load(home)) == 1 and again["cause"] != "other")
    check("tokens are scrubbed from the cause", "xoxb-" + "123" not in json.dumps(O.load(home)))
    O.update_open(home, rolled_back={"from": "b" * 40, "to": "a" * 40, "ok": True})
    O.close(home, end=now - 3600, how="watchdog")
    line = O.describe(O.load(home)[0], now)
    check("described: how long, why, what was rolled back",
          "down 3.0d" in line and "NameError" in line and "bbbbbbbb → aaaaaaaa" in line, line)
    text = D.render([], now, outages=O.load(home))
    lines = text.splitlines()
    check("the digest says it first, even on a day with nothing else",
          len(lines) > 1 and lines[1].startswith(":rotating_light: Silkworm was down"), text)
    check("an outage that ended before the window is not repeated",
          "down" not in D.render([], now + 2 * 86400, outages=O.load(home)))
    rec = cli.install_record(home)
    check("status reads it from the install, so it works with the bot down",
          rec["outages"] and not rec["outage_open"] and "NameError" in rec["outages"][-1]["line"])
    out = io.StringIO()

    def unreachable(path, payload, timeout=10):
        raise cli.BotUnreachable("did not answer")
    with contextlib.redirect_stdout(out):
        rc = cli.do_status_json(ask=unreachable, home=home)
    doc = json.loads(out.getvalue())
    check("status --json with the bot down still carries the outages and the pin",
          rc == cli.EXIT_UNREACHABLE and doc["outages"] and "live" in doc, doc)


def test_health_warns_on_uncommitted_running_code():
    P = _probes()
    from health import OK, WARN
    print("\nthe mini's health: running uncommitted code is a warning")
    sha = "a" * 40

    def run(r):
        return P.make_revision(running=lambda: r)({"slug": "mini"}, None)
    res = run({"sha": sha, "dirty": True, "code_dir": "/x"})
    check("a dirty boot warns (the hand-patched live tree)", res["status"] == WARN
          and "uncommitted" in res["summary"], res)
    res = run({"sha": "", "dirty": False})
    check("no commit at all warns", res["status"] == WARN, res)
    res = run({"sha": sha, "dirty": False, "live": {"live": "b" * 40}})
    check("running something other than the pin warns", res["status"] == WARN
          and "pinned" in res["summary"], res)
    res = run({"sha": sha, "dirty": False, "home": "/i", "code_dir": "/i", "live": {}})
    check("running from the install itself, unpinned, warns", res["status"] == WARN, res)
    res = run({"sha": sha, "dirty": False, "home": "/i", "code_dir": "/l",
               "live": {"live": sha}})
    check("a clean commit from the pin is ok", res["status"] == OK, res)
    check("bot.py hands the probe what it booted",
          "health_probes.RUNNING = lambda: {**REVISION" in (BASE / "bot.py").read_text())


def test_deploy_outcome_goes_direct_when_the_bot_cannot_post():
    cli = _load_cli()
    print("\na deploy's outcome is posted directly when the bot will not take it")
    box = Path(tempfile.mkdtemp())
    sent = []
    with contextlib.redirect_stdout(io.StringIO()):
        where = cli.report_outcome(lambda p, b: {}, "C1:2.0", "deploy failed", outbox=box,
                                   sleep=lambda s: None,
                                   direct=lambda th, t: sent.append((th, t)) or {"slack": True})
    check("straight to the thread, not left in outbox/",
          where == "direct" and sent == [("C1:2.0", "deploy failed")] and not any(box.iterdir()))
    with contextlib.redirect_stdout(io.StringIO()):
        where = cli.report_outcome(lambda p, b: {}, "C1:2.0", "x", outbox=box,
                                   sleep=lambda s: None, direct=lambda th, t: {"slack": False})
    check("and only when that fails too, the outbox", where == "outbox")

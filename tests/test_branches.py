"""Worktrees and branches: checkouts, unmerged work, discards, generated files.

Run by tests/run.py with every other tests/test_*.py; see tests/harness.py."""

from harness import *  # noqa: F401,F403


# --- tasks run in parallel, and each gets its own checkout -----------------------
# Three concurrent tasks: one got a worktree and two silently fell back to
# sharing the main checkout, losing both isolation and the parallelism. The
# branch survived each failure, so the retry could never work.

def test_parallel_tasks():
    import worktrees as W
    print("\nparallel workers, one checkout each")

    root = Path(tempfile.mkdtemp())
    W.ROOT = root / "wts"
    repo = root / "repo"; repo.mkdir()
    def git(cwd, *a): return subprocess.run(["git", *a], cwd=str(cwd),
                                            capture_output=True, text=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("base\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-q", "-b", "research")
    (repo / "r.txt").write_text("research\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "research work")
    git(repo, "checkout", "-q", "main")

    # A project is not always working on main.
    d = W.create(repo, "tsk_d", fetch=False)
    check("the default base is the default branch",
          not (d / "r.txt").exists())
    r = W.create(repo, "tsk_r", fetch=False, base="research")
    check("a project can name the branch its work builds on",
          (r / "r.txt").exists(),
          "branching the trader off main would drop 18 commits of research")
    m = W.create(repo, "tsk_m", fetch=False, base="no-such-branch")
    check("an unknown base falls back rather than failing the task",
          m is not None and not (m / "r.txt").exists())

    # The bug: `worktree add -b` makes the branch first and the tree second, so
    # a failure at the second step leaves the branch behind and every retry
    # then dies on "a branch named X already exists".
    src = (BASE / "worktrees.py").read_text()
    create = src[src.index("def create("):src.index("def main_repo")]
    check("a failed creation deletes the branch it made",
          create.count("discard.drop(repo, branch)") == 2,
          "otherwise the retry fails forever, and a stale branch blocks the next run")
    check("and goes through discard rather than `branch -D`",
          '"branch", "-D"' not in create,
          "one reason `worktree add -b` fails is that the branch already exists, "
          "and then a bare -D deletes finished work")
    check("creation is serialised per repository",
          "with _repo_create_lock(repo):" in create,
          "concurrent `worktree add` on one repo makes all but one fail")
    check("the fetch happens inside that lock too",
          create.index("_repo_create_lock") < create.index("base_ref("),
          "concurrent fetches contend on the same refs")
    check("the real error is logged, not the progress line",
          '(r.stderr or "").strip()[-300:]' in create,
          'git writes "Preparing worktree..." to stderr, which hid the cause')

    # Six at once, which is what surfaced it.
    import threading
    out = {}
    def go(n): out[n] = W.create(repo, f"tsk_c{n}", fetch=False)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    [t.start() for t in ts]; [t.join(90) for t in ts]
    check("six concurrent creations all succeed",
          sum(1 for v in out.values() if v) == 6,
          f"got {sum(1 for v in out.values() if v)}/6")
    check("and each is a distinct checkout",
          len({str(v) for v in out.values() if v}) == 6)

    bot = (BASE / "bot.py").read_text()
    check("workers are separate from the scheduler",
          "def _task_worker" in bot and "def _task_scheduler" in bot,
          "blocked -> queued is a transition; racing workers would have all "
          "but one refused")
    check("how many run at once is configurable",
          'os.environ.get("TASK_WORKERS"' in bot)
    check("the count is logged at startup, like the other limits",
          "task workers: %d" in bot)
    check("a task's base branch reaches the worktree -- its project's, as it is now",
          "worktrees.create(cwd, tid, base=task_base(task))" in bot)


# --- finished work that never reached the base -------------------------------
# Eight tasks completed, the board recorded eight `done`, and eight fixes sat on
# eight branches nobody merged -- so none of those bugs were fixed in the bot
# that was actually running. Two of the eight were the same fix, implemented
# twice on different nights, because the first never landed and the gap was
# still there for the next pass to find.

def test_pruning_merged_branches():
    import branches as B
    import tasks as T
    import worktrees as W
    print("\nfinished branches already in the base are pruned, and nothing else")

    root = Path(tempfile.mkdtemp())
    old_root = W.ROOT
    W.ROOT = root / "worktrees"
    repo = root / "repo"; repo.mkdir()

    def git(cwd, *a):
        return subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True)

    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("hello\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "base")

    def rec(tid, state=T.DONE):
        r = T.make(f"do {tid}", state=T.QUEUED, scope={"cwd": str(repo)})
        r["id"] = tid
        r["state"] = state
        return r

    def work(tid):
        wt = W.create(repo, tid, fetch=False)
        (Path(wt) / f"{tid}.txt").write_text(tid)
        git(wt, "add", "-A"); git(wt, "commit", "-qm", f"work {tid}")
        W.release(wt)
        return f"silkworm/{tid}"

    def has(branch):
        return git(repo, "rev-parse", "--verify", "--quiet",
                   f"refs/heads/{branch}").returncode == 0

    try:
        # The case that prompted this: landed by hand, so nothing tidied it.
        landed = work("tsk_hand")
        git(repo, "merge", "--ff-only", "-q", landed)
        kept = work("tsk_open")
        pruned = B.prune_merged([rec("tsk_hand"), rec("tsk_open")])
        check("a done task's branch that is in the base is pruned",
              pruned == [landed] and not has(landed), f"got {pruned}")
        check("without a tag, since the base already holds it",
              git(repo, "tag", "-l", "discarded/*").stdout.strip() == "",
              "a tag per landing would be clutter of its own")
        check("a done task's branch with work not in the base is kept",
              has(kept), "that is stranded work, not litter")

        # Anything that can still move may come back for its branch.
        for state in (T.AWAITING_APPROVAL, T.NEEDS_INPUT, T.FAILED, T.RUNNING,
                      T.QUEUED, T.BLOCKED, T.PROPOSED):
            b = work(f"tsk_{state}")
            git(repo, "merge", "--ff-only", "-q", b)
            B.prune_merged([rec(f"tsk_{state}", state=state)])
            check(f"a merged branch of a task in {state} is left alone", has(b))
        b = work("tsk_cancel")
        git(repo, "merge", "--ff-only", "-q", b)
        B.prune_merged([rec("tsk_cancel", state=T.CANCELLED)])
        check("a cancelled task's merged branch is pruned", not has(b))

        # A git failure must read as "keep it". ahead() says 0 when git fails,
        # which is the survey's safe direction and a deleter's unsafe one.
        b = work("tsk_gitfail")
        real_git = B._git
        # Both ways of measuring fail, so the test holds against either.
        B._git = lambda cwd, *a, **k: (B._Failed() if a and a[0] in ("merge-base", "rev-list")
                                       else real_git(cwd, *a, **k))
        try:
            B.prune_merged([rec("tsk_gitfail")])
        finally:
            B._git = real_git
        check("a branch git could not measure is kept", has(b),
              "an unmeasured branch was deleted as if it were merged")

        # Checked out somewhere -- a landing in progress -- git refuses, and
        # the prune says so rather than raising.
        b = work("tsk_busy")
        git(repo, "merge", "--ff-only", "-q", b)
        here = W.attach(repo, "tsk_busy", b)
        try:
            B.prune_merged([rec("tsk_busy")])
            check("a merged branch checked out in a worktree is left", has(b))
        finally:
            W.release(here, delete_empty_branch=False)

        # Approve moves a task to done and *then* lands it. Between the
        # fast-forward and the post-merge suite the branch is in the base and
        # may be checked out nowhere, and if that suite fails the base is reset
        # -- so the branch is the only thing left holding the work.
        b = work("tsk_landing")
        git(repo, "merge", "--ff-only", "-q", b)
        B.prune_merged([rec("tsk_landing")], skip={"tsk_landing"})
        check("a done task whose landing is under way keeps its branch", has(b),
              "Approve lands after done; pruning mid-landing can lose the work")

        # One repository that raises must not cost the others their pass.
        other = root / "other"; other.mkdir()
        git(other, "init", "-q", "-b", "main")
        git(other, "config", "user.email", "t@t"); git(other, "config", "user.name", "t")
        (other / "o.txt").write_text("o\n")
        git(other, "add", "-A"); git(other, "commit", "-qm", "base")
        git(other, "branch", "silkworm/tsk_otherrepo")
        wedged = rec("tsk_hand")
        wedged["scope"] = {"cwd": str(repo)}
        fine = T.make("x", state=T.QUEUED, scope={"cwd": str(other)})
        fine["id"], fine["state"] = "tsk_otherrepo", T.DONE
        real_existing = B.existing
        B.existing = lambda r: ((_ for _ in ()).throw(RuntimeError("wedged"))
                                if Path(r) == repo else real_existing(r))
        try:
            got = B.prune_merged([wedged, fine])
        except Exception as e:
            got = e
        finally:
            B.existing = real_existing
        check("a repository that raises does not stop the others",
              got == ["silkworm/tsk_otherrepo"], f"got {got!r}")
    finally:
        W.ROOT = old_root


def test_the_sweeper_prunes_around_landings():
    import threading
    from unittest.mock import MagicMock
    print("\nthe sweeper prunes under the landing guard, skipping landings")

    class Stop(Exception):
        pass

    lock = threading.Lock()
    seen = []

    scopes = []

    def prune(records, skip=(), scope_for=None):
        seen.append((sorted(r["id"] for r in records), set(skip), lock.locked()))
        scopes.append(scope_for)
        return []

    def current_scope(slug):
        return {}

    board = {"tsk_live": {"id": "tsk_live", "state": "done"},
             "tsk_marked": {"id": "tsk_marked", "state": "done",
                            "result": {"landing": {"stage": "in-progress"}}},
             "tsk_idle": {"id": "tsk_idle", "state": "done",
                          "result": {"landing": {"stage": "done"}}}}
    fn = _bot_func("_worktree_sweeper",
                   worktrees=types.SimpleNamespace(sweep=lambda keep: 0),
                   live_worktree_tasks=lambda: set(),
                   branches=types.SimpleNamespace(prune_merged=prune),
                   project_store=types.SimpleNamespace(scope_for=current_scope),
                   task_store=types.SimpleNamespace(all=lambda: dict(board)),
                   _landing_guard=lock, _landing_now={"tsk_live"},
                   LANDING_UNDERWAY="in-progress", log=MagicMock(),
                   time=types.SimpleNamespace(
                       sleep=lambda s: (_ for _ in ()).throw(Stop())))
    try:
        fn()
    except Stop:
        pass
    check("the sweeper prunes merged branches", len(seen) == 1)
    check("against each project's current base, not the one its tasks recorded",
          scopes == [current_scope], str(scopes))
    if seen:
        ids, skip, held = seen[0]
        check("with every task on the board", ids == ["tsk_idle", "tsk_live", "tsk_marked"])
        check("skipping a landing running in this process", "tsk_live" in skip)
        check("and one the record says is under way", "tsk_marked" in skip)
        check("but not a finished one", "tsk_idle" not in skip)
        check("while holding the landing guard, so none can start mid-prune", held)


def test_unmerged_branches():
    import branches as B
    import tasks as T
    import worktrees as W
    print("\nfinished work on unmerged branches is visible")

    root = Path(tempfile.mkdtemp())
    old_root = W.ROOT
    W.ROOT = root / "worktrees"
    repo = root / "repo"; repo.mkdir()

    def git(cwd, *a):
        return subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True)

    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("hello\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "base")

    def finished(tid, state=T.DONE, **extra):
        rec = T.make(f"do {tid}", state=T.QUEUED, scope={"cwd": str(repo)}, **extra)
        rec["id"] = tid
        rec["state"] = state
        rec["title"] = f"work for {tid}"
        return rec

    def work(tid, text):
        wt = W.create(repo, tid, fetch=False)
        (Path(wt) / f"{tid}.txt").write_text(text)
        git(wt, "add", "-A"); git(wt, "commit", "-qm", f"work {tid}")
        return wt

    # One task that did work and stopped. This is the whole scenario.
    wt_a = work("tsk_aaa", "a")
    W.release(wt_a)
    rows = B.survey([finished("tsk_aaa")])
    check("a finished task's branch is named", len(rows) == 1)
    check("with its commit count", rows and rows[0]["commits"] == 1,
          "\"it is on a branch\" without a size is not enough to decide on")
    check("and the base it would go to", rows and rows[0]["base"] == "main")
    stale = finished("tsk_aaa")
    stale["base"] = "universe"
    stale_rows = B.survey([stale])
    check("the row names the base it measured, not one stored on the record",
          stale_rows and stale_rows[0]["base"] == "main",
          "printing one ref while counting against another is the bug itself")
    check("the summary line counts them", "1 finished task" in B.line(rows))

    # Merging it must make it disappear without anything telling us so: merge
    # state is asked of git, never stored, because you can land a branch by hand.
    git(repo, "merge", "--ff-only", "-q", "silkworm/tsk_aaa")
    check("merging it by hand clears it, with no flag to update",
          B.survey([finished("tsk_aaa")]) == [],
          "a stored merge flag would still say unmerged")
    check("and the summary goes quiet", B.line([]) == "")

    # A branch that was deleted is not unmerged work, it is gone. The eight
    # that prompted this were later discarded; the survey must not resurrect
    # them as things to land.
    wt_b = work("tsk_bbb", "b")
    W.release(wt_b)
    git(repo, "branch", "-D", "silkworm/tsk_bbb")
    check("a deleted branch is not reported", B.survey([finished("tsk_bbb")]) == [],
          "there is nothing left to land")

    # The branch list exists to bound the cost: without it the survey would ask
    # git about every finished task ever recorded, one subprocess each, behind
    # a dashboard panel. Deleting the check leaves the answers right and the
    # cost linear in the board, which no correctness assertion can see.
    asked = []
    real_ahead = B.ahead
    B.ahead = lambda repo, base, branch: (asked.append(branch), real_ahead(repo, base, branch))[1]
    try:
        B.survey([finished(f"tsk_gone{i}") for i in range(40)])
    finally:
        B.ahead = real_ahead
    check("branches that do not exist are never asked about individually",
          asked == [],
          f"one subprocess per finished task, forever: {len(asked)} of them")

    # Still-running work owns its branch; listing it would be noise every night.
    work("tsk_ccc", "c")
    for state in (T.RUNNING, T.QUEUED, T.BLOCKED, T.PROPOSED):
        check(f"a task still in {state} is left alone",
              B.survey([finished("tsk_ccc", state=state)]) == [])
    check("but the same task, once stopped, is reported",
          len(B.survey([finished("tsk_ccc", state=T.AWAITING_APPROVAL)])) == 1,
          "awaiting approval means the work is done and sitting there")

    # One unreadable or wedged repository must cost that repository's row, not
    # the whole survey: this runs behind a dashboard panel and inside the
    # nightly goal, and neither may fail on it.
    broken = finished("tsk_aaa")
    broken["scope"] = {"cwd": str(repo)}
    real_run = B.subprocess.run
    B.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(OSError("no git"))
    try:
        check("a repository git cannot be run against is skipped, not raised",
              B.survey([broken]) == [])
    finally:
        B.subprocess.run = real_run

    # A project with no repository has no branches, and asking git about a
    # kitchen renovation should cost nothing rather than throwing.
    plain = T.make("plan the trip", state=T.QUEUED, scope={"cwd": str(root / "nope")})
    plain["state"] = T.DONE
    check("a project with no repo is skipped", B.survey([plain]) == [])

    # The eight branches predate the field entirely, so the survey has to fall
    # back to the convention or the feature is blind to exactly the case that
    # caused it.
    old = finished("tsk_ccc")
    old.pop("branch", None)
    check("a record written before the field still resolves its branch",
          B.name_for(old) == "silkworm/tsk_ccc")
    check("and a recorded branch wins over the convention",
          B.name_for({"id": "tsk_x", "branch": "feature/thing"}) == "feature/thing")

    # origin/HEAD is a symbolic ref: splitting it on "/" gives "HEAD" rather
    # than the branch, which has already made one landing refuse itself.
    git(repo, "remote", "add", "origin", str(repo))
    git(repo, "fetch", "-q", "origin")
    git(repo, "remote", "set-head", "origin", "main")
    check("a symbolic base ref reports the branch it points at",
          B.base_name(repo, "origin/HEAD") == "main")

    # And a name with a slash in it survives being resolved. Taking the last
    # segment reported the trader's `origin/research/point-in-time-universe`
    # as `universe` -- a branch that does not exist, so nothing could be looked
    # up under it or, below, measured against it.
    git(repo, "branch", "research/point-in-time")
    git(repo, "fetch", "-q", "origin")
    check("a base whose branch name holds slashes is reported in full",
          B.base_name(repo, "origin/research/point-in-time")
          == "research/point-in-time",
          f"got {B.base_name(repo, 'origin/research/point-in-time')!r}")

    # The base a merge question is asked of has to be the base the row names.
    # It was not. `worktrees.base_ref` prefers `origin/HEAD`, which is right
    # for *cutting* new work; landing fast-forwards the *local* branch and
    # never pushes, so origin falls one commit behind per landing. The row then
    # resolved "main" for its label and counted against origin/main -- and
    # branches whose every commit was on main were announced as stranded work,
    # to the dashboard, to `silkworm status`, and to the nightly ideator, which
    # was told the fix was "not on the base you are reading" about code that
    # was. Three such branches at the time, twenty-one phantom commits, one of
    # them main's own tip.
    wt_d = work("tsk_ddd", "d")
    W.release(wt_d)
    git(repo, "merge", "--ff-only", "-q", "silkworm/tsk_ddd")

    # The fixture is only worth anything if origin really is behind, and the
    # branch really is merged. Both asserted rather than assumed: without the
    # lag there is no disagreement for the fix to resolve.
    check("landing leaves origin behind the local base, since it never pushes",
          int(git(repo, "rev-list", "--count",
                  "origin/main..main").stdout.strip()) == 1,
          "no lag means the case this covers cannot arise")
    check("and the branch is wholly contained in the local base",
          git(repo, "merge-base", "--is-ancestor",
              "silkworm/tsk_ddd", "main").returncode == 0)
    refs, shown = B.base_for(repo, "")
    check("everything measured against is a copy of the base it names",
          shown == "main"
          and all(B.base_name(repo, r) == shown for r in refs)
          and "refs/heads/main" in refs,
          f"names {shown!r}, measures {refs}")
    check("work that is on the local base is not reported as stranded",
          B.survey([finished("tsk_ddd")]) == [],
          "counted against a remote-tracking ref that landing never advances")

    # Preferring the local branch is not the same as requiring one. With no
    # local branch of that name the remote-tracking ref is the only thing the
    # name can mean, so it is both what gets named and what gets measured --
    # the two still agree, which is the invariant, not "always local".
    check("a preferred base picks up both copies when both exist",
          B.base_for(repo, "research/point-in-time")
          == (("refs/heads/research/point-in-time",
               "refs/remotes/origin/research/point-in-time"),
              "research/point-in-time"),
          f"got {B.base_for(repo, 'research/point-in-time')}")
    git(repo, "branch", "-D", "research/point-in-time")
    refs, shown = B.base_for(repo, "research/point-in-time")
    check("and one copy alone is enough, still naming what it measured",
          refs == ("refs/remotes/origin/research/point-in-time",)
          and shown == "research/point-in-time",
          f"named {shown!r}, measured {refs}")

    # And the mirror of the case above, which preferring the local branch got
    # exactly as wrong in the other direction. A pull request merged on the
    # forge advances `origin/main` and leaves the local branch behind; every
    # isolated task fetches origin when its worktree is made, so this arrives
    # on its own. `update-ref` is what that fetch leaves behind, without
    # needing a second repository to push to.
    wt_e = work("tsk_eee", "e")
    W.release(wt_e)
    git(repo, "update-ref", "refs/remotes/origin/main", "silkworm/tsk_eee")

    check("the forge moved origin ahead of the local base",
          int(git(repo, "rev-list", "--count",
                  "main..origin/main").stdout.strip()) >= 1,
          "no lag the other way means this case cannot arise either")
    check("and the branch is on origin's copy but not the local one",
          git(repo, "merge-base", "--is-ancestor",
              "silkworm/tsk_eee", "origin/main").returncode == 0
          and git(repo, "merge-base", "--is-ancestor",
                  "silkworm/tsk_eee", "main").returncode != 0)
    check("work merged on the forge is not reported as stranded either",
          B.survey([finished("tsk_eee")]) == [],
          "measuring against the local branch alone is the same bug reversed")
    check("both copies of the base are measured against",
          set(B.base_for(repo, "")[0])
          == {"refs/heads/main", "refs/remotes/origin/main"},
          f"got {B.base_for(repo, '')[0]}")

    # Neither case above needs *both* copies on its own: while the base is
    # merely ahead, `base_ref` already names the local branch, and while it is
    # merely behind, the remote it names is the copy holding the work. It is a
    # base that has *diverged* -- each copy holding something the other does
    # not -- that separates the two. There the remote is rightly named, and the
    # work sits on the local copy alone, so measuring against the named ref by
    # itself invents a stranded branch that is already merged.
    # Cut by hand off the *local* branch. `work` goes through `base_ref`, which
    # at this point rightly names origin -- and a branch cut from there merges
    # into main by fast-forwarding past origin's tip, which would leave the two
    # in step rather than diverged.
    fff = root / "fff"
    git(repo, "worktree", "add", "-q", "-b", "silkworm/tsk_fff", str(fff), "main")
    (fff / "fff.txt").write_text("f")
    git(fff, "add", "-A"); git(fff, "commit", "-qm", "work tsk_fff")
    git(repo, "worktree", "remove", str(fff))
    git(repo, "merge", "--ff-only", "-q", "silkworm/tsk_fff")
    check("the base and origin each hold what the other does not",
          git(repo, "merge-base", "--is-ancestor",
              "origin/main", "main").returncode != 0
          and git(repo, "merge-base", "--is-ancestor",
                  "main", "origin/main").returncode != 0,
          "without a real divergence one ref would answer this correctly")
    check("and the ref the base is named by is not the copy holding the work",
          W.base_ref(repo, fetch=False) == "origin/HEAD"
          and git(repo, "merge-base", "--is-ancestor",
                  "silkworm/tsk_fff", "origin/main").returncode != 0
          and git(repo, "merge-base", "--is-ancestor",
                  "silkworm/tsk_fff", "main").returncode == 0,
          "otherwise the named ref alone would still give the right answer")
    check("work merged into a diverged base is not reported as stranded",
          B.survey([finished("tsk_fff")]) == [],
          "one ref cannot answer this; both copies have to be measured")

    # A name that is ambiguous -- a branch and a tag sharing it -- makes
    # `rev-parse --symbolic-full-name` exit zero with nothing to say. That used
    # to fall through to the last-segment split and reproduce the trader bug
    # exactly: a row reading `point-in-time` while counting against
    # `research/point-in-time`.
    git(repo, "branch", "research/ambiguous")
    git(repo, "tag", "research/ambiguous")
    refs, shown = B.base_for(repo, "research/ambiguous")
    check("an ambiguous name is not chopped into a guess",
          shown == "research/ambiguous"
          and refs == ("refs/heads/research/ambiguous",),
          f"named {shown!r}, measured {refs}")

    # A tag is a legitimate answer from `base_ref`, which only asks git whether
    # the ref verifies. It resolves to no branch, so the ref itself is the only
    # honest name for it.
    git(repo, "tag", "release/v1")
    check("a tag base names and measures the same thing",
          B.base_for(repo, "release/v1") == (("release/v1",), "release/v1"),
          f"got {B.base_for(repo, 'release/v1')}")

    # `ahead` splats its bases into the command line, so a bare string would
    # splat into single characters and quietly count against nothing.
    check("a single base may still be given as one string",
          B.ahead(repo, "main", "silkworm/tsk_ccc")
          == B.ahead(repo, ["main"], "silkworm/tsk_ccc") == 1)
    check("and no bases at all counts nothing rather than everything",
          B.ahead(repo, (), "silkworm/tsk_ccc") == 0)

    # A branch that was pushed and then lost its local ref. Asking only
    # `refs/heads/` made it invisible, and not hypothetically:
    # `origin/silkworm/tsk_e37a60256d` sat in this repository with one commit
    # not in main and no local branch, and was named by neither the dashboard
    # panel, nor `silkworm status`, nor the nightly note. A worktree release, a
    # sweep or a hand can all delete the local copy; push first and the commits
    # survive while the row does not. `update-ref` is what a push and a fetch
    # leave behind, without needing a second repository to push to.
    wt_r = work("tsk_rrr", "f")
    W.release(wt_r)
    git(repo, "update-ref", "refs/remotes/origin/silkworm/tsk_rrr",
        "silkworm/tsk_rrr")
    git(repo, "branch", "-D", "silkworm/tsk_rrr")
    check("the fixture really is remote-only, or it proves nothing",
          git(repo, "rev-parse", "--verify", "--quiet",
              "refs/heads/silkworm/tsk_rrr").returncode != 0
          and git(repo, "rev-parse", "--verify", "--quiet",
                  "refs/remotes/origin/silkworm/tsk_rrr").returncode == 0)
    remote_only = B.survey([finished("tsk_rrr")])
    check("a branch that exists only on a remote is still reported",
          len(remote_only) == 1 and remote_only[0]["commits"] == 1,
          "pushed, local ref deleted, and it fell out of the safety net")
    check("and the row says there is no local branch to go and look at",
          remote_only and remote_only[0]["local"] is False
          and remote_only[0]["remote"] == "origin")
    # Several remotes can hold the same branch, and the row names one of them.
    # Taking whichever git listed first names an alphabetical accident: "on
    # alpha only" about a branch that is on origin too.
    git(repo, "update-ref", "refs/remotes/alpha/silkworm/tsk_rrr",
        "refs/remotes/origin/silkworm/tsk_rrr")
    two = B.existing(repo).get("silkworm/tsk_rrr",
                               {"refs": (), "remote": ""})
    check("a branch on several remotes is measured across all of them",
          len(two["refs"]) == 2 and "refs/remotes/alpha/silkworm/tsk_rrr"
          in two["refs"])
    check("and is named by origin rather than by whichever sorted first",
          two["remote"] == "origin", f"named {two['remote']!r}")
    git(repo, "update-ref", "-d", "refs/remotes/alpha/silkworm/tsk_rrr")

    # for-each-ref matches a glob with WM_PATHNAME, so `refs/remotes/*/silkworm/*`
    # is one path segment and misses both a branch name with a further slash
    # and a remote whose own name holds one -- while the local pattern, being a
    # literal prefix, matches however deep either goes. The two halves of the
    # survey have to see the same shapes.
    git(repo, "remote", "add", "my/remote", str(repo))
    git(repo, "update-ref", "refs/remotes/origin/silkworm/tsk_hhh/part",
        "refs/remotes/origin/silkworm/tsk_rrr")
    git(repo, "update-ref", "refs/remotes/my/remote/silkworm/tsk_iii",
        "refs/remotes/origin/silkworm/tsk_rrr")
    deep = B.existing(repo)
    check("a remote branch name with a slash in it is seen",
          "silkworm/tsk_hhh/part" in deep,
          "the local half of the same survey sees one")
    check("and a remote whose own name holds a slash is read correctly",
          deep.get("silkworm/tsk_iii", {}).get("remote") == "my/remote",
          f"got {deep.get('silkworm/tsk_iii')}")
    check("while the remote's own HEAD is still not a branch",
          not any(k.endswith("HEAD") for k in deep))
    git(repo, "update-ref", "-d", "refs/remotes/origin/silkworm/tsk_hhh/part")
    git(repo, "update-ref", "-d", "refs/remotes/my/remote/silkworm/tsk_iii")
    git(repo, "remote", "remove", "my/remote")

    # Reading remotes brought discarded branches back from the dead. `git
    # branch -D` removes refs/heads only, this module never pushes, and
    # `base_ref` fetches without --prune, so the remote-tracking copy is
    # recreated on every fetch for as long as the branch is on origin. The one
    # remote-only branch in the real checkout turned out to be exactly this:
    # `origin/silkworm/tsk_e37a60256d` is the tip of
    # `discarded/2026-09-12/silkworm/tsk_e37a60256d-e3541be`.
    import discard as D
    check("the fixture is a branch the survey would otherwise report",
          len(B.survey([finished("tsk_rrr")])) == 1,
          "nothing to silence means this proves nothing")
    dropped, tag, _ = D.drop(repo, "silkworm/tsk_rrr", when="2026-09-12")
    check("discard has nothing local left to delete once it is remote-only",
          not dropped and not tag,
          "the fixture is meant to be past the point where -D can help")
    # So do what the real reset did while the branch was still local: tag the
    # tip under the discarded namespace. That tag is the decision record.
    fff_tip = git(repo, "rev-parse",
                  "refs/remotes/origin/silkworm/tsk_rrr").stdout.strip()
    git(repo, "tag", "-a", D.tag_for("silkworm/tsk_rrr", fff_tip, "2026-09-12"),
        fff_tip, "-m", "retired")
    check("a tip somebody deliberately retired is not resurrected",
          B.survey([finished("tsk_rrr")]) == [],
          "nothing prunes the remote copy, so this would never go quiet again")
    check("the tag is matched on branch and tip, not on either alone",
          ("silkworm/tsk_rrr", fff_tip[:7]) in B.retired(repo)
          and ("silkworm/tsk_aaa", fff_tip[:7]) not in B.retired(repo),
          "a re-run reuses a branch name with a different tip")
    # And a tip that has moved on since it was retired is not the tip anyone
    # retired, so the branch speaks up again.
    git(repo, "update-ref", "refs/remotes/origin/silkworm/tsk_rrr",
        "silkworm/tsk_ccc")
    check("a branch that gained commits after being retired comes back",
          len(B.survey([finished("tsk_rrr")])) == 1,
          "the tag records one tip, not a branch name for ever")
    git(repo, "update-ref", "refs/remotes/origin/silkworm/tsk_rrr", fff_tip)

    # The real path, not a hand-written tag: drop a branch while it is still
    # local and also on origin. `discard` used to count origin's copy as
    # already keeping the tip, wrote no tag, and so left nothing for `retired`
    # to match -- the remote copy came straight back as remote-only work, on a
    # row the panel offers no Drop for. Every branch ever pushed for a PR.
    wt_t = work("tsk_ttt", "t")
    W.release(wt_t)
    git(repo, "update-ref", "refs/remotes/origin/silkworm/tsk_ttt",
        "silkworm/tsk_ttt")
    check("the fixture is pushed, and the survey reports it",
          len(B.survey([finished("tsk_ttt")])) == 1)
    dropped, tag, _ = D.drop(repo, "silkworm/tsk_ttt")
    check("discarding a pushed branch still writes the tag that records it",
          dropped and tag.startswith(f"{D.NAMESPACE}/"),
          f"dropped={dropped} tag={tag!r}")
    check("so its remote copy does not come back as unmerged work",
          B.survey([finished("tsk_ttt")]) == [],
          "nothing prunes origin's copy, and the panel offers no Drop for it")

    # The same hole from the other side. Branches here have been observed reset
    # to a pre-work commit between turns; measured on the local copy alone that
    # reads as merged, while the commits are sitting on origin.
    wt_g = work("tsk_ggg", "g")
    W.release(wt_g)
    git(repo, "update-ref", "refs/remotes/origin/silkworm/tsk_ggg",
        "silkworm/tsk_ggg")
    git(repo, "update-ref", "refs/heads/silkworm/tsk_ggg", "origin/main")
    check("the local copy really was reset back to the base",
          B.ahead(repo, ["origin/main"], "refs/heads/silkworm/tsk_ggg") == 0
          and B.ahead(repo, ["origin/main"],
                      "refs/remotes/origin/silkworm/tsk_ggg") == 1,
          "without the reset there is no disagreement between the copies")
    reset = B.survey([finished("tsk_ggg")])
    check("a branch is measured across every copy of itself",
          len(reset) == 1 and reset[0]["commits"] == 1,
          "the local ref was wound back; the work is still on origin")
    check("and it is not mistaken for a remote-only branch",
          reset and reset[0]["local"] is True)

    # `_git` never raises -- a timeout or a git that cannot be run comes back
    # as a stub with empty stdout. Reading that as 0 meant "merged", and
    # `survey` drops merged branches, so the one outcome this module exists to
    # prevent was produced by a stopwatch.
    real_git = B._git
    B._git = lambda cwd, *a, **k: (B._Failed() if a and a[0] == "rev-list"
                                   else real_git(cwd, *a, **k))
    try:
        check("a git call that will not answer yields no count, not zero",
              B.ahead(repo, ["main"], "silkworm/tsk_ccc") is None,
              "zero reads as merged, and merged means the row disappears")
        blind = B.survey([finished("tsk_ccc")])
        check("and the branch keeps its row rather than vanishing",
              len(blind) == 1 and blind[0]["commits"] is None,
              "a branch nobody could measure is not a branch nobody left")
        check("the summary counts it without inventing commits for it",
              B.line(blind) == "1 finished task on unmerged branch "
                               "(1 unmeasured)", B.line(blind))
        check("and a count that is known is still added up beside it",
              B.line(blind + [{"commits": 3}, {"commits": None}])
              == "3 finished tasks on unmerged branches (3 commits, "
                 "2 unmeasured)",
              B.line(blind + [{"commits": 3}, {"commits": None}]))
    finally:
        B._git = real_git

    # The prompt block: an ideator told nothing re-derives a fix that already
    # exists on a branch, which is how one got implemented twice.
    import scoping as S
    rows = B.survey([finished("tsk_ccc")])
    note = S.unmerged_note(rows)
    check("the nightly goal is told which branches already hold the fix",
          "silkworm/tsk_ccc" in note and "1 commit" in note)
    check("and told not to file it again",
          "not file it as work" in note)
    check("a project with nothing outstanding gets no paragraph",
          S.unmerged_note([]) == "",
          "a healthy project should not pay tokens for an empty list")

    # The cut used to be `rows[:12]` over `survey`'s newest-first order, so a
    # repo with twenty-two unmerged branches showed twelve and dropped the ten
    # oldest -- the ones a nightly pass has had the most chances to re-derive,
    # which is the entire point of the paragraph. So the fixture is built
    # newest-first, the order `survey` really hands over: a note that took its
    # caller's order would lose the oldest ten here exactly as it did there.
    many = [{"branch": f"silkworm/b{i:02d}", "commits": 1, "base": "main",
             "title": f"fix number {i}", "updated": 1000 + i}
            for i in reversed(range(S.MAX_LISTED + 10))]
    check("the fixture is actually longer than the cap, and newest-first",
          len(many) > S.MAX_LISTED
          and many[0]["updated"] > many[-1]["updated"],
          "otherwise the truncation is never exercised in the shape it broke in")
    long_note = S.unmerged_note(many)
    check("a list longer than the cap still names its oldest branches",
          all(f"silkworm/b{i:02d}" in long_note for i in range(S.MAX_LISTED)),
          "the cut has to fall on the newest, never on the oldest")
    check("and says how many it left out rather than showing a slice as the whole",
          "10 newer ones" in long_note and "not all of them" in long_note,
          "the closing instruction reads as a claim about every unmerged branch")
    check("a list that fits is not announced as cut",
          "not listed here" not in S.unmerged_note(many[:S.MAX_LISTED]))
    check("the order the caller happens to use cannot decide what is dropped",
          S.unmerged_note(list(reversed(many))) == long_note,
          "survey sorts newest-first for the dashboard; the note must not inherit it")
    check("and the cap sits above any list this has actually had to print",
          S.MAX_LISTED >= 40,
          "at 12 it cut ten of this repo's twenty-two branches every night")

    # MAX_LISTED is deliberately shared between this note and `board_note`.
    # They were written on two branches, each of which defined the constant
    # itself, and git merged the two disjoint hunks without a word: the
    # consolidated tree had two bindings of MAX_LISTED forty lines apart, the
    # second silently shadowing the first for every reader, while the default
    # argument of unmerged_note stayed compiled against the first. Identical
    # that day, free to drift any day after, and nothing would have said so.
    #
    # Every spelling of a module-level binding counts, not just the bare one.
    # `MAX_LISTED: int = 40` parses as a different node, and
    # `MAX_LISTED, MAX_NAME_CHARS = 40, 100` hides the names inside a tuple
    # target -- which is not a hypothetical, because those two constants sat
    # next to each other before this and pairing them is the obvious thing to
    # write. So walk the targets for stored names rather than matching a shape.
    tree = ast.parse((BASE / "scoping.py").read_text())
    names = []
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        for target in targets:
            names += [n.id for n in ast.walk(target)
                      if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)]
    check("no constant in scoping.py is defined twice",
          len(names) == len(set(names)),
          f"defined more than once: {sorted({n for n in names if names.count(n) > 1})}")

    # An unknown count must not be printed as nothing on the branch, which is
    # the one thing a branch in this list cannot be.
    check("a count git refused to give is admitted rather than zeroed",
          "commit count unknown" in S.unmerged_note(
              [{"branch": "silkworm/tsk_x", "commits": None, "base": "main",
                "title": "t", "updated": 1}]),
          "'0 commits' describes an empty branch, which this never is")
    check("and a branch with no local copy says where it actually is",
          "on origin only" in S.unmerged_note(remote_only),
          S.unmerged_note(remote_only))
    # An older bot's payload has no `local` field at all. The dashboard reads
    # that as local and says nothing extra; these two must not disagree about
    # the same row, and the quieter guess is the right one to share.
    legacy = [{"branch": "silkworm/tsk_x", "commits": 1, "base": "main",
               "title": "t", "updated": 1, "remote": "origin"}]
    check("a payload from before the field is not called remote-only",
          "on origin only" not in S.unmerged_note(legacy),
          "the panel treats a missing `local` as local; so must this")

    # `silkworm status` prints the same rows, and reached them through
    # `.get("commits", 0)` -- which renders an unmeasured branch as the word
    # None. Driven out of the shipped CLI rather than grepped for.
    import types as _types
    cli_src = (BASE / "bin" / "silkworm").read_text()
    row_fn = next(n for n in ast.parse(cli_src).body
                  if isinstance(n, ast.FunctionDef) and n.name == "unmerged_row")
    cli = _types.ModuleType("cli")
    exec(compile(ast.Module(body=[row_fn], type_ignores=[]), "<cli>", "exec"),
         cli.__dict__)
    check("the status listing shows an unmeasured branch as unknown",
          cli.unmerged_row({"branch": "silkworm/tsk_x", "commits": None,
                            "base": "main", "title": "t"})
          == "silkworm/tsk_x (? on main) t",
          cli.unmerged_row({"branch": "silkworm/tsk_x", "commits": None,
                            "base": "main", "title": "t"}))
    check("and names a remote-only branch under the name git knows it by",
          cli.unmerged_row({"branch": "silkworm/tsk_x", "commits": 1,
                            "base": "main", "title": "t", "remote": "origin",
                            "local": False})
          == "origin/silkworm/tsk_x (1 on main) t",
          cli.unmerged_row({"branch": "silkworm/tsk_x", "commits": 1,
                            "base": "main", "title": "t", "remote": "origin",
                            "local": False}))
    check("while an ordinary row is left exactly as it was",
          cli.unmerged_row({"branch": "silkworm/tsk_x", "commits": 2,
                            "base": "main", "title": "t", "local": True})
          == "silkworm/tsk_x (2 on main) t")
    check("and a payload from before the field is not called remote-only",
          cli.unmerged_row({"branch": "silkworm/tsk_x", "commits": 2,
                            "base": "main", "title": "t", "remote": "origin"})
          == "silkworm/tsk_x (2 on main) t",
          "the panel treats a missing `local` as local; so must this")

    bot = (BASE / "bot.py").read_text()
    ideate = bot[bot.index("def run_ideation"):bot.index("def _ideation_scheduler")]
    check("the nightly pass actually carries the list",
          "scoping.unmerged_note(branches.survey(" in ideate,
          "the ideator re-derives what is fixed on a branch it was never shown")

    # Recorded as the checkout closes, because afterwards nothing knows.
    ex = bot[bot.index("def execute_task"):bot.index("MAX_VERIFY_ATTEMPTS")]
    check("the branch is written down before the checkout is released",
          ex.count("record_branch(tid, worktree, task_base(task))") == 2
          and ex.index("record_branch(tid, worktree, task_base(task))")
          < ex.index("worktrees.release(worktree,"),
          "released first, there is nothing left to ask which branch it was")
    # Behavioural rather than textual: the docstring says "raised", so grepping
    # for the word proves nothing. Ask the tree.
    fn = next(n for n in ast.walk(ast.parse(bot))
              if isinstance(n, ast.FunctionDef) and n.name == "record_branch")
    guarded = [n for n in fn.body if isinstance(n, ast.Try)]
    check("and recording it can never cost the reply",
          len(fn.body) == 2 and len(guarded) == 1
          and any(h.type and getattr(h.type, "id", "") == "Exception"
                  for h in guarded[0].handlers)
          and not [n for n in ast.walk(fn) if isinstance(n, ast.Raise)],
          "losing the note is survivable; losing the turn is not")

    # The badge polls the task list every five seconds. A git survey folded
    # into that would run twice a minute for an answer that changes when you
    # merge something.
    handler = bot[bot.index("def handle_tasks"):bot.index("def handle_projects")]
    check("the survey is its own request, not part of the polled list",
          'if action == "unmerged":' in handler
          and "branches.survey" not in handler[:handler.index('if action == "unmerged":')],
          "folding it into `list` would shell out to git every five seconds")

    W.ROOT = old_root


# --- a checkout holding uncommitted work must be visible ---------------------
# Nothing here may delete it, and nothing did -- but nothing said so either.
# Two trader checkouts were logged "leaving orphaned worktree with uncommitted
# work" 508 times over six days, every half hour, and appeared in `silkworm
# status`, the dashboard and their own task rows exactly nowhere.

def test_held_checkouts():
    import holding as H
    import tasks as T
    import worktrees as W
    from tasks import TaskStore
    print("\ncheckouts still holding uncommitted work are visible")

    root = Path(tempfile.mkdtemp())
    old_root, W.ROOT = W.ROOT, root / "worktrees"
    old_announced = set(W._announced)
    W._announced.clear()
    repo = root / "repo"; repo.mkdir()

    def git(cwd, *a):
        return subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True)

    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("hello\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "base")

    st = TaskStore(Path(tempfile.mkdtemp()) / "t.json")

    def task_in(state, tid_out=None):
        """A task carried to `state` the legal way, with its own checkout."""
        t = st.create("do a thing", driver="queue", isolate=True, role="implementor",
                      scope={"cwd": str(repo)})
        tid = t["id"]
        st.update(tid, title=f"work for {tid}", project="proj",
                  thread=f"C1:{tid}")
        for step in {T.RUNNING: [T.RUNNING],
                     T.AWAITING_APPROVAL: [T.RUNNING, T.AWAITING_APPROVAL],
                     T.DONE: [T.RUNNING, T.DONE],
                     T.CANCELLED: [T.CANCELLED]}[state]:
            st.transition(tid, step)
        wt = W.create(repo, tid, fetch=False)
        st.update(tid, scope={**st.get(tid)["scope"], "worktree": str(wt)})
        return tid, Path(wt)

    def dirty(wt, *names):
        for n in names:
            (wt / n).write_text("half-finished\n")

    # The two shapes of the bug, side by side. Non-terminal: live_worktree_tasks
    # puts it in the sweeper's keep set, so the tree is skipped before the dirty
    # check is even reached -- held for ever, in silence. Terminal: it drops out
    # of the keep set, reaches the dirty check, and is logged twice an hour for
    # ever with nothing able to act on the line.
    waiting, wt_waiting = task_in(T.AWAITING_APPROVAL)
    dirty(wt_waiting, "draft.md", "script.py")
    finished, wt_finished = task_in(T.DONE)
    dirty(wt_finished, "leftover.txt")

    rows = H.survey(list(st.all().values()))
    by_id = {r["id"]: r for r in rows}
    check("a checkout held by a task still awaiting approval is reported",
          waiting in by_id,
          "the sweeper skips it before the dirty check, so nothing ever "
          "mentions it at all")
    check("and one held by a task that has finished is reported too",
          finished in by_id,
          "the sweeper logs this one every half hour and nobody reads it")
    check("each row says where the checkout is",
          by_id.get(waiting, {}).get("path") == str(wt_waiting)
          and by_id.get(finished, {}).get("path") == str(wt_finished),
          "a count with no path cannot be acted on")
    check("and how much is uncommitted in it",
          by_id.get(waiting, {}).get("changes") == 2
          and by_id.get(finished, {}).get("changes") == 1)
    check("and names the files, so a virtualenv is not mistaken for work",
          sorted(by_id.get(waiting, {}).get("files") or []) == ["draft.md", "script.py"])
    check("and which task owns it", by_id.get(waiting, {}).get("title") == f"work for {waiting}")
    check("and whether anything will ever ask about it again",
          by_id.get(waiting, {}).get("terminal") is False
          and by_id.get(finished, {}).get("terminal") is True,
          "a finished task's checkout is stranded; a waiting one's is a promise")
    check("the summary line counts them",
          "2 checkouts still holding uncommitted work" in H.line(rows), H.line(rows))
    check("and says nothing when there is nothing to say", H.line([]) == "")

    # Work in progress is not stuck work. Reporting it would put a row on the
    # panel every time anything ran, which is how a panel stops being read.
    live, wt_live = task_in(T.RUNNING)
    dirty(wt_live, "mid-edit.py")
    check("a task running right now is not reported",
          live not in {r["id"] for r in H.survey(list(st.all().values()))},
          "somebody is typing in there")

    # A clean checkout is not holding anything: the sweeper will take it away
    # in the ordinary course, and it needs no row.
    clean, _ = task_in(T.DONE)
    check("a clean checkout is not reported",
          clean not in {r["id"] for r in H.survey(list(st.all().values()))})

    # The worst case, and the reason this reads the directory rather than the
    # board: a tree whose task nobody can name.
    orphan = W.create(repo, "tsk_nobody", fetch=False)
    dirty(Path(orphan), "mystery.txt")
    orphan_rows = [r for r in H.survey(list(st.all().values()))
                   if r["id"] == "tsk_nobody"]
    check("a checkout no task on the board claims is still reported",
          len(orphan_rows) == 1 and orphan_rows[0]["known"] is False,
          "being unattributable is a reason to say more about it, not less")

    # Asked of git, never stored -- you can go and commit those files yourself,
    # and a flag written at release time would still say they were there.
    git(wt_finished, "add", "-A"); git(wt_finished, "commit", "-qm", "kept it")
    check("committing the work by hand clears the row, with no flag to update",
          finished not in {r["id"] for r in H.survey(list(st.all().values()))})

    # A checkout git will not answer about is the one case that must never be
    # rounded down to "clean". A child killed mid-rebase leaves an index.lock,
    # `git status` exits non-zero, and reading that as nothing prints the
    # all-clear over a tree full of half-finished work -- at the moment
    # somebody is deciding whether to dismiss the task.
    locked, wt_locked = task_in(T.DONE)
    dirty(wt_locked, "half-done.py")
    (wt_locked / ".git").write_text((wt_locked / ".git").read_text() + "\nbroken\n")
    blind = [r for r in H.survey(list(st.all().values())) if r["id"] == locked]
    check("a checkout git will not answer about is still reported",
          len(blind) == 1 and blind[0]["unreadable"] is True,
          "dropping the row reads downstream as an all-clear, which is the one "
          "direction this must never fail in")
    check("and it is not counted as holding nothing",
          "could not read" in H.line(blind), H.line(blind))
    top = (H.survey(list(st.all().values())) or [{}])[0]
    check("and it sorts above the checkouts whose size is known",
          top.get("unreadable") is True,
          "it is the least certain row, not the smallest")
    check("and approving it says so rather than claiming it is empty",
          bool(blind) and "may be holding uncommitted work" in H.note(blind[0]))
    shutil.rmtree(wt_locked, ignore_errors=True)
    W._git(repo, "worktree", "prune")

    # One checkout git cannot be *run* against must still cost its own row and
    # nothing else: this sits behind a dashboard panel and inside `silkworm
    # status`, and neither may raise.
    real_run = H.subprocess.run
    H.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(OSError("no git"))
    try:
        blind = H.survey(list(st.all().values()))
        check("a checkout git cannot be run against is reported, not raised",
              bool(blind) and all(r["unreadable"] for r in blind))
    finally:
        H.subprocess.run = real_run

    # --- approving or dismissing is the moment it becomes unreachable --------
    posted = []

    class FakeClient:
        def chat_postMessage(self, **kw): posted.append(kw)

    ns = lift("handle_tasks", "approve_task", "stop_task", "tell_thread",
                       tasks=T, task_store=st, holding=H, RUNNING_TASKS={},
                       start_landing=lambda tid: None,
                       app=types.SimpleNamespace(client=FakeClient()),
              absent=TASK_ROUTES + STEP_ROUTES)
    route = ns["handle_tasks"]

    r = route({"action": "approve", "id": waiting})
    check("approving a task that still holds a checkout says so in the reply",
          r["ok"] and str(wt_waiting) in (r.get("note") or ""), r.get("note"))
    check("the task's own event log records it too",
          str(wt_waiting) in st.get(waiting)["events"][-1]["detail"],
          "a toast scrolls away; this is the only durable copy on the record")
    check("and the thread is told, where it outlives the toast",
          any(str(wt_waiting) in (m.get("text") or "") for m in posted)
          and any("draft.md" in (m.get("text") or "") for m in posted),
          "naming the files is the point: what is in there is the whole "
          "question, and the system cannot answer it")
    check("the checkout itself is left exactly where it was",
          wt_waiting.exists() and (wt_waiting / "draft.md").exists(),
          "reclaiming it automatically would mean committing a virtualenv or "
          "deleting somebody's afternoon")

    dismissed, wt_dismissed = task_in(T.AWAITING_APPROVAL)
    dirty(wt_dismissed, "notes.md")
    posted.clear()
    r = route({"action": "dismiss", "id": dismissed})
    check("dismissing says it as well, being the same door",
          r["ok"] and str(wt_dismissed) in (r.get("note") or "")
          and any(str(wt_dismissed) in (m.get("text") or "") for m in posted),
          r.get("note"))

    clean_id, _ = task_in(T.AWAITING_APPROVAL)
    posted.clear()
    r = route({"action": "approve", "id": clean_id})
    check("approving a task holding nothing says nothing about checkouts",
          r["ok"] and not r.get("note") and not posted,
          "a note on every approval is a note nobody reads")

    # for_task reads the task's recorded checkout, so it costs one git call in
    # a path a person is waiting on.
    check("a released checkout is not reported against its task",
          H.for_task({"scope": {"worktree": str(root / "gone")}}) is None)
    check("nor is a task that never had one", H.for_task({"scope": {}}) is None)

    # --- the sweeper stops re-announcing what it cannot act on --------------
    keep = lift("live_worktree_tasks", tasks=T, task_store=st,
                         RUNNING_TASKS={})["live_worktree_tasks"]
    stuck, wt_stuck = task_in(T.DONE)
    dirty(wt_stuck, "still-here.txt")
    logged = []
    real_info = W.log.info
    W.log.info = lambda msg, *a, **k: logged.append(msg % a if a else msg)
    try:
        W.sweep(keep=keep(), min_age_s=0)
        first = [m for m in logged if "uncommitted work" in m and str(wt_stuck) in m]
        logged.clear()
        W.sweep(keep=keep(), min_age_s=0)
        W.sweep(keep=keep(), min_age_s=0)
        again = [m for m in logged if "uncommitted work" in m and str(wt_stuck) in m]
    finally:
        W.log.info = real_info
    check("a held checkout is named in the log the first time it is met",
          len(first) == 1, f"{first}")
    check("and not again on every pass for ever",
          again == [],
          "508 lines over six days, half-hourly, with nothing able to act on one")
    check("and it is still there, untouched",
          wt_stuck.exists() and (wt_stuck / "still-here.txt").exists(),
          "the rule that nothing destroys uncommitted work is the point")

    # Both worktree layouts resolve back to a task id. `repo--land--taskid` is
    # the checkout a landing borrows; splitting on the first separator turns it
    # into "land--tsk_...", which matches no task at all.
    check("a task's own checkout name resolves to its id",
          W.task_of(root / f"repo{W.SEP}tsk_abc") == "tsk_abc")
    check("and a landing's borrowed one resolves to the same id",
          W.task_of(root / f"repo{W.SEP}land{W.SEP}tsk_abc") == "tsk_abc")
    swept = ast.parse((BASE / "worktrees.py").read_text())
    fn = next(n for n in ast.walk(swept)
              if isinstance(n, ast.FunctionDef) and n.name == "sweep")
    check("and the sweeper reads it that way too, not by splitting",
          any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "task_of"
              for n in ast.walk(fn))
          and not [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                   and getattr(n.func, "attr", "") == "split"],
          "a landing's checkout would match no task and never be kept")

    # --- it has to reach the places a person actually looks -----------------
    cli = (BASE / "bin" / "silkworm").read_text()
    check("`silkworm status` asks for the survey",
          '{"action": "holding"}' in cli)
    check("and prints the paths, not only a count",
          "r.get('path', '')" in cli,
          "a count you cannot locate is the same silence in a shorter form")
    check("and treats 'could not ask' as unknown rather than all-clear",
          cli.count("no checkout is holding uncommitted work") == 2,
          "a bot running older code does not know the action")

    bot = (BASE / "bot.py").read_text()
    handler = bot[bot.index("def handle_tasks"):bot.index("def handle_projects")]
    check("the survey is its own request, not part of the polled list",
          'if action == "holding":' in handler
          and "holding.survey" not in handler[:handler.index('if action == "holding":')],
          "the badge polls `list` every five seconds, and this walks every "
          "worktree asking git")
    fn = next(n for n in ast.walk(ast.parse(bot))
              if isinstance(n, ast.FunctionDef) and n.name == "tell_thread")
    check("telling the thread can never cost the action it is attached to",
          any(isinstance(n, ast.Try) for n in fn.body)
          and not [n for n in ast.walk(fn) if isinstance(n, ast.Raise)],
          "a task that could not be told is better than one that could not be "
          "approved")

    import re as _r
    sys.argv = ["x"]
    import visualizer as V
    js = _r.search(r"<script>(.*?)</script>", V.PAGE, _r.S).group(1)
    check("the dashboard has a panel for it", 'id="holding"' in V.PAGE
          and "async function renderHolding()" in js)
    check("and a task row says its checkout is still holding work",
          "function held(t)" in js and "${held(t)}" in js,
          "approving that row is the moment the work becomes unreachable")
    marked = js.find("await renderHolding();")
    drawn = js.find("drawKeyed(list, r.tasks.map")      # the rows' one draw (keyed, so it animates)
    body = js[js.index("async function renderHolding()"):js.index("function updateTaskBadge")]
    check("and a failed survey costs the marker, never the list of rows",
          "try {" in body and body.index("try {") < body.index('taskCall({action: "holding"'),
          "renderTasks awaits this before drawing anything, so a rejected "
          "request would blank the board instead of one row's marker")
    check("and a refusal is not drawn as an all-clear",
          "r.ok === false" in body,
          "a bot running older code answers {ok: false, unknown action}, which "
          "would take the marker off every row and leave the buttons")
    check("the row is marked before the buttons are drawn",
          -1 < marked < drawn,
          "an unawaited survey would race the buttons that act on it")

    W._announced.clear(); W._announced.update(old_announced)
    W.ROOT = old_root


# --- and the panel has to say what the survey found -------------------------
# The row is the only place some of these branches are named at all, so the
# rendering is driven for real rather than read: a panel that drops a row, or
# prints "null" where a count should be, looks fine from the Python side.

def test_unmerged_panel_renders():
    import re, json as _j, subprocess as _sp, tempfile as _tf
    sys.argv = ["x"]
    import visualizer as V
    print("\nthe unmerged-branch panel")

    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    prelude = """
const _els = {};
function _el(id) {
  return _els[id] || (_els[id] = {innerHTML: "", value: "", style: {},
    classList: {add(){}, remove(){}, toggle(){}}, appendChild(){}, addEventListener(){}});
}
globalThis.document = {getElementById: _el, addEventListener(){},
  createElement: () => _el("_new"), querySelectorAll: () => [], body: _el("body")};
globalThis.window = globalThis;
globalThis.fetch = async () => ({json: async () => ({sessions: [], tasks: []}),
                                 text: async () => ""});
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
globalThis.localStorage = {getItem: () => null, setItem(){}};
process.on("unhandledRejection", () => {});
"""
    drive = """
const ROWS = [
  {id: "tsk_1", title: "a local one", branch: "silkworm/tsk_1", base: "main",
   commits: 2, head: "abcdef12", state: "done", repo: "/r", thread: "",
   local: true, remote: ""},
  {id: "tsk_2", title: "unmeasurable", branch: "silkworm/tsk_2", base: "main",
   commits: null, head: "abcdef34", state: "done", repo: "/r", thread: "",
   local: true, remote: ""},
  {id: "tsk_3", title: "pushed then deleted", branch: "silkworm/tsk_3",
   base: "main", commits: 1, head: "abcdef56", state: "done", repo: "/r",
   thread: "", local: false, remote: "origin"},
];
taskCall = async () => ({ok: true, unmerged: ROWS, summary: "3 finished tasks"});
renderUnmerged().then(() => {
  console.log(JSON.stringify({html: _el("unmerged").innerHTML}));
});
"""
    f = Path(_tf.mkdtemp()) / "h.js"
    f.write_text(prelude + js + drive)
    r = _sp.run(["node", str(f)], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        check("the panel runs in a browser-like context", False,
              (r.stderr or "").strip().splitlines()[-1] if r.stderr else "no output")
        return
    html = _j.loads(r.stdout.strip().splitlines()[-1])["html"]

    check("every branch the survey found reaches the panel",
          html.count("<b>") == 3, html)
    check("an ordinary branch shows its count", "tsk_1 <b>2</b>" in html, html)
    check("a count git would not give is shown as a question, not as null",
          "tsk_2 <b>?</b>" in html and "null" not in html,
          "\"null\" in the panel is how a reader learns to ignore the panel")
    check("and the tip says why there is no number",
          "git could not count its commits" in html, html)
    check("a branch with no local copy is named where it actually is",
          "origin/tsk_3 <b>1</b>" in html, html)
    check("and says so in full on hover",
          "no local branch" in html, html)
    # Land and Drop both act on the local branch. On a remote-only row Land
    # would answer "its branch is gone: nothing to land" over commits sitting
    # on origin, so neither button may be offered there.
    rows_html = html.split('<span class="ub">')[1:]
    remote_row = next((h for h in rows_html if "origin/tsk_3" in h), "")
    check("a remote-only branch offers no Land or Drop to misreport it",
          remote_row and "'land'" not in remote_row and "'drop'" not in remote_row,
          remote_row)
    check("while a local branch keeps both",
          sum("'land'" in h and "'drop'" in h for h in rows_html) == 2, html)


def test_dashboard_js_is_whole():
    import re
    sys.argv = ["x"]
    import visualizer as V
    print("\ndashboard javascript")
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    defined = set(re.findall(r"(?:async\s+)?function\s+([A-Za-z_]\w*)", js))

    for name in ("renderKindFilter", "setKind", "matchesKind",
                 "loadList", "loadStats", "loadTranscript", "renderAlerts", "jumpTo",
                 "taskCall", "toggleTasks", "setTaskView", "renderProjects", "newProjectForm",
                 "taskAction", "taskButtons", "renderTasks", "updateTaskBadge",
                 "refreshTaskBadge", "taskDetail", "lastEvent", "landing",
                 "releaseThread", "retitle", "nameAllThreads",
                 "resummarize", "toggleLearn", "renderLearnings", "renderUnmerged",
                 "toggleBoard", "closeBoard", "setBoardProject", "renderBoard",
                 "refreshBoard", "boardSearch", "loadOverview", "renderOverview",
                 "projectCard", "renderColumns", "boardCard", "cardButtons",
                 "cardReview", "cardLanding", "unfiledThreads", "openCard",
                 "closeDetail", "renderDetail", "fmtCost", "boardFilters"):
        check(f"{name}() is defined", name in defined)

    # Anything wired to an onclick must exist, or the click is a dead button.
    handlers = set(re.findall(r'onclick="([a-zA-Z_]\w*)\(', js))
    keywords = {"if", "for", "while", "return", "switch", "confirm", "prompt", "alert"}
    missing = sorted(h for h in handlers if h not in defined and h not in keywords)
    check("every onclick handler is defined", not missing, f"missing: {missing}")

    # Every element the script looks up must be in the markup it ships with.
    looked_up = set(re.findall(r'getElementById\("([^"]+)"\)', js))
    present = set(re.findall(r'id="([^"]+)"', V.PAGE))
    absent = sorted(looked_up - present)
    check("every element the script reads exists in the page", not absent,
          f"absent: {absent}")

    # A marker nothing renders is the same silence it was written to end.
    row = re.search(r"async function renderTasks\(.*?^}", js, re.S | re.M).group(0)
    check("the task row says when commits never landed", "${landing(t)}" in row,
          "landing() exists but the board never calls it")


# --- discarding a branch keeps what was on it --------------------------------
# On 2026-09-12 fifteen branches were deleted when the backlog was reset, and
# what was kept was a list of shas and a line promising "the reflog for ~90
# days". Deleting a branch deletes its reflog, and the work had been done in
# worktrees whose reflogs went with them, so fourteen of the fifteen were
# reachable from no ref at all and auto-gc would have taken them from about
# 2026-09-24. Three proposals on the board still say to read those diffs.


def _repo_with_history():
    """A real repository. These invariants are about refs, not about strings."""
    d = Path(tempfile.mkdtemp())
    run = lambda *a: subprocess.run(["git", *a], cwd=d, capture_output=True, text=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t"); run("config", "user.name", "t")
    (d / "a.txt").write_text("1"); run("add", "-A"); run("commit", "-qm", "base")
    return d, run


def test_discarding_a_branch_keeps_it():
    print("\ndiscarding a branch keeps what was on it")
    import discard

    d, run = _repo_with_history()
    # The commit is made in a worktree and the worktree is then removed, which
    # is how the real ones were made. It matters: doing it with `git checkout
    # -b` here leaves the tip in this repository's own HEAD reflog, which keeps
    # it alive through a prune all by itself -- so the test would pass with the
    # tagging taken out and prove nothing. A worktree's reflog is deleted with
    # the worktree, which is why fourteen of the fifteen were held by nothing.
    wt = d / "wt"
    run("worktree", "add", "-q", "-b", "feature", str(wt), "main")
    (wt / "b.txt").write_text("work")
    subprocess.run(["git", "add", "-A"], cwd=wt, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "real work"], cwd=wt, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt,
                         capture_output=True, text=True).stdout.strip()
    run("worktree", "remove", "--force", str(wt))
    check("the fixture reproduces the real conditions: no reflog holds it",
          sha not in run("reflog", "--all").stdout,
          "otherwise the prune below proves nothing")

    deleted, tag, _ = discard.drop(d, "feature")
    check("the branch is gone", deleted and not discard.tip(d, "feature"))
    check("its tip was tagged first", bool(tag) and tag.endswith(sha[:7]),
          "a sha written in a file is not a ref, and only a ref survives gc")
    # The actual failure being prevented, reproduced rather than argued about.
    run("gc", "--prune=now", "--quiet")
    check("and it survives git gc --prune=now",
          run("cat-file", "-t", sha).stdout.strip() == "commit",
          "this is exactly what would have happened to the 2026-09-12 branches")
    check("the diff survives too, not just the commit object",
          "b.txt" in run("show", "--stat", "--format=", sha).stdout)
    check("recovery instruction in the tag actually works",
          run("checkout", "-qb", "back", tag).returncode == 0
          and run("rev-parse", "back").stdout.strip() == sha)

    # A task that committed nothing points at the base, which main holds. Every
    # run leaves one, and tagging them would bury the real rescues.
    d, run = _repo_with_history()
    run("branch", "silkworm/tsk_empty")
    deleted, tag, _ = discard.drop(d, "silkworm/tsk_empty")
    check("a branch holding nothing new is deleted without a tag",
          deleted and tag == "",
          "otherwise one tag per run makes `git tag -l discarded/*` useless")

    # Four of the fifteen names appeared twice, with divergent tips: a task id
    # re-ran and reused its branch. Name-only tags would have kept eight.
    d, run = _repo_with_history()
    tips = []
    for n in ("first", "second"):
        # Both on one date, which is the case that actually happened: a task
        # re-ran the same day and its branch name came round again.
        wt = d / f"wt-{n}"
        run("worktree", "add", "-q", "-b", "silkworm/tsk_same", str(wt), "main")
        (wt / f"{n}.txt").write_text(n)
        subprocess.run(["git", "add", "-A"], cwd=wt, capture_output=True)
        subprocess.run(["git", "commit", "-qm", n], cwd=wt, capture_output=True)
        tips.append(subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt,
                                   capture_output=True, text=True).stdout.strip())
        run("worktree", "remove", "--force", str(wt))
        discard.drop(d, "silkworm/tsk_same", "2026-09-12")
    run("gc", "--prune=now", "--quiet")
    check("a reused branch name does not overwrite the earlier rescue",
          all(run("cat-file", "-t", t).stdout.strip() == "commit" for t in tips),
          "the tag name carries the sha, so two tips of one name both survive")
    check("both are found under one namespace",
          len([t for t in run("tag", "-l", "discarded/*").stdout.split() if t]) == 2)

    # The ordering invariant. Tagging after the delete is a race with gc and
    # with a crash; failing to tag has to mean not deleting.
    d, run = _repo_with_history()
    run("checkout", "-qb", "fragile")
    (d / "c.txt").write_text("x"); run("add", "-A"); run("commit", "-qm", "precious")
    run("checkout", "-q", "main")
    real = discard.preserve
    def explode(*a, **k):
        raise RuntimeError("no tag for you")
    discard.preserve = explode
    try:
        deleted, tag, note = discard.drop(d, "fragile")
    finally:
        discard.preserve = real
    check("a branch whose tip cannot be tagged is not deleted",
          not deleted and bool(discard.tip(d, "fragile")),
          "deleting on a failed preserve is the original bug with extra steps")
    check("and the reason is reported rather than swallowed", "no tag for you" in note)

    # The note is the thing someone reads under pressure.
    d, run = _repo_with_history()
    run("checkout", "-qb", "silkworm/tsk_note")
    (d / "n.txt").write_text("x"); run("add", "-A"); run("commit", "-qm", "noted")
    run("checkout", "-q", "main")
    discard.reset(d, ["silkworm/tsk_note"], "2026-09-30")
    text = (d / ".discarded" / "2026-09-30-reset.txt").read_text()
    check("the note points at the tag, not only at a sha",
          "discarded/2026-09-30/silkworm/tsk_note-" in text)
    check("the note does not promise the reflog",
          "reflog for" not in text,
          "that promise was false: a deleted branch takes its reflog with it")

    # The CLI is the path a future reset is meant to take, so drive it rather
    # than reading it. It shipped with argv[0] read as a branch name -- it
    # reported "no branch discard" and would have deleted one called that.
    d, run = _repo_with_history()
    run("branch", "discard")
    cli = subprocess.run([sys.executable, str(BASE / "bin" / "silkworm"),
                          "discard", "silkworm/tsk_none"],
                         cwd=d, capture_output=True, text=True)
    check("the CLI does not read its own subcommand as a branch",
          bool(discard.tip(d, "discard")) and "no branch discard" not in cli.stdout,
          "argv includes the subcommand itself")
    check("and asks for arguments rather than doing nothing quietly",
          subprocess.run([sys.executable, str(BASE / "bin" / "silkworm"), "discard"],
                         cwd=d, capture_output=True, text=True).returncode == 2)

    # What this task was actually for. The note is ignored by git and lives in
    # the main checkout, so resolve that rather than BASE -- these tests
    # usually run from a worktree, where BASE has no .discarded at all and the
    # check would quietly never run.
    common = subprocess.run(["git", "rev-parse", "--path-format=absolute",
                             "--git-common-dir"], cwd=BASE,
                            capture_output=True, text=True).stdout.strip()
    main = Path(common).parent if common.endswith("/.git") else BASE
    listed = main / ".discarded" / "2026-09-12-reset.txt"
    if listed.exists():
        text = listed.read_text()
        rows = [ln.split() for ln in text.splitlines()
                if ln.strip() and not ln.startswith("#")]
        loose = [r[0] for r in rows if len(r) < 3]
        check("the note names a tag for every commit, not just a sha",
              not loose, f"no tag recorded for {', '.join(s[:8] for s in loose)}")
        # Resolved against the repository, so a tag that was renamed or never
        # made fails here rather than reading as fine because the text says so.
        missing = [r[0] for r in rows if len(r) > 2 and r[2] not in
                   subprocess.run(["git", "tag", "--contains", r[0]], cwd=BASE,
                                  capture_output=True, text=True).stdout.split()]
        check("every branch discarded on 2026-09-12 is reachable from a tag",
              not missing and len(rows) >= 15,
              f"{len(missing)} of {len(rows)} would be pruned")
        check("and the note explains what actually protects them",
              "That was wrong" in text and "Nothing but a ref keeps a commit" in text,
              "the reflog line was the instruction someone would have followed")

# --- a branch whose work survives only on origin can still land --------------
# The unmerged panel counts every copy of a task branch, so a local ref reset
# to the base while origin held the commits showed "N commits" and a Land
# button -- and Land refused with "everything on its branch is already on the
# base", having asked about the local ref alone.

def test_remote_only_work_lands():
    import logging
    import merge as M
    import worktrees as W
    import branches as B
    print("\na branch whose work survives only on origin can still land")

    def git(where, *a):
        return subprocess.run(["git", "-C", str(where), "-c", "user.email=t@t",
                               "-c", "user.name=t", *a], capture_output=True, text=True)
    origin = Path(tempfile.mkdtemp())
    git(origin, "init", "-q", "--bare", "-b", "main")
    repo = Path(tempfile.mkdtemp())
    git(repo, "init", "-q", "-b", "main")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    git(repo, "push", "-q", "origin", "main")
    for tid in ("tsk_reset", "tsk_deleted", "tsk_own", "tsk_split"):
        git(repo, "checkout", "-q", "-b", f"silkworm/{tid}", "main")
        (repo / f"{tid}.txt").write_text("pushed work\n")
        git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", f"{tid} pushed")
        git(repo, "push", "-q", "origin", f"silkworm/{tid}")
    git(repo, "checkout", "-q", "main")
    git(repo, "fetch", "-q", "origin")
    git(repo, "branch", "-f", "silkworm/tsk_reset", "main")     # reset to the base
    git(repo, "branch", "-D", "silkworm/tsk_deleted")          # local copy gone
    # tsk_own: the local copy has work of its own, and origin a stale other version
    git(repo, "branch", "-f", "silkworm/tsk_own", "main")
    git(repo, "checkout", "-q", "silkworm/tsk_own")
    (repo / "own.txt").write_text("local work\n")
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "local work")
    git(repo, "checkout", "-q", "main")
    # tsk_split: two remotes disagree, the local copy has nothing
    other = Path(tempfile.mkdtemp())
    git(other, "init", "-q", "--bare", "-b", "main")
    git(repo, "remote", "add", "fork", str(other))
    git(repo, "checkout", "-q", "-b", "alt", "main")
    (repo / "alt.txt").write_text("alt\n")
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "alt")
    git(repo, "push", "-q", "fork", "alt:silkworm/tsk_split")
    git(repo, "checkout", "-q", "main"); git(repo, "branch", "-D", "alt")
    git(repo, "fetch", "-q", "fork")
    git(repo, "branch", "-f", "silkworm/tsk_split", "main")

    sha = lambda ref: git(repo, "rev-parse", ref).stdout.strip()
    pushed = sha("origin/silkworm/tsk_reset")
    check("fixture: local reset to the base, origin ahead",
          sha("silkworm/tsk_reset") == sha("main") and pushed != sha("main"))

    rows = {r["branch"]: r for r in B.survey([
        {"id": "tsk_reset", "state": "done", "scope": {"cwd": str(repo)}}])}
    check("the panel counts the work origin holds",
          rows.get("silkworm/tsk_reset", {}).get("commits") == 1, str(rows))
    for tid in ("tsk_reset", "tsk_deleted"):
        why = B.nothing_to_land(repo, f"silkworm/{tid}", "origin/main")
        check(f"{tid}: Land agrees there is something to land", why == "", why)

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
            attached.append((branch, sha(branch)))
            return None                                # stop before git merges anything
    ns = {"project_store": Projects(), "worktrees": Worktrees(), "merge": M,
          "branches": B, "log": logging.getLogger("test")}
    lift({"land_if_ready", "landing_enabled"}, ns=ns, absent=LANDING_ROUTES)
    land = ns["land_if_ready"]
    base = {"project": "p", "role": "implementor", "verified": True,
            "scope": {"cwd": str(repo)}}

    r = land({**base, "id": "tsk_reset"})
    check("reset branch: goes on to land, not 'nothing to land'",
          r.get("stage") == "attach", repr(r))
    check("and what it attaches is origin's work, not the empty local ref",
          attached[-1] == ("silkworm/tsk_reset", pushed), str(attached))
    r = land({**base, "id": "tsk_deleted"})
    check("deleted local branch: recreated from origin and landed",
          r.get("stage") == "attach" and attached[-1] == (
              "silkworm/tsk_deleted", sha("origin/silkworm/tsk_deleted")), f"{attached} {r!r}")
    check("without tracking origin", git(repo, "config", "branch.silkworm/tsk_deleted.remote").stdout == "")
    own = sha("silkworm/tsk_own")
    land({**base, "id": "tsk_own"})
    check("a local copy with its own work is landed as itself, not replaced by origin's",
          attached[-1] == ("silkworm/tsk_own", own), str(attached))
    n = len(attached)
    r = land({**base, "id": "tsk_split"})
    check("remotes that disagree: refused for a person, nothing attached",
          r.get("stage") == "restore" and M.needs_a_person(r) and len(attached) == n
          and sha("silkworm/tsk_split") == sha("main"), repr(r))

    # A branch pushed, then landed by rebasing onto a moved base, then deleted
    # as landed: origin's copy holds the same work under other hashes, and
    # Land must not recreate it and land it twice.
    git(repo, "checkout", "-q", "-b", "silkworm/tsk_landed", "main")
    (repo / "landed.txt").write_text("landed work\n")
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "landed work")
    git(repo, "push", "-q", "origin", "silkworm/tsk_landed")
    git(repo, "checkout", "-q", "main")
    (repo / "moved.txt").write_text("base moved\n")
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "base moved")
    git(repo, "cherry-pick", "silkworm/tsk_landed")             # the landing's rebase
    git(repo, "push", "-q", "origin", "main")
    git(repo, "branch", "-D", "silkworm/tsk_landed")
    git(repo, "fetch", "-q", "origin")
    check("fixture: origin's copy is not in main by hash",
          git(repo, "merge-base", "--is-ancestor", "origin/silkworm/tsk_landed", "main").returncode != 0)
    n = len(attached)
    r = land({**base, "id": "tsk_landed"})
    check("a stale copy of already-landed work is nothing to land",
          r.get("stage") == "nothing-to-land" and len(attached) == n
          and not B.exists(repo, "silkworm/tsk_landed"), repr(r))
    check("nor would a direct restore recreate the branch from it",
          B.restore_from_remote(repo, "silkworm/tsk_landed", "origin/main") == ""
          and not B.exists(repo, "silkworm/tsk_landed"))
    rows = {r["branch"]: r for r in B.survey([
        {"id": "tsk_landed", "state": "done", "scope": {"cwd": str(repo)}},
        {"id": "tsk_deleted", "state": "done", "scope": {"cwd": str(repo)}}])}
    check("and the panel shows no row, so no Land button, for it",
          "silkworm/tsk_landed" not in rows, str(rows))
    check("while a remote-only branch with real work keeps its row and count",
          rows.get("silkworm/tsk_deleted", {}).get("commits") == 1, str(rows))

    # A remote copy someone discarded on purpose is not work waiting.
    git(repo, "tag", f"discarded/2026-10-03/silkworm/tsk_gone-{sha('origin/silkworm/tsk_deleted')[:7]}",
        "origin/silkworm/tsk_deleted")
    git(repo, "push", "-q", "origin", "origin/silkworm/tsk_deleted:refs/heads/silkworm/tsk_gone")
    git(repo, "fetch", "-q", "origin")
    why = B.nothing_to_land(repo, "silkworm/tsk_gone", "origin/main")
    check("a discarded remote-only copy has nothing to land", why != "", why)


# --- a task's base is its project's base now, not the one it was filed with ---
# trader named research/point-in-time-universe as its base until 2026-10-01,
# when the setting was cleared: that branch had long been merged into main. But
# each task copied the project's scope when it was filed, and landing, the
# unmerged survey and reruns all read the copy -- eleven reviewed tasks were
# refused with "the checkout is on 'main', not 'research/...'", and the survey
# reported 39 branches / 485 commits where `git cherry main` showed 33 / 103.

def test_a_tasks_base_is_its_projects_current_base():
    import contextlib
    import merge as M
    import verify as V
    import worktrees as W
    import branches as B
    import projects as P
    import tasks as T
    print("\na task's base is its project's current base, not the one it recorded")

    def git(where, *a):
        return subprocess.run(["git", "-C", str(where), "-c", "user.email=t@t",
                               "-c", "user.name=t", *a], capture_output=True, text=True)
    sha = lambda ref: git(repo, "rev-parse", ref).stdout.strip()

    def commit(name):
        (repo / f"{name}.txt").write_text(name + "\n")
        git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", name)

    root = Path(tempfile.mkdtemp())
    origin = root / "origin.git"
    git(root, "init", "-q", "--bare", "-b", "main", str(origin))
    repo = root / "trader"; repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "remote", "add", "origin", str(origin))
    commit("base")
    # The old base: a research branch, since merged into main, which moved on.
    git(repo, "checkout", "-q", "-b", "research/old")
    commit("research")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--ff-only", "research/old")
    commit("m1")
    git(repo, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q", "origin")
    commit("m2")                       # landed locally, never pushed: origin lags

    ts = T.TaskStore(root / "t.json")
    ps = P.ProjectStore(root / "p.json")
    ps.ensure("trader", scope={"cwd": str(repo)}, test_cmd="true", auto_merge=True)
    stale = {"cwd": str(repo), "branch": "research/old"}   # what tasks copied

    def task(name, project="trader", state=T.DONE, own=True):
        rec = ts.create(name, project=project, scope=dict(stale), role="implementor",
                        driver="queue", verified=True)
        git(repo, "branch", f"silkworm/{rec['id']}", "main")
        if own:
            git(repo, "checkout", "-q", f"silkworm/{rec['id']}")
            commit(name)
            git(repo, "checkout", "-q", "main")
        ts.transition(rec["id"], T.RUNNING)
        ts.transition(rec["id"], state)
        return ts.get(rec["id"])

    work = task("work")                          # one commit of its own, off main
    empty = task("empty", own=False)             # nothing of its own at all
    loose = task("loose", project="")            # filed under no project

    check("fixture: the task recorded the old base",
          (work.get("scope") or {}).get("branch") == "research/old", str(work.get("scope")))
    check("fixture: the project no longer names one", ps.scope_for("trader").get("branch") is None
          or ps.scope_for("trader").get("branch") == "", str(ps.scope_for("trader")))
    check("fixture: origin lags local main", sha("origin/main") != sha("main"))

    # --- the resolver ---------------------------------------------------------
    check("a task with a project takes the project's current base",
          B.base_pref(work, ps.scope_for) == "")
    check("a task with no project keeps the base it was filed with",
          B.base_pref(loose, ps.scope_for) == "research/old")

    # --- the survey agrees with git cherry --------------------------------------
    def cherry(base, branch):
        return sum(1 for l in git(repo, "cherry", base, branch).stdout.splitlines()
                   if l.startswith("+"))
    old_rows = {r["id"]: r for r in B.survey([work, empty])}
    check("fixture: measured against the old base the two disagree with cherry",
          old_rows.get(work["id"], {}).get("commits") == 3
          and empty["id"] in old_rows, str(old_rows))
    rows = {r["id"]: r for r in B.survey([work, empty, loose], scope_for=ps.scope_for)}
    w = rows.get(work["id"], {})
    check("the survey counts what git cherry against the current base counts",
          w.get("commits") == cherry("main", f"silkworm/{work['id']}") == 1, str(w))
    check("and names that base", w.get("base") == "main", str(w))
    check("work already on the base is not listed at all",
          empty["id"] not in rows and cherry("main", f"silkworm/{empty['id']}") == 0,
          str(rows.get(empty["id"])))
    check("a task with no project is still measured against its own base",
          rows.get(loose["id"], {}).get("base") == "research/old"
          and rows.get(loose["id"], {}).get("commits") == 3, str(rows.get(loose["id"])))

    # --- pruning ------------------------------------------------------------------
    pruned = B.prune_merged([empty, work], scope_for=ps.scope_for)
    check("pruning measures against the current base too",
          pruned == [f"silkworm/{empty['id']}"], str(pruned))

    # --- landing --------------------------------------------------------------------
    recorded = []
    ns = {"project_store": ps, "worktrees": W, "merge": M, "branches": B, "verify": V,
          "repo_guard": lambda cwd, progress=None: contextlib.nullcontext(),
          "record_landing": lambda t, o: recorded.append(o),
          "LANDING_UNDERWAY": "landing", "log": logging.getLogger("test")}
    got = lift({"land_if_ready", "landing_enabled"}, ns=ns, absent=LANDING_ROUTES)["__lifted__"]
    check("the landing gate and its base resolver were lifted out of bot.py",
          {"land_if_ready", "task_base"} <= got, str(sorted(got)))
    land = ns["land_if_ready"]
    r = land(work)
    check("a task that recorded the old base lands on the current one",
          r.get("landed") is True, f"{r!r}")
    check("and its commit is now on main",
          git(repo, "cat-file", "-e", "main:work.txt").returncode == 0)
    check("and the old base was left alone", sha("research/old") != sha("main"))
    again = task("again", own=False)
    r = land(again)
    check("nothing-to-land asks the current base too",
          r.get("stage") == "nothing-to-land", f"{r!r}")

    # --- reruns ------------------------------------------------------------------------
    ns2 = {"project_store": ps, "branches": B}
    lift({"task_base"}, ns=ns2)
    rerun = W.create(repo, "tsk_rerun", fetch=False, base=ns2["task_base"](work))
    check("a rerun starts from the current base",
          rerun and git(rerun, "rev-parse", "HEAD").stdout.strip() == sha("main"),
          str(rerun))
    if rerun:
        W.release(rerun)
    check("a rerun of a task with no project starts from its own",
          ns2["task_base"](loose) == "research/old")
    bot = (BASE / "bot.py").read_text()
    check("execute_task cuts reruns from task_base",
          "worktrees.create(cwd, tid, base=task_base(task))" in bot)

    # Every other reader of the base goes through the one resolver. The only
    # `scope.get("branch")` left in bot.py reads a *project's* scope.
    tree = ast.parse(bot)
    readers = sorted(
        {f.name for f in tree.body if isinstance(f, ast.FunctionDef)
         for n in ast.walk(f)
         if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
         and n.func.attr == "get" and n.args
         and isinstance(n.args[0], ast.Constant) and n.args[0].value == "branch"
         and "scope" in ast.unparse(n.func.value)})
    # handle_projects reads it to show and set a project's base (`get`, `base`).
    check("no task's recorded base is read anywhere else",
          readers == ["handle_command", "handle_projects", "release_checkout"], str(readers))
    for caller in ("handle_tasks", "digest_inputs", "run_ideation", "_worktree_sweeper"):
        fn = next((f for f in tree.body if isinstance(f, ast.FunctionDef) and f.name == caller), None)
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr in ("survey", "prune_merged")
                 and ast.unparse(n.func.value) == "branches"] if fn else []
        check(f"{caller} surveys against current bases",
              calls and all(any(k.arg == "scope_for"
                                and ast.unparse(k.value) == "project_store.scope_for"
                                for k in c.keywords) for c in calls), caller)
    # Not only calls: dedup is handed the survey by reference, and a bare
    # `branches.survey` passed along measures against the recorded base.
    called = {id(c.func) for c in ast.walk(tree) if isinstance(c, ast.Call)
              and any(k.arg == "scope_for"
                      and ast.unparse(k.value) == "project_store.scope_for"
                      for k in c.keywords)}
    bare = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Attribute)
            and n.attr in ("survey", "prune_merged")
            and ast.unparse(n.value) == "branches" and id(n) not in called]
    check("every reference to the survey in bot.py passes the current bases",
          not bare, f"bare at lines {bare}")
    dups = [c for c in ast.walk(tree) if isinstance(c, ast.Call)
            and ast.unparse(c.func) == "dedup.duplicate"]
    check("and the duplicate check is handed the survey that does",
          len(dups) >= 2 and all(len(c.args) > 2 and ast.unparse(c.args[2]) == "unmerged_survey"
                                 for c in dups), str([ast.unparse(c) for c in dups]))
    ns3 = {"project_store": ps, "branches": B}
    lift({"unmerged_survey"}, ns=ns3)
    check("which measures a stale-base task against the current base",
          [r["id"] for r in ns3["unmerged_survey"]([again])] == []
          and [r["id"] for r in B.survey([again])] == [again["id"]])
    board = ast.unparse(next(n for n in tree.body if isinstance(n, ast.Assign)
                             and any(getattr(t, "id", "") == "BOARD" for t in n.targets)))
    check("so does the board's unmerged line",
          "scope_for=project_store.scope_for" in board)


# --- generated files are not uncommitted work ---------------------------------
# 2026-10-12: every Cadence build regenerates both Info.plists (injecting keys
# that must never be committed), so every finished Cadence checkout looked
# dirty, was kept, kept its branch checked out, and all seven landings refused
# with "could not check out". With those checkouts cleared by hand, all six
# then refused at base-dirty, because the main checkout holds the same two
# files modified for the person's own builds.

_GEN = ["ios/Cadence/Info.plist", "ios/CadenceWidget/Info.plist"]
_SECRET = b"<plist><key>SUPABASE_KEY</key><string>injected-not-committed</string></plist>\n"


def _gen_git(where, *a):
    return subprocess.run(["git", "-C", str(where), "-c", "user.email=t@t",
                           "-c", "user.name=t", *a], capture_output=True, text=True)


def _gen_repo():
    """A Cadence-shaped repository: two committed plists and a source file."""
    repo = Path(tempfile.mkdtemp()) / "cadence"
    repo.mkdir()
    _gen_git(repo, "init", "-q", "-b", "main")
    for rel in _GEN:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(b"<plist>bare</plist>\n")
    (repo / "app.swift").write_text("let x = 1\n")
    _gen_git(repo, "add", "-A")
    _gen_git(repo, "commit", "-q", "-m", "base")
    return repo


@contextlib.contextmanager
def _gen_world(lookup=lambda repo: list(_GEN)):
    import worktrees as W
    old_root, old_lookup = W.ROOT, W.generated_lookup
    W.ROOT = Path(tempfile.mkdtemp()) / "worktrees"
    W.generated_lookup = lookup
    try:
        yield W
    finally:
        W.ROOT, W.generated_lookup = old_root, old_lookup


def _gen_task(W, repo, tid, *, real=False):
    """A finished task checkout: one commit, a build's regenerated plists, and
    (if `real`) one genuinely uncommitted change."""
    wt = W.create(repo, tid, fetch=False)
    (wt / "feature.swift").write_text(f"// {tid}\n")
    _gen_git(wt, "add", "-A")
    _gen_git(wt, "commit", "-q", "-m", f"{tid} work")
    for rel in _GEN:
        (wt / rel).write_bytes(_SECRET)
    if real:
        (wt / "app.swift").write_text("let x = 2   // not committed\n")
    return wt


def test_generated_paths_are_not_uncommitted_work():
    import holding
    print("\na project's generated files are not uncommitted work")
    repo = _gen_repo()
    with _gen_world() as W:
        wt = _gen_task(W, repo, "tsk_gen_only")
        check("a checkout holding only regenerated files holds no work",
              W.changes(wt) == [] and not W.is_dirty(wt) and W.uncommitted(wt) == [],
              str(W.changes(wt)))
        check("and the ignored half is still reported as such",
              sorted(W.generated_changes(wt)) == sorted(_GEN), str(W.generated_changes(wt)))
        check("the approve/dismiss handback sees nothing held",
              holding.changes(wt) == [] and holding.for_task(
                  {"id": "tsk_gen_only", "scope": {"worktree": str(wt)}}) is None)

        # Everything else still counts, exactly as before.
        (wt / "notes.txt").write_text("somebody's notes\n")
        check("an untracked file still counts", W.changes(wt) == ["notes.txt"], str(W.changes(wt)))
        (wt / "notes.txt").unlink()
        (wt / "ios/Cadence/Extra.plist").write_text("new\n")
        check("a new file beside a generated one still counts",
              W.changes(wt) == ["ios/Cadence/Extra.plist"], str(W.changes(wt)))
        (wt / "ios/Cadence/Extra.plist").unlink()
        (wt / "app.swift").write_text("let x = 3\n")
        check("a tracked non-generated change still counts",
              W.changes(wt) == ["app.swift"] and W.is_dirty(wt), str(W.changes(wt)))
        _gen_git(wt, "checkout", "--", "app.swift")
        # A rename whose destination is a listed path is still a rename: it
        # cannot be put back by a checkout, and nothing else names its source.
        W.generated_lookup = lambda r: list(_GEN) + ["ios/Cadence/Moved.plist"]
        _gen_git(wt, "mv", _GEN[0], "ios/Cadence/Moved.plist")
        check("a rename onto a generated path is a change, not a regeneration",
              W.changes(wt) == ["ios/Cadence/Moved.plist"], str(W.changes(wt)))
        W.generated_lookup = lambda r: list(_GEN)
        _gen_git(wt, "reset", "-q", "--hard")
        _gen_git(wt, "rm", "-q", "--cached", _GEN[1])
        check("a generated path that is untracked is new work, and counts",
              W.uncommitted(wt) == [_GEN[1]], str(W.uncommitted(wt)))
        _gen_git(wt, "reset", "-q", "--hard")
        for rel in _GEN:
            (wt / rel).write_bytes(_SECRET)

        check("a conflict on a generated path is never a build's output",
              not W._ignorable("DD", _GEN[0], _GEN) and not W._ignorable("UU", _GEN[0], _GEN)
              and not W._ignorable("AA", _GEN[0], _GEN) and W._ignorable("MM", _GEN[0], _GEN))

        # Another project's list is not this one's.
        W.generated_lookup = lambda r: []
        check("without the setting the same checkout is dirty, as it always was",
              W.is_dirty(wt) and sorted(W.changes(wt)) == sorted(_GEN))
        W.generated_lookup = lambda r: list(_GEN)

        # release(): puts the files back and goes; the branch and its commit stay.
        ok, note = W.release(wt)
        check("release lets go of a checkout holding only generated files",
              ok and not wt.exists(), note)
        check("and the task's branch and commit survive it",
              _gen_git(repo, "log", "-1", "--format=%s", "silkworm/tsk_gen_only").stdout.strip()
              == "tsk_gen_only work")
        check("the base checkout's files were not touched",
              all((repo / rel).read_bytes() == b"<plist>bare</plist>\n" for rel in _GEN))

        kept = _gen_task(W, repo, "tsk_gen_real", real=True)
        ok, note = W.release(kept)
        check("release keeps a checkout holding a real change",
              not ok and kept.exists() and str(kept) in note, note)
        check("and leaves even its generated files alone",
              (kept / _GEN[0]).read_bytes() == _SECRET)
        row = holding.for_task({"id": "tsk_gen_real", "scope": {"worktree": str(kept)}})
        check("the handback names the real change and only that",
              row and row["files"] == ["app.swift"], str(row))

        # The orphan sweep: same helper, same answer.
        swept = _gen_task(W, repo, "tsk_gen_swept")
        n = W.sweep(keep=set(), min_age_s=0)
        check("the sweep removes an orphan holding only generated files",
              not swept.exists() and kept.exists() and n == 1, f"removed {n}")
        check("and the sweep never deletes a branch",
              _gen_git(repo, "rev-parse", "--verify", "-q", "silkworm/tsk_gen_swept").returncode == 0)

        # A review reattaching an existing detached checkout.
        tip = _gen_git(repo, "rev-parse", "silkworm/tsk_gen_only").stdout.strip()
        rv = W.attach(repo, "tsk_gen_only", "silkworm/tsk_gen_only", label="review", detach=True)
        for rel in _GEN:
            (rv / rel).write_bytes(_SECRET)
        again = W.attach(repo, "tsk_gen_only", "silkworm/tsk_gen_only", label="review", detach=True)
        check("a review checkout holding only generated files is still the tip, untouched",
              again == rv and _gen_git(rv, "rev-parse", "HEAD").stdout.strip() == tip)


def test_landing_frees_its_own_generated_only_checkout():
    import logging
    import merge as M
    import branches as B
    print("\nlanding releases the task's own checkout when it holds only generated files")
    repo = _gen_repo()
    with _gen_world() as W:
        clean = _gen_task(W, repo, "tsk_land_gen")
        dirty = _gen_task(W, repo, "tsk_land_real", real=True)
        check("fixture: plain attach refuses while the task's checkout holds the branch",
              W.attach(repo, "tsk_probe", "silkworm/tsk_land_gen") is None)

        class Projects:
            def scope_for(self, slug):
                return {}
            def get(self, slug):
                return {"auto_merge": True, "test_cmd": "true", "generated": list(_GEN)}
        recorded = []
        ns = {"project_store": Projects(), "worktrees": W, "merge": M, "branches": B,
              "verify": types.SimpleNamespace(run=lambda *a, **k: {"ok": True, "output": ""}),
              "repo_guard": lambda cwd: contextlib.nullcontext(),
              "record_landing": lambda task, out: recorded.append(out),
              "LANDING_UNDERWAY": "in-progress", "log": logging.getLogger("test")}
        lift({"land_if_ready", "landing_enabled"}, ns=ns, absent=LANDING_ROUTES)
        land = ns["land_if_ready"]
        base = {"project": "cadence", "role": "implementor", "verified": True,
                "scope": {"cwd": str(repo)}}

        # The base checkout is where the person builds: its plists hold keys.
        for rel in _GEN:
            (repo / rel).write_bytes(_SECRET)

        r = land({**base, "id": "tsk_land_gen"})
        check("a checkout holding only generated files is released and the work lands",
              r.get("landed") is True, repr(r))
        check("its checkout is gone", not clean.exists())
        check("the commit is on main",
              _gen_git(repo, "log", "-1", "--format=%s", "main").stdout.strip() == "tsk_land_gen work")
        check("and the base's injected plists are byte-identical",
              all((repo / rel).read_bytes() == _SECRET for rel in _GEN))

        head = _gen_git(repo, "rev-parse", "main").stdout.strip()
        r = land({**base, "id": "tsk_land_real"})
        check("one holding a real change refuses at attach",
              r.get("stage") == "attach" and not r.get("landed"), repr(r))
        check("naming the checkout and the changed file, not a bare 'could not check out'",
              str(dirty) in r.get("detail", "") and "app.swift" in r.get("detail", "")
              and "Info.plist" not in r.get("detail", ""), r.get("detail"))
        check("and that checkout is kept with its change",
              dirty.exists() and "2" in (dirty / "app.swift").read_text())
        check("nothing was merged", _gen_git(repo, "rev-parse", "main").stdout.strip() == head)

        # Held by some other checkout: still refused, and said where.
        other = W.ROOT / "elsewhere"
        _gen_git(repo, "branch", "silkworm/tsk_land_other", "main~1")
        _gen_git(repo, "worktree", "add", "-q", str(other), "silkworm/tsk_land_other")
        (other / "o.txt").write_text("o\n")
        _gen_git(other, "add", "-A"); _gen_git(other, "commit", "-q", "-m", "other")
        r = land({**base, "id": "tsk_land_other"})
        check("a branch held by a checkout that is not the task's own is refused, named",
              r.get("stage") == "attach" and str(other) in r.get("detail", "")
              and other.exists(), repr(r))


def test_base_checkout_generated_files_do_not_block_landing():
    import merge as M
    print("\na base checkout dirty only in generated files still lands, untouched")
    ok = lambda where: {"ok": True, "output": ""}

    def rig(change=None):
        repo = _gen_repo()
        for rel in _GEN:
            (repo / rel).write_bytes(_SECRET)
        wt = repo.parent / "wt"
        _gen_git(repo, "worktree", "add", "-q", "-b", "silkworm/tsk_b", str(wt), "main")
        (wt / (change or "feature.swift")).write_bytes(b"changed by the branch\n")
        _gen_git(wt, "add", "-A"); _gen_git(wt, "commit", "-q", "-m", "work")
        return repo, wt

    repo, wt = rig()
    before = _gen_git(repo, "rev-parse", "HEAD").stdout.strip()
    r = M.land(wt, repo, "silkworm/tsk_b", "main", ok, generated=_GEN)
    check("dirty only in generated paths: it lands", r.get("landed") is True, repr(r))
    check("with the base's plists byte-identical",
          all((repo / rel).read_bytes() == _SECRET for rel in _GEN))
    check("and the base moved", _gen_git(repo, "rev-parse", "HEAD").stdout.strip() != before)

    with _gen_world() as W:
        repo, wt = rig()
        r = M.land(wt, repo, "silkworm/tsk_b", "main", ok)
        check("the project's list is found by lookup when not passed",
              r.get("landed") is True, repr(r))

    repo, wt = rig()
    (repo / "app.swift").write_text("let x = 9   // the person's edit\n")
    r = M.land(wt, repo, "silkworm/tsk_b", "main", ok, generated=_GEN)
    check("dirty in a non-generated tracked file: still refused",
          r.get("stage") == "base-dirty" and "app.swift" in r.get("detail", ""), repr(r))
    check("and that refusal does not list the generated files",
          "Info.plist" not in r.get("detail", ""), r.get("detail"))

    repo, wt = rig()
    r = M.land(wt, repo, "silkworm/tsk_b", "main", ok)
    check("without the setting the same checkout refuses as base-dirty, as before",
          r.get("stage") == "base-dirty", repr(r))

    repo, wt = rig(change=_GEN[0])
    before = _gen_git(repo, "rev-parse", "HEAD").stdout.strip()
    ran = []
    r = M.land(wt, repo, "silkworm/tsk_b", "main",
               lambda where: ran.append(where) or {"ok": True}, generated=_GEN)
    check("a landing that would overwrite a held generated file refuses",
          r.get("stage") == "base-generated" and _GEN[0] in r.get("detail", ""), repr(r))
    check("before spending a test run, without merging, and leaving the file alone",
          not ran and _gen_git(repo, "rev-parse", "HEAD").stdout.strip() == before
          and (repo / _GEN[0]).read_bytes() == _SECRET)

    # A branch moving a held file away is a change to it too.
    repo, wt = rig()
    _gen_git(wt, "mv", _GEN[0], "ios/Cadence/Renamed.plist")
    _gen_git(wt, "commit", "-q", "-m", "rename")
    ran = []
    r = M.land(wt, repo, "silkworm/tsk_b", "main",
               lambda where: ran.append(where) or {"ok": True}, generated=_GEN)
    check("a branch renaming a held generated file away is refused up front",
          r.get("stage") == "base-generated" and not ran
          and (repo / _GEN[0]).read_bytes() == _SECRET, repr(r))

    # A file regenerated *during* the suite run is caught at the merge.
    repo, wt = rig(change=_GEN[1])
    (repo / _GEN[1]).write_bytes(b"<plist>bare</plist>\n")       # clean when it starts
    before = _gen_git(repo, "rev-parse", "HEAD").stdout.strip()
    def build_during(where):
        (repo / _GEN[1]).write_bytes(_SECRET)
        return {"ok": True}
    r = M.land(wt, repo, "silkworm/tsk_b", "main", build_during, generated=_GEN)
    check("a generated file a build rewrote mid-landing is caught before the merge",
          r.get("stage") == "base-generated"
          and _gen_git(repo, "rev-parse", "HEAD").stdout.strip() == before
          and (repo / _GEN[1]).read_bytes() == _SECRET, repr(r))

    # Broken by the merge: put back, and the injected plists kept.
    repo, wt = rig()
    before = _gen_git(repo, "rev-parse", "HEAD").stdout.strip()
    calls = []
    def second_fails(where):
        calls.append(where)
        return {"ok": len(calls) == 1, "output": "boom"}
    r = M.land(wt, repo, "silkworm/tsk_b", "main", second_fails, generated=_GEN)
    check("a landing that breaks the base is still put back",
          r.get("stage") == "tests-after-merge"
          and _gen_git(repo, "rev-parse", "HEAD").stdout.strip() == before, repr(r))
    check("without the reset discarding the base's injected plists",
          all((repo / rel).read_bytes() == _SECRET for rel in _GEN))

    # The person's state of those files survives exactly: staged and deleted.
    repo, wt = rig()
    _gen_git(repo, "add", _GEN[0])
    (repo / _GEN[1]).unlink()
    before = _gen_git(repo, "rev-parse", "HEAD").stdout.strip()
    calls = []
    r = M.land(wt, repo, "silkworm/tsk_b", "main", second_fails, generated=_GEN)
    st = _gen_git(repo, "status", "--porcelain").stdout
    # (`reset --keep` leaves the index at the reset commit, so a staged copy
    # comes back unstaged -- its bytes are what matter, and they never land.)
    check("after a put-back a staged generated file keeps its bytes, a deleted one stays gone",
          r.get("stage") == "tests-after-merge"
          and _gen_git(repo, "rev-parse", "HEAD").stdout.strip() == before
          and f" D {_GEN[1]}" in st and not (repo / _GEN[1]).exists()
          and (repo / _GEN[0]).read_bytes() == _SECRET, st)


def test_generated_paths_setting():
    import logging
    import projects as P
    print("\nthe generated-paths setting: stored, seeded once, settable")
    d = Path(tempfile.mkdtemp())
    (d / "projects.json").write_text(json.dumps({
        "cadence": {"slug": "cadence", "title": "Cadence"},
        "saga": {"slug": "saga", "title": "Saga"},
    }))
    ps = P.ProjectStore(d / "projects.json")
    check("Cadence's two plists are seeded on a record that predates the field",
          ps.get("cadence")["generated"] == _GEN, str(ps.get("cadence")))
    check("other projects get none", ps.get("saga")["generated"] == [])
    ps.ensure("cadence", generated=[])
    ps2 = P.ProjectStore(d / "projects.json")
    check("a list the user cleared is never seeded back", ps2.get("cadence")["generated"] == [])

    check("paths parse from commas, spaces and ./",
          P.parse_paths("./a/b.plist, c.plist  d/") == ["a/b.plist", "c.plist", "d/"])
    check("off clears", P.parse_paths("off") == [])
    for bad in ("/etc/passwd", "../outside.plist"):
        try:
            P.parse_paths(bad)
            check(f"{bad!r} is refused", False)
        except ValueError:
            check(f"{bad!r} is refused", True)

    hp = {"projects": P, "project_store": ps2, "log": logging.getLogger("test")}
    lift({"handle_projects"}, ns=hp, absent=PROJECT_ROUTES)
    r = hp["handle_projects"]({"action": "generated", "slug": "cadence",
                               "paths": ", ".join(_GEN)})
    check("the dashboard route sets them", r.get("ok") and ps2.get("cadence")["generated"] == _GEN, str(r))
    r = hp["handle_projects"]({"action": "generated", "slug": "nope", "paths": "a"})
    check("an unknown project is refused", r.get("ok") is False)

    # The bot's lookup: by the project whose checkout this is.
    ps2.ensure("cadence", scope={"cwd": str(d)})
    gp = {"project_store": ps2, "Path": Path}
    lift({"generated_paths"}, ns=gp)
    check("the bot's lookup finds a project by its checkout",
          gp["generated_paths"](d) == _GEN and gp["generated_paths"](d / "x") == [])
    src = (BASE / "bot.py").read_text()
    check("and it is what worktrees asks", "worktrees.generated_lookup = generated_paths" in src)
    check("the dashboard has the field and its save",
          'id="psgenerated"' in (BASE / "visualizer.py").read_text()
          and 'action: "generated"' in (BASE / "visualizer.py").read_text())
    check("!project generated is a command",
          'arg.lower().startswith("generated ")' in src)
    check("landing passes the project's list to merge",
          'generated=list(proj.get("generated") or [])' in src)

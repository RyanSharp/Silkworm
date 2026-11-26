"""State files, projects and retention.

Run by tests/run.py with every other tests/test_*.py; see tests/harness.py."""

from harness import *  # noqa: F401,F403


# --- old threads can be put away -------------------------------------------------
# 37 threads, 13 of them one-off task runs, ten untouched for a fortnight. The
# ask was "delete, or at a minimum hide" -- and deleting is the wrong half of
# that, because dropping a record destroys its title, summary, cost history and
# file list along with the session id.

def test_hiding_threads():
    print("\nthreads can be hidden, and are never deleted to do it")
    import schema
    check("hidden is part of the record", "hidden" in schema.FIELDS)
    check("and defaults to visible", schema.default("hidden") is False)

    st = tmp_store()
    st.update("C:1", title="a conversation", kind="thread")
    st.update("C:2", title="a task run", kind="task")
    st.update("C:3", title="an old conversation", kind="thread")
    st.update("C:4", title="a running task run", kind="task",
              pending={"session_id": "s"})
    # update() stamps `updated` itself, so age has to be set behind it.
    old_ts = time.time() - 30 * 86400
    for k in ("C:2", "C:3", "C:4"):
        st._data[k]["updated"] = old_ts

    check("hiding one works", st.set_hidden("C:1", True) and st.get("C:1").get("hidden"))
    check("nothing is discarded by hiding",
          st.get("C:1")["title"] == "a conversation",
          "the title, summary, cost and files all have to survive it")
    check("unhiding works", st.set_hidden("C:1", False)
          and not st.get("C:1").get("hidden"))
    check("hiding an unknown thread is refused", not st.set_hidden("C:nope", True))

    hidden = st.hide_older_than(14, kinds=("task",))
    check("a bulk tidy hides old task runs", hidden == ["C:2"], f"got {hidden}")
    check("and leaves an old conversation alone",
          not st.get("C:3").get("hidden"),
          "a quiet conversation may still be one you come back to")
    check("and never hides a thread with a turn in flight",
          not st.get("C:4").get("hidden"))
    check("hiding does not count as activity",
          st.get("C:2")["updated"] < time.time() - 14 * 86400,
          "or a hidden thread would look freshly used")

    viz = (BASE / "visualizer.py").read_text()
    check("the list filters hidden out by default",
          "if (!showHidden && (s.hidden || s.shelved)) return false;" in viz)
    check("hidden threads stay reachable", "function toggleHidden" in viz,
          "put away is not thrown away")
    check("the hide control does not open the thread it hides",
          "ev.stopPropagation()" in viz)
    check("the payload says which are hidden", '"hidden": bool(entry.get("hidden"))' in viz)
    bot = (BASE / "bot.py").read_text()
    check("the bot exposes it", 'server.route("/hide", handle_hide)' in bot)
    check("and never deletes to satisfy it",
          "store.drop" not in bot[bot.index("def handle_hide"):bot.index("server = LocalServer")])


# --- putting a thread away must not queue it up to be deleted -----------------
# Hiding and deleting were built at different times and never met. A task run
# was hidden at 14 days, kept its old `updated` stamp (hiding is not activity),
# and the six-hourly sweeper then deleted it outright at 30 -- taking the
# title, summary, cost history and file list the hiding was meant to protect.

def test_putting_away_is_not_deleting():
    print("\nage alone is not a reason to delete a thread")
    st = tmp_store()
    st.update("C:1", title="a task run", kind="task",
              summary="what this run did", session_id="s-1")
    st.add_cost("C:1", 1.25)
    st.add_file("C:1", {"name": "report.md"})
    # update()/add_cost() stamp `updated` themselves, so age is set behind them.
    st._data["C:1"]["updated"] = time.time() - 15 * 86400
    check("the bulk tidy hides an old task run",
          st.hide_older_than(14, kinds=("task",)) == ["C:1"])

    st._data["C:1"]["updated"] = time.time() - 40 * 86400   # past the old horizon
    st.forget_empty(30)
    rec = st.get("C:1")
    check("a hidden thread is still there afterwards", rec is not None,
          "hiding it at 14 days and deleting it at 30 is not retention")
    check("its title survives", bool(rec) and rec.get("title") == "a task run")
    check("its summary survives", bool(rec) and rec.get("summary") == "what this run did")
    check("its cost history survives",
          bool(rec) and rec.get("costs") == [1.25] and rec.get("cost") == 1.25,
          "'I am done looking at this' is not 'erase what it cost me'")
    check("its file list survives", bool(rec) and len(rec.get("files") or []) == 1)

    # What may go: a husk. No title, no summary, no cost, no files, no events,
    # nothing pointing at it -- there is nothing in it to lose.
    st.update("C:husk", kind="task")
    st.update("C:referenced", kind="task")
    st.update("C:live", kind="task", pending={"session_id": "s"})
    st.update("C:resumable", kind="task", session_id="s-2")
    st.update("C:putaway", kind="task", hidden=True)
    for k in ("C:husk", "C:referenced", "C:live", "C:resumable", "C:putaway"):
        st._data[k]["updated"] = time.time() - 40 * 86400
    gone = st.forget_empty(30, keep={"C:referenced"})
    check("an empty record does age out", gone == ["C:husk"], f"got {gone}")
    check("but never one still holding a session to resume",
          st.get("C:resumable") is not None)
    check("and never one you deliberately put away",
          st.get("C:putaway") is not None,
          "deleting it would quietly undo the decision to hide it")
    check("but never one a task still points at", st.get("C:referenced") is not None,
          "deleting it orphans a task that may be waiting on a person")
    check("and never one with a turn in flight", st.get("C:live") is not None)

    check("age-only deletion is gone, not just uncalled",
          not hasattr(st, "sweep"),
          "leaving it in place invites it being wired back up")
    tree = ast.parse((BASE / "bot.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "store"]
    check("the six-hourly sweeper no longer deletes on age",
          not any(c.func.attr == "sweep" for c in calls))
    sweeper = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                    and n.name == "_sweep_pass"), None)
    forgets = [n for n in ast.walk(sweeper) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute) and n.func.attr == "forget_empty"]
    check("and tells the store which threads tasks point at",
          bool(forgets) and all(any(kw.arg == "keep" for kw in c.keywords) for c in forgets),
          "without `keep` it would delete a session a waiting task refers to")


# --- projects -----------------------------------------------------------------

def test_projects():
    import projects
    import tasks as T
    from projects import ProjectStore
    from tasks import TaskStore
    print("\nprojects")

    check("Asia Trip slugifies", projects.slugify("Asia Trip") == "asia-trip")
    check("an empty name still yields a slug", projects.slugify("") == "untitled")

    ps = ProjectStore(Path(tempfile.mkdtemp()) / "p.json")
    a = ps.ensure("Asia Trip")
    check("created on first use, no setup step", a["slug"] == "asia-trip")
    ps.ensure("asia-trip")
    check("the same project by name or slug is not duplicated", len(ps.all()) == 1)

    ps.ensure("Trader", scope={"cwd": "/w/trader"})
    check("a project may carry a default scope",
          ps.scope_for("trader")["cwd"] == "/w/trader")
    check("a project without a repo has no scope, and that is fine",
          ps.scope_for("asia-trip") == {})
    check("project is separate from scope in the task record",
          "project" in T.FIELDS and "scope" in T.FIELDS)

    ts = TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    ts.create("book flights", project="asia-trip", state=T.PROPOSED)
    ts.create("hotel", project="asia-trip")
    ts.create("reconnect", project="trader")
    ts.create("unfiled")
    check("tasks filter by project", len(ts.by_project("asia-trip")) == 2)
    check("unfiled tasks belong to no project", len(ts.by_project("")) == 1)

    rows = projects.summarise(ps.all(), list(ts.all().values()), T.NEEDS_ATTENTION)
    top = rows[0]
    check("projects sort by what needs you first",
          top["slug"] == "asia-trip" and top["needs"] == 1)
    check("counts distinguish open from total",
          top["open"] == 2 and top["total"] == 2)

    ts.create("stray", project="ghost")
    rows = projects.summarise(ps.all(), list(ts.all().values()), T.NEEDS_ATTENTION)
    check("a project filed against but never registered still appears",
          any(r["slug"] == "ghost" for r in rows))

    ps.set_archived("trader", True)
    check("archiving hides it from pickers",
          not any(p["slug"] == "trader" for p in ps.all(include_archived=False)))
    check("archiving keeps its tasks", len(ts.by_project("trader")) == 1)

    # A project with no repo gets no learnings (those scope by git remote), so
    # its context lives in a CLAUDE.md that Claude Code loads by itself -- no
    # injection machinery of ours.
    import tempfile as _tf
    projects.PROJECT_ROOT = Path(_tf.mkdtemp())
    ps.ensure("Asia Trip")
    ps.set_brief("asia-trip", "Kyoto over Osaka. April.")
    path = ps.brief_path("asia-trip")
    check("the brief is a CLAUDE.md in the project's own directory",
          path is not None and path.name == "CLAUDE.md" and path.exists())
    check("it reads back without the heading we add",
          ps.brief_for("asia-trip") == "Kyoto over Osaka. April.")
    check("the project's directory becomes its scope",
          ps.scope_for("asia-trip").get("cwd") == str(path.parent),
          "running there is what makes CLAUDE.md load")
    ps.set_brief("asia-trip", "")
    check("clearing removes the file rather than leaving an empty one",
          not path.exists())

    ps.ensure("Trader", scope={"cwd": "/w/trader", "repo": "github.com/x/trader"})
    check("a repo-backed project has no Silkworm-owned home",
          ps.home("trader") is None,
          "its CLAUDE.md belongs to the user; learnings cover it")

    # `!project` sets cwd from the thread and leaves `repo` empty, so the record
    # alone said "repo-less" for a real checkout -- and set_brief would have
    # replaced a hand-written CLAUDE.md with a 150-word generated one.
    real = Path(_tf.mkdtemp()) / "checkout"
    (real / ".git").mkdir(parents=True)
    (real / "CLAUDE.md").write_text("# Hand written\n\nDo not clobber me.\n")
    ps.ensure("Odin", scope={"cwd": str(real)})          # note: no repo field
    check("a directory that is itself a repo is never ours to write",
          ps.home("odin") is None,
          "the filesystem is the authority, not a field !project never sets")
    ps.set_brief("odin", "a generated brief")
    check("so set_brief leaves a real CLAUDE.md alone",
          (real / "CLAUDE.md").read_text().startswith("# Hand written"),
          "overwriting 289 lines of guide with a paragraph is not recoverable")
    check("and records nothing as having been written",
          (ps.get("odin") or {}).get("brief_at") == 0.0)
    before = ps.get("trader")
    ps.set_brief("trader", "we should not write this")
    check("writing a brief to a repo-backed project is a no-op",
          ps.get("trader").get("scope") == before.get("scope"))

    # The brief moved from a record field to a file; a reader left pointing at
    # the old field silently made every rewrite start from scratch, discarding
    # everything learned so far.
    bot_src = (BASE / "bot.py").read_text()
    rb = bot_src[bot_src.index("def refresh_brief("):bot_src.index("def refresh_summary(")]
    check("the rewrite reads the brief from where it now lives",
          "project_store.brief_for(slug)" in rb)
    check("the rewrite does not read the removed record field",
          'rec.get("brief")' not in rb,
          "that always returns None and throws away the existing brief")
    check("a conversation in a project-bound thread updates the brief too",
          bot_src.count("refresh_brief(") >= 3,
          "definition plus both trigger sites")

    check("no prompt-injection machinery remains",
          not hasattr(projects, "context_block"),
          "CLAUDE.md is the injection")

    bot = (BASE / "bot.py").read_text()
    check("the bot no longer injects project context by hand",
          "project_context(" not in bot,
          "Claude Code loads CLAUDE.md from the working directory itself")
    check("a project task is given its project's directory to run in",
          "project_store.home(proj, create=True)" in bot)
    ft = bot[bot.index("def file_thread_under("):bot.index("def github_for_thread_project(")]
    check("filing a thread under a repo-less project moves it into that directory",
          "project_store.home(slug, create=True)" in ft and "where = str(home)" in ft,
          "its CLAUDE.md only loads if the thread runs there; "
          "test_filing_a_thread_never_rewrites_a_projects_scope runs it")
    check("the brief is rewritten, not appended",
          "do not append" in (BASE / "projects.py").read_text())
    check("completing a task folds the outcome back in",
          "refresh_brief(task.get(" in bot)
    check("a thread's project is inherited by its tasks",
          "project=file_by_directory(key, cwd)," in bot
          and 'return entry.get("project") or ""' in bot)
    check("!project files a thread", 'elif lower.startswith("!project")' in bot)


# --- tasks.json only ever grew ---------------------------------------------------
# 412 records in 1.1 MB after a fortnight, 367 of them done, and the whole file
# re-serialised on every create, update, transition and claim -- because every
# Slack turn files a task and nothing ever took one away. Sessions had already
# settled this (cdbe005): put the record away, don't destroy it.

def test_task_retention():
    import tasks as T
    from tasks import TaskStore
    print("\nfinished tasks are compacted, not deleted")
    st = TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    check("compacted is part of the record", "compacted" in T.FIELDS)
    check("and defaults to whole", T.default("compacted") is False)

    def aged(state, days, **fields):
        """A task parked in `state`, last touched `days` ago."""
        t = st.create("do a thing", **fields)
        st.transition(t["id"], T.RUNNING)
        st.update(t["id"], result={"text": "x" * 4000, "cost": 0.42})
        st.transition(t["id"], state, "finished")
        # update()/transition() stamp `updated`, so age has to be set behind them.
        st._data[t["id"]]["updated"] = time.time() - days * 86400
        return t["id"]

    old = aged(T.DONE, 30, title="landed a month ago", project="silkworm")
    recent = aged(T.DONE, 1)
    cancelled = aged(T.CANCELLED, 30)
    # A task that went through the review gate. The dashboard renders the
    # verdict on every row -- review(t) is called for all of them, and `list`
    # returns finished tasks -- so compacting must not blank out "Review
    # passed" on work whose review is the whole record that it was checked.
    reviewed = aged(T.DONE, 30)
    st.update(reviewed, result={**(st.get(reviewed)["result"] or {}),
                                "review": {"ok": True, "summary": "looks right",
                                           "findings": ["one nit"], "parsed": True},
                                "landed": "abc1234"})
    st._data[reviewed]["updated"] = time.time() - 30 * 86400
    # And one whose landing refused. This is the marker that stops a finished
    # task with unmerged commits reading as plainly done, so dropping it at a
    # fortnight would defer that silence rather than end it.
    stranded = aged(T.DONE, 30)
    st.update(stranded, result={**(st.get(stranded)["result"] or {}),
                                "landing": {"eligible": True, "landed": False,
                                            "stage": "rebase",
                                            "branch": "silkworm/tsk_old",
                                            "detail": "conflicts"}})
    st._data[stranded]["updated"] = time.time() - 30 * 86400

    done = st.compact_older_than(14)
    check("old finished tasks are compacted",
          sorted(done) == sorted([old, cancelled, reviewed, stranded]), f"got {done}")

    rec = st.get(old)
    check("the record itself survives", rec is not None and rec["state"] == T.DONE)
    check("and keeps what the history is read for",
          rec["title"] == "landed a month ago" and rec["project"] == "silkworm"
          and rec["created"] and rec["updated"] and rec["attempts"] == 1)
    check("cost survives", (rec["result"] or {}).get("cost") == 0.42)
    check("the reply text is dropped", not (rec["result"] or {}).get("text"))
    check("the event log is dropped", rec["events"] == [])
    check("and it says so", rec["compacted"] is True,
          "or an empty result reads as work that produced nothing")
    check("compacting does not count as activity",
          rec["updated"] < time.time() - 14 * 86400,
          "or a compacted task looks freshly worked on")
    rv = (st.get(reviewed)["result"] or {}).get("review") or {}
    check("the review verdict survives compaction",
          rv.get("ok") is True and rv.get("summary") == "looks right"
          and rv.get("findings") == ["one nit"],
          "the dashboard shows it on finished rows too, and it is the record "
          "that the work was checked at all")
    check("but the reviewed task's own reply text still goes",
          not (st.get(reviewed)["result"] or {}).get("text"))
    check("the commit it landed as survives too",
          (st.get(reviewed)["result"] or {}).get("landed") == "abc1234",
          "nothing else records that the work reached the base branch, so "
          "dropping it makes a landed task look like a discarded one")
    landing = (st.get(stranded)["result"] or {}).get("landing") or {}
    check("and so does a landing that refused",
          landing.get("stage") == "rebase" and landing.get("branch") == "silkworm/tsk_old",
          "losing it lets a task with unmerged commits go back to reading as done")
    viz = (BASE / "visualizer.py").read_text()
    check("every task row asks for the verdict, not just the waiting ones",
          "${review(t)}" in viz and "review" in T.RESULT_KEEPS,
          "so anything review() reads has to survive compaction")
    check("recent work is left whole",
          len((st.get(recent)["result"] or {}).get("text") or "") == 4000)

    # `compacted` is only worth carrying if it means something. A task that was
    # cancelled before it ever ran is old, finished and empty, and it must come
    # out of the sweep unbadged -- otherwise the flag reads as "we threw the
    # detail away" on work that never had any, and an empty result stops being
    # answerable either way.
    barren = st.create("cancelled before it ran")
    st.transition(barren["id"], T.CANCELLED, "never started")
    st._data[barren["id"]]["updated"] = time.time() - 30 * 86400
    st._data[barren["id"]]["events"] = []
    check("a finished task that produced nothing is not compacted",
          st.compact_older_than(14) == [] and st.get(barren["id"])["compacted"] is False,
          "or `compacted` stops distinguishing dropped detail from no detail")
    check("a second pass finds nothing to do", st.compact_older_than(14) == [])
    check("compacted records still carry every field",
          set(TaskStore(st._path).get(old)) == set(T.FIELDS))

    # The point of the whole exercise.
    ages = {}
    for state in T.NEEDS_ATTENTION:
        via = {T.PROPOSED: None, T.FAILED: T.FAILED,
               T.AWAITING_APPROVAL: T.AWAITING_APPROVAL,
               T.NEEDS_INPUT: T.NEEDS_INPUT}[state]
        t = st.create("needs you", state=T.PROPOSED if via is None else T.QUEUED)
        if via is not None:
            st.transition(t["id"], T.RUNNING)
            st.update(t["id"], result={"text": "y" * 4000, "cost": 1.0})
            st.transition(t["id"], via)
        else:
            st.update(t["id"], result={"text": "y" * 4000, "cost": 1.0})
        st._data[t["id"]]["updated"] = time.time() - 3650 * 86400
        ages[state] = t["id"]

    check("every attention state is covered", set(ages) == set(T.NEEDS_ATTENTION))
    check("nothing needing attention is compacted, however old",
          st.compact_older_than(14) == [],
          "that is the list you work from -- ten years old or not")
    for state, tid in ages.items():
        r = st.get(tid)
        check(f"{state} keeps its detail",
              not r["compacted"] and len((r["result"] or {}).get("text") or "") == 4000
              and (r["events"] or state == T.PROPOSED))

    # Today NEEDS_ATTENTION and TERMINAL do not overlap, so the TERMINAL test
    # alone would hold all of the above and the NEEDS_ATTENTION guard would be
    # decoration -- passing without ever being consulted. It is there for the
    # drift where they do overlap: `failed` is a plausible future terminal
    # state, since a task that failed is finished. So make that the case and
    # check the guard, not the coincidence, is what protects the list.
    terminal = T.TERMINAL
    try:
        T.TERMINAL = tuple(terminal) + (T.FAILED,)
        check("attention states are held by name, not by not being terminal",
              st.compact_older_than(14) == []
              and not st.get(ages[T.FAILED])["compacted"],
              "if `failed` ever becomes terminal, this is the only thing left "
              "standing between the board and a wiped failure")
    finally:
        T.TERMINAL = terminal

    # And a live task is not finished either, however long it has been running.
    running = st.create("still going", driver="queue")
    st.transition(running["id"], T.RUNNING)
    st.update(running["id"], result={"text": "z" * 4000})
    st._data[running["id"]]["updated"] = time.time() - 100 * 86400
    check("in-flight work is never compacted", st.compact_older_than(14) == [])

    bot = (BASE / "bot.py").read_text()
    check("the sweeper runs it", "task_store.compact_older_than(TASK_COMPACT_AFTER_DAYS)" in bot)
    check("the window is configurable",
          'os.environ.get("TASK_COMPACT_AFTER_DAYS"' in bot)
    check("and never deletes a task to save space", "task_store.drop" not in bot)
    env = (BASE / ".env.example").read_text()
    check("documented next to the session window",
          "TASK_COMPACT_AFTER_DAYS" in env
          and 0 < env.index("TASK_COMPACT_AFTER_DAYS") - env.index("SESSION_MAX_AGE_DAYS") < 600)
# --- state files survive being killed mid-save --------------------------------
# Every store used to save with write_text(), which truncates the file and then
# writes it. A restart landing in that window left a half-written tasks.json,
# and the load path parsed it outside any try -- so the bot could not start at
# all, with 400+ task records and every thread's history on the floor.

def test_atomic_persistence():
    import threading as _th
    import jsonstore
    import projects as _projects
    import tasks as _tasks
    from learnings import LearningStore

    print("\nstate files survive being killed mid-save")
    d = Path(tempfile.mkdtemp())

    def attempt(fn, *a, **kw):
        """Run `fn`, reporting a raise as a value rather than as a crash.

        Every check below is about a guard that can be broken, and a broken
        guard usually raises -- a store that will not open, a file that is no
        longer there. Without this the first one to go takes the rest of the
        test with it and says nothing about them.
        """
        try:
            return fn(*a, **kw)
        except Exception as exc:                 # noqa: BLE001 - it is the answer
            return exc

    def reads(path):
        return attempt(lambda: json.loads(path.read_text()))

    # No state file may go back to truncate-then-write. Checked on the parse
    # tree, not the text, so a comment mentioning write_text doesn't pass for
    # one -- and by the *name being written to* rather than by which module it
    # is in, because the last one found doing this was learnings_git.py writing
    # through a parameter, which a `self._path` rule could not see and a list
    # of store modules did not include.
    state = {"_path",                       # the four stores
             "learnings_file", "state_path",  # passed in as a parameter
             "EMAIL_STATE_FILE", "HARVEST_STATE", "LEARNINGS_FILE", "BOARD_STATE",
             "SESSIONS_FILE", "TASKS_FILE", "PROJECTS_FILE"}
    def writes_in_place(node):
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("write_text", "write_bytes"))
    def written_to(node):
        target = node.func.value
        return (target.attr if isinstance(target, ast.Attribute)
                else target.id if isinstance(target, ast.Name) else None)
    offenders, saves, scanned = [], 0, 0
    for mod in sorted(f.name for f in BASE.glob("*.py")):
        if mod == "jsonstore.py":           # the one place that may, carefully
            continue
        scanned += 1
        tree = ast.parse((BASE / mod).read_text())
        for node in ast.walk(tree):
            # (Other files written here -- a project's CLAUDE.md -- are fine.)
            if writes_in_place(node) and written_to(node) in state:
                offenders.append(f"{mod}:{node.lineno}")
            if isinstance(node, ast.FunctionDef) and node.name == "_save":
                saves += 1
                offenders += [f"{mod}:{n.lineno}" for n in ast.walk(node)
                              if writes_in_place(n)]
    # Ten: the four stores, the task board's message pointer (home.Board),
    # the operator's state (operator_role.State), push subscriptions
    # (push.PushStore), health results (health.HealthStore), the outage log
    # (outages.py) and the watchdog's counters (watchdog.py).
    check("every store's _save was found to check", saves == 10, f"found {saves}")
    check("and every module was looked at", scanned > 20, f"{scanned}")
    check("nothing writes a state file in place", not offenders, f"at {offenders}")

    # A save leaves either the whole old file or the whole new one.
    path = d / "tasks.json"
    store = _tasks.TaskStore(path)
    ids = [store.create(f"task {i}")["id"] for i in range(20)]
    before = path.read_text()
    boom = jsonstore._scratch(path)

    real_replace = os.replace
    def die_before_replace(src, dst):        # a kill after the temp file is written
        if str(src).startswith(str(path) + ".tmp"):
            raise KeyboardInterrupt("killed mid-save")
        return real_replace(src, dst)
    os.replace = die_before_replace
    try:
        store.create("the one that gets killed")
    except KeyboardInterrupt:
        pass
    finally:
        os.replace = real_replace
    check("a kill mid-save leaves the previous file whole",
          path.read_text() == before and json.loads(path.read_text()))
    litter = sorted(f.name for f in d.iterdir() if ".tmp." in f.name)
    check("and leaves no half-written temp file behind", not litter, f"{litter}")
    check("the half-written copy never reached the store", not boom.exists())

    # Truncate the primary the way a SIGKILL mid-write would, then come back up.
    store.create("written after the near miss")
    live = json.loads(path.read_text())
    path.write_text(json.dumps(live)[: len(json.dumps(live)) // 2])
    try:
        reopened = _tasks.TaskStore(path)
        raised = None
    except Exception as exc:                 # noqa: BLE001 - reporting it is the test
        reopened, raised = None, exc
    check("a truncated tasks.json still comes up", raised is None, f"raised {raised!r}")
    if reopened is not None:
        check("holding its records", all(reopened.get(t) for t in ids),
              f"{len(reopened.all())} of {len(ids) + 1} records")
        check("not silently empty", len(reopened.all()) >= len(ids))
    check("the unreadable copy is kept", jsonstore.corrupt_path(path).exists())
    check("and the primary is readable again", bool(reads(path)) is True,
          f"{reads(path)!r}")

    # The same for sessions, projects and learnings: same failure, same fix.
    sp = d / "sessions.json"
    sessions = SessionStore(sp)
    sessions.update("C:1", session_id="abc")
    sessions.update("C:2", session_id="def")
    sp.write_text(sp.read_text()[:40])
    back = attempt(SessionStore, sp)
    check("a truncated sessions.json keeps its threads",
          not isinstance(back, Exception)
          and (back.get("C:1") or {}).get("session_id") == "abc", f"{back!r}")

    pp = d / "projects.json"
    ps = _projects.ProjectStore(pp)
    ps.ensure("Saga"); ps.ensure("Cadence")
    pp.write_text(pp.read_text()[:40])
    back = attempt(_projects.ProjectStore, pp)
    check("a truncated projects.json keeps its projects",
          not isinstance(back, Exception) and back.get("saga") is not None,
          f"{back!r}")

    lp = d / "learnings.json"
    ls = LearningStore(lp)
    ls.add("do", "commit before a long build")
    ls.add("avoid", "never stash across worktrees")
    lp.write_text(lp.read_text()[:30])
    back = attempt(LearningStore, lp)
    check("a truncated learnings.json keeps its learnings",
          not isinstance(back, Exception) and len(back.all()) == 2, f"{back!r}")

    # The backup tracks the last finished save, not the one before it, so
    # recovering costs at most the single write that was interrupted.
    check("the backup is the last completed save, not a stale one",
          (reads(jsonstore.backup_path(sp)) or {}) != {} and not isinstance(
              reads(jsonstore.backup_path(sp)), Exception)
          and reads(jsonstore.backup_path(sp)).keys() == {"C:1", "C:2"},
          f"{reads(jsonstore.backup_path(sp))!r}")
    # And it is a file of its own. A hard link would share an inode with the
    # primary, so truncating one would truncate both -- no backup at all.
    check("the backup is a separate file, not a link to the primary",
          attempt(lambda: jsonstore.backup_path(sp).stat().st_ino
                  != sp.stat().st_ino) is True)

    # The shared learnings directory has a .gitignore of its own, written when
    # it was first set up -- before these sidecars existed. Re-running init has
    # to add what is missing rather than skip the file for already being there,
    # or one machine's recovery copy gets pushed to every other machine.
    import learnings_git
    shared = d / "shared"
    shared.mkdir()
    (shared / ".gitignore").write_text("harvest_state.json\n")
    learnings_git.init(shared / "learnings.json")
    ignored = (shared / ".gitignore").read_text().split()
    check("init adds the sidecars to a .gitignore that already exists",
          {"*.json.prev", "*.json.corrupt", "*.json*.tmp.*"} <= set(ignored), f"{ignored}")
    check("and keeps what was already in it", "harvest_state.json" in ignored)
    learnings_git.init(shared / "learnings.json")
    check("without doubling up when run again",
          (shared / ".gitignore").read_text().split() == ignored)

    # A store that has only ever been read has no backup yet -- which would
    # have left the first boot after this shipped with a file it had just
    # proved good and no second copy of it. Reading one seeds it.
    seeded = d / "seeded.json"
    seeded.write_text(json.dumps({"written": "by an older build"}))
    check("no backup before anything reads it",
          not jsonstore.backup_path(seeded).exists())
    check("a clean read seeds one", attempt(jsonstore.load, seeded)
          == {"written": "by an older build"}
          and reads(jsonstore.backup_path(seeded)) == {"written": "by an older build"})
    # ...and a reader that does not own the file does not write into its directory.
    unowned = d / "unowned.json"
    unowned.write_text(json.dumps({"owner": "the bot"}))
    jsonstore.load(unowned, repair=False)
    check("but a read-only caller seeds nothing",
          not jsonstore.backup_path(unowned).exists())

    # The fallback has to be the backup actually being read, not the primary's
    # remnants happening to parse: give the two copies different contents and
    # name which one came back.
    pick = d / "pick.json"
    jsonstore.save(pick, {"kept": "the finished save"})
    pick.write_text(json.dumps({"kept": "a write that was interrupted"})[:20])
    check("recovery reads the backup, not what is left of the primary",
          attempt(jsonstore.load, pick) == {"kept": "the finished save"})

    # The backup is refreshed after the primary lands, not before, so it can
    # only ever hold a save that finished. A primary write that dies takes
    # nothing with it.
    ordered = d / "ordered.json"
    jsonstore.save(ordered, {"n": 1})
    def die_replacing_primary(src, dst):
        if str(dst) == str(ordered):
            raise KeyboardInterrupt("killed before the primary landed")
        return real_replace(src, dst)
    os.replace = die_replacing_primary
    try:
        jsonstore.save(ordered, {"n": 2})
    except KeyboardInterrupt:
        pass
    finally:
        os.replace = real_replace
    check("a save that never landed does not reach the backup",
          reads(jsonstore.backup_path(ordered)) == {"n": 1},
          f"{reads(jsonstore.backup_path(ordered))!r}")

    # Replacing a file with a fresh temp file would hand it whatever the umask
    # says; write_text() wrote through the old one and kept its mode.
    moded = d / "moded.json"
    jsonstore.save(moded, {"a": 1})
    os.chmod(moded, 0o600)
    jsonstore.save(moded, {"a": 2})
    check("a save keeps the mode the file already had",
          moded.stat().st_mode & 0o777 == 0o600,
          oct(moded.stat().st_mode & 0o777))

    # A reader that does not own the file -- the dashboard is a second process
    # on the bot's live state -- gets the fallback without touching anything.
    # Renaming the primary out from under a running bot, which holds the real
    # records in memory, is how a readable situation becomes a lost one.
    theirs = d / "theirs.json"
    jsonstore.save(theirs, {"owner": "the bot"})
    theirs.write_text("{half")
    check("a read-only caller still recovers",
          attempt(jsonstore.load, theirs, repair=False) == {"owner": "the bot"})
    check("without moving the owner's file aside or rewriting it",
          # attempt(): a reader that broke this rule has renamed the file away,
          # so reading it raises -- which would take the rest of the case with it.
          attempt(theirs.read_text) == "{half"
          and not jsonstore.corrupt_path(theirs).exists())
    check("leaving the repair to whoever owns it",
          attempt(jsonstore.load, theirs) == {"owner": "the bot"}
          and jsonstore.corrupt_path(theirs).exists()
          and reads(theirs) == {"owner": "the bot"})

    # Recovering and then failing to tidy up must not cost the records: we are
    # holding them, and a full disk is no reason to hand back nothing.
    stuck = d / "stuck.json"
    jsonstore.save(stuck, {"records": "recovered"})
    stuck.write_text("{half")
    real_write = jsonstore._write
    def no_room(path, text):
        raise OSError(28, "No space left on device")
    jsonstore._write = no_room
    try:
        got = attempt(jsonstore.load, stuck)
    finally:
        jsonstore._write = real_write
    check("a recovery that cannot write itself back still returns the records",
          got == {"records": "recovered"}, f"{got!r}")

    # Losing both copies must be loud. Starting empty here is the failure mode
    # that makes the loss invisible: an empty store looks like a fresh install.
    bp = d / "both.json"
    jsonstore.save(bp, {"a": 1}); jsonstore.save(bp, {"a": 2})
    bp.write_text("{trunc")
    jsonstore.backup_path(bp).write_text("{also trunc")
    refusals = [attempt(jsonstore.load, bp, default={}) for _ in range(3)]
    check("both copies unreadable raises rather than starting empty",
          isinstance(refusals[0], jsonstore.CorruptStore), f"{refusals[0]!r}")
    check("and says so in terms of both files",
          bool(refusals[0]) and "both.json.prev" in str(refusals[0]), str(refusals[0])[:120])
    # The refusal has to hold every time it is asked. Setting the wreckage
    # aside here is what made it last exactly one boot: the primary was then
    # *missing* next time, which reads as a fresh install and returns empty
    # without so much as a log line -- and the bot's launchd job has KeepAlive,
    # so it would be asked again ten seconds later and come up with nothing.
    check("and keeps refusing, rather than lasting one boot",
          all(isinstance(r, jsonstore.CorruptStore) for r in refusals),
          f"{[type(r).__name__ for r in refusals]}")
    check("with both files left exactly where they were",
          attempt(bp.read_text) == "{trunc"
          and attempt(jsonstore.backup_path(bp).read_text) == "{also trunc"
          and not jsonstore.corrupt_path(bp).exists())

    # Same, for the case that makes it reachable at all: the first boot on this
    # code, on a file the *old* code left truncated, with no backup yet.
    first = d / "firstboot.json"
    first.write_text('{"tsk_1": {"goal": "half a rec')
    again = [attempt(jsonstore.load, first, default={}) for _ in range(3)]
    check("a truncated store with no backup refuses every time",
          all(isinstance(r, jsonstore.CorruptStore) for r in again),
          f"{[type(r).__name__ for r in again]}")
    check("and is still there to be recovered by hand",
          attempt(first.read_text) == '{"tsk_1": {"goal": "half a rec')

    # And the same when the primary is gone rather than broken -- deleted by
    # hand, or by a tidy-up -- with a backup that will not parse. There is no
    # way to tell that from a fresh install by looking, but the backup being
    # there says a store existed, so it is not one.
    gone = d / "gone.json"
    jsonstore.backup_path(gone).write_text("{half a backup")
    check("a missing store with an unreadable backup is not a fresh install",
          isinstance(attempt(jsonstore.load, gone, default={}), jsonstore.CorruptStore),
          f"{attempt(jsonstore.load, gone, default={})!r}")
    check("but a directory with nothing in it is",
          attempt(jsonstore.load, d / "never-existed.json", default={}) == {})

    # Readable JSON that is not a store is corruption too, not an empty store.
    # The callers coerce with `or {}`, so without this it loads as nothing and
    # the next save writes that nothing over both copies.
    shaped = d / "shaped.json"
    jsonstore.save(shaped, {"tsk_1": {"goal": "real work"}})
    for wrong in ("null", "[]", "0", '"a string"'):
        shaped.write_text(wrong)
        jsonstore.corrupt_path(shaped).unlink(missing_ok=True)
        got = attempt(jsonstore.load, shaped, default={})
        check(f"a store holding {wrong} recovers rather than coming up empty",
              got == {"tsk_1": {"goal": "real work"}}, f"{got!r}")
    shaped.write_text("{}")
    check("while a store that is genuinely empty is left to be empty",
          attempt(jsonstore.load, shaped, default={}) == {})

    # Not being able to read a file is not the same as the file being bad: a
    # permission or a momentarily exhausted fd table would otherwise get a
    # perfectly good store renamed out from under the next process to try.
    unread = d / "unread.json"
    jsonstore.save(unread, {"records": "here"})
    real_read = Path.read_text
    def cannot_read(self, *a, **kw):
        if self.name.startswith("unread.json"):
            raise OSError(24, "Too many open files")
        return real_read(self, *a, **kw)
    Path.read_text = cannot_read
    try:
        jsonstore.load(unread)
        raised = None
    except Exception as exc:                     # noqa: BLE001
        raised = exc
    finally:
        Path.read_text = real_read
    check("an unreadable-but-fine file still raises rather than starting empty",
          isinstance(raised, jsonstore.CorruptStore))
    check("and is left where it is, not renamed to .corrupt",
          unread.exists() and not jsonstore.corrupt_path(unread).exists())
    check("so the next attempt just works",
          attempt(jsonstore.load, unread) == {"records": "here"})

    # ...and that holds with a *readable* backup sitting right next to it, which
    # is the whole case. The backup is an older save; handing it back for a
    # primary we could not open means the bot boots on stale records and writes
    # them over a file that was fine, so the guard has to fire before the
    # fallback is even consulted. Declining to rename the primary and then
    # overwriting it, which is where this landed twice before, is worse than
    # renaming it: it is the same loss with the evidence destroyed too.
    flaky = d / "flaky.json"
    jsonstore.save(flaky, {"records": "the older save"})
    jsonstore._write(flaky, '{"records": "newer, and perfectly good"}')
    def only_primary(self, *a, **kw):
        if self.name == "flaky.json":            # not flaky.json.prev
            raise OSError(13, "Permission denied")
        return real_read(self, *a, **kw)
    Path.read_text = only_primary
    try:
        got = jsonstore.load(flaky)
        raised = None
    except Exception as exc:                     # noqa: BLE001
        got, raised = None, exc
    finally:
        Path.read_text = real_read
    check("a readable backup does not excuse substituting it for an unopenable primary",
          isinstance(raised, jsonstore.CorruptStore), f"returned {got!r}")
    check("and the reason names the file it refused to rewind",
          bool(raised) and "flaky.json.prev" in str(raised), str(raised))
    check("the primary is left byte-for-byte as it was",
          reads(flaky) == {"records": "newer, and perfectly good"}, f"{reads(flaky)!r}")
    check("with nothing set aside",
          not jsonstore.corrupt_path(flaky).exists())
    check("so once the fd table clears, the newer save is still there",
          attempt(jsonstore.load, flaky) == {"records": "newer, and perfectly good"})

    # load() refuses before it ever gets there, so _set_aside's own version of
    # that rule is never reached through it. Checked directly rather than left
    # to be believed: it is the contract of the function that does the renaming,
    # and the next caller to arrive at it is the one that would learn otherwise.
    fine = d / "fine.json"
    fine.write_text('{"records": "perfectly readable"}')
    moved = jsonstore._set_aside(fine, OSError(13, "Permission denied"))
    check("setting aside is refused outright for an error that is not the contents",
          moved is False and fine.exists() and not jsonstore.corrupt_path(fine).exists())
    moved = jsonstore._set_aside(fine, json.JSONDecodeError("bad", "{", 0))
    check("and done for one that is", moved is True and not fine.exists()
          and jsonstore.corrupt_path(fine).exists())

    # The refusal binds whoever writes. A read-only reader -- the dashboard on
    # the bot's live files -- cannot rewind anything, so it still gets the
    # fallback: a page drawn from a slightly old copy beats failing the page
    # over a passing EACCES. What it must not do is touch either file.
    Path.read_text = only_primary
    try:
        seen = attempt(jsonstore.load, flaky, repair=False)
    finally:
        Path.read_text = real_read
    check("a reader that writes nothing still gets the fallback",
          seen == {"records": "the older save"}, f"{seen!r}")
    check("and leaves the owner's primary alone",
          reads(flaky) == {"records": "newer, and perfectly good"}
          and not jsonstore.corrupt_path(flaky).exists())

    # A watermark takes the same refusal as a return-empty rather than a rewind.
    flaky_wm = d / "flakywm.json"
    jsonstore.save(flaky_wm, {"seen": "older"})
    jsonstore._write(flaky_wm, '{"seen": "newer"}')
    def only_wm(self, *a, **kw):
        if self.name == "flakywm.json":
            raise OSError(24, "Too many open files")
        return real_read(self, *a, **kw)
    Path.read_text = only_wm
    try:
        got = attempt(jsonstore.load, flaky_wm, default={}, strict=False)
    finally:
        Path.read_text = real_read
    check("a watermark rescans rather than resuming from a stale backup", got == {})
    check("and its file is untouched too", reads(flaky_wm) == {"seen": "newer"})

    # Recovery empties the primary slot by moving the wreckage to .corrupt. If
    # that move fails, the corrupt file is still the only copy of what went
    # wrong -- so writing the recovered contents over it would destroy the
    # evidence this promised to keep, and the log would say it had kept it.
    stubborn = d / "stubborn.json"
    jsonstore.save(stubborn, {"records": "recoverable"})
    stubborn.write_text("{half")
    real_replace = os.replace
    def wont_rename(src, dst, *a, **kw):
        if str(dst).endswith(jsonstore.CORRUPT_SUFFIX):
            raise OSError(1, "Operation not permitted")
        return real_replace(src, dst, *a, **kw)
    said = io.StringIO()
    listener = logging.StreamHandler(said)
    jsonstore.log.addHandler(listener)
    os.replace = wont_rename
    try:
        got = attempt(jsonstore.load, stubborn)
    finally:
        os.replace = real_replace
        jsonstore.log.removeHandler(listener)
    check("a recovery that cannot set the wreckage aside still returns the records",
          got == {"records": "recoverable"}, f"{got!r}")
    check("and does not claim to have kept a copy it could not move",
          jsonstore.CORRUPT_SUFFIX not in said.getvalue(), said.getvalue())
    check("and leaves the wreckage rather than writing over the only copy of it",
          stubborn.read_text() == "{half", stubborn.read_text()[:40])
    check("so the next boot recovers again instead of finding nothing",
          attempt(jsonstore.load, stubborn) == {"records": "recoverable"}
          and attempt(jsonstore.corrupt_path(stubborn).read_text) == "{half")

    # A watermark is not records: re-scanning is cheaper than refusing to start.
    wm = d / "watermark.json"
    wm.write_text("{trunc")
    check("a watermark falls back to empty instead",
          attempt(jsonstore.load, wm, default={"x": 1}, strict=False) == {"x": 1})

    # Readers only ever see whole files, even while writers are hammering.
    hot = d / "hot.json"
    hs = _tasks.TaskStore(hot)
    hs.create("seed")
    # `seen`, not `reads`: that name is the helper defined at the top of this
    # test, and rebinding it to a list leaves anything added below calling a
    # list.
    stop, bad, seen = _th.Event(), [], []
    def writer(n):
        for i in range(40):
            hs.create(f"w{n}-{i}")
    def reader():
        while not stop.is_set():
            try:
                seen.append(len(json.loads(hot.read_text())))
            except Exception as exc:         # noqa: BLE001
                bad.append(repr(exc))
    r = _th.Thread(target=reader, daemon=True); r.start()
    ws = [_th.Thread(target=writer, args=(n,)) for n in range(4)]
    [w.start() for w in ws]; [w.join() for w in ws]
    stop.set(); r.join(timeout=5)
    check("concurrent readers never see a partial file", not bad, f"{bad[:2]}")
    check("the reader actually read during the writes", len(seen) > 1)
    check("every write landed", len(hs.all()) == 161)

    # A temp file is cleaned up in a `finally`, which a signal does not run --
    # and a signal is the case this module is about. Every restart landing
    # mid-save otherwise leaves a full copy of the store behind: 3 MB for the
    # board, gitignored and so invisible to `git status`, forever. Killed for
    # real here, with the signal `silkworm restart` actually sends.
    killed = d / "killed.json"
    jsonstore.save(killed, {"a": 1})
    child = subprocess.Popen([sys.executable, "-c", f"""
import os, sys, time
sys.path.insert(0, {str(BASE)!r})
import jsonstore
from pathlib import Path
p = Path({str(killed)!r})
real = os.replace
def stall(src, dst, *a, **kw):
    if str(src).startswith(str(p) + ".tmp"):
        time.sleep(30)
    return real(src, dst, *a, **kw)
os.replace = stall
jsonstore.save(p, {{"a": 2}})
"""])
    for _ in range(200):                     # wait for its temp file to appear
        if any(".tmp." in f.name for f in d.iterdir()):
            break
        time.sleep(0.05)
    child.kill(); child.wait(timeout=10)
    orphans = [f.name for f in d.iterdir() if f.name.startswith("killed.json.tmp.")]
    check("a real kill mid-save does leave its temp file behind", orphans, f"{orphans}")
    # ...while a *different* process that is still writing keeps its own. The
    # pid in the name is the only thing separating an orphan from a write in
    # progress, so the live one here belongs to a real, live, unrelated pid --
    # this process's own is skipped a step earlier and would prove nothing.
    alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    theirs = killed.with_name(f"{killed.name}.tmp.{alive.pid}.1")
    theirs.write_text("in flight")
    check("the next load sweeps the orphan up",
          attempt(jsonstore.load, killed, default={}) == {"a": 1}
          and not any(f.name in orphans for f in d.iterdir()), f"{orphans}")
    check("and leaves another live writer's temp file alone",
          theirs.exists(), "a sweep that cannot tell them apart deletes a write in progress")
    alive.kill(); alive.wait(timeout=10)
    jsonstore.load(killed, default={})
    check("but takes it once that writer is gone too", not theirs.exists())

    # The temp file holds the whole store before it is renamed into place, so
    # it must not be readable by anyone the store itself is not.
    private = d / "private.json"
    jsonstore.save(private, {"a": 1})
    os.chmod(private, 0o600)
    at_rename = []
    def note_mode(src, dst, *a, **kw):
        at_rename.append(os.stat(src).st_mode & 0o777)
        return real_replace(src, dst, *a, **kw)
    os.replace = note_mode
    try:
        jsonstore.save(private, {"a": 2})
    finally:
        os.replace = real_replace
    check("the temp file is never wider than the store it becomes",
          at_rename and all(m == 0o600 for m in at_rename),
          f"{[oct(m) for m in at_rename]}")

    # Finally, for real: a separate process opening a half-written store.
    boot = d / "boot.json"
    _tasks.TaskStore(boot).create("survive me")
    ts = _tasks.TaskStore(boot); ts.create("and me")
    boot.write_text(boot.read_text()[:70])
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); import tasks;"
         "print(len(tasks.TaskStore(__import__('pathlib').Path(%r)).all()))"
         % (str(BASE), str(boot))],
        capture_output=True, text=True)
    check("a fresh process comes up on a truncated store",
          proc.returncode == 0 and proc.stdout.strip() == "2",
          f"rc={proc.returncode} out={proc.stdout.strip()!r} err={proc.stderr.strip()[-300:]}")


# --- bounded state ------------------------------------------------------------

def test_bounded_state():
    print("\nper-thread state stays bounded")
    store = tmp_store()
    for i in range(60):
        store.add_event("C:1", "timeout", f"e{i}")
    ev = store.get("C:1")["events"]
    check("event log capped at 50", len(ev) == 50 and ev[-1]["detail"] == "e59")
    for i in range(60):
        store.add_cost("C:2", 0.1)
    e = store.get("C:2")
    check("cost history capped at 50", len(e["costs"]) == 50)
    check("turns and total still accumulate", e["turns"] == 60 and round(e["cost"], 2) == 6.0)


# --- everyone else who touches a state file -----------------------------------
# The stores themselves go through jsonstore now, but two callers reach around
# them: the CLI reads sessions.json directly, and the mail watermark is read,
# edited and written back by two callers at once. An atomic save fixes neither.

def test_state_files_have_one_reader_and_one_writer():
    import threading as _th
    import jsonstore

    print("\nthe callers that reach around the stores")
    d = Path(tempfile.mkdtemp())

    # The CLI's `import` compares against sessions.json, which the bot rewrites
    # underneath it. It caught OSError only, so a half-written file -- the very
    # thing this module exists for -- crashed the CLI on a JSONDecodeError.
    cli = ast.parse((BASE / "bin" / "silkworm").read_text())
    do_import = next((n for n in ast.walk(cli) if isinstance(n, ast.FunctionDef)
                      and n.name == "do_import"), None)
    check("the CLI still has an import command to check", do_import is not None)
    calls = [n for n in ast.walk(do_import) if isinstance(n, ast.Call)] if do_import else []
    def called(node, dotted):
        obj, _, attr = dotted.partition(".")
        return (isinstance(node.func, ast.Attribute) and node.func.attr == attr
                and isinstance(node.func.value, ast.Name) and node.func.value.id == obj)
    check("it does not parse the session store by hand",
          not any(called(c, "json.loads") for c in calls))
    loads = [c for c in calls if called(c, "jsonstore.load")]
    check("it reads it the way the store does", len(loads) == 1)
    check("and as a visitor, not the owner: no repair from a second process",
          any(kw.arg == "repair" and kw.value.value is False
              for c in loads for kw in c.keywords),
          "renaming the bot's live file out from under it is the loss, not the fix")
    # ...and it names things that exist. A lifted-function test would supply
    # them; the real script has to import them itself.
    imported = {a.asname or a.name.split(".")[0]
                for n in ast.walk(cli) if isinstance(n, ast.Import) for a in n.names}
    check("and imports the module it calls", "jsonstore" in imported, f"{sorted(imported)}")

    # The mail watermark is the other one. Two callers -- the poll loop and the
    # dashboard's ingest-email button -- each read it, mark their own messages
    # seen and write it back, so the later save drops the other's progress and
    # that mail is proposed a second time. os.replace makes each *save* whole;
    # it cannot make a read-modify-write exclusive. Its harvest twin has always
    # held a lock. Checked by running the real function, twice, at once.
    tree = ast.parse((BASE / "bot.py").read_text())
    check("bot.py defines the lock itself, rather than the test handing it one",
          any(isinstance(n, ast.Assign) and any(
                  isinstance(t_, ast.Name) and t_.id == "_email_lock" for t_ in n.targets)
              for n in tree.body),
          "otherwise the live bot raises NameError where the test passes")

    state_file = d / "email_state.json"

    class FakeIngest:
        """Marks one message seen, slowly enough that an unlocked pass overlaps.

        The pause is inside the pass, not a rendezvous between the two: under
        the lock there is no second pass to meet, and a barrier would deadlock
        on the fix rather than fail on the bug.
        """
        def ingest_facts(self, project_store, labels, **kw):
            time.sleep(0.05)            # long enough for an unlocked pass to load too
            labels[kw["who"]] = "seen"
            return {"filed": 1}

        def ingest(self, *a, **kw):
            return {}

    ns = lift("run_email_ingest",
                       GMAIL_USER="u", GMAIL_APP_PASSWORD="p", GMAIL_HOST="h",
                       GMAIL_MAX_PER_RUN=1, GMAIL_TRIAGE=False, GMAIL_MAILBOX="INBOX",
                       CLAUDE_BIN="claude", NAMING_MODEL="haiku",
                       CLAUDE_CWD=d, claude_env=lambda: {},
                       EMAIL_STATE_FILE=state_file, jsonstore=jsonstore,
                       email_ingest=FakeIngest(), project_store=None, task_store=None,
                       _email_lock=_th.Lock())
    run = ns["run_email_ingest"]

    # Each pass needs its own message id; the thread name is the simplest carrier.
    class PerThread(FakeIngest):
        def ingest_facts(self, project_store, labels, **kw):
            kw["who"] = _th.current_thread().name
            return FakeIngest.ingest_facts(self, project_store, labels, **kw)

    ns["email_ingest"] = PerThread()
    results = []
    threads = [_th.Thread(target=lambda: results.append(run()), name=n)
               for n in ("first", "second")]
    [t_.start() for t_ in threads]
    [t_.join(timeout=10) for t_ in threads]

    seen = (jsonstore.load(state_file, default={}, strict=False) or {}).get("labels", {})
    check("two passes at once do not lose each other's progress",
          set(seen) == {"first", "second"}, f"kept {sorted(seen)}")
    check("and both of them ran", len(results) == 2, f"{results}")

    # Silkworm's own .gitignore has to cover the sidecars, and the names come
    # from jsonstore rather than from typing them out again here -- a change to
    # how a temp file is named is exactly what would leave a 3 MB copy of the
    # board staged. Asked of git, not of fnmatch.
    made = [jsonstore.backup_path(Path("tasks.json")),
            jsonstore.corrupt_path(Path("tasks.json")),
            jsonstore._scratch(Path("tasks.json")),
            jsonstore._scratch(jsonstore.backup_path(Path("tasks.json")))]
    tracked = [f.name for f in made
               if subprocess.run(["git", "check-ignore", "-q", f.name],
                                 cwd=BASE).returncode != 0]
    check("every sidecar jsonstore can create is gitignored", not tracked, f"{tracked}")


# --- artifacts/ has a retention rule, and a pending thread is exempt from it ---
# upload_outbox() archived every file a turn sent into artifacts/ and nothing
# ever removed one: 301MB by 2026-09-29, mostly video, 135MB of it named by no
# record at all. The rule lives in artifacts.py; these pin each clause of it.

def _artifact(root, key, name, age_days, sessions, *, uploaded=True, recorded=True):
    d = root / key.replace(":", "__")
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(b"x" * 1000)
    then = time.time() - age_days * 86400
    os.utime(f, (then, then))
    if recorded:
        sessions.setdefault(key, {}).setdefault("files", []).append(
            {"name": name, "path": str(f), "direction": "out", "ts": then,
             "uploaded": uploaded})
    return f


def test_artifact_retention():
    print("\nartifacts/ retention: old mirrors and orphans go, records and pending threads stay")
    root = Path(tempfile.mkdtemp()) / "artifacts"
    sessions: dict = {}
    old = _artifact(root, "C1:1.0", "old.mp4", 45, sessions)
    fresh = _artifact(root, "C1:1.0", "fresh.png", 3, sessions)
    only_copy = _artifact(root, "C1:1.0", "failed-upload.mp4", 45, sessions, uploaded=False)
    orphan = _artifact(root, "C1:1.0", "fell-off-the-cap.mp4", 45, sessions, recorded=False)
    # A !reset thread: the directory is there, the record is not.
    reset_orphan = _artifact(root, "C1:2.0", "reset.zip", 5, sessions, recorded=False)
    # Unreferenced ctime is when it landed here, which a rename sets to now: the
    # grace protects a file moved in just before upload_outbox records it.
    young_orphan = _artifact(root, "C1:1.0", "being-archived.mp4", 0, sessions, recorded=False)
    # A failed upload whose record has since gone: the name still says so.
    unsent_orphan = _artifact(root, "C1:2.0", "1784000000" + artifacts.UNSENT + "clip.mp4",
                              45, sessions, recorded=False)
    held_old = _artifact(root, "C1:3.0", "held.mp4", 45, sessions)
    held_orphan = _artifact(root, "C1:3.0", "held-orphan.mp4", 45, sessions, recorded=False)
    sessions["C1:3.0"]["pending"] = {"msg_ts": "3.1", "started": "x"}
    # Not a thread's directory: a plan's progress tracker, kept here by a turn.
    foreign = root / "plan-2026-10-02" / "progress.txt"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("step 3: tsk_x")
    os.utime(foreign, (time.time() - 45 * 86400,) * 2)

    later = time.time() + 2 * 86400     # every orphan past its day of grace
    doomed = {Path(i["path"]).name: i["why"] for i in artifacts.plan(root, sessions, 30, now=later)}
    check("an uploaded file past the age limit is planned for removal",
          doomed.get("old.mp4") == "expired", str(doomed))
    check("a file inside the age limit is not", "fresh.png" not in doomed)
    check("a file that never reached Slack is kept however old -- it is the only copy",
          "failed-upload.mp4" not in doomed)
    check("files no record names are removed (the cap, and !reset)",
          doomed.get("fell-off-the-cap.mp4") == "unreferenced"
          and doomed.get("reset.zip") == "unreferenced", str(doomed))
    check("an unreferenced file marked unsent is kept -- the record is gone, the only copy is not",
          not any(artifacts.UNSENT in n for n in doomed), str(doomed))
    check("a pending thread's artifacts survive the rule, orphans included",
          "held.mp4" not in doomed and "held-orphan.mp4" not in doomed, str(doomed))
    check("an unreferenced file inside its day of grace is left alone",
          not [i for i in artifacts.plan(root, sessions, 30) if i["why"] == "unreferenced"],
          "just after landing every orphan here is inside its grace, whatever its mtime")
    check("days=0 turns the age rule off but not the orphan rule",
          {Path(i["path"]).name for i in artifacts.plan(root, sessions, 0, now=later)}
          == {"fell-off-the-cap.mp4", "reset.zip", "being-archived.mp4"})
    check("a directory upload_outbox did not make is not the rule's to sweep",
          "progress.txt" not in doomed, str(doomed))
    check("planning touched nothing", all(p.exists() for p in
          (foreign, old, fresh, only_copy, orphan, reset_orphan, young_orphan, unsent_orphan,
           held_old, held_orphan)))

    # upload_outbox is where the mark is made; run it with an upload that fails.
    ob = Path(tempfile.mkdtemp()) / "ob"
    ob.mkdir()
    (ob / "clip.mp4").write_bytes(b"v")
    (ob / "shot.png").write_bytes(b"p")
    arch = Path(tempfile.mkdtemp())

    class Client:
        def files_upload_v2(self, **kw):
            if kw["title"] == "clip.mp4":
                raise RuntimeError("upload refused")
    ub = lift("upload_outbox", ARTIFACTS_ROOT=arch, artifacts=artifacts, Path=Path,
                       shutil=shutil, store=tmp_store())
    ub["log"].disabled = True
    ub["upload_outbox"](Client(), ob, "C1", "1.0", "C1:1.0")
    names = sorted(p.name for p in (arch / "C1__1.0").iterdir())
    ub["log"].disabled = False
    check("upload_outbox marks a failed upload unsent in its archived name, and only that one",
          len(names) == 2
          and [artifacts.UNSENT in n for n in names if n.endswith("clip.mp4")] == [True]
          and [artifacts.UNSENT in n for n in names if n.endswith("shot.png")] == [False],
          str(names))

    # Through the sweeper, against a real SessionStore: off logs, on removes.
    st = tmp_store()
    for key, entry in sessions.items():
        st.update(key, **entry)
    before = st.get("C1:1.0")["updated"]
    ns = lift("_sweep_pass", store=st, task_store=types.SimpleNamespace(
                           all=lambda: {}, compact_older_than=lambda d: None),
                       OUTBOX_ROOT=Path(tempfile.mkdtemp()), shutil=shutil,
                       SESSION_MAX_AGE_DAYS=30, TASK_COMPACT_AFTER_DAYS=14,
                       ARTIFACTS_ROOT=root, artifacts=artifacts,
                       ARTIFACT_MAX_AGE_DAYS=30, ARTIFACT_PRUNE=False)
    real_time = artifacts.time
    artifacts.time = types.SimpleNamespace(time=lambda: later)
    try:
        ns["_sweep_pass"]()
        check("with ARTIFACT_PRUNE off the sweeper removes nothing",
              old.exists() and orphan.exists() and reset_orphan.exists())
        ns["ARTIFACT_PRUNE"] = True
        ns["_sweep_pass"]()
    finally:
        artifacts.time = real_time
    check("with it on, the expired file and the old orphans are gone",
          not old.exists() and not orphan.exists() and not reset_orphan.exists())
    # (young_orphan went too: two days on, its grace is over.)
    check("and the fresh, the only copy and the pending thread's are still there",
          all(p.exists() for p in (fresh, only_copy, unsent_orphan, held_old, held_orphan,
                                   foreign)))
    recs = {r["name"]: r for r in st.get("C1:1.0")["files"]}
    check("the removed file's record stays, stamped pruned",
          "old.mp4" in recs and recs["old.mp4"].get("pruned"), str(recs.get("old.mp4")))
    check("and only that record is stamped",
          not any(r.get("pruned") for n, r in recs.items() if n != "old.mp4"))
    check("pruning is not activity -- the thread's updated stamp is unchanged",
          st.get("C1:1.0")["updated"] == before)
    check("the pending thread's record is untouched",
          not any(r.get("pruned") for r in st.get("C1:3.0")["files"]))

    # A turn that starts between plan() and apply() still holds its thread.
    root2 = Path(tempfile.mkdtemp()) / "artifacts"
    s2: dict = {}
    late = _artifact(root2, "C2:1.0", "late.mp4", 45, s2)
    plan2 = artifacts.plan(root2, s2, 30)
    st2 = tmp_store()
    st2.update("C2:1.0", **s2["C2:1.0"])
    st2.update("C2:1.0", pending={"msg_ts": "1.1", "started": "x"})
    artifacts.apply(plan2, st2)
    check("a thread whose turn began after planning is still exempt when removing",
          plan2 and late.exists(), str(plan2))

    # A record naming its file by another spelling of the same path (here a
    # symlinked root) is still the one stamped.
    real = Path(tempfile.mkdtemp())
    link = Path(tempfile.mkdtemp()) / "via"
    link.symlink_to(real)
    s3: dict = {}
    _artifact(link, "C3:1.0", "aliased.mp4", 45, s3)
    plan3 = artifacts.plan(real, s3, 30)
    st3 = tmp_store()
    st3.update("C3:1.0", **s3["C3:1.0"])
    artifacts.apply(plan3, st3)
    check("the record is stamped even when it spells the path differently from the walk",
          [i["why"] for i in plan3] == ["expired"]
          and st3.get("C3:1.0")["files"][0].get("pruned"), str(plan3))


# --- projects by directory ---------------------------------------------------

def _run_git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _git_repo(path, remote=""):
    path.mkdir(parents=True, exist_ok=True)
    _run_git(path, "init", "-q")
    if remote:
        _run_git(path, "remote", "add", "origin", remote)
    return path


class _FakeGh:
    """Stands in for `gh`: records every call, and on `repo create` does what
    the real one does to the checkout -- adds origin -- without any network."""

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, cmd, cwd, timeout=60):
        self.calls.append(list(cmd))
        if self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", "HTTP 422: name already exists")
        name = cmd[3].split("/")[-1]
        owner = cmd[3].split("/")[0] if "/" in cmd[3] else "someone"
        _run_git(cwd, "remote", "add", "origin", f"git@github.com:{owner}/{name}.git")
        return subprocess.CompletedProcess(
            cmd, 0, f"✓ Created repository {owner}/{name} on GitHub\n"
                    f"  https://github.com/{owner}/{name}\n", "")


def test_project_for_most_specific_directory_wins():
    print("\nprojects: a directory belongs to its most specific project")
    import workspaces
    root = Path(os.path.realpath(tempfile.mkdtemp()))
    for d in ("ws/x/sub", "ws/xy", "ws/Silkworm/workspace/scratchy", "ws/old"):
        (root / d).mkdir(parents=True)
    recs = [{"slug": "ws", "scope": {"cwd": str(root / "ws")}},
            {"slug": "x", "scope": {"cwd": str(root / "ws/x")}},
            {"slug": "silkworm", "scope": {"cwd": str(root / "ws/Silkworm")}},
            {"slug": "scr", "scope": {"cwd": str(root / "ws/Silkworm/workspace/scratchy")}},
            {"slug": "old", "archived": True, "scope": {"cwd": str(root / "ws/old")}},
            {"slug": "nodir", "scope": {}}]
    pf = workspaces.project_for
    scratch = root / "ws/Silkworm/workspace"
    check("a project at ws/x beats one at ws, either way round",
          pf(root / "ws/x/sub", recs) == "x" and pf(root / "ws/x/sub", recs[::-1]) == "x",
          f"{pf(root / 'ws/x/sub', recs)} / {pf(root / 'ws/x/sub', recs[::-1])}")
    check("the directory itself counts", pf(root / "ws/x", recs) == "x")
    check("a sibling that only shares a prefix is not inside (xy is not x)",
          pf(root / "ws/xy", recs) == "ws", pf(root / "ws/xy", recs))
    check("a directory in no project's stays unfiled",
          pf(root, recs) == "" and pf("", recs) == "" and pf(None, recs) == "")
    check("an archived project takes no new work; its parent does",
          pf(root / "ws/old", recs) == "ws", pf(root / "ws/old", recs))
    check("the scratch folder is not Silkworm's",
          pf(scratch, recs, scratch=scratch) == "", pf(scratch, recs, scratch=scratch))
    check("but a project living inside the scratch folder is still found",
          pf(scratch / "scratchy", recs, scratch=scratch) == "scr")
    at_scratch = recs + [{"slug": "groceries", "scope": {"cwd": str(scratch)}}]
    check("a project registered AT the scratch folder does not absorb it",
          pf(scratch, at_scratch, scratch=scratch) == ""
          and pf(scratch / "scratchy", at_scratch, scratch=scratch) == "scr",
          pf(scratch, at_scratch, scratch=scratch))
    check("Silkworm's own checkout outside the scratch folder is Silkworm's",
          pf(root / "ws/Silkworm", recs, scratch=scratch) == "silkworm")
    # A symlinked path is the directory it points at.
    (root / "link").symlink_to(root / "ws/x/sub")
    check("a symlink resolves to where it points", pf(root / "link", recs) == "x")


def test_project_new_parses_its_command():
    print("\nprojects: `!project new` reads name, path, flags and purpose")
    import workspaces
    p = workspaces.parse_new('Widget ~/code/widget --github -- a thing that does widgets')
    check("name, path, github and purpose",
          p == {"name": "Widget", "path": "~/code/widget", "github": True, "adopt": False,
                "purpose": "a thing that does widgets"}, str(p))
    p = workspaces.parse_new('"Silk Swing" --adopt — the rope game')
    check("a quoted name, an em dash, adopt; github is off unless named",
          p["name"] == "Silk Swing" and p["adopt"] and not p["github"]
          and p["purpose"] == "the rope game" and p["path"] == "", str(p))
    p = workspaces.parse_new("“Silk Swing” —github — the rope game")
    check("a phone's curly quotes and em-dash flag read as meant",
          p["name"] == "Silk Swing" and p["github"] and p["path"] == ""
          and p["purpose"] == "the rope game", str(p))
    check("a purpose may itself contain double dashes",
          workspaces.parse_new("W -- uses --flags inside")["purpose"] == "uses --flags inside")
    for bad in ("", "--github", "a b c", "W --gihtub"):
        try:
            workspaces.parse_new(bad)
            ok = False
        except ValueError:
            ok = True
        check(f"refused: {bad!r}", ok)


def test_project_new_creates_in_one_step():
    print("\nprojects: one step makes the directory, repo, CLAUDE.md and record")
    import projects
    import workspaces
    root = Path(os.path.realpath(tempfile.mkdtemp()))
    ws = root / "workspace"
    ws.mkdir()
    ps = projects.ProjectStore(root / "projects.json")
    gh = _FakeGh()

    done = workspaces.create("Widget", ps, purpose="Makes widgets.", root=ws, gh=gh)
    d = ws / "widget"
    log_ = _run_git(d, "log", "--format=%s").stdout.split("\n")
    check("made ~/workspace/<slug> and a git repo in it",
          d.is_dir() and (d / ".git").exists() and done["made_dir"] and done["initialised"])
    check("CLAUDE.md is the title and the one-line purpose",
          (d / "CLAUDE.md").read_text() == "# Widget\n\nMakes widgets.\n",
          (d / "CLAUDE.md").read_text())
    check("with a first commit holding it",
          done["committed"] and log_[0] == "Start Widget"
          and _run_git(d, "status", "--porcelain").stdout == "", str(log_))
    rec = ps.get("widget")
    check("registered with its directory, and no repo without a GitHub remote",
          rec and rec["title"] == "Widget" and rec["scope"] == {"cwd": str(d)}, str(rec))
    check("and GitHub never heard about it", gh.calls == [], str(gh.calls))
    check("what it did is said", "registered" in workspaces.describe(done)
          and str(d) in workspaces.describe(done))

    # --- refusals: before anything is touched --------------------------------
    def refused(**kw):
        name = kw.pop("name")
        try:
            workspaces.create(name, ps, root=ws, gh=gh, **kw)
        except workspaces.Refused as e:
            return str(e)
        return ""
    check("a name that is already a project is refused", "already a project" in refused(name="widget"))
    (ws / "taken").mkdir()
    (ws / "taken" / "notes.txt").write_text("mine")
    why = refused(name="Taken")
    check("an existing directory is not adopted on a bare name",
          "--adopt" in why and not (ws / "taken" / ".git").exists()
          and not ps.get("taken"), why)
    check("nor is a directory another project already lives in",
          "already project" in refused(name="Other", path=str(d)))
    outer = _git_repo(root / "outer")
    why = refused(name="Nested", path=str(outer / "inner"))
    check("a repository nested in another one's working tree is refused, leaving nothing",
          "inside the git repository" in why and not (outer / "inner").exists()
          and not ps.get("nested"), why)
    (outer / ".gitignore").write_text("inner/\n")
    check("unless that repository ignores the spot",
          workspaces.create("Nested", ps, path=str(outer / "inner"), gh=gh)["initialised"])

    done = workspaces.create("Rel", ps, path="sub/rel", root=ws, gh=gh)
    check("a relative path is relative to the workspace",
          done["cwd"] == str(ws / "sub" / "rel") and (ws / "sub" / "rel" / ".git").exists(),
          done["cwd"])

    def failing_commit(cmd, cwd, timeout=60):
        if cmd[:2] == ["git", "commit"]:
            return subprocess.CompletedProcess(cmd, 1, "", "Please tell me who you are")
        return workspaces._run(cmd, cwd, timeout=timeout)
    why = ""
    try:
        workspaces.create("Broken", ps, root=ws, run=failing_commit, gh=gh)
    except workspaces.Refused as e:
        why = str(e)
    check("a git step failing part way leaves nothing behind, and says why",
          "who you are" in why and not (ws / "broken").exists() and not ps.get("broken"), why)
    (ws / "keepme").mkdir()
    (ws / "keepme" / "data.txt").write_text("x")
    try:
        workspaces.create("Keepme", ps, root=ws, adopt=True, run=failing_commit, gh=gh)
    except workspaces.Refused:
        pass
    check("and undoing an adoption removes only what it added",
          sorted(p.name for p in (ws / "keepme").iterdir()) == ["data.txt"],
          str(sorted(p.name for p in (ws / "keepme").iterdir())))

    # --- adopting --------------------------------------------------------------
    done = workspaces.create("Taken", ps, root=ws, adopt=True, gh=gh)
    tracked = _run_git(ws / "taken", "ls-files").stdout.split()
    check("adopting an existing directory commits what was there",
          not done["made_dir"] and done["committed"]
          and sorted(tracked) == ["CLAUDE.md", "notes.txt"], str(tracked))
    old = _git_repo(ws / "oldrepo", remote="https://github.com/me/OldRepo.git")
    (old / "a.txt").write_text("a")
    _run_git(old, "add", "-A")
    _run_git(old, "commit", "-qm", "first")
    done = workspaces.create("OldRepo", ps, path=str(old), gh=gh)
    check("naming the path of an existing repo adopts it: no init, no commit of ours",
          not done["initialised"] and not done["committed"]
          and _run_git(old, "log", "--format=%s").stdout.strip() == "first")
    check("its GitHub remote is the record's repo",
          ps.get("oldrepo")["scope"] == {"cwd": str(old), "repo": "github.com/me/OldRepo"},
          str(ps.get("oldrepo")))
    check("and a CLAUDE.md it lacked is started but left for you to commit",
          (old / "CLAUDE.md").exists() and any("not committed" in n for n in done["notes"]))
    check("still nothing sent to GitHub", gh.calls == [], str(gh.calls))

    # --- GitHub, only when asked -------------------------------------------------
    done = workspaces.create("Gadget Two", ps, root=ws, github=True, owner="me", gh=gh)
    check("asked for, it is a PRIVATE repo pushed from the new directory",
          len(gh.calls) == 1 and gh.calls[0][:4] == ["gh", "repo", "create", "me/Gadget-Two"]
          and "--private" in gh.calls[0] and "--push" in gh.calls[0]
          and f"--source={ws / 'gadget-two'}" in gh.calls[0], str(gh.calls))
    check("and the URL is reported and the repo recorded",
          done["github_url"] == "https://github.com/me/Gadget-Two"
          and ps.get("gadget-two")["scope"]["repo"] == "github.com/me/Gadget-Two",
          str(done))
    gh.calls.clear()
    done = workspaces.create("Has Remote", ps, path=str(_git_repo(
        ws / "hr", remote="git@github.com:me/HR.git")), github=True, gh=gh)
    check("a repo that already has a remote gets no second one",
          gh.calls == [] and any("already has a remote" in n for n in done["notes"]))
    bad = _FakeGh(fail=True)
    done = workspaces.create("Fails", ps, root=ws, github=True, gh=bad)
    check("a failed gh is reported, and the project is still registered locally",
          ps.get("fails") and not done["github_url"]
          and any("422" in n for n in done["notes"]), str(done["notes"]))


def test_project_new_from_slack_and_dashboard():
    print("\nprojects: `!project new` files and moves the thread; the form never publishes by itself")
    import functools
    import projects
    import workspaces
    root = Path(os.path.realpath(tempfile.mkdtemp()))
    ps = projects.ProjectStore(root / "projects.json")
    st = SessionStore(root / "s.json")
    gh = _FakeGh()
    fake_ws = types.SimpleNamespace(**{k: getattr(workspaces, k) for k in dir(workspaces)
                                       if not k.startswith("__")})
    fake_ws.create = functools.partial(workspaces.create, gh=gh, root=root / "workspace")
    fake_ws.github_repo = functools.partial(workspaces.github_repo, gh=gh)
    ns = lift("new_project_from_thread", "github_for_thread_project",
                       "handle_projects",
                       store=st, project_store=ps, workspaces=fake_ws, GITHUB_OWNER="me",
                       repos=__import__("repos"), Path=Path, projects=projects,
                       install_git_guards=lambda *a: None, RUNNING={"C:busy": object()},
              absent=PROJECT_ROUTES)
    st.update("C:busy", cwd=str(root))
    st.update("C:pend", cwd=str(root), pending={"ts": "1"})
    busy = [ns["new_project_from_thread"](k, f"Busy{i}") for i, k in
            enumerate(("C:busy", "C:pend"))]
    check("a thread with a turn in flight is asked to wait, and nothing is made",
          all("turn running" in r for r in busy) and not ps.get("busy0")
          and not ps.get("busy1") and not (root / "workspace").exists(), str(busy))
    st.update("C:1", session_id="sess-old", cwd=str(root), project="", unfiled=True)
    reply = ns["new_project_from_thread"]("C:1", "Thing -- a thing")
    e = st.get("C:1")
    d = root / "workspace" / "thing"
    check("the thread is filed under it and works in its directory",
          e["project"] == "thing" and e["cwd"] == str(d) and e["unfiled"] is False, str(e))
    check("its old session is retired, kept, not resumed from the wrong place",
          e["session_id"] is None and e["previous_sessions"] == ["sess-old"], str(e))
    check("the reply says what happened, and offers GitHub instead of doing it",
          "registered" in reply and "`!project github create`" in reply and gh.calls == [], reply)
    reply = ns["github_for_thread_project"]("C:1")
    check("`!project github` makes the private repo and records it",
          "https://github.com/me/Thing" in reply
          and ps.get("thing")["scope"].get("repo") == "github.com/me/Thing", reply)
    check("a refused name says why and changes nothing",
          ":warning:" in ns["new_project_from_thread"]("C:2", "Thing")
          and st.get("C:2") is None)

    gh.calls.clear()
    r = ns["handle_projects"]({"action": "new", "name": "Formed", "purpose": "p",
                               "github": "yes"})
    check("the form's GitHub box must actually be ticked (true), not merely truthy",
          r["ok"] and gh.calls == [] and not r["created"]["github_url"], str(r))
    r = ns["handle_projects"]({"action": "new", "name": "Boxed", "github": True})
    check("ticked, it creates the private repo and says where",
          r["ok"] and len(gh.calls) == 1 and "--private" in gh.calls[0]
          and r["created"]["github_url"].endswith("/me/Boxed"), str(r))
    r = ns["handle_projects"]({"action": "new", "name": "Formed"})
    check("and refuses a collision with a reason", not r["ok"] and "already" in r["error"])


def test_threads_and_tasks_filed_by_directory():
    print("\nprojects: threads and tasks are filed by directory, once, idempotently")
    import projects
    import tasks as T
    import workspaces
    from tasks import TaskStore
    root = Path(os.path.realpath(tempfile.mkdtemp()))
    for d in ("ws/a/deep", "ws/b", "ws/Silkworm/workspace", "elsewhere"):
        (root / d).mkdir(parents=True)
    ps = projects.ProjectStore(root / "projects.json")
    ps.ensure("ws", scope={"cwd": str(root / "ws")})
    ps.ensure("a", scope={"cwd": str(root / "ws/a")})
    ps.ensure("b", scope={"cwd": str(root / "ws/b")})
    ps.ensure("silkworm", scope={"cwd": str(root / "ws/Silkworm")})
    st = SessionStore(root / "s.json")
    ts = TaskStore(root / "t.json")
    st.update("deep", cwd=str(root / "ws/a/deep"))
    st.update("bee", cwd=str(root / "ws/b"))
    st.update("hand", cwd=str(root / "ws/b"), project="a")          # filed by hand
    st.update("out", cwd=str(root / "ws/b"), unfiled=True)          # unfiled on purpose
    st.update("scratch", cwd=str(root / "ws/Silkworm/workspace"))
    st.update("nowhere", cwd=str(root / "elsewhere"))
    st.update("nocwd", title="x")
    stamp = {k: v["updated"] for k, v in st.all().items()}
    t_deep = ts.create("x", role="assistant", thread="deep",
                       scope={"cwd": str(root / "ws/a/deep")})
    t_hand = ts.create("x", state="done", thread="hand", scope={"cwd": str(root / "ws/b")})
    t_out = ts.create("x", state="done", thread="out", scope={"cwd": str(root / "ws/b")})
    t_free = ts.create("x", state="done", scope={"cwd": str(root / "ws/b")})
    t_rev = ts.create("x", state="done", role="reviewer", scope={"cwd": str(root / "ws/b")})
    t_kept = ts.create("x", state="done", project="b", thread="deep",
                       scope={"cwd": str(root / "ws/a")})
    t_none = ts.create("x", state="done", scope={"cwd": str(root / "elsewhere")})
    # Open work waiting on a person: filing it under a ready project would
    # let it run, or land, unsupervised.
    t_open = ts.create("x", role="implementor", state="queued", scope={"cwd": str(root / "ws/b")})
    t_prop = ts.create("x", role="implementor", state="proposed",
                       scope={"cwd": str(root / "ws/b")})
    tstamp = {k: v["updated"] for k, v in ts.all().items()}
    ns = lift("file_existing_by_directory", "file_by_directory",
                       store=st, task_store=ts, project_store=ps, workspaces=workspaces,
                       CLAUDE_CWD=root / "ws/Silkworm/workspace")
    first = ns["file_existing_by_directory"]()
    got = {k: v.get("project", "") for k, v in st.all().items()}
    check("threads: most specific project, hand-filed kept, unfiled and scratch left alone",
          got == {"deep": "a", "bee": "b", "hand": "a", "out": "", "scratch": "",
                  "nowhere": "", "nocwd": ""}, str(got))
    tp = {t["id"]: t.get("project") for t in ts.all().values()}
    check("tasks: by their thread's project, else their own directory",
          tp[t_deep["id"]] == "a" and tp[t_hand["id"]] == "a" and tp[t_free["id"]] == "b",
          str(tp))
    check("tasks: a reviewer, an unfiled thread's task, a filed one and a homeless one untouched",
          tp[t_rev["id"]] == "" and tp[t_out["id"]] == "" and tp[t_kept["id"]] == "b"
          and tp[t_none["id"]] == "", str(tp))
    check("open implementor work is not given a project after the fact",
          tp[t_open["id"]] == "" and tp[t_prop["id"]] == "", str(tp))
    check("filing is not activity: nothing's `updated` moved",
          {k: v["updated"] for k, v in st.all().items()} == stamp
          and {k: v["updated"] for k, v in ts.all().items()} == tstamp)
    check("it went through the stores, so it is on disk",
          SessionStore(root / "s.json").get("bee")["project"] == "b"
          and TaskStore(root / "t.json").get(t_free["id"])["project"] == "b")
    before = ((root / "s.json").read_bytes(), (root / "t.json").read_bytes())
    second = ns["file_existing_by_directory"]()
    check("a second run files nothing and writes nothing",
          second == {"threads": [], "tasks": []}
          and ((root / "s.json").read_bytes(), (root / "t.json").read_bytes()) == before,
          str(second))
    check("the first run said what it filed",
          sorted(first["threads"]) == ["bee", "deep"] and len(first["tasks"]) == 3, str(first))

    # Each layer holds the rule on its own, so neither leans on the other.
    recs = ps.all()
    plan = workspaces.threads_to_file(st.all(), recs)
    check("the planner never proposes refiling a filed or unfiled-on-purpose thread",
          "hand" not in plan and "out" not in plan and "deep" not in plan, str(plan))
    check("the session store refuses to refile one even when asked",
          st.file_under({"hand": "b", "out": "b", "bee": "a"}) == []
          and st.get("hand")["project"] == "a" and not st.get("out").get("project")
          and st.get("bee")["project"] == "b")
    check("the task store refuses to move filed work to another project",
          ts.file_under({t_kept["id"]: "a", t_free["id"]: "a"}) == []
          and ts.get(t_kept["id"])["project"] == "b"
          and ts.get(t_free["id"])["project"] == "b")
    tplan = workspaces.tasks_to_file(ts.all(), st.all(), recs)
    check("and the task planner proposes nothing already filed", tplan == {}, str(tplan))

    # --- a new turn ---------------------------------------------------------------
    fbd = ns["file_by_directory"]
    check("a new thread in a project's directory is filed at its first turn",
          fbd("new", root / "ws/a/deep") == "a" and st.get("new")["project"] == "a")
    check("an unfiled-on-purpose thread stays out", fbd("out", root / "ws/b") == ""
          and not st.get("out").get("project"))
    check("a filed thread keeps its project wherever it runs",
          fbd("hand", root / "ws/b") == "a")
    check("a scratch-folder thread stays unfiled",
          fbd("scr2", root / "ws/Silkworm/workspace") == "" and st.get("scr2") is None)
    # The turn's task is created with whatever this returns.
    src = (BASE / "bot.py").read_text()
    check("handle_prompt files its task through it",
          "project=file_by_directory(key, cwd)," in src)
    check("startup runs the backfill", 'daemons.start(_project_filer, "filer", forever=False)' in src)


def test_unregistered_repos_are_reported():
    print("\nprojects: repos in the workspace that no project covers are named")
    import digest
    import workspaces
    root = Path(os.path.realpath(tempfile.mkdtemp()))
    ws, scratch = root / "workspace", root / "workspace/Silkworm/workspace"
    _git_repo(ws / "known")
    _git_repo(ws / "fresh")
    _git_repo(ws / "clone", remote="git@github.com:me/Known.git")
    _git_repo(ws / ".archive" / "gone")
    _git_repo(ws / ".worktrees")
    _git_repo(ws / ".hidden")
    (ws / "plain").mkdir()
    _git_repo(ws / "mono")
    (ws / "mono" / "app").mkdir()
    _git_repo(ws / "Silkworm")
    _git_repo(scratch / "lostgame")
    _git_repo(scratch / "nested" / "deeper")          # not *directly* under a root
    recs = [{"slug": "known", "scope": {"cwd": str(ws / "known"), "repo": "github.com/me/Known"}},
            {"slug": "app", "archived": True, "scope": {"cwd": str(ws / "mono" / "app")}},
            {"slug": "silkworm", "scope": {"cwd": str(ws / "Silkworm")}}]
    found = workspaces.unregistered([ws, scratch, root / "missing"], recs)
    check("only repos no project covers, from both roots, sorted",
          found == sorted([str(ws / "fresh"), str(scratch / "lostgame")]), str(found))
    check("Silkworm's project does not hide repos in its scratch folder",
          str(scratch / "lostgame") in found)
    line = digest.render([], 1e9, unregistered=found)
    check("the daily digest lists them, even on a quiet day",
          "unregistered repos 2" in line and "fresh" in line and "lostgame" in line, line)
    check("and says nothing when there are none",
          "unregistered" not in digest.render([], 1e9, unregistered=[]))
    pj = root / "projects.json"
    pj.write_text(json.dumps({r["slug"]: r for r in recs}))
    cli = _load_cli()
    with contextlib.redirect_stdout(io.StringIO()) as out:
        rows = cli.check_unregistered(pj, roots=[ws, scratch])
    check("`silkworm status` lists them", rows == found and "fresh" in out.getvalue()
          and "✘" in out.getvalue(), out.getvalue())
    status_fn = next(n for n in ast.parse((BASE / "bin" / "silkworm").read_text()).body
                     if isinstance(n, ast.FunctionDef) and n.name == "do_status")
    check("and do_status runs that check",
          any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "check_unregistered"
              for n in ast.walk(status_fn)))
    src = (BASE / "bot.py").read_text()
    check("the digest is handed them", "unregistered=found)" in src)

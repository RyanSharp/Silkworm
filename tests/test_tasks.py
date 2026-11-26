"""Tasks and the queue: lifecycle, lanes, filing, ideation, scoping and research.

Run by tests/run.py with every other tests/test_*.py; see tests/harness.py."""

from harness import *  # noqa: F401,F403


# --- task lifecycle -----------------------------------------------------------

def test_task_lifecycle():
    import tasks as T
    from tasks import TaskStore, InvalidTransition
    print("\ntask lifecycle")
    st = TaskStore(Path(tempfile.mkdtemp()) / "t.json")

    def refuses(tid, to):
        try:
            st.transition(tid, to)
            return False
        except InvalidTransition:
            return True

    t = st.create("do a thing", source="slack")
    check("new task starts queued", t["state"] == T.QUEUED)
    check("record has every declared field", set(t) == set(T.FIELDS))
    check("ingested task can start proposed",
          st.create("x", state=T.PROPOSED)["state"] == T.PROPOSED)

    st.transition(t["id"], T.RUNNING)
    st.transition(t["id"], T.AWAITING_APPROVAL, "merge?")
    st.transition(t["id"], T.RUNNING, "approved")
    st.transition(t["id"], T.DONE)
    check("approval round trip reaches done", st.get(t["id"])["state"] == T.DONE)
    check("terminal state is terminal", refuses(t["id"], T.RUNNING))
    check("a task cannot finish without running", refuses(st.create("y")["id"], T.DONE))
    check("triage cannot be skipped",
          refuses(st.create("z", state=T.PROPOSED)["id"], T.RUNNING))

    f = st.create("flaky")
    st.transition(f["id"], T.RUNNING)
    st.transition(f["id"], T.FAILED)
    st.transition(f["id"], T.QUEUED, "retry")
    st.transition(f["id"], T.RUNNING)
    check("failed tasks can be retried", st.get(f["id"])["state"] == T.RUNNING)
    check("attempts counted per run", st.get(f["id"])["attempts"] == 2)
    st.transition(f["id"], T.RUNNING)
    check("repeat transition is a no-op", st.get(f["id"])["attempts"] == 2)

    need = st.needs_attention()
    check("only actionable states demand attention",
          all(x["state"] in T.NEEDS_ATTENTION for x in need))
    check("running/queued/done never demand attention",
          not any(x["state"] in (T.QUEUED, T.RUNNING, T.DONE) for x in need))

    try:
        st.update(t["id"], state=T.RUNNING)
        check("update() cannot change state", False)
    except ValueError:
        check("update() cannot change state", True)

    check("tasks survive a reload",
          TaskStore(st._path).get(t["id"])["state"] == T.DONE)

    # A create whose disk write fails must not leave the task in memory, where
    # the runner would claim it and the next unrelated save would persist it.
    import jsonstore
    real_save = jsonstore.save
    def broken(*a, **k):
        raise OSError("disk full")
    jsonstore.save = broken
    try:
        try:
            st.create("never filed", id="tsk_unsaved")
            raised = False
        except OSError:
            raised = True
    finally:
        jsonstore.save = real_save
    check("a create that cannot be saved raises", raised)
    check("and leaves nothing behind in memory",
          st.get("tsk_unsaved") is None and "tsk_unsaved" not in st._data)
    st.update(t["id"], title="later save")
    check("nor on disk after a later save",
          TaskStore(st._path).get("tsk_unsaved") is None)
    jsonstore.save = broken
    try:
        try:
            st.create("overwrite", id=t["id"])
        except OSError:
            pass
    finally:
        jsonstore.save = real_save
    check("a failed create over an existing id restores the record it replaced",
          st.get(t["id"])["title"] == "later save" and st.get(t["id"])["state"] == T.DONE)

    # update and transition edit the record in place before saving; a failed
    # save must put it back, or the next unrelated save writes the edit anyway.
    def failing(fn):
        jsonstore.save = broken
        try:
            fn()
            return False
        except OSError:
            return True
        finally:
            jsonstore.save = real_save
    import copy
    u = st.create("rollback probe", id="tsk_rollback")
    # get() copies only the top level; events would alias the live list.
    before = copy.deepcopy(st.get(u["id"]))
    check("an update that cannot be saved raises",
          failing(lambda: st.update(u["id"], title="never written", result={"x": 1})))
    check("and leaves the record as it was in memory", st.get(u["id"]) == before)
    st.update(t["id"], title="unrelated save")
    check("nor reaches disk on a later unrelated save",
          TaskStore(st._path).get(u["id"])["title"] == "rollback probe")
    before = copy.deepcopy(st.get(u["id"]))
    check("a transition that cannot be saved raises",
          failing(lambda: st.transition(u["id"], T.RUNNING, "started")))
    after = st.get(u["id"])
    check("and leaves state, events and attempts as they were",
          after == before and after["state"] == T.QUEUED)
    st.update(t["id"], title="another unrelated save")
    check("nor reaches disk on a later unrelated save",
          TaskStore(st._path).get(u["id"])["state"] == T.QUEUED)
    # An ended blocker whose save failed has not ended: nothing waiting on it
    # may be released.
    st.transition(u["id"], T.RUNNING)
    w = st.create("waits on the probe", id="tsk_rollback_waiter",
                  state=T.BLOCKED, blocked_on=[u["id"]])
    check("a failed ending raises",
          failing(lambda: st.transition(u["id"], T.FAILED, "boom")))
    check("and releases nobody",
          st.get(u["id"])["state"] == T.RUNNING
          and st.get(w["id"])["state"] == T.BLOCKED
          and st.get(w["id"])["blocked_on"] == [u["id"]])
    st.transition(u["id"], T.DONE)

    # claim, refund_attempt and _drop_blockers edit in place too.
    cs = TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    q = cs.create("claim probe", id="tsk_claim_probe", driver="queue")
    before = copy.deepcopy(cs.get(q["id"]))
    check("a claim that cannot be saved raises", failing(lambda: cs.claim()))
    check("and leaves the task queued, attempts and events untouched",
          cs.get(q["id"]) == before and cs.get(q["id"])["state"] == T.QUEUED)
    check("so the next claim still finds it",
          (cs.claim() or {}).get("id") == q["id"])
    before = copy.deepcopy(cs.get(q["id"]))
    check("a refund that cannot be saved raises",
          failing(lambda: cs.refund_attempt(q["id"])))
    check("and leaves attempts and false_starts as they were",
          cs.get(q["id"]) == before and cs.get(q["id"])["attempts"] == 1)
    other = cs.create("unrelated", id="tsk_claim_other")
    cs.update(other["id"], title="unrelated save")
    on_disk = TaskStore(cs._path).get(q["id"])
    check("nor does a later unrelated save write the refund",
          on_disk["attempts"] == 1 and not on_disk.get("false_starts"))

    gone = cs.create("ended blocker", id="tsk_gone", state=T.DONE)
    live = cs.create("live blocker", id="tsk_live")
    wt = cs.create("waits on both", id="tsk_waits_both", state=T.BLOCKED,
                   blocked_on=[gone["id"], live["id"]])
    check("a drop that cannot be saved raises",
          failing(lambda: cs._drop_blockers(wt["id"], (gone["id"],))))
    check("and leaves blocked_on as it was",
          cs.get(wt["id"])["blocked_on"] == [gone["id"], live["id"]])
    # The release around somebody else's transition must not turn that
    # failure into theirs.
    jsonstore.save = broken
    try:
        try:
            cs._release_waiters(gone["id"], T.DONE, "", {gone["id"]})
            swallowed = True
        except OSError:
            swallowed = False
    finally:
        jsonstore.save = real_save
    check("a release whose drop fails does not raise", swallowed)
    check("and leaves the waiter as it was",
          cs.get(wt["id"])["blocked_on"] == [gone["id"], live["id"]]
          and cs.get(wt["id"])["state"] == T.BLOCKED)


# --- every turn is a task ------------------------------------------------------

def test_turn_is_a_task():
    src = (BASE / "bot.py").read_text()
    print("\nturns are wired to the task lifecycle")
    check("a prompt creates a task", "task_store.create(" in src)
    check("task moves to running when Claude is invoked",
          "task_state(task_id, tasks.RUNNING)" in src)
    check("success records a result and completes",
          "task_state(task_id, tasks.DONE)" in src and '"cost": turn_cost' in src)
    # The reported figure is the session's running total (turncost.py), so
    # the turn's own cost is what add_cost differenced it to.
    check("and the turn's own cost, not the session's total",
          '"cost": result.cost_usd' not in src)
    check("stop cancels rather than fails", "tasks.CANCELLED, \"stopped by the user\"" in src)
    # The OUTER handlers are the ones that end a turn; the inner ClaudeError
    # handler retries and must not mark anything failed.
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "handle_prompt")
    outer = [h for t in fn.body if isinstance(t, ast.Try) for h in t.handlers]
    def marks_failed(exc_name):
        for h in outer:
            if exc_name not in ast.dump(h.type or ast.Constant(None)):
                continue
            # ast.dump renders tasks.FAILED as an Attribute, never that literal
            # string, so match the node rather than the text.
            for node in ast.walk(ast.Module(body=h.body, type_ignores=[])):
                if isinstance(node, ast.Attribute) and node.attr == "FAILED":
                    return True
        return False
    check("ClaudeTimeout marks the task failed", marks_failed("ClaudeTimeout"))
    check("ClaudeError marks the task failed", marks_failed("ClaudeError"))

    # A lifecycle complaint must never cost the user their reply, so the helper
    # must contain no raise at all (string matching here trips on "raised").
    helper = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "task_state")
    check("a refused transition is swallowed, not raised",
          not [n for n in ast.walk(helper) if isinstance(n, ast.Raise)])
    check("the refusal is caught explicitly",
          any(isinstance(h.type, ast.Attribute) and h.type.attr == "InvalidTransition"
              for n in ast.walk(helper) if isinstance(n, ast.Try) for h in n.handlers))


# --- queue runner -------------------------------------------------------------

def test_task_runner_claim():
    import tasks as T
    from tasks import TaskStore
    import threading as th
    print("\nqueue runner claim semantics")
    st = TaskStore(Path(tempfile.mkdtemp()) / "t.json")

    inline = st.create("a slack turn")
    check("tasks default to inline (never auto-run merely by existing)",
          inline["driver"] == "inline")
    check("runner ignores inline tasks", st.claim() is None)
    src0 = (BASE / "bot.py").read_text()
    anchor = src0[src0.index("def task_thread("):src0.index("def execute_task(")]
    check("an anchor thread is labelled a task run, not left untitled",
          'kind="task"' in anchor and 'title=f"Task: ' in anchor)

    for i in range(20):
        st.create(f"q{i}", driver="queue")
    got = []
    def worker():
        while True:
            t = st.claim()
            if not t:
                return
            got.append(t["id"])
    ths = [th.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    check("every queued task claimed exactly once",
          len(got) == 20 and len(set(got)) == 20)

    moved = st.requeue_interrupted()
    check("interrupted queue tasks are requeued", moved == 20)
    check("inline tasks are not requeued (recovery handles their reply)",
          st.get(inline["id"])["state"] == T.QUEUED and st.get(inline["id"])["driver"] == "inline")
    trail = [e["kind"] for e in st.get(got[0])["events"]]
    check("the interruption is recorded, not hidden",
          "failed" in trail and trail[-1] == "queued")

    st2 = TaskStore(Path(tempfile.mkdtemp()) / "t2.json")
    first = st2.create("first", driver="queue")
    st2.create("second", driver="queue")
    check("claims are oldest-first", st2.claim()["id"] == first["id"])

    # At startup no inline task can still be owned -- its handler lived in the
    # previous process. Gating on session liveness was wrong: a resumed session
    # is shared by every turn in its thread, so it is alive whenever a newer
    # turn runs, which would leave the orphan stuck in `running` forever.
    src = (BASE / "bot.py").read_text()
    co = src[src.index("def close_out_orphans"):src.index("def _recoverer")]
    check("startup closes out inline tasks orphaned by a restart",
          "interrupted by a restart" in co)
    check("the closeout does not gate on session liveness", "session_alive" not in co)
    check("recovery resolves the task it rescued",
          "recovered after a restart" in src)
    # This used to be guaranteed by ordering -- the closeout ran *after*
    # recovery, so anything rescued was already settled. That cost an hour of
    # records claiming work was in flight, because the startup pass waits that
    # long on a live child. The guarantee is now explicit instead of positional:
    # threads recovery still owns are skipped by name.
    check("the live turn recovery will resolve is never marked failed",
          'rec.get("source_ref") == live[thread]' in co,
          "a false failure that lasts the whole wait is what stops the board being read")
    # Skipping the whole *thread* left a task running for sixteen hours: the
    # conversation carried on, so a marker was always present, and it belonged
    # to a newer turn every time.
    check("but an older task on that same thread still gets closed out",
          'rec.get("thread") in pending' not in co,
          "a busy thread always has a pending marker, so this never fired")
    check("the marker is matched by message, which is what identifies a turn",
          '(e.get("pending") or {}).get("msg_ts")' in co)
    check("and the marker it keys on is the one recovery writes",
          'e.get("pending")' in co)


# --- gmail ingestion ----------------------------------------------------------

def test_email_ingest():
    import types
    import email_ingest as E
    from tasks import TaskStore
    print("\ngmail ingestion")

    src = (BASE / "email_ingest.py").read_text()
    check("mailbox is opened read-only", "readonly=True" in src)
    check("bodies are fetched with PEEK, never marking mail read", "BODY.PEEK" in src)
    check("flags are never written", "STORE" not in src)

    mail = [
        {"uid": 11, "id": "m1", "from": "Landlord", "subject": "Rent due Friday",
         "date": "", "snippet": "Confirm payment by Friday."},
        {"uid": 12, "id": "m2", "from": "Deals", "subject": "50% OFF",
         "date": "", "snippet": "Shop now"},
    ]
    def stub(out):
        E.subprocess = types.SimpleNamespace(
            run=lambda *a, **k: types.SimpleNamespace(stdout=out))

    for name, out in (("no json array", "message 1 matters"),
                      ("malformed json", "[{oops}]"),
                      ("hallucinated id", '[{"id":"nope","title":"x"}]')):
        stub(out)
        check(f"triage fails closed on {name}",
              E.triage(mail, binary="c", model="m", env={}) == [])

    stub('[{"id":"m1","title":"Confirm rent","why":"deadline"}]')
    flagged = E.triage(mail, binary="c", model="m", env={})
    check("only flagged mail survives triage",
          len(flagged) == 1 and flagged[0]["id"] == "m1")

    store = TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    fetch = lambda h, u, p, mb, since, lim: (            # noqa: E731
        [m for m in mail if m["uid"] > since][:lim],
        max([m["uid"] for m in mail] + [since]))
    state = {}
    r = E.ingest(store, state, host="h", user="u", password="p", fetch=fetch)
    t = store.get(r["tasks"][0])
    check("ingested mail lands as proposed, never queued", t["state"] == "proposed")
    check("the message is referenced, not forked",
          t["source"] == "email" and t["source_ref"] == "m1")

    r2 = E.ingest(store, state, host="h", user="u", password="p", fetch=fetch)
    check("a second pass proposes nothing", r2["proposed"] == 0)

    state["uid"] = 0
    r3 = E.ingest(store, state, host="h", user="u", password="p", fetch=fetch)
    check("a reset watermark still does not re-propose", r3["proposed"] == 0)

    bot = (BASE / "bot.py").read_text()
    check("watching is off unless credentials are set",
          'if not (GMAIL_USER and GMAIL_APP_PASSWORD):' in bot)


# --- a project can be reviewed while you sleep -----------------------------------
# A nightly look that proposes work, which you accept or dismiss in the morning.
# The `proposed` state existed for exactly this and had never once been used.

def test_ideation():
    import projects, roles, datetime as dt
    print("\nnightly review proposes, and can do nothing else")

    for text, want in (("2am", "02:00"), ("02:00", "02:00"), ("2:30pm", "14:30"),
                       ("12am", "00:00"), ("12pm", "12:00")):
        check(f"{text} reads as {want}", projects.parse_at(text) == want)
    for bad in ("25:00", "nonsense", "", "13pm"):
        try:
            projects.parse_at(bad)
            check(f"{bad!r} is refused", False, "it was accepted")
        except ValueError:
            check(f"{bad!r} is refused", True)

    ready = {"test_cmd": "make test", "auto_merge": True}
    recs = [{"slug": "trader", "ideate_at": "02:00", "ideate_on": "", "archived": False, **ready},
            {"slug": "saga", "ideate_at": "02:00", "ideate_on": "2026-09-06", "archived": False, **ready},
            {"slug": "odin", "ideate_at": "", "ideate_on": "", "archived": False, **ready},
            {"slug": "old", "ideate_at": "02:00", "ideate_on": "", "archived": True, **ready},
            # Switched on before the rule existed, and not ready: skipped.
            {"slug": "benji", "ideate_at": "02:00", "ideate_on": "", "archived": False}]
    due = projects.due_for_ideation(recs, dt.datetime(2026, 9, 6, 3, 0))
    check("only a project whose time has passed and has not run is due", due == ["trader"],
          f"got {due}")
    check("nothing is due before its time",
          projects.due_for_ideation(recs, dt.datetime(2026, 9, 6, 1, 0)) == [])
    check("a late start still runs the pass",
          projects.due_for_ideation(recs, dt.datetime(2026, 9, 6, 23, 0)) == ["trader"],
          "a bot asleep at 02:00 should not silently skip the night")

    check("the ideator is read-only", roles.get("ideator")["restricted"],
          "it runs unattended; nothing it thinks should ship on its own")
    check("and starts fresh each night", roles.get("ideator")["fresh"])
    tools = roles.permission_args("ideator", ["--dangerously-skip-permissions"],
                                  bin="/x/silkworm")[1]
    check("it may file proposals", "Bash(/x/silkworm task:*)" in tools)
    check("the path is absolute, not a glob", "*silkworm" not in tools,
          "these are prefix patterns; a leading * matched nothing and silently "
          "left the first ideator able to think but not to file")
    check("it may not edit, run tests, or push",
          "Edit" not in tools and "Write" not in tools and "Bash(git push" not in tools)

    # Schedulable from the dashboard as well as Slack, and validated in the
    # bot either way -- "2am" should work, and a typo should come back with a
    # reason rather than being stored as a time that never fires.
    bot = (BASE / "bot.py").read_text()
    pr = bot[bot.index('if action == "ideate"'):bot.index('if action == "brief"')]
    check("the dashboard can schedule a nightly review", "project_store.ensure(slug, ideate_at=" in pr)
    check("an unknown project is refused", "unknown project" in pr)
    check("a bad time is refused with a reason", "projects.parse_at(want)" in pr
          and "except ValueError as e" in pr,
          "storing an unparseable time would just never fire")
    check("off is spelled several plausible ways",
          '("", "off", "none", "clear")' in pr)

    # A fresh session every night, shown the code and not the board, re-derives
    # last night's gaps and files them again -- correctly, since a real gap is
    # still there tomorrow. The same landing defect arrived twice under two
    # framings, and one project collected seven proposals asking for the same
    # re-run. So the goal has to carry the project's own board.
    import tasks as T, scoping as S
    ts = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")

    def filed(title, state=T.PROPOSED, **kw):
        rec = ts.create(title, title=title, project="silkworm",
                        source="ideation", state=T.PROPOSED, **kw)
        for step in {T.PROPOSED: (), T.BLOCKED: (T.QUEUED, T.RUNNING, T.BLOCKED),
                     T.DONE: (T.QUEUED, T.RUNNING, T.DONE),
                     T.CANCELLED: (T.CANCELLED,)}[state]:
            rec = ts.transition(rec["id"], step)
        return rec

    waiting = filed("Add a test for the retry path")
    accepted = filed("Split visualizer.py", state=T.BLOCKED)
    shipped = filed("Give the dashboard a favicon", state=T.DONE)
    said_no = filed("Rewrite it all in Rust", state=T.CANCELLED)
    nightly = ts.create("Look over the silkworm project", title="Nightly review: Silkworm",
                        project="silkworm", role="ideator", source="ideation",
                        state=T.QUEUED)
    elsewhere = ts.create("Fix the Saga webhook", title="Fix the Saga webhook",
                          project="saga", source="ideation", state=T.PROPOSED)

    board = ts.by_project("silkworm")
    note = S.board_note(*S.already_filed(board, time.time()))

    check("the nightly goal names a proposal still waiting to be triaged",
          waiting["title"] in note,
          "without it the ideator re-derives and refiles the same idea tonight")
    check("and work already accepted and under way", accepted["title"] in note,
          "accepted is the strongest reason not to propose it a second time")
    check("and tells it not to file them again",
          "do not file any of these again" in note)
    check("finished work is not listed", shipped["title"] not in note,
          "shipping something is not a reason never to touch that area again")
    check("a dismissed proposal is remembered",
          said_no["title"] in note and "do not bring them back" in note,
          "an idea you rejected must not come back tomorrow looking fresh")
    check("the nightly review task is not itself listed as an idea",
          "Nightly review" not in note, f"{nightly['id']} is the job, not an idea")
    check("another project's board is not listed",
          elsewhere["title"] not in note, f"{elsewhere['id']} belongs to saga")
    check("filing nothing is named as an acceptable night",
          "file nothing and say so" in note,
          "otherwise a pass with nothing new invents something to justify itself")

    # A proposal you accepted and then stopped mid-run is not a dismissal: you
    # wanted it. `attempts` is the only durable record of having run at all.
    ran_then_stopped = filed("Rewrite the scheduler", state=T.BLOCKED)
    ts.transition(ran_then_stopped["id"], T.CANCELLED)
    check("work that was accepted, ran, and was then stopped is not a refusal",
          ran_then_stopped["title"] not in
          S.board_note(*S.already_filed(ts.by_project("silkworm"), time.time())),
          "listing it as 'the user said no' would bury an idea that was wanted")
    check("and the record that tells them apart is the one that survives "
          "compaction",
          ts.get(said_no["id"])["attempts"] == 0
          and ts.get(ran_then_stopped["id"])["attempts"] >= 1,
          "attempts is a plain int on the record, not an event that is trimmed")

    # Every Slack message and every scheduled wake-up is a task too, carrying
    # the thread's project and sitting non-terminal while it runs -- and
    # staying there if a restart or a quota error kills it. None of them is an
    # idea, and each one listed costs a slot a real proposal wanted.
    turn = ts.create("check whether the rejects cleared", title="check whether the "
                     "rejects cleared", project="silkworm", role="assistant",
                     source="slack", state=T.QUEUED)
    ts.transition(turn["id"], T.RUNNING)
    ts.transition(turn["id"], T.FAILED, "quota")
    wake = ts.create("look again in two hours", title="look again in two hours",
                     project="silkworm", role="assistant", source="defer",
                     state=T.QUEUED)
    ts.transition(wake["id"], T.BLOCKED)
    gate = ts.create("Review: something", title="Review: something",
                     project="silkworm", role="reviewer", source="review",
                     state=T.QUEUED)
    desk = ts.create("Split the runner out of bot.py", title="Split the runner out "
                     "of bot.py", project="silkworm", role="implementor",
                     source="ui", state=T.QUEUED)
    listed = S.board_note(*S.already_filed(ts.by_project("silkworm"), time.time()))
    check("a conversation turn is not listed as an idea already had",
          turn["title"] not in listed,
          f"{turn['id']} is a message that failed on quota, not a proposal")
    check("nor a scheduled wake-up", wake["title"] not in listed,
          f"{wake['id']} inherits the thread's project and sits blocked")
    check("nor the review gate itself", gate["title"] not in listed,
          "the gate is the board running, not something on it")
    check("but work typed into the dashboard is", desk["title"] in listed,
          f"{desk['id']} is a real item the pass should not propose again")

    # The dashboard route used to file under `ui` too -- the source a web chat
    # message gets -- so an assistant task typed into the form read as a
    # conversation turn and was never listed. It files as `dashboard` now.
    create, _store, _made = _dashboard_create_impl()
    asked = create({"action": "create", "goal": "Summarise last week's fills",
                    "role": "assistant", "project": "silkworm"})["task"]
    check("the dashboard files its tasks under their own source",
          asked["source"] == "dashboard" and asked["source"] != "ui",
          f"filed as {asked['source']!r}, which a web chat turn also uses")
    check("so an assistant task typed into it is work on the board",
          S.is_work(asked),
          "it was asked for from the task form, not said in a conversation")
    # A web chat turn names the message it answers (handle_prompt files it
    # with source_ref=msg_ts); what the old form filed under `ui` never did.
    check("while a web chat turn still is not",
          not S.is_work({"role": "assistant", "source": "ui",
                         "source_ref": "1784068538.524369"}))
    check("and an assistant task the old form filed under `ui` is work",
          S.is_work({"role": "assistant", "source": "ui", "source_ref": ""}),
          "no message ts: it was typed into the task form, not the chat")

    # `update` always stamps `updated`, so age the record directly.
    stale = dict(ts.get(said_no["id"]),
                 updated=time.time() - (S.DISMISSAL_MEMORY_DAYS + 1) * 86400)
    check("but a dismissal is forgotten eventually",
          S.already_filed([stale], time.time())[1] == [],
          "'not now' is not 'never'")
    check("a project with a clear board gets no paragraph at all",
          S.board_note([], []) == "",
          "a healthy project should not pay tokens for an empty list")

    for _ in range(S.MAX_LISTED + 5):
        filed("x" * 400)
    names, _ = S.already_filed(ts.by_project("silkworm"), time.time())
    deep = S.board_note(names, [])
    check("a long name is cut rather than sent whole",
          names and max(len(n) for n in names) <= S.MAX_NAME_CHARS,
          "the goal is rebuilt every night; one 400-char title is not worth it")
    check("and a board longer than the listing is capped",
          len(names) > S.MAX_LISTED
          and deep.count("\n  - ") == S.MAX_LISTED,
          f"got {len(names)} names, {deep.count(chr(10) + '  - ')} listed")
    check("but a cut list says so, instead of passing for the whole board",
          f"…and {len(names) - S.MAX_LISTED} more" in deep,
          "'do not file any of these again' over a silent slice is how the "
          "oldest untriaged proposal gets re-derived every night for ever")
    check("the cap is set above a real board, not at it",
          S.MAX_LISTED >= 40,
          "at 20 it cut six of trader's open items, two of them the very "
          "duplicates this paragraph exists to stop")

    # Behavioural rather than textual: computing the note and then dropping it
    # would leave every substring in place and the suite green, which is the
    # one mistake a wiring test exists to catch. Ask the tree what run_ideation
    # does with what it builds.
    fn = next(n for n in ast.walk(ast.parse(bot))
              if isinstance(n, ast.FunctionDef) and n.name == "run_ideation")
    called = {ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)}
    appended = [n for n in ast.walk(fn) if isinstance(n, ast.AugAssign)
                and isinstance(n.target, ast.Name) and n.target.id == "goal"
                and isinstance(n.op, ast.Add) and "note" in ast.unparse(n.value)]
    check("the nightly pass builds the board into the goal",
          {"scoping.already_filed", "scoping.board_note"} <= called,
          "the ideator is read-only over the repo; the board lives in Silkworm")
    check("and adds it to the goal rather than computing and dropping it",
          len(appended) == 2,
          f"expected the board note and the unmerged-branch note; got "
          f"{len(appended)} appends to goal")

    viz = (BASE / "visualizer.py").read_text()
    check("the panel shows which projects are scheduled", "function renderNightly" in viz)
    check("and says so when none are", "none scheduled" in viz,
          "the answer to 'what runs overnight' should be visible, not remembered")
    check("cancelling the prompt is not the same as turning it off",
          "if (at === null) return;" in viz)
    check("it shows the current time rather than making you recall it",
          "cur || \"02:00\"" in viz)

    h = bot[bot.index("def handle_file_task"):bot.index("server = LocalServer")]
    check("a proposal waits rather than running", "tasks.PROPOSED if propose" in h)
    import roles as _R
    check("and an ideator cannot file more ideators",
          "roles.validate_filed(role)" in h
          and bool(_R.validate_filed("ideator"))
          and bool(_R.validate_filed("reviewer")))
    ex = bot[bot.index("def execute_task"):bot.index("def resolve_review")]
    check("a read-only role gets no worktree",
          'not roles.get(role_name).get("restricted")' in ex,
          "it cannot write, so the worktree only leaves an empty branch behind")
    check("the scheduler records the date it ran", "ideate_on=" in bot,
          "or a restart in the small hours would run it twice")

    # The nightly pass fed a pile nothing emptied: 41 tasks sat in `proposed`
    # at once, on a board whose default view is "what needs me?". A night that
    # would only be refused at filing time is a session spent to learn what
    # the board already knew, so it does not start at all.
    import scoping as S
    fn = next(n for n in ast.walk(ast.parse(bot))
              if isinstance(n, ast.FunctionDef) and n.name == "run_ideation")
    guards = [n for n in ast.walk(fn) if isinstance(n, ast.If)
              and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                      and c.func.attr == "backlog_full" for c in ast.walk(n.test))]
    creates = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute) and n.func.attr == "create"]
    check("a deep board skips the night rather than filing a pass that gets refused",
          len(guards) == 1 and bool(creates)
          and all(line > guards[0].lineno for line in creates),
          "a refused pass still costs a session to find out")
    body = [n for g in guards for n in ast.walk(g) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)]
    check("the skipped night creates nothing",
          not any(c.func.attr == "create" for c in body),
          "a skipped pass that still files a task has skipped nothing")
    check("and is still marked as dealt with for today",
          any(c.func.attr == "ensure" and any(k.arg == "ideate_on" for k in c.keywords)
              for c in body),
          "otherwise the scheduler re-decides it every five minutes until midnight")
    # The skip stamps `ideate_on` so the scheduler stops re-deciding the same
    # night every five minutes until midnight. What it must not do is cost the
    # project its *next* look: the marker is a date, so it silences today and
    # nothing further. Driven rather than read, because "stamped" and "stamped
    # in a way that still lets tomorrow run" look identical in the source.
    import projects as _P
    _ps = _P.ProjectStore(Path(tempfile.mkdtemp()) / "p.json")
    _ps.ensure("Silkworm", ideate_at="02:00", test_cmd="true", auto_merge=True)
    _today = datetime(2026, 9, 22, 3, 0)
    check("a project with room is due once its time has passed",
          _P.due_for_ideation(_ps.all(), _today) == ["silkworm"])
    # What run_ideation does when it skips, done here directly.
    _ps.ensure("silkworm", ideate_on=_today.strftime("%Y-%m-%d"))
    check("a skipped night is not re-decided for the rest of that night",
          _P.due_for_ideation(_ps.all(), datetime(2026, 9, 22, 23, 59)) == [],
          "the scheduler wakes every five minutes; it would skip, and log, "
          "each time until midnight")
    check("but the skip does not cost the project its next night",
          _P.due_for_ideation(_ps.all(), datetime(2026, 9, 23, 3, 0)) == ["silkworm"],
          "a marker that outlived the night it was set for would turn a full "
          "board into a schedule that never runs again, even once emptied")

    check("the skip is logged with the reason",
          any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "info" for g in guards for n in ast.walk(g)),
          "ideation going quiet should be findable, not a mystery")
    # The panel is the only place a paused schedule can be seen: "it ran and
    # found nothing" and "it did not run" look identical from outside.
    check("the projects payload carries the limit the page compares against",
          '"proposal_limit": scoping.max_open_proposals()' in bot)
    import projects as P
    rows = P.summarise([{"slug": "silkworm", "title": "Silkworm", "archived": False}],
                       [{"project": "silkworm", "state": "proposed"},
                        {"project": "silkworm", "state": "proposed"},
                        {"project": "silkworm", "state": "queued"},
                        {"project": "silkworm", "state": "failed"}],
                       ("proposed", "awaiting_approval", "needs_input", "failed"))
    check("a project summary counts its untriaged proposals on their own",
          rows[0]["proposed"] == 2 and rows[0]["needs"] == 3,
          f"got {rows[0]}")
    check("the nightly panel is told the limit as well as the rows",
          "renderNightly(rows, r.proposal_limit" in (BASE / "visualizer.py").read_text())
    check("and a project with none says zero rather than omitting it",
          P.summarise([{"slug": "odin", "title": "Odin", "archived": False}], [],
                      ("proposed",))[0]["proposed"] == 0,
          "an absent field reads as unknown in the page, not as none")


def test_nightly_panel_shows_paused():
    """A skipped night must look skipped, not quiet.

    `renderNightly` is driven rather than read: a panel that renders the
    button but never reaches the paused branch looks identical in the source.
    """
    import re, json as _j, subprocess as _sp, tempfile as _tf
    sys.argv = ["x"]
    import visualizer as V
    print("\nthe nightly panel says when a project is paused")

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
const _n = _el("nightly");
const rows = [{slug: "silkworm", title: "Silkworm", ideate_at: "02:00", proposed: 10},
              {slug: "saga", title: "Saga", ideate_at: "02:00", proposed: 2},
              {slug: "odin", title: "Odin", ideate_at: "", proposed: 40}];
renderNightly(rows, 10);
const out = {withLimit: _n.innerHTML};
_n.innerHTML = "";
renderNightly(rows, 0);
out.noLimit = _n.innerHTML;
console.log(JSON.stringify(out));
"""
    f = Path(_tf.mkdtemp()) / "h.js"
    f.write_text(prelude + js + drive)
    r = _sp.run(["node", str(f)], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        check("the panel renders in a browser-like context", False,
              (r.stderr or "").strip().splitlines()[-1] if r.stderr else "no output")
        return
    out = _j.loads(r.stdout.strip().splitlines()[-1])
    html = out["withLimit"]
    silkworm = html[html.index("Silkworm"):html.index("Saga")]
    saga = html[html.index("Saga"):]
    check("a project at the limit is labelled paused, not scheduled",
          "paused" in silkworm and "10 proposals waiting" in html,
          f"got {silkworm!r}")
    check("and says what to do about it",
          "accept or dismiss" in html and "02:00" in silkworm)
    check("a project with room still shows its time",
          "paused" not in saga and "<b>02:00</b>" in saga, f"got {saga!r}")
    check("one that is switched off is not called paused",
          "Odin" in html and "paused" not in html[html.index("Odin"):],
          "it has no schedule to pause")
    check("no limit means nothing is claimed to be paused",
          "paused" not in out["noLimit"],
          "an older payload should not invent a state")


def test_a_deep_board_costs_no_session():
    """The guard is run, not read.

    Every other check on this is structural -- that the `if` is there, that
    nothing is created inside it. None of them would notice a guard that was
    present and never true, so this one drives the real function against real
    stores and counts the sessions it would have started.
    """
    import projects as P, scoping, tasks
    print("\na project at its standing limit is not looked at")
    run_ideation, ts, ps = _run_ideation_impl()
    ps.ensure("Silkworm", ideate_at="02:00", test_cmd="true", auto_merge=True)

    out = run_ideation("silkworm")
    check("an empty board still gets its nightly look",
          out.get("id") and len(ts.all()) == 1,
          f"got {out}")
    check("and the night is recorded as run",
          ps.get("silkworm")["ideate_on"] == datetime.now().strftime("%Y-%m-%d"))

    for i in range(scoping.max_open_proposals()):
        ts.create(f"Proposal number {i} worth deciding on", project="silkworm",
                  state=tasks.PROPOSED, role="implementor")
    before = len(ts.all())
    ps.ensure("silkworm", ideate_on="")          # a fresh night
    out = run_ideation("silkworm")
    check("a board at the limit starts no session at all",
          out.get("skipped") == "backlog" and not out.get("id")
          and len(ts.all()) == before,
          f"got {out} and {len(ts.all()) - before} new task(s)")
    check("and the skip says how deep the board is and what the limit was",
          out.get("open") == scoping.max_open_proposals()
          and out.get("limit") == scoping.max_open_proposals(),
          f"got {out}")

    # The marker is the part that could wedge this permanently: stamped so the
    # scheduler stops re-deciding tonight, but a *date*, so tomorrow is free.
    today = datetime.now().strftime("%Y-%m-%d")
    check("a skipped night is still marked as dealt with",
          ps.get("silkworm")["ideate_on"] == today,
          "the scheduler wakes every five minutes until midnight")
    check("so it is not re-decided for the rest of tonight",
          P.due_for_ideation(ps.all(), datetime.now().replace(hour=23, minute=59)) == [])

    # Triage the backlog, and the next night must come back on its own.
    for t in ts.by_project("silkworm"):
        if t.get("state") == tasks.PROPOSED:
            ts.transition(t["id"], tasks.CANCELLED, "dismissed")
    tomorrow = datetime.now() + timedelta(days=1)
    check("emptying the board brings the schedule back without touching config",
          P.due_for_ideation(ps.all(), tomorrow.replace(hour=3, minute=0))
          == ["silkworm"],
          "a pause that needed a setting changed to undo would be a trap")
    ps.ensure("silkworm", ideate_on="")
    out = run_ideation("silkworm")
    check("and the look actually runs again",
          out.get("id") and not out.get("skipped"), f"got {out}")


def test_nightly_review_rotates_its_lens():
    """Each night looks through one lens -- features, quality, correctness --
    rotated by project and date, overridable per project, and recorded on the
    pass and on what it files.

    One generic goal under a prompt about gaps and risks made every night's
    proposals fixes; nothing on the board said which kind of night produced
    what, so acceptance could not be read per kind.
    """
    import ideation as I, roles as R, tasks as T, digest, projects
    from datetime import date
    print("\nthe nightly review rotates features, quality and correctness")

    # Rotation, from the date: three nights in a row are three lenses, for
    # every project, and the projects do not all share tonight's.
    for slug in ("silkworm", "trader", "cadence", "mondays", ""):
        d = date(2026, 11, 17)
        three = [I.lens_for(slug, d + timedelta(days=i)) for i in range(3)]
        check(f"three consecutive nights give three lenses ({slug or 'no slug'})",
              sorted(three) == sorted(I.LENSES), f"got {three}")
        check(f"and the cycle is features -> quality -> correctness ({slug or 'no slug'})",
              all(I.LENSES[(I.LENSES.index(a) + 1) % 3] == b
                  for a, b in zip(three, three[1:])), f"got {three}")
        check(f"a skipped night does not repeat its lens ({slug or 'no slug'})",
              I.lens_for(slug, d) != I.lens_for(slug, d + timedelta(days=2)),
              "the night after a skip must move on, not look again the same way")
        check(f"a restart the same night gives the same lens ({slug or 'no slug'})",
              I.lens_for(slug, datetime(2026, 11, 17, 2, 0))
              == I.lens_for(slug, datetime(2026, 11, 17, 23, 59)))
    tonight = {I.lens_for(s, date(2026, 11, 17)) for s in
               ("silkworm", "trader", "cadence", "mondays", "saga", "odin")}
    check("projects reviewed the same night do not all share one lens",
          len(tonight) > 1, f"all {tonight}")

    # The override: its own cycle, still by date; a single lens is every night.
    d = date(2026, 11, 17)
    two = [I.lens_for("trader", d + timedelta(days=i), ["features", "correctness"])
           for i in range(4)]
    check("an override is honoured", set(two) == {"features", "correctness"}, f"got {two}")
    check("and still rotates night by night", two[0] != two[1] and two[1] != two[2],
          f"got {two}")
    check("a single-lens override looks through it every night",
          {I.lens_for("trader", d + timedelta(days=i), ["quality"]) for i in range(5)}
          == {"quality"})
    check("an empty override is the default",
          [I.lens_for("trader", d + timedelta(days=i), []) for i in range(3)]
          == [I.lens_for("trader", d + timedelta(days=i)) for i in range(3)])
    check("a stored override with a typo falls back rather than raising in the scheduler",
          I.cycle(["featurs"]) == I.LENSES)

    # Names are validated, not stored on trust.
    check("lens names parse from a comma list",
          I.parse("features, correctness") == ["features", "correctness"]
          and I.parse("Quality") == ["quality"] and I.parse(["quality", "quality"]) == ["quality"])
    check("and default, off or empty mean the default cycle",
          I.parse("") == [] and I.parse("default") == [] and I.parse(None) == [])
    for bad in ("featurs", "features,security", "bugs", ["features", 3]):
        try:
            I.parse(bad)
            check(f"{bad!r} is refused", False, "an unknown lens was accepted")
        except ValueError as e:
            check(f"{bad!r} is refused, naming the lenses there are",
                  "features" in str(e) or "text" in str(e), str(e))

    # The route, from Slack and the dashboard: refused with a reason, and
    # nothing written.
    root = Path(tempfile.mkdtemp())
    ps = projects.ProjectStore(root / "p.json")
    ps.ensure("Trader", test_cmd="pytest", auto_merge=True)
    hp = {"projects": projects, "project_store": ps, "log": logging.getLogger("test")}
    lift({"handle_projects", "set_ideate_lenses"}, ns=hp, absent=PROJECT_ROUTES)
    r = hp["handle_projects"]({"action": "ideate-lenses", "slug": "trader",
                               "lenses": "features,correctness"})
    check("the dashboard can set a project's lenses",
          r.get("ok") and ps.get("trader")["ideate_lenses"] == ["features", "correctness"], str(r))
    r = hp["handle_projects"]({"action": "ideate-lenses", "slug": "trader", "lenses": "featurs"})
    check("a bad lens name is refused with a reason",
          r.get("ok") is False and "unknown lens" in r.get("error", ""), str(r))
    check("and the stored lenses are left as they were",
          ps.get("trader")["ideate_lenses"] == ["features", "correctness"])
    r = hp["handle_projects"]({"action": "ideate-lenses", "slug": "trader", "lenses": ""})
    check("an empty override puts the default back",
          r.get("ok") and ps.get("trader")["ideate_lenses"] == [], str(r))
    r = hp["handle_projects"]({"action": "ideate-lenses", "slug": "nope", "lenses": "quality"})
    check("an unknown project is refused", r.get("ok") is False, str(r))
    bot = (BASE / "bot.py").read_text()
    cmd = bot[bot.index('elif arg.lower().startswith("ideate"):'):
              bot.index('elif arg.lower() == "test" or arg.lower().startswith("test "):')]
    check("Slack's `!project ideate lenses ...` goes through the same validation",
          'when.lower().startswith("lenses ")' in cmd
          and "set_ideate_lenses(slug, when[6:].strip())" in cmd)

    # The goal: each lens's paragraph and the shared rules, and the lens on
    # the task -- driven through the real run_ideation.
    run_ideation, ts, ps2 = _run_ideation_impl()
    ps2.ensure("Silkworm", ideate_at="02:00", test_cmd="true", auto_merge=True)
    for lens in I.LENSES:
        ps2.ensure("silkworm", ideate_on="", ideate_lenses=[lens])
        out = run_ideation("silkworm")
        rec = ts.get(out.get("id") or "") or {}
        goal = rec.get("goal") or ""
        check(f"a {lens} night's goal carries its paragraph", I.PARAGRAPHS[lens] in goal)
        check(f"and the shared rules ({lens})", I.SHARED_RULES in goal)
        check(f"and no other lens's paragraph ({lens})",
              not any(I.PARAGRAPHS[o] in goal for o in I.LENSES if o != lens))
        check(f"and still how to file, at most MAX_PROPOSALS ({lens})",
              "task --propose --project silkworm" in goal
              and f"at most {_max_proposals()} of them" in goal)
        check(f"the lens is recorded on the task ({lens})",
              rec.get("lens") == lens and out.get("lens") == lens, f"got {rec.get('lens')!r}")
        check(f"and named in its title ({lens})",
              rec.get("title") == f"Nightly review ({lens}): Silkworm", rec.get("title"))
    ps2.ensure("silkworm", ideate_on="", ideate_lenses=[])
    out = run_ideation("silkworm")
    check("with no override, tonight's lens is the rotation's",
          ts.get(out["id"])["lens"] == I.lens_for("silkworm", datetime.now()))
    check("the features paragraph asks who, why, how big and what it costs to run",
          all(w in I.PARAGRAPHS["features"] for w in
              ("who it is for", "why they", "how big", "costs to run", "privacy policy")))
    check("the system prompt is lens-neutral",
          "a real gap, a real inconsistency, a real risk" not in R.IDEATOR_SYSTEM
          and I.SHARED_RULES in R.IDEATOR_SYSTEM
          and R.get("ideator")["system"] == R.IDEATOR_SYSTEM)

    # The proposals it files carry its lens -- read off the bot's own record
    # of the pass running on the thread, never off the caller.
    file_task, filed_this_turn, _begin, fts, _made = _file_task_impl()
    KEY = "C1:1785644289.000200"
    night = fts.create("Look over saga", title="Nightly review (quality): saga",
                       role="ideator", source="ideation", thread=KEY, lens="quality",
                       state=T.QUEUED, driver="queue", project="saga")
    fts.transition(night["id"], T.RUNNING)
    r = file_task({"key": KEY, "goal": "Split the sync engine out of AppModel.swift",
                   "caller_role": "ideator", "caller_task": night["id"], "propose": True,
                   "project": "saga", "lens": "features"})
    rec = fts.get(r.get("id") or "") or {}
    check("a proposal filed by the nightly pass carries its lens",
          r.get("ok") and rec.get("lens") == "quality", f"{r} / {rec.get('lens')!r}")
    check("and the caller cannot name a different one", rec.get("lens") != "features")
    filed_this_turn.clear()
    r = file_task({"key": "C1:1785644289.000300", "goal": "Add CSV export of the week",
                   "propose": True, "project": "saga"})
    check("a proposal filed from any other thread carries no lens",
          r.get("ok") and fts.get(r["id"]).get("lens") == "", str(r))
    filed_this_turn.clear()
    other = "C1:1785644289.000400"
    turn = fts.create("a conversation", role="assistant", source="slack", thread=other,
                      lens="features", state=T.QUEUED, project="saga")
    fts.transition(turn["id"], T.RUNNING)
    r = file_task({"key": other, "goal": "Add a weekly summary screen to saga",
                   "propose": True, "project": "saga", "caller_task": turn["id"]})
    check("only an ideation pass lends its lens, not any running task",
          r.get("ok") and fts.get(r["id"]).get("lens") == "", str(r))
    check("no lens when no pass is running there",
          I.filed_under(fts, "", "C9:1.0") == "" and I.filed_under(fts, night["id"], "") == "")

    # Shown: the digest says which lens each night looked through and what it
    # filed; the dashboard draws it on cards, rows and the detail view.
    now = time.time()
    rows = [dict(fts.get(night["id"]), created=now - 3600),
            dict(fts.get(r["id"]), created=now - 60)]
    for p in [x for x in fts.all().values() if x.get("lens") == "quality"
              and x.get("role") != "ideator"]:
        rows.append(dict(p, created=now - 1800))
    sec = digest.sections(rows, now).get("saga") or {}
    check("the digest names the night's lens and what it proposed",
          sec.get("nightly") == ["quality, 1 proposed"], str(sec.get("nightly")))
    text = digest.render(rows, now)
    check("under its own heading", "nightly review: quality, 1 proposed" in text, text)
    import board
    check("the dashboard's slim rows carry the lens", "lens" in board.ROW_FIELDS)
    viz = (BASE / "visualizer.py").read_text()
    check("the dashboard draws it", viz.count("t.lens ?") >= 3)
    check("and project settings can set the lenses",
          'action: "ideate-lenses"' in viz and 'id="pslenses"' in viz)
    check("the task and project records declare their fields",
          "lens" in T.FIELDS and "ideate_lenses" in projects.FIELDS)

    # Two passes in one window (a late catch-up, then the next night), both
    # quality: each counts only what it filed, not the other's too.
    two = [{"id": "n1", "role": "ideator", "source": "ideation", "lens": "quality",
            "project": "saga", "state": "done", "created": now - 20 * 3600},
           {"id": "p1", "role": "implementor", "source": "ideation", "lens": "quality",
            "project": "saga", "state": "proposed", "created": now - 19 * 3600},
           {"id": "n2", "role": "ideator", "source": "ideation", "lens": "quality",
            "project": "saga", "state": "done", "created": now - 2 * 3600},
           {"id": "p2", "role": "implementor", "source": "ideation", "lens": "quality",
            "project": "saga", "state": "proposed", "created": now - 3600}]
    got = (digest.sections(two, now).get("saga") or {}).get("nightly")
    check("two passes in one window each count only their own proposals",
          got == ["quality, 1 proposed", "quality, 1 proposed"], str(got))

    # A hand-edited record must not raise in the scheduler thread, where it
    # would skip every project after it, every five minutes, all night.
    for junk in (5, True, {"x": 1}, "featurs"):
        try:
            ok = I.cycle(junk) == I.LENSES
        except Exception as e:
            ok = f"raised {e!r}"
        check(f"a malformed stored override ({junk!r}) falls back to the default", ok is True, str(ok))
    check("a stored string override is read as one",
          I.cycle("quality") == ("quality",) and I.cycle("features, quality") == ("features", "quality"))
    check("typing `!project ideate lenses` alone shows the cycle, not resets it",
          'elif when.lower() == "lenses":' in cmd
          and "set_ideate_lenses" not in cmd[cmd.index('elif when.lower() == "lenses":'):
                                             cmd.index('elif when.lower().startswith("lenses ")')])


def _max_proposals():
    import scoping
    return scoping.MAX_PROPOSALS


# --- a scoping conversation must be able to emit work ----------------------------
# Of 143 recent tasks, 130 were live conversation and 3 were made in the
# dashboard. Not a preference for chat: a conversation could not *emit*
# anything, so scoping ended with a plan in a thread and no way to act on it.

def test_scoping():
    import roles as R, scoping as S, tasks as T
    print("\nscoped work can be filed from a conversation")

    check("a goal too short to act on is refused",
          "at least" in S.validate("do it"))
    check("a workable goal passes",
          S.validate("Add an appearance preference to Settings") == "")
    check("an enormous goal is refused, not truncated",
          "split it" in S.validate("x" * (S.MAX_GOAL_CHARS + 1)))
    check("a turn cannot file without limit",
          "limit for one turn" in S.validate("Add an appearance preference",
                                             filed_already=S.MAX_PER_TURN),
          "every task costs a session, or two once reviewed")
    check("the cap leaves room for a real plan", 3 <= S.MAX_PER_TURN <= 20)

    # The five-proposal cap used to exist only as a sentence in the ideator's
    # prompt: a run that miscounted filed ten, and every extra one costs the
    # decision the proposed gate exists to protect.
    check("a proposal run is cut off at five",
          "limit for one pass" in S.validate("Add an appearance preference",
                                             filed_already=S.MAX_PROPOSALS,
                                             propose=True),
          "the sixth proposal must be refused, not asked for politely")
    check("the fifth proposal still goes through",
          S.validate("Add an appearance preference",
                     filed_already=S.MAX_PROPOSALS - 1, propose=True) == "")
    check("a scoping conversation keeps the larger budget",
          S.validate("Add an appearance preference",
                     filed_already=S.MAX_PROPOSALS) == "",
          "work agreed with you is not rationed like an unattended pass")
    check("proposals are the scarcer of the two budgets",
          S.MAX_PROPOSALS < S.MAX_PER_TURN
          and S.limit_for(True) == S.MAX_PROPOSALS
          and S.limit_for(False) == S.MAX_PER_TURN)

    # The per-pass cap counts only what the running pass filed, so every night
    # started from zero however many of its predecessors' proposals were still
    # untriaged. That put 41 tasks in `proposed` at once, on a board whose
    # whole premise is that it can reach empty.
    check("a board already at the standing limit takes no more proposals",
          "standing limit" in S.validate("Add an appearance preference",
                                         propose=True, slug="silkworm",
                                         open_now=S.max_open_proposals()),
          "the per-pass cap resets nightly; nothing bounded the pile")
    # Deliberately a count that is not the limit: asking with open_now equal
    # to the limit makes the two numbers indistinguishable, and an assertion
    # that cannot tell them apart passed while the count was dropped entirely.
    said = S.backlog_refusal("silkworm", S.max_open_proposals() + 2)
    check("the refusal says how many are waiting, what the limit is, and where",
          str(S.max_open_proposals() + 2) in said
          and str(S.max_open_proposals()) in said and "silkworm" in said,
          f"got {said!r}")
    check("one below the limit still goes through",
          S.validate("Add an appearance preference", propose=True,
                     open_now=S.max_open_proposals() - 1) == "",
          "the limit is a ceiling on the pile, not a freeze at the approach")
    check("work scoped with you is not rationed by the proposal backlog",
          S.validate("Add an appearance preference", propose=False,
                     open_now=S.max_open_proposals() * 10) == "",
          "you agreed it; a nightly pass getting ahead of itself is not a "
          "reason to refuse the thing you just asked for")
    check("only untriaged proposals count toward it",
          S.open_proposals([{"state": "proposed"}, {"state": "proposed"},
                            {"state": "queued"}, {"state": "awaiting_approval"},
                            {"state": "done"}, {"state": "cancelled"}]) == 2,
          "accepted work is not a decision you still owe, and a dismissal is "
          "an answer")
    check("the standing limit leaves room for more than one night",
          S.MAX_PROPOSALS < S.DEFAULT_OPEN_PROPOSALS <= 20,
          "below one pass's worth it would refuse mid-night; far above it and "
          "the list stops being readable, which is the failure it exists for")
    check("it is configurable without editing the source",
          "_env_int(\"MAX_OPEN_PROPOSALS\"" in (BASE / "scoping.py").read_text())
    # bot.py imports this module and calls load_dotenv() afterwards, so a
    # limit bound at import time is fixed before `.env` is read: documented,
    # and inert. Set after import here for exactly that reason.
    os.environ["MAX_OPEN_PROPOSALS"] = "3"
    check("an override set after import is still honoured",
          S.max_open_proposals() == 3 and S.backlog_full(3)
          and not S.backlog_full(2),
          "a constant evaluated at import would ignore everything in .env")
    check("and the refusal quotes the configured limit, not the default",
          "limit is 3" in S.backlog_refusal("silkworm", 4))
    check("an override is read from the environment", S._env_int("MAX_OPEN_PROPOSALS", 10) == 3)
    for bad in ("nonsense", "", "0", "-2"):
        os.environ["MAX_OPEN_PROPOSALS"] = bad
        check(f"{bad!r} falls back to the default rather than freezing ideation",
              S.max_open_proposals() == S.DEFAULT_OPEN_PROPOSALS,
              "a limit under one would silently stop a schedule the user "
              "still believes is running")
    os.environ.pop("MAX_OPEN_PROPOSALS", None)
    check("and with nothing set at all, the default applies",
          S.max_open_proposals() == S.DEFAULT_OPEN_PROPOSALS)

    fn = next(n for n in ast.walk(ast.parse((BASE / "bot.py").read_text()))
              if isinstance(n, ast.FunctionDef) and n.name == "handle_file_task")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "validate"]
    kw = {k.arg: k.value for c in calls for k in c.keywords}
    check("the filing path counts the board, not a per-turn tally",
          "open_now" in kw and "slug" in kw,
          "otherwise a pass that began while there was room files past the limit")
    guarded = [n for n in ast.walk(fn) if isinstance(n, ast.IfExp)
               and any(isinstance(v, ast.Name) and v.id == "slug"
                       for v in ast.walk(n.test))
               and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                       and c.func.attr == "open_proposals" for c in ast.walk(n))]
    check("an unfiled proposal is not counted against a project",
          len(guarded) == 1,
          'by_project("") is every task filed under no project at all — '
          "mail triage's proposals — and counting them would refuse an "
          "unrelated filing over a pile no project panel can show")
    counted = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute)
               and n.func.attr == "open_proposals"]
    check("and counts it live from the task store",
          len(counted) == 1 and any(isinstance(a, ast.Call)
                                    and isinstance(a.func, ast.Attribute)
                                    and a.func.attr == "by_project"
                                    for a in counted[0].args),
          "a cached count would let a long pass file past the limit")
    # Counting the board needs the project's key, and the obvious way to get
    # one -- `project_store.ensure` -- creates the record and its home
    # directory on disk. That write has to stay below the gates, or a refused
    # filing leaves a project behind, done on behalf of a role whose whole
    # point is that it cannot write. So the count slugifies instead.
    gate = next(n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "validate")
    writes = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)
              and n.func.attr in ("ensure", "home")
              and isinstance(n.func.value, ast.Name)
              and n.func.value.id == "project_store"]
    check("naming a project to count it does not create one",
          bool(writes) and all(line > gate for line in writes),
          "a refused filing would leave a project record and a directory "
          "behind it")
    check("the count reaches the slug without writing",
          any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "slug_for" and n.lineno < gate
              for n in ast.walk(fn)),
          "by_project is keyed by slug, and slug_for is the read-only way there "
          "(slugify alone misses a renamed project)")

    bot = (BASE / "bot.py").read_text()
    h = bot[bot.index("def handle_file_task"):bot.index("server = LocalServer")]
    check("work scoped with you is queued, not proposed",
          "tasks.PROPOSED if propose else tasks.QUEUED" in h,
          "you scoped it with the user, who is who the proposed gate asks — "
          "only an unattended proposal has to wait")
    check("it defaults to implementor, so output gets reviewed",
          'payload.get("role") or roles.DEFAULT_FILED' in h
          and R.DEFAULT_FILED == "implementor" and R.needs_review(R.DEFAULT_FILED))
    check("neither a reviewer nor an ideator can be filed as work",
          "roles.validate_filed(role)" in h
          and bool(R.validate_filed("reviewer")) and bool(R.validate_filed("ideator")),
          "reviewing a review would not terminate, and an ideator that could "
          "file ideators would propose its way into a loop")
    check("it runs on the queue, not inline", 'driver="queue"' in h,
          "nobody is holding a live message for it")
    check("project and scope are inherited from the thread",
          'entry.get("project")' in h and "project_store.scope_for" in h,
          "a bound conversation should not restate where its work belongs")
    # The budget used to be reset only on the live Slack path, and this check
    # was a grep for the text of that one line -- so it passed throughout,
    # while every task the queue runner started kept whatever the last run had
    # spent. Two checks replace it: the budget itself is driven through the
    # real filing route, and the two paths that start a turn are read for the
    # reset. Reading is the weaker of the two, but execute_task cannot be run
    # here, so what it pins is made as narrow as possible: the exact call, in
    # the right place, whatever spelling it is written in.
    file_task, _filed, begin_turn, _ts, _made = _file_task_impl()
    THREAD = "C1:1785644289.053039"

    def propose(key=THREAD):
        return file_task({"key": key, "propose": True,
                          "goal": "Add an appearance preference to Settings"})

    spent = [propose() for _ in range(S.MAX_PROPOSALS)]
    check("a pass may file up to the proposal cap",
          all(r["ok"] for r in spent) and spent[-1]["remaining"] == 0)
    check("and is refused once it has spent it",
          "limit for one pass" in propose().get("error", ""))
    begin_turn(THREAD)
    check("the reset hands the next turn a whole budget back",
          propose().get("ok") is True,
          "a task keeps its thread key across runs, so a retried, reworked or "
          "sent-back ideator would file nothing at all")
    # Several tasks share one thread key -- a reviewer child, a wake-up,
    # orphans handed back to the runner. A reset that cleared the whole budget
    # would hand every other live turn one that had already been spent.
    OTHER = "C1:1785644999.111111"
    for _ in range(S.MAX_PROPOSALS):
        propose(OTHER)
    begin_turn(THREAD)
    check("and leaves every other thread's alone",
          "limit for one pass" in propose(OTHER).get("error", ""))

    # Neither path may drift: a turn that starts without resetting first is
    # the same bug, wherever it is written. Matched on the attribute as well
    # as the bare name, because `claude_runner.run_turn(...)` is a third
    # starter neither check would otherwise see.
    tree = ast.parse(bot)
    def calls(node, name):
        return [c for c in ast.walk(node) if isinstance(c, ast.Call)
                and (getattr(c.func, "id", "") == name
                     or getattr(c.func, "attr", "") == name)]
    starters = [f for f in ast.walk(tree)
                if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                and calls(f, "run_turn")]
    check("both of the paths that start a turn are accounted for",
          {f.name for f in starters} == {"handle_prompt", "execute_task"},
          f"found {sorted(f.name for f in starters)}")
    # Under the thread's lock, not merely somewhere above the run. Resetting
    # before the lock is taken lets a second worker claiming a task on the
    # same key wipe the budget of the turn already running on it -- which is
    # the concurrent case the check above describes.
    locked = []
    for f in starters:
        # The `with lock, ...` block the turn itself runs inside.
        held = [w for w in ast.walk(f) if isinstance(w, ast.With)
                and any(isinstance(i.context_expr, ast.Name)
                        and i.context_expr.id == "lock" for i in w.items)
                and calls(w, "run_turn")]
        locked.append(bool(held))
        for w in held:
            reset, run = calls(w, "begin_turn"), calls(w, "run_turn")
            locked.append(bool(reset) and min(c.lineno for c in reset)
                          < min(c.lineno for c in run))
    check("every path resets the budget under the lock, before it runs",
          bool(starters) and all(locked),
          "the queue runner ran every queued, ideator, implementor and "
          "reviewer task without one")
    check("the model is told the capability exists", "scoping.HOW_TO" in bot)

    fn = next(n for n in ast.walk(ast.parse(bot))
              if isinstance(n, ast.FunctionDef) and n.name == "handle_file_task")
    validates = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "validate"]
    kw = {k.arg: k.value for c in validates for k in c.keywords}
    check("the filing path tells the validator what it is filing",
          len(validates) == 1 and isinstance(kw.get("propose"), ast.Name)
          and kw["propose"].id == "propose",
          "otherwise the proposal cap is only a sentence in a prompt")
    decided = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "propose" for t in n.targets)]
    check("and knows which it is before it validates",
          bool(decided) and bool(validates) and max(decided) < validates[0].lineno)
    limits = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute) and n.func.attr == "limit_for"]
    check("the budget reported back is the one enforced",
          len(limits) == 1
          and [a.id for a in limits[0].args if isinstance(a, ast.Name)]
          == ["propose", "from_research"]
          and "MAX_PER_TURN" not in h,
          "a proposal run told '9 more allowed' would go on filing")
    check("and the research budget is the one validated for a research run",
          isinstance(kw.get("research"), ast.Name) and kw["research"].id == "from_research",
          "a research run told four but refused at two")

    # Where a filed task lands is decided by who filed it, not by a flag the
    # filer chose. The ideator's allowlist is `Bash({bin} task:*)` -- a prefix,
    # so it permits an invocation with no --propose at all, and that filed
    # straight to `queued` with role implementor. The queue runner then ran it
    # overnight with full write permissions: read-only stopped the ideator
    # editing the tree, and did not stop it commissioning an agent that would.
    # Driven through the real route rather than read off the source, because
    # the thing that broke was the behaviour, not the wording.
    file_task, filed_this_turn, _begin, ts, projects_made = _file_task_impl()
    store_of = ts.get
    KEY = "C1:1785644289.000100"

    r = file_task({"key": KEY, "goal": "Cache the résumé parser's output",
                   "caller_role": "ideator"})
    rec = store_of(r["id"])
    check("an ideator that omits --propose still lands in proposed",
          r["ok"] and r["state"] == T.PROPOSED and rec["state"] == T.PROPOSED,
          f"filed {r.get('state')} — the runner would have run this unattended")
    check("and is recorded as the unattended pass it was",
          rec["source"] == "ideation",
          "queued-by-an-ideator read as work you scoped")
    check("the tighter budget applies to it too",
          r["remaining"] == S.MAX_PROPOSALS - 1,
          f"told {r.get('remaining')} more of {S.MAX_PER_TURN}, not "
          f"{S.MAX_PROPOSALS}")

    filed_this_turn.clear()
    r = file_task({"key": KEY, "goal": "Cache the résumé parser's output",
                   "caller_role": "reviewer"})
    check("so does a reviewer, for the same reason",
          r["ok"] and r["state"] == T.PROPOSED)

    filed_this_turn.clear()
    r = file_task({"key": KEY, "goal": "Cache the résumé parser's output",
                   "caller_role": "wat"})
    check("an unrecognised caller does not buy full autonomy",
          r["ok"] and r["state"] == T.PROPOSED,
          "roles.get reads an unknown name as assistant, so a typo would queue")

    filed_this_turn.clear()
    r = file_task({"key": KEY, "goal": "Cache the résumé parser's output",
                   "caller_role": "assistant"})
    check("work scoped with you in a conversation still queues",
          r["ok"] and r["state"] == T.QUEUED and r["remaining"] == S.MAX_PER_TURN - 1,
          "the person the proposed gate asks was in the room")

    filed_this_turn.clear()
    r = file_task({"key": KEY, "goal": "Cache the résumé parser's output"})
    check("and so does a person at a terminal, with no role at all",
          r["ok"] and r["state"] == T.QUEUED)

    # The environment reaches this route back through the CLI. The role of the
    # task actually running on the thread does not: it is the bot's own record.
    # Either one saying "restricted" is enough, so neither is load-bearing on
    # its own -- here the caller claims nothing at all and is still held.
    ts.create("Nightly review: saga", role="ideator", thread=KEY,
              state=T.RUNNING, driver="queue")
    filed_this_turn.clear()
    r = file_task({"key": KEY, "goal": "Cache the résumé parser's output"})
    check("a caller that claims nothing is still read off the running task",
          r["ok"] and r["state"] == T.PROPOSED,
          "the thread has a running ideator on it")
    filed_this_turn.clear()
    r = file_task({"key": "C1:1785644289.000999",
                   "goal": "Cache the résumé parser's output"})
    check("and only on its own thread",
          r["ok"] and r["state"] == T.QUEUED,
          "someone else's nightly pass must not hold your filing")

    filed_this_turn.clear()
    filed = [n for n in range(9)
             if file_task({"key": KEY, "caller_role": "ideator",
                           "goal": f"Cache the résumé parser's output {n}"})["ok"]]
    check("and an unattended run is refused at the sixth",
          filed == list(range(S.MAX_PROPOSALS)),
          f"it filed {len(filed)} without ever asking to propose")

    # Naming a project creates its record and its home directory. That ran
    # above the checks, so a filing the ideator was refused still left one
    # behind -- a write, on behalf of the one role that may not write.
    filed_this_turn.clear()
    projects_made.clear()
    r = file_task({"key": KEY, "goal": "too short", "project": "invented",
                   "caller_role": "ideator"})
    check("a refused filing creates no project",
          not r["ok"] and projects_made == [],
          f"made {projects_made} anyway")
    r = file_task({"key": KEY, "goal": "Cache the résumé parser's output",
                   "project": "invented", "caller_role": "ideator"})
    check("and one that goes through still does",
          r["ok"] and "invented" in projects_made)

    # And the CLI half, driven rather than read: what the bot is told about
    # the caller has to come from the environment the bot set up for the run,
    # not from anything on the command line, or it is a flag again.
    cli_file_task, sent = _cli_file_task_impl()
    env_was = dict(os.environ)
    try:
        os.environ["SILKWORM_THREAD"] = KEY
        os.environ["SILKWORM_ROLE"] = "ideator"
        # It prints its confirmation for the model to read; here that would
        # only interleave with the test output.
        with contextlib.redirect_stdout(io.StringIO()):
            cli_file_task(["--project", "saga", "Cache the résumé parser's output"])
    finally:
        os.environ.clear(); os.environ.update(env_was)
    check("the CLI reports the run's own role to the bot",
          sent and sent[-1][1].get("caller_role") == "ideator",
          f"sent {sent[-1][1] if sent else None}")
    try:
        os.environ["SILKWORM_THREAD"] = KEY
        os.environ["SILKWORM_ROLE"] = "ideator"
        with contextlib.redirect_stdout(io.StringIO()):
            cli_file_task(["--role", "assistant", "Cache the résumé parser's output"])
    finally:
        os.environ.clear(); os.environ.update(env_was)
    check("and the command line cannot talk it down",
          sent[-1][1].get("caller_role") == "ideator"
          and sent[-1][1].get("role") == "assistant",
          "--role names the role of the task being filed, not of the filer")

    check("and the bot is what sets that, per run",
          'env["SILKWORM_ROLE"] = role or "assistant"' in bot)
    ex = bot[bot.index("def execute_task"):bot.index("def resolve_review")]
    check("a task's run is told the role it is running as",
          "role=role_name" in ex,
          "without it every task, ideator included, reports as assistant")

    cli = (BASE / "bin" / "silkworm").read_text()
    check("the CLI refuses outside a turn",
          "only works from inside a Silkworm turn" in cli)
    env = {**os.environ, "SILKWORM_THREAD": "C1:1.0"}
    r = subprocess.run([sys.executable, str(BASE / "bin" / "silkworm"), "task"],
                       capture_output=True, text=True, env=env, timeout=30)
    check("a missing goal is a usage error", r.returncode == 2,
          (r.stdout + r.stderr).strip()[:120])
    check("the subcommand is not read as the goal",
          "silkworm task" not in r.stdout.split("usage:")[-1].split('"')[0]
          or "--project" in r.stdout)


def _close_out_orphans_impl(sess, task_store, task_state):
    """close_out_orphans, loaded from bot.py without importing it."""
    import logging
    import tasks as T
    return _bot_func("close_out_orphans", store=sess, task_store=task_store,
                     tasks=T, task_state=task_state, log=logging.getLogger("test"))


# --- a restart must not move a conversation into a worktree ----------------------
# Two rules met badly. close_out_orphans sets driver="queue" on a Slack message
# that was still queued when the process died, so the runner picks it up rather
# than the message being lost. Isolation was then read off that same flag, so
# "fix what I'm working on" came back in a fresh worktree off the base branch --
# without the user's uncommitted edits -- and committed to silkworm/<task-id>.

def test_isolation_is_not_a_scheduling_decision():
    from tasks import TaskStore
    import tasks as T
    print("\na restart must not move a conversation into a worktree")

    st = TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    sess = tmp_store()
    def task_state(tid, state, detail=""):
        st.transition(tid, state, detail)

    conv = st.create("fix what I'm working on", driver="inline", source="slack",
                     thread="C1:1.0", source_ref="1.0", isolate=False,
                     scope={"cwd": "/repo/yours"})["id"]
    filed = st.create("add the missing test", driver="queue", source="ui",
                      isolate=True, scope={"cwd": "/repo/yours"})["id"]
    check("a conversation is not isolated", not T.isolated(st.get(conv)),
          "a worktree cannot see the edits it is being asked about")
    check("filed work is", T.isolated(st.get(filed)))

    _close_out_orphans_impl(sess, st, task_state)()

    rec = st.get(conv)
    check("a message orphaned by a restart is still handed to the runner",
          rec["driver"] == "queue", "otherwise the message is simply lost")
    check("but it still runs in your checkout, not a worktree",
          not T.isolated(rec),
          "who runs it changed; what kind of work it is did not")
    check("and its directory is untouched", rec["scope"]["cwd"] == "/repo/yours")
    check("filed work is unaffected by the closeout", T.isolated(st.get(filed)))

    # Records written before `isolate` existed keep the answer the old rule
    # gave them -- read at load, before the closeout rewrites the driver they
    # would have been inferred from.
    old = Path(tempfile.mkdtemp()) / "legacy.json"
    old.write_text(json.dumps({
        "tsk_oldconv": {"id": "tsk_oldconv", "goal": "g", "state": "queued",
                        "driver": "inline", "source": "slack", "thread": "C1:2.0",
                        "source_ref": "2.0", "scope": {"cwd": "/repo/yours"}},
        "tsk_oldfiled": {"id": "tsk_oldfiled", "goal": "g", "state": "queued",
                         "driver": "queue", "source": "ui",
                         "scope": {"cwd": "/repo/yours"}},
        # One an earlier restart had already flipped: the driver no longer
        # says what this is, and reading it is the bug being fixed.
        "tsk_rescued": {"id": "tsk_rescued", "goal": "g", "state": "queued",
                        "driver": "queue", "source": "slack", "thread": "C1:4.0",
                        "scope": {"cwd": "/repo/yours"}},
        "tsk_wakeup": {"id": "tsk_wakeup", "goal": "g", "state": "blocked",
                       "driver": "queue", "source": "defer", "thread": "C1:5.0",
                       "scope": {"cwd": "/repo/yours"}},
    }))
    legacy = TaskStore(old)
    check("an existing filed task keeps its checkout",
          T.isolated(legacy.get("tsk_oldfiled")))
    check("a conversation a previous restart already rescued does not gain one",
          not T.isolated(legacy.get("tsk_rescued")),
          "its driver was rewritten before the field existed; its source was not")
    check("nor does a scheduled wake-up", not T.isolated(legacy.get("tsk_wakeup")),
          "it resumes the thread's own session, in the thread's own checkout")
    _close_out_orphans_impl(tmp_store(), legacy, lambda *a, **k: None)()
    check("an existing conversation does not gain one at upgrade",
          legacy.get("tsk_oldconv")["driver"] == "queue"
          and not T.isolated(legacy.get("tsk_oldconv")),
          "the migration must read the driver before the closeout rewrites it")

    # Parking a task for a transient failure rewrites the driver for the same
    # reason the closeout does -- whoever was driving it is gone by the time it
    # retries -- so it must not move the work either.
    import retry as R, logging
    parked = st.create("quota ran out mid-answer", driver="inline", source="slack",
                       thread="C1:3.0", source_ref="3.0", isolate=False,
                       state=T.RUNNING, scope={"cwd": "/repo/yours"})["id"]
    _bot_func("fail_or_retry", task_store=st, retry=R, tasks=T,
              log=logging.getLogger("test"),
              task_state=lambda tid, state, detail="": st.transition(tid, state, detail),
              )(parked, "Claude usage limit reached")
    check("a conversation parked for a retry is handed to the runner too",
          st.get(parked)["driver"] == "queue")
    check("and it is still not isolated when it comes back",
          not T.isolated(st.get(parked)),
          "the same rewrite, the same conversation, the same working tree")

    # And now the thing itself: run the rescued turn and see where it lands.
    ran = _run_a_task(conv_record=st.get(conv))
    check("a rescued conversation runs in the thread's own directory",
          ran["cwd"] == ran["repo"],
          f"ran in {ran['cwd']}, which is not where your uncommitted edits are")
    check("it makes no checkout of its own", ran["worktrees"] == [],
          "a worktree off the base branch holds none of your work")
    check("and no throwaway branch to commit onto", ran["branches"] == [],
          "commits would have landed on silkworm/<a task you never filed>")
    check("nothing records one against it", "worktree" not in ran["scope"])
    check("the thread's home is still yours", ran["home"] == ran["repo"])

    # Not vacuous: the same harness, on work that did ask for a checkout.
    filed_run = _run_a_task(conv_record=None)
    check("filed work in the same repo does get its own checkout",
          filed_run["cwd"] != filed_run["repo"] and filed_run["worktrees"],
          "if nothing is ever isolated here, the check above proves nothing")
    check("on a branch of its own", filed_run["branches"] != [])
    check("and the thread's home is still not the checkout it used",
          filed_run["home"] == filed_run["repo"],
          "a released worktree left as a thread's cwd breaks it permanently")

    # The decision is on the record. Asserted on the syntax rather than the
    # text, so `driver` reappearing in an unrelated line nearby cannot pass.
    ex = next(n for n in ast.parse((BASE / "bot.py").read_text()).body
              if isinstance(n, ast.FunctionDef) and n.name == "execute_task")
    def makes_a_worktree(node):
        # Its own body, not the whole subtree: an enclosing `if` whose *else*
        # creates the worktree would otherwise answer for this, and the test it
        # is asked about would be the enclosing one's.
        return any(isinstance(c, ast.Attribute) and c.attr == "create"
                   and getattr(c.value, "id", "") == "worktrees"
                   for stmt in node.body for c in ast.walk(stmt))
    branch = next(n for n in ast.walk(ex)
                  if isinstance(n, ast.If) and makes_a_worktree(n))
    check("isolation is read from the record, not from who is driving",
          "attr='isolated'" in ast.dump(branch.test)
          and "'driver'" not in ast.dump(branch.test),
          "driver is a scheduling flag; a restart rewrites it")

    # Every way a task comes into being answers it, rather than falling
    # through to the default. The default has to be *something*, and whichever
    # way it points it is right for half the callers by accident -- which is
    # how one flag came to answer two questions in the first place.
    for mod in ("bot.py", "email_ingest.py"):
        for made in (c for c in ast.walk(ast.parse((BASE / mod).read_text()))
                     if isinstance(c, ast.Call)
                     and getattr(c.func, "attr", "") == "create"
                     and getattr(c.func.value, "id", "") == "task_store"):
            check(f"{mod}:{made.lineno} says where its task may run",
                  any(k.arg == "isolate" for k in made.keywords),
                  "a new way of filing work must not inherit the answer")

    prompt = next(n for n in ast.parse((BASE / "bot.py").read_text()).body
                  if isinstance(n, ast.FunctionDef) and n.name == "handle_prompt")
    made = next(c for c in ast.walk(prompt) if isinstance(c, ast.Call)
                and getattr(c.func, "attr", "") == "create"
                and getattr(c.func.value, "id", "") == "task_store")
    says = {k.arg: getattr(k.value, "value", None) for k in made.keywords}
    check("a conversation records the decision when it is created",
          says.get("isolate") is False,
          "left to the default it would be right by luck, not by decision")


def _run_a_task(conv_record):
    """Drive execute_task over a real repo. Returns where the turn happened.

    `conv_record` is the rescued conversation to run; None runs a filed task
    instead, so the same harness shows both answers and neither check can pass
    by the harness simply never isolating anything.
    """
    import shutil, threading, logging
    import tasks as T, roles, worktrees as W
    from tasks import TaskStore

    root = Path(tempfile.mkdtemp())
    W.ROOT = root / "wts"
    repo = root / "repo"; repo.mkdir()
    def git(*a): return subprocess.run(["git", *a], cwd=str(repo),
                                       capture_output=True, text=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (repo / "mine.txt").write_text("what I am working on\n")
    git("add", "-A"); git("commit", "-qm", "base")

    st = TaskStore(root / "t.json")
    if conv_record:
        # The record exactly as the closeout left it, pointed at this repo.
        rec = st.create(conv_record["goal"], thread="C1:1.0",
                        **{f: conv_record[f] for f in
                           ("driver", "source", "source_ref", "isolate")},
                        scope={"cwd": str(repo)})
    else:
        rec = st.create("add the missing test", driver="queue", source="ui",
                        isolate=True, thread="C1:1.0",
                        scope={"cwd": str(repo)})
    st.transition(rec["id"], T.RUNNING, "claimed by the runner")
    task = st.get(rec["id"])

    sess = tmp_store()
    seen = {}
    def run_turn(goal, **kw):
        # Snapshot while the turn is happening: execute_task releases its
        # checkout before it returns, so looking afterwards finds nothing
        # either way and would pass whatever the answer had been.
        seen["cwd"] = Path(kw["cwd"])
        seen["worktrees"] = sorted(d.name for d in W.ROOT.iterdir()) \
            if W.ROOT.exists() else []
        seen["branches"] = [b for b in git("branch", "--format=%(refname:short)")
                            .stdout.split() if b != "main"]
        return types.SimpleNamespace(text="done", cost_usd=0.0, duration_ms=1,
                                     session_id="sess")
    execute_task = _bot_func(
        "execute_task", tasks=T, task_store=st, store=sess, roles=roles,
        review_branch=lift("review_branch",
                                    task_store=st)["review_branch"],
        worktrees=W, Path=Path, run_turn=run_turn, shutil=shutil,
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
    )
    execute_task(task)

    return {"cwd": seen.get("cwd"), "repo": repo,
            "worktrees": seen.get("worktrees"), "branches": seen.get("branches"),
            "scope": (st.get(rec["id"]) or {}).get("scope") or {},
            "home": Path((sess.get("C1:1.0") or {}).get("cwd", ""))}


# --- a queued task must not work in your checkout --------------------------------
# Turns sharing a repo were serialised, which was right but blunt: a queued task
# also ran in the tree you edit, so it could leave it dirty or on another
# branch. A worktree gives it somewhere private and decouples the two.

def test_worktrees():
    import worktrees as W
    print("\nqueued tasks get their own checkout")

    root = Path(tempfile.mkdtemp())
    W.ROOT = root / "worktrees"
    repo = root / "repo"; repo.mkdir()
    def git(cwd, *a): return subprocess.run(["git", *a], cwd=str(cwd),
                                            capture_output=True, text=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("hello\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "base")

    check("a non-repo gets no worktree", W.create(root, "tsk_x", fetch=False) is None,
          "the shared scratch directory is not a checkout")

    wt = W.create(repo, "tsk_abc", fetch=False)
    check("a repo-backed task gets one", wt is not None and wt.exists())
    check("on its own branch", W.branch_of(wt) == "silkworm/tsk_abc")
    check("it lives outside the repository", repo not in wt.parents,
          "inside, a task would scan or commit it by accident")
    check("your checkout is untouched",
          sorted(x.name for x in repo.iterdir() if x.name != ".git") == ["a.txt"])

    (wt / "b.txt").write_text("task work\n")
    git(wt, "add", "-A"); git(wt, "commit", "-qm", "work")
    check("your checkout is still untouched after the task commits",
          sorted(x.name for x in repo.iterdir() if x.name != ".git") == ["a.txt"],
          "this is the whole point, not a side effect")

    check("commits are counted without being told the base",
          W.commits_on(wt) == 1,
          "origin/HEAD is unset in many repos, and rev-list against a ref that "
          "does not exist fails silently into 'the task did nothing'")

    removed, note = W.release(wt)
    check("a finished task's worktree is removed", removed and not wt.exists())
    check("and the commit count is reported, not guessed",
          "1 commit" in note, f"got {note!r} — origin/HEAD is unset in many repos")
    check("the branch survives for review",
          bool(git(repo, "branch", "--list", "silkworm/tsk_abc").stdout.strip()))

    dirty = W.create(repo, "tsk_dirty", fetch=False)
    (dirty / "wip.txt").write_text("half done\n")
    removed, note = W.release(dirty)
    check("uncommitted work is never discarded",
          not removed and dirty.exists() and "uncommitted" in note,
          "a task's unfinished work is still work")

    orphan = W.create(repo, "tsk_orphan", fetch=False)
    check("a worktree is not swept the moment it appears",
          W.sweep(keep=set()) == 0 and orphan.exists(),
          "the sweep runs on a guess; an hour of patience costs a little disk, "
          "and guessing wrong costs somebody's uncommitted afternoon")
    swept = W.sweep(keep=set(), min_age_s=0)
    check("orphans left by a restart are swept", swept == 1 and not orphan.exists())
    check("but never a dirty one", dirty.exists(),
          "sweeping is tidying, not deleting someone's work")
    live = W.create(repo, "tsk_live", fetch=False)
    W.sweep(keep={"tsk_live"}, min_age_s=0)
    check("a running task's worktree is left alone", live.exists())

    # An agent that has just committed and is running a long test suite has a
    # clean tree for minutes at a time -- so the dirty check does not save it,
    # and losing the branch as well as the checkout loses the work outright.
    committed = W.create(repo, "tsk_swept", fetch=False)
    (committed / "c.txt").write_text("real work\n")
    git(committed, "add", "-A"); git(committed, "commit", "-qm", "real work")
    empty = W.create(repo, "tsk_empty", fetch=False)
    W.sweep(keep=set(), min_age_s=0)
    check("the sweep never deletes a branch it is only guessing about",
          bool(git(repo, "branch", "--list", "silkworm/tsk_swept").stdout.strip()),
          "the checkout is recoverable from the branch; the branch is not "
          "recoverable from anything")
    check("not even an apparently empty one",
          bool(git(repo, "branch", "--list", "silkworm/tsk_empty").stdout.strip()),
          "`git branch -D` is unrecoverable, and the sweep parsed a directory name")
    W.release(W.create(repo, "tsk_tidy", fetch=False))
    check("but the task's own executor still tidies its empty branch away",
          not git(repo, "branch", "--list", "silkworm/tsk_tidy").stdout.strip(),
          "release() knows the run is over; the sweep only thinks it might be")

    bot = (BASE / "bot.py").read_text()
    ex = bot[bot.index("def execute_task"):bot.index("def resolve_review")]
    # A turn running in a worktree wrote that path back as the *thread's* cwd.
    # The worktree is released when the turn ends, so every later message in
    # that thread failed with "working directory no longer exists" -- for good,
    # and with nothing pointing at the task that caused it. Two real trader
    # conversations broke this way.
    check("a turn never leaves its worktree as the thread's home",
          "cwd=str(home_cwd)" in ex and "home_cwd = cwd" in ex,
          "the worktree outlives nothing; the thread outlives everything")
    check("and the run still happens in the worktree", "cwd = worktree" in ex)

    check("only work that asked to be isolated is",
          'tasks.isolated(task) and worktrees.is_repo(cwd)' in ex,
          "a conversation must stay where your uncommitted edits are")
    check("a failed task still releases its checkout",
          "could not release worktree" in ex, "otherwise every failure leaks one")
    check("the branch is named in the reply", "isolated checkout" in ex.lower())
    check("orphans are swept periodically",
          'daemons.start(_worktree_sweeper, "wtsweep")' in bot)


# --- cancelling must stop the work, not just relabel it --------------------------
# A Saga implementor was cancelled 71 seconds after the runner claimed it. The
# record said cancelled; the agent carried on working and spending in a
# checkout that nothing now owned, and the next sweep deleted that checkout --
# branch and all -- out from under it. It lost its first pass of the work.

class FakeHandle:
    """Stands in for a RunHandle, and remembers when it was stopped."""

    def __init__(self, on_stop=None):
        self.stopped = 0
        self._on_stop = on_stop

    def stop(self):
        self.stopped += 1
        if self._on_stop:
            self._on_stop()


def test_cancel_stops_the_child():
    import tasks as T
    from tasks import TaskStore
    print("\ncancelling a running task stops it")

    st = TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    running_tasks = {}
    import holding as H
    ns = lift("stop_task", "handle_tasks", "live_worktree_tasks",
                       tasks=T, task_store=st, RUNNING_TASKS=running_tasks,
                       holding=H,
              absent=TASK_ROUTES + STEP_ROUTES)
    tasks_route, stop_task = ns["handle_tasks"], ns["stop_task"]

    def running_task(**fields):
        t = st.create("do a thing", driver="queue", **fields)
        st.transition(t["id"], T.RUNNING)
        return t["id"]

    # What the record said at the moment the child was told to stop. If the
    # transition happens first, the agent is working on a task the board has
    # already written off -- which is the whole bug.
    seen = []
    tid = running_task()
    running_tasks[tid] = FakeHandle(lambda: seen.append(st.get(tid)["state"]))
    r = tasks_route({"action": "cancel", "id": tid})

    check("cancelling a running task kills its child",
          running_tasks[tid].stopped == 1,
          "the record said cancelled while the agent kept working and spending")
    check("it is stopped before it is written off", seen == [T.RUNNING],
          f"stopped while the record said {seen}")
    check("and the task ends up cancelled", r["ok"] and st.get(tid)["state"] == T.CANCELLED)
    check("the reply says the work was stopped", r.get("note") == "stopped")
    check("the event log records it too",
          "stopped" in st.get(tid)["events"][-1]["detail"],
          "otherwise nothing distinguishes a real stop from a relabelling")

    dis = running_task()
    running_tasks[dis] = FakeHandle()
    tasks_route({"action": "dismiss", "id": dis})
    check("dismiss is the same door and stops it too",
          running_tasks[dis].stopped == 1 and st.get(dis)["state"] == T.CANCELLED,
          "the dashboard offers both, and they differ only in wording")

    orphan = running_task()
    r = tasks_route({"action": "cancel", "id": orphan})
    check("a running task with no child of ours can still be cancelled",
          r["ok"] and st.get(orphan)["state"] == T.CANCELLED,
          "a restart leaves records running with nobody running them; refusing "
          "would make those uncancellable")
    check("and the reply is honest that nothing was stopped",
          "orphaned" in (r.get("note") or ""), r.get("note"))

    q = st.create("not started", driver="queue")
    r = tasks_route({"action": "cancel", "id": q["id"]})
    check("cancelling queued work stops nothing and says nothing",
          r["ok"] and not r.get("note") and st.get(q["id"])["state"] == T.CANCELLED)
    check("stopping a task that is not running is a no-op",
          stop_task("tsk_nothing") is False)

    # The registry the whole mechanism reads from. RUNNING is keyed by thread,
    # which cannot answer "is *this task* still going".
    src = (BASE / "bot.py").read_text()
    tree = ast.parse(src)
    for fname in ("handle_prompt", "execute_task"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == fname)
        body = ast.dump(fn)
        check(f"{fname} registers its child by task id", "RUNNING_TASKS" in body)
        final = [x for t in ast.walk(fn) if isinstance(t, ast.Try) for x in t.finalbody]
        fin = ast.dump(ast.Module(body=final, type_ignores=[]))
        check(f"{fname} lets it go in finally",
              "RUNNING_TASKS" in fin or "id='release_turn'" in fin,
              "a handle left behind would keep a finished task's checkout forever")
    rel = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "release_turn")
    check("release_turn lets go of the task's handle", "RUNNING_TASKS" in ast.dump(rel))


# --- a checkout must outlive its task leaving RUNNING ----------------------------
# The sweeper kept only `running` tasks, so a task became sweepable the instant
# anything happened to it -- a cancel, a failure, a review gate -- while its
# checkout was still the only place its work existed.

def test_worktree_survives_leaving_running():
    import tasks as T
    import worktrees as W
    from tasks import TaskStore
    print("\na checkout outlives its task leaving running")

    st = TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    running_tasks = {}
    keep = lift("live_worktree_tasks", tasks=T, task_store=st,
                         RUNNING_TASKS=running_tasks)["live_worktree_tasks"]

    def task_in(state):
        t = st.create("work", driver="queue")
        for step in {T.RUNNING: [T.RUNNING],
                     T.FAILED: [T.RUNNING, T.FAILED],
                     T.AWAITING_APPROVAL: [T.RUNNING, T.AWAITING_APPROVAL],
                     T.BLOCKED: [T.RUNNING, T.BLOCKED],
                     T.NEEDS_INPUT: [T.RUNNING, T.NEEDS_INPUT],
                     T.DONE: [T.RUNNING, T.DONE],
                     T.CANCELLED: [T.CANCELLED],
                     T.QUEUED: []}[state]:
            st.transition(t["id"], step)
        return t["id"]

    held = {state: task_in(state) for state in T.STATES if state != T.PROPOSED}
    kept = keep()
    for state, tid in held.items():
        if state in T.TERMINAL:
            check(f"a {state} task's checkout is litter", tid not in kept)
        else:
            check(f"a {state} task keeps its checkout", tid in kept,
                  "its work may exist nowhere else")

    # The cancel window: the record goes terminal at once, the child takes a
    # few seconds to die, and the executor releases the checkout on its way
    # out. Until it does, the live handle is what holds the sweep off.
    dying = held[T.CANCELLED]
    running_tasks[dying] = FakeHandle()
    check("a child still in there keeps its checkout even once written off",
          dying in keep(),
          "otherwise the sweep races the agent it just told to stop")

    # End to end, against real git.
    root = Path(tempfile.mkdtemp())
    W.ROOT = root / "worktrees"
    repo = root / "repo"; repo.mkdir()
    def git(cwd, *a): return subprocess.run(["git", *a], cwd=str(cwd),
                                            capture_output=True, text=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("base\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "base")

    trees = {state: W.create(repo, tid, fetch=False) for state, tid in held.items()}
    # Committed, and mid-test-run: a clean tree, which is exactly when the
    # dirty check saves nothing.
    for state in (T.CANCELLED, T.FAILED, T.AWAITING_APPROVAL):
        (trees[state] / "work.txt").write_text("hours of it\n")
        git(trees[state], "add", "-A"); git(trees[state], "commit", "-qm", "work")
    W.sweep(keep=keep(), min_age_s=0)

    check("a cancelled task still being stopped keeps its work",
          trees[T.CANCELLED].exists(),
          "this is the failure exactly: cancelled, swept, work gone")
    check("a failed task keeps its checkout to be looked at",
          trees[T.FAILED].exists())
    check("work waiting on its review keeps its checkout",
          trees[T.AWAITING_APPROVAL].exists(),
          "the review gate is not the end of the task")
    check("and a finished one is still tidied away",
          not trees[T.DONE].exists(),
          "the sweep must still do its job, or orphans pile up invisibly")


# --- labelled mail is a fact about a project, not a task ---------------------
# A booking confirmation needs nothing from you. Putting it on the board would
# mean clicking to dismiss something true; it belongs in the project's files.

def test_mail_facts():
    import types
    import email_ingest as E
    import projects
    print("\nlabelled mail becomes project facts")

    src = (BASE / "email_ingest.py").read_text()
    check("labelled mail is read whether or not it is unread",
          "(ALL)" in src and "UNSEEN" not in src[src.index("def fetch_label"):],
          "you label mail you have already opened")
    check("label mailboxes are quoted for IMAP", "_mbox(label)" in src,
          "an unquoted 'Asia Trip' selects nothing")
    check("labelled mail still never marks anything read",
          "readonly=True" in src[src.index("def fetch_label"):])

    root = Path(tempfile.mkdtemp())
    projects.PROJECT_ROOT = root
    ps = projects.ProjectStore(root / "p.json")
    ps.ensure("Asia Trip")
    ps.ensure("Trader", scope={"cwd": "/w/trader", "repo": "github.com/x/trader"})
    ps.ensure("Old Trip"); ps.set_archived("old-trip", True)

    check("a project's label defaults to its title, needing no setup",
          ps.label_for("asia-trip") == "Asia Trip")
    ps.ensure("Asia Trip", mail_label="Travel/Asia")
    check("a differently-named label can be pointed at",
          ps.label_for("asia-trip") == "Travel/Asia")
    targets = {s for s, _, _ in ps.mail_targets()}
    check("repo-backed projects are not mail targets", "trader" not in targets,
          "their files are the user's; we do not write there")
    check("archived projects are not mail targets", "old-trip" not in targets)

    mail = [{"uid": 7, "id": "f1", "from": "Air Canada", "subject": "Your itinerary",
             "date": "Tue, 3 Feb 2026", "snippet": "AC123 YYZ-NRT ...",
             "attachments": [("boarding pass.pdf", b"%PDF-1.4 stub")]},
            {"uid": 8, "id": "f2", "from": "Mum", "subject": "have fun!",
             "date": "", "snippet": "so excited for you", "attachments": []}]
    fetch = lambda h, u, p, label, since, lim: (         # noqa: E731
        [m for m in mail if m["uid"] > since][:lim],
        max([m["uid"] for m in mail] + [since]))

    def stub(out):
        E.subprocess = types.SimpleNamespace(
            run=lambda *a, **k: types.SimpleNamespace(stdout=out))

    for name, out in (("no json object", "seems like a flight"),
                      ("malformed json", "{oops}"),
                      ("no body", '{"skip": false, "title": "x", "body": ""}')):
        stub(out)
        check(f"extraction fails closed on {name}",
              E.extract(mail[0], binary="c", model="m", env={}) is None,
              "a garbled entry in a reference file is worse than a missing one")

    stub('{"skip": true}')
    check("ordinary correspondence files nothing",
          E.extract(mail[1], binary="c", model="m", env={}) is None)

    stub('{"skip": false, "date": "2026-04-14", "title": "Flight AC123 YYZ-NRT",'
         ' "body": "- Depart 09:15\\n- Confirmation XR4K2P"}')
    state = {}
    r = E.ingest_facts(ps, state, host="h", user="u", password="p", fetch=fetch)
    check("facts are filed against the labelled project",
          r["filed"] == 2 and all(f["project"] == "asia-trip" for f in r["facts"]))

    log = (ps.home("asia-trip") / projects.LOGISTICS).read_text()
    check("the fact lands in logistics.md, not the brief",
          "Confirmation XR4K2P" in log)
    check("it is dated by when it happens", "## 2026-04-14" in log)
    check("the source is recorded", "Air Canada" in log)
    att = ps.home("asia-trip") / E.ATTACH_DIR / "7-boarding-pass.pdf"
    check("attachments are saved into the project", att.exists())
    check("and referenced from the entry", "7-boarding-pass.pdf" in log)

    brief = (ps.home("asia-trip") / "CLAUDE.md").read_text()
    check("CLAUDE.md points at the logistics file", projects.POINTER in brief)
    ps.set_brief("asia-trip", "Kyoto over Osaka.")
    brief = (ps.home("asia-trip") / "CLAUDE.md").read_text()
    check("a brief rewrite cannot lose the pointer",
          projects.POINTER in brief and "Kyoto over Osaka." in brief,
          "the brief is rewritten wholesale after every task")
    check("the pointer is not fed back into the next rewrite",
          projects.POINTER not in ps.brief_for("asia-trip"))

    r2 = E.ingest_facts(ps, state, host="h", user="u", password="p", fetch=fetch)
    check("a second pass files nothing twice", r2["filed"] == 0)

    bot = (BASE / "bot.py").read_text()
    check("labelled mail files facts, never tasks",
          "ingest_facts(\n            project_store" in bot
          or "ingest_facts(" in bot and "task_store, state, mailbox=" in bot)
    check("inbox triage is opt-in", 'os.environ.get("GMAIL_TRIAGE"' in bot,
          "you already read your inbox; re-surfacing it duplicates your own work")


# --- every state you can be in must have a way out of it ----------------------
# `running` offered nothing but Thread. handle_tasks() has had a branch that
# kills the child of a running task since the day cancelling one only
# relabelled the record -- the agent carried on working and spending in an
# isolated checkout the sweeper was then free to delete underneath it, which
# cost one task its first pass of uncommitted work. Nothing in the dashboard
# reached it: the only way in was to open the anchor thread and type !stop, and
# the dashboard is the surface that lists running tasks.
#
# Tying the button set to TRANSITIONS rather than to a list of states is what
# stops a state added later from landing with no way out of it.

def test_every_open_state_has_a_button():
    import tasks as T
    sys.argv = ["x"]
    import visualizer as V
    print("\nevery non-terminal state offers a permitted action")

    tree = ast.parse((BASE / "bot.py").read_text())
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    def moves_to(node):
        """The state the first task_store.transition() under `node` moves to."""
        for n in ast.walk(node):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "transition" and len(n.args) >= 2
                    and isinstance(n.args[1], ast.Attribute)):
                return getattr(T, n.args[1].attr, None)
        return None

    # accept/dismiss/retry/cancel share one literal mapping in handle_tasks.
    # Read it out of the source rather than restating it here: if `cancel` stops
    # meaning CANCELLED, this test should follow bot.py rather than quietly
    # disagree with it and pass anyway.
    targets = {}
    for n in ast.walk(fns["handle_tasks"]):
        if (isinstance(n, ast.Dict) and n.keys
                and all(isinstance(k, ast.Constant) and isinstance(k.value, str)
                        for k in n.keys)
                and all(isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name)
                        and v.value.id == "tasks" for v in n.values)
                and "cancel" in [k.value for k in n.keys]):
            targets = {k.value: getattr(T, v.attr, None)
                       for k, v in zip(n.keys, n.values)}
            break
    check("the action map was read out of handle_tasks",
          set(targets) == {"accept", "retry", "dismiss", "cancel"},
          f"got {sorted(targets)} — the rest of this test rests on it")

    # `approve` and `rework` are their own code paths, not entries in that dict.
    targets["approve"] = moves_to(fns["approve_task"])
    for n in ast.walk(fns["handle_tasks"]):
        if (isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
                and isinstance(n.test.comparators[0], ast.Constant)
                and n.test.comparators[0].value == "rework"):
            # It sends work back through the shared mechanism, which is
            # where the transition lives.
            calls = {c.func.id for c in ast.walk(n) if isinstance(c, ast.Call)
                     and isinstance(c.func, ast.Name)}
            targets["rework"] = moves_to(n) or (
                moves_to(fns["send_back"]) if "send_back" in calls else None)
    check("and so were approve and rework",
          targets.get("approve") is T.DONE and targets.get("rework") is T.QUEUED,
          f"approve={targets.get('approve')} rework={targets.get('rework')}")

    js = _re.search(r"<script>(.*?)</script>", V.PAGE, _re.S).group(1)
    fn = js[js.index("function taskButtons(t)"):js.index("async function renderTasks()")]

    def action_of(handler):
        """The backend action an onclick reaches, or None if it is not one.

        Either taskAction(id, "<action>") directly, or a wrapper that confirms
        or prompts first and then calls it. Resolved by what the wrapper calls
        rather than by name, so renaming Stop or Send back cannot detach a
        button from the action it stands for without this noticing.
        """
        m = _re.match(r"""taskAction\('[^']*',\s*['"]([a-z_]+)['"]\)""", handler)
        if m:
            return m.group(1)
        name = handler.split("(")[0]
        i = js.find(f"function {name}(")
        if i < 0:
            return None
        end = js.find("\n}", i)
        if end < 0:
            return None                 # not a top-level function; say so, do not raise
        body = js[i:end]
        m = _re.search(r"""taskAction\([^,]+,\s*['"]([a-z_]+)['"]""", body)
        return m.group(1) if m else None

    # esc() comes along because every other render function in the page uses it,
    # and the harness splices only taskButtons: the day this function escapes
    # anything, an un-prefixed harness would throw ReferenceError instead.
    esc = _re.search(r"const esc = .*", js).group(0)
    harness = (esc + "\n" + fn + "\nconst out = {};\n"
               + "for (const s of JSON.parse(process.env.STATES)) "
                 'out[s] = taskButtons({id: "tsk_1", state: s, thread: "T1"});\n'
               + "process.stdout.write(JSON.stringify(out));\n")
    run, rendered = None, None
    try:
        run = subprocess.run(["node", "-e", harness], capture_output=True, text=True,
                             timeout=30,
                             env={**os.environ, "STATES": json.dumps(list(T.STATES))})
    except (OSError, subprocess.SubprocessError):
        pass                            # no node on this machine; checked below
    if run is not None:
        # A node that ran and failed is not a node that is missing. Collapsing
        # the two is how this test quietly became nine substring checks: make
        # taskButtons call a helper the harness has not spliced in and it exits
        # 1, which read as "node unavailable" and passed green.
        try:
            rendered = json.loads(run.stdout)
        except ValueError:
            rendered = None
        if rendered is None:
            # node's last stderr line is its own version banner, so name the
            # line that actually says what went wrong.
            lines = [x.strip() for x in (run.stderr or "").splitlines() if x.strip()]
            why = next((x for x in lines if "Error" in x),
                       lines[0] if lines else f"node exited {run.returncode}, no output")
            check("taskButtons() runs without throwing", False, why)
            return

    if rendered is None:
        # No node here. The weaker claim still catches the failure this was
        # written for: a non-terminal state the function does not mention at all
        # falls through to Thread and nothing else.
        print("    (node unavailable — taskButtons() checked by contract only)")
        for state in T.STATES:
            if state not in T.TERMINAL:
                check(f"taskButtons() mentions {state}", f'"{state}"' in fn)
        return

    # A stub -- or a function that returned before reaching its branches --
    # would satisfy every terminal-state check below. The Thread button is proof
    # the fixture reached the end of the function; the outputs differing between
    # states is proof it took more than one branch on the way. "<button" alone
    # was not: Thread supplies one for every state, terminal ones included.
    check("the harness really ran the shipped function",
          set(rendered) == set(T.STATES)
          and all("jumpTo(" in h for h in rendered.values())
          and len(set(rendered.values())) > 1,
          f"{len(set(rendered.values()))} distinct outputs for {len(rendered)} states")

    for state in T.STATES:
        acts = [a for a in (action_of(h) for h
                            in _re.findall(r'onclick="([^"]+)"', rendered[state])) if a]
        if state in T.TERMINAL:
            # Offering one would be a button the state machine refuses, which
            # the dashboard renders as a bare "Not allowed".
            check(f"{state} is terminal and offers no action", not acts, f"offers {acts}")
            continue
        check(f"{state} offers an action, not just Thread", bool(acts),
              "a state with no button is only reachable by typing !stop at it")
        refused = [a for a in acts if not T.can(state, targets.get(a))]
        check(f"and every action {state} offers is one TRANSITIONS permits",
              not refused, f"offers {refused}, which transition() would refuse")

    # The one this was written for, said plainly rather than left to the sweep.
    running = [a for a in (action_of(h) for h
                           in _re.findall(r'onclick="([^"]+)"', rendered[T.RUNNING])) if a]
    check("a running agent can be stopped from the board",
          T.CANCELLED in [targets.get(a) for a in running],
          "handle_tasks kills the child; until now nothing in the UI reached it")
    check("and it is not labelled the same as dropping a queue entry",
          "Stop" in rendered[T.RUNNING] and "Cancel" in rendered[T.QUEUED]
          and "Cancel" not in rendered[T.RUNNING],
          "killing a live session and dropping an unstarted one are not one act")
    # The note is the only part of the outcome the board cannot show: whether
    # the child was really killed, or was orphaned by a restart and is now
    # reap_runaways' problem.
    act = _re.search(r"async function taskAction\(.*?^}", js, _re.S | _re.M).group(0)
    check("and the reply's note reaches the user", "r.note" in act,
          "handle_tasks returns it and nothing rendered it")


# --- a task must not be stranded by its blocker ---------------------------------
# An implementor waits in `blocked` for its reviewer. Only the reviewer
# finishing normally ever moved it on, so a reviewer that errored, was stopped,
# or was dismissed from the dashboard left the parent in a state that is not in
# NEEDS_ATTENTION -- invisible for ever, with its finished work on a branch.

def test_blocked_tasks_are_never_stranded():
    import tasks as T
    from tasks import TaskStore
    print("\nnothing waits on a task that has stopped")
    st = TaskStore(Path(tempfile.mkdtemp()) / "t.json")

    def blocked_pair(**parent_fields):
        """An implementor parked on its reviewer, exactly as resolve_review leaves it."""
        parent = st.create("implement the thing", driver="queue", **parent_fields)
        st.transition(parent["id"], T.RUNNING)
        reviewer = st.create("review it", role="reviewer", driver="queue",
                             parent=parent["id"], source="review")
        st.update(parent["id"], blocked_on=[reviewer["id"]])
        st.transition(parent["id"], T.BLOCKED, f"awaiting review {reviewer['id']}")
        st.transition(reviewer["id"], T.RUNNING)
        return parent["id"], reviewer["id"]

    # 1. the reviewer dies on a ClaudeError
    pid, rid = blocked_pair(result={"text": "did the thing"})
    st.transition(rid, T.FAILED, "Connection closed mid-response")
    parent = st.get(pid)
    check("a failed reviewer does not strand its parent",
          parent["state"] == T.AWAITING_APPROVAL, parent["state"])
    check("the parent is back in the what-needs-me view",
          parent["state"] in T.NEEDS_ATTENTION)
    last = parent["events"][-1]["detail"]
    check("the detail names what happened to the reviewer",
          rid in last and "failed" in last, last)
    check("the dead reviewer is dropped from blocked_on", parent["blocked_on"] == [],
          "leaving it there makes a rerun skip both verification and review")

    # 2. the user stops the reviewer: it lands in cancelled
    pid, rid = blocked_pair(result={"text": "did the thing"})
    st.transition(rid, T.CANCELLED, "stopped by the user")
    parent = st.get(pid)
    check("a cancelled reviewer does not strand its parent",
          parent["state"] == T.AWAITING_APPROVAL, parent["state"])
    check("the detail says it was cancelled",
          "cancelled" in parent["events"][-1]["detail"])

    # 3. the reviewer is dismissed from the dashboard (queued -> cancelled)
    parent = st.create("implement", driver="queue", result={"text": "done"})
    st.transition(parent["id"], T.RUNNING)
    reviewer = st.create("review", role="reviewer", driver="queue", parent=parent["id"])
    st.update(parent["id"], blocked_on=[reviewer["id"]])
    st.transition(parent["id"], T.BLOCKED)
    st.transition(reviewer["id"], T.CANCELLED, "dismiss via ui")
    check("a dismissed reviewer does not strand its parent",
          st.get(parent["id"])["state"] == T.AWAITING_APPROVAL)

    # A task with nothing to show has not earned an approval prompt, but it
    # must still say so somewhere the user looks.
    pid, rid = blocked_pair()
    st.transition(rid, T.FAILED, "gave up")
    check("a parent with no work to show fails rather than asking for approval",
          st.get(pid)["state"] == T.FAILED)

    # The normal path must be untouched: the reviewer moves its parent on
    # before finishing, and the parent keeps the reviewer id that stops it
    # being reviewed a second time.
    pid, rid = blocked_pair(result={"text": "did the thing"})
    st.transition(pid, T.DONE, "review passed")
    st.transition(rid, T.DONE)
    check("a passed review still completes the parent", st.get(pid)["state"] == T.DONE)
    check("a settled parent keeps the reviewer in blocked_on",
          st.get(pid)["blocked_on"] == [rid],
          "clearing it would let a rerun skip the gate")

    # Waiting on two things: one ending is not the same as being free.
    parent = st.create("implement", driver="queue", result={"text": "done"})
    st.transition(parent["id"], T.RUNNING)
    a, b = st.create("a", driver="queue"), st.create("b", driver="queue")
    st.update(parent["id"], blocked_on=[a["id"], b["id"]])
    st.transition(parent["id"], T.BLOCKED)
    st.transition(a["id"], T.RUNNING)
    st.transition(a["id"], T.CANCELLED)
    check("one blocker of two ending leaves the task blocked",
          st.get(parent["id"])["state"] == T.BLOCKED)
    check("the ended blocker is still dropped",
          st.get(parent["id"])["blocked_on"] == [b["id"]])
    st.transition(b["id"], T.RUNNING)
    st.transition(b["id"], T.DONE)
    check("the last blocker ending releases it",
          st.get(parent["id"])["state"] == T.AWAITING_APPROVAL)

    # A restart is not a stranding: the reviewer is requeued in the same breath.
    pid, rid = blocked_pair(result={"text": "done"})
    st.requeue_interrupted()
    check("a restart requeues the reviewer", st.get(rid)["state"] == T.QUEUED)
    check("a requeued reviewer does not release its parent",
          st.get(pid)["state"] == T.BLOCKED, st.get(pid)["state"])

    # The audit: anything stranded before the rule existed, or by a crash
    # between the blocker ending and the release, must still surface.
    st2 = TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    orphan = st2.create("implement", driver="queue", result={"text": "done"})
    rev = st2.create("review", role="reviewer", driver="queue", parent=orphan["id"])
    st2.transition(rev["id"], T.CANCELLED)
    st2.transition(orphan["id"], T.RUNNING)
    st2.update(orphan["id"], blocked_on=[rev["id"]])      # blocked after the fact
    st2.transition(orphan["id"], T.BLOCKED)
    check("the audit finds a task already stranded",
          st2.release_stranded() == [orphan["id"]])
    check("the audit puts it in front of the user",
          st2.get(orphan["id"])["state"] == T.AWAITING_APPROVAL)
    check("a second pass has nothing to do", st2.release_stranded() == [])

    ghost = st2.create("implement", driver="queue")
    st2.transition(ghost["id"], T.RUNNING)
    st2.update(ghost["id"], blocked_on=["tsk_doesnotexist"])
    st2.transition(ghost["id"], T.BLOCKED)
    st2.release_stranded()
    check("blocked on a task that does not exist is also stranded",
          st2.get(ghost["id"])["state"] == T.FAILED)

    check("releases survive a reload",
          TaskStore(st2._path).get(orphan["id"])["state"] == T.AWAITING_APPROVAL)

    # A release that does not finish must leave the task exactly as it was.
    # Persisting the dropped blocker before the state change meant a crash in
    # between left a task `blocked` with an empty `blocked_on` -- the one shape
    # the audit cannot find, which is the original bug wearing a new hat. Run
    # with every write in the release failing in turn, because which write is
    # the dangerous one is exactly what a refactor gets wrong.
    # From the second write on: the first is the blocker's own transition, and
    # that failing is its caller's problem -- nothing has been released yet and
    # the waiter is still legitimately waiting.
    stranded_by_a_crash, escaped, recovered = [], [], []
    for nth in range(2, 6):
        st3 = TaskStore(Path(tempfile.mkdtemp()) / "t.json")
        pid = st3.create("implement", driver="queue", result={"text": "done"})["id"]
        rid = st3.create("review", role="reviewer", driver="queue", parent=pid)["id"]
        st3.transition(pid, T.RUNNING)
        st3.update(pid, blocked_on=[rid])
        st3.transition(pid, T.BLOCKED)
        st3.transition(rid, T.RUNNING)
        real_save, writes = st3._save, []

        def flaky_save(_real=real_save, _writes=writes, _nth=nth):
            _writes.append(1)
            if len(_writes) == _nth:
                raise OSError("disk went away")
            _real()

        st3._save = flaky_save
        try:
            st3.transition(rid, T.FAILED, "Connection closed mid-response")
        except Exception as exc:              # the release must swallow its own
            escaped.append(f"write {nth}: {exc!r}")
        st3._save = real_save
        after = TaskStore(st3._path)          # as if the process had died and come back
        rec = after.get(pid)
        if rec["state"] == T.BLOCKED and not rec["blocked_on"]:
            stranded_by_a_crash.append(nth)
        after.release_stranded()
        if after.get(pid)["state"] == T.BLOCKED:
            recovered.append(nth)

    check("a crash mid-release never empties blocked_on",
          not stranded_by_a_crash, f"writes {stranded_by_a_crash} left it unfindable")
    check("a crash mid-release always leaves the task findable",
          not recovered, f"writes {recovered} stayed blocked after the audit")
    check("a release cannot turn its blocker's transition into an error",
          not escaped, "; ".join(escaped))

    # A blocker that had already ended when the wait began is still a blocker
    # that has ended: when the live one finishes, the waiter goes now, not on
    # whenever the audit next runs.
    st5 = TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    pid = st5.create("implement", driver="queue", result={"text": "done"})["id"]
    stale = st5.create("finished long ago")["id"]
    live = st5.create("still going")["id"]
    st5.transition(stale, T.CANCELLED)
    st5.transition(pid, T.RUNNING)
    st5.update(pid, blocked_on=[stale, live])
    st5.transition(pid, T.BLOCKED)
    st5.transition(live, T.RUNNING)
    st5.transition(live, T.DONE)
    check("an already-ended blocker does not hold the release up",
          st5.get(pid)["state"] == T.AWAITING_APPROVAL, st5.get(pid)["state"])
    check("and it is forgotten in the same breath",
          st5.get(pid)["blocked_on"] == [], st5.get(pid)["blocked_on"])

    # Two blockers that ended before anyone looked: the audit frees the waiter
    # on the first one, and the second must not be left behind in blocked_on --
    # a leftover id makes a retry skip verification and review.
    st4 = TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    pid = st4.create("implement", driver="queue", result={"text": "done"})["id"]
    dead = [st4.create("a")["id"], st4.create("b")["id"]]
    for d in dead:
        st4.transition(d, T.CANCELLED)
    st4.transition(pid, T.RUNNING)
    st4.update(pid, blocked_on=dead)
    st4.transition(pid, T.BLOCKED)
    st4.release_stranded()
    check("the audit forgets every ended blocker, not just the first",
          st4.get(pid)["blocked_on"] == [], st4.get(pid)["blocked_on"])

    # The audit is worth nothing if nothing calls it.
    bot = (BASE / "bot.py").read_text()
    sched = bot[bot.index("def _task_scheduler("):bot.index("def _task_worker(")]
    check("the scheduler audits blocked tasks on its beat",
          "release_stranded()" in sched)
    check("the audit runs after the restart requeue, not before",
          sched.index("requeue_interrupted") < sched.index("release_stranded"))


# --- an unrecognised role must not become an unrestricted one ------------------
# roles.get() was ROLES.get(name, ROLES["assistant"]): a typo, a stray trailing
# space, or a role name from another build resolved to `assistant`, which is
# unrestricted, so a role meant to be read-only ran with
# --dangerously-skip-permissions. And only one of the two places a task is
# created ever checked the name, so the bad value could be stored in the first
# place.

def test_unknown_role_fails_closed():
    import roles
    import tasks as T
    print("\nan unrecognised role fails closed")

    # Half one: a name nothing understands never reaches the store.
    for bad in ("reviewr", "reviewer ", "Reviewer", "admin", "root"):
        try:
            T.make("do a thing", role=bad)
            check(f"{bad!r} is refused at creation", False, "it was accepted")
        except ValueError:
            check(f"{bad!r} is refused at creation", True)
    for good in sorted(roles.ROLES):
        check(f"{good!r} still builds", T.make("x", role=good)["role"] == good)
    check("an absent role is still the default",
          T.make("x")["role"] == "assistant"
          and T.make("x", role="")["role"] == "assistant"
          and T.make("x", role=None)["role"] == "assistant")

    store = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    try:
        store.create("x", role="reviewr")
        check("the store refuses it too", False, "it was accepted")
    except ValueError:
        check("the store refuses it too", True)
    check("and a refused task is not persisted", store.all() == {})

    # Half two: even if one were stored, resolving it gives the *most*
    # restricted template, not the least.
    default = ["--dangerously-skip-permissions"]
    for bad in ("reviewr", "reviewer ", "totally-made-up"):
        args = roles.permission_args(bad, default)
        check(f"{bad!r} never gets full autonomy",
              "--dangerously-skip-permissions" not in args, f"got {args}")
        check(f"{bad!r} resolves to a read-only role", roles.get(bad).get("restricted"))
        check(f"{bad!r} cannot edit or write",
              "Edit" not in " ".join(args) and "Write" not in " ".join(args))
    check("a made-up name is not reported as known", not roles.known("reviewr"))
    check("the genuine default is untouched",
          roles.known("") and roles.known(None)
          and roles.permission_args("assistant", default) == default
          and roles.permission_args("", default) == default,
          "failing closed must not change how a Slack turn runs")

    # Both halves again, where they are wired in.
    tree = ast.parse((BASE / "bot.py").read_text())

    def fn(name):
        return next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == name)

    def lines_of(node, dotted):
        """Line numbers of every `a.b(...)` call inside node."""
        out = []
        for c in ast.walk(node):
            if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                    and isinstance(c.func.value, ast.Name)
                    and f"{c.func.value.id}.{c.func.attr}" == dotted):
                out.append(c.lineno)
        return out

    ex = fn("execute_task")
    guard, runs = lines_of(ex, "roles.known"), lines_of(ex, "roles.permission_args")
    check("the runner checks the role name before it builds any run args",
          guard and runs and min(guard) < min(runs),
          "permissions derived from a name nobody recognises are a guess")
    guards = [n for n in ast.walk(ex) if isinstance(n, ast.If)
              and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                      and c.func.attr == "known" for c in ast.walk(n.test))]
    check("and refuses the task instead of running it anyway",
          any(any(isinstance(b, ast.Return) for b in ast.walk(g))
              and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                      and c.func.id == "task_state" for c in ast.walk(g))
              for g in guards),
          "it must stop and say so, not fall through")

    branch = next(n for n in ast.walk(fn("handle_tasks"))
                  if isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
                  and isinstance(n.test.left, ast.Name) and n.test.left.id == "action"
                  and isinstance(n.test.comparators[0], ast.Constant)
                  and n.test.comparators[0].value == "create")
    checked = lines_of(branch, "roles.validate_filed")
    check("the dashboard route validates the role before filing",
          checked and lines_of(branch, "task_store.create")
          and min(checked) < min(lines_of(branch, "task_store.create")),
          "this route passed whatever string it was handed straight to the store")
    check("and before a project is made as a side effect",
          checked and lines_of(branch, "project_store.ensure")
          and min(checked) < min(lines_of(branch, "project_store.ensure")),
          "a refused filing should leave nothing behind")


# --- a spent retry time must not requeue a task out of its own review ---------
# A quota retry sets retry_at and nothing ever cleared it. When the task later
# parked in `blocked` to wait for its reviewer, the retry sweeper saw a blocked
# task with a retry time in the past and requeued it within the minute. The
# implementor ran a second time, went to `done` with the review and landing
# gates skipped (both wait on `not blocked_on`), and the reviewed commits sat
# on a branch nothing tried to land -- tsk_5b8bad528b and tsk_96ff880fa2, and
# every other task that had once hit a limit.

def test_spent_retry_does_not_preempt_review():
    import tasks as T
    print("\na spent retry time does not requeue a task out of its review")
    store = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    tid = store.create("implement something", state=T.QUEUED, driver="queue")["id"]
    t0 = time.time()
    # Hits a quota limit: blocked with a retry time, exactly as fail_or_retry does.
    store.transition(tid, T.RUNNING)
    store.update(tid, retry_at=t0 + 60)
    store.transition(tid, T.BLOCKED, "quota: retrying")
    check("a due retry is found", store.due_retries(t0 + 61) == [tid])
    store.transition(tid, T.QUEUED, "retry time reached")
    check("waking consumes the retry time", not store.get(tid).get("retry_at"))
    store.transition(tid, T.RUNNING)
    # Finishes, and parks waiting for its reviewer -- as the review gate does.
    store.update(tid, blocked_on=["tsk_reviewer"])
    store.transition(tid, T.BLOCKED, "awaiting review tsk_reviewer")
    check("a task waiting on its review is not a due retry",
          store.due_retries(t0 + 3600) == [])

    # A record written before the fix still carries the old time. The sweeper
    # must not trust it: a task blocked on another task waits for that task.
    reviewer = store.create("the review of it, still reading", state=T.QUEUED)["id"]
    old = store.create("an older record", state=T.QUEUED)["id"]
    store.transition(old, T.RUNNING)
    store.update(old, retry_at=t0 - 5000, blocked_on=[reviewer])
    store.transition(old, T.BLOCKED, f"awaiting review {reviewer}")
    check("even with a stale retry time left on the record",
          old not in store.due_retries(t0))

    # But blocked_on outlives the review it named: retrying a failed task does
    # not clear it. A guard on "has any blocked_on" would then strand a quota
    # retry for ever. Only a task waiting on something still open is skipped.
    rv = store.create("the review, long finished", state=T.QUEUED)["id"]
    store.transition(rv, T.RUNNING); store.transition(rv, T.DONE)
    q = store.create("retried after an earlier review", state=T.QUEUED)["id"]
    store.transition(q, T.RUNNING)
    store.update(q, blocked_on=[rv], retry_at=t0 - 1)
    store.transition(q, T.BLOCKED, "quota: retrying")
    check("a quota retry still fires when blocked_on names a finished task",
          q in store.due_retries(t0))

    # A scheduled wake-up (silkworm defer) is the other user of retry_at and
    # must still fire.
    w = store.create("check on the deploy", state=T.BLOCKED, source="defer",
                     retry_at=t0 - 1)["id"]
    check("a scheduled wake-up still fires", w in store.due_retries(t0))
# (moved here: used only by this file's tests)
def _dashboard_create_impl():
    """Load handle_tasks out of bot.py without importing it.

    The same trick as _file_task_impl, and for the same reason: bot.py needs
    Slack tokens to import, and the point is to drive the shipped route rather
    than read its source. Only the two stores are stand-ins -- one real
    TaskStore, and a project_store that records what it was asked to create so
    a refused filing can be shown to have left nothing behind.
    """
    import types
    import roles as R, tasks as T
    from tasks import TaskStore

    ts = TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    made: list = []
    mod = types.ModuleType("dashboard")
    mod.__dict__.update(
        roles=R, tasks=T, task_store=ts, time=time,
        log=logging.getLogger("test"), CLAUDE_CWD=Path(tempfile.mkdtemp()),
        workspaces=__import__("workspaces"), scoping=__import__("scoping"),
        projects=__import__("projects"),
        project_store=types.SimpleNamespace(
            ensure=lambda n, **kw: (made.append(n), {"slug": n})[1],
            # What the real store answers when no project was renamed.
            slug_for=lambda n: __import__("projects").slugify(n),
            home=lambda n, create=False: made.append(n),
            get=lambda n: None,
            scope_for=lambda n: None, all=lambda: []),
    )
    lift("handle_tasks", ns=mod.__dict__,
         absent=TASK_ROUTES + STEP_ROUTES)
    return mod.handle_tasks, ts, made
# --- both front doors must file the same kind of work -------------------------
# Typing a goal into the dashboard and saying the same sentence in Slack are
# both first-class ways to file work (DESIGN.md step 3), and they disagreed.
# The form sent no role at all, handle_tasks defaulted to `assistant`, and
# needs_review("assistant") is False -- so no reviewer was ever spawned,
# verify_work never ran (it is behind the same flag), `verified` stayed None,
# and land_if_ready then refused the work for never having been verified. The
# task still got its own worktree and still ran with full permissions, so it
# committed happily; the commits just sat on a branch nothing would ever
# review, verify or land. Nothing in the form said which of the two you had
# asked for.

def test_front_doors_agree():
    import re
    import roles as R, tasks as T
    print("\nfiling the same work either way gets the same task")

    create, dash_store, projects_made = _dashboard_create_impl()
    file_task, filed_this_turn, _begin, _conv_store, _made = _file_task_impl()
    GOAL = "Cache the résumé parser's output"
    KEY = "C1:1785644289.000100"

    def dash(**kw):
        return create({"action": "create", "goal": GOAL, **kw})

    def conv(**kw):
        filed_this_turn.clear()
        return file_task({"key": KEY, "goal": GOAL, **kw})

    a, b = dash(), conv()
    check("the same goal filed either way gets the same role",
          a["ok"] and b["ok"] and a["task"]["role"] == b["role"],
          f"the dashboard filed {(a.get('task') or {}).get('role')!r} and a "
          f"conversation filed {b.get('role')!r}")
    check("and it is a role whose output is checked",
          R.needs_review(a["task"]["role"]),
          "work nobody reviews is never verified either, so it can never land")
    check("saying so explicitly still works from either",
          dash(role="assistant")["task"]["role"] == "assistant"
          and conv(role="assistant")["role"] == "assistant",
          "the default is a default, not the only answer")

    # A name nothing recognises is refused, with a reason, at both doors --
    # rather than resolving to the permissive template and running there.
    for bad in ("implementer", "Implementor", "admin", "root"):
        d, c = dash(role=bad), conv(role=bad)
        check(f"the dashboard refuses {bad!r}",
              not d["ok"] and bad in (d.get("error") or ""),
              f"answered {d.get('error') or d}")
        check(f"and so does a conversation",
              not c["ok"] and bad in (c.get("error") or ""),
              f"answered {c.get('error') or c}")
    for internal in ("reviewer", "ideator"):
        d = dash(role=internal)
        check(f"{internal!r} cannot be filed as work", not d["ok"])
        check("and is refused for existing, not for being unknown",
              "internal" in (d.get("error") or "").lower(),
              f"'{d.get('error')}' — it does exist; saying it does not is "
              "a confusing thing to read")

    # Refused before anything is written: naming a project creates a record
    # and a directory on disk, and that used to run above the check.
    projects_made.clear()
    before = len(dash_store.all())
    d = dash(role="admin", project="invented")
    check("a refused filing stores nothing and makes no project",
          not d["ok"] and projects_made == [] and len(dash_store.all()) == before,
          f"made {projects_made}")

    # driver decides whether the queue runner may ever claim the task. `state`
    # was already checked by the factory; this was not, so a value nothing
    # recognises filed a task no runner would take and no handler owned.
    for bad in ("queued", "runner", "inline ", "Queue"):
        d = dash(driver=bad)
        check(f"a driver of {bad!r} is refused",
              not d["ok"] and "driver" in (d.get("error") or ""),
              f"filed it with driver={(d.get('task') or {}).get('driver')!r}")
    check("both real drivers still file",
          all(dash(driver=x)["ok"] for x in T.DRIVERS)
          and set(T.DRIVERS) == {"inline", "queue"})
    check("and filed work is driven by the queue unless told otherwise",
          dash()["task"]["driver"] == "queue",
          "nobody is holding a live message for it")
    for bad, field in (("runner", "driver"), ("nonsense", "state")):
        try:
            T.make("x", **{field: bad})
            check(f"the factory refuses a bad {field}", False, "it was accepted")
        except ValueError as e:
            check(f"the factory refuses a bad {field}", field in str(e), str(e))

    # A filing starts queued or proposed. Every other state is a real state --
    # so the factory passes it -- and means something that has not happened.
    for bad in (T.RUNNING, T.DONE, T.BLOCKED, T.AWAITING_APPROVAL):
        d = dash(state=bad)
        check(f"a task cannot be filed straight into {bad!r}",
              not d["ok"] and bad in (d.get("error") or ""),
              f"filed it in {(d.get('task') or {}).get('state')!r}")
    check("queued and proposed both go through",
          all(dash(state=st)["ok"] for st in (T.QUEUED, T.PROPOSED)))

    # And the form itself, which is the half a person actually sees.
    r = create({"action": "roles"})
    check("the route publishes exactly what may be filed",
          r.get("ok") and [x["name"] for x in r.get("roles") or []]
                          == list(R.FILEABLE))
    check("and which one you get by not choosing",
          r.get("default") == R.DEFAULT_FILED
          and [x["name"] for x in r.get("roles") or [] if x.get("default")]
              == [R.DEFAULT_FILED])
    check("each choice says what it means",
          all(x.get("hint") for x in r.get("roles") or []),
          "'implementor' on its own does not tell you the work gets reviewed")

    viz = (BASE / "visualizer.py").read_text()
    js = re.search(r"<script>(.*?)</script>", viz, re.S).group(1)
    check("the form offers the choice at all", 'name="tfrole"' in js)
    check("it asks the route what the choices are", 'action: "roles"' in js)
    check("and sends back what was picked", "role: pickedRole()" in js)
    check("the page names no role of its own",
          not any(name in js for name in R.FILEABLE),
          "a second copy of the list is a second thing to drift")

    # There is a third door -- mail -- and it defaulted the way the dashboard
    # did, so it filed work nothing would review, verify or land. Rather than
    # pin that one line, state the rule all of them have to keep: work filed to
    # run on its own says what it is. A conversation is exempt because it is
    # not filed work (isolate=False) -- it runs where you are, and `assistant`
    # is the right answer for it.
    print("\n  every door that files isolated work names the role")
    for mod in ("bot.py", "email_ingest.py"):
        tree = ast.parse((BASE / mod).read_text())
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "create"
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "task_store"):
                continue
            kw = {k.arg: k.value for k in call.keywords if k.arg}
            isolated = isinstance(kw.get("isolate"), ast.Constant) and kw["isolate"].value is True
            if not isolated:
                continue
            check(f"{mod}:{call.lineno} files isolated work under a named role",
                  "role" in kw,
                  "it would default to 'assistant', which is never reviewed, "
                  "never verified, and therefore can never land")
            named = kw.get("role")
            if isinstance(named, ast.Constant):
                role = named.value
            elif isinstance(named, ast.Attribute) and named.attr == "DEFAULT_FILED":
                role = R.DEFAULT_FILED
            else:
                continue                      # a variable: validated at the route
            check(f"  and {role!r} is a role that exists", R.known(role),
                  "roles.get() would fall back to read-only and the runner refuse it")


# --- reviews have their own lane -------------------------------------------------

def _review_roles():
    """REVIEW_ROLES as bot.py defines it, evaluated rather than retyped."""
    return lift("REVIEW_ROLES")["REVIEW_ROLES"]


def _lanes(review_workers=1, wake_workers=1):
    return lift("lane_filter", REVIEW_WORKERS=review_workers,
                         WAKE_WORKERS=wake_workers,
                         REVIEW_ROLES=_review_roles())["lane_filter"]


def test_reviews_are_claimed_by_their_own_lane():
    """A review waited behind whatever implementor held the one worker.

    Median five hours, p90 two and a half days, for a read-only check in its
    own checkout. Reviews now have a lane; the task workers no longer take
    them, so implementors stay one at a time and reviews stop queueing behind
    them.
    """
    import tasks as T
    import threading as th
    print("\nreviews have their own lane")
    check("the review lane claims the reviewer role", "reviewer" in _review_roles())
    lane = _lanes()
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    impl = st.create("build it", role="implementor", driver="queue", thread="C:1")
    rev = st.create("review it", role="reviewer", driver="queue", thread="C:2")
    asst = st.create("look into it", role="assistant", driver="queue", thread="C:3")
    for i, t in enumerate((rev, impl, asst)):     # the review is the oldest
        st.update(t["id"], created=1000.0 + i)

    got = st.claim(**lane("review"))
    check("the review lane takes the review", got and got["id"] == rev["id"], str(got and got["id"]))
    check("and nothing else, though implementors are queued",
          st.claim(**lane("review")) is None)
    taken = [st.claim(**lane("task")), st.claim(**lane("task"))]
    check("the task workers take the rest, oldest first",
          [t and t["id"] for t in taken] == [impl["id"], asst["id"]])

    st2 = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    r2 = st2.create("review it", role="reviewer", driver="queue", thread="C:9")
    check("the task workers never take a review while the lane exists",
          st2.claim(**lane("task")) is None and st2.get(r2["id"])["state"] == T.QUEUED)
    check("with REVIEW_WORKERS=0 they do, or reviews would never run",
          (st2.claim(**_lanes(0)("task")) or {}).get("id") == r2["id"])

    # The real race: both lanes, several workers each, one store.
    st3 = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    want = {}
    for i in range(24):
        for role in ("implementor", "reviewer"):
            tid = st3.create(f"{role} {i}", role=role, driver="queue",
                             thread=f"C:{role}{i}")["id"]
            want[tid] = role
            # Implementors oldest, so a review lane that ignored its filter
            # would take one on its very first claim, whatever the timing.
            st3.update(tid, created=(1000.0 if role == "implementor" else 2000.0) + i)
    got = {"task": [], "review": []}
    def worker(name):
        while True:
            t = st3.claim(**lane(name))
            if not t:
                return
            got[name].append(t["id"])
    ths = [th.Thread(target=worker, args=(n,)) for n in ("task", "review") * 4]
    [t.start() for t in ths]
    [t.join(30) for t in ths]
    every = got["task"] + got["review"]
    check("racing lanes claim every task exactly once",
          len(every) == 48 and len(set(every)) == 48, f"{len(every)} / {len(set(every))}")
    check("no implementor was claimed by the review lane",
          all(want[t] == "reviewer" for t in got["review"]) and len(got["review"]) == 24)
    check("no review was claimed by a task worker",
          all(want[t] == "implementor" for t in got["task"]) and len(got["task"]) == 24)

    # Started like every other long-lived loop, so /status sees it.
    tree = ast.parse((BASE / "bot.py").read_text())
    starts = [c for c in ast.walk(tree) if isinstance(c, ast.Call)
              and isinstance(c.func, ast.Attribute) and c.func.attr == "start"
              and isinstance(c.func.value, ast.Name) and c.func.value.id == "daemons"
              and c.args and isinstance(c.args[0], ast.Name) and c.args[0].id == "_task_worker"]
    lanes = {e.value for c in starts for k in c.keywords if k.arg == "args"
             and isinstance(k.value, ast.Tuple) for e in k.value.elts
             if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    check("the review lane is started through daemons.start", "review" in lanes, str(lanes))
    check("its size is configurable", 'os.environ.get("REVIEW_WORKERS"' in (BASE / "bot.py").read_text())
    worker = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_task_worker")
    calls = [c for c in ast.walk(worker) if isinstance(c, ast.Call)
             and isinstance(c.func, ast.Attribute) and c.func.attr == "claim"]
    check("every claim the worker makes goes through its lane's filter",
          calls and all(any(k.arg is None for k in c.keywords) for c in calls))


def test_a_task_is_not_claimed_beside_its_relatives():
    """Two lanes make it possible to claim a review while its implementor runs.

    Possible after a rework: the implementor is queued again while its review
    still waits. They share a thread, so the second would only sit on the
    thread's lock holding a worker -- and a review that checked out the branch
    first would read a tip the implementor was still moving.
    """
    import tasks as T
    print("\nno claim beside a running relative")
    lane = _lanes()
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    parent = st.create("build it", role="implementor", driver="queue", thread="C:1")
    st.transition(parent["id"], T.RUNNING, "reworked, running again")
    rev = st.create("review it", role="reviewer", driver="queue", thread="C:1",
                    parent=parent["id"])
    check("a review is not claimed while its implementor runs",
          st.claim(**lane("review")) is None)
    other = st.create("review another", role="reviewer", driver="queue", thread="C:2")
    check("an unrelated review still is", (st.claim(**lane("review")) or {}).get("id") == other["id"])
    # Stopping parks it on its review, as resolve_review does. Not `done`: a
    # task that has ended closes its unrun reviews (TaskStore._close_reviews),
    # so a queued review of a finished task is cancelled, not claimed.
    st.transition(parent["id"], T.BLOCKED, "awaiting review")
    check("and the waiting one once the implementor stops",
          (st.claim(**lane("review")) or {}).get("id") == rev["id"])

    st2 = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    p2 = st2.create("build it", role="implementor", driver="queue", thread="C:1")
    r2 = st2.create("review it", role="reviewer", driver="queue", thread="C:7", parent=p2["id"])
    st2.transition(r2["id"], T.RUNNING, "reviewing")
    check("nor an implementor while its own review runs, whatever the thread",
          st2.claim(**lane("task")) is None)
    sib = st2.create("wake up", role="assistant", driver="queue", thread="C:7")
    check("nor anything else on a thread a queue task is running on",
          st2.claim(**lane("task")) is None and st2.get(sib["id"])["state"] == T.QUEUED)

    st3 = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    slack = st3.create("a slack turn", thread="C:1")             # inline
    st3.transition(slack["id"], T.RUNNING, "live")
    q = st3.create("review it", role="reviewer", driver="queue", thread="C:1")
    check("a live Slack turn does not hold the thread's queue work",
          (st3.claim(**lane("review")) or {}).get("id") == q["id"],
          "inline records are not reset at startup, so one left behind would wedge it")


def test_a_review_runs_while_an_implementor_is_running():
    """The point of the lane, with the real worker loop on a real store."""
    import drain as D
    import retry as R
    import tasks as T
    import threading as th
    print("\na review runs while an implementor is running")
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    impl = st.create("build it", role="implementor", driver="queue", thread="C:1")
    rev = st.create("review it", role="reviewer", driver="queue", thread="C:2")
    started, reviewed = th.Event(), th.Event()
    seen = {}

    def execute_task(task):
        if task["role"] == "implementor":
            started.set()
            # Hours, in life. Here: until the review is done, or the review is
            # stuck behind it -- in which case this gives up and says so.
            seen["review finished first"] = reviewed.wait(10)
            st.transition(task["id"], T.DONE, "built")
        else:
            started.wait(10)
            seen["implementor running"] = st.get(impl["id"])["state"] == T.RUNNING
            st.transition(task["id"], T.DONE, "reviewed")
            reviewed.set()

    class Idle(BaseException):
        pass
    def idle(_s):
        raise Idle()

    def lane_worker(name):
        worker = _bot_func("_task_worker", task_store=st, execute_task=execute_task,
                           hold_unsupervised=lambda task: False, lane_filter=_lanes(),
                           RUNNER_HOLD=R.Hold(), DRAIN=D.Drain(), TASK_POLL_S=5,
                           log=logging.getLogger("test"),
                           time=types.SimpleNamespace(sleep=idle, time=time.time))
        try:
            worker(0, name)
        except Idle:
            pass
    ths = [th.Thread(target=lane_worker, args=(n,)) for n in ("task", "review")]
    [t.start() for t in ths]
    [t.join(30) for t in ths]
    check("the review ran while the implementor was still running",
          seen.get("implementor running") is True, str(seen))
    check("and finished before it", seen.get("review finished first") is True, str(seen))
    check("both are done", st.get(impl["id"])["state"] == st.get(rev["id"])["state"] == T.DONE)

    # The lane honours the same hold and drain as the task workers.
    for name, hold, drn in (("hold", R.Hold(), D.Drain()), ("drain", R.Hold(), D.Drain())):
        st4 = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
        r4 = st4.create("review it", role="reviewer", driver="queue", thread="C:2")
        if name == "hold":
            hold.close(time.time() + 600, "quota")
        else:
            drn.start(600)
        ran = []
        w = _bot_func("_task_worker", task_store=st4, execute_task=lambda t: ran.append(t),
                      hold_unsupervised=lambda task: False, lane_filter=_lanes(),
                      RUNNER_HOLD=hold, DRAIN=drn, TASK_POLL_S=5, log=logging.getLogger("test"),
                      time=types.SimpleNamespace(sleep=idle, time=time.time))
        try:
            w(0, "review")
        except Idle:
            pass
        check(f"the review lane claims nothing under a {name}",
              not ran and st4.get(r4["id"])["state"] == T.QUEUED)


# --- wake-ups have their own lane --------------------------------------------------

def _wake(st, thread="C:w", due_ago=5.0):
    """A wake-up as handle_defer files it, already requeued by the scheduler."""
    import tasks as T
    w = st.create("check the deploy", title="check the deploy", state=T.BLOCKED,
                  driver="queue", source=T.WAKE_SOURCE, thread=thread, isolate=False,
                  retry_at=time.time() - due_ago, defers=1)
    for tid in st.due_retries(time.time()):
        if tid == w["id"]:
            st.transition(tid, T.QUEUED, "retry time reached")
    return st.get(w["id"])


def _busy_threads(locks=None, running=None):
    import threading as th
    return lift("busy_threads", _thread_locks=locks if locks is not None else {},
                         _thread_locks_guard=th.Lock(),
                         RUNNING=running if running is not None else {})["busy_threads"]


def _lane_worker(st, execute_task, lane_filter=None, drain=None, busy=None):
    """The real _task_worker loop on a real store, returning when it goes idle."""
    import drain as D
    import retry as R

    class Idle(BaseException):
        pass

    def idle(_s):
        raise Idle()
    w = _bot_func("_task_worker", task_store=st, execute_task=execute_task,
                  hold_unsupervised=lambda task: False,
                  lane_filter=lane_filter or _lanes(),
                  wake_skip=busy or _busy_threads(),
                  RUNNER_HOLD=R.Hold(), DRAIN=drain or D.Drain(), TASK_POLL_S=5,
                  log=logging.getLogger("test"),
                  time=types.SimpleNamespace(sleep=idle, time=time.time))

    def run(lane):
        try:
            w(0, lane)
        except Idle:
            pass
    return run


def test_wakeups_are_claimed_by_their_own_lane():
    """A due wake-up sat over an hour behind implementors in the one task lane.

    2026-10-12: tsk_f8c864330d was requeued on time at 14:17 and was still
    queued an hour later, behind long implementor tasks and accepted
    proposals. Wake-ups now have a lane; the task workers leave them alone,
    and everything else -- quota retries included -- stays where it was.
    """
    import tasks as T
    print("\nwake-ups have their own lane")
    lane = _lanes()
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    impl = st.create("build it", role="implementor", driver="queue", thread="C:1")
    st.update(impl["id"], created=1.0)                  # oldest, so a leaky filter takes it
    wake = _wake(st, thread="C:w")
    check("a due wake-up is queued, marked by its source",
          wake["state"] == T.QUEUED and T.is_wakeup(wake))
    check("the wake lane takes the wake-up",
          (st.claim(**lane("wake")) or {}).get("id") == wake["id"])
    check("and nothing else, though an implementor is queued",
          st.claim(**lane("wake")) is None and st.get(impl["id"])["state"] == T.QUEUED)
    check("the review lane does not take it either",
          st.claim(**lane("review")) is None)

    st2 = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    w2 = _wake(st2, thread="C:w2")
    check("the task workers never take a wake-up while the lane exists",
          st2.claim(**lane("task")) is None and st2.get(w2["id"])["state"] == T.QUEUED)
    check("with WAKE_WORKERS=0 they do, or wake-ups would never run",
          (st2.claim(**_lanes(1, 0)("task")) or {}).get("id") == w2["id"])

    # A quota retry: blocked with a retry time, requeued by the same scheduler
    # beat -- but filed by a conversation, not by defer. Its lane is unchanged.
    st3 = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    q = st3.create("do the thing", role="assistant", driver="queue", source="slack",
                   thread="C:q", state=T.BLOCKED, retry_at=time.time() - 5)
    for tid in st3.due_retries(time.time()):
        st3.transition(tid, T.QUEUED, "retry time reached")
    check("a quota retry is not a wake-up", not T.is_wakeup(st3.get(q["id"])))
    check("the wake lane leaves a quota retry alone", st3.claim(**lane("wake")) is None)
    check("and the task lane runs it, as before",
          (st3.claim(**lane("task")) or {}).get("id") == q["id"])

    # Identified by the marker, not by what it looks like.
    st4 = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    st4.create("check the deploy", title="check the deploy", role="assistant",
               driver="queue", source="slack", thread="C:x")
    check("a task merely titled like a wake-up is not one",
          st4.claim(**lane("wake")) is None)

    # Started like the other lanes, so /status sees it, and sized by .env.
    tree = ast.parse((BASE / "bot.py").read_text())
    starts = [c for c in ast.walk(tree) if isinstance(c, ast.Call)
              and isinstance(c.func, ast.Attribute) and c.func.attr == "start"
              and isinstance(c.func.value, ast.Name) and c.func.value.id == "daemons"
              and c.args and isinstance(c.args[0], ast.Name) and c.args[0].id == "_task_worker"]
    lanes = {e.value for c in starts for k in c.keywords if k.arg == "args"
             and isinstance(k.value, ast.Tuple) for e in k.value.elts
             if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    check("the wake lane is started through daemons.start", "wake" in lanes, str(lanes))
    check("its size is configurable", 'os.environ.get("WAKE_WORKERS"' in (BASE / "bot.py").read_text())


def test_a_wakeup_runs_while_an_implementor_is_running():
    """The incident itself, with the real worker loop: the one task worker is
    busy with a long implementor, and the wake-up still runs on time."""
    import tasks as T
    import threading as th
    print("\na wake-up runs while an implementor is running")
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    impl = st.create("build it", role="implementor", driver="queue", thread="C:1")
    started, woke = th.Event(), th.Event()
    seen = {}

    def execute_task(task):
        if task["role"] == "implementor":
            started.set()
            seen["wake-up ran first"] = woke.wait(10)    # hours, in life
            st.transition(task["id"], T.DONE, "built")
        else:
            seen["implementor running"] = st.get(impl["id"])["state"] == T.RUNNING
            st.transition(task["id"], T.DONE, "checked")
            woke.set()

    task_lane = th.Thread(target=_lane_worker(st, execute_task), args=("task",))
    task_lane.start()
    started.wait(10)
    wake = _wake(st, thread="C:w")             # falls due mid-implementor
    wake_lane = th.Thread(target=_lane_worker(st, execute_task), args=("wake",))
    wake_lane.start()
    [t.join(30) for t in (task_lane, wake_lane)]
    check("the wake-up ran while the implementor was still running",
          seen.get("implementor running") is True, str(seen))
    check("and finished before it", seen.get("wake-up ran first") is True, str(seen))
    check("both are done", st.get(impl["id"])["state"] == st.get(wake["id"])["state"] == T.DONE)


def test_a_wakeup_waits_for_its_own_thread_and_for_drain():
    """It waits on its thread, not on unrelated work -- and not through a drain."""
    import drain as D
    import tasks as T
    import threading as th
    print("\na wake-up waits for its own thread, and for a drain")

    # Mid-turn by a live Slack turn: no queue task, only the thread's lock.
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    mine = _wake(st, thread="C:busy")
    other = _wake(st, thread="C:free")
    st.update(mine["id"], created=1.0)          # oldest: it would go first
    lk = th.Lock()
    lk.acquire()
    busy = _busy_threads(locks={"C:busy": lk})
    check("a thread holding its lock reads as busy", busy() == {"C:busy"})
    ran = []
    _lane_worker(st, lambda t: (ran.append(t["id"]),
                                st.transition(t["id"], T.DONE, "ok")), busy=busy)("wake")
    check("the wake-up on a thread mid-turn is not claimed",
          st.get(mine["id"])["state"] == T.QUEUED and mine["id"] not in ran)
    check("another thread's wake-up is not held behind it", ran == [other["id"]], str(ran))
    lk.release()
    _lane_worker(st, lambda t: (ran.append(t["id"]),
                                st.transition(t["id"], T.DONE, "ok")), busy=busy)("wake")
    check("once the turn ends it runs", ran == [other["id"], mine["id"]], str(ran))

    # A turn the bot is driving (RUNNING) counts too, lock or not.
    check("a thread in RUNNING reads as busy",
          _busy_threads(running={"C:r": object()})() == {"C:r"})
    # A queue task on the thread is already caught by claim() itself.
    st2 = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    q = st2.create("earlier work", role="assistant", driver="queue", thread="C:7")
    st2.transition(q["id"], T.RUNNING, "running")
    w2 = _wake(st2, thread="C:7")
    check("nor while a queue task runs on its thread",
          st2.claim(**_lanes()("wake")) is None and st2.get(w2["id"])["state"] == T.QUEUED)

    # Its checkout held -- a landing, or a turn on another thread in the same
    # repo: execute_task would park the lane's one worker on repo_guard.
    repo = Path(tempfile.mkdtemp())
    (repo / ".git").mkdir()
    st5 = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    held = _wake(st5, thread="C:repo")
    st5.update(held["id"], created=1.0, scope={"cwd": str(repo)})
    free = _wake(st5, thread="C:elsewhere")
    ns = lift("wake_skip", "busy_threads", "_repo_lock",
                       _thread_locks={}, _thread_locks_guard=th.Lock(), RUNNING={},
                       _repo_locks={}, _repo_locks_guard=th.Lock(), task_store=st5,
                       tasks=T, CLAUDE_CWD=Path(tempfile.mkdtemp()), Path=Path,
                       threading=th)
    check("a free checkout holds nothing back", ns["wake_skip"]() == set())
    ns["_repo_lock"](repo).acquire()
    check("a held checkout holds back its wake-up's thread",
          ns["wake_skip"]() == {"C:repo"}, str(ns["wake_skip"]()))
    ran5 = []
    _lane_worker(st5, lambda t: (ran5.append(t["id"]),
                                 st5.transition(t["id"], T.DONE, "ok")),
                 busy=ns["wake_skip"])("wake")
    check("so the lane runs the other thread's wake-up instead of waiting",
          ran5 == [free["id"]] and st5.get(held["id"])["state"] == T.QUEUED, str(ran5))
    ns["_repo_lock"](repo).release()
    _lane_worker(st5, lambda t: (ran5.append(t["id"]),
                                 st5.transition(t["id"], T.DONE, "ok")),
                 busy=ns["wake_skip"])("wake")
    check("and the held one once the checkout is free", ran5 == [free["id"], held["id"]])

    # Draining for a deploy: nothing new starts, in this lane as in the others.
    st3 = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    w3 = _wake(st3)
    drn = D.Drain()
    drn.start(600)
    ran3 = []
    _lane_worker(st3, lambda t: ran3.append(t), drain=drn)("wake")
    check("the wake lane claims nothing while draining",
          not ran3 and st3.get(w3["id"])["state"] == T.QUEUED)
    drn.stop()
    _lane_worker(st3, lambda t: ran3.append(t), drain=drn)("wake")
    check("and claims it once the drain lifts", [t["id"] for t in ran3] == [w3["id"]])


def test_a_late_wakeup_is_reported():
    """Due and unclaimed past a couple of minutes is late, and says so."""
    import tasks as T
    print("\nlate wake-ups are reported")
    now = time.time()
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    fresh = _wake(st, thread="C:1", due_ago=5)
    stale = st.create("check it", driver="queue", source=T.WAKE_SOURCE, thread="C:2",
                      state=T.BLOCKED, retry_at=now - 3600)
    stuck = st.create("check again", driver="queue", source=T.WAKE_SOURCE, thread="C:3",
                      state=T.BLOCKED, retry_at=now - 10)
    st.transition(stuck["id"], T.QUEUED, "retry time reached")
    rec = st.get(stuck["id"])
    rec["events"][-1]["at"] = now - 600          # requeued ten minutes ago
    st._data[stuck["id"]]["events"] = rec["events"]
    future = st.create("later", driver="queue", source=T.WAKE_SOURCE, thread="C:4",
                       state=T.BLOCKED, retry_at=now + 600)
    quota = st.create("retry", driver="queue", source="slack", thread="C:5",
                      state=T.BLOCKED, retry_at=now - 3600)
    late = {w["id"]: w for w in st.late_wakeups(now)}
    check("a wake-up queued ten minutes ago and unclaimed is late",
          stuck["id"] in late and late[stuck["id"]]["late_s"] >= 590, str(late))
    check("so is one still blocked an hour past its time", stale["id"] in late)
    check("one due seconds ago is not, yet", fresh["id"] not in late)
    check("nor one not due", future["id"] not in late)
    check("nor a quota retry, however overdue", quota["id"] not in late)
    st.transition(stuck["id"], T.RUNNING, "claimed")
    check("nor one already claimed", stuck["id"] not in {w["id"] for w in st.late_wakeups(now)})
    check("the grace is a couple of minutes", 60 <= T.LATE_WAKE_S <= 300)

    # Surfaced where you look: /status carries them, and both commands print them.
    st_late = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    gone = st_late.create("check it", driver="queue", source=T.WAKE_SOURCE, thread="C:2",
                          state=T.BLOCKED, retry_at=time.time() - 3600)
    out = lift(
        "handle_status", store=types.SimpleNamespace(all=lambda: {}), RUNNING={},
        RUNNER_HOLD=__import__("retry").Hold(), DRAIN=__import__("drain").Drain(),
        credentials=types.SimpleNamespace(state=lambda has_token: {"mode": "token"}),
        slack=types.SimpleNamespace(status=lambda now: {}),
        revision=types.SimpleNamespace(state=lambda *a: {}),
        BASE_DIR=BASE, REVISION={}, os=os, daemons=__import__("daemons"),
        task_store=st_late)["handle_status"]({})
    check("/status reports late wake-ups",
          [w["id"] for w in out.get("late_wakeups") or []] == [gone["id"]],
          str(out.get("late_wakeups")))
    cli = (BASE / "bin" / "silkworm").read_text()
    check("silkworm status checks them", "scheduled wake-ups on time" in cli)
    check("silkworm watching marks them", 'w.get("late")' in cli)


def _turn_runner(st, sessions, locks, RUNNING, RUNNING_TASKS, run_turn, resolve_review):
    """execute_task as the workers run it, sharing the real per-turn bookkeeping."""
    import roles
    import tasks as T
    release = lift("release_turn", RUNNING=RUNNING, RUNNING_TASKS=RUNNING_TASKS,
                            store=sessions, recovery=recovery)["release_turn"]

    class Progress:
        def __init__(self, *a):
            self.ts = "p.1"
        update = finalize = delete = lambda self, *a: None

    def run(task):
        _bot_func("execute_task", tasks=T, task_store=st, store=sessions, roles=roles,
                  ProgressMessage=Progress,
                  recovery=recovery, uuid=__import__("uuid"), release_turn=release,
                  Path=Path, run_turn=run_turn, review_branch=lambda t: "",
                  worktrees=__import__("worktrees"), shutil=shutil,
                  OUTBOX_ROOT=Path(tempfile.mkdtemp()), SILKWORM_BIN="/x/silkworm",
                  permission_args=lambda: [], log=logging.getLogger("test"),
                  task_thread=lambda t: tuple(t["thread"].split(":")),
                  task_state=lambda tid, s, d="": st.transition(tid, s, d),
                  _thread_lock=locks["_thread_lock"], repo_guard=locks["repo_guard"],
                  render_block=lambda _: "", chunk=lambda text: [text],
                  to_mrkdwn=lambda text: text, resolve_review=resolve_review,
                  verify_work=lambda t, c: {"ran": False, "ok": False},
                  upload_outbox=lambda *a, **k: [], RUNNING=RUNNING,
                  RUNNING_TASKS=RUNNING_TASKS, ClaudeStopped=ClaudeStopped,
                  ClaudeError=ClaudeError)(task)
    return run


def _real_locks():
    import threading as th
    return lift("_thread_lock", "_repo_lock", "repo_guard",
                         threading=th, Path=Path, contextlib=contextlib,
                         _thread_locks={}, _thread_locks_guard=th.Lock(),
                         _repo_locks={}, _repo_locks_guard=th.Lock())


def test_two_turns_at_once_do_not_clobber_each_other():
    """Real execute_task turns overlapping, as the two lanes now make them.

    Different threads in different checkouts: neither waits on the other. And
    the case only the lanes create -- a review claimed on its implementor's
    thread the moment the implementor parks, while that implementor's turn is
    still on its way out. Its cleanup used to pop the thread's RunHandle and
    recovery marker by key, which by then were the review's: `!stop` and the
    board said nothing was running, and a restart had nothing to recover.
    """
    import threading as th
    import tasks as T
    print("\ntwo turns at once")
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    sessions = tmp_store()
    a, b = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    (a / ".git").mkdir()
    (b / ".git").mkdir()                 # two checkouts, so two repo locks
    impl = st.create("build it", role="implementor", driver="queue", thread="C:1",
                     isolate=False, scope={"cwd": str(a)})
    rev = st.create("review it", role="reviewer", driver="queue", thread="C:2",
                    isolate=False, scope={"cwd": str(b)})
    RUNNING, RUNNING_TASKS = {}, {}
    impl_in, review_done = th.Event(), th.Event()
    seen = {}

    def run_turn(goal, **kw):
        handle = object()
        kw["on_start"](handle)
        if "build" in goal:
            impl_in.set()
            seen["review finished during the build"] = review_done.wait(10)
            seen["build still keyed after the review"] = (
                RUNNING.get("C:1") is handle and RUNNING_TASKS.get(impl["id"]) is handle
                and (sessions.get("C:1") or {}).get("pending") is not None)
        else:
            impl_in.wait(10)
            seen["both running at once"] = (set(RUNNING) == {"C:1", "C:2"}
                                            and set(RUNNING_TASKS) == {impl["id"], rev["id"]})
        return types.SimpleNamespace(text="ok", cost_usd=0.1, duration_ms=1,
                                     session_id="s-" + goal[:5])

    def finished(task, *a, **k):
        if task["role"] == "reviewer":
            review_done.set()
        return False

    run = _turn_runner(st, sessions, _real_locks(), RUNNING, RUNNING_TASKS, run_turn, finished)
    for t in (impl, rev):
        st.transition(t["id"], T.RUNNING, "claimed")
    ths = [th.Thread(target=run, args=(st.get(t["id"]),)) for t in (impl, rev)]
    [t.start() for t in ths]
    [t.join(30) for t in ths]
    check("the two turns ran at once", seen.get("both running at once") is True, str(seen))
    check("the review finished while the implementor was mid-turn",
          seen.get("review finished during the build") is True, str(seen))
    check("the review ending did not touch the implementor's handles or marker",
          seen.get("build still keyed after the review") is True, str(seen))
    check("both handles are gone once both turns end", not RUNNING and not RUNNING_TASKS)
    check("and both markers", not (sessions.get("C:1") or {}).get("pending")
          and not (sessions.get("C:2") or {}).get("pending"))
    check("both tasks finished", st.get(impl["id"])["state"] == st.get(rev["id"])["state"] == T.DONE)

    # The hand-off on one thread.
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    sessions = tmp_store()
    RUNNING, RUNNING_TASKS = {}, {}
    parent = st.create("build it", role="implementor", driver="queue", thread="C:5",
                       isolate=False, scope={"cwd": str(a)})
    child = {}
    review_in, parent_gone = th.Event(), th.Event()
    seen = {}

    def run_turn2(goal, **kw):
        handle = object()
        kw["on_start"](handle)
        if "review" in goal:
            review_in.set()
            parent_gone.wait(10)          # the implementor's turn has fully ended
            seen["review still running"] = RUNNING.get("C:5") is handle
            seen["review still a running task"] = RUNNING_TASKS.get(child["id"]) is handle
            seen["review's marker survived"] = bool((sessions.get("C:5") or {}).get("pending"))
        return types.SimpleNamespace(text="ok", cost_usd=0.1, duration_ms=1, session_id="s")

    threads = []
    def park_for_review(task, *a, **k):
        if task["role"] != "implementor":
            return False
        # What resolve_review does, then the review lane claiming it at once.
        r = st.create("review it", role="reviewer", driver="queue", thread="C:5",
                      isolate=False, scope={"cwd": str(a)}, parent=task["id"])
        child["id"] = r["id"]
        st.transition(task["id"], T.BLOCKED, "awaiting review")
        st.transition(r["id"], T.RUNNING, "claimed by the review lane")
        t = th.Thread(target=run, args=(st.get(r["id"]),))
        threads.append(t)
        t.start()
        review_in.wait(10)                # in, before this turn's cleanup runs
        return True

    run = _turn_runner(st, sessions, _real_locks(), RUNNING, RUNNING_TASKS, run_turn2,
                       park_for_review)
    st.transition(parent["id"], T.RUNNING, "claimed")
    run(st.get(parent["id"]))
    parent_gone.set()
    [t.join(30) for t in threads]
    check("a review that starts as its implementor ends is still listed as running",
          seen.get("review still running") is True, str(seen))
    check("and still stoppable by task id", seen.get("review still a running task") is True, str(seen))
    check("and still recoverable after a restart", seen.get("review's marker survived") is True, str(seen))
    check("its own cleanup still clears it",
          not RUNNING and not RUNNING_TASKS and not (sessions.get("C:5") or {}).get("pending"))


# --- a run that hands you the last step must not close as done ----------------
# tsk_fd122a33d3 ("Cut new test flight build") rightly did not release --
# releasing is yours, and implementors cannot tag or push -- and said exactly
# what to type: `!release cadence all minor`. Review agreed, landing found
# nothing to land, and it closed as `done`. The board read as finished and
# TestFlight never got a build. A run now says so in a block the record keeps,
# and the task waits in needs_input with that step on every surface.

_ASKS = ('Nothing to change in the repo: the build is cut by a release, '
         'which is yours.\n\n```json\n'
         '{"needs_user": "!release cadence all minor", '
         '"why": "releasing is user-triggered"}\n```')


def test_needs_user_block_is_read_and_fails_closed():
    import roles
    print("\nthe needs_user block is read, and anything else is ignored")
    got = roles.parse_needs_user(_ASKS, "cadence")
    check("a release step is read with its reason",
          got == {"action": "!release cadence all minor",
                  "why": "releasing is user-triggered",
                  "release": "cadence all minor"}, str(got))
    other = roles.parse_needs_user('ok\n```json\n{"needs_user": "add STRIPE_KEY to .env"}\n```')
    check("any other step is kept as text, with no release to run",
          other == {"action": "add STRIPE_KEY to .env", "why": "", "release": ""}, str(other))
    check("a bare object is tolerated, as a verdict's is",
          (roles.parse_needs_user('{"needs_user": "approve the $40 plan"}') or {})
          .get("action") == "approve the $40 plan")
    sneaky = roles.parse_needs_user(
        '```json\n{"needs_user": "!release cadence all minor; rm -rf ~"}\n```')
    check("a release with anything after it is not offered as a button",
          sneaky and sneaky["release"] == "", str(sneaky))
    check("a release in backticks still is",
          (roles.parse_needs_user('```json\n{"needs_user": "`!release saga app 1.2.0`"}\n```',
                                  "saga") or {}).get("release") == "saga app 1.2.0")
    # The slug is model-written: a cadence task must not hand you a button
    # that deploys trader.
    elsewhere = roles.parse_needs_user(_ASKS, "trader")
    check("a release of another project than the task's is kept as text, no button",
          elsewhere and elsewhere["action"] == "!release cadence all minor"
          and elsewhere["release"] == "", str(elsewhere))
    check("as is one from a task with no project to check it against",
          (roles.parse_needs_user(_ASKS) or {}).get("release") == ""
          and (roles.parse_needs_user(_ASKS, "") or {}).get("release") == "")
    check("the project is matched whole, not as a prefix",
          not roles.release_is_for("cadence all minor", "cad")
          and not roles.release_is_for("cadence-support all", "cadence")
          and roles.release_is_for("cadence all minor", "Cadence"))
    for name, text in (
            ("no block", "All done: tests pass, committed on my branch."),
            ("json that does not parse", '```json\n{"needs_user": "x",}\n```'),
            ("a block about something else", '```json\n{"ok": true}\n```'),
            ("an empty action", '```json\n{"needs_user": "   "}\n```'),
            ("an action that is not a string", '```json\n{"needs_user": ["a", "b"]}\n```'),
            ("a block that is not an object", '```json\n["needs_user"]\n```'),
            ("an example quoted earlier, then a final block without it",
             'I could have said ```json\n{"needs_user": "x"}\n``` but it is done.\n'
             '```json\n{"summary": "done"}\n```'),
            ("nothing at all", None)):
        check(f"{name} reads as no step (the task ends as it always did)",
              roles.parse_needs_user(text) is None)
    check("the note is asked of implementors and queued assistants, nobody else",
          roles.asks_user("implementor") and roles.asks_user("assistant")
          and not roles.asks_user("reviewer") and not roles.asks_user("ideator")
          and not roles.asks_user("no-such-role"))
    check("the reviewer is told an unmarked hand-off is a finding",
          "needs_user" in roles.REVIEWER_SYSTEM and "finding" in
          roles.REVIEWER_SYSTEM[roles.REVIEWER_SYSTEM.index("needs_user"):][:300])


def test_a_run_that_hands_you_a_step_waits_for_you():
    print("\na run that hands you a step ends in needs_input, not done")
    import threading
    import roles
    import tasks as T
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    root = Path(tempfile.mkdtemp())
    told, prompts = [], []
    state = _task_state_for(st)
    replies = iter([_ASKS, "Released nothing; the build is cut and committed.",
                    '```json\n{"needs_user": 7}\n```', _ASKS])

    def run_turn(goal, **kw):
        prompts.append(kw.get("append_system_prompt") or "")
        return types.SimpleNamespace(text=next(replies), cost_usd=0.1,
                                     duration_ms=1, session_id="s")
    fn = _bot_func("execute_task", tasks=T, task_store=st, store=tmp_store(),
                   roles=roles, Path=Path, run_turn=run_turn,
                   review_branch=lambda t: "", worktrees=__import__("worktrees"),
                   OUTBOX_ROOT=root / "outbox", SILKWORM_BIN="/x/silkworm",
                   permission_args=lambda: [], log=logging.getLogger("test"),
                   task_thread=lambda t: ("C1", "1.0"), task_key=lambda t: "C1:1.0",
                   task_state=state, tell_thread=lambda key, text: told.append((key, text)),
                   defer=__import__("defer"),
                   _thread_lock=lambda key: threading.Lock(),
                   repo_guard=lambda *a, **k: contextlib.nullcontext(),
                   render_block=lambda _: "", chunk=lambda text: [text],
                   to_mrkdwn=lambda text: text, resolve_review=lambda *a, **k: False,
                   upload_outbox=lambda *a, **k: [], RUNNING={}, RUNNING_TASKS={},
                   COST_NOTE="(list)", ClaudeStopped=ClaudeStopped, ClaudeError=ClaudeError)

    def run(**fields):
        rec = st.create("Cut new test flight build", role="assistant", project="cadence",
                        thread="C1:1.0", driver="queue", isolate=False,
                        scope={"cwd": str(root)}, **fields)
        st.transition(rec["id"], T.RUNNING, "claimed")
        fn(st.get(rec["id"]))
        return st.get(rec["id"])

    asked = run()
    check("the run was asked to mark a step that is yours",
          roles.NEEDS_USER_NOTE in prompts[-1])
    check("a reply ending in a needs_user block ends in needs_input, not done",
          asked["state"] == T.NEEDS_INPUT, asked["state"])
    check("with the step recorded on the task",
          (asked.get("needs_user") or {}).get("action") == "!release cadence all minor"
          and asked["needs_user"].get("release") == "cadence all minor", str(asked.get("needs_user")))
    check("and said in the event that parked it",
          asked["events"][-1]["detail"] == T.NEEDS_USER_PREFIX + "!release cadence all minor",
          asked["events"][-1]["detail"])
    check("and in its thread, once", sum("!release cadence all minor" in t for _, t in told) == 1
          and told[-1][0] == "C1:1.0", str(told))
    check("it is in front of you", asked["id"] in [t["id"] for t in st.needs_attention()])
    plain = run()
    check("a reply without one ends done, as before",
          plain["state"] == T.DONE and plain.get("needs_user") is None, plain["state"])
    bad = run()
    check("a malformed block fails closed to done",
          bad["state"] == T.DONE and bad.get("needs_user") is None, bad["state"])
    watch = run(source="defer")
    check("a scheduled wake-up is neither asked nor read for one",
          watch["state"] == T.DONE and watch.get("needs_user") is None
          and roles.NEEDS_USER_NOTE not in prompts[-1], watch["state"])


def test_a_passing_review_still_leaves_the_step_with_you():
    print("\nreview passing does not close a task that left you a step")
    from unittest.mock import MagicMock
    import roles
    import tasks as T
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    state = _task_state_for(st)
    told = []
    resolve = _bot_func("resolve_review", task_store=st, tasks=T, roles=roles,
                        app=MagicMock(), time=time, task_state=state,
                        tell_thread=lambda key, text: told.append((key, text)),
                        merge=__import__("merge"),
                        # What land_if_ready's never() returns for a task
                        # whose job was already done: nothing to land.
                        land_and_record=lambda *a: {"eligible": False, "landed": False,
                                                    "stage": "nothing-to-land"},
                        file_followups=lambda *a, **k: [],
                        rework_flagged_review=lambda *a: False,
                        rework_conflict=lambda *a: False, log=logging.getLogger("t"))

    def reviewed(ok, asks):
        impl = st.create("Cut new test flight build", role="implementor", project="cadence")
        st.transition(impl["id"], T.RUNNING)
        st.update(impl["id"], needs_user=asks)
        st.transition(impl["id"], T.BLOCKED, "awaiting review")
        rev = st.create("review it", role="reviewer", parent=impl["id"])
        resolve(dict(rev), "reviewer", '```json\n' + json.dumps(
            {"ok": ok, "summary": "fine" if ok else "wrong",
             "findings": [] if ok else ["it is wrong"]}) + '\n```', "C1", "1.0")
        return st.get(impl["id"])

    step = roles.parse_needs_user(_ASKS, "cadence")
    t = reviewed(True, step)
    check("passed review, nothing to land, a step left: needs_input",
          t["state"] == T.NEEDS_INPUT and t["needs_user"] == step, t["state"])
    check("and its thread is told the step is now over to you",
          any("!release cadence all minor" in text for _, text in told), str(told))
    check("the verdict is still recorded on it",
          ((t.get("result") or {}).get("review") or {}).get("ok") is True)
    check("without a step, the same verdict closes it", reviewed(True, None)["state"] == T.DONE)
    check("a flagged review still goes to approval, step and all",
          reviewed(False, step)["state"] == T.AWAITING_APPROVAL)
    # Closed by you while its review ran: it stays closed, and says nothing.
    impl = st.create("closed meanwhile", role="implementor", project="cadence")
    st.transition(impl["id"], T.RUNNING)
    st.update(impl["id"], needs_user=step)
    st.transition(impl["id"], T.BLOCKED, "awaiting review")
    st.transition(impl["id"], T.DONE, "approve via you")
    before = len(told)
    rev = st.create("review it", role="reviewer", parent=impl["id"])
    resolve(dict(rev), "reviewer", '```json\n{"ok": true, "summary": "fine"}\n```', "C1", "1.0")
    check("a parent closed during its review is not told it waits on you",
          st.get(impl["id"])["state"] == T.DONE and len(told) == before, str(told[before:]))


def test_the_step_is_on_every_surface():
    print("\nthe step shows on the board, the Slack board and the digest")
    import board
    import digest
    import home
    import roles
    import tasks as T
    sys.argv = ["x"]
    import visualizer as V
    now = time.time()
    step = roles.parse_needs_user(_ASKS, "cadence")
    rec = {"id": "tsk_rel", "title": "Cut new test flight build", "project": "cadence",
           "state": T.NEEDS_INPUT, "role": "implementor", "needs_user": step,
           "created": now - 600, "updated": now - 60, "thread": "C1:1.0",
           "events": [{"at": now - 60, "kind": T.NEEDS_INPUT,
                       "detail": T.NEEDS_USER_PREFIX + step["action"]}]}
    other = dict(rec, id="tsk_key", needs_user={"action": "add the APNs key", "why": "",
                                                 "release": ""})
    held = dict(rec, id="tsk_held", needs_user=None, title="Held task",
                events=[{"at": now - 60, "kind": T.NEEDS_INPUT,
                         "detail": "not run: cadence needs a test command"}])

    check("a board card carries the step", board.card(rec).get("needs_user") == step)

    line = json.dumps(home.render([rec], now=now), ensure_ascii=False)
    # With its reason: the parking event says the command too, so the command
    # alone would pass on a line that read the event and not the step.
    check("the Slack board line names the step, and why",
          "yours to do: !release cadence all minor \u2014 releasing is user-triggered" in line,
          line[:300])
    acts = [b[1] for b in home.buttons_for(rec)]
    check("and offers Release and Done ahead of Answer and Dismiss",
          acts == ["release", "resolve", "answer", "dismiss"], str(acts))
    check("a step that is not a release offers Done, not Release",
          [b[1] for b in home.buttons_for(other)] == ["resolve", "answer", "dismiss"])
    check("a task held for another reason is offered what it always was",
          home.buttons_for(held) == home.BUTTONS[T.NEEDS_INPUT])
    check("the Release confirmation names the exact command",
          "!release cadence all minor" in json.dumps(home.confirm_modal(rec, "release")))
    check("both are actions the Slack board relays", {"release", "resolve"} <= set(home.DIRECT))

    text = digest.render([rec, held], now)
    check("the digest lists it under yours to do, with the command",
          "yours to do" in text and "!release cadence all minor" in text, text)
    check("and not also as held, which is for the system's own holds",
          "held: yours to do" not in text and "held: not run" in text, text)

    js = _re.search(r"<script>(.*?)</script>", V.PAGE, _re.S).group(1)
    esc = _re.search(r"const esc = .*", js).group(0)
    fns = "".join(js[js.index(f"function {n}("):js.index("\n}\n", js.index(f"function {n}(")) + 3]
                  for n in ("taskButtons", "yoursToDo"))
    harness = (esc + "\n" + fns + "\nprocess.stdout.write(JSON.stringify({"
               "rel: taskButtons(JSON.parse(process.env.REL)) + yoursToDo(JSON.parse(process.env.REL)),"
               "key: taskButtons(JSON.parse(process.env.KEY)),"
               "held: taskButtons(JSON.parse(process.env.HELD)) + yoursToDo(JSON.parse(process.env.HELD)),"
               "done: yoursToDo(Object.assign(JSON.parse(process.env.REL), {state: 'done'})),"
               "failed: yoursToDo(Object.assign(JSON.parse(process.env.REL), {state: 'failed'}))}));")
    try:
        out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30,
                             env={**os.environ, "REL": json.dumps(rec), "KEY": json.dumps(other),
                                  "HELD": json.dumps(held)})
        got = json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError):
        print("    (node unavailable — the dashboard is checked by the board payload only)")
        return
    except ValueError:
        check("the dashboard's renderers run", False, out.stderr[-300:])
        return
    check("the dashboard shows the step on the card",
          "yours to do" in got["rel"] and "!release cadence all minor" in got["rel"], got["rel"])
    check("with a Release button for a release",
          "releaseStep('tsk_rel')" in got["rel"] and "taskAction('tsk_rel','resolve')" in got["rel"])
    check("and only Done for anything else",
          "releaseStep" not in got["key"] and "taskAction('tsk_key','resolve')" in got["key"])
    check("a task held for another reason has neither",
          "releaseStep" not in got["held"] and "resolve" not in got["held"]
          and "yours to do" not in got["held"])
    check("a finished or failed task no longer shows it", got["done"] == got["failed"] == "")
    rs = js[js.index("async function releaseStep("):js.index("\n}\n", js.index("async function releaseStep("))]
    check("the Release button confirms, then sends only the task id (and its round)",
          "confirm(" in rs and 'taskAction(id, "release", "", round)' in rs)


def test_taking_the_step_from_the_board():
    print("\nRelease and Done close a task only when the step is taken")
    import tasks as T
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    said, ran, landings = [], [], []
    state = _task_state_for(st)
    started = {"ok": None}

    def release_command(arg, post, then=None):
        ran.append(arg)
        if arg.startswith("busy"):
            return "A release of `busy` is already running."
        started["then"] = then
        return ":rocket: Releasing `cadence` (everything pending, minor)…"
    ns = lift("handle_tasks", "approve_task", task_store=st, tasks=T,
                       holding=__import__("holding"), task_state=state,
                       release_command=release_command, RELEASE_STARTED=":rocket:",
                       LANDING_UNDERWAY="in-progress", merge=__import__("merge"),
                       roles=__import__("roles"),
                       tell_thread=lambda key, text: said.append(text),
                       stop_task=lambda tid: False,
                       start_landing=lambda tid, **k: landings.append((tid, k)),
              absent=TASK_ROUTES)
    route = ns["handle_tasks"]

    def parked(asks, project="cadence"):
        t = st.create("Cut new test flight build", role="implementor", project=project,
                      thread="C1:1.0")
        st.transition(t["id"], T.RUNNING)
        st.update(t["id"], needs_user=asks)
        st.transition(t["id"], T.NEEDS_INPUT, "yours")
        return t["id"]

    rel = {"action": "!release cadence all minor", "why": "", "release": "cadence all minor"}
    a = parked(rel)
    r = route({"action": "release", "id": a, "by": "you"})
    check("Release runs the recorded command, nothing else",
          r["ok"] and ran == ["cadence all minor"], str(r))
    check("the task waits while the release is going", st.get(a)["state"] == T.NEEDS_INPUT)
    started["then"](False)
    check("a release that does not go out leaves it waiting on you",
          st.get(a)["state"] == T.NEEDS_INPUT)
    started["then"](True)
    check("one that does closes it as done, saying how",
          st.get(a)["state"] == T.DONE and "released via you" in st.get(a)["events"][-1]["detail"])
    check("and its thread is told what was run", any("!release cadence all minor" in x for x in said))

    b = parked(dict(rel, release="busy all minor"), project="busy")
    r = route({"action": "release", "id": b})
    check("a release that could not start is refused with its reason",
          not r["ok"] and "already running" in r["error"] and st.get(b)["state"] == T.NEEDS_INPUT)
    # Recorded before the slug was checked at parse time, or written some
    # other way: the board still refuses to run another project's release.
    x = parked(dict(rel, action="!release trader all", release="trader all"))
    r = route({"action": "release", "id": x, "by": "you"})
    check("a release of another project than the task's is refused, nothing run",
          not r["ok"] and "not a release of this task's project" in r["error"]
          and ran[-1] == "busy all minor" and st.get(x)["state"] == T.NEEDS_INPUT, str(r))
    y = parked(rel, project="")
    r = route({"action": "release", "id": y})
    check("as is any release from a task with no project",
          not r["ok"] and ran[-1] == "busy all minor" and st.get(y)["state"] == T.NEEDS_INPUT, str(r))
    c = parked({"action": "add the APNs key", "why": "", "release": ""})
    r = route({"action": "release", "id": c})
    check("a step that is not a release cannot be run", not r["ok"] and ran[-1] == "busy all minor")
    r = route({"action": "resolve", "id": c, "by": "you"})
    check("Done closes it, attributed",
          r["ok"] and st.get(c)["state"] == T.DONE and "done via you" in st.get(c)["events"][-1]["detail"])
    q = st.create("queued work")["id"]
    check("neither acts on a task that is not waiting on you",
          not route({"action": "resolve", "id": q})["ok"]
          and not route({"action": "release", "id": q})["ok"] and st.get(q)["state"] == T.QUEUED)

    # Approving flagged work does not take the step for you either.
    d = st.create("flagged, with a step", role="implementor", thread="C1:1.0")["id"]
    st.transition(d, T.RUNNING); st.update(d, needs_user=rel)
    st.transition(d, T.AWAITING_APPROVAL, "flagged")
    r = route({"action": "approve", "id": d})
    check("approving work that left you a step parks it on that step",
          r["ok"] and st.get(d)["state"] == T.NEEDS_INPUT
          and st.get(d)["events"][-1]["detail"].startswith(T.NEEDS_USER_PREFIX),
          str(st.get(d)["state"]))
    check("and still lands it, as approving always does",
          landings == [(d, {"approved": True})], str(landings))
    r = route({"action": "approve", "id": d})
    check("a second Approve does not close it past its step",
          not r["ok"] and st.get(d)["state"] == T.NEEDS_INPUT, str(r))

    # Not while the branch it approved is still landing, or was refused.
    for stage, eligible, landed in (("in-progress", False, False), ("rebase", True, False)):
        e = parked(rel)
        st.update(e, result={"landing": {"stage": stage, "eligible": eligible,
                                         "landed": landed}})
        before = len(ran)
        r = route({"action": "release", "id": e})
        check(f"Release waits for a landing that is {stage}",
              not r["ok"] and "has not landed" in r["error"] and len(ran) == before, str(r))
    e = parked(rel)
    st.update(e, result={"landing": {"stage": "merged", "eligible": True, "landed": True}})
    check("and runs once the branch has landed", route({"action": "release", "id": e})["ok"])

    # A release outlives the click: the task may have moved on by its end.
    f = parked(rel)
    route({"action": "release", "id": f})
    late = started["then"]
    r = route({"action": "rework", "id": f, "notes": "actually, bump it to major"})
    check("sending it back clears the step it was waiting on",
          r["ok"] and st.get(f)["state"] == T.QUEUED and st.get(f).get("needs_user") is None,
          str(st.get(f).get("needs_user")))
    st.transition(f, T.RUNNING, "claimed")
    late(True)
    check("a release finishing after that does not close the rerun",
          st.get(f)["state"] == T.RUNNING, st.get(f)["state"])
    g = parked(rel)
    st.transition(g, T.QUEUED, "answered")
    check("nor does Done on a task no longer waiting on you",
          not route({"action": "resolve", "id": g})["ok"] and st.get(g)["state"] == T.QUEUED)


# --- a person may not send back, requeue or close a task mid-run -------------
# On 2026-10-07 Send back with notes reached tsk_3172171f8b a minute after the
# runner claimed it. running -> queued is legal -- it is how the runner sends
# back its own failing work -- so the route requeued a task mid-run, and that
# run never saw the notes.

def test_a_running_task_refuses_a_persons_send_back():
    print("\na running task refuses a person's send-back, the runner's still work")
    from unittest.mock import MagicMock
    import tasks as T
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    state = _bot_func("task_state", task_store=st, tasks=T, log=logging.getLogger("t"))
    ns = lift("handle_tasks", "approve_task", task_store=st, tasks=T,
                       holding=__import__("holding"), stop_task=lambda tid: True,
                       start_landing=lambda tid: None, tell_thread=lambda *a: None,
              absent=TASK_ROUTES + STEP_ROUTES)
    route = ns["handle_tasks"]

    def running(goal="the job"):
        t = st.create(goal, driver="queue")
        st.transition(t["id"], T.RUNNING, "claimed")
        return t["id"]

    tid = running()
    r = route({"action": "rework", "id": tid, "notes": "also fix the footer", "by": "you"})
    check("Send back on a running task is refused, saying what to do instead",
          not r["ok"] and r["error"] == T.RUNNING_REFUSAL, str(r))
    t = st.get(tid)
    check("and changes nothing: still running, goal untouched, no event",
          t["state"] == T.RUNNING and t["goal"] == "the job"
          and t["events"][-1]["kind"] == T.RUNNING, str(t["state"]))
    for action in ("retry", "accept", "approve"):
        r = route({"action": action, "id": tid})
        check(f"{action} on a running task is refused too",
              not r["ok"] and "it is running" in r["error"]
              and st.get(tid)["state"] == T.RUNNING, str(r))
    r = route({"action": "cancel", "id": tid})
    check("Stop still works on a running task",
          r["ok"] and st.get(tid)["state"] == T.CANCELLED, str(r))

    # The same refusal is asked under the store's lock, not only before it.
    racing = running()
    try:
        st.transition(racing, T.QUEUED, "x", refuse_running=T.RUNNING_REFUSAL,
                      fields={"goal": "changed"})
        refused = False
    except T.NotEditable:
        refused = True
    check("the store refuses the move itself, writing none of its fields",
          refused and st.get(racing)["goal"] == "the job" and st.get(racing)["state"] == T.RUNNING)
    try:
        st.transition(racing, T.NEEDS_INPUT, "approved", expect=T.AWAITING_APPROVAL)
        moved = True
    except T.InvalidTransition:
        moved = False
    check("and a move decided on a state it has since left is refused under the lock",
          not moved and st.get(racing)["state"] == T.RUNNING)

    # After it finishes, the send-back goes through with the notes attached.
    st.transition(racing, T.AWAITING_APPROVAL, "flagged")
    r = route({"action": "rework", "id": racing, "notes": "also fix the footer"})
    check("sent back once it has finished, the notes are on the goal",
          r["ok"] and st.get(racing)["state"] == T.QUEUED
          and "also fix the footer" in st.get(racing)["goal"], str(r))

    # The runner's own send-backs, from running and from a review, still requeue.
    app = MagicMock()
    for_tests = _bot_func("send_back_for_tests", task_store=st, tasks=T, task_state=state,
                          app=app, verify=__import__("verify"), MAX_VERIFY_ATTEMPTS=2)
    mid = running()
    for_tests(st.get(mid), {"ok": False, "ran": True, "output": "1 failed"}, "C1", "1.0")
    check("failing tests still send a running task back",
          st.get(mid)["state"] == T.QUEUED and "1 failed" in st.get(mid)["goal"],
          st.get(mid)["state"])
    send_back = _bot_func("send_back", task_store=st, tasks=T)
    conflict = _bot_func("rework_conflict", task_store=st, tasks=T, send_back=send_back,
                         _unsupervised=lambda t: True, CONFLICT_STAGES=("rebase",),
                         MAX_CONFLICT_REWORKS=1,
                         conflict_addendum=lambda *a: "catch up with main",
                         tell_thread=lambda *a: None)
    parked = running()
    st.transition(parked, T.BLOCKED, "awaiting review")
    sent = conflict(parked, {"stage": "rebase", "branch": "silkworm/x"}, "C1", "1.0")
    check("a landing conflict still sends its task back",
          sent and st.get(parked)["state"] == T.QUEUED
          and "catch up with main" in st.get(parked)["goal"], st.get(parked)["state"])
    own = running()
    try:
        send_back(own, "the runner's own note", "sent back")
    except (T.NotEditable, T.InvalidTransition):
        pass                        # reported by the check below, not raised
    check("and send_back itself, unmarked, still requeues a running task",
          st.get(own)["state"] == T.QUEUED and "the runner's own note" in st.get(own)["goal"])


# --- research: findings you review and decide on --------------------------------
# Research was filed as `assistant` on the queue and finished `done`, which is
# off the board, so its findings sat unread in a thread with no way to turn
# them into next steps -- and a scheduled wake-up is a queued `assistant` too,
# so the role could not tell the two apart. `researcher` parks its findings in
# needs_input, files its next steps as proposed children, and the board offers
# Close, Dig deeper and Discuss.


def test_researcher_is_a_role_you_can_file():
    import roles as R, scoping as S, tasks as T
    print("\nresearcher is a role a person can file, everywhere a role is chosen")
    check("it is a role", "researcher" in R.ROLES and R.known("researcher"))
    check("anyone may file it", R.validate_filed("researcher") == "")
    check("and the form says what it is for",
          "review and decide" in R.FILEABLE.get("researcher", ""), R.FILEABLE)
    check("it runs as assistant does: unrestricted, unreviewed, resumable",
          not R.is_restricted("researcher") and not R.needs_review("researcher")
          and not R.is_fresh("researcher") and R.asks_user("researcher")
          and R.permission_args("researcher", ["--dangerously-skip-permissions"])
          == R.permission_args("assistant", ["--dangerously-skip-permissions"]))
    prompt = R.system_prompt("researcher", bin="/opt/sw/bin/silkworm")
    check("its prompt asks for the fixed findings shape",
          all(k in prompt for k in ("Answer:", "Confidence:", "Key evidence:",
                                    "Next steps:", "2-4", "standalone")), prompt[:200])
    check("and says how to file next steps, with the real binary",
          '/opt/sw/bin/silkworm task "' in prompt and "{bin}" not in prompt)
    check("and to say which were not filed", "not filed" in prompt)
    check("other roles' prompts are untouched by the bin substitution",
          R.system_prompt("implementor", bin="/x") == R.IMPLEMENTOR_SYSTEM
          and R.system_prompt("assistant", bin="/x") == "")
    check("the in-turn filing instructions offer it, with what it means",
          "implementor|assistant|researcher" in S.HOW_TO
          and "findings you will review and decide on" in S.HOW_TO)
    check("a record may be made with it", T.make("Why is the cache stale?",
                                                 role="researcher")["role"] == "researcher")

    cli_file_task, sent = _cli_file_task_impl()
    env_was = dict(os.environ)
    try:
        os.environ.update(SILKWORM_THREAD="C1:1.0", SILKWORM_ROLE="assistant")
        with contextlib.redirect_stdout(io.StringIO()):
            cli_file_task(["--role", "researcher", "Find out why the cache goes stale"])
    finally:
        os.environ.clear(); os.environ.update(env_was)
    check("the CLI files --role researcher", sent and sent[-1][1].get("role") == "researcher",
          str(sent[-1][1] if sent else None))
    src = (BASE / "bin" / "silkworm").read_text()
    check("and its usage line names it", "implementor|assistant|researcher" in src)
    file_task, _filed, _begin, ts, _made = _file_task_impl()
    r = file_task({"key": "C9:9.0", "goal": "Find out why the cache goes stale",
                   "role": "researcher"})
    check("and the bot accepts it", r.get("ok") and r.get("role") == "researcher"
          and ts.get(r["id"])["role"] == "researcher", str(r))


def test_research_completion_parks_findings():
    print("\na completed research run waits for you; assistant and wake-ups do not")
    import threading
    import research
    import roles
    import tasks as T
    check("ending: research parks its findings",
          T.ending({"role": "researcher", "source": "dashboard"})
          == (T.NEEDS_INPUT, T.FINDINGS_DETAIL))
    check("ending: an assistant is done, as before",
          T.ending({"role": "assistant", "source": "dashboard"}) == (T.DONE, ""))
    check("ending: a wake-up is done, whatever role it carries",
          T.ending({"role": "researcher", "source": "defer"}) == (T.DONE, "")
          and T.ending({"role": "assistant", "source": "defer"}) == (T.DONE, ""))
    check("ending: a conversation turn is done",
          T.ending({"role": "researcher", "source": "slack"}) == (T.DONE, ""))

    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    root = Path(tempfile.mkdtemp())
    told, prompts = [], []
    state = _task_state_for(st)

    def run_turn(goal, **kw):
        prompts.append(kw.get("append_system_prompt") or "")
        return types.SimpleNamespace(text=_FINDINGS, cost_usd=0.1,
                                     duration_ms=1, session_id="s")
    fn = _bot_func("execute_task", tasks=T, task_store=st, store=tmp_store(),
                   roles=roles, research=research, Path=Path, run_turn=run_turn,
                   review_branch=lambda t: "", worktrees=__import__("worktrees"),
                   OUTBOX_ROOT=root / "outbox", SILKWORM_BIN="/x/silkworm",
                   permission_args=lambda: [], log=logging.getLogger("test"),
                   task_thread=lambda t: ("C1", "1.0"), task_key=lambda t: "C1:1.0",
                   task_state=state, tell_thread=lambda key, text: told.append((key, text)),
                   defer=__import__("defer"),
                   _thread_lock=lambda key: threading.Lock(),
                   repo_guard=lambda *a, **k: contextlib.nullcontext(),
                   render_block=lambda _: "", chunk=lambda text: [text],
                   to_mrkdwn=lambda text: text, resolve_review=lambda *a, **k: False,
                   upload_outbox=lambda *a, **k: [], RUNNING={}, RUNNING_TASKS={},
                   COST_NOTE="(list)", ClaudeStopped=ClaudeStopped, ClaudeError=ClaudeError)

    def run(role, **fields):
        rec = st.create("Why does the cache go stale after deploys?", role=role,
                        project="alpha", thread="C1:1.0", driver="queue", isolate=False,
                        scope={"cwd": str(root)}, **fields)
        st.transition(rec["id"], T.RUNNING, "claimed")
        fn(st.get(rec["id"]))
        return st.get(rec["id"])

    r = run("researcher", source="dashboard")
    check("a completed researcher task is in needs_input, not done",
          r["state"] == T.NEEDS_INPUT, r["state"])
    check("parked by a transition, with its reason",
          r["events"][-1]["kind"] == T.NEEDS_INPUT
          and r["events"][-1]["detail"] == T.FINDINGS_DETAIL, str(r["events"][-1]))
    check("with its findings on the record", (r.get("result") or {}).get("text") == _FINDINGS)
    check("it is in front of you", r["id"] in [t["id"] for t in st.needs_attention()])
    check("its thread is told what to do with them",
          any("Findings ready" in t and "Dig deeper" in t for _, t in told), str(told[-1:]))
    check("the run was given the research prompt, with the real binary",
          "*Answer:*" in prompts[-1] and '/x/silkworm task "' in prompts[-1])
    a = run("assistant", source="dashboard")
    check("an assistant task still finishes done", a["state"] == T.DONE, a["state"])
    check("and was not given the research prompt", "*Answer:*" not in prompts[-1])
    w = run("assistant", source="defer")
    check("a wake-up still finishes done", w["state"] == T.DONE, w["state"])
    w2 = run("researcher", source="defer")
    check("even one on a research thread", w2["state"] == T.DONE, w2["state"])

    bot = (BASE / "bot.py").read_text()
    tree = ast.parse(bot)
    users = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)
             and any(isinstance(c, ast.Attribute) and c.attr == "ending"
                     and isinstance(c.value, ast.Name) and c.value.id == "tasks"
                     for c in ast.walk(n))}
    check("an inline Slack turn never asks how a task ends",
          "handle_prompt" not in users and users <= {"execute_task", "resolve_review"},
          str(users))


def test_research_next_steps_are_proposed_children():
    import research, roles, tasks as T, scoping as S
    print("\na research run's next steps are proposals under it, capped and deduped")
    file_task, filed_this_turn, begin_turn, ts, _made = _file_task_impl()
    key = "C5:5.0"
    rt = ts.create("Why does the cache go stale after deploys?", role="researcher",
                   project="alpha", thread=key, driver="queue")
    ts.transition(rt["id"], T.RUNNING, "claimed")
    rid = rt["id"]
    was = os.environ.get("MAX_OPEN_PROPOSALS")
    os.environ["MAX_OPEN_PROPOSALS"] = "6"
    try:
        r = file_task({"key": key, "goal": "Add a TTL to the deploy cache entries in cache.py",
                       "caller_role": "researcher", "caller_task": rid,
                       "project": "beta", "role": "implementor"})
        kid = ts.get(r.get("id") or "") or {}
        check("a next step is not refused as a duplicate of the research it answers",
              r.get("ok") and not r.get("duplicate"), str(r))
        check("a next step is filed as a proposal, though --propose was not passed",
              r.get("ok") and r.get("state") == T.PROPOSED and kid.get("state") == T.PROPOSED,
              str(r))
        check("as a child of the research task",
              kid.get("parent") == rid and kid.get("root") == rid
              and kid.get("source") == research.SOURCE and kid.get("source_ref") == rid,
              str({k: kid.get(k) for k in ("parent", "root", "source", "source_ref")}))
        check("in the research task's project, not the one asked for",
              kid.get("project") == "alpha", kid.get("project"))
        check("with the research budget left, not the nightly one",
              r.get("remaining") == S.MAX_RESEARCH_STEPS - 1, str(r))
        # A researcher with no task id in its environment: the one running here.
        r2 = file_task({"key": key, "goal": "Invalidate the CDN on every deploy of the site",
                        "caller_role": "researcher"})
        kid2 = ts.get(r2.get("id") or "") or {}
        check("a researcher's filing without its task id is still a child of the one running",
              r2.get("ok") and kid2.get("state") == T.PROPOSED and kid2.get("parent") == rid,
              str(r2))
        # The prompt asks for 2-4 next steps; under the nightly pass's budget
        # of two, steps three and four were always refused.
        r3 = file_task({"key": key, "goal": "Write a runbook for clearing stale caches by hand",
                        "caller_role": "researcher", "caller_task": rid})
        r3b = file_task({"key": key, "goal": "Alert when the build manifest hash changes unexpectedly",
                         "caller_role": "researcher", "caller_task": rid})
        check("a research run may file its third and fourth next steps",
              r3.get("ok") and r3b.get("ok") and r3b.get("remaining") == 0,
              str([r3, r3b]))
        check("the research budget is wider than the nightly pass's",
              S.MAX_RESEARCH_STEPS == 4 > S.MAX_PROPOSALS
              and S.limit_for(True, research=True) == S.MAX_RESEARCH_STEPS)
        r3c = file_task({"key": key, "goal": "Document the deploy ordering for cache warmers",
                         "caller_role": "researcher", "caller_task": rid})
        check("but a fifth is refused by the per-run budget",
              not r3c.get("ok") and "limit for one research run" in r3c.get("error", ""),
              str(r3c))
        held = ts.get(rid).get("next_steps_held") or []
        check("and what it refused is kept on the research task, with why",
              len(held) == 1 and "deploy ordering" in held[0]["goal"]
              and "limit" in held[0]["why"], str(held))
        begin_turn(key)
        r4 = file_task({"key": key, "goal": "Add a TTL to the deploy cache entries in cache.py",
                        "caller_role": "researcher", "caller_task": rid})
        check("a duplicate of an open proposal is refused by the existing dedup",
              not r4.get("ok") and r4.get("duplicate"), str(r4))
        check("and kept as not filed too",
              len(ts.get(rid).get("next_steps_held") or []) == 2)
        # The standing cap: six open proposals in alpha now.
        ts.create("Something else entirely about logging levels", project="alpha",
                  state=T.PROPOSED)
        ts.create("Rotate the metrics database credentials yearly", project="alpha",
                  state=T.PROPOSED)
        r5 = file_task({"key": key, "goal": "Measure cache hit rate before and after deploys",
                        "caller_role": "researcher", "caller_task": rid})
        check("the project's open-proposal cap applies to it",
              not r5.get("ok") and str(S.max_open_proposals()) in r5.get("error", "")
              and "already waiting" in r5.get("error", ""), str(r5))
        check("so nothing was filed past the cap",
              S.open_proposals(ts.by_project("alpha")) == 6)
        check("and the cap's refusal is kept on the research task",
              "already waiting" in (ts.get(rid)["next_steps_held"][-1]["why"]))
    finally:
        if was is None:
            os.environ.pop("MAX_OPEN_PROPOSALS", None)
        else:
            os.environ["MAX_OPEN_PROPOSALS"] = was
    # A Slack turn on the research thread (Discuss) while a Dig deeper is
    # claimed and waiting for the thread: it is not the research filing.
    begin_turn(key)
    inline = file_task({"key": key, "goal": "Build the importer for the new feed format",
                        "role": "implementor", "caller_role": "assistant"})
    check("a conversation turn on the research thread files as it always did",
          inline.get("ok") and inline.get("state") == T.QUEUED
          and not ts.get(inline["id"]).get("parent"), str(inline))
    other = file_task({"key": "C6:6.0", "goal": "Build the importer for the old feed format",
                       "role": "implementor", "caller_role": "assistant"})
    check("a filing from any other thread is untouched: queued, no parent",
          other.get("ok") and other.get("state") == T.QUEUED
          and not ts.get(other["id"]).get("parent"), str(other))
    kids = research.children(rid, ts.all().values())
    check("the children are listed under the research task",
          [k["id"] for k in kids] == [r["id"], r2["id"], r3["id"], r3b["id"]], str(kids))
    check("the researcher's prompt asks for no more next steps than one run may file",
          "2-%d concrete" % S.MAX_RESEARCH_STEPS in roles.RESEARCHER_SYSTEM
          and "at most four" in roles.RESEARCHER_SYSTEM)


def test_research_children_show_on_the_board():
    import board, home, research, tasks as T
    print("\na research task's next steps show on its board row and detail")
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    rid = _research_parked(st)
    kid = st.create("Add a TTL to the deploy cache entries", project="alpha",
                    state=T.PROPOSED, parent=rid, root=rid, source=research.SOURCE)
    st.update(rid, next_steps_held=[research.held_entry("Write a runbook", "limit reached", 1)])
    recs = list(st.all().values())
    b = board.board(recs, "alpha")
    card = next(c for c in b["columns"]["needs"] if c["id"] == rid)
    check("its card says it is waiting on you with findings",
          card["findings"] is True and board.card(st.get(kid["id"]))["findings"] is False)
    check("its card lists the child and the one held",
          [k["id"] for k in card["research"]["children"]] == [kid["id"]]
          and card["research"]["held"][0]["why"] == "limit reached"
          and "1 next step filed" in card["research"]["line"]
          and "1 not filed" in card["research"]["line"], str(card["research"]))
    check("and carries its thread as a slacklinks link",
          card["thread_link"] == __import__("slacklinks").for_key("C1:1.0"))
    d = board.detail(st.get(rid), recs)
    check("so does its detail", [k["id"] for k in d["research"]["children"]] == [kid["id"]])
    check("an ordinary task's card has no research section",
          board.card(st.get(kid["id"]))["research"] is None)
    view = json.dumps(home.render([dict(r) for r in recs], now=time.time(),
                                  base_url="https://x.slack.com"), ensure_ascii=False)
    check("the Slack board shows the answer and the next step",
          "the cache is never invalidated on deploy" in view and kid["id"] in view
          and "not filed" in view, view[:400])
    line = json.dumps(home.render([dict(r) for r in recs], now=time.time(),
                                  base_url="https://x.slack.com"), ensure_ascii=False)
    check("and its one-line form counts them", "1 next step filed" in line, line[:400])


def test_findings_actions_from_the_board():
    print("\nClose and Dig deeper move research only from where you saw it")
    import research, tasks as T
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    state = _task_state_for(st)
    ns = lift("handle_tasks", "decide_findings", task_store=st, tasks=T,
                       research=research, holding=__import__("holding"), task_state=state,
                       merge=__import__("merge"), roles=__import__("roles"),
                       tell_thread=lambda *a: None, stop_task=lambda tid: False,
                       start_landing=lambda *a, **k: None,
              absent=TASK_ROUTES + STEP_ROUTES)
    route = ns["handle_tasks"]

    a = _research_parked(st)
    r = route({"action": "close", "id": a, "by": "you"})
    check("Close: needs_input -> done, attributed",
          r["ok"] and st.get(a)["state"] == T.DONE
          and "findings closed via you" in st.get(a)["events"][-1]["detail"], str(r))
    r = route({"action": "close", "id": a})
    check("a second Close on a closed task is refused, not reapplied",
          not r["ok"] and st.get(a)["state"] == T.DONE, str(r))
    check("saying it is no longer waiting on you, and where it is",
          "no longer waiting on you" in r.get("error", "") and "done" in r["error"], str(r))
    r = route({"action": "dig", "id": a, "notes": "and the CDN?"})
    check("as is Dig deeper on one", not r["ok"] and st.get(a)["state"] == T.DONE)

    b = _research_parked(st)
    st.update(b, next_steps_held=[research.held_entry("a runbook", "limit", 1)])
    r = route({"action": "dig", "id": b, "notes": "   "})
    check("Dig deeper with no question is refused and changes nothing",
          not r["ok"] and st.get(b)["state"] == T.NEEDS_INPUT, str(r))
    r = route({"action": "dig", "id": b, "notes": "Does the CDN cache it too?", "by": "you"})
    rec = st.get(b)
    check("Dig deeper: needs_input -> queued, with the question appended",
          r["ok"] and rec["state"] == T.QUEUED
          and rec["goal"].endswith("Does the CDN cache it too?")
          and rec["goal"].startswith("Why does the cache go stale"), rec["goal"][-200:])
    check("through the send-back mechanism (queued for the runner, nothing stale kept)",
          rec["driver"] == "queue" and rec.get("needs_user") is None
          and rec.get("checkpoint") is None and rec.get("next_steps_held") == [])
    r = route({"action": "dig", "id": b, "notes": "again?"})
    check("a second Dig deeper on the requeued task is refused",
          not r["ok"] and st.get(b)["goal"].count("Dig deeper") == 1, str(r))
    st.transition(b, T.RUNNING, "claimed")
    for act in ("close", "dig"):
        r = route({"action": act, "id": b, "notes": "x?"})
        check(f"{act} refuses a running task, cleanly",
              not r["ok"] and "running" in r["error"] and st.get(b)["state"] == T.RUNNING, str(r))

    impl = st.create("Cut a build", role="implementor", project="alpha")["id"]
    st.transition(impl, T.RUNNING)
    st.update(impl, needs_user={"action": "add the key", "why": "", "release": ""})
    st.transition(impl, T.NEEDS_INPUT, "yours")
    for act in ("close", "dig"):
        r = route({"action": act, "id": impl, "notes": "x?"})
        check(f"{act} is only for research", not r["ok"] and st.get(impl)["state"] == T.NEEDS_INPUT)

    # A click decided on a snapshot that has since moved: the store's lock, not
    # the snapshot, decides. Running is refused (running -> done and -> queued
    # are both legal, so only refuse_running stops it), and so is anywhere else
    # the move would be legal from (blocked -> done and -> queued).
    class Stale:
        def __init__(self, real, snap):
            self.real, self.snap = real, snap
        def get(self, tid):
            return dict(self.snap)
        def __getattr__(self, name):
            return getattr(self.real, name)
    for real_state in (T.RUNNING, T.BLOCKED):
        for act in ("close", "dig"):
            c = _research_parked(st)
            snap = st.get(c)
            st.transition(c, T.QUEUED, "dug elsewhere")
            st.transition(c, T.RUNNING, "claimed")
            if real_state == T.BLOCKED:
                st.transition(c, T.BLOCKED, "quota")
            ns["task_store"] = Stale(st, snap)
            try:
                r = route({"action": act, "id": c, "notes": "x?"})
            finally:
                ns["task_store"] = st
            check(f"a stale {act} on a task now {real_state} is refused under the lock",
                  not r["ok"] and st.get(c)["state"] == real_state, f"{r} {st.get(c)['state']}")
            if real_state == T.RUNNING:
                check(f"and says it is running, not that the move is illegal",
                      r["error"] in (T.RUNNING_BUSY, T.RUNNING_REFUSAL), r["error"])


def test_findings_actions_on_the_slack_board():
    print("\nthe Slack board offers Close, Dig deeper and Discuss, and refuses stale clicks")
    import home, research, slacklinks, tasks as T
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    rid = _research_parked(st)
    rec = dict(st.get(rid), id=rid)
    acts = [b[1] for b in home.buttons_for(rec)]
    check("a research task with findings offers Close, Dig deeper, Dismiss",
          acts == ["close", "dig", "dismiss"], str(acts))
    check("with Release first when it also left a release",
          [b[1] for b in home.buttons_for(dict(rec, needs_user={
              "action": "!release alpha all", "why": "", "release": "alpha all"}))]
          == ["release", "close", "dig", "dismiss"])
    check("an ordinary needs_input task is offered what it always was",
          home.buttons_for(dict(rec, role="implementor")) == home.BUTTONS[T.NEEDS_INPUT])
    check("Close is a relayed action with a confirmation",
          "close" in home.DIRECT
          and home.confirm_modal(rec, "close")["submit"]["text"] == "Close findings")
    check("Dig deeper opens a window", "dig" in home.NOTES and "dig" not in home.DIRECT)
    line = home._task_line(rec, time.time(), "https://x.slack.com")
    opts = {o["value"].split("|")[0]: o for o in line["accessory"]["options"]}
    check("the menu has Discuss, linking the thread through slacklinks",
          opts.get("thread", {}).get("text", {}).get("text") == "Discuss"
          and opts["thread"]["url"] == slacklinks.for_key("C1:1.0", "https://x.slack.com"),
          str(opts.get("thread")))
    check("and every menu value carries the state it was drawn for, and which findings",
          all(o["value"].endswith(f"|{rid}|needs_input@{research.round_of(rec)}")
              for o in opts.values()) and research.round_of(rec), str(opts))
    modal = home.rework_modal(rec, False, dig=True)
    check("the Dig deeper window requires a question",
          modal["blocks"][1]["optional"] is False
          and json.loads(modal["private_metadata"])["dig"] is True)
    check("Close's confirmation shows the findings",
          "the cache is never invalidated" in json.dumps(home.confirm_modal(rec, "close")))

    calls, opened = [], []

    class Client:
        def views_open(self, trigger_id, view):
            opened.append(view)
    h = _board_for(st, lambda p: calls.append(p) or {"ok": True}, allowed_users=[])
    h.publish = lambda *a, **k: None
    body = lambda value: {"user": {"id": "U1"}, "trigger_id": "t",
                          "actions": [{"selected_option": {"value": value}}]}
    h.on_menu(lambda **k: None, body(f"dig|{rid}|needs_input"), Client())
    check("picking Dig deeper opens its window and does nothing yet",
          calls == [] and opened and json.loads(opened[-1]["private_metadata"]).get("dig"))
    acked = []
    view = dict(opened[-1], state={"values": {"notes": {"notes": {"value": ""}}}})
    h.on_submit(lambda **k: acked.append(k), {"user": {"id": "U1"}}, Client(), view)
    check("submitting it with no question is refused in the window, nothing sent",
          calls == [] and acked and acked[-1].get("response_action") == "errors")
    view = dict(opened[-1], state={"values": {"notes": {"notes": {"value": "the CDN?"}}}})
    h.on_submit(lambda **k: acked.append(k), {"user": {"id": "U1"}}, Client(), view)
    check("with one, it is sent as dig with the question",
          calls and calls[-1]["action"] == "dig" and calls[-1]["notes"] == "the CDN?", str(calls))
    st.transition(rid, T.DONE, "closed elsewhere")
    n = len(calls)
    h.on_submit(lambda **k: None, {"user": {"id": "U1"}}, Client(), view)
    h.on_menu(lambda **k: None, body(f"close|{rid}|needs_input"), Client())
    h.on_confirm(lambda **k: None, {"user": {"id": "U1"}}, Client(),
                 {"private_metadata": json.dumps({"action": "close", "id": rid,
                                                  "seen": "needs_input"})})
    check("a stale Close or Dig deeper, after it moved on, sends nothing",
          len(calls) == n, str(calls[n:]))


def test_findings_actions_on_the_dashboard():
    print("\nthe dashboard offers Close, Dig deeper and Discuss for research")
    sys.argv = ["x"]
    import visualizer as V
    js = _re.search(r"<script>(.*?)</script>", V.PAGE, _re.S).group(1)
    esc = _re.search(r"const esc = .*", js).group(0)

    def fn(name, prefix="function "):
        i = js.index(f"{prefix}{name}(")
        return js[i:js.index("\n}\n", i) + 3]
    harness = (esc + "\n" + fn("taskButtons") + fn("digDeeper", "async function ")
               + fn("findings") + """
let answers = [], sent = [];
function prompt() { return answers.shift(); }
function threadLink(k) { return "fallback:" + k; }
async function taskAction(id, action, notes, round) { sent.push([id, action, notes, round]); }
const R = JSON.parse(process.env.R);
(async () => {
  const out = {buttons: taskButtons(R), plain: taskButtons(Object.assign({}, R, {role: "assistant", findings: false})),
               findings: findings(R)};
  answers = [null]; await digDeeper("t1"); out.cancelled = sent.length;
  answers = ["", "  ", null]; await digDeeper("t1"); out.blankThenCancel = sent.length;
  answers = ["", "and the CDN?"]; await digDeeper("t1", "12.5"); out.sent = sent;
  process.stdout.write(JSON.stringify(out));
})();""")
    rec = {"id": "t1", "role": "researcher", "state": "needs_input", "thread": "C1:1.0",
           "findings": True, "round": "12.5",
           "thread_link": "https://slack.com/archives/C1/p10?thread_ts=1.0&cid=C1",
           "research": {"children": [{"id": "tsk_kid", "state": "proposed", "title": "Add a TTL"}],
                        "held": [{"goal": "Write a runbook", "why": "limit reached"}],
                        "line": "1 next step filed"}}
    try:
        out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30,
                             env={**os.environ, "R": json.dumps(rec)})
        got = json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError):
        print("    (node unavailable — dashboard not checked)")
        return
    except ValueError:
        check("the dashboard's research renderers run", False, out.stderr[-300:])
        return
    b = got["buttons"]
    check("Close, Dig deeper and Discuss are offered",
          "closeFindings('t1','12.5')" in b and "digDeeper('t1','12.5')" in b
          and "Discuss" in b, b)
    check("Discuss links the thread the server built with slacklinks",
          'href="https://slack.com/archives/C1/p10?thread_ts=1.0&amp;cid=C1"' in b, b)
    check("and Answer is not offered for research", "sendBack('t1',true)" not in b)
    check("an assistant in needs_input still gets Answer, not these",
          "sendBack('t1',true)" in got["plain"] and "closeFindings" not in got["plain"])
    check("cancelling the question cancels the action", got["cancelled"] == 0)
    check("a blank question is asked again, and cancelling then still sends nothing",
          got["blankThenCancel"] == 0)
    check("a question is sent as dig, with the round its button was drawn for",
          got["sent"] == [["t1", "dig", "and the CDN?", "12.5"]], str(got["sent"]))
    check("the row lists its next steps and the one not filed",
          "tsk_kid" in got["findings"] and "limit reached" in got["findings"], got["findings"])
    cf = js[js.index("async function closeFindings("):]
    cf = cf[:cf.index("\n}\n")]
    check("Close asks for confirmation before acting, and sends its round",
          "confirm(" in cf and cf.index("confirm(") < cf.index('taskAction(id, "close", "", round)'))
    ta = fn("taskAction", "async function ")
    check("taskAction puts the round in the request",
          "p.round = round" in ta and "taskCall(p)" in ta, ta[:300])


def test_findings_clicks_tell_rounds_apart():
    print("\na Close or Dig deeper drawn before a Dig deeper cannot act on the next round")
    import home, research, tasks as T
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    state = _task_state_for(st)
    ns = lift("handle_tasks", "decide_findings", "send_back", task_store=st,
                       tasks=T, research=research, holding=__import__("holding"),
                       task_state=state, merge=__import__("merge"),
                       roles=__import__("roles"), tell_thread=lambda *a: None,
                       stop_task=lambda tid: False, start_landing=lambda *a, **k: None,
              absent=TASK_ROUTES + STEP_ROUTES)
    route = ns["handle_tasks"]

    def repark(tid):
        st.transition(tid, T.RUNNING, "claimed")
        st.transition(tid, T.NEEDS_INPUT, T.FINDINGS_DETAIL)

    def dig_and_repark(tid, rnd):
        r = route({"action": "dig", "id": tid, "notes": "and the CDN?", "round": rnd})
        assert r["ok"], r
        time.sleep(0.002)
        repark(tid)

    a = _research_parked(st)
    first = research.round_of(st.get(a))
    check("a parked research task has a round", bool(first))
    check("which is not a state anything else carries",
          research.round_of(dict(st.get(a), role="implementor")) == "")
    dig_and_repark(a, first)
    second = research.round_of(st.get(a))
    check("parking again after a dig is a new round", second and second != first,
          f"{first} {second}")
    for act in ("close", "dig"):
        r = route({"action": act, "id": a, "notes": "x?", "round": first})
        check(f"a {act} drawn for the first round is refused on the second",
              not r["ok"] and r["error"] == research.STALE_ROUND
              and st.get(a)["state"] == T.NEEDS_INPUT
              and st.get(a)["goal"].count("Dig deeper") == 1, str(r))
    r = route({"action": "close", "id": a})
    check("one carrying no round is asked only the state, as before",
          r["ok"] and st.get(a)["state"] == T.DONE, str(r))

    b = _research_parked(st)
    rnd = research.round_of(st.get(b))
    r = route({"action": "close", "id": b, "round": rnd, "by": "you"})
    check("a Close for the round that is waiting closes it",
          r["ok"] and st.get(b)["state"] == T.DONE, str(r))

    # The early check reads a snapshot; the lock decides. A snapshot of the
    # first round (still needs_input) passes it, and the move must still refuse.
    class Stale:
        def __init__(self, real, snap):
            self.real, self.snap = real, snap
        def get(self, tid):
            return dict(self.snap)
        def __getattr__(self, name):
            return getattr(self.real, name)
    for act in ("close", "dig"):
        c = _research_parked(st)
        old = research.round_of(st.get(c))
        dig_and_repark(c, old)
        snap = dict(st.get(c))
        snap["events"] = [e for e in snap["events"]][:3]   # as it was at round one
        check("(the snapshot still reads as round one)",
              research.round_of(snap) == old, research.round_of(snap))
        ns["task_store"] = Stale(st, snap)
        try:
            r = route({"action": act, "id": c, "notes": "x?", "round": old})
        finally:
            ns["task_store"] = st
        check(f"a stale {act} that got past the snapshot is refused under the lock",
              not r["ok"] and st.get(c)["state"] == T.NEEDS_INPUT
              and st.get(c)["goal"].count("Dig deeper") == 1, str(r))
    import board as B
    check("board cards and the detail view carry the round the page sends back",
          B.card(st.get(c)).get("round") == research.round_of(st.get(c))
          == B.detail(st.get(c), list(st.all().values())).get("round"))
    w = lift("_with_costs", task_store=st, costs=__import__("costs"),
                      research=research, board=B, slacklinks=__import__("slacklinks"),
                      tasks=__import__("tasks"))
    check("as do the task panel's rows",
          w["_with_costs"]([st.get(c)])[0].get("round") == research.round_of(st.get(c)))

    # The Slack board: a button drawn before the dig is refused before anything
    # is sent; one drawn after carries the round through to the bot.
    d = _research_parked(st)
    drawn = home.seen_of(dict(st.get(d), id=d))
    check("a Slack button records the round", drawn == "needs_input@" + research.round_of(st.get(d)),
          drawn)
    dig_and_repark(d, home.seen_round(drawn))
    calls, notes = [], []
    h = _board_for(st, lambda p: calls.append(p) or {"ok": True}, allowed_users=[])
    h.note = lambda user, text: notes.append(text)
    h.publish = lambda *a, **k: None

    class Client:
        def views_open(self, trigger_id, view): pass
    h.on_confirm(lambda **k: None, {"user": {"id": "U1"}}, Client(),
                 {"private_metadata": json.dumps({"action": "close", "id": d, "seen": drawn})})
    view = {"private_metadata": json.dumps({"id": d, "dig": True, "answer": False, "seen": drawn}),
            "state": {"values": {"notes": {"notes": {"value": "the CDN?"}}}}}
    h.on_submit(lambda **k: None, {"user": {"id": "U1"}}, Client(), view)
    h.on_menu(lambda **k: None, {"user": {"id": "U1"}, "trigger_id": "t", "actions": [
        {"selected_option": {"value": f"close|{d}|{drawn}"}}]}, Client())
    check("a Close, Dig deeper or menu pick drawn before the dig sends nothing",
          calls == [] and st.get(d)["state"] == T.NEEDS_INPUT, str(calls))
    check("and says the findings are new, not that it moved on to the same state",
          notes and all("new findings" in n for n in notes), str(notes))
    now = home.seen_of(dict(st.get(d), id=d))
    h.on_confirm(lambda **k: None, {"user": {"id": "U1"}}, Client(),
                 {"private_metadata": json.dumps({"action": "close", "id": d, "seen": now})})
    check("one drawn for this round is sent, carrying it",
          calls and calls[-1]["action"] == "close"
          and calls[-1].get("round") == research.round_of(st.get(d)), str(calls))
    h.on_confirm(lambda **k: None, {"user": {"id": "U1"}}, Client(),
                 {"private_metadata": json.dumps({"action": "close", "id": d,
                                                  "seen": "needs_input"})})
    check("a value drawn before rounds were carried is asked the state only",
          len(calls) == 2 and "round" not in calls[-1], str(calls))


def test_release_and_dismiss_on_findings_tell_rounds_apart():
    print("\nRelease and Dismiss on research findings cannot act on a later round")
    import research, tasks as T
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    state = _task_state_for(st)
    started = {}

    def release_command(arg, post, then=None):
        started["then"] = then
        return ":rocket: Releasing `alpha`…"
    ns = lift("handle_tasks", "decide_findings", task_store=st, tasks=T,
                       research=research, holding=__import__("holding"), task_state=state,
                       release_command=release_command, RELEASE_STARTED=":rocket:",
                       LANDING_UNDERWAY="in-progress", merge=__import__("merge"),
                       roles=__import__("roles"), tell_thread=lambda *a: None,
                       stop_task=lambda tid: False, start_landing=lambda *a, **k: None,
              absent=TASK_ROUTES)
    route = ns["handle_tasks"]
    asks = {"action": "!release alpha all", "why": "", "release": "alpha all"}

    def parked():
        tid = _research_parked(st)
        st.update(tid, needs_user=asks)
        return tid

    def dig_and_repark(tid):
        r = route({"action": "dig", "id": tid, "notes": "and the CDN?"})
        assert r["ok"], r
        time.sleep(0.002)
        st.transition(tid, T.RUNNING, "claimed")
        st.update(tid, needs_user=asks)
        st.transition(tid, T.NEEDS_INPUT, T.FINDINGS_DETAIL)

    a = parked()
    old = research.round_of(st.get(a))
    dig_and_repark(a)
    for act in ("release", "resolve", "dismiss"):
        r = route({"action": act, "id": a, "round": old, "by": "you"})
        check(f"a stale {act} drawn for the first round is refused on the second",
              not r["ok"] and st.get(a)["state"] == T.NEEDS_INPUT and "then" not in started,
              str(r))
    r = route({"action": "dismiss", "id": a, "round": research.round_of(st.get(a))})
    check("a Dismiss for the round that is waiting dismisses it",
          r["ok"] and st.get(a)["state"] == T.CANCELLED, str(r))

    # A release started on round one; dug into while it ran; round two parks;
    # then the release goes out. It must not close round two as released.
    b = parked()
    r = route({"action": "release", "id": b, "round": research.round_of(st.get(b))})
    check("Release for the round that is waiting starts it", r["ok"] and started.get("then"), str(r))
    dig_and_repark(b)
    started["then"](True)
    check("the release finishing does not close the round the dig produced",
          st.get(b)["state"] == T.NEEDS_INPUT, st.get(b)["state"])
    c = parked()
    route({"action": "release", "id": c})
    started["then"](True)
    check("while one finishing on the round it started closes it",
          st.get(c)["state"] == T.DONE, st.get(c)["state"])

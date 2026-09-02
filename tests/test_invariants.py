"""Regression tests for the things that have actually broken.

Every case here corresponds to a real failure: a turn that hung for a day, a
thread that silently lost its history, a reply that was never delivered. They
run without Slack, without a bot, and without spending anything.

    python3 tests/test_invariants.py
"""

import ast
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import procs                                     # noqa: E402
import recovery                                  # noqa: E402
from claude_runner import (ClaudeError, ClaudeStopped,  # noqa: E402
                           ClaudeTimeout, RunHandle)
from store import SessionStore                   # noqa: E402

BASE = Path(__file__).resolve().parent.parent
PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'✔' if cond else '✘'} {name}" + (f" — {detail}" if detail and not cond else ""))


def tmp_store():
    return SessionStore(Path(tempfile.mkdtemp()) / "s.json")


# --- a transient error must never discard a thread's history ------------------
# A mid-turn API hiccup ("Connection closed mid-response") once wiped four
# threads: the retry path dropped the session entry and started fresh.

def test_resume_retry_requires_missing_transcript():
    src = (BASE / "bot.py").read_text()
    print("\ntransient errors must not discard a session")
    check("timeout/stop re-raise before the retry path",
          src.index("except (ClaudeStopped, ClaudeTimeout):") < src.index("has no transcript on disk"))
    check("fresh session only when the transcript is gone",
          "if session_id is None or harvester.find_transcript(session_id):" in src)
    # !reset drops the entry on purpose; the retry path must not.
    retry_block = src[src.index("except ClaudeError as e:"):]
    retry_block = retry_block[:retry_block.index("store.update(key, session_id=result.session_id")]
    check("retry path does not drop the entry", "store.drop" not in retry_block,
          "dropping it also destroys title, summary, cost and files")
    check("!reset is still allowed to drop", "store.drop(key)" in src)
    check("the replaced session id is remembered",
          "previous_sessions" in src)


# --- a hung child must always die --------------------------------------------
# A child ignored SIGTERM and ran for 24h, holding its thread's lock; every
# later message queued behind it forever.

def test_stop_escalates_to_sigkill():
    print("\nstop() escalates when SIGTERM is ignored")
    script = ("import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
              "print('r',flush=True)\ntime.sleep(120)\n")
    p = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                         text=True, start_new_session=True)
    p.stdout.readline()
    RunHandle(p).stop(grace=1.5)
    for _ in range(40):
        if p.poll() is not None:
            break
        time.sleep(0.1)
    check("child ignoring SIGTERM is killed", p.poll() is not None)
    check("killed via SIGKILL", p.poll() == -9, f"returncode {p.poll()}")

    fast = subprocess.Popen([sys.executable, "-c", "import time;print('r',flush=True);time.sleep(120)"],
                            stdout=subprocess.PIPE, text=True, start_new_session=True)
    fast.stdout.readline()
    t0 = time.time()
    RunHandle(fast).stop(grace=5.0)
    check("well-behaved child doesn't wait out the grace period", time.time() - t0 < 2.0)


def test_timeout_is_distinct():
    print("\ntimeout is its own exception")
    check("ClaudeTimeout is a ClaudeError", issubclass(ClaudeTimeout, ClaudeError))
    check("ClaudeTimeout is not ClaudeStopped", not issubclass(ClaudeTimeout, ClaudeStopped))


# --- recovery must never post a fragment as the answer ------------------------

def test_recovery():
    print("\nrecovery of an interrupted turn")
    tmp = Path(tempfile.mkdtemp())
    tp = tmp / "t.jsonl"
    entry = lambda ts, txt: json.dumps(          # noqa: E731
        {"type": "assistant", "timestamp": ts,
         "message": {"content": [{"type": "text", "text": txt}]}})
    tp.write_text("\n".join([entry("2026-08-01T12:00:30.000Z", "mid-turn narration"),
                             entry("2026-08-01T12:01:00.000Z", "FINAL REPLY")]) + "\n")
    recovery.find_transcript = lambda sid: tp
    recovery.POLL_S = 0.05
    pend = {"started": "2026-08-01T11:00:00.000Z", "msg_ts": "1.1",
            "progress_ts": "1.2", "session_id": "s"}

    class RX:
        def done(self): calls.append("done")
        def failed(self): calls.append("failed")

    store = tmp_store()
    store.update("C:1", session_id="s", pending=dict(pend))
    recovery._claude_alive = lambda sid: True
    calls = []
    st = recovery.recover(store, finalize=lambda *a: calls.append(a[3]),
                          reactions_for=lambda *a: RX(), say=lambda *a: calls.append(a),
                          wait_s=0)
    check("still-running turn posts nothing", st["still_running"] == 1 and not calls)
    check("still-running turn stays pending for the next start",
          bool(store.get("C:1").get("pending")))

    recovery._claude_alive = lambda sid: False
    calls = []
    st = recovery.recover(store, finalize=lambda *a: calls.append(a[3]),
                          reactions_for=lambda *a: RX(), say=lambda *a: calls.append(a),
                          wait_s=2)
    body = next((c for c in calls if isinstance(c, str)), "")
    check("finished turn's reply is recovered", st["recovered"] == 1 and "FINAL REPLY" in body)
    check("mid-turn narration is not posted as the answer", "mid-turn narration" not in body)
    check("marker cleared after recovery", store.get("C:1").get("pending") is None)

    # A rescued reply means the turn succeeded; without telling the caller, every
    # restart would file a false failure into the "needs you" list.
    outcomes = []
    store.update("C:2", session_id="s", pending=dict(pend))
    recovery.recover(store, finalize=lambda *a: None, reactions_for=lambda *a: RX(),
                     say=lambda *a: None, wait_s=2,
                     on_outcome=lambda k, ok: outcomes.append((k, ok)))
    check("recovery reports a rescued reply as success", outcomes == [("C:2", True)])

    tp.write_text("")            # nothing was produced
    outcomes.clear()
    store.update("C:3", session_id="s", pending=dict(pend))
    recovery.recover(store, finalize=lambda *a: None, reactions_for=lambda *a: RX(),
                     say=lambda *a: None, wait_s=2,
                     on_outcome=lambda k, ok: outcomes.append((k, ok)))
    check("recovery reports an empty turn as failure", outcomes == [("C:3", False)])

    # A turn this process is running has a live handler that will clear its own
    # marker; sweeping it here would post the reply a second time.
    tp.write_text("\n".join([entry("2026-08-01T12:01:00.000Z", "FINAL REPLY")]) + "\n")
    calls = []
    store.update("C:4", session_id="s", pending=dict(pend))
    st = recovery.recover(store, finalize=lambda *a: calls.append(a[3]),
                          reactions_for=lambda *a: RX(), say=lambda *a: None,
                          wait_s=0, skip={"C:4"})
    import tasks as _T
    check("a late rescue can correct a failure it already recorded",
          _T.DONE in _T.TRANSITIONS[_T.FAILED],
          "a restart marks the turn failed; the sweep then delivers its reply")
    check("a failed task can still be retried or dropped",
          _T.QUEUED in _T.TRANSITIONS[_T.FAILED] and _T.CANCELLED in _T.TRANSITIONS[_T.FAILED])
    check("done is still terminal", _T.TRANSITIONS[_T.DONE] == ())
    check("a turn this process is running is never swept",
          not calls and st["recovered"] == 0)
    check("and its marker is left for its own handler",
          bool(store.get("C:4").get("pending")))

    # The startup pass leaves a still-running turn pending on purpose, so
    # something must come back for it -- otherwise restarting during a long
    # turn swallows the answer, which is exactly what happened.
    # Two passes really can meet: the startup pass waits up to an hour on a
    # live child while the sweep runs alongside it. Both would post the reply.
    # A fresh store: earlier cases deliberately leave markers behind, and this
    # asserts that *nothing* was delivered.
    store = tmp_store()
    calls = []
    store.update("C:5", session_id="s", pending=dict(pend))
    recovery._claim("C:5")                      # pretend a pass already has it
    st = recovery.recover(store, finalize=lambda *a: calls.append(a[3]),
                          reactions_for=lambda *a: RX(), say=lambda *a: None, wait_s=0)
    check("a thread another pass is resolving is left alone",
          not calls and st["recovered"] == 0)
    recovery._release("C:5")
    st = recovery.recover(store, finalize=lambda *a: calls.append(a[3]),
                          reactions_for=lambda *a: RX(), say=lambda *a: None, wait_s=0)
    check("and is picked up once that pass releases it", st["recovered"] == 1)
    check("the claim is released after a pass", "C:5" not in recovery._inflight)

    store.update("C:6", session_id="s", pending=dict(pend))
    recovery._claude_alive = lambda sid: True   # still running -> early continue
    recovery.recover(store, finalize=lambda *a: None, reactions_for=lambda *a: RX(),
                     say=lambda *a: None, wait_s=0)
    src_r = (BASE / "recovery.py").read_text()
    check("a sweep does not warn about a healthy turn in progress",
          "if wait_s:" in src_r and "log.debug" in src_r,
          "it visits every marker every two minutes; warnings would bury real ones")
    check("a still-running thread releases its claim too",
          "C:6" not in recovery._inflight,
          "a leaked claim locks that thread out of every future pass")
    recovery._claude_alive = lambda sid: False

    src = (BASE / "bot.py").read_text()
    sweep = src[src.index("def _recovery_sweeper"):]
    sweep = sweep[:sweep.index("\ndef ", 1)]
    check("the sweep is its own thread, not a tail on the startup pass",
          "def _recovery_sweeper" in src and
          'name="rsweep"' in src,
          "the startup pass waits up to an hour; a sweep behind it never engages")
    check("the sweep never waits on a live child", "wait_s=0" in sweep,
          "only a finished child gives a trustworthy reply")
    check("the sweep skips this process's own turns", "skip=set(RUNNING)" in sweep)
    rec = src[src.index("def _recoverer"):]
    rec = rec[:rec.index("\ndef ", 1)]          # _recoverer alone, not its neighbours
    check("the orphaned-task closeout stays one-shot", "while True:" not in rec,
          "running it on a loop would fail live turns")
    check("and still runs at startup", "close_out_orphans()" in rec)
    check("before recovery, which can wait an hour on a live child",
          rec.index("close_out_orphans()") < rec.index("run_recovery()"),
          "otherwise those records claim work is in flight for that whole hour")

    co = src[src.index("def close_out_orphans"):src.index("def _recoverer")]
    check("a queued inline task is closed out too, not left forever",
          "never started; its handler is gone" in co,
          "inline means a handler drives it, and that handler is gone")
    check("and cancelled rather than failed, since nothing was attempted",
          co.index("tasks.CANCELLED") > co.index("tasks.QUEUED"))


# --- process lookup must not be fooled ---------------------------------------
# pgrep could not see these processes at all; a naive substring match on ps
# matches any shell that merely mentions a session id.

def test_procs():
    print("\nprocess lookup")
    check("empty session id finds nothing", procs.session_pids("") == [])
    check("unknown session is not alive",
          not procs.session_alive("deadbeef-0000-0000-0000-000000000000"))
    check("alive_sessions([]) makes no call", procs.alive_sessions([]) == set())
    check("pgrep is not used", "pgrep" not in (BASE / "procs.py").read_text().split('"""')[2])


# --- dashboard classifiers ----------------------------------------------------

def test_dashboard_classifiers():
    print("\ndashboard classifiers")
    sys.argv = ["x"]
    import visualizer as V

    live = {"live"}
    def health(mins, sid):
        started = (datetime.now(timezone.utc) - timedelta(minutes=mins)
                   ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        return V.turn_health({"pending": {"started": started, "session_id": sid}}, live)

    check("no marker -> nothing to show", V.turn_health({}, live) is None)
    check("fresh turn is not stalled", health(3, "live")["stalled"] is False)
    check("24h turn with a live child is stalled", health(1440, "live")["stalled"] is True)
    check("turn whose child died is stalled", health(5, "dead")["stalled"] is True)
    check("corrupt timestamp is ignored",
          V.turn_health({"pending": {"started": "garbage"}}, live) is None)

    check("steady spend is not flagged",
          V.cost_anomaly({"costs": [.4, .5, .45, .5, .48, .52]}) is None)
    check("10x spike is flagged",
          (V.cost_anomaly({"costs": [.4, .5, .45, .5, .48, 5.0]}) or {}).get("ratio") == 10.4)
    check("tiny absolute spike is ignored",
          V.cost_anomaly({"costs": [.001, .002, .001, .002, .001, .02]}) is None)
    check("too little history is not flagged", V.cost_anomaly({"costs": [.1, 5.0]}) is None)

    stale = lambda e: bool(e.get("summary") and "summary_turns" in e     # noqa: E731
                           and e.get("turns", 0) > e["summary_turns"])
    check("summary matching turn count is fresh",
          not stale({"summary": "x", "turns": 5, "summary_turns": 5}))
    check("summary behind turn count is stale",
          stale({"summary": "x", "turns": 7, "summary_turns": 5}))
    check("entry predating summary_turns is not flagged",
          not stale({"summary": "x", "turns": 3}))


# --- redelivery watermark -----------------------------------------------------

def test_watermark():
    print("\nredelivery watermark")
    store = tmp_store()
    src = ast.parse((BASE / "bot.py").read_text())
    fn = next(n for n in src.body if isinstance(n, ast.FunctionDef)
              and n.name == "_already_handled")
    ns = {"store": store}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<x>", "exec"), ns)
    handled = ns["_already_handled"]

    check("unknown thread is never blocked", not handled("C:9", "1785644289.053039"))
    store.update("C:1", last_msg_ts="1785644289.053039")
    check("exact redelivery is caught", handled("C:1", "1785644289.053039"))
    check("older arrival is caught", handled("C:1", "1785644200.000000"))
    check("newer message passes", not handled("C:1", "1785644999.111111"))
    check("sub-second precision is preserved", not handled("C:1", "1785644289.053040"))
    store.update("C:2", last_msg_ts="not-a-number")
    check("corrupt watermark fails open", not handled("C:2", "1785644289.05"))


# --- schema ------------------------------------------------------------------

def test_schema():
    import schema
    print("\nsession record schema")
    check("v1 bare string migrates", schema.migrate("abc")["session_id"] == "abc")
    check("v0 dict gets stamped", schema.migrate({"session_id": "x"})["v"] == schema.VERSION)
    cur = {"v": schema.VERSION, "future_field": 1}
    check("current record is untouched", schema.migrate(cur) is cur)
    check("unknown fields are kept", "future_field" in schema.migrate(cur))
    check("summary_turns default is unknown, not 0", schema.default("summary_turns") is None)
    a, b = schema.default("events"), schema.default("events")
    a.append(1)
    check("mutable defaults are not shared", b == [])
    check("a session records what it is for", "kind" in schema.FIELDS)
    check("sessions default to being a conversation", schema.default("kind") == "thread")
    check("every schema field is documented",
          all(isinstance(d, str) and d for _, d in schema.FIELDS.values()))

    # migrate() edits in place, so a naive "did it change?" check compares an
    # object with itself and never persists. Every entry here is a dict, which
    # is what production looks like.
    tmp2 = Path(tempfile.mkdtemp()) / "s.json"
    tmp2.write_text(json.dumps({"C:1": {"session_id": "a"}, "C:2": {"session_id": "b"}}))
    SessionStore(tmp2)
    disk = json.loads(tmp2.read_text())
    check("migration persists for dict-only stores",
          all(v.get("v") == schema.VERSION for v in disk.values()))
    mtime = tmp2.stat().st_mtime_ns
    SessionStore(tmp2)
    check("reloading an already-migrated store rewrites nothing",
          tmp2.stat().st_mtime_ns == mtime)

    live = json.loads((BASE / "sessions.json").read_text()) if (BASE / "sessions.json").exists() else {}
    if live:
        tmp = Path(tempfile.mkdtemp()) / "s.json"
        tmp.write_text(json.dumps(live))
        out = SessionStore(tmp).all()
        same = all(out[k].get(f) == v for k, e in live.items() for f, v in e.items())
        check("real sessions.json round-trips with no field lost",
              same and len(out) == len(live))


# --- command redelivery -------------------------------------------------------

def test_command_dedup():
    src = (BASE / "bot.py").read_text()
    print("\ncommands are guarded against redelivery")
    block = src[src.index("if text.startswith(\"!\") and handle_command"):]
    block = block[:block.index("if not text and not files")]
    check("handled commands record the watermark", "last_msg_ts=msg_ts" in block)
    check("only for threads that already exist", "store.get(key)" in block)


# --- dashboard exposure -------------------------------------------------------

def test_viz_bind_requires_token():
    src = (BASE / "visualizer.py").read_text()
    print("\ndashboard refuses to be exposed without auth")
    check("non-loopback bind without a token is refused",
          "if not LOOPBACK and not TOKEN:" in src and "raise SystemExit" in src)
    check("token comparison is constant-time", "secrets.compare_digest" in src)
    check("both GET and POST are gated", src.count("if not self._authed(url)") >= 2)


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


# --- every turn is a task ------------------------------------------------------

def test_turn_is_a_task():
    src = (BASE / "bot.py").read_text()
    print("\nturns are wired to the task lifecycle")
    check("a prompt creates a task", "task_store.create(" in src)
    check("task moves to running when Claude is invoked",
          "task_state(task_id, tasks.RUNNING)" in src)
    check("success records a result and completes",
          "task_state(task_id, tasks.DONE)" in src and '"cost": result.cost_usd' in src)
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
    check("a thread recovery may still resolve is never marked failed",
          'rec.get("thread") in pending' in co,
          "a false failure that lasts the whole wait is what stops the board being read")
    check("and the marker it keys on is the one recovery writes",
          'e.get("pending")' in co)


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
    check("the original goal is kept, not replaced",
          "task.get('goal', '')" in rework)
    check("reviewer findings are appended for the rerun", "findings" in rework)
    check("your notes are appended too", 'payload.get("notes")' in rework)
    check("your notes are attributed to you, not the reviewer",
          "From {payload.get('by'" in rework)
    check("a rework is handed to the runner", 'driver="queue"' in rework)

    src = (BASE / "bot.py").read_text()
    gate = src[src.index("def resolve_review("):src.index("def _task_runner(")]
    check("a review task is titled readably, not by its prompt",
          'title=f"Review: ' in gate)
    check("passing review completes the parent", "tasks.DONE if verdict" in gate)
    check("a flagged review asks the user", "tasks.AWAITING_APPROVAL" in gate)
    check("the implementor waits rather than self-certifying", "tasks.BLOCKED" in gate)
    exe = src[src.index("def execute_task("):src.index("def resolve_review(")]
    check("a fresh role does not resume the thread's session",
          "None if fresh else" in exe)
    check("a fresh role does not repoint the thread's session",
          "if not fresh:" in exe)


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


def _repo_guard_impl():
    """Loaded from bot.py without importing it (bot.py needs Slack tokens)."""
    import ast, types, contextlib, threading, logging
    from pathlib import Path as _P
    src = (BASE / "bot.py").read_text()
    tree = ast.parse(src)
    want = {"_repo_lock", "repo_guard"}
    nodes = [n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name in want]
    wanted = ("_repo_locks", "_repo_locks_guard")
    def names(n):
        if isinstance(n, ast.Assign):
            return [getattr(t, "id", "") for t in n.targets]
        if isinstance(n, ast.AnnAssign):      # `_repo_locks: dict[...] = {}`
            return [getattr(n.target, "id", "")]
        return []
    assigns = [n for n in tree.body if any(x in wanted for x in names(n))]
    mod = types.ModuleType("guard")
    mod.__dict__.update(contextlib=contextlib, threading=threading, Path=_P,
                        log=logging.getLogger("test"))
    exec(compile(ast.Module(body=assigns + nodes, type_ignores=[]), "<guard>", "exec"),
         mod.__dict__)
    return mod


# --- a stale credential must be visible before it kills every turn ---------------
# On 2026-08-30 Claude Code's two credential stores drifted and every headless
# turn failed with "OAuth session expired" while `claude` in a terminal worked.
# Nothing reported it; it was found by noticing failed tasks. The refresh
# token's expiry does not roll forward on refresh, so this is a scheduled
# outage, and a restart cannot fix it -- the dead token is on disk.

def test_credentials_check():
    import credentials as C
    import json as _j, time as _t
    print("\ncredential expiry is announced, not discovered")

    tmp = Path(tempfile.mkdtemp())
    check("a long-lived token short-circuits the question",
          C.state(has_token=True)["mode"] == "token",
          "it bypasses both stores, so neither drift nor expiry applies")
    # Which single store holds the credentials has already changed under us: on
    # 2026-08-31 they moved from the file into the Keychain, and a check that
    # only knew about the file called that "missing" while every turn succeeded.
    real_stores = C.stores
    C.stores = lambda path=None: []
    check("a missing file is reported, not ignored",
          C.state(path=tmp / "nope.json")["mode"] == "missing")
    C.stores = lambda path=None: ["keychain"]
    C._keychain_read = lambda: {"claudeAiOauth": {
        "refreshTokenExpiresAt": (_t.time() + 40 * 3600) * 1000}}
    st = C.state(path=tmp / "nope.json")
    check("credentials only in the Keychain are found, not called missing",
          st["mode"] == "oauth" and st["source"] == "keychain",
          f"got {st.get('mode')} — this said 'every turn will fail' while they were succeeding")
    check("and that is not itself a problem worth warning about",
          C.warning(st) == "", "one store is fine; two is the bug")
    C.stores = lambda path=None: ["file", "keychain"]
    both = C.warning({"mode": "oauth", "stores": ["file", "keychain"], "hours_left": 999})
    check("two stores at once does warn, whatever their expiry",
          "two places" in both,
          "that pairing is exactly what broke every headless turn on 2026-08-30")
    C.stores = real_stores
    bad = tmp / "bad.json"; bad.write_text("{not json")
    C.stores = lambda path=None: ["file"]
    check("an unreadable one too", C.state(path=bad)["mode"] == "unreadable")

    def creds(hours):
        f = tmp / f"c{hours}.json"
        f.write_text(_j.dumps({"claudeAiOauth": {
            "refreshTokenExpiresAt": (_t.time() + hours * 3600) * 1000,
            "accessToken": "sk-must-not-be-printed"}}))
        return f

    C.stores = lambda path=None: ["file"]
    healthy = C.state(path=creds(72))
    check("a healthy credential reports hours remaining",
          healthy["mode"] == "oauth" and 71 < healthy["hours_left"] < 73)
    check("and never returns the token itself",
          not any("must-not-be-printed" in str(v) for v in healthy.values()),
          "this is printed to a terminal and posted to Slack")
    check("a healthy credential says nothing", C.warning(healthy) == "")

    soon = C.warning(C.state(path=creds(5)))
    check("one about to expire warns", soon.startswith(":key:"))
    check("the warning says a restart will not help",
          "restarting will not help" in soon,
          "that is the first thing anyone tries, and it re-reads the same file")
    check("and names the actual fix", "claude setup-token" in soon)
    check("an already-expired one still warns",
          "have expired" in C.warning(C.state(path=creds(-2))))
    check("a missing file warns too", C.warning({"mode": "missing"}).startswith(":key:"))

    bot = (BASE / "bot.py").read_text()
    w = bot[bot.index("def _credential_watcher"):bot.index("def _task_runner")]
    check("the bot warns on its own, without being asked",
          'name="creds"' in bot and "chat_postMessage" in w)
    check("it warns once per credential, not hourly",
          "_warned_expiry" in w and 'expiry != _warned_expiry[0]' in w,
          "a nag every hour is a nag you filter out")
    check("a replaced credential re-arms the warning",
          "_warned_expiry[0] = 0.0" in w)
    check("a configured token silences it entirely",
          'os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")' in w)

    cli = (BASE / "bin" / "silkworm").read_text()
    check("the CLI shares the module rather than copying it",
          "import credentials" in cli and "def state(" not in cli,
          "two copies of a credential parser is one too many")

    # The token branch of `silkworm status` had never executed until a token was
    # actually configured, and it called ok() -- which does not exist; only
    # check(label, ok, hint) does. Driven for real rather than grepped.
    env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-fake"}
    r = subprocess.run([sys.executable, str(BASE / "bin" / "silkworm"), "status"],
                       capture_output=True, text=True, env=env, timeout=60)
    blob = r.stdout + r.stderr
    check("status survives token mode", "Traceback" not in blob,
          blob.strip().splitlines()[-1] if blob.strip() else "no output")
    check("and says so", "long-lived token" in blob, blob[-160:])

    # Editing .env without restarting is the normal case; the running bot and
    # the file disagree until then, and that gap is the thing worth reporting.
    check("status compares the running bot against .env",
          'bot.get("auth")' in cli and "running bot matches .env" in cli,
          "reading the file alone would call a stale bot fixed")
    bot_src = (BASE / "bot.py").read_text()
    check("the bot reports the auth it actually resolved",
          '"auth": auth' in bot_src and "credentials.state(has_token=" in bot_src)
    check("and never ships an expiry timestamp it does not need",
          'auth.pop("expires_at", None)' in bot_src)


def test_repo_guard():
    import threading, time as _t
    G = _repo_guard_impl()
    print("\nturns sharing a checkout are serialised")

    plain = Path(tempfile.mkdtemp())                 # not a repo
    repo = Path(tempfile.mkdtemp()); (repo / ".git").mkdir()

    check("a directory that is not a checkout is not locked",
          G._repo_lock(str(plain)) is None,
          "the shared scratch dir holds unrelated projects; locking it "
          "would queue every thread behind every other")
    check("a checkout gets a lock", G._repo_lock(str(repo)) is not None)
    check("the same checkout gets the same lock",
          G._repo_lock(str(repo)) is G._repo_lock(str(repo) + "/"),
          "resolved path, so two spellings do not both get in")

    order, started = [], threading.Event()

    def hold():
        with G.repo_guard(str(repo)):
            started.set()
            order.append("a-in")
            _t.sleep(0.6)
            order.append("a-out")

    def contend():
        started.wait(2)
        with G.repo_guard(str(repo)):
            order.append("b-in")

    ta, tb = threading.Thread(target=hold), threading.Thread(target=contend)
    ta.start(); tb.start(); ta.join(5); tb.join(5)
    check("a second turn waits for the first to finish",
          order == ["a-in", "a-out", "b-in"], f"got {order}")

    # Non-repo directories must stay concurrent, or normal use grinds.
    order2, started2 = [], threading.Event()

    def hold2():
        with G.repo_guard(str(plain)):
            started2.set(); _t.sleep(0.6); order2.append("a-out")

    def free2():
        started2.wait(2)
        with G.repo_guard(str(plain)):
            order2.append("b-in")

    t1, t2 = threading.Thread(target=hold2), threading.Thread(target=free2)
    t1.start(); t2.start(); t1.join(5); t2.join(5)
    check("turns in a non-checkout directory still run concurrently",
          order2 == ["b-in", "a-out"], f"got {order2}")

    bot = (BASE / "bot.py").read_text()
    check("both call sites take it", bot.count("repo_guard(cwd, progress)") == 2,
          "a Slack turn and a queued task are exactly the pair that collide")
    for site in ("with lock, repo_guard(cwd, progress):",):
        check("taken inside the thread lock, so the order cannot deadlock",
              site in bot)


# --- a turn may run as long as it is still working -------------------------------
# The 900s wall clock killed three real turns in three days -- a strategy
# backtest and two game-dev iterations -- because elapsed time cannot tell
# progress from a wedge. Only silence can.

def test_turn_deadline_is_idleness():
    import claude_runner as CR
    print("\nturns end on silence, not on the clock")

    # A stand-in for the real binary: it must be executable and ignore the
    # flags run_turn always passes (-p, --output-format ...), so parameters
    # come through the environment instead of argv.
    fake = Path(tempfile.mkdtemp()) / "fakeclaude"
    fake.write_text(
        "#!/bin/sh\n"
        'exec "$PYBIN" -c \'\n'
        "import json, os, sys, time\n"
        "sys.stdin.read()\n"
        "chatty = float(os.environ[\"CHATTY\"]); quiet = float(os.environ[\"QUIET\"])\n"
        "end = time.time() + chatty\n"
        "while time.time() < end:\n"
        "    print(json.dumps({\"type\": \"system\", \"subtype\": \"init\",\n"
        "                      \"session_id\": \"fake-session\"}), flush=True)\n"
        "    time.sleep(0.2)\n"
        "time.sleep(quiet)\n"
        "print(json.dumps({\"type\": \"result\", \"result\": \"done\",\n"
        "                  \"session_id\": \"fake-session\"}), flush=True)\n"
        "'\n")
    fake.chmod(0o755)

    def run(chatty, quiet, **kw):
        env = {**os.environ, "PYBIN": sys.executable,
               "CHATTY": str(chatty), "QUIET": str(quiet)}
        return CR.run_turn("go", binary=str(fake), cwd=str(BASE),
                           permission_args=[], env=env, **kw)

    # Busy for far longer than the idle limit: must survive.
    t0 = time.time()
    r = run(6, 0, timeout=0, idle_timeout=3)
    busy = time.time() - t0
    check("a turn that keeps working outlives the idle limit",
          r.text == "done" and busy > 5,
          f"ran {busy:.1f}s under a 3s idle limit")

    # Quiet for longer than the idle limit: must die, and say why.
    try:
        run(0.5, 30, timeout=0, idle_timeout=3)
        check("a silent turn is stopped", False, "it was allowed to hang")
    except CR.ClaudeTimeout as e:
        check("a silent turn is stopped", True)
        check("and the error names silence, not elapsed time",
              "no output" in str(e) and "as long as it likes" in str(e), str(e)[:90])

    # The absolute cap still exists for anyone who wants one.
    try:
        run(30, 0, timeout=3, idle_timeout=0)
        check("an explicit absolute cap still applies", False, "it ran past the cap")
    except CR.ClaudeTimeout as e:
        check("an explicit absolute cap still applies", "timed out after 3s" in str(e),
              str(e)[:80])

    check("a timeout is still not treated as a dead session",
          issubclass(CR.ClaudeTimeout, CR.ClaudeError))

    src = (BASE / "claude_runner.py").read_text()
    check("any output counts as alive, even lines we cannot parse",
          src.index("last_seen[0] = time.monotonic()") < src.index("line = line.strip()"),
          "the question is whether the process is doing anything")
    bot = (BASE / "bot.py").read_text()
    check("turns run uncapped by default",
          'os.environ.get("CLAUDE_TIMEOUT", "0")' in bot)
    check("the reaper is keyed off the idle limit, not the absolute cap",
          "max(CLAUDE_IDLE_TIMEOUT * 2, CLAUDE_TIMEOUT * 2, 3600)" in bot,
          "deriving a bound from a cap of 0 would reap orphans mid-work")


# --- "get back to me when it's done" --------------------------------------------
# A turn is request/response: one prompt in, one reply out, and the session is
# dormant either side of it. Held open, a watch hits the 15 minute cap, holds
# its thread's lock meanwhile, and dies on restart. Ended, whatever it
# backgrounded reports to nobody. Both ways the instruction was simply lost.

def test_defer():
    import defer as D
    from tasks import TaskStore
    import tasks as T
    print("\nscheduling a later turn instead of holding one open")

    check("plain seconds", D.parse_delay("90") == 90)
    check("units", (D.parse_delay("10m"), D.parse_delay("2h"), D.parse_delay("1d"))
          == (600, 7200, 86400))
    for bad, why in (("bogus", "unreadable"), ("1s", "below the floor"),
                     ("48h", "above the ceiling"), ("", "empty")):
        try:
            D.parse_delay(bad)
            check(f"{why} delay is refused", False, f"{bad!r} was accepted")
        except ValueError:
            check(f"{why} delay is refused", True)
    check("the floor keeps a wake-up from being a busy-loop", D.MIN_DELAY_S >= 30)

    check("a chain counts up", D.next_depth(0) == 1 and D.next_depth(3) == 4)
    check("a missing depth starts at one", D.next_depth(None) == 1)
    try:
        D.next_depth(D.MAX_DEFERS)
        check("a chain cannot poll forever", False, "the cap was not enforced")
    except ValueError as e:
        check("a chain cannot poll forever", "report what you know" in str(e),
              "otherwise a model that misjudges 'is it done' bills you all night")

    # The wake-up itself is an ordinary blocked task with a retry_at, which the
    # queue runner already knows how to requeue.
    ts = TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    now = time.time()
    t = ts.create("check the deploy", state=T.BLOCKED, driver="queue",
                  source="defer", thread="C:1", retry_at=now + 600, defers=1)
    check("a wake-up waits in blocked, costing nothing", t["state"] == T.BLOCKED)
    check("and is invisible until it fires", T.BLOCKED not in T.NEEDS_ATTENTION,
          "a pending watch is not something you have to act on")
    check("not yet due", ts.due_retries(now) == [])
    check("due when its time comes", ts.due_retries(now + 601) == [t["id"]])
    check("it carries its thread, so it can speak there", t["thread"] == "C:1")
    check("the chain depth survives the wait", t["defers"] == 1)

    # "nothing yet" with no successor is a watch that stopped watching.
    ts2 = TaskStore(Path(tempfile.mkdtemp()) / "t2.json")
    check("no wake-up pending on an unknown thread",
          not ts2.has_pending_wakeup("C:9"))
    ts2.create("check again", state=T.BLOCKED, driver="queue", source="defer",
               thread="C:9", retry_at=now + 300)
    check("a scheduled wake-up is visible to its predecessor",
          ts2.has_pending_wakeup("C:9"))
    check("a wake-up on another thread does not count",
          not ts2.has_pending_wakeup("C:8"))
    ts3 = TaskStore(Path(tempfile.mkdtemp()) / "t3.json")
    ts3.create("ordinary queued work", state=T.BLOCKED, driver="queue",
               thread="C:9", retry_at=now + 300)
    check("a blocked task that is not a wake-up does not count",
          not ts3.has_pending_wakeup("C:9"),
          "only a scheduled check keeps the promise to report back")

    bot = (BASE / "bot.py").read_text()
    # You cannot trust a watch you cannot see. `blocked` also holds quota
    # retries, which are the system waiting on itself rather than a promise
    # made to you, so the view has to tell them apart.
    watch = bot[bot.index('if action == "watching"'):bot.index('if action == "ingest-email"')]
    check("the watching view exists", 'return {"ok": True, "watching"' in watch)
    check("it lists only scheduled wake-ups, not every blocked task",
          'r.get("source") != "defer"' in watch,
          "a quota retry is not a promise made to you")
    check("it says when each one fires", '"in_s"' in watch)
    check("and how much of the chain is left", '"remaining"' in watch)
    check("soonest first", 'sorted(out, key=lambda w: w["in_s"])' in watch)
    cli_src = (BASE / "bin" / "silkworm").read_text()
    check("the CLI exposes it", 'cmd == "watching"' in cli_src)
    check("and it is documented in the usage text",
          "silkworm watching" in cli_src.split("def ")[0],
          "an undiscoverable view is one you never use")

    check("the turn is told which thread it may schedule against",
          'env["SILKWORM_THREAD"] = key' in bot)
    check("and how deep its chain already is",
          'env["SILKWORM_DEFERS"]' in bot)
    ex = bot[bot.index("def execute_task"):bot.index("def resolve_review")]
    check("a wake-up resumes the same conversation",
          "defer.WAKE_NOTE" in ex and "roles.is_fresh" in bot,
          "the note it left itself is useless without the context")
    check("a wake-up with nothing to say deletes its own placeholder",
          "progress.delete()" in ex and "defer.QUIET" in ex,
          "a watch that narrates every poll is worse than no watch")
    check("and still closes out as done",
          "nothing to report yet" in ex)
    check("but only when it actually scheduled the next check",
          "has_pending_wakeup(key)" in ex and "defer.STOPPED" in ex,
          "silently ending the watch is the failure this exists to prevent")
    check("a stopped watch asks for you instead of vanishing",
          "tasks.NEEDS_INPUT" in ex)
    check("ProgressMessage can actually delete", "def delete(self)" in bot
          and "chat_delete" in bot)
    check("ordinary turns are told how to schedule", "defer.HOW_TO" in bot)

    cli = (BASE / "bin" / "silkworm").read_text()
    check("the CLI refuses to run outside a turn",
          'SILKWORM_THREAD' in cli and "only works from inside a Silkworm turn" in cli,
          "there would be no thread to wake")
    check("the bot exposes the route", 'server.route("/defer", handle_defer)' in bot)

    # Driven for real, not grepped: the first version passed argv straight
    # through, so the subcommand itself was read as the delay and every call
    # died with "could not read a delay from 'defer'".
    env = {**os.environ, "SILKWORM_THREAD": "C:1", "SILKWORM_DEFERS": "0"}
    cli = str(BASE / "bin" / "silkworm")
    r = subprocess.run([sys.executable, cli, "defer", "10m"],
                       capture_output=True, text=True, env=env, timeout=30)
    check("a missing goal is a usage error", r.returncode == 2,
          f"exit {r.returncode}: {(r.stdout + r.stderr).strip()[:120]}")
    check("the delay is not read as the subcommand",
          "could not read a delay" not in (r.stdout + r.stderr))
    r = subprocess.run([sys.executable, cli, "defer"],
                       capture_output=True, text=True, env=env, timeout=30)
    check("no arguments at all is a usage error", r.returncode == 2)
    r = subprocess.run([sys.executable, cli, "defer", "10m", "x"],
                       capture_output=True, text=True,
                       env={k: v for k, v in os.environ.items()
                            if k not in ("SILKWORM_THREAD",)}, timeout=30)
    check("outside a turn it refuses rather than guessing a thread",
          r.returncode == 1 and "inside a Silkworm turn" in r.stdout + r.stderr)
    check("an absolute path is handed to the model, not a bare name",
          "SILKWORM_BIN = str(Path(__file__).resolve().parent" in bot,
          "a turn's PATH is not ours to assume")


# --- messages sent while the link was down --------------------------------------
# Socket Mode does not queue. During the 2026-08-24 outage five messages were
# sent and none were delivered; four were noticed and re-typed by hand, and
# "is this working?" was never answered at all.

def test_backfill():
    import backfill as B
    print("\nreplaying messages Slack never delivered")

    # Timestamps sit inside the age window, as a real outage's would; the ~17
    # hour gap on 2026-08-24 is well within it.
    now = 1_787_700_000.0
    mark = now - 17 * 3600
    at = lambda offset: f"{mark + offset:.6f}"          # noqa: E731
    entries = {"C:100": {"last_msg_ts": at(0)}, "C:200": {}}
    msgs = {
        ("C", "100"): [
            {"ts": at(0), "user": "U1", "text": "already answered"},
            {"ts": at(50), "user": "U1", "text": "is this working?"},
            {"ts": at(60), "user": "BOT", "text": "my own reply"},
            {"ts": at(70), "bot_id": "B1", "text": "another app"},
            {"ts": at(80), "user": "U1", "subtype": "channel_join", "text": "joined"},
            {"ts": at(90), "user": "U1", "subtype": "file_share", "text": "screenshot",
             "files": [{"id": "F1"}]},
        ],
        ("C", "200"): [{"ts": at(50), "user": "U1", "text": "no watermark here"}],
    }
    got = B.missed(entries, replies=lambda c, t: msgs[(c, t)], bot_user_id="BOT",
                   handled_subtypes={"file_share"}, now=now)
    texts = [e["text"] for e in got]
    check("a message past the watermark is replayed", "is this working?" in texts,
          "the one that was never answered")
    check("a message at the watermark is not", "already answered" not in texts)
    check("our own reply is not replayed", "my own reply" not in texts)
    check("another app's message is not", "another app" not in texts)
    check("channel noise is not", "joined" not in texts)
    check("an upload is", "screenshot" in texts)
    check("its files come with it", got[-1]["files"] == [{"id": "F1"}])
    check("a thread with no watermark is skipped", "no watermark here" not in texts,
          "everything ever said is not a backlog")
    check("replayed oldest first",
          [e["ts"] for e in got] == [at(50), at(90)])
    check("the event looks like a real DM",
          got[0]["channel"] == "C" and got[0]["thread_ts"] == "100"
          and got[0]["channel_type"] == "im")
    check("the 17 hour outage is inside the replay window",
          17 * 3600 < B.MAX_AGE_S)

    old = {"C:1": {"last_msg_ts": str(now - 7200)}}
    stale = {("C", "1"): [{"ts": str(now - B.MAX_AGE_S - 1), "user": "U1", "text": "last week"}]}
    check("a message older than the window is left alone",
          B.missed(old, replies=lambda c, t: stale[(c, t)], bot_user_id="B",
                   handled_subtypes=set(), now=now) == [],
          "it was re-asked or stopped mattering")

    many = {("C", "1"): [{"ts": str(now - 60 + i), "user": "U1", "text": f"m{i}"}
                         for i in range(9)]}
    capped = B.missed(old, replies=lambda c, t: many[(c, t)], bot_user_id="B",
                      handled_subtypes=set(), now=now, max_per_thread=3)
    check("a chatty gap is capped", len(capped) == 3)
    check("and the newest are kept", [e["text"] for e in capped] == ["m6", "m7", "m8"])
    src = (BASE / "backfill.py").read_text()
    check("a dropped message is logged, not silently forgotten",
          "log.warning" in src and "skipping" in src)

    check("a thread Slack cannot return is skipped, not fatal",
          B.missed(old, replies=lambda c, t: (_ for _ in ()).throw(RuntimeError("nope")),
                   bot_user_id="B", handled_subtypes=set(), now=now) == [])

    bot = (BASE / "bot.py").read_text()
    check("backfill goes through the ordinary prompt path",
          "handle_prompt(event, say, app.client)" in
          bot[bot.index("def run_backfill"):bot.index("def _backfiller")],
          "which already refuses redeliveries")
    check("replayed events are not marked _web",
          '"_web"' not in bot[bot.index("def run_backfill"):bot.index("def _backfiller")],
          "_web skips the redelivery guard this relies on")
    check("a reconnect also backfills",
          "run_backfill()" in bot[bot.index("def _slack_repair"):bot.index("def _slack_watchdog")],
          "the socket does not queue during the gap either")


# --- a running bot is not a connected bot -------------------------------------
# On 2026-08-24 the socket-mode link broke at 22:26 and spun in a reconnect loop
# for seventeen hours. The process never exited, so launchd's KeepAlive saw a
# healthy service and `silkworm status` reported the bot reachable the whole
# time -- it probes the local HTTP server, which was genuinely fine.

def test_slack_health():
    import slack_health as H
    print("\nslack link health")

    h = H.Health(window=10, min_fraction=0.5)
    for i in range(9):
        check_none = h.sample(False, i)
    check("a partial window cannot condemn the link", check_none is None,
          "startup is not an outage")
    check("and the link is not called unhealthy yet", h.healthy())

    # The real failure mode: a session really is established every few seconds,
    # so an occasional sample honestly reports "connected".
    h = H.Health(window=10, min_fraction=0.5)
    flapping = [False, False, False, False, True, False, False, False, False, False]
    actions = [h.sample(c, i) for i, c in enumerate(flapping)]
    check("a flapping link is judged down despite connecting sometimes",
          actions[-1] == H.REPAIR, f"got {actions[-1]}")
    check("one lucky sample would have said connected", any(flapping))

    # Escalation: repair first, restart only if the new connection is bad too.
    for i, c in enumerate(flapping):
        action = h.sample(c, 100 + i)
    check("a second bad window escalates to a restart", action == H.RESTART)

    h = H.Health(window=10, min_fraction=0.5)
    for i, c in enumerate(flapping):
        h.sample(c, i)                       # -> REPAIR, window cleared
    for i in range(10):
        action = h.sample(True, 100 + i)
    check("a recovered link is not restarted", action is None)
    # The window rolls rather than resetting, so the verdict lands part-way
    # through the next bad stretch -- sooner than waiting out a fresh window.
    again = [h.sample(c, 200 + i) for i, c in enumerate(flapping)]
    check("and a good window forgives the earlier repair",
          H.REPAIR in again and H.RESTART not in again,
          f"otherwise one blip arms a restart forever; got {again}")

    h = H.Health(window=10, min_fraction=0.5)
    for i in range(10):
        action = h.sample(True, i)
    check("a healthy link asks for nothing", action is None)
    check("a healthy link reports fully connected", h.fraction() == 1.0)
    check("status names how long it has been down",
          H.Health().status(0.0)["down_for"] == 0.0)

    bot = (BASE / "bot.py").read_text()
    check("the handler is kept, not fired and forgotten",
          "slack_handler = SocketModeHandler(" in bot,
          "nothing could check a connection nobody held a reference to")
    check("a restart verdict actually exits so KeepAlive can act",
          "os._exit(1)" in bot[bot.index("def _slack_watchdog"):])
    check("/status reports the link, not just the HTTP server",
          '"slack": slack.status(' in bot)
    cli = (BASE / "bin" / "silkworm").read_text()
    check("silkworm status checks the link too", '"connected to slack"' in cli,
          "it reported the bot reachable throughout the outage")
    viz = (BASE / "visualizer.py").read_text()
    check("the dashboard passes the link to its alert bar",
          "renderAlerts(data.sessions, data.slack)" in viz,
          "during an outage the dashboard is the thing still working")
    check("and only alerts on a full sampling window",
          "slack.ready && !slack.connected" in viz,
          "a bot that just started is not a bot that is down")


# --- a missing cwd must not be reported as a missing binary -------------------
# Popen raises FileNotFoundError for either. Blaming the binary unconditionally
# sent a real diagnosis looking for a PATH problem that did not exist.

def test_missing_cwd_is_named():
    import claude_runner
    print("\na vanished working directory says so")
    gone = Path(tempfile.mkdtemp()) / "deleted"
    try:
        claude_runner.run_turn("hi", cwd=str(gone), binary=sys.executable,
                               permission_args=[])
        check("a missing cwd raises", False, "no error at all")
    except ClaudeError as e:
        check("a missing cwd names the directory, not the binary",
              str(gone) in str(e) and "on PATH" not in str(e), str(e))
    try:
        claude_runner.run_turn("hi", cwd=str(BASE),
                               binary="definitely-not-claude", permission_args=[])
        check("a missing binary raises", False, "no error at all")
    except ClaudeError as e:
        check("a missing binary still says so", "on PATH" in str(e), str(e))


# --- a module used but never imported ----------------------------------------
# `retry` was used in fail_or_retry and never imported, so every transient
# failure raised NameError instead of being requeued. Its own tests passed:
# they exercised retry.py directly and checked bot.py as *text*, which cannot
# tell a used name from an imported one.

def test_modules_are_imported():
    print("\nevery module a file uses is imported")
    siblings = {p.stem for p in BASE.glob("*.py")}
    for path in sorted(BASE.glob("*.py")):
        tree = ast.parse(path.read_text())
        imported, bound = set(), set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imported |= {(a.asname or a.name).split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                imported |= {a.asname or a.name for a in n.names}
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(n.name)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                bound.add(n.id)
            elif isinstance(n, ast.arg):
                bound.add(n.arg)
        used = {n.value.id for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
        missing = sorted((used & siblings) - imported - bound - {path.stem})
        check(f"{path.name} imports every sibling module it uses", not missing,
              f"uses but never imports: {', '.join(missing)}")


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
    check("filing a thread under a repo-less project moves it into that directory",
          'store.update(key, project=proj["slug"],' in bot
          and '"cwd": str(home)' in bot,
          "its CLAUDE.md only loads if the thread runs there")
    check("the brief is rewritten, not appended",
          "do not append" in (BASE / "projects.py").read_text())
    check("completing a task folds the outcome back in",
          "refresh_brief(task.get(" in bot)
    check("a thread's project is inherited by its tasks",
          'project=(store.get(key) or {}).get("project", "")' in bot)
    check("!project files a thread", 'elif lower.startswith("!project")' in bot)


# --- transient failures are waited out, not escalated --------------------------
# Quota exhaustion and API overload filled the "needs you" list with things no
# person could act on, which is how a board stops being trusted.

def test_transient_retry():
    import retry as R
    import tasks as T
    from tasks import TaskStore
    from datetime import datetime
    print("\ntransient failures")
    now = datetime(2026, 8, 20, 15, 0).timestamp()

    for text in ("You've hit your session limit · resets 7pm",
                 "Claude usage limit reached", "rate_limit_error",
                 "rate limit exceeded"):
        check(f"quota: {text[:34]}", R.classify(text) == R.QUOTA)
    check("529 is transient", R.classify("API Error: 529 Overloaded") == R.OVERLOADED)
    check("a dropped connection is transient",
          R.classify("API Error: Connection closed mid-response.") == R.NETWORK)
    for text in ("Claude reported an error.", "bash: command not found",
                 "Claude timed out after 900s.", ""):
        check(f"real failure stays a failure: {text[:28] or '(empty)'}",
              R.classify(text) is None)

    check("a stated reset time is honoured",
          datetime.fromtimestamp(R.reset_at("resets 7pm", now)).hour == 19)
    check("a reset time already past waits for tomorrow",
          R.reset_at("resets 9am", now) > now)
    check("no time given is not a parse", R.reset_at("no time here", now) is None)

    kind, when = R.retry_at("session limit · resets 7pm", 1, now)
    check("quota waits for the reset", datetime.fromtimestamp(when).hour == 19)
    _, when = R.retry_at("usage limit reached", 1, now)
    check("quota with no stated time waits a while", 25 <= (when - now) / 60 <= 35)
    first = R.retry_at("529 Overloaded", 1, now)[1] - now
    later = R.retry_at("529 Overloaded", 3, now)[1] - now
    check("overload backs off progressively", later > first)
    check("backoff is capped", R.retry_at("529 Overloaded", 4, now)[1] - now <= R.BACKOFF_CAP_S)
    check("auto-retry gives up eventually",
          R.retry_at("529 Overloaded", R.MAX_AUTO_RETRIES, now) is None)
    check("a real error is never auto-retried",
          R.retry_at("Claude reported an error.", 1, now) is None)

    st = TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    due = st.create("due"); st.transition(due["id"], T.RUNNING)
    st.update(due["id"], retry_at=time.time() - 5, driver="queue")
    st.transition(due["id"], T.BLOCKED, "quota")
    soon = st.create("soon"); st.transition(soon["id"], T.RUNNING)
    st.update(soon["id"], retry_at=time.time() + 3600)
    st.transition(soon["id"], T.BLOCKED, "quota")
    check("only tasks whose time has come are requeued",
          st.due_retries(time.time()) == [due["id"]])
    check("a waiting task never appears in 'needs you'",
          T.BLOCKED not in T.NEEDS_ATTENTION)
    st.transition(due["id"], T.QUEUED, "retry time reached")
    check("a requeued task is handed to the runner",
          st.get(due["id"])["driver"] == "queue",
          "whatever was driving it is gone by the time it retries")

    bot = (BASE / "bot.py").read_text()
    check("failure paths try parking before escalating",
          bot.count("if not fail_or_retry(") >= 3)
    check("the runner promotes due retries", "due_retries(time.time())" in bot)


# --- failures overtaken by events -----------------------------------------------
# Four stale failures sat on the board while their threads had long since
# carried on and produced answers.

def test_supersede_stale_failures():
    import tasks as T
    from tasks import TaskStore
    print("\nstale failures are superseded")
    st = TaskStore(Path(tempfile.mkdtemp()) / "t.json")

    old = st.create("msg", thread="C:1", driver="inline")
    st.transition(old["id"], T.RUNNING)
    st.transition(old["id"], T.FAILED, "Claude reported an error.")
    time.sleep(0.01)
    new = st.create("msg", thread="C:1", driver="inline")
    st.transition(new["id"], T.RUNNING)
    st.transition(new["id"], T.DONE)

    gone = st.supersede_failed("C:1", new["created"])
    check("a later answer clears an earlier failure on that thread",
          gone == [old["id"]] and st.get(old["id"])["state"] == T.CANCELLED)
    check("the reason is recorded, not silently dropped",
          st.get(old["id"])["events"][-1]["detail"].startswith("superseded"))

    other = st.create("msg", thread="C:2", driver="inline")
    st.transition(other["id"], T.RUNNING)
    st.transition(other["id"], T.FAILED, "boom")
    st.supersede_failed("C:1", time.time())
    check("a failure on another thread is untouched",
          st.get(other["id"])["state"] == T.FAILED)

    newer = st.create("msg", thread="C:1", driver="inline")
    st.transition(newer["id"], T.RUNNING)
    st.transition(newer["id"], T.FAILED, "boom")
    st.supersede_failed("C:1", new["created"])
    check("a failure newer than the success survives",
          st.get(newer["id"])["state"] == T.FAILED)

    q = st.create("real work", thread="C:3", driver="queue")
    st.transition(q["id"], T.RUNNING)
    st.transition(q["id"], T.FAILED, "boom")
    time.sleep(0.01)
    d = st.create("other", thread="C:3", driver="queue")
    st.transition(d["id"], T.RUNNING)
    st.transition(d["id"], T.DONE)
    check("queued work is never superseded",
          st.supersede_failed("C:3", d["created"]) == []
          and st.get(q["id"])["state"] == T.FAILED,
          "a queued failure is work that did not happen")

    prop = st.create("ingested", thread="C:1", driver="inline", state=T.PROPOSED)
    appr = st.create("gated", thread="C:1", driver="inline")
    st.transition(appr["id"], T.RUNNING)
    st.transition(appr["id"], T.AWAITING_APPROVAL)
    st.supersede_failed("C:1", time.time())
    check("proposed and awaiting_approval are never superseded",
          st.get(prop["id"])["state"] == T.PROPOSED
          and st.get(appr["id"])["state"] == T.AWAITING_APPROVAL,
          "those are deliberate asks, not failures")

    bot = (BASE / "bot.py").read_text()
    check("completing a task triggers the sweep", "supersede_failed(" in bot)


# --- file uploads reach the handler -------------------------------------------
# Slack delivers an upload as a message with subtype "file_share". Dropping
# every subtyped message silently discarded every screenshot ever sent.

def test_file_uploads_are_handled():
    print("\nfile uploads")
    src = (BASE / "bot.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == "should_handle")
    subs = next(n for n in tree.body if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", "") == "HANDLED_SUBTYPES")
    ns = {"BOT_USER_ID": "UBOT"}
    exec(compile(ast.Module(body=[subs, fn], type_ignores=[]), "<x>", "exec"), ns)
    should = ns["should_handle"]

    upload = {"channel_type": "im", "user": "U1", "subtype": "file_share",
              "files": [{"name": "shot.png"}], "text": "what is wrong here?"}
    check("a screenshot upload is handled", should(upload) is True)
    check("an upload with no caption is handled",
          should({**upload, "text": ""}) is True)
    check("a plain DM is still handled",
          should({"channel_type": "im", "user": "U1", "text": "hi"}) is True)
    for subtype in ("message_changed", "message_deleted", "channel_join"):
        check(f"{subtype} is still ignored",
              should({"channel_type": "im", "user": "U1", "subtype": subtype}) is False)
    check("our own messages are ignored",
          should({"channel_type": "im", "user": "UBOT", "text": "hi"}) is False)
    check("other bots are ignored",
          should({"channel_type": "im", "bot_id": "B1", "text": "hi"}) is False)
    check("non-DM channels are ignored",
          should({"channel_type": "channel", "user": "U1", "text": "hi"}) is False)

    check("images are pointed out as viewable, not just listed",
          "use Read to look at it" in src)
    # A thread's cwd is usually a git repo; writing uploads there leaves
    # untracked noise a stray `git add -A` would commit.
    check("uploads are not written into the thread's working directory",
          'cwd / "slack-uploads"' not in src)
    check("uploads live under Silkworm, beside outbox and artifacts",
          'UPLOADS_ROOT = BASE_DIR / "uploads"' in src
          and "UPLOADS_ROOT / key.replace" in src)
    check("uploads are gitignored",
          "uploads/" in (BASE / ".gitignore").read_text())
    check("the download uses the bot token",
          'Authorization": f"Bearer {token}' in src)


# --- the dashboard's own javascript --------------------------------------------
# An edit anchored on text that did not exist silently inserted nothing, so the
# tasks panel shipped as markup calling functions that were never defined.
# loadList() called one of them, threw, and the whole page rendered empty --
# while node --check passed, because an undefined call is a runtime error.

def test_dashboard_js_is_whole():
    import re
    sys.argv = ["x"]
    import visualizer as V
    print("\ndashboard javascript")
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    defined = set(re.findall(r"(?:async\s+)?function\s+([A-Za-z_]\w*)", js))

    for name in ("loadList", "loadStats", "loadTranscript", "renderAlerts", "jumpTo",
                 "taskCall", "toggleTasks", "setTaskView", "renderProjects", "addTask",
                 "taskAction", "taskButtons", "renderTasks", "updateTaskBadge",
                 "refreshTaskBadge", "releaseThread", "retitle", "nameAllThreads",
                 "resummarize", "toggleLearn", "renderLearnings"):
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


if __name__ == "__main__":
    for t in (test_resume_retry_requires_missing_transcript, test_stop_escalates_to_sigkill,
              test_timeout_is_distinct, test_recovery, test_procs,
              test_dashboard_classifiers, test_watermark, test_bounded_state,
              test_schema, test_command_dedup, test_viz_bind_requires_token,
              test_task_lifecycle, test_turn_is_a_task, test_task_runner_claim,
              test_review_gate, test_email_ingest, test_modules_are_imported,
              test_missing_cwd_is_named, test_slack_health, test_backfill, test_defer, test_repo_guard,
              test_credentials_check,
              test_turn_deadline_is_idleness,
              test_mail_facts, test_projects,
              test_transient_retry, test_supersede_stale_failures,
              test_file_uploads_are_handled, test_dashboard_js_is_whole):
        try:
            t()
        except Exception as exc:
            FAILED.append(f"{t.__name__} raised {exc!r}")
            print(f"  ✘ {t.__name__} raised {exc!r}")
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for f in FAILED:
        print(f"  FAILED: {f}")
    sys.exit(1 if FAILED else 0)

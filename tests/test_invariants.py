"""Regression tests for the things that have actually broken.

Every case here corresponds to a real failure: a turn that hung for a day, a
thread that silently lost its history, a reply that was never delivered. They
run without Slack, without a bot, and without spending anything.

    python3 tests/test_invariants.py
"""

import ast
import contextlib
import io
import json
import logging
import os
import re as _re
import subprocess
import sys
import tempfile
import time
import types
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


def bot_functions(*names, **globals_):
    """Lift named functions out of bot.py and run them against fakes.

    Importing bot.py needs a Slack token and would open the live tasks.json, so
    most checks here read it as text. Compiling just the functions under test
    into a namespace we control buys the real thing instead: the code actually
    runs, against a scratch store, and a mistake in it fails rather than merely
    reading differently.
    """
    tree = ast.parse((BASE / "bot.py").read_text())
    wanted = [n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name in names]
    missing = set(names) - {n.name for n in wanted}
    assert not missing, f"bot.py has no {', '.join(sorted(missing))}"
    ns = {"log": logging.getLogger("test"), "time": time, **globals_}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "bot.py", "exec"), ns)
    return ns


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
    # Cancelling these and trusting the backfill lost real messages. The
    # backfill keys on one high-water mark, but messages are not handled in
    # arrival order: one that queued behind another and died in a restart is
    # invisible the moment any later message has run.
    check("a queued inline task is handed to the runner, not dropped",
          'task_store.update(tid, driver="queue")' in co,
          "its handler is gone but the work is still wanted")
    check("and is not cancelled any more",
          "never started; its handler is gone" not in co,
          "that silently lost messages when you were three deep in a thread")
    check("nothing has to be recovered from Slack for it",
          "backfill" in co.lower(),
          "the record already holds the goal, thread and project")


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
    gate = src[src.index("def resolve_review("):src.index("def _task_scheduler(")]
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
    check("a task's base branch reaches the worktree",
          'base=scope.get("branch")' in bot)


# --- the dashboard has an icon ---------------------------------------------------
# Drawn separately from logo.svg on purpose: a favicon is read at 16px, where
# the logo's five body segments, dashed thread and face collapse into a smudge.

def test_favicon():
    print("\nfavicon")
    icon = BASE / "assets" / "favicon.svg"
    check("there is a favicon", icon.exists())
    body = icon.read_text() if icon.exists() else ""
    check("it is square, so browsers do not letterbox it",
          'viewBox="0 0 512 512"' in body)
    check("it is small enough to serve from memory", len(body) < 4000,
          f"{len(body)} bytes")
    check("it is simpler than the logo",
          body.count("<circle") < (BASE / "assets" / "logo.svg").read_text().count("<circle"),
          "detail that vanishes at 16px is detail that muddies it")

    viz = (BASE / "visualizer.py").read_text()
    check("the page asks for it", 'rel="icon"' in viz)
    check("it is served", '"/favicon.svg", "/favicon.ico"' in viz,
          "browsers request /favicon.ico unprompted; a 404 for it is log noise")
    check("with an image content type", '"image/svg+xml"' in viz)
    check("and read once rather than per request", "FAVICON = (BASE_DIR" in viz)
    check("a missing file does not break the dashboard", "FAVICON = b\"\"" in viz)


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
          "if (!showHidden && s.hidden) return false;" in viz)
    check("hidden threads stay reachable", "function toggleHidden" in viz,
          "put away is not thrown away")
    check("the hide control does not open the thread it hides",
          "ev.stopPropagation()" in viz)
    check("the payload says which are hidden", '"hidden": bool(entry.get("hidden"))' in viz)
    bot = (BASE / "bot.py").read_text()
    check("the bot exposes it", 'server.route("/hide", handle_hide)' in bot)
    check("and never deletes to satisfy it",
          "store.drop" not in bot[bot.index("def handle_hide"):bot.index("server = LocalServer")])


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
          ex.index("verify_work(task, cwd)") < ex.index("worktrees.release(worktree)"),
          "afterwards there is nothing left to test")
    rw = bot[bot.index('if action == "rework"'):bot.index('if action in ("accept"')]
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
    g(repo, "remote", "remove", "origin")

    bot = (BASE / "bot.py").read_text()
    li = bot[bot.index("def land_if_ready"):bot.index("def run_email_ingest")]
    check("a project must opt in", 'proj.get("auto_merge")' in li)
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
    import projects as P
    check("auto-merge is off by default", P.default("auto_merge") is False)


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

    recs = [{"slug": "trader", "ideate_at": "02:00", "ideate_on": "", "archived": False},
            {"slug": "saga", "ideate_at": "02:00", "ideate_on": "2026-09-06", "archived": False},
            {"slug": "odin", "ideate_at": "", "ideate_on": "", "archived": False},
            {"slug": "old", "ideate_at": "02:00", "ideate_on": "", "archived": True}]
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
    check("and an ideator cannot file more ideators",
          'role in ("reviewer", "ideator")' in h)
    ex = bot[bot.index("def execute_task"):bot.index("def resolve_review")]
    check("a read-only role gets no worktree",
          'not roles.get(role_name).get("restricted")' in ex,
          "it cannot write, so the worktree only leaves an empty branch behind")
    check("the scheduler records the date it ran", "ideate_on=" in bot,
          "or a restart in the small hours would run it twice")


def _file_task_impl():
    """Load handle_file_task out of bot.py without importing it.

    bot.py needs Slack tokens to import, so the route is compiled on its own
    against the real roles/scoping/tasks modules and a temporary task store.
    Only the two session lookups are stubbed -- the decision under test, and
    every rule it depends on, is the shipped code.
    """
    import types
    import roles as R, scoping as S, tasks as T
    from tasks import TaskStore

    src = (BASE / "bot.py").read_text()
    tree = ast.parse(src)
    want = ("handle_file_task", "filed_by_restricted_role", "_filed_this_turn")
    def named(n):
        if isinstance(n, ast.FunctionDef):
            return n.name
        if isinstance(n, ast.AnnAssign):
            return getattr(n.target, "id", "")
        return ""
    nodes = [n for n in tree.body if named(n) in want]
    assert len(nodes) == len(want), \
        f"expected all of {want}, found {[named(n) for n in nodes]}"

    ts = TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    # Naming a project writes: a record, and a home directory on disk. Recorded
    # rather than stubbed away, so a filing that gets refused can be shown to
    # have left nothing behind.
    made: list = []
    mod = types.ModuleType("filing")
    mod.__dict__.update(
        re=__import__("re"), roles=R, scoping=S, tasks=T, task_store=ts,
        log=logging.getLogger("test"), CLAUDE_CWD=Path(tempfile.mkdtemp()),
        store=types.SimpleNamespace(get=lambda k: {}),
        project_store=types.SimpleNamespace(
            ensure=lambda n, **kw: (made.append(n), {"slug": n})[1],
            home=lambda n, create=False: made.append(n),
            scope_for=lambda n: None),
    )
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<filing>", "exec"),
         mod.__dict__)
    return mod.handle_file_task, mod._filed_this_turn, ts, made


def _cli_file_task_impl():
    """Load `silkworm task` out of the CLI without a bot to call.

    bot_call is replaced with a recorder, so what the CLI would have sent is
    inspectable; everything up to it -- the argument parsing and the read of
    the environment the bot set up -- is the shipped code.
    """
    import types
    src = (BASE / "bin" / "silkworm").read_text()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "do_file_task")
    sent: list = []
    def bot_call(path, payload, timeout=3):
        sent.append((path, payload))
        return {"ok": True, "id": "tsk_0", "state": "proposed",
                "role": payload.get("role"), "project": payload.get("project"),
                "cwd": "/tmp", "remaining": 4}
    mod = types.ModuleType("cli")
    mod.__dict__.update(os=os, bot_call=bot_call)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<cli>", "exec"),
         mod.__dict__)
    return mod.do_file_task, sent


# --- a scoping conversation must be able to emit work ----------------------------
# Of 143 recent tasks, 130 were live conversation and 3 were made in the
# dashboard. Not a preference for chat: a conversation could not *emit*
# anything, so scoping ended with a plan in a thread and no way to act on it.

def test_scoping():
    import scoping as S, tasks as T
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

    bot = (BASE / "bot.py").read_text()
    h = bot[bot.index("def handle_file_task"):bot.index("server = LocalServer")]
    check("work scoped with you is queued, not proposed",
          "tasks.PROPOSED if propose else tasks.QUEUED" in h,
          "you scoped it with the user, who is who the proposed gate asks — "
          "only an unattended proposal has to wait")
    check("it defaults to implementor, so output gets reviewed",
          'payload.get("role") or "implementor"' in h)
    check("neither a reviewer nor an ideator can be filed as work",
          'role in ("reviewer", "ideator")' in h,
          "reviewing a review would not terminate, and an ideator that could "
          "file ideators would propose its way into a loop")
    check("it runs on the queue, not inline", 'driver="queue"' in h,
          "nobody is holding a live message for it")
    check("project and scope are inherited from the thread",
          'entry.get("project")' in h and "project_store.scope_for" in h,
          "a bound conversation should not restate where its work belongs")
    check("the per-turn budget resets each turn",
          "_filed_this_turn.pop(key, None)" in bot,
          "otherwise the cap becomes per-process and blocks later scoping")
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
          and [a.id for a in limits[0].args if isinstance(a, ast.Name)] == ["propose"]
          and "MAX_PER_TURN" not in h,
          "a proposal run told '9 more allowed' would go on filing")

    # Where a filed task lands is decided by who filed it, not by a flag the
    # filer chose. The ideator's allowlist is `Bash({bin} task:*)` -- a prefix,
    # so it permits an invocation with no --propose at all, and that filed
    # straight to `queued` with role implementor. The queue runner then ran it
    # overnight with full write permissions: read-only stopped the ideator
    # editing the tree, and did not stop it commissioning an agent that would.
    # Driven through the real route rather than read off the source, because
    # the thing that broke was the behaviour, not the wording.
    file_task, filed_this_turn, ts, projects_made = _file_task_impl()
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


def _bot_func(name, **namespace):
    """One function out of bot.py, loaded without importing it.

    bot.py wants Slack tokens at import time, so the function is compiled on
    its own into a namespace the caller supplies. Every free name it reads and
    the caller did not supply becomes a MagicMock, so a big function can be
    driven by handing it only the few things the behaviour under test turns
    on -- and so a name the function stops using is not silently still stubbed.
    """
    import builtins
    from unittest.mock import MagicMock
    tree = ast.parse((BASE / "bot.py").read_text())
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == name)
    bound = {a.arg for a in node.args.args}
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(n.name)
            bound.update(a.arg for a in n.args.args)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
    free = {n.id for n in ast.walk(node)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    mod = types.ModuleType(f"bot_{name}")
    for free_name in free - bound - set(dir(builtins)):
        mod.__dict__[free_name] = MagicMock(name=free_name)
    mod.__dict__.update(namespace)
    exec(compile(ast.Module(body=[node], type_ignores=[]), f"<{name}>", "exec"),
         mod.__dict__)
    return mod.__dict__[name]


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
        return any(isinstance(c, ast.Attribute) and c.attr == "create"
                   and getattr(c.value, "id", "") == "worktrees"
                   for c in ast.walk(node))
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
    check("orphans are swept periodically", 'name="wtsweep"' in bot)


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
    ns = bot_functions("stop_task", "handle_tasks", "live_worktree_tasks",
                       tasks=T, task_store=st, RUNNING_TASKS=running_tasks)
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
        check(f"{fname} lets it go in finally",
              "RUNNING_TASKS" in ast.dump(ast.Module(body=final, type_ignores=[])),
              "a handle left behind would keep a finished task's checkout forever")


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
    keep = bot_functions("live_worktree_tasks", tasks=T, task_store=st,
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
    w = bot[bot.index("def _credential_watcher"):bot.index("def _task_scheduler")]
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

# --- the thread list is two different things wearing one name --------------------
# Conversations you return to, and the one-off threads a task narrates into.
# Ten of the latter buries the former, and there was no way to say which.

def test_thread_kind_filter():
    import re, json as _j, subprocess as _sp, tempfile as _tf
    sys.argv = ["x"]
    import visualizer as V
    print("\nthreads can be told apart")

    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    # Drive the real functions rather than assert on their source: a filter that
    # renders but does not filter looks identical from the outside.
    # The stub has to come *before* the script: it wires listeners and timers
    # at load, so a prelude appended afterwards never runs.
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
// The page kicks off its own polling on load, which our stub fetch cannot
// satisfy. That is not what is under test; the filter is driven synchronously
// below and has already reported by the time any of it settles.
process.on("unhandledRejection", () => {});
"""
    # Drive the real functions rather than assert on their source: a filter that
    # renders but does not filter looks identical from the outside.
    drive = """
loadList = function () {};        // setKind calls it; not under test here
const _bar = _el("kindfilter");
const S = [{key: "a", kind: "thread"}, {key: "b", kind: "task"},
           {key: "c", kind: "task"}, {key: "d"}];
renderKindFilter(S);
const out = {bar: _bar.innerHTML, allVisible: S.filter(matchesKind).length};
setKind("task");
out.taskOnly = S.filter(matchesKind).map(s => s.key);
setKind("thread");
out.threadOnly = S.filter(matchesKind).map(s => s.key);
setKind("all");
out.backToAll = S.filter(matchesKind).length;
_bar.innerHTML = "";
renderKindFilter([{key: "x", kind: "thread"}]);
out.singleKindBar = _bar.innerHTML;
console.log(JSON.stringify(out));
"""
    harness = prelude + js + drive
    f = Path(_tf.mkdtemp()) / "h.js"
    f.write_text(harness)
    r = _sp.run(["node", str(f)], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        check("the filter runs in a browser-like context", False,
              (r.stderr or "").strip().splitlines()[-1] if r.stderr else "no output")
        return
    out = _j.loads(r.stdout.strip().splitlines()[-1])

    check("every kind present gets a button",
          "Conversations" in out["bar"] and "Task runs" in out["bar"])
    check("with its count, so the split is visible at a glance",
          "<b>2</b>" in out["bar"] and "<b>4</b>" in out["bar"])
    check("nothing is hidden by default", out["allVisible"] == 4)
    check("task runs can be isolated", out["taskOnly"] == ["b", "c"])
    check("conversations can be isolated", out["threadOnly"] == ["a", "d"],
          "a thread with no kind set is a conversation, not an unknown")
    check("and All comes back", out["backToAll"] == 4)
    check("one kind shows no filter at all", out["singleKindBar"] == "",
          "a single-option filter is noise, not a choice")


# --- finished work that never reached the base -------------------------------
# Eight tasks completed, the board recorded eight `done`, and eight fixes sat on
# eight branches nobody merged -- so none of those bugs were fixed in the bot
# that was actually running. Two of the eight were the same fix, implemented
# twice on different nights, because the first never landed and the gap was
# still there for the next pass to find.

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

    bot = (BASE / "bot.py").read_text()
    ideate = bot[bot.index("def run_ideation"):bot.index("def _ideation_scheduler")]
    check("the nightly pass actually carries the list",
          "scoping.unmerged_note(branches.survey(" in ideate,
          "the ideator re-derives what is fixed on a branch it was never shown")

    # Recorded as the checkout closes, because afterwards nothing knows.
    ex = bot[bot.index("def execute_task"):bot.index("MAX_VERIFY_ATTEMPTS")]
    check("the branch is written down before the checkout is released",
          ex.count("record_branch(tid, worktree, scope)") == 2
          and ex.index("record_branch(tid, worktree, scope)")
          < ex.index("worktrees.release(worktree)"),
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


def test_dashboard_js_is_whole():
    import re
    sys.argv = ["x"]
    import visualizer as V
    print("\ndashboard javascript")
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    defined = set(re.findall(r"(?:async\s+)?function\s+([A-Za-z_]\w*)", js))

    for name in ("renderKindFilter", "setKind", "matchesKind",
                 "loadList", "loadStats", "loadTranscript", "renderAlerts", "jumpTo",
                 "taskCall", "toggleTasks", "setTaskView", "renderProjects", "addTask",
                 "taskAction", "taskButtons", "renderTasks", "updateTaskBadge",
                 "refreshTaskBadge", "releaseThread", "retitle", "nameAllThreads",
                 "resummarize", "toggleLearn", "renderLearnings", "renderUnmerged"):
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

    done = st.compact_older_than(14)
    check("old finished tasks are compacted",
          sorted(done) == sorted([old, cancelled, reviewed]), f"got {done}")

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

    # No store may go back to truncate-then-write. Checked on the parse tree,
    # not the text, so a comment mentioning write_text doesn't pass for one.
    offenders, saves = [], 0
    def writes_in_place(node):
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("write_text", "write_bytes"))
    for mod in ("store.py", "tasks.py", "projects.py", "learnings.py",
                "harvester.py", "bot.py"):
        tree = ast.parse((BASE / mod).read_text())
        for node in ast.walk(tree):
            # The store file itself, whatever the surrounding function is
            # called. (Other files here -- a project's CLAUDE.md -- are fine.)
            if (writes_in_place(node) and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "_path"):
                offenders.append(f"{mod}:{node.lineno}")
            if isinstance(node, ast.FunctionDef) and node.name == "_save":
                saves += 1
                offenders += [f"{mod}:{n.lineno}" for n in ast.walk(node)
                              if writes_in_place(n)]
    check("every store's _save was found to check", saves == 4, f"found {saves}")
    check("no store writes its file in place", not offenders, f"at {offenders}")

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
          theirs.read_text() == "{half" and not jsonstore.corrupt_path(theirs).exists())
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
    try:
        jsonstore.load(bp, default={})
        raised = None
    except jsonstore.CorruptStore as exc:
        raised = exc
    check("both copies unreadable raises rather than starting empty",
          isinstance(raised, jsonstore.CorruptStore))
    check("and says where the wreckage is", raised and "both.json.corrupt" in str(raised))

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
    stop, bad, reads = _th.Event(), [], []
    def writer(n):
        for i in range(40):
            hs.create(f"w{n}-{i}")
    def reader():
        while not stop.is_set():
            try:
                reads.append(len(json.loads(hot.read_text())))
            except Exception as exc:         # noqa: BLE001
                bad.append(repr(exc))
    r = _th.Thread(target=reader, daemon=True); r.start()
    ws = [_th.Thread(target=writer, args=(n,)) for n in range(4)]
    [w.start() for w in ws]; [w.join() for w in ws]
    stop.set(); r.join(timeout=5)
    check("concurrent readers never see a partial file", not bad, f"{bad[:2]}")
    check("the reader actually read during the writes", len(reads) > 1)
    check("every write landed", len(hs.all()) == 161)

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
    src = ast.parse((BASE / "bot.py").read_text())
    fn = next(n for n in src.body if isinstance(n, ast.FunctionDef)
              and n.name == "file_followups")
    ns = {"task_store": store, "scoping": scoping, "tasks": T, "log": logging.getLogger("t"),
          "project_store": types.SimpleNamespace(scope_for=lambda p: None)}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<x>", "exec"), ns)
    file_followups = ns["file_followups"]

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

    many = file_followups(parent, [f"finding number {i}" for i in range(20)])
    check("filing is capped like any other unattended pass",
          len(many) == scoping.limit_for(propose=True))
    check("a followup that cannot be made into a valid goal is skipped, not filed",
          file_followups(parent, ["x" * (scoping.MAX_GOAL_CHARS + 1)]) == [],
          "better a lost note in the log than an unrunnable task on the board")

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
    gate_fn = next(n for n in src.body if isinstance(n, ast.FunctionDef)
                   and n.name == "resolve_review")
    posted = []
    gns = dict(ns)
    gns.update({
        "roles": roles, "task_store": store,
        "app": types.SimpleNamespace(client=types.SimpleNamespace(
            chat_postMessage=lambda **kw: posted.append(kw["text"]))),
        "land_if_ready": lambda t, c, th: "",
        "task_state": lambda tid, st, why="": store.transition(tid, st, why),
    })
    exec(compile(ast.Module(body=[gate_fn], type_ignores=[]), "<x>", "exec"), gns)
    resolve = gns["resolve_review"]

    work = store.create("do it", title="Do it", project="silkworm", role="implementor",
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



if __name__ == "__main__":
    for t in (test_resume_retry_requires_missing_transcript, test_stop_escalates_to_sigkill,
              test_timeout_is_distinct, test_recovery, test_procs,
              test_dashboard_classifiers, test_watermark, test_bounded_state,
              test_schema, test_command_dedup, test_viz_bind_requires_token,
              test_task_lifecycle, test_turn_is_a_task, test_task_runner_claim,
              test_review_gate, test_review_followups, test_email_ingest, test_modules_are_imported,
              test_missing_cwd_is_named, test_slack_health, test_backfill, test_defer, test_repo_guard, test_scoping, test_ideation, test_hiding_threads, test_favicon, test_verification, test_landing, test_parallel_tasks,
              test_worktrees, test_cancel_stops_the_child,
              test_worktree_survives_leaving_running,
              test_isolation_is_not_a_scheduling_decision,
              test_credentials_check,
              test_turn_deadline_is_idleness,
              test_mail_facts, test_projects,
              test_transient_retry, test_supersede_stale_failures,
              test_file_uploads_are_handled, test_thread_kind_filter,
              test_unmerged_branches,
              test_dashboard_js_is_whole,
              test_discarding_a_branch_keeps_it, test_task_retention,
              test_atomic_persistence):
        try:
            t()
        except Exception as exc:
            FAILED.append(f"{t.__name__} raised {exc!r}")
            print(f"  ✘ {t.__name__} raised {exc!r}")
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for f in FAILED:
        print(f"  FAILED: {f}")
    sys.exit(1 if FAILED else 0)

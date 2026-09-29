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
import shutil
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


def discover():
    """Every test in this file, in definition order.

    Found, not listed. This used to be a tuple of names written out by hand at
    the bottom of the file, and a tuple is silent about what is missing from
    it: a test left off it never ran, reported nothing, and the closing
    "N passed, 0 failed" still read as healthy. That is the wrong kind of
    silence now that a green run here is what authorises a branch to be rebased
    and landed with nobody watching.

    The one gap discovery leaves is a def that lands *below* the __main__
    guard, which is not bound yet when this looks -- `test_every_test_runs`
    reads the file itself to catch that.
    """
    return [fn for name, fn in globals().items()
            if name.startswith("test_") and isinstance(fn, types.FunctionType)
            and fn.__module__ == __name__]


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
          'daemons.start(_recovery_sweeper, "rsweep")' in src,
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
    ns = bot_functions("record_branch", "review_branch", "resolve_review",
                       roles=roles, tasks=T, task_store=st, worktrees=W,
                       branches=__import__("branches"),
                       task_state=lambda tid, state, detail="": st.transition(
                           tid, state, detail))

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
    ns["record_branch"](tid, wt, impl["scope"])
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
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "attach"]
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
    ns["record_branch"](gone, gwt, {"cwd": str(repo)})
    W.release(gwt)
    ns["resolve_review"](st.get(gone), "implementor", "did it", "C1", "1.0")
    orphan = [r for r in st.all().values() if r.get("parent") == gone][0]
    # The branch goes: a send-back re-runs the implementor under the same id,
    # and discard.drop clears the branch to get a fresh worktree.
    git(repo, "branch", "-D", f"{W.BRANCH_PREFIX}{gone}")
    st.transition(orphan["id"], T.RUNNING)
    review_turn(st.get(orphan["id"]),
                refuse_review=bot_functions(
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
    keeping = bot_functions("live_worktree_tasks", tasks=T, task_store=st,
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


def _bot_fns(names, ns):
    """Compile named top-level functions out of bot.py into `ns`.

    bot.py needs Slack tokens to import, so the pieces under test are lifted
    out of it the way _repo_guard_impl does. Returns the names it found, so a
    missing one fails a named check rather than raising.
    """
    tree = ast.parse((BASE / "bot.py").read_text())

    def defines(n):
        if isinstance(n, ast.FunctionDef):
            return {n.name}
        if isinstance(n, ast.Assign):
            return {t.id for t in n.targets if isinstance(t, ast.Name)}
        if isinstance(n, ast.AnnAssign):        # `_landing_now: set[str] = set()`
            return {n.target.id} if isinstance(n.target, ast.Name) else set()
        return set()

    nodes = [n for n in tree.body if defines(n) & set(names)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<bot>", "exec"), ns)
    return {name for n in nodes for name in defines(n)}


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
    check("a project must opt in, decided in one place",
          li.count('"auto_merge"') == 1 and "if not landing_enabled(task):" in li,
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
    _ps.ensure("Silkworm", ideate_at="02:00")
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


def _run_ideation_impl():
    """Load run_ideation out of bot.py without importing it.

    Mirrors _file_task_impl: bot.py needs Slack tokens to import, so the
    function is compiled on its own against the real stores and the real
    scoping/tasks modules. Only `branches.survey` is stubbed, because it
    shells out to git; the decision under test is the shipped code.
    """
    import types
    import projects as P, scoping as S, tasks as T
    from tasks import TaskStore

    src = (BASE / "bot.py").read_text()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "run_ideation")
    root = Path(tempfile.mkdtemp())
    ts, ps = TaskStore(root / "t.json"), P.ProjectStore(root / "p.json")
    mod = types.ModuleType("ideating")
    mod.__dict__.update(
        scoping=S, tasks=T, task_store=ts, project_store=ps, datetime=datetime,
        time=time,
        log=logging.getLogger("test"), CLAUDE_CWD=root, SILKWORM_BIN="silkworm",
        branches=types.SimpleNamespace(survey=lambda _t: []))
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<ideating>", "exec"),
         mod.__dict__)
    return mod.run_ideation, ts, ps


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
    ps.ensure("Silkworm", ideate_at="02:00")

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


def _file_task_impl():
    """Load handle_file_task out of bot.py without importing it.

    bot.py needs Slack tokens to import, so the route is compiled on its own
    against the real roles/scoping/tasks/projects modules and a temporary task
    store.
    Only the two session lookups are stubbed -- the decision under test, and
    every rule it depends on, is the shipped code.
    """
    import types
    import projects as P, roles as R, scoping as S, tasks as T
    from tasks import TaskStore

    src = (BASE / "bot.py").read_text()
    tree = ast.parse(src)
    want = ("handle_file_task", "filed_by_restricted_role", "_filed_this_turn",
            "begin_turn")
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
        re=__import__("re"), roles=R, scoping=S, tasks=T, projects=P,
        task_store=ts,
        log=logging.getLogger("test"), CLAUDE_CWD=Path(tempfile.mkdtemp()),
        store=types.SimpleNamespace(get=lambda k: {}),
        project_store=types.SimpleNamespace(
            ensure=lambda n, **kw: (made.append(n), {"slug": n})[1],
            home=lambda n, create=False: made.append(n),
            scope_for=lambda n: None),
    )
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<filing>", "exec"),
         mod.__dict__)
    return mod.handle_file_task, mod._filed_this_turn, mod.begin_turn, ts, made


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
              and n.func.attr == "slugify" and n.lineno < gate
              for n in ast.walk(fn)),
          "by_project is keyed by slug, and slugify is the pure way there")

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
        review_branch=bot_functions("review_branch",
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
    ns = bot_functions("stop_task", "handle_tasks", "live_worktree_tasks",
                       tasks=T, task_store=st, RUNNING_TASKS=running_tasks,
                       holding=H)
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
          'daemons.start(_credential_watcher, "creds")' in bot and "chat_postMessage" in w)
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
          "renderAlerts(data.sessions, data.slack," in viz,
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

    # And every test this file defines is actually run. The list below __main__
    # used to be hand-maintained, so a test could be written, pass on its own, and never
    # run in the suite -- which is how test_every_open_state_has_a_button first
    # went in: 970 checks passed without it. A test nothing calls is worse than
    # no test, because it reads as cover.
    own = ast.parse((BASE / "tests" / "test_invariants.py").read_text())
    # Top level and class bodies, not every nested def: a helper named test_*
    # inside another test is not a test. async too -- a plain FunctionDef check
    # would let `async def test_x` past, and the tuple cannot await one anyway.
    scopes = [own] + [n for n in own.body if isinstance(n, ast.ClassDef)]
    names = [n.name for sc in scopes for n in sc.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name.startswith("test_")]
    # Names the runner actually discovers, not every name defined: the list
    # below __main__ was replaced by discover(), and a def that lands below the
    # guard is still not bound when it looks.
    listed = {fn.__name__ for fn in discover()}
    check("this file defines tests at all", len(names) > 40, f"found {len(names)}")
    # Python takes the last of two same-named defs and says nothing. That is a
    # silent regression with a green suite -- the shadowed test simply stops
    # running -- and a set of names cannot see it.
    dupes = sorted({n for n in names if names.count(n) > 1})
    check("no test is defined twice", not dupes,
          f"shadowed, so only the last one runs: {', '.join(dupes)}")
    unrun = sorted(set(names) - listed)
    check("every test this file defines is one the runner discovers", not unrun,
          f"defined but never run: {', '.join(unrun)}")


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

    def prune(records, skip=()):
        seen.append((sorted(r["id"] for r in records), set(skip), lock.locked()))
        return []

    board = {"tsk_live": {"id": "tsk_live", "state": "done"},
             "tsk_marked": {"id": "tsk_marked", "state": "done",
                            "result": {"landing": {"stage": "in-progress"}}},
             "tsk_idle": {"id": "tsk_idle", "state": "done",
                          "result": {"landing": {"stage": "done"}}}}
    fn = _bot_func("_worktree_sweeper",
                   worktrees=types.SimpleNamespace(sweep=lambda keep: 0),
                   live_worktree_tasks=lambda: set(),
                   branches=types.SimpleNamespace(prune_merged=prune),
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
        t = st.create("do a thing", driver="queue", isolate=True,
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

    ns = bot_functions("handle_tasks", "approve_task", "stop_task", "tell_thread",
                       tasks=T, task_store=st, holding=H, RUNNING_TASKS={},
                       start_landing=lambda tid: None,
                       app=types.SimpleNamespace(client=FakeClient()))
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
    keep = bot_functions("live_worktree_tasks", tasks=T, task_store=st,
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
    drawn = js.find("list.innerHTML = r.tasks.map")
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
                 "refreshTaskBadge", "taskDetail", "lastEvent", "landing",
                 "releaseThread", "retitle", "nameAllThreads",
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

    # A marker nothing renders is the same silence it was written to end.
    row = re.search(r"async function renderTasks\(.*?^}", js, re.S | re.M).group(0)
    check("the task row says when commits never landed", "${landing(t)}" in row,
          "landing() exists but the board never calls it")


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
            targets["rework"] = moves_to(n)
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



# --- reviewed work must not read as done while it sits on a branch ------------
# Five branches carrying eleven commits reached `done` with nothing on the
# board to say so. Two of them were the same fix, proposed on two different
# nights, because the first never landed and the gap was still there to find.

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
        def __init__(self, rec):
            self.rec = rec
        def get(self, slug):
            return dict(self.rec)

    import branches as B
    gate = {"project_store": Projects({"auto_merge": True, "test_cmd": "true"}),
            "worktrees": W, "merge": M, "branches": B, "log": LOG}
    _bot_fns({"land_if_ready", "landing_enabled"}, gate)
    t = {"id": "tsk_guard", "project": "p", "scope": {"cwd": "/nowhere"}}

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
    rec_ns = {"task_store": store, "log": LOG}
    _bot_fns({"record_landing"}, rec_ns)
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
              "start_landing": lambda tid: bool(started.append(tid))}
    _bot_fns({"approve_task"}, app_ns)
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
    check("approving flagged work goes through the landing gate",
          bool(approve) and started == [reviewed],
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
              "land_if_ready": lambda *a, **k: ":hand: _Not landed (rebase)._"}
    _bot_fns({"resolve_review"}, rev_ns)
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
    sweep_ns = {"task_store": store, "log": LOG}
    _bot_fns({"record_landing", "clear_interrupted_landings", "LANDING_UNDERWAY"},
             sweep_ns)
    sweep = sweep_ns.get("clear_interrupted_landings")
    mid = store.create("do the thing", project="p")
    store.update(mid["id"], result={"landing": {
        "eligible": True, "landed": False, "stage": "in-progress",
        "branch": "silkworm/" + mid["id"], "detail": "landing under way"}})
    settled = store.create("do the thing", project="p")
    store.update(settled["id"], result={"landing": {
        "eligible": True, "landed": True, "stage": "done", "head": "abc1234"}})
    cleared = sweep() if sweep else []
    after_mid = ((store.get(mid["id"]).get("result") or {}).get("landing")) or {}
    check("a landing a restart interrupted stops claiming to be under way",
          bool(sweep) and cleared == [mid["id"]]
          and after_mid.get("stage") == "interrupted",
          f"got {cleared} / {after_mid.get('stage')!r}")
    check("and says nothing was merged",
          "nothing was merged" in (after_mid.get("detail") or ""))
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
                "threading": _th,
                "landing_enabled": lambda task: True,
                "land_and_record": lambda tid, c, t: (threads_run.append(tid),
                                                      holding.set(),
                                                      release.wait(5))}
    _bot_fns({"start_landing", "record_landing", "LANDING_UNDERWAY",
              "_landing_now", "_landing_guard"}, start_ns)
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
    _bot_fns({"approve_task"}, boom)
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


# --- a task row has to say what it is ---------------------------------------------
# Fifty-one proposals sat in the panel showing nothing but the goal's first line
# cut at sixty characters, while the whole goal -- two or three thousand words of
# evidence -- was already in the payload, fetched on every poll and thrown away.
# Accept spends a session, Dismiss throws one away, and both were being decided
# from a sentence fragment. The same held for a failure: Retry and Dismiss, with
# the reason sitting unread in events[-1].detail.

DASHBOARD_DRIVER = r"""
// Runs the dashboard's own javascript against a stub DOM and a stub fetch, then
// prints the task list exactly as the browser would build it. The point is to
// drive the real renderTasks rather than to read its source: a row that should
// show its goal has to actually show it.
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const tasks = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
process.on("unhandledRejection", () => {});

function el(id) {
  return {id, innerHTML: "", textContent: "", value: "", title: "", className: "",
          style: {}, disabled: false, dataset: {}, children: [],
          classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
          appendChild() {}, removeChild() {}, remove() {}, addEventListener() {},
          insertAdjacentHTML() {}, focus() {}, scrollIntoView() {},
          querySelector() { return null; }, querySelectorAll() { return []; },
          getBoundingClientRect() { return {top: 0, left: 0, width: 0, height: 0}; }};
}
const els = {};
const byId = id => els[id] || (els[id] = el(id));
globalThis.document = {getElementById: byId, createElement: () => el("new"),
                       querySelector: () => null, querySelectorAll: () => [],
                       addEventListener() {}, body: el("body")};
globalThis.window = {addEventListener() {}, location: {search: "", hash: "", href: ""},
                     matchMedia: () => ({matches: false, addEventListener() {}})};
globalThis.location = globalThis.window.location;
globalThis.localStorage = {getItem: () => null, setItem() {}, removeItem() {}};
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
globalThis.fetch = async (url, opts) => {
  const body = opts && opts.body ? JSON.parse(opts.body) : {};
  let out = {ok: true};
  if (url.startsWith("/api/sessions")) out = {bot_online: true, sessions: [], slack: {}};
  else if (url.startsWith("/api/stats"))
    out = {total_cost: 0, cache_rate: null, threads: 0, days: []};
  else if (url.startsWith("/api/projects")) out = {ok: true, projects: []};
  else if (url.startsWith("/api/tasks"))
    out = (body.action === "list" || body.action === "attention")
      ? {ok: true, tasks, counts: {}} : {ok: true};
  return {json: async () => out, text: async () => ""};
};

(0, eval)(src + "\n;globalThis.__renderTasks = renderTasks;");
(async () => {
  await globalThis.__renderTasks();
  process.stdout.write(byId("tlist").innerHTML);
})();
"""


def test_task_row_shows_its_own_goal():
    import re
    import shutil
    sys.argv = ["x"]
    import visualizer as V
    print("\na task row carries its goal and its reason")
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)

    # A nightly proposal: the title is the first line, the case is 2,600
    # characters below it, and the panel has to be able to show both.
    goal = ("Auto-merge lands work on local main and never publishes it, so the next "
            "branch rebases onto a stale base.\n\n"
            + "merge.py:118 rebases onto origin/HEAD but fast-forwards the local "
              "checkout, and nothing pushes. " * 24
            + "\n\nDone: land() pushes, or says why it did not. <script>alert(1)</script>\n"
              "FINAL LINE OF THE CASE")
    assert len(goal) > 2000, "the fixture has to be long enough to be truncated"
    deep = "RAN OUT OF ROOM IN THE TITLE"
    goal = goal[:1400] + deep + goal[1400:]

    rows = [
        {"id": "tsk_proposed1", "state": "proposed", "title": goal.splitlines()[0][:60],
         "goal": goal, "project": "silkworm", "source": "ideate", "attempts": 1,
         "created": time.time() - 3600, "thread": "", "events": [], "result": {}},
        {"id": "tsk_failed1", "state": "failed", "title": "Say whether a cancelled task stopped",
         "goal": "Say whether a cancelled task stopped\n\nand more besides", "project": "",
         "source": "ui", "attempts": 2, "created": time.time() - 7200, "thread": "",
         "result": {},
         "events": [{"at": 1, "kind": "running", "detail": "claimed by the runner"},
                    {"at": 2, "kind": "failed",
                     "detail": "claude exited 1: NO WORKTREE, BRANCH ALREADY EXISTS"}]},
        # A goal that fits in its title has nothing behind it, and opening an
        # empty disclosure is worse than no disclosure.
        {"id": "tsk_short1", "state": "queued", "title": "Bump the favicon",
         "goal": "Bump the favicon", "project": "", "source": "ui", "attempts": 1,
         "created": time.time() - 60, "thread": "", "events": [], "result": {}},
    ]

    node = shutil.which("node")
    if not node:
        # Couldn't run is not the same as passed. Fall back to the weaker
        # structural reading so the invariant is not simply unchecked here.
        print("  … node not installed — cannot run the page's own javascript")
        body = js[js.index("async function renderTasks"):]
        check("the row is built from taskDetail(t), which reads t.goal",
              "taskDetail(t)" in body and "t.goal" in js)
        return

    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    (d / "tasks.json").write_text(json.dumps(rows))
    (d / "drive.js").write_text(DASHBOARD_DRIVER)
    p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js"), str(d / "tasks.json")],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        check("the dashboard's javascript runs", False, p.stderr.strip()[-400:])
        return
    html = p.stdout

    check("the proposal's full goal reaches the row",
          deep in html and "FINAL LINE OF THE CASE" in html,
          "the panel still shows only the truncated title")
    check("a failed row says why it failed",
          "NO WORKTREE, BRANCH ALREADY EXISTS" in html,
          "Retry and Dismiss with no reason on offer")
    check("the goal is escaped, not injected",
          "&lt;script&gt;alert(1)&lt;/script&gt;" in html
          and "<script>alert(1)</script>" not in html)

    # Scannable by default: fifty rows of three thousand characters is only a
    # different way of being unreadable.
    opened = _re.findall(r"<details[^>]*>", html)
    check("every row opens shut", opened and not any("open" in o for o in opened),
          f"found: {opened}")
    # Scope this to the row itself: a window of characters would reach back
    # into the proposal above it and find that row's disclosure instead.
    short = next(r for r in html.split('<div class="task">') if "tsk_short1" in r)
    check("a goal that fits its title gets no disclosure at all",
          "<details" not in short, "an empty expander is worse than none")


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
    # Five: the four stores, and the task board's message pointer (home.Board).
    check("every store's _save was found to check", saves == 5, f"found {saves}")
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
    check("a review files nothing onto a board already at its standing limit",
          file_followups(deep, ["something genuinely worth a decision"]) == []
          and len(store.all()) == at_limit,
          "the nightly pass stops here; the other producer of proposals must too")
    check("and a project with room is unaffected by another's backlog",
          len(file_followups(parent, ["a finding on a project with room"])) == 1,
          "the limit is per project, like the nightly pass it mirrors")
    # One place left, so the batch has to stop partway rather than being
    # refused outright -- the case that tells a live count from one taken once.
    spare = next(t for t in store.by_project("backlogged")
                 if t["state"] == T.PROPOSED and t["goal"].startswith("Padding"))
    store.transition(spare["id"], T.CANCELLED, "dismissed")
    got = file_followups(deep, [f"another finding number {i}" for i in range(5)])
    check("and with one place left it files one, not the whole batch",
          len(got) == 1,
          "a count taken once for the batch would file all five past the limit")
    check("which leaves the board exactly full, never over",
          scoping.open_proposals(store.by_project("backlogged"))
          == scoping.max_open_proposals())

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
        # Landing has its own cases in test_landing_is_visible; here it must
        # simply not stand between the verdict and where it routes.
        "merge": __import__("merge"),
        "land_and_record": lambda tid, c, th: {"eligible": True, "landed": True,
                                               "stage": "done", "head": "abc1234"},
        "task_state": lambda tid, st, why="": store.transition(tid, st, why),
    })
    exec(compile(ast.Module(body=[gate_fn], type_ignores=[]), "<x>", "exec"), gns)
    resolve = gns["resolve_review"]

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

    ns = bot_functions("run_email_ingest",
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




# --- the Home tab: the task board, away from the local network ---------------
# The dashboard binds loopback, so decisions that waited on it waited until you
# were home. The Home tab is the same board in Slack. What must not happen is
# for the two to disagree about what a click may do, for the board to leak to
# someone off the allowlist, or for an oversized backlog to make Slack refuse
# the whole view and show nothing.

def test_home_tab():
    import home
    import tasks as T
    print("\nthe Home tab")

    class Client:
        def __init__(self):
            self.published, self.opened = [], []
        def views_publish(self, user_id, view):
            self.published.append((user_id, view))
        def views_open(self, trigger_id, view):
            self.opened.append(view)

    def texts(view):
        return json.dumps(view, ensure_ascii=False)

    def buttons(view, tid):
        for b in view["blocks"]:
            if b.get("block_id") == f"a:{tid}":
                return [e["action_id"].removeprefix("home_") for e in b["elements"]]
        return []

    now = time.time()
    def rec(tid, state, **kw):
        return {"id": tid, "state": state, "goal": f"goal of {tid}",
                "updated": now - 3600, "created": now - 7200, **kw}

    board = [rec("tsk_prop", T.PROPOSED), rec("tsk_aw", T.AWAITING_APPROVAL,
                 result={"review": {"summary": "looks close",
                                    "findings": ["one", "two", "three", "four"]}},
                 verified=True),
             rec("tsk_in", T.NEEDS_INPUT), rec("tsk_fail", T.FAILED,
                 events=[{"kind": "failed", "detail": "claude exited (code 143)"}]),
             rec("tsk_run", T.RUNNING), rec("tsk_done", T.DONE)]
    view = home.render(board, now=now)

    # Parity with the dashboard. Its taskButtons() is the other place a state
    # decides what you may do; read it rather than restate it here.
    js = (BASE / "visualizer.py").read_text()
    fn = js[js.index("function taskButtons(t)"):]
    fn = fn[:fn.index("return b.join")]
    dash = {}
    for cond, body in _re.findall(r'(?:if|else if) \(([^)]*)\) \{(.*?)\n  \}', fn, _re.S):
        acts = _re.findall(r"taskAction\('\$\{t\.id\}','(\w+)'\)", body)
        acts += ["answer" if flag == "true" else "rework"
                 for flag in _re.findall(r"sendBack\('\$\{t\.id\}',(true|false)\)", body)]
        for st in _re.findall(r't\.state === "(\w+)"', cond):
            dash[st] = set(acts)
    check("the dashboard's buttons were read", len(dash) >= 5, str(dash))
    for st, acts in dash.items():
        mine = {a for _, a, _, _ in home.BUTTONS.get(st, [])}
        check(f"{st}: same actions as the dashboard", mine == acts,
              f"home {sorted(mine)} vs dashboard {sorted(acts)}")

    # Every action a button sends must be one handle_tasks actually handles,
    # or the click comes back "unknown action" on your phone.
    src = (BASE / "bot.py").read_text()
    ht = src[src.index("def handle_tasks("):src.index("def handle_projects(")]
    handled = set(_re.findall(r'action == "(\w+)"', ht))
    for tup in _re.findall(r'action in \(([^)]*)\)', ht):
        handled |= set(_re.findall(r'"(\w+)"', tup))
    for a in home.DIRECT + ("rework",):
        check(f"handle_tasks handles {a!r}", a in handled)

    check("awaiting approval offers approve / send back / dismiss",
          buttons(view, "tsk_aw") == ["approve", "rework", "dismiss"])
    check("needs input offers answer", "answer" in buttons(view, "tsk_in"))
    check("approve asks before landing", any(
        e.get("confirm") for b in view["blocks"] if b.get("block_id") == "a:tsk_aw"
        for e in b["elements"] if e["action_id"] == "home_approve"))
    s = texts(view)
    check("the review is shown before you approve", "looks close" in s and "one" in s)
    check("findings beyond three are counted, not dropped", "1 more finding" in s)
    check("why it failed is shown", "code 143" in s)
    check("running work is listed", "goal of tsk_run" in s)
    check("finished work is not on the board", "tsk_done" not in s)
    order = [b["block_id"][2:] for b in view["blocks"] if b.get("block_id", "").startswith("t:")]
    check("most urgent first: approval, input, failed, proposed",
          order == ["tsk_aw", "tsk_in", "tsk_fail", "tsk_prop"], str(order))

    # Goals are arbitrary text. Unescaped, one containing <!channel> would be
    # a live mention in the view.
    evil = home.render([rec("tsk_x", T.PROPOSED, title="ping <!channel> & <http://x|y>")], now=now)
    s = texts(evil)
    check("goals are escaped for mrkdwn", "<!channel>" not in s and "&lt;!channel&gt;" in s)

    # Slack rejects a Home view over 100 blocks -- the whole view, not the
    # tail -- so a large backlog must be cut, and the cut must be counted.
    many = [rec(f"tsk_{i:03}", T.PROPOSED) for i in range(300)]
    big = home.render(many + [rec("tsk_r", T.RUNNING)], now=now,
                      watching=[{"goal": "w", "in_s": 60}], unmerged="67 finished tasks")
    shown = sum(1 for b in big["blocks"] if b.get("block_id", "").startswith("t:"))
    m = _re.search(r"…and (\d+) more waiting", texts(big))
    check("a huge backlog stays under Slack's 100-block limit", len(big["blocks"]) <= 100,
          str(len(big["blocks"])))
    check("and says how many it left out", bool(m) and shown + int(m.group(1)) == 300,
          f"shown {shown}, overflow {m and m.group(1)}")
    s = texts(big)
    check("the tail sections survive a full board",
          "Running" in s and "Watching" in s and "67 finished tasks" in s)
    for b in big["blocks"]:
        ids = [e["action_id"] for e in b.get("elements", []) if "action_id" in e]
        if len(ids) != len(set(ids)):
            check("action ids unique within a block", False, str(ids)); break
        if len(json.dumps(b.get("text", {}))) > 3100:
            check("section text within Slack's limit", False); break

    check("an empty board says so", "Nothing needs you" in texts(home.render([], now=now)))
    check("unknown age is not fifty years", home.ago(0, now) == "?")
    check("an overdue wake-up says so", home.until(-30) == "due now")
    check("thread links from channel:ts", home.thread_url("D1:1790.25", "https://x.slack.com/")
          == "https://x.slack.com/archives/D1/p179025?thread_ts=1790.25&cid=D1")

    # --- clicks, through the real handle_tasks and a real store ---------------
    store = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    ns = bot_functions("handle_tasks", "approve_task", task_store=store, tasks=T,
                       holding=__import__("holding"),
                       stop_task=lambda tid: False, start_landing=lambda tid: None)
    prop = store.create("a proposal worth accepting", state=T.PROPOSED)["id"]
    need = store.create("a task that asked a question", state=T.QUEUED)["id"]
    store.transition(need, T.RUNNING); store.transition(need, T.NEEDS_INPUT)
    calls = []
    def call(p):
        calls.append(p)
        return ns["handle_tasks"](p)

    h = home.Home(store=store, call=call, allowed_users={"U_ME"})
    c = Client()
    click = lambda user, action, tid: {"user": {"id": user}, "trigger_id": "trig",
                                       "actions": [{"action_id": f"home_{action}", "value": tid}]}
    acked = []
    ack = lambda *a, **k: acked.append(k)

    h.on_direct(ack, click("U_STRANGER", "accept", prop), c)
    check("someone off the allowlist cannot act", not calls
          and store.get(prop)["state"] == T.PROPOSED)
    check("and is shown nothing of the board",
          "a proposal worth" not in texts(c.published[-1][1]))

    h.on_direct(ack, click("U_ME", "accept", prop), c)
    check("Accept moves the real task to queued", store.get(prop)["state"] == T.QUEUED)
    check("attributed to the Slack user", "slack:U_ME" in json.dumps(store.get(prop)["events"]))
    check("the result is shown at the top of the tab",
          "Accepted" in texts(c.published[-1][1]))

    h.on_direct(ack, click("U_ME", "accept", prop), c)          # already queued
    h.on_direct(ack, click("U_ME", "dismiss", "tsk_nope"), c)
    check("a refused click says why rather than failing silently",
          "Couldn't dismiss" in texts(c.published[-1][1]))

    h.on_modal(ack, click("U_ME", "answer", need), c)
    modal = c.opened[-1]
    check("Answer opens a form", modal["callback_id"] == home.MODAL_CALLBACK)
    submit = lambda notes: {"private_metadata": modal["private_metadata"],
                            "state": {"values": {"notes": {"notes": {"value": notes}}}}}
    before = len(calls); acked.clear()
    h.on_submit(ack, {"user": {"id": "U_ME"}}, c, submit("  "))
    check("a blank answer is refused in the form", len(calls) == before
          and acked and acked[-1].get("response_action") == "errors")
    h.on_submit(ack, {"user": {"id": "U_ME"}}, c, submit("use the second option"))
    t = store.get(need)
    check("an answer requeues the task with it", t["state"] == T.QUEUED
          and "use the second option" in t["goal"])
    check("closing the form does nothing: no close handler is asked for",
          not modal.get("notify_on_close"))

    # A Home view is not re-rendered until you open it again, and running may
    # legally move to queued or done. So a button rendered for one state and
    # clicked in another must be refused, or a stale Retry requeues work that
    # is running -- two agents on one checkout -- and a stale Approve closes it.
    fl = store.create("a task that failed once", state=T.QUEUED)["id"]
    store.transition(fl, T.RUNNING); store.transition(fl, T.FAILED)
    shown = h.view_for("U_ME")
    retry = next(e for b in shown["blocks"] if b.get("block_id") == f"a:{fl}"
                 for e in b["elements"] if e["action_id"] == "home_retry")
    store.transition(fl, T.QUEUED); store.transition(fl, T.RUNNING)   # retried elsewhere
    before = len(calls)
    h.on_direct(ack, {"user": {"id": "U_ME"}, "trigger_id": "t",
                      "actions": [{"action_id": "home_retry", "value": retry["value"]}]}, c)
    check("a stale button does not act on a task that has moved on",
          len(calls) == before and store.get(fl)["state"] == T.RUNNING)
    check("and says so", "moved on" in texts(c.published[-1][1]))
    aw = store.create("work to approve", state=T.QUEUED)["id"]
    store.transition(aw, T.RUNNING); store.transition(aw, T.AWAITING_APPROVAL)
    shown = h.view_for("U_ME")
    rework = next(e for b in shown["blocks"] if b.get("block_id") == f"a:{aw}"
                  for e in b["elements"] if e["action_id"] == "home_rework")
    h.on_modal(ack, {"user": {"id": "U_ME"}, "trigger_id": "t",
                     "actions": [{"action_id": "home_rework", "value": rework["value"]}]}, c)
    form = c.opened[-1]
    store.transition(aw, T.DONE)                                      # approved elsewhere
    before = len(calls)
    h.on_submit(ack, {"user": {"id": "U_ME"}}, c,
                {"private_metadata": form["private_metadata"],
                 "state": {"values": {"notes": {"notes": {"value": "redo it"}}}}})
    check("a send-back form opened before it was approved does not reopen it",
          len(calls) == before and store.get(aw)["state"] == T.DONE)

    h.on_opened({"tab": "messages", "user": "U_ME"}, c)
    n = len(c.published)
    h.on_opened({"tab": "home", "user": "U_ME"}, c)
    check("opening the Home tab publishes it; the Messages tab does not",
          len(c.published) == n + 1)



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
# --- a thread link opens the thread, not the app -------------------------------
# A bare /archives/<channel>/p<ts> link names the message but not the
# conversation, and for a DM with an app Slack resolves it to the app. Once the
# Home tab was switched on, the app opened on Home: every "open thread" link --
# dashboard, !sessions, the Home tab itself -- landed there instead.

def test_thread_links_open_the_thread():
    import slacklinks
    print("\na thread link opens the thread")
    # Exactly what chat.getPermalink returned for this thread on 09-28.
    real = ("https://stryin.slack.com/archives/D0BH336V73L/p1784068538524369"
            "?thread_ts=1784068538.524369&cid=D0BH336V73L")
    check("matches the permalink Slack itself generates",
          slacklinks.thread_link("D0BH336V73L", "1784068538.524369",
                                 "https://stryin.slack.com/") == real)
    check("from a thread key", slacklinks.for_key("D0BH336V73L:1784068538.524369",
                                                  "https://stryin.slack.com") == real)
    check("not a key, no link", slacklinks.for_key("", "https://x") == ""
          and slacklinks.for_key("nothing", "https://x") == "")

    # One builder. Anything else hand-assembling an archive link will have the
    # old shape back within a feature or two.
    for f in sorted(BASE.glob("*.py")):
        if f.name == "slacklinks.py":
            continue
        src = f.read_text()
        py = [l for l in src.splitlines() if "archives/" in l and "function threadLink" not in l
              and "`https://slack.com/archives/${ch}" not in l]
        check(f"{f.name} builds no thread link of its own", not py, py[:1])

    # The dashboard builds its link in the browser; run it and compare.
    js = (BASE / "visualizer.py").read_text()
    fn = js[js.index("function threadLink(key)"):]
    fn = fn[:fn.index("\n}\n") + 3]
    out = subprocess.run(["node", "-e", fn + "\nprocess.stdout.write(threadLink('D0BH336V73L:1784068538.524369'))"],
                         capture_output=True, text=True)
    check("the dashboard's link is the same one",
          out.stdout == slacklinks.thread_link("D0BH336V73L", "1784068538.524369"),
          out.stdout or out.stderr[:200])



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

    fn = next(n for n in ast.parse((BASE / "bot.py").read_text()).body
              if isinstance(n, ast.FunctionDef) and n.name == "handle_tasks")
    ts = TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    made: list = []
    mod = types.ModuleType("dashboard")
    mod.__dict__.update(
        roles=R, tasks=T, task_store=ts, time=time,
        log=logging.getLogger("test"), CLAUDE_CWD=Path(tempfile.mkdtemp()),
        project_store=types.SimpleNamespace(
            ensure=lambda n, **kw: (made.append(n), {"slug": n})[1],
            home=lambda n, create=False: made.append(n),
            scope_for=lambda n: None),
    )
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<dashboard>", "exec"),
         mod.__dict__)
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
    check("the form offers the choice at all", 'id="trole"' in viz)
    check("it asks the route what the choices are", 'action: "roles"' in js)
    check("and sends back what was picked", "role: role || undefined" in js)
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


# --- no test is defined and then never run ------------------------------------
# For as long as the suite ran a hand-written list, a test added to this file
# but left off that list never ran, said nothing about it, and left the total
# looking healthy. Discovery removed the list; this covers the last way a test
# defined here could still fail to run.

def test_every_test_runs():
    print("\nevery test defined here is actually run")
    defined = [n.name for n in ast.parse(Path(__file__).read_text()).body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name.startswith("test_")]
    # Without this, a parse that found nothing would leave the check below
    # passing over an empty list.
    check("this file's tests can be read back from its source", len(defined) > 1,
          "found no test definitions to compare against")
    dupes = sorted({n for n in defined if defined.count(n) > 1})
    check("no test is defined twice, shadowing the earlier one", not dupes,
          f"defined more than once: {', '.join(dupes)}")
    missing = sorted(set(defined) - {fn.__name__ for fn in discover()})
    check("every test_* defined in this file is discovered and run", not missing,
          f"never runs: {', '.join(missing)} — defined below the __main__ guard?")


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
    check("from the checkout it was loaded from, not the workspace",
          assigns and "BASE_DIR" in ast.dump(assigns[0].value)
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
          guarded and "silkworm restart" in ast.dump(guarded[0]),
          "the warning has to name the remedy")

    # And the predicate itself, run rather than read.
    own = bot_functions("_is_own_checkout", Path=Path, BASE_DIR=BASE)["_is_own_checkout"]
    check("its own checkout is recognised", own(str(BASE)) is True)
    check("through a non-canonical path", own(str(BASE / "tests" / "..")) is True)
    check("another project's checkout is not", own(str(BASE / "tests")) is False)
    check("and a path that cannot be resolved is not", own("") is False)

    cli = (BASE / "bin" / "silkworm").read_text()
    check("silkworm status reads the bot's revision, not git",
          'bot.get("revision")' in cli and "rev-parse" not in cli)
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


# --- a failing sweep costs one pass, never the thread --------------------------
# _sweeper guarded only its middle step. The session step ends in a jsonstore
# save a full disk fails, and iterdir() then stat() races the outbox dirs turns
# create -- either ended the thread, which is the only caller of both, so
# tasks.json would never have compacted again and nothing would have said so.

class _Stop(Exception):
    """Raised by a fake sleep to end an otherwise endless loop under test."""


def _sweeper_ns(outbox, *, forget=None, compact=None):
    import shutil as _shutil
    ran = []

    class Store:
        def forget_empty(self, days, keep=()):
            ran.append("forget")
            if forget:
                raise forget
            return []

    class Tasks:
        def all(self):
            return {}

        def compact_older_than(self, days):
            ran.append("compact")
            if compact:
                raise compact

    ns = bot_functions("_sweep_pass", "_sweeper", store=Store(), task_store=Tasks(),
                       OUTBOX_ROOT=outbox, shutil=_shutil,
                       SESSION_MAX_AGE_DAYS=30, TASK_COMPACT_AFTER_DAYS=14)
    ns["log"] = logging.getLogger("test.sweeper")
    ns["log"].disabled = True
    return ns, ran


def _old_outbox(root, name):
    d = root / name
    d.mkdir()
    old = time.time() - 3 * 86400
    os.utime(d, (old, old))
    return d


def test_sweeper_survives_a_raising_step():
    print("\nthe sweeper survives an exception from any of its steps")

    # Each step raising in turn: the pass returns, and the other steps still ran.
    for which in ("forget", "compact", "outbox"):
        root = Path(tempfile.mkdtemp())
        stale = _old_outbox(root, "old")
        outbox = root
        if which == "outbox":
            class Gone:                       # the directory vanished under us
                def iterdir(self):
                    raise FileNotFoundError("outbox root removed")
            outbox = Gone()
        ns, ran = _sweeper_ns(outbox,
                              forget=OSError(28, "No space left") if which == "forget" else None,
                              compact=OSError(30, "Read-only") if which == "compact" else None)
        try:
            ns["_sweep_pass"]()
            raised = None
        except Exception as e:                # noqa: BLE001
            raised = e
        check(f"a raising {which} step does not escape the pass", raised is None, repr(raised))
        check(f"and the other steps still ran when {which} raised",
              ran == ["forget", "compact"], f"ran {ran}")
        if which != "outbox":
            check(f"old outboxes are still swept when {which} raised", not stale.exists())

    # The race itself: a dir listed by iterdir() and gone before stat().
    root = Path(tempfile.mkdtemp())
    before, after = _old_outbox(root, "a"), _old_outbox(root, "z")

    class Vanished:                           # still a dir at is_dir(), gone at stat()
        def is_dir(self):
            return True

        def stat(self):
            raise FileNotFoundError(2, "No such file or directory", "m-vanished")
    vanished = Vanished()

    class Racy:
        def iterdir(self):
            yield before
            yield vanished                    # listed, then removed before stat
            yield after
    ns, _ = _sweeper_ns(Racy())
    ns["_sweep_pass"]()
    check("a dir removed mid-walk is skipped, not fatal to the walk",
          not before.exists() and not after.exists(),
          "the directories after the vanished one must still be swept")

    # The loop itself: every step raising, three passes, and it is still going.
    root = Path(tempfile.mkdtemp())
    class Gone:
        def iterdir(self):
            raise FileNotFoundError("gone")
    ns, ran = _sweeper_ns(Gone(), forget=OSError("disk full"), compact=RuntimeError("bad"))
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise _Stop
    ns["time"] = types.SimpleNamespace(sleep=sleep, time=time.time)
    try:
        ns["_sweeper"]()
    except _Stop:
        pass
    check("with every step raising, the loop keeps going pass after pass",
          len(sleeps) == 3 and ran.count("forget") == 3 and ran.count("compact") == 3,
          f"sleeps={sleeps} ran={ran}")

    # And the backstop: whatever is added to the pass later cannot end the loop.
    calls = []

    def boom():
        calls.append(1)
        raise ValueError("a future step nobody guarded")
    ns["_sweep_pass"] = boom
    sleeps.clear()
    try:
        ns["_sweeper"]()
    except _Stop:
        pass
    except ValueError:
        pass
    check("an exception escaping the pass costs one round, not the thread",
          len(calls) == 3, f"pass ran {len(calls)} time(s) before the loop ended")


# --- a dead background thread is visible ----------------------------------------
# Nothing checked whether any of the ~14 daemon threads was still alive. A loop
# that raised simply stopped, logged nothing further, and the process -- and
# every health check -- carried on looking fine.

def test_dead_background_threads_are_reported():
    import threading as _threading
    import daemons
    print("\nstatus names any background thread that has stopped")

    hook = _threading.excepthook
    _threading.excepthook = lambda args: None      # the raise is the point here
    daemons.reset()
    try:
        hold = _threading.Event()

        def forever():
            hold.wait()

        def crash():
            raise OSError(28, "No space left on device")

        def finishes():
            return

        daemons.start(forever, "alive")
        daemons.start(crash, "crashed")
        daemons.start(finishes, "loop-returned")
        daemons.start(finishes, "startup-pass", forever=False)
        daemons.start(crash, "startup-crashed", forever=False)
        daemons.start(lambda n: hold.wait(), "worker0", args=(0,))
        for t in _threading.enumerate():
            if t.name in ("crashed", "loop-returned", "startup-pass", "startup-crashed"):
                t.join(5)

        st = daemons.status()
        gone = {d["name"]: d for d in st["dead"]}
        check("a live loop is not reported", "alive" not in gone and "worker0" not in gone)
        check("a loop that raised is reported, with why",
              "crashed" in gone and "No space left" in gone["crashed"]["reason"],
              str(gone.get("crashed")))
        check("a loop meant to run forever that returned is reported",
              "loop-returned" in gone
              and gone["loop-returned"]["reason"] == "returned without raising")
        check("a startup pass that finished is not reported", "startup-pass" not in gone)
        check("but one that raised is", "startup-crashed" in gone)
        check("counts add up: a finished startup pass is neither alive nor dead",
              st["count"] == 6 and st["alive"] == 2 and len(st["dead"]) == 3,
              str(st))
        check("dead_for is measured from when it was first noticed",
              daemons.dead(time.time() + 120)[0]["dead_for"] >= 119)
        hold.set()
    finally:
        _threading.excepthook = hook
        daemons.reset()

    # bot.py: the endpoint reports them, and every boot-time loop is registered.
    src = (BASE / "bot.py").read_text()
    tree = ast.parse(src)
    status_fn = next(n for n in tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == "handle_status")
    check("/status carries daemons.status()",
          "daemons.status()" in ast.unparse(status_fn))
    main = next(n for n in tree.body if isinstance(n, ast.If)
                and "__name__" in ast.unparse(n.test))
    raw = [n for n in ast.walk(main) if isinstance(n, ast.Call)
           and ast.unparse(n.func) in ("threading.Thread", "Thread")]
    check("no background loop is started behind the registry's back", not raw,
          f"{len(raw)} bare threading.Thread in __main__")
    started = {n.args[0].id for n in ast.walk(main) if isinstance(n, ast.Call)
               and ast.unparse(n.func) == "daemons.start" and n.args
               and isinstance(n.args[0], ast.Name)}
    for name in ("_sweeper", "_worktree_sweeper", "_recovery_sweeper", "_watchdog",
                 "_task_scheduler", "_task_worker", "_ideation_scheduler",
                 "_credential_watcher", "_harvester", "_email_watcher",
                 "_slack_watchdog", "_recoverer", "_backfiller"):
        check(f"{name} is registered", name in started)

    # The real handle_status, against fakes, returns what the CLI will read.
    daemons.reset()
    try:
        daemons.start(lambda: None, "sweeper")
        for t in _threading.enumerate():
            if t.name == "sweeper":
                t.join(5)
        ns = bot_functions(
            "handle_status", store=types.SimpleNamespace(all=lambda: {}), RUNNING={},
            RUNNER_HOLD=__import__("retry").Hold(),
            credentials=types.SimpleNamespace(state=lambda has_token: {"mode": "token"}),
            slack=types.SimpleNamespace(status=lambda now: {}),
            revision=types.SimpleNamespace(state=lambda *a: {}),
            BASE_DIR=BASE, REVISION={}, os=os, daemons=daemons)
        out = ns["handle_status"]({})
        check("/status names the dead thread",
              [d.get("name") for d in (out.get("daemons") or {}).get("dead", [])] == ["sweeper"],
              str(out.get("daemons")))
    finally:
        daemons.reset()

    # bin/silkworm: the check fails, and names it; an old bot is not all-clear.
    cli = ast.parse((BASE / "bin" / "silkworm").read_text())
    fn = next(n for n in cli.body
              if isinstance(n, ast.FunctionDef) and n.name == "check_daemons")
    printed = []

    def fake_check(label, ok, hint=""):
        printed.append((label, ok, hint))
        return ok
    mod = {"check": fake_check}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<cli>", "exec"), mod)
    mod["check_daemons"]({"count": 14, "alive": 13, "dead": [
        {"name": "sweeper", "reason": "OSError: [Errno 28] No space left", "dead_for": 7200}]})
    label, ok, hint = printed[-1]
    check("silkworm status fails when one has stopped", ok is False)
    check("and names it, with the reason", "sweeper" in hint and "No space left" in hint, hint)
    check("and shows how many are alive", "13/14" in label, label)
    mod["check_daemons"]({"count": 14, "alive": 14, "dead": []})
    check("all alive passes", printed[-1][1] is True)
    mod["check_daemons"]({"count": 0, "alive": 0, "dead": []})
    check("none registered yet (still booting) is not all-clear", printed[-1][1] is False,
          "/status answers from import time, before any loop has started")
    mod["check_daemons"](None)
    check("a bot too old to report them fails rather than passes", printed[-1][1] is False)
    status_src = ast.unparse(next(n for n in cli.body
                                  if isinstance(n, ast.FunctionDef) and n.name == "do_status"))
    check("do_status runs the check", "check_daemons(bot.get('daemons'))" in status_src)



# --- the task board as a channel message ---------------------------------------
# The Home tab sent Reply in Slack's Threads view to the app's Home instead of
# the thread (switching the tab off fixed it). The board moved to one message
# in a private channel. A channel message is read by everyone in the channel,
# so the rule that mattered for the Home tab -- nobody off the allowlist sees
# the board -- has to be enforced on the channel's membership instead.

def test_board_channel():
    import home
    import tasks as T
    print("\nthe task board as a channel message")

    class Err(Exception):
        def __init__(self, error):
            self.response = {"error": error}

    class Slack:
        def __init__(self, members=("U_ME", "B_BOT"), channels=None):
            self.members = list(members)
            self.channels = channels if channels is not None else [
                {"id": "G_BOARD", "name": "silkworm-board", "is_member": True,
                 "is_private": True},
                {"id": "C_OTHER", "name": "general", "is_member": True}]
            self.calls, self.fail_update = [], None
            self.n = 0
        def conversations_list(self, **kw):
            return {"channels": self.channels, "response_metadata": {}}
        def conversations_info(self, channel, **kw):
            c = next((c for c in self.channels if c["id"] == channel), None)
            if c is None:
                raise Err("channel_not_found")
            return {"channel": c}
        def conversations_members(self, channel, **kw):
            return {"members": self.members, "response_metadata": {}}
        def chat_postMessage(self, channel, text, blocks):
            self.n += 1
            self.calls.append(("post", channel, blocks)); return {"ts": f"1.{self.n}"}
        def chat_update(self, channel, ts, text, blocks):
            if self.fail_update:
                raise Err(self.fail_update)
            self.calls.append(("update", channel, ts, blocks))
        def chat_delete(self, channel, ts):
            self.calls.append(("delete", channel, ts))
        def chat_postEphemeral(self, channel, user, text):
            self.calls.append(("ephemeral", channel, user, text))
        def views_open(self, trigger_id, view):
            self.calls.append(("modal", view))

    d = Path(tempfile.mkdtemp())
    store = T.TaskStore(d / "t.json")
    ns = bot_functions("handle_tasks", "approve_task", task_store=store, tasks=T,
                       stop_task=lambda tid: False, start_landing=lambda tid: None)
    def board(**kw):
        return home.Board(state_path=d / "board.json", bot_user="B_BOT", store=store,
                          call=ns["handle_tasks"], allowed_users={"U_ME"}, **kw)

    b, s = board(), Slack()
    prop = store.create("a proposal worth accepting", state=T.PROPOSED)["id"]
    check("found by name among the bot's channels", b.channel(s) == "G_BOARD")
    check("first pass posts the board", b.sync(s) == "posted"
          and s.calls[-1][:2] == ("post", "G_BOARD"))
    check("and remembers where", json.loads((d / "board.json").read_text())["ts"] == "1.1")
    n = len(s.calls)
    check("nothing changed, nothing sent", b.sync(s) == "unchanged" and len(s.calls) == n)
    store.transition(prop, T.QUEUED)
    check("a task moving redraws it in place", b.sync(s) == "updated"
          and s.calls[-1][:3] == ("update", "G_BOARD", "1.1"))
    b2 = board()                                   # a restart
    store.create("another", state=T.PROPOSED)
    check("after a restart it edits the same message rather than posting twice",
          b2.sync(s) == "updated" and s.calls[-1][2] == "1.1")

    s.fail_update = "message_not_found"            # deleted by hand
    store.create("a third", state=T.PROPOSED)
    check("deleted by hand, it is posted again", b2.sync(s) == "posted")
    s.fail_update = None

    many = [store.create(f"task {i}", state=T.PROPOSED) for i in range(80)]
    # A realistic full board: every attention state (each brings a heading),
    # plus running work, wake-ups and the unmerged line -- the tail the budget
    # has to leave room for. One state alone fits by the item cap regardless.
    for i in range(20):
        x = store.create(f"work {i}", state=T.QUEUED)["id"]
        store.transition(x, T.RUNNING)
        if i < 15:
            store.transition(x, (T.AWAITING_APPROVAL, T.NEEDS_INPUT, T.FAILED)[i % 3])
    b2._watching = lambda: [{"id": f"w{i}", "goal": f"watch {i}", "in_s": 60} for i in range(20)]
    b2._unmerged = lambda: "67 finished tasks on unmerged branches"
    b2._cache = ("", 0.0)
    b2.sync(s, force=True)
    blocks = s.calls[-1][-1]
    check("a full board fits Slack's 50-block message limit", len(blocks) <= 50, str(len(blocks)))
    check("and counts what it left out", "more waiting" in json.dumps(blocks, ensure_ascii=False))

    # Membership is the privacy boundary now.
    s2 = Slack(members=("U_ME", "B_BOT", "U_STEPH"))
    b3 = board()
    check("not posted where someone off the allowlist can read it",
          b3.sync(s2) == "outsiders" and not [c for c in s2.calls if c[0] == "post"])
    s3 = Slack()
    b4 = home.Board(state_path=d / "b4.json", bot_user="B_BOT", store=store,
                    call=ns["handle_tasks"], allowed_users={"U_ME"})
    b4.sync(s3)
    s3.members.append("U_STEPH")                   # someone joins later
    check("taken down when someone off the allowlist joins",
          b4.sync(s3) == "outsiders" and s3.calls[-1][0] == "delete")
    # A take-down that fails must be tried again, not forgotten: forgetting
    # the message leaves it readable by the newcomer for good.
    s6 = Slack()
    b7 = home.Board(state_path=d / "b7.json", bot_user="B_BOT", store=store,
                    call=ns["handle_tasks"], allowed_users={"U_ME"})
    b7.sync(s6); s6.members.append("U_STEPH")
    real_delete = s6.chat_delete
    def flaky(channel, ts):
        s6.chat_delete = real_delete
        raise Err("ratelimited")
    s6.chat_delete = flaky
    b7.sync(s6)
    b7.sync(s6)
    check("a take-down that failed once is tried again",
          any(c[0] == "delete" for c in s6.calls))
    # Anyone in the workspace can read a public channel without joining it,
    # so a clean member list says nothing there.
    pub = Slack(channels=[{"id": "C_PUB", "name": "silkworm-board", "is_member": True,
                           "is_private": False}])
    check("never posted in a public channel, whoever its members are",
          board().sync(pub) != "posted" and not [c for c in pub.calls if c[0] == "post"])
    # A real-looking id: C_PUB would not parse as one, and the check would
    # pass by finding no channel at all.
    pub_id = Slack(channels=[{"id": "C0PUB123", "name": "anything", "is_member": True,
                              "is_private": False}])
    by_id = board(channel="C0PUB123")
    check("an id is taken as an id", by_id.channel(pub_id) == "C0PUB123")
    check("nor in one configured by id",
          by_id.sync(pub_id) != "posted" and not [c for c in pub_id.calls if c[0] == "post"])
    s8 = Slack()
    b8 = home.Board(state_path=d / "b8.json", bot_user="B_BOT", store=store,
                    call=ns["handle_tasks"], allowed_users={"U_ME"})
    b8.sync(s8); s8.members.append("U_STEPH")
    b8.on_member_joined({"channel": "C_OTHER", "user": "U_STEPH"}, s8)
    check("a join elsewhere is ignored", s8.calls[-1][0] != "delete")
    b8.on_member_joined({"channel": "G_BOARD", "user": "U_STEPH"}, s8)
    check("a join to the board channel takes it down at once, not next pass",
          s8.calls[-1][0] == "delete")
    check("no channel yet, nothing posted and no crash",
          board().sync(Slack(channels=[])) == "no-channel")

    # Clicks: result to the clicker, privately; the board redrawn for everyone.
    s4, b5 = Slack(), board()
    b5.sync(s4)
    tid = many[0]["id"]
    body = lambda user, v: {"user": {"id": user}, "trigger_id": "t",
                            "actions": [{"action_id": "home_accept", "value": v}]}
    b5.on_direct(lambda *a, **k: None, body("U_STEPH", f"{tid}|proposed"), s4)
    check("a click from off the allowlist does nothing but say so, privately",
          store.get(tid)["state"] == T.PROPOSED
          and s4.calls[-1][0] == "ephemeral" and s4.calls[-1][2] == "U_STEPH")
    b5.on_direct(lambda *a, **k: None, body("U_ME", f"{tid}|proposed"), s4)
    kinds = [c[0] for c in s4.calls[-2:]]
    check("Accept works from the board message", store.get(tid)["state"] == T.QUEUED)
    check("the result goes to the clicker only, then the board redraws",
          kinds == ["ephemeral", "update"] and "Accepted" in s4.calls[-2][3])
    b5.on_direct(lambda *a, **k: None, body("U_ME", f"{tid}|proposed"), s4)
    check("a stale button is still refused", "moved on" in s4.calls[-2][3])

    # The channel going away is noticed rather than failing for ever.
    s5, b6 = Slack(), board()
    b6.sync(s5); s5.fail_update = "channel_not_found"
    store.create("one more", state=T.PROPOSED)
    check("losing the channel is noticed, not retried for ever",
          b6.sync(s5) == "channel-gone" and b6._channel_id == "")

    src = (BASE / "bot.py").read_text()
    check("the bot keeps it current in a supervised loop",
          'daemons.start(_board_loop, "board")' in src)
    m = json.loads((BASE / "manifest.json").read_text())
    check("the Home tab stays off in the manifest",
          not m["features"]["app_home"].get("home_tab_enabled")
          and "app_home_opened" not in m["settings"]["event_subscriptions"]["bot_events"])


# --- one global outage must not drain the queue ----------------------------------
# The runner claimed, ran, and claimed again with no memory of how the last one
# ended. A quota message kills every turn in three seconds, so on 2026-09-26 it
# walked 15 tasks in 39 seconds -- each given a worktree, each parked unrun --
# and on 2026-09-22 did it five times, some 260 claims. Every claim spent one of
# the task's MAX_AUTO_RETRIES, so five outages failed work that never started.

def _outage_harness():
    """The real worker, executor and fail_or_retry, over a scratch store."""
    import threading
    import retry as R, tasks as T, roles
    from tasks import TaskStore

    class Idle(BaseException):
        """Raised by the worker's first sleep: the pass is over."""

    root = Path(tempfile.mkdtemp())
    st = TaskStore(root / "t.json")
    hold = R.Hold()
    calls = []

    def run_turn(goal, **kw):
        calls.append(goal)
        if "does work" in goal:
            kw["on_activity"]("Bash", {"command": "make"})
        raise ClaudeError("You've hit your session limit · resets 6pm")

    fail_or_retry = _bot_func("fail_or_retry", task_store=st, retry=R, tasks=T,
                              RUNNER_HOLD=hold, log=logging.getLogger("test"),
                              task_state=lambda tid, s, d="": st.transition(tid, s, d))
    execute_task = _bot_func(
        "execute_task", tasks=T, task_store=st, store=tmp_store(), roles=roles,
        review_branch=lambda task: None, Path=Path, run_turn=run_turn,
        shutil=shutil, OUTBOX_ROOT=root / "outbox", SILKWORM_BIN="/x/silkworm",
        permission_args=lambda: [], log=logging.getLogger("test"),
        task_thread=lambda t: ("C1", "1.0"),
        task_state=lambda tid, s, d="": st.transition(tid, s, d),
        _thread_lock=lambda key: threading.Lock(),
        repo_guard=lambda *a, **k: contextlib.nullcontext(),
        render_block=lambda _: "", RUNNING={}, RUNNING_TASKS={},
        ClaudeStopped=ClaudeStopped, ClaudeError=ClaudeError,
        RUNNER_HOLD=hold, fail_or_retry=fail_or_retry)

    def idle(_s):
        raise Idle()
    worker = _bot_func("_task_worker", task_store=st, execute_task=execute_task,
                       RUNNER_HOLD=hold, TASK_POLL_S=5, log=logging.getLogger("test"),
                       time=types.SimpleNamespace(sleep=idle, time=time.time))

    def one_pass():
        """Run the worker until it first waits."""
        try:
            worker(0)
        except Idle:
            pass

    def next_window():
        """The outage window passes: the hold lapses, the scheduler requeues."""
        hold.open()
        for tid in st.due_retries(time.time() + 2 * 86400):
            st.transition(tid, T.QUEUED, "retry time reached")

    return types.SimpleNamespace(st=st, hold=hold, calls=calls, T=T, R=R,
                                 one_pass=one_pass, next_window=next_window,
                                 cwd=str(root))


def test_a_global_outage_does_not_drain_the_queue():
    print("\none global outage must not drain the queue")
    h = _outage_harness()
    st, T, R = h.st, h.T, h.R
    ids = [st.create(f"task {i}", driver="queue", source="ui",
                     scope={"cwd": h.cwd})["id"] for i in range(10)]
    for i, tid in enumerate(ids):              # claim() takes the oldest first
        st.update(tid, created=1000.0 + i)

    h.one_pass()
    states = [st.get(t)["state"] for t in ids]
    check("a quota failure stops the runner after one task",
          len(h.calls) == 1, f"{len(h.calls)} turns run in one pass")
    check("the one that hit it is parked for the reset",
          states[0] == T.BLOCKED and st.get(ids[0])["retry_at"])
    check("everything behind it is still queued, unclaimed",
          states[1:] == [T.QUEUED] * 9, str(states))
    check("and nothing behind it was charged an attempt",
          all(not st.get(t)["attempts"] for t in ids[1:]))
    check("the runner is held, not merely slowed",
          h.hold.remaining() > 60, f"{h.hold.remaining():.0f}s")
    check("a run that never reached work costs no attempt",
          st.get(ids[0])["attempts"] == 0 and st.get(ids[0])["false_starts"] == 1,
          str({k: st.get(ids[0])[k] for k in ("attempts", "false_starts")}))

    # Many outage windows in a row, more than the retry budget.
    windows = R.MAX_AUTO_RETRIES * 2
    for _ in range(windows):
        h.next_window()
        h.one_pass()
    recs = [st.get(t) for t in ids]
    check(f"{windows} outages later nothing has failed",
          not [r for r in recs if r["state"] == T.FAILED],
          str([r["state"] for r in recs]))
    check("and no task has spent any of its retry budget",
          all(not r["attempts"] for r in recs))
    check("each window cost one probe, not a pass over the queue",
          len(h.calls) == windows + 1, f"{len(h.calls)} turns")
    check("the refunds are counted, not forgotten",
          sum(r["false_starts"] for r in recs) == windows + 1)

    # Still bounded: a task that can never start does eventually ask.
    st.update(ids[0], false_starts=R.MAX_FALSE_STARTS)
    before = len(h.calls)
    h.next_window(); h.one_pass()
    check("a task that never gets started still fails in the end",
          st.get(ids[0])["state"] == T.FAILED, st.get(ids[0])["state"])
    # Counted, because the next task's failure would close the hold anyway:
    # the question is whether the runner went on to claim it at all.
    check("and still holds the runner, though it is not retried",
          len(h.calls) == before + 1 and h.hold.remaining() > 60,
          f"{len(h.calls) - before} turns in the pass")

    # A success anywhere is evidence the outage is over.
    bot = (BASE / "bot.py").read_text()
    check("every successful turn lifts the hold",
          bot.count("RUNNER_HOLD.open()") >= 2)


def test_work_that_started_still_spends_its_budget():
    print("\na run that did real work still counts against its retries")
    h = _outage_harness()
    st, T, R = h.st, h.T, h.R
    tid = st.create("this one does work", driver="queue", source="ui",
                    scope={"cwd": h.cwd})["id"]
    h.one_pass()
    check("a run that reached a tool call is charged",
          st.get(tid)["attempts"] == 1 and not st.get(tid)["false_starts"])
    for _ in range(R.MAX_AUTO_RETRIES):
        h.next_window(); h.one_pass()
    check("and after MAX_AUTO_RETRIES of them it asks for a person",
          st.get(tid)["state"] == T.FAILED,
          "otherwise the refund has quietly made retries unlimited")
    check("the budget ran out exactly where it always did",
          len(h.calls) == R.MAX_AUTO_RETRIES, f"{len(h.calls)} runs")


def test_false_starts_still_back_off():
    import retry as R
    print("\nrefunded runs still lengthen the backoff")
    now = 1_000_000.0
    first = R.retry_at("529 overloaded", 0, now=now)[1] - now
    later = R.retry_at("529 overloaded", 0, now=now, false_starts=6)[1] - now
    check("an overloaded API is not retried every minute forever",
          later > first and later == R.BACKOFF_CAP_S, f"{first}s then {later}s")
    check("backoff is unchanged for runs that did work",
          R.retry_at("529 overloaded", 3, now=now)[1] - now == 4 * R.BACKOFF_BASE_S)
    real = time.time()
    hold = R.Hold()
    hold.close(real + 100, "a"); hold.close(real + 50, "b")
    check("the hold only ever extends", 99 < hold.remaining(real) <= 100)
    check("and lapses on its own, with no success needed to lift it",
          hold.remaining(real + 101) == 0)
    hold.close(real + 86400, "resets 6pm, read at 6:01pm")
    check("a misread reset time cannot idle the queue for a day",
          hold.remaining() <= R.HOLD_CAP_S and hold.remaining() > R.HOLD_CAP_S - 5)

    # Inline turns report whether they started too; a Slack turn killed by the
    # same quota must not be charged for it either.
    tree = ast.parse((BASE / "bot.py").read_text())
    hp = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "handle_prompt")
    calls = [c for c in ast.walk(hp) if isinstance(c, ast.Call)
             and ast.unparse(c.func) == "fail_or_retry"]
    check("every failure path in handle_prompt says whether the turn started",
          calls and all(any(k.arg == "started" and ast.unparse(k.value) == "worked[0]"
                             for k in c.keywords) for c in calls),
          f"{len(calls)} call(s)")
    marks = [n for n in ast.walk(hp) if isinstance(n, ast.FunctionDef)
             and n.name == "on_activity"
             and "worked[0] = True" in ast.unparse(n)]
    check("and a tool call is what marks it started", len(marks) == 1)

    # The refund must not make an accepted proposal read as a dismissed one.
    import scoping, tasks as T
    rec = {"title": "wanted idea", "goal": "g", "state": T.CANCELLED,
           "source": "ideation", "role": "implementor", "attempts": 0,
           "false_starts": 1, "created": real, "updated": real}
    _open, dismissed = scoping.already_filed([rec], real)
    check("a proposal an outage cancelled is not remembered as dismissed",
          "wanted idea" not in dismissed, str(dismissed))
    _open, dismissed = scoping.already_filed([{**rec, "false_starts": 0}], real)
    check("while one that truly never ran still is", "wanted idea" in dismissed,
          "otherwise the check above proves nothing")


if __name__ == "__main__":
    tests = discover()
    if not tests:
        # Otherwise this prints "0 passed, 0 failed" and exits 0: a green run
        # that verified nothing, which is exactly what landing must not trust.
        print("  ✘ no tests were discovered in this file")
        sys.exit(1)
    for t in tests:
        try:
            t()
        except Exception as exc:
            FAILED.append(f"{t.__name__} raised {exc!r}")
            print(f"  ✘ {t.__name__} raised {exc!r}")
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for f in FAILED:
        print(f"  FAILED: {f}")
    sys.exit(1 if FAILED else 0)


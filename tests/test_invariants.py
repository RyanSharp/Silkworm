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
import artifacts                                 # noqa: E402

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
    wanted += _shared_helpers(tree, wanted, set(names) | set(globals_))
    ns = {"log": logging.getLogger("test"), "time": time, **globals_}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "bot.py", "exec"), ns)
    return ns


#: Helpers other lifted functions call as part of their own behaviour, so a
#: lifted caller gets the real one rather than a stub or a NameError. Sending
#: work back is one mechanism shared by the dashboard and the review gate.
SHARED_HELPERS = ("send_back", "review_addendum", "task_base", "unmerged_survey",
                  "held_reason", "edit_task", "_task_title", "take_step", "release_steps")


def _shared_helpers(tree, nodes, supplied) -> list:
    used = {n.id for f in nodes for n in ast.walk(f) if isinstance(n, ast.Name)}
    return [n for n in tree.body if isinstance(n, ast.FunctionDef)
            and n.name in SHARED_HELPERS and n.name in used
            and n.name not in supplied]


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
    ns = bot_functions("record_branch", "review_branch", "resolve_review",
                       roles=roles, tasks=T, task_store=st, worktrees=W,
                       branches=__import__("branches"),
                       project_store=types.SimpleNamespace(scope_for=lambda slug: {}),
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
    nodes += _shared_helpers(tree, nodes, set(names) | set(ns))
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
    check("a task's base branch reaches the worktree -- its project's, as it is now",
          "worktrees.create(cwd, tid, base=task_base(task))" in bot)


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
    check("while a web chat turn still is not",
          not S.is_work({"role": "assistant", "source": "ui"}))

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
        scoping=S, tasks=T, task_store=ts, project_store=ps, datetime=datetime, projects=P,
        time=time,
        log=logging.getLogger("test"), CLAUDE_CWD=root, SILKWORM_BIN="silkworm",
        branches=types.SimpleNamespace(survey=lambda _t, **_: []))
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
            "begin_turn", "unmerged_survey")
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
        dedup=__import__("dedup"), branches=__import__("branches"),
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


#: Words that share nothing, for fixtures that need many different goals.
_DISTINCT_WORDS = ("apple", "bridge", "candle", "dolphin", "engine", "forest",
                   "glacier", "harbor", "island", "jungle", "kettle", "lantern",
                   "meadow", "needle", "orchard", "pepper", "quarry", "river",
                   "saddle", "tunnel", "umbrella", "valley", "walnut", "yarrow",
                   "zephyr")


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
    # Pure arithmetic over records, with nothing to stub: a mock here would
    # only turn a cost into an object the store cannot serialise.
    for real in ("costs", "dedup"):
        if real in free:
            mod.__dict__[real] = __import__(real)
    # The shared helpers compiled in alongside it read globals of their own
    # (task_base reads `branches` and `project_store`), which the caller has
    # as little reason to supply as the function's own -- stub those too.
    helpers = _shared_helpers(tree, [node], set(namespace))
    for h in helpers:
        own = {a.arg for a in h.args.args} | {
            n.id for n in ast.walk(h)
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))}
        for free_name in ({n.id for n in ast.walk(h) if isinstance(n, ast.Name)
                           and isinstance(n.ctx, ast.Load)}
                          - own - set(dir(builtins)) - set(mod.__dict__)
                          - {x.name for x in helpers}):
            mod.__dict__[free_name] = MagicMock(name=free_name)
    mod.__dict__.update(namespace)
    exec(compile(ast.Module(body=[node] + helpers,
                            type_ignores=[]), f"<{name}>", "exec"),
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



# --- a first run's survivor can be found after a restart ------------------------
# A restarted task waits for its interrupted child rather than resuming beside
# it, and finds that child by the session id on its command line. A resumed
# child carries `--resume <sid>`; a first run used to carry nothing, so if it
# outlived the restart, the task resumed a second claude on the same session.

def test_fresh_session_is_findable_while_it_runs():
    import threading
    import claude_runner as CR
    print("\na fresh run names its session on its command line")

    # procs only accepts a process whose executable is called `claude`;
    # `exec -a` keeps that name while perl (which, unlike framework python,
    # does not re-exec itself) does the work.
    d = Path(tempfile.mkdtemp())
    (d / "fake.pl").write_text(r"""
undef $/; <STDIN>; $| = 1;
my ($sid, $res) = ("", "");
for my $i (0 .. $#ARGV - 1) {
    $sid = $ARGV[$i + 1] if $ARGV[$i] eq "--session-id";
    $res = $ARGV[$i + 1] if $ARGV[$i] eq "--resume";
}
my $id = $res || $sid;
print "{\"type\":\"system\",\"subtype\":\"init\",\"session_id\":\"$id\"}\n";
sleep 1 while -e $ENV{HOLD};
my $how = ($sid && $res) ? "both" : $res ? "resumed" : $sid ? "named" : "anonymous";
print "{\"type\":\"result\",\"result\":\"$how\",\"session_id\":\"$id\"}\n";
""")
    fake = d / "claude"
    fake.write_text('#!/bin/bash\nexec -a "$0" /usr/bin/perl "$(dirname "$0")/fake.pl" "$@"\n')
    fake.chmod(0o755)

    def run(session_id=None):
        hold = d / "hold"
        hold.write_text("")
        seen, out = {}, {}

        def init(sid):
            seen["sid"] = sid
            seen["alive"] = procs.session_alive(sid)   # the child is running now
            hold.unlink()

        def go():
            try:
                out["r"] = CR.run_turn("go", binary=str(fake), cwd=str(d),
                                       permission_args=[], session_id=session_id,
                                       env={**os.environ, "HOLD": str(hold)},
                                       on_init=init, idle_timeout=30)
            except Exception as e:  # surfaced through the checks below
                out["e"] = e

        t = threading.Thread(target=go)
        t.start()
        t.join(30)
        if hold.exists():
            hold.unlink()
        return seen, out

    seen, out = run()
    r = out.get("r")
    check("a fresh run is given a session id on its command line",
          r is not None and r.text == "named", repr(out))
    check("and procs.session_alive finds that child while it runs",
          seen.get("alive") is True, repr(seen))
    check("the id the stream reports is the one we named",
          bool(seen.get("sid")) and r is not None and r.session_id == seen["sid"])
    check("and it is not found once it has exited",
          not procs.session_alive(seen.get("sid") or "no-such-session"))

    seen2, out2 = run(session_id="11111111-2222-3333-4444-555555555555")
    r2 = out2.get("r")
    check("a resumed run passes --resume alone, never a second id",
          r2 is not None and r2.text == "resumed", repr(out2))
    check("and is found by the session it resumes",
          seen2.get("alive") is True, repr(seen2))

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
    got = B.missed(entries, replies=lambda c, t, o: msgs[(c, t)], bot_user_id="BOT",
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
          B.missed(old, replies=lambda c, t, o: stale[(c, t)], bot_user_id="B",
                   handled_subtypes=set(), now=now) == [],
          "it was re-asked or stopped mattering")

    many = {("C", "1"): [{"ts": str(now - 60 + i), "user": "U1", "text": f"m{i}"}
                         for i in range(9)]}
    capped = B.missed(old, replies=lambda c, t, o: many[(c, t)], bot_user_id="B",
                      handled_subtypes=set(), now=now, max_per_thread=3)
    check("a chatty gap is capped", len(capped) == 3)
    check("and the newest are kept", [e["text"] for e in capped] == ["m6", "m7", "m8"])
    src = (BASE / "backfill.py").read_text()
    check("a dropped message is logged, not silently forgotten",
          "log.warning" in src and "skipping" in src)

    check("a thread Slack cannot return is skipped, not fatal",
          B.missed(old, replies=lambda c, t, o: (_ for _ in ()).throw(RuntimeError("nope")),
                   bot_user_id="B", handled_subtypes=set(), now=now) == [])

    # A long thread. Slack returns replies oldest-first, so one unpaginated
    # call read only the first page and never reached the watermark at its end.
    class FakeSlack:
        def __init__(self, msgs, fail=0):
            self.msgs, self.fail, self.calls = msgs, fail, []

        def conversations_replies(self, *, channel, ts, limit, cursor=None,
                                  oldest=None, inclusive=None):
            self.calls.append({"cursor": cursor, "oldest": oldest})
            if self.fail:
                self.fail -= 1
                raise IOError("IncompleteRead(74664 bytes read, 340397 more expected)")
            pool = [m for m in self.msgs[1:]
                    if oldest is None or float(m["ts"]) > float(oldest)]
            start = int(cursor or 0)
            # As Slack does: the parent heads every page, past the watermark or not.
            page = [self.msgs[0]] + pool[start:start + limit - 1]
            more = start + limit - 1 < len(pool)
            return {"messages": page,
                    "response_metadata": {"next_cursor": str(start + limit - 1) if more else ""}}

    long_mark = now - 3600
    thread = [{"ts": f"{long_mark - 200 + i:.6f}", "user": "U1", "text": f"old{i}"}
              for i in range(150)]
    thread += [{"ts": f"{long_mark + 10 + i:.6f}", "user": "U1", "text": f"new{i}"}
               for i in range(2)]
    long_entries = {"D:1": {"last_msg_ts": f"{long_mark:.6f}"}}
    nap = lambda s: None                                 # noqa: E731

    def read(client, **kw):
        return lambda c, t, o: B.read_thread(client, c, t, oldest=o, sleep=nap, **kw)

    slack = FakeSlack(thread)
    got = B.missed(long_entries, replies=read(slack), bot_user_id="B",
                   handled_subtypes=set(), now=now)
    check("a long thread's new messages are replayed",
          [e["text"] for e in got] == ["new0", "new1"],
          "they sit past message 100, beyond a first page")
    check("the read starts at the watermark",
          slack.calls and slack.calls[0]["oldest"] == f"{long_mark:.6f}",
          "not the start of the thread, every boot")

    # Without the watermark the same thread must still be read to its end.
    slack = FakeSlack(thread)
    full = B.read_thread(slack, "D", "1", page=100, sleep=nap)
    check("pages are followed to the end", len(full) == len(thread)
          and len(slack.calls) == 2)
    check("the parent Slack repeats on each page is kept once, in place",
          [m["ts"] for m in full] == [m["ts"] for m in thread])
    tail = B.read_thread(FakeSlack(thread), "D", "1", page=100, keep_last=60, sleep=nap)
    check("so a tail is in time order", [m["ts"] for m in tail]
          == [m["ts"] for m in thread[-60:]],
          "not the thread's opening wedged between recent messages")

    class Refusal(Exception):
        response = {"ok": False, "error": "missing_scope"}

    class Refuses(FakeSlack):
        def conversations_replies(self, **kw):
            self.calls.append(kw)
            raise Refusal()
    slack = Refuses(thread)
    try:
        B.read_thread(slack, "D", "1", sleep=nap)
    except Refusal:
        pass
    check("a permanent refusal is not retried", len(slack.calls) == 1,
          "missing_scope says the same thing a second time")
    check("keep_last holds the newest",
          [m["text"] for m in B.read_thread(FakeSlack(thread), "D", "1",
                                            keep_last=2, sleep=nap)] == ["new0", "new1"],
          "a fresh session wants the recent end, not the opening")

    # A fresh session needs only the tail. Reading a long thread from its
    # opening to keep 50 cost every page, and one failed page cost it all.
    class FailsFrom(FakeSlack):
        def __init__(self, msgs, from_call):
            super().__init__(msgs)
            self.from_call = from_call

        def conversations_replies(self, **kw):
            if len(self.calls) + 1 >= self.from_call:
                self.calls.append(kw)
                raise IOError("IncompleteRead")
            return super().conversations_replies(**kw)

    try:
        B.read_thread(FailsFrom(thread, 2), "D", "1", page=100, sleep=nap)
        raised = False
    except IOError:
        raised = True
    check("without partial, a failed later page still raises", raised)
    def tolerant(f, *a, **kw):              # a crash is a failed check, not a crashed test
        try:
            return f(*a, **kw)
        except Exception as e:
            return [{"ts": f"raised {e!r}", "text": f"raised {e!r}"}]
    part = tolerant(B.read_thread, FailsFrom(thread, 2), "D", "1", page=100,
                    partial=True, sleep=nap)
    check("with partial, the pages already read are kept",
          [m["ts"] for m in part] == [m["ts"] for m in thread[:100]], part[0]["ts"])

    day = 24 * 3600
    aged = [{"ts": f"{now - 10 * day:.6f}", "user": "U1", "text": "parent"}]
    aged += [{"ts": f"{now - 5 * day + i:.6f}", "user": "U1", "text": f"a{i}"}
             for i in range(300)]
    aged += [{"ts": f"{now - 3600 + i:.6f}", "user": "U1", "text": f"r{i}"}
             for i in range(60)]
    slack = FakeSlack(aged)
    tail = B.read_tail(slack, "D", aged[0]["ts"], keep_last=50, before=f"{now:.6f}",
                       page=100, sleep=nap)
    check("a long old thread's tail is read from the last day only",
          [m["text"] for m in tail] == [f"r{i}" for i in range(10, 60)]
          and len(slack.calls) == 1 and slack.calls[0]["oldest"] is not None,
          f"{len(slack.calls)} calls")
    slack = FakeSlack(aged)
    tail = B.read_tail(slack, "D", aged[0]["ts"], keep_last=80, before=f"{now:.6f}",
                       page=100, sleep=nap)
    check("a window too thin falls back to the whole thread",
          [m["ts"] for m in tail] == [m["ts"] for m in aged[-80:]])
    tail = tolerant(B.read_tail, FailsFrom(aged, 3), "D", aged[0]["ts"], keep_last=80,
                    before=f"{now:.6f}", page=100, sleep=nap)
    check("a whole-thread read that fails keeps the recent window",
          [m["text"] for m in tail] == ["parent"] + [f"r{i}" for i in range(60)],
          "some recent context beats none")
    busy = aged[:1] + [{"ts": f"{now - 7200 + i:.6f}", "user": "U1", "text": f"b{i}"}
                       for i in range(150)]
    slack = FailsFrom(busy, 2)
    tail = tolerant(B.read_tail, slack, "D", busy[0]["ts"], keep_last=50,
                    before=f"{now:.6f}", page=100, sleep=nap)
    check("a busy day's window cut short is used as far as it got",
          [m["text"] for m in tail] == [f"b{i}" for i in range(49, 99)]
          and slack.calls[0]["oldest"] is not None, tail[0].get("text"))
    slack = FailsFrom(thread, 2)
    tail = tolerant(B.read_tail, slack, "D", thread[0]["ts"], keep_last=50,
                    before=thread[-1]["ts"], page=100, sleep=nap)
    check("a young thread is read whole, and a failed page keeps what was read",
          [m["ts"] for m in tail] == [m["ts"] for m in thread[50:100]]
          and slack.calls[0]["oldest"] is None)

    slack = FakeSlack(thread, fail=1)
    got = B.missed(long_entries, replies=read(slack), bot_user_id="B",
                   handled_subtypes=set(), now=now)
    check("a cut-off read is retried, not the thread skipped",
          [e["text"] for e in got] == ["new0", "new1"],
          "2026-10-02: IncompleteRead skipped the whole thread")
    slack = FakeSlack(thread, fail=99)
    check("a read that never succeeds still gives up",
          B.missed(long_entries, replies=read(slack), bot_user_id="B",
                   handled_subtypes=set(), now=now) == []
          and len(slack.calls) == B.ATTEMPTS)

    bot = (BASE / "bot.py").read_text()
    check("backfill reads through read_thread with the watermark",
          "backfill.read_thread(app.client, channel, thread_ts, oldest=oldest)" in
          bot[bot.index("def run_backfill"):bot.index("def _backfiller")])
    ctx = bot[bot.index("def thread_context"):bot.index("# --- Per-channel working dirs")]
    check("a fresh session's context is the thread's recent end",
          "read_tail(" in ctx and "keep_last=" in ctx and "before=" in ctx
          and "conversations_replies" not in ctx)
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
          "project=file_by_directory(key, cwd)," in bot
          and 'return entry.get("project") or ""' in bot)
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
    r = M.land(wt, repo, name, "main",
               lambda cwd: {"ran": True, "ok": str(cwd) != str(repo),
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
               lambda cwd: run if str(cwd) == str(repo) else {"ran": True, "ok": True})
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
    rec_ns = {"task_store": store, "log": LOG, "time": time}
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
              "start_landing": lambda tid, approved=False: bool(started.append((tid, approved)))}
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
    _bot_fns({"resolve_review", "rework_flagged_review", "rework_conflict",
              "send_back", "review_addendum", "_unsupervised", "MAX_REVIEW_REWORKS",
              "MAX_CONFLICT_REWORKS", "CONFLICT_STAGES"}, rev_ns)
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
    _bot_fns({"record_landing", "clear_interrupted_landings", "LANDING_UNDERWAY",
              "_interrupted_landing_detail"}, sweep_ns)
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
          "dedup": __import__("dedup"), "branches": __import__("branches"),
          "project_store": types.SimpleNamespace(scope_for=lambda p: None)}
    exec(compile(ast.Module(body=[fn] + _shared_helpers(src, [fn], set(ns)),
                            type_ignores=[]), "<x>", "exec"), ns)
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
        # Automatic send-backs have their own test; here nothing is sent back.
        "rework_flagged_review": lambda *a: False,
        "rework_conflict": lambda *a: False,
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
        acts += ["release" for _ in _re.findall(r"releaseStep\('\$\{t\.id\}'\)", body)]
        acts += ["answer" if flag == "true" else "rework"
                 for flag in _re.findall(r"sendBack\('\$\{t\.id\}',(true|false)\)", body)]
        for st in _re.findall(r't\.state === "(\w+)"', cond):
            dash[st] = set(acts)
    check("the dashboard's buttons were read", len(dash) >= 5, str(dash))
    for st, acts in dash.items():
        # A task carrying a step (needs_user) is offered Release and Done as
        # well, on both surfaces; the dashboard's branch shows every button.
        mine = {a for _, a, _, _ in home.BUTTONS.get(st, [])
                + (home.STEP_BUTTONS if st == T.NEEDS_INPUT else [])}
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

    tree = ast.parse((BASE / "bot.py").read_text())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "handle_tasks")
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
            home=lambda n, create=False: made.append(n),
            get=lambda n: None,
            scope_for=lambda n: None, all=lambda: []),
    )
    body = [fn] + _shared_helpers(tree, [fn], set())
    exec(compile(ast.Module(body=body, type_ignores=[]), "<dashboard>", "exec"),
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
        def all(self):
            return {}

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
                       SESSION_MAX_AGE_DAYS=30, TASK_COMPACT_AFTER_DAYS=14,
                       ARTIFACTS_ROOT=Path(tempfile.mkdtemp()), artifacts=artifacts,
                       ARTIFACT_MAX_AGE_DAYS=30, ARTIFACT_PRUNE=True)
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
            RUNNER_HOLD=__import__("retry").Hold(), DRAIN=__import__("drain").Drain(),
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
    # The result goes into the board, not under it. An ephemeral reply is
    # invisible to the channel's history but not to the person who clicked:
    # each one pushed the board up their screen.
    n = len(s4.calls)
    b5.on_direct(lambda *a, **k: None, body("U_ME", f"{tid}|proposed"), s4)
    after = s4.calls[n:]
    check("Accept works from the board message", store.get(tid)["state"] == T.QUEUED)
    check("nothing is posted under the board", not [c for c in after if c[0] in ("ephemeral", "post")],
          str([c[0] for c in after]))
    drawn = json.dumps(after[-1][-1], ensure_ascii=False) if after and after[-1][0] == "update" else ""
    check("the result is in the board itself", "Accepted" in drawn)
    check("at the bottom, where Slack opens", "Accepted" in json.dumps(after[-1][-1][-4:], ensure_ascii=False)
          if after else False)
    n = len(s4.calls)
    b5.on_direct(lambda *a, **k: None, body("U_ME", f"{tid}|proposed"), s4)
    check("a stale button is still refused, in the board", "moved on" in json.dumps(s4.calls[-1][-1]))
    old_s = b5.NOTICE_S
    b5.NOTICE_S = 0
    try:
        check("and once it is stale the next pass takes it down",
              b5.sync(s4) == "updated" and "moved on" not in json.dumps(s4.calls[-1][-1]))
    finally:
        b5.NOTICE_S = old_s

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
    # Readiness is its own test (test_unsupervised_work_needs_a_ready_project);
    # here every project is ready, so the hold under test is the only one.
    worker = _bot_func("_task_worker", task_store=st, execute_task=execute_task,
                       hold_unsupervised=lambda task: False, lane_filter=lambda lane: {},
                       RUNNER_HOLD=hold, DRAIN=__import__("drain").Drain(), TASK_POLL_S=5, log=logging.getLogger("test"),
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
    _bot_fns({"land_if_ready", "landing_enabled"}, ns)
    land = ns["land_if_ready"]
    base = {"project": "p", "verified": True, "scope": {"cwd": str(repo)}}

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
    _bot_fns({"land_if_ready", "landing_enabled"}, ns)
    land = ns["land_if_ready"]
    base = {"project": "p", "verified": True, "scope": {"cwd": str(repo)}}

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
    _bot_fns({"send_back_uncommitted", "MAX_COMMIT_ATTEMPTS"}, ns)
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
    _bot_fns({"land_if_ready", "landing_enabled"}, ns)
    land = ns["land_if_ready"]
    t = {"id": "tsk_work", "project": "p", "scope": {"cwd": str(repo)}}

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
    _bot_fns({"land_or_drop"}, ns2)
    lod = ns2.get("land_or_drop")
    check("there is a Land/Drop action", bool(lod))
    run = store.create("still going", state=T.QUEUED)["id"]
    store.transition(run, T.RUNNING)
    check("not on work still running", lod("land", {"id": run}).get("ok") is False and not begun)
    done = store.create("finished work", state=T.QUEUED, scope={"cwd": str(repo)},
                        branch="silkworm/tsk_work")["id"]
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
    _bot_fns({"hold_unsupervised"}, ns)
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
    _bot_fns({"handle_projects"}, hp)
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



# --- releases: merged is not released -----------------------------------------
# A push to Cadence's main started an Xcode Cloud archive bound for TestFlight,
# so every task that landed would have been a build. Merging and releasing are
# separate events, and a repo holds several things that ship separately.

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
          "repo_guard": lambda cwd: contextlib.nullcontext()}
    ns["board"] = __import__("board")
    _bot_fns({"release_command", "release_plan", "run_release", "_releasing", "_releasing_guard",
              "_board_releases", "release_checkout"}, ns)
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

    _os.environ["SLACK_BOT_TOKEN"] = "xoxb-should-not-reach-a-deploy"
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



# --- the board is a screen, not a scroll ---------------------------------------
# Slack opens a channel at the bottom of its newest message. The board spent
# three blocks on every task, so with eleven waiting you landed at its foot and
# scrolled back up to read it. One line per task, actions in a menu, and the
# summary at the bottom where you land.

def test_board_is_compact():
    import home
    import tasks as T
    print("\nthe board is a screen, not a scroll")
    now = time.time()
    rec = lambda tid, st, **kw: {"id": tid, "state": st, "goal": f"goal of {tid}",
                                 "updated": now - 60, "thread": "D1:1.2", **kw}
    board = [rec("tsk_aw", T.AWAITING_APPROVAL, result={"review": {
                 "summary": "close", "findings": [f"finding {i}" for i in range(7)]}}),
             rec("tsk_in", T.NEEDS_INPUT), rec("tsk_pr", T.PROPOSED),
             rec("tsk_fl", T.FAILED, events=[{"kind": "failed", "detail": "exit 143"}])]
    v = home.render(board, now=now, base_url="https://x.slack.com", compact=True,
                    max_blocks=50, max_attention=12)
    rows = [b for b in v["blocks"] if b.get("block_id", "").startswith("t:")]
    check("one block per task", len(rows) == 4 and not any(
        b.get("block_id", "").startswith("a:") for b in v["blocks"]))
    menu = lambda tid: [o["value"].split("|")[0] for b in rows if b["block_id"] == f"t:{tid}"
                        for o in b["accessory"]["options"]]
    for tid, st in (("tsk_aw", T.AWAITING_APPROVAL), ("tsk_in", T.NEEDS_INPUT),
                    ("tsk_pr", T.PROPOSED), ("tsk_fl", T.FAILED)):
        want = [b[1] for b in home.BUTTONS[st]] + ["thread"]
        check(f"{st}: the menu offers the dashboard's actions and the thread", menu(tid) == want,
              str(menu(tid)))
    check("the summary and Refresh are at the bottom, where Slack opens",
          "need you" in json.dumps(v["blocks"][-2:]) and "home_refresh" in json.dumps(v["blocks"][-2:]))
    many = home.render([rec(f"tsk_{i:03}", T.PROPOSED) for i in range(300)], now=now,
                       compact=True, max_blocks=50, max_attention=12)
    check("a long board still fits a message", len(many["blocks"]) <= 50)
    bstore = T.TaskStore(Path(tempfile.mkdtemp()) / "b.json")
    bstore.create("one waiting", state=T.PROPOSED)
    drawn = home.Board(state_path=Path(tempfile.mkdtemp()) / "board.json", store=bstore,
                       call=None, allowed_users=set()).blocks()
    check("and the board in the channel is the compact one",
          any(b.get("accessory", {}).get("type") == "overflow" for b in drawn)
          and not any(b.get("block_id", "").startswith("a:") for b in drawn))

    # Picking an action opens its confirmation, with the full detail; only
    # submitting it acts.
    store = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    ns = bot_functions("handle_tasks", "approve_task", task_store=store, tasks=T,
                       stop_task=lambda tid: False, start_landing=lambda tid, **kw: None,
                       holding=__import__("holding"), tell_thread=lambda *a: None)
    aw = store.create("work to approve", state=T.QUEUED)["id"]
    store.transition(aw, T.RUNNING); store.transition(aw, T.AWAITING_APPROVAL)
    store.update(aw, result={"review": {"summary": "ok-ish",
                                        "findings": [f"issue number {i}" for i in range(7)]}})
    opened, posted = [], []
    client = types.SimpleNamespace(views_open=lambda trigger_id, view: opened.append(view),
                                   views_publish=lambda **kw: posted.append(kw))
    h = home.Home(store=store, call=ns["handle_tasks"], allowed_users={"U_ME"})
    pick = lambda user, value: {"user": {"id": user}, "trigger_id": "t",
                                "actions": [{"action_id": "home_menu",
                                             "selected_option": {"value": value}}]}
    h.on_menu(lambda *a, **k: None, pick("U_ME", f"approve|{aw}|awaiting_approval"), client)
    check("picking Approve does not approve", store.get(aw)["state"] == T.AWAITING_APPROVAL)
    check("it opens a confirmation", opened and opened[-1]["callback_id"] == home.CONFIRM_CALLBACK)
    shown = json.dumps(opened[-1])
    check("showing every finding, not three", all(f"issue number {i}" in shown for i in range(7)))
    h.on_confirm(lambda *a, **k: None, {"user": {"id": "U_ME"}}, client, opened[-1])
    check("confirming it approves", store.get(aw)["state"] == T.DONE)
    n = len(opened)
    h.on_menu(lambda *a, **k: None, pick("U_ME", f"dismiss|{aw}|awaiting_approval"), client)
    check("an action on a task that has moved on opens nothing", len(opened) == n)
    pr = store.create("a proposal", state=T.PROPOSED)["id"]
    h.on_menu(lambda *a, **k: None, pick("U_STEPH", f"accept|{pr}|proposed"), client)
    check("someone off the allowlist gets no window", len(opened) == n)
    h.on_menu(lambda *a, **k: None, pick("U_ME", f"accept|{pr}|proposed"), client)
    form = opened[-1]
    store.transition(pr, T.CANCELLED)                    # dismissed elsewhere meanwhile
    h.on_confirm(lambda *a, **k: None, {"user": {"id": "U_ME"}}, client, form)
    check("a confirmation left open while the task moved on does nothing",
          store.get(pr)["state"] == T.CANCELLED)
    need = store.create("asks something", state=T.QUEUED)["id"]
    store.transition(need, T.RUNNING); store.transition(need, T.NEEDS_INPUT)
    h.on_menu(lambda *a, **k: None, pick("U_ME", f"answer|{need}|needs_input"), client)
    check("Answer still opens the answer form", opened[-1]["callback_id"] == home.MODAL_CALLBACK)



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

def _load_cli():
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("silkworm_cli_deploy",
                                                  str(BASE / "bin" / "silkworm"))
    spec = importlib.util.spec_from_loader("silkworm_cli_deploy", loader)
    cli = importlib.util.module_from_spec(spec)
    loader.exec_module(cli)
    return cli


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


# --- an interrupted task resumes its session, it does not start over ------------

def _resume_harness(repo, st, sess, transcripts, seen, alive=()):
    import shutil, threading, logging
    import tasks as T, roles, worktrees as W

    def run_turn(prompt, **kw):
        seen.append({"prompt": prompt, "session_id": kw.get("session_id"),
                     "cwd": Path(kw["cwd"]),
                     "files": sorted(p.name for p in Path(kw["cwd"]).iterdir())})
        kw["on_init"]("sess-new")
        return types.SimpleNamespace(text="done", cost_usd=0.0, duration_ms=1,
                                     session_id="sess-new")
    return _bot_func(
        "execute_task", tasks=T, task_store=st, store=sess, roles=roles,
        review_branch=lambda task: None, worktrees=W, Path=Path, time=time,
        run_turn=run_turn, shutil=shutil,
        harvester=types.SimpleNamespace(
            find_transcript=lambda sid: Path(f"/x/{sid}.jsonl") if sid in transcripts else None),
        procs=types.SimpleNamespace(session_alive=lambda sid: sid in alive),
        RESUME_WAIT_S=60, fail_or_retry=lambda *a, **k: False,
        OUTBOX_ROOT=repo.parent / "outbox", SILKWORM_BIN="/x/silkworm",
        permission_args=lambda: [], log=logging.getLogger("test"),
        task_thread=lambda t: ("C1", "1.0"),
        task_state=lambda tid, state, detail="": st.transition(tid, state, detail),
        _thread_lock=lambda key: threading.Lock(),
        repo_guard=lambda *a, **k: contextlib.nullcontext(),
        render_block=lambda _: "", chunk=lambda text: [text],
        to_mrkdwn=lambda text: text, resolve_review=lambda *a, **k: False,
        upload_outbox=lambda *a, **k: [], RUNNING={}, RUNNING_TASKS={},
        ClaudeStopped=ClaudeStopped, ClaudeError=ClaudeError)


def test_an_interrupted_task_resumes_its_session():
    import tasks as T, worktrees as W
    from tasks import TaskStore
    print("\na restart-interrupted task resumes its session in its checkout")

    root = Path(tempfile.mkdtemp())
    W.ROOT = root / "wts"
    repo = root / "repo"; repo.mkdir()
    def git(cwd, *a): return subprocess.run(["git", *a], cwd=str(cwd),
                                            capture_output=True, text=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "base")

    st = TaskStore(root / "t.json")
    sess = tmp_store()

    # First, the turn as it starts: the session is on the record at once.
    tid = st.create("build the thing", driver="queue", source="ui", isolate=True,
                    thread="C1:1.0", scope={"cwd": str(repo)})["id"]
    seen = []
    during = {}
    def first_turn(prompt, **kw):
        kw["on_init"]("sess-1")
        during.update(st.get(tid))
        # The process dies here. Nothing below it -- the turn's end, the
        # executor's finally -- runs; so stop the harness the same way.
        raise SystemExit("killed by a restart")
    st.transition(tid, T.RUNNING, "claimed")
    ex = _resume_harness(repo, st, sess, set(), seen)
    ex.__globals__["run_turn"] = first_turn
    real_release = W.release
    try:
        # A killed process releases nothing, so neither does this run.
        W.release = lambda *a, **k: (False, "kept")
        ex.__globals__["record_branch"] = lambda *a, **k: None
        try:
            ex(st.get(tid))
        except SystemExit:
            pass
    finally:
        W.release = real_release
    check("the session id is on the task the moment the session starts",
          during.get("session_id") == "sess-1"
          and (during.get("checkpoint") or {}).get("session_id") == "sess-1",
          str({k: during.get(k) for k in ("session_id", "checkpoint")}))
    # What the killed turn left behind: half-done work in its checkout. (The
    # executor's finally ran in this test process and cleared the checkpoint,
    # which a killed one would not -- so the record is put back as it was.)
    wt = W.path_for(repo, tid)
    (wt / "half_done.txt").write_text("in progress\n")
    st.update(tid, checkpoint={"session_id": "sess-1", "at": time.time()})

    # The restart.
    moved = st.requeue_interrupted()
    rec = st.get(tid)
    check("the restart requeues it with its session and worktree kept",
          moved == 1 and rec["state"] == T.QUEUED
          and (rec.get("checkpoint") or {}).get("session_id") == "sess-1"
          and rec["scope"].get("worktree") == str(wt),
          str({k: rec.get(k) for k in ("state", "checkpoint", "scope")}))

    claimed = st.claim()
    seen = []
    _resume_harness(repo, st, sess, {"sess-1"}, seen)(claimed)
    check("the next run resumes the interrupted session",
          seen and seen[0]["session_id"] == "sess-1", str(seen[:1]))
    check("with the short resume prompt, not the whole goal again",
          seen and seen[0]["prompt"] == T.RESUME_PROMPT, str(seen[:1])[:200])
    check("in the same checkout, where its half-done work still is",
          seen and seen[0]["cwd"] == wt and "half_done.txt" in seen[0]["files"],
          str(seen[:1]))
    check("and only once: the checkpoint is gone when the turn ends",
          not st.get(tid).get("checkpoint"))

    # No transcript left on disk: nothing to resume, so start fresh as before.
    tid2 = st.create("build another", driver="queue", source="ui", isolate=True,
                     thread="C1:1.0", scope={"cwd": str(repo)})["id"]
    st.update(tid2, session_id="sess-gone",
              checkpoint={"session_id": "sess-gone", "at": time.time()})
    st.transition(tid2, T.RUNNING, "claimed")
    st.requeue_interrupted()
    seen = []
    _resume_harness(repo, st, sess, set(), seen)(st.claim())
    check("an interrupted session with no transcript falls back to a fresh run",
          seen and seen[0]["session_id"] is None and seen[0]["prompt"] == "build another",
          str(seen[:1]))

    # The checkout itself did not survive, but its branch did, with a commit on
    # it: the resumed session gets that branch back, not a fresh one off main.
    tid3 = st.create("build a third", driver="queue", source="ui", isolate=True,
                     thread="C1:1.0", scope={"cwd": str(repo)})["id"]
    wt3 = W.create(repo, tid3, fetch=False)
    (wt3 / "committed.txt").write_text("done before the restart\n")
    git(wt3, "add", "-A"); git(wt3, "commit", "-qm", "partial")
    git(repo, "worktree", "remove", "--force", str(wt3))
    st.update(tid3, checkpoint={"session_id": "sess-3", "at": time.time()})
    st.transition(tid3, T.RUNNING, "claimed")
    st.requeue_interrupted()
    seen = []
    _resume_harness(repo, st, sess, {"sess-3"}, seen)(st.claim())
    check("a lost checkout is rebuilt from the task's own branch, commits and all",
          seen and seen[0]["session_id"] == "sess-3"
          and "committed.txt" in seen[0]["files"], str(seen[:1]))

    # An ordinary rerun (no checkpoint) is untouched by any of this.
    tid4 = st.create("plain", driver="queue", source="ui", thread="C1:1.0",
                     scope={"cwd": str(root)})["id"]
    st.transition(tid4, T.RUNNING, "claimed")
    seen = []
    _resume_harness(repo, st, sess, {"sess-1"}, seen)(st.get(tid4))
    check("a task that was not interrupted runs its goal",
          seen and seen[0]["prompt"] == "plain", str(seen[:1]))


def test_resume_edge_cases():
    import tasks as T, worktrees as W
    from tasks import TaskStore
    print("\nresuming: live orphans, false starts, moved checkouts, rework")

    root = Path(tempfile.mkdtemp())
    W.ROOT = root / "wts"
    repo = root / "repo"; repo.mkdir()
    def git(cwd, *a): return subprocess.run(["git", *a], cwd=str(cwd),
                                            capture_output=True, text=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "base")
    st = TaskStore(root / "t.json")
    sess = tmp_store()

    def interrupted(goal, sid, cwd=None):
        tid = st.create(goal, driver="queue", source="ui", isolate=True,
                        thread="C1:1.0", scope={"cwd": str(repo)})["id"]
        cp = {"session_id": sid, "at": time.time()}
        if cwd is not None:
            cp["cwd"] = str(cwd)
        st.update(tid, session_id=sid, checkpoint=cp)
        st.transition(tid, T.RUNNING, "claimed")
        st.requeue_interrupted()
        # Claimed by id: claim() takes the oldest queued task, which would be
        # an earlier case's, not this one.
        before = st.get(tid)["attempts"]
        st.transition(tid, T.RUNNING, "claimed by the runner")
        return tid, st.get(tid) | {"attempts_before": before}

    # The restart did not take the child with it: it is still running.
    tid, task = interrupted("orphaned", "sess-live")
    seen = []
    _resume_harness(repo, st, sess, {"sess-live"}, seen, alive={"sess-live"})(task)
    rec = st.get(tid)
    check("a session whose child is still alive is not resumed under it",
          seen == [], str(seen[:1]))
    check("the task waits for it, parked with a retry time, not failed",
          rec["state"] == T.BLOCKED and rec["retry_at"] and rec["retry_at"] > time.time(),
          str({k: rec.get(k) for k in ("state", "retry_at")}))
    check("still holding its checkpoint, and charged no attempt",
          (rec.get("checkpoint") or {}).get("session_id") == "sess-live"
          and rec["attempts"] == task["attempts_before"] and not rec["false_starts"],
          str({k: rec.get(k) for k in ("checkpoint", "attempts", "false_starts")}))

    # The resumed run hits a quota wall before doing anything.
    tid, task = interrupted("quota", "sess-q")
    def quota(prompt, **kw):
        kw["on_init"]("sess-q")
        raise ClaudeError("You've hit your session limit · resets 6pm")
    ex = _resume_harness(repo, st, sess, {"sess-q"}, [])
    ex.__globals__["run_turn"] = quota
    ex(task)
    check("a resume that dies before any work keeps its checkpoint for the retry",
          (st.get(tid).get("checkpoint") or {}).get("session_id") == "sess-q",
          str(st.get(tid).get("checkpoint")))
    # ...but one that got going and then failed is ordinary failed work.
    tid, task = interrupted("worked then failed", "sess-w")
    def worked_then_failed(prompt, **kw):
        kw["on_init"]("sess-w")
        kw["on_activity"]("Bash", {"command": "make"})
        raise ClaudeError("Claude reported an error")
    ex = _resume_harness(repo, st, sess, {"sess-w"}, [])
    ex.__globals__["run_turn"] = worked_then_failed
    ex(task)
    check("a resume that did work and then failed does not keep it",
          not st.get(tid).get("checkpoint"), str(st.get(tid).get("checkpoint")))

    # The session ran somewhere other than where this run would put it.
    tid, task = interrupted("moved", "sess-m", cwd=root / "somewhere-else")
    seen = []
    _resume_harness(repo, st, sess, {"sess-m"}, seen)(task)
    check("a session from another directory is not resumed here; it starts fresh",
          seen and seen[0]["session_id"] is None and seen[0]["prompt"] == "moved",
          str(seen[:1]))
    # ...and where it did run is recorded, so this can be told.
    tid, task = interrupted("recorded", "sess-r")
    seen = []
    during = {}
    def look(prompt, **kw):
        kw["on_init"]("sess-r")
        during.update(st.get(tid))
        return types.SimpleNamespace(text="ok", cost_usd=0.0, duration_ms=1,
                                     session_id="sess-r")
    ex = _resume_harness(repo, st, sess, {"sess-r"}, seen)
    ex.__globals__["run_turn"] = look
    ex(task)
    check("the checkpoint records the directory the session runs in",
          (during.get("checkpoint") or {}).get("cwd") == str(W.path_for(repo, tid)),
          str(during.get("checkpoint")))

    # A fresh-context role resumes its *own* interrupted session too. Before,
    # it always started over: fresh roles never resumed anything.
    tid = st.create("look at it", driver="queue", source="ui", role="ideator",
                    thread="C1:1.0", scope={"cwd": str(root)})["id"]
    st.update(tid, checkpoint={"session_id": "sess-idea", "at": time.time(),
                               "cwd": str(root)})
    st.transition(tid, T.RUNNING, "claimed")
    st.requeue_interrupted()
    st.transition(tid, T.RUNNING, "claimed by the runner")
    seen = []
    _resume_harness(repo, st, sess, {"sess-idea"}, seen)(st.get(tid))
    check("a fresh-context role resumes its own interrupted session",
          seen and seen[0]["session_id"] == "sess-idea"
          and seen[0]["prompt"] == T.RESUME_PROMPT, str(seen[:1]))

    # Work sent back with new instructions must read them, not "carry on".
    T_ = T
    def stale(goal):
        tid = st.create(goal, driver="queue", source="ui", role="implementor",
                        thread="C1:1.0", scope={"cwd": str(repo)})["id"]
        st.update(tid, checkpoint={"session_id": "sess-old", "at": 1.0})
        st.transition(tid, T_.RUNNING, "claimed")
        return tid
    posted = []
    client = types.SimpleNamespace(chat_postMessage=lambda **kw: posted.append(kw))
    ns = {"task_store": st, "tasks": T, "app": types.SimpleNamespace(client=client),
          "log": logging.getLogger("test"), "verify": __import__("verify"), "os": os,
          "task_state": lambda tid, s_, d="": st.transition(tid, s_, d)}
    _bot_fns({"send_back_uncommitted", "send_back_for_tests",
              "MAX_COMMIT_ATTEMPTS", "MAX_VERIFY_ATTEMPTS"}, ns)
    a = stale("loose")
    ns["send_back_uncommitted"](st.get(a), ["x.txt"], "C1", "1.0")
    b = stale("failing")
    ns["send_back_for_tests"](st.get(b), {"output": "1 failed", "ok": False, "ran": True},
                              "C1", "1.0")
    c = stale("reviewed")
    st.transition(c, T.AWAITING_APPROVAL, "review flagged it")
    handle = _bot_func("handle_tasks", task_store=st, tasks=T,
                       log=logging.getLogger("test"))
    r = handle({"action": "rework", "id": c, "notes": "do it the other way"})
    for tid, how in ((a, "for uncommitted work"), (b, "for failing tests"),
                     (c, "from the dashboard")):
        rec = st.get(tid)
        check(f"sent back {how}: requeued without the stale checkpoint",
              rec["state"] == T.QUEUED and not rec.get("checkpoint"),
              str({k: rec.get(k) for k in ("state", "checkpoint")}))


def test_flagged_work_fixes_itself_once():
    """A flagged review, or a landing that conflicts with work merged first,
    is sent back once on its own before it reaches a person.

    48 of 119 reviews in the fortnight to 2026-10-02 flagged their work and
    went straight to awaiting_approval, and every landing refused at the
    rebase parked there too -- each on the board even when the fix was
    mechanical. Driven through resolve_review against a real TaskStore.
    """
    print("\nflagged reviews and conflicts are sent back once on their own")
    import roles
    import tasks as T

    store = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    posted, said = [], []
    outcome = {}
    # The base a conflict is told about is the project's, as it stands now.
    projects_ = {"ready": {"slug": "ready", "test_cmd": "make test", "auto_merge": True,
                           "scope": {"cwd": "/repo", "branch": "main"}},
                 "manual": {"slug": "manual", "test_cmd": "make test",
                            "scope": {"cwd": "/repo", "branch": "main"}}}

    def task_state(tid, state, detail=""):
        try:
            store.transition(tid, state, detail)
        except T.InvalidTransition:
            pass
    ns = {"task_store": store, "tasks": T, "roles": roles, "merge": __import__("merge"),
          "projects": __import__("projects"), "branches": __import__("branches"),
          "log": logging.getLogger("test"), "task_state": task_state,
          "project_store": types.SimpleNamespace(
              get=lambda slug: projects_.get(slug),
              scope_for=lambda slug: dict((projects_.get(slug) or {}).get("scope") or {})),
          "app": types.SimpleNamespace(client=types.SimpleNamespace(
              chat_postMessage=lambda **kw: posted.append(kw["text"]))),
          "tell_thread": lambda key, text: said.append((key, text)),
          "land_and_record": lambda tid, ch, ts: outcome,
          "file_followups": lambda *a, **k: []}
    got = _bot_fns({"resolve_review", "rework_flagged_review", "rework_conflict",
                    "conflict_addendum", "send_back", "review_addendum", "_unsupervised",
                    "MAX_REVIEW_REWORKS", "MAX_CONFLICT_REWORKS", "CONFLICT_STAGES"}, ns)
    check("the gate and its send-backs were lifted out of bot.py",
          {"resolve_review", "rework_flagged_review", "rework_conflict",
           "send_back"} <= got, str(sorted(got)))
    resolve = ns["resolve_review"]

    def parked(project):
        t = store.create("Make the thing work", title="Thing", project=project,
                         role="implementor", driver="queue",
                         scope={"cwd": "/repo", "branch": "main"})
        store.transition(t["id"], T.RUNNING, "claimed")
        return t["id"]

    def review(tid, ok, findings=(), landing=None):
        """One review cycle: the implementor parks for it, the verdict lands."""
        nonlocal outcome
        outcome = landing or {"eligible": True, "landed": True, "stage": "done",
                              "head": "abc1234"}
        if store.get(tid)["state"] == T.QUEUED:          # the rerun
            store.transition(tid, T.RUNNING, "claimed")
            # What execute_task writes when the rerun's turn ends: `result`
            # whole, so the previous review is gone from it.
            store.update(tid, result={"text": "addressed it", "cost": 0.0})
        store.update(tid, blocked_on=["rev"], verified=True)
        store.transition(tid, T.BLOCKED, "awaiting review")
        rev = store.create("review it", role="reviewer", parent=tid)
        said.clear()
        resolve(rev, "reviewer", "```json\n" + json.dumps(
            {"ok": ok, "summary": "s", "findings": list(findings)}) + "\n```",
            "C1", "1.0")
        return store.get(tid)

    # --- review: the first flag goes back, the second comes to you -----------
    tid = parked("ready")
    t = review(tid, False, ["the guard fails open"])
    check("a first flagged review on a ready project is sent back, not parked",
          t["state"] == T.QUEUED, f"state is {t['state']}")
    check("and counted on the task", t.get("review_reworks") == 1,
          str(t.get("review_reworks")))
    check("with the findings appended to its goal",
          t["goal"].startswith("Make the thing work")
          and "- the guard fails open" in t["goal"], t["goal"])
    check("the way Send back does: blocked_on and the verdict cleared",
          t.get("blocked_on") == [] and t.get("verified") is None
          and t.get("driver") == "queue", str({k: t.get(k) for k in
                                               ("blocked_on", "verified", "driver")}))
    check("and one line in its thread saying why",
          len(said) == 1 and said[0][0] == "C1:1.0"
          and "review" in said[0][1].lower() and "automatically" in said[0][1],
          str(said))
    t = review(tid, False, ["the guard still fails open on timeout"])
    check("a second flagged review parks for a person",
          t["state"] == T.AWAITING_APPROVAL, f"state is {t['state']}")
    check("without being sent back again", t.get("review_reworks") == 1 and not said,
          str((t.get("review_reworks"), said)))
    rv = t["result"]["review"]
    check("with both reviews' findings on the record",
          rv["findings"] == ["the guard still fails open on timeout"]
          and rv.get("earlier") == ["the guard fails open"], str(rv))
    check("and both in the verdict posted to the thread",
          "the guard fails open\n" in posted[-1] + "\n"
          and "still fails open on timeout" in posted[-1], posted[-1])

    # A flag with nothing to act on is not mechanical: it parks.
    bare = review(parked("ready"), False, [])
    check("a flag without findings is not sent back",
          bare["state"] == T.AWAITING_APPROVAL and not bare.get("review_reworks"))

    # --- landing: the first conflict goes back, the second comes to you ------
    conflict = {"eligible": True, "landed": False, "stage": "rebase",
                "detail": "CONFLICT (content): Merge conflict in bot.py",
                "branch": "silkworm/tsk_x"}
    lid = parked("ready")
    t = review(lid, True, landing=conflict)
    check("a first rebase conflict on a ready project is sent back, not parked",
          t["state"] == T.QUEUED, f"state is {t['state']}")
    check("counted apart from review send-backs",
          t.get("conflict_reworks") == 1 and not t.get("review_reworks"),
          str({k: t.get(k) for k in ("conflict_reworks", "review_reworks")}))
    check("with the refusal and what to do about it in its goal",
          "Merge conflict in bot.py" in t["goal"] and "up to date" in t["goal"]
          and "silkworm/tsk_x" in t["goal"] and "main" in t["goal"], t["goal"])
    check("and the gates reset so it is tested and reviewed again",
          t.get("blocked_on") == [] and t.get("verified") is None)
    check("and one line in its thread saying why",
          len(said) == 1 and "rebase" in said[0][1] and "automatically" in said[0][1],
          str(said))
    # The rerun's review may still flag it: that budget was not spent.
    t = review(lid, False, ["the conflict resolution dropped a branch"])
    check("a conflict send-back does not spend the review's",
          t["state"] == T.QUEUED and t.get("review_reworks") == 1, t["state"])
    t = review(lid, True, landing=dict(conflict, stage="tests-after-rebase",
                                        detail="1 failed"))
    check("a second conflict parks for a person",
          t["state"] == T.AWAITING_APPROVAL and t.get("conflict_reworks") == 1
          and not said, f"state is {t['state']}, said {said}")
    check("saying the landing refused",
          "landing refused" in t["events"][-1]["detail"], t["events"][-1]["detail"])

    t = review(parked("ready"), True,
               landing=dict(conflict, stage="tests-after-rebase", detail="2 failed"))
    check("failing tests after the rebase are sent back too",
          t["state"] == T.QUEUED and t.get("conflict_reworks") == 1
          and "2 failed" in t["goal"])
    for stage in ("base-moved", "merge", "attach", "errored", "no-test-command"):
        t = review(parked("ready"), True, landing=dict(conflict, stage=stage))
        check(f"a {stage} refusal parks as before",
              t["state"] == T.AWAITING_APPROVAL and not t.get("conflict_reworks")
              and not said, t["state"])

    # --- projects that are not ready: exactly as before ----------------------
    for project in ("manual", "nobody-registered-this"):
        t = review(parked(project), False, ["broken"])
        check(f"a flagged review on {project!r} still parks",
              t["state"] == T.AWAITING_APPROVAL and not t.get("review_reworks")
              and "- broken" not in t["goal"] and not said, t["state"])
        t = review(parked(project), True, landing=conflict)
        check(f"a conflict on {project!r} still parks",
              t["state"] == T.AWAITING_APPROVAL and not t.get("conflict_reworks")
              and not said, t["state"])
    t = review(parked("manual"), False, ["broken"])
    check("and its record carries no earlier findings it never had",
          "earlier" not in t["result"]["review"])

    # Both reviews' findings are where the decision is made, not only stored.
    import home
    detail = home.full_detail(store.get(tid))
    check("the board's detail shows both reviews' findings",
          "still fails open on timeout" in detail and "the guard fails open" in detail
          and "earlier pass" in detail, detail)
    vz = (BASE / "visualizer.py").read_text()
    rvjs = vz[vz.index("function review(t)"):vz.index("function lastEvent(")]
    check("and so does the dashboard", "rv.earlier" in rvjs)
    check("both counters, and the findings sent back for, are declared fields",
          {"review_reworks", "conflict_reworks", "reworked_findings"} <= set(T.FIELDS))

    # --- a task closed while its review ran is not reopened ------------------
    gone = parked("ready")
    store.update(gone, blocked_on=["rev"])
    store.transition(gone, T.BLOCKED, "awaiting review")
    store.transition(gone, T.CANCELLED, "dismissed")
    rev = store.create("review it", role="reviewer", parent=gone)
    said.clear()
    resolve(rev, "reviewer", '```json\n{"ok": false, "summary": "s", '
            '"findings": ["x"]}\n```', "C1", "1.0")
    t = store.get(gone)
    check("a task closed during its review is not sent back",
          t["state"] == T.CANCELLED and not t.get("review_reworks")
          and t["goal"] == "Make the thing work" and not said,
          str({k: t.get(k) for k in ("state", "review_reworks")}))

    # A verdict that arrives after its task was claimed again must not queue a
    # second concurrent run of it.
    late = parked("ready")
    store.update(late, blocked_on=["rev"])
    store.transition(late, T.BLOCKED, "awaiting review")
    store.transition(late, T.QUEUED, "requeued")
    store.transition(late, T.RUNNING, "claimed again")
    said.clear()
    resolve(store.create("review it", role="reviewer", parent=late), "reviewer",
            '```json\n{"ok": false, "summary": "s", "findings": ["x"]}\n```',
            "C1", "1.0")
    t = store.get(late)
    check("a late verdict does not send back a task that is running again",
          t["state"] != T.QUEUED and not t.get("review_reworks") and not said,
          str({k: t.get(k) for k in ("state", "review_reworks")}))

    # --- the rerun stands on the branch it is told to fix ----------------------
    # Sending work back is pointless if the rerun starts from the base: the
    # findings, and the rebase instruction, are both about commits on the
    # task's own branch. Driven through the real execute_task and git.
    import worktrees as W
    import contextlib
    import threading
    root = Path(tempfile.mkdtemp())
    W.ROOT = root / "wts"
    repo = root / "repo"; repo.mkdir()
    def git(cwd, *a):
        return subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a\n"); git(repo, "add", "-A"); git(repo, "commit", "-qm", "base")
    gt = store.create("Make the thing work", project="ready", role="implementor",
                      driver="queue", isolate=True, scope={"cwd": str(repo)})["id"]
    store.transition(gt, T.RUNNING, "claimed")
    wt = W.create(repo, gt, fetch=False)
    (wt / "fix.py").write_text("x = 1\n"); git(wt, "add", "-A"); git(wt, "commit", "-qm", "the work")
    W.release(wt)
    store.update(gt, blocked_on=["rev"], branch=W.BRANCH_PREFIX + gt)
    store.transition(gt, T.BLOCKED, "awaiting review")
    resolve(store.create("review it", role="reviewer", parent=gt), "reviewer",
            '```json\n{"ok": false, "summary": "s", "findings": ["add a test"]}\n```',
            "C1", "1.0")
    check("(the work was sent back)", store.get(gt)["state"] == T.QUEUED)
    store.transition(gt, T.RUNNING, "claimed by the runner")
    seen = {}

    def run_turn(goal, **kw):
        here = Path(kw["cwd"])
        seen.update(cwd=here, has_work=(here / "fix.py").exists(),
                    head=git(here, "log", "--format=%s", "-1").stdout.strip())
        return types.SimpleNamespace(text="added the test", cost_usd=0.0,
                                     duration_ms=1, session_id="s")
    _bot_func("execute_task", tasks=T, task_store=store, store=tmp_store(),
              roles=roles, worktrees=W, Path=Path, run_turn=run_turn,
              OUTBOX_ROOT=root / "outbox", SILKWORM_BIN="/x/silkworm",
              permission_args=lambda: [], log=logging.getLogger("test"),
              task_thread=lambda t: ("C1", "1.0"),
              task_state=lambda t_, s_, d="": task_state(t_, s_, d),
              _thread_lock=lambda key: threading.Lock(),
              repo_guard=lambda *a, **k: contextlib.nullcontext(),
              render_block=lambda _: "", chunk=lambda text: [text],
              to_mrkdwn=lambda text: text, resolve_review=lambda *a, **k: False,
              upload_outbox=lambda *a, **k: [], RUNNING={}, RUNNING_TASKS={},
              ClaudeStopped=ClaudeStopped, ClaudeError=ClaudeError,
              # An implementor borrows no one's branch; a stub would read as one.
              review_branch=lambda t_: "",
              record_branch=lambda *a, **k: None,
              )(store.get(gt))
    check("a sent-back rerun works on the branch holding the work it is fixing",
          seen.get("has_work") and seen.get("head") == "the work"
          and seen.get("cwd") != repo,
          f"ran in {seen.get('cwd')} at {seen.get('head')!r}: a fresh branch off "
          "the base has none of what the findings are about")
    check("and the branch was not set aside to make way for a fresh one",
          not git(repo, "tag", "-l", "discarded/*").stdout.strip(),
          git(repo, "tag", "-l").stdout)

    # --- a person's Send back spends the same once -----------------------------
    hand = parked("ready")
    store.update(hand, blocked_on=["rev"])
    store.transition(hand, T.BLOCKED, "awaiting review")
    store.transition(hand, T.AWAITING_APPROVAL, "flagged")
    store.update(hand, result={"review": {"ok": False, "findings": ["y"]}})
    handle = _bot_func("handle_tasks", task_store=store, tasks=T,
                       log=logging.getLogger("test"))
    r = handle({"action": "rework", "id": hand, "notes": "try again"})
    t = store.get(hand)
    check("the dashboard's Send back still requeues with findings and notes",
          r.get("ok") and t["state"] == T.QUEUED and "- y" in t["goal"]
          and "try again" in t["goal"], str(r))
    check("and counts as the review's one send-back",
          t.get("review_reworks") == 1, str(t.get("review_reworks")))
    t = review(hand, False, ["still y"])
    check("so the next flag comes to the person who already sent it back",
          t["state"] == T.AWAITING_APPROVAL, t["state"])


def test_proposals_are_deduplicated():
    """A proposal the board already holds is refused, naming what it matched.

    226 of 228 proposals in the fortnight to 2026-10-01 were accepted, and
    twelve implementor runs finished with nothing to land: the duplicate got
    through filing and then through triage. Both unattended filing doors are
    driven here -- the `/file-task` route the nightly ideator calls, and the
    review gate's follow-ups -- against a real TaskStore.
    """
    print("\nproposals are refused when the board already holds them")
    import dedup
    import tasks as T
    from unittest.mock import MagicMock

    # The measured threshold separates a rephrasing from a different job.
    a = ("Make tasks.json and sessions.json survive being killed mid-write: "
         "TaskStore._save and SessionStore._save truncate the file and then "
         "write it back, so a crash in between loses the whole board.")
    b = ("Write tasks.json and sessions.json atomically so being killed "
         "mid-write cannot truncate the board: TaskStore._save and "
         "SessionStore._save write the file back in place.")
    c = ("Show what each task cost on the dashboard, including the reviewer "
         "it spawned, and a per-project weekly spend line on the board.")
    check("a rephrasing of the same job scores over the line",
          dedup.similarity(a, b) >= dedup.THRESHOLD,
          f"{dedup.similarity(a, b):.2f}")
    check("a different job does not",
          dedup.similarity(a, c) < dedup.THRESHOLD, f"{dedup.similarity(a, c):.2f}")
    wrapped = {"goal": "A review of tsk_1 (Some title) found this alongside that "
                       "task, rather than in it:\n\nthe frobnicator leaks handles"
                       "\n\nCheck it is still true before changing anything -- the "
                       "review read the tree as it was then. If it is not, stop.",
               "title": "From review: the frobnicator leaks handles"}
    check("a follow-up is compared by its finding, not its shared framing",
          dedup.words(dedup.gist(wrapped))
          == dedup.words("the frobnicator leaks handles") != frozenset(),
          str(sorted(dedup.words(dedup.gist(wrapped)))))

    # --- the nightly pass's door ----------------------------------------------
    handle, filed_this_turn, begin_turn, ts, made = _file_task_impl()
    key = "C1:1.0"
    first = handle({"key": key, "goal": a, "propose": True, "project": "proj"})
    check("the first proposal files", first.get("ok"), str(first))
    before = len(ts.all())
    dup = handle({"key": key, "goal": b, "propose": True, "project": "proj"})
    check("a near-duplicate proposal is refused",
          not dup.get("ok") and len(ts.all()) == before, str(dup))
    check("and the refusal names the task it matched, with its title",
          (dup.get("error") or "").startswith(f"duplicate of {first['id']}: ")
          and "survive being killed" in dup.get("error", ""), dup.get("error"))
    other = handle({"key": key, "goal": c, "propose": True, "project": "proj"})
    check("a different idea on the same project still files", other.get("ok"), str(other))
    elsewhere = handle({"key": "C2:1.0", "goal": b, "propose": True, "project": "other"})
    check("the same idea on another project is not a duplicate",
          elsewhere.get("ok"), str(elsewhere))
    scoped = handle({"key": "C3:1.0", "goal": b, "project": "proj"})
    check("work scoped in conversation is not checked: you agreed to it",
          scoped.get("ok") and scoped.get("state") == T.QUEUED, str(scoped))

    # What counts: open, recently finished, unmerged -- not old or dismissed.
    now = time.time()
    def finished(goal, state, days_ago, **kw):
        rec = ts.create(goal, title=goal[:70], project="hist", role="implementor",
                        **kw)
        if state == T.DONE:
            ts.transition(rec["id"], T.RUNNING)
        ts.transition(rec["id"], state)
        with ts._lock:
            ts._data[rec["id"]]["updated"] = now - days_ago * 86400
        return ts.get(rec["id"])
    recent = finished(a, T.DONE, 3)
    hist = ts.by_project("hist")
    check("work finished this week is a duplicate",
          (dedup.find(b, hist, now=now) or [{}])[0].get("id") == recent["id"])
    ts._data.pop(recent["id"])
    old = finished(a, T.DONE, dedup.RECENT_DAYS + 6)
    hist = ts.by_project("hist")
    check("work finished weeks ago is not: it may have regressed since",
          dedup.find(b, hist, now=now) is None)
    check("unless its branch never merged",
          (dedup.find(b, hist, unmerged_ids=[old["id"]], now=now)
           or [{}])[0].get("id") == old["id"])
    ts._data.pop(old["id"])
    finished(a, T.CANCELLED, 1)
    check("a dismissed proposal is the board note's business, not a refusal",
          dedup.find(b, ts.by_project("hist"), now=now) is None)

    # survey asks git; a git that will not answer must not cost the filing.
    def broken(records):
        raise RuntimeError("git timed out")
    def guarded(*a):
        try:
            return dedup.duplicate(*a)
        except Exception as e:
            return f"raised {e!r}"
    got = guarded(b, ts.by_project("proj"), broken)
    check("an unanswerable survey still compares open work",
          got.startswith("duplicate of"), got)
    check("and refuses nothing it cannot match",
          guarded(c + " but for mail", [], broken) == "")
    surveyed = []
    dedup.duplicate(b, ts.by_project("proj"),
                    lambda recs: surveyed.append(len(recs)) or [])
    check("the unmerged half is asked of the project's own records",
          surveyed == [len(ts.by_project("proj"))], str(surveyed))

    # --- the review gate's door -------------------------------------------------
    store = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    ns = {"task_store": store, "scoping": __import__("scoping"), "tasks": T,
          "log": logging.getLogger("t"), "dedup": dedup,
          "branches": __import__("branches"),
          "project_store": types.SimpleNamespace(scope_for=lambda p: None)}
    _bot_fns({"file_followups"}, ns)
    file_followups = ns["file_followups"]
    open_one = store.create(a, title=a[:70], project="proj", role="implementor",
                            state=T.PROPOSED)
    parent = store.create("do the parent thing", title="Parent", project="proj",
                          role="implementor")
    seen: list = []
    got = file_followups(parent, [b, c], seen)
    check("a follow-up repeating open work is not filed",
          len(got) == 1 and "survive" not in store.get(got[0])["goal"], str(got))
    check("and what it matched is handed back to the gate",
          len(seen) == 1 and seen[0].startswith(f"duplicate of {open_one['id']}: "),
          str(seen))
    again = file_followups(parent, [c])
    check("two reviews finding the same thing file it once",
          again == [], str(again))
    # A follow-up is found next to the reviewed work and shares its words;
    # it must not be refused as a duplicate of that very task.
    near = ("The queue worker retry loop also retries immediately on a runner "
            "timeout error; it should back off there too.")
    reviewed = store.create("Make the queue worker back off when the runner "
                            "reports a quota error instead of retrying at once.",
                            project="neighbour", role="implementor")
    store.transition(reviewed["id"], T.RUNNING)
    store.transition(reviewed["id"], T.BLOCKED, "awaiting review")
    check("the fixture really does read as a duplicate of its parent",
          dedup.find(near, store.by_project("neighbour")) is not None)
    kin = file_followups(store.get(reviewed["id"]), [near])
    check("a follow-up is never refused as a duplicate of the task under review",
          len(kin) == 1, str(kin))

    # The gate records it on the parent and says it in the thread.
    store2 = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    ns2 = dict(ns, task_store=store2)
    _bot_fns({"file_followups"}, ns2)
    store2.create(a, title="Atomic saves — and survive a corrupt file",
                  project="proj", role="implementor", state=T.PROPOSED)
    impl = store2.create("the implementor's job", title="Impl", project="proj",
                         role="implementor")
    rev = store2.create("review it", role="reviewer", parent=impl["id"])
    app = MagicMock()
    resolve = _bot_func("resolve_review", task_store=store2, tasks=T,
                        roles=__import__("roles"), app=app,
                        file_followups=ns2["file_followups"],
                        rework_flagged_review=lambda *a: False,
                        rework_conflict=lambda *a: False,
                        log=logging.getLogger("t"))
    verdict = ('```json\n' + json.dumps({"ok": True, "summary": "fine",
                                         "followups": [b]}) + '\n```')
    resolve(dict(rev), "reviewer", verdict, "C1", "1.0")
    review = (store2.get(impl["id"]).get("result") or {}).get("review") or {}
    check("the gate records the refused follow-up on the task",
          len(review.get("duplicates") or []) == 1
          and review.get("filed") == [], str(review))
    posted = " ".join(str(c.kwargs.get("text", ""))
                      for c in app.client.chat_postMessage.call_args_list)
    check("and the thread says what it duplicated",
          "Not filed, already on the board" in posted and "duplicate of" in posted,
          posted[:300])
    check("naming it in full, even when its title has a dash in it",
          "Atomic saves — and survive a corrupt file (proposed)" in posted, posted[:400])


def test_task_costs():
    """What work costs: a task with its reviews, a project over a week.

    A missing cost is unknown, not zero -- adding it as $0 makes a floor read
    as a total.
    """
    print("\ntask and project costs")
    import costs
    import home
    import tasks as T

    ts = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    impl = ts.create("implement the thing", title="Implement", project="proj",
                     role="implementor")
    ts.transition(impl["id"], T.RUNNING)
    ts.update(impl["id"], result={"cost": 2.5})
    ts.transition(impl["id"], T.AWAITING_APPROVAL)
    r1 = ts.create("review", role="reviewer", parent=impl["id"])
    ts.transition(r1["id"], T.RUNNING)
    ts.update(r1["id"], result={"cost": 0.75})
    ts.transition(r1["id"], T.DONE)
    recs = [dict(r, id=k) for k, r in ts.all().items()]
    reviews = costs.reviews_by_parent(recs)
    total = costs.total(ts.get(impl["id"]), reviews.get(impl["id"], ()))
    check("a task's total includes the review it spawned",
          total == {"usd": 3.25, "complete": True}, str(total))
    check("rendered as dollars", costs.fmt(total) == "$3.25")

    # A second review that died without recording a cost.
    r2 = ts.create("review again", role="reviewer", parent=impl["id"])
    ts.transition(r2["id"], T.RUNNING)
    ts.transition(r2["id"], T.FAILED)
    recs = [dict(r, id=k) for k, r in ts.all().items()]
    reviews = costs.reviews_by_parent(recs)
    total = costs.total(ts.get(impl["id"]), reviews.get(impl["id"], ()))
    check("a run with no recorded cost makes the total a floor, not a sum with $0",
          total == {"usd": 3.25, "complete": False}, str(total))
    check("and it is shown as one", costs.fmt(total) == "$3.25+")
    lost = ts.create("ran, nothing recorded", project="proj", role="implementor")
    ts.transition(lost["id"], T.RUNNING)
    check("a task with nothing recorded at all is unknown, not $0",
          costs.fmt(costs.total(ts.get(lost["id"]))) == "$?")
    waiting = ts.create("not run yet", project="proj", state=T.PROPOSED,
                        role="implementor")
    check("one that never ran has no figure to show",
          costs.total(ts.get(waiting["id"])) is None)
    check("a cost of zero is a cost, not a missing one",
          costs.total({"state": T.DONE, "result": {"cost": 0.0}})
          == {"usd": 0.0, "complete": True})

    # The week, per project: the reviewer is filed under no project.
    now = time.time()
    old = ts.create("last month", project="proj", role="implementor")
    ts.transition(old["id"], T.RUNNING)
    ts.update(old["id"], result={"cost": 100.0})
    with ts._lock:
        ts._data[old["id"]]["updated"] = now - 10 * 86400
    other = ts.create("elsewhere", project="other", role="assistant")
    ts.transition(other["id"], T.RUNNING)
    ts.update(other["id"], result={"cost": 1.0})
    ts.transition(other["id"], T.DONE)
    recs = [dict(r, id=k) for k, r in ts.all().items()]
    week = costs.by_project(recs, now)
    check("a project's week counts its reviews and leaves older work out",
          week["proj"]["usd"] == 3.25 and week["other"]["usd"] == 1.0, str(week))
    check("and is marked incomplete when a run recorded nothing",
          week["proj"]["complete"] is False and week["other"]["complete"] is True)
    # Dated by when the money was spent, not when the record was touched.
    stale = ts.create("ran a fortnight ago", project="dated", role="implementor")
    ts.transition(stale["id"], T.RUNNING)
    ts.update(stale["id"], result={"cost": 50.0})
    ts.transition(stale["id"], T.AWAITING_APPROVAL)
    with ts._lock:
        for e in ts._data[stale["id"]]["events"]:
            e["at"] = now - 14 * 86400
    ts.transition(stale["id"], T.DONE, "approved today")
    recs = [dict(r, id=k) for k, r in ts.all().items()]
    check("approving old work today does not put its cost in this week",
          "dated" not in costs.by_project(recs, now),
          str(costs.by_project(recs, now).get("dated")))
    # Reworked today: only today's run is this week's.
    rec = ts.get(stale["id"])
    fields = costs.add_run(dict(rec, events=rec["events"] + [
        {"at": now - 60, "kind": T.RUNNING}]), 5.0, now=now)
    check("a rerun adds to the total", fields["cost"] == 55.0, str(fields))
    with ts._lock:
        ts._data[stale["id"]]["result"] = fields
    recs = [dict(r, id=k) for k, r in ts.all().items()]
    got = costs.by_project(recs, now).get("dated") or {}
    check("and only the rerun's dollars count in this week", got.get("usd") == 5.0, str(got))
    with ts._lock:
        ts._data.pop(stale["id"])
    recs = [dict(r, id=k) for k, r in ts.all().items()]
    line = costs.week_line(week)
    check("the week reads biggest first", line.index("proj") < line.index("other"), line)

    # --- the board ------------------------------------------------------------
    board = [dict(r, id=k) for k, r in ts.all().items()]
    for compact in (True, False):
        view = home.render(board, now=now, compact=compact)
        text = json.dumps(view)
        row = next(b for b in view["blocks"] if b.get("block_id") == f"t:{impl['id']}")
        rowtext = json.dumps(row) + json.dumps(
            next((b for b in view["blocks"] if b.get("type") == "context"
                  and impl["id"] in json.dumps(b)), {}))
        check(f"the board shows a task's cost with its reviews ({'compact' if compact else 'full'})",
              "$3.25+" in rowtext, rowtext[:300])
        check(f"and the project week in its summary ({'compact' if compact else 'full'})",
              "last 7d" in text and "proj $3.25+" in text)
    compact = home.render(board, now=now, compact=True)
    row = next(b for b in compact["blocks"] if b.get("block_id") == f"t:{impl['id']}")
    check("a compact task is still one line of title and one of meta",
          row["text"]["text"].count("\n") == 1, row["text"]["text"])
    check("the summary with the week is still where the board lands",
          "last 7d" in json.dumps(compact["blocks"][-3:]))

    # --- the dashboard ----------------------------------------------------------
    handle = _bot_func("handle_tasks", task_store=ts, tasks=T,
                       log=logging.getLogger("t"),
                       _with_costs=None, _spend=None)
    ns = {"task_store": ts, "costs": costs}
    _bot_fns({"_with_costs", "_spend"}, ns)
    handle.__globals__.update(_with_costs=ns["_with_costs"], _spend=ns["_spend"])
    r = handle({"action": "attention"})
    row = next(t for t in r["tasks"] if t["id"] == impl["id"])
    check("the dashboard's task rows carry the total",
          row.get("cost_total") == {"usd": 3.25, "complete": False}, str(row.get("cost_total")))
    check("without writing it into the store",
          "cost_total" not in ts.get(impl["id"]))
    check("and the panel gets the week",
          "proj $3.25+" in r.get("spend_line", ""), r.get("spend_line"))
    only = handle({"action": "list", "project": "other"})
    check("filtered to a project, the week is that project's",
          set(only.get("spend") or {}) == {"other"}, str(only.get("spend")))

    # The panel renders what the payload carries.
    js = _re.search(r"<script>(.*?)</script>", (BASE / "visualizer.py").read_text(),
                    _re.S).group(1)
    fn = js[js.index("function costText"):js.index("function taskButtons")]
    probe = fn + """
      console.log(JSON.stringify([
        costText({cost_total: {usd: 3.25, complete: true}}),
        costText({cost_total: {usd: 3.25, complete: false}}),
        costText({cost_total: {usd: 0, complete: false}}),
        costText({cost_total: null}),
        costText({cost_total: {usd: 5602.4, complete: true}})]));"""
    out = subprocess.run(["node", "-e", probe], capture_output=True, text=True)
    if out.returncode != 0:
        check("the dashboard's cost text runs", False, out.stderr[:300])
    else:
        got = json.loads(out.stdout)
        check("the dashboard shows the total, its floor, and unknown, as the board does",
              ">$3.25<" in got[0] and ">$3.25+<" in got[1] and ">$?<" in got[2]
              and got[3] == "" and ">$5,602<" in got[4], str(got))


def test_a_rerun_adds_to_a_tasks_cost():
    """A task sent back runs again under the same id; its cost is the sum.

    Each run used to replace `result` whole, so a task reworked twice
    reported only its last run -- an undercount on exactly the tasks that
    cost the most.
    """
    print("\na rerun adds to a task's cost")
    import logging
    import threading
    import roles
    import tasks as T
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    root = Path(tempfile.mkdtemp())
    rec = st.create("answer a question again", role="assistant", project="p",
                    driver="queue", isolate=False, scope={"cwd": str(root)})
    # The first run, as execute_task left it.
    st.transition(rec["id"], T.RUNNING)
    st.update(rec["id"], result={"text": "first", "cost": 1.5})
    st.transition(rec["id"], T.NEEDS_INPUT, "asked a question")
    st.transition(rec["id"], T.QUEUED, "answered")
    st.transition(rec["id"], T.RUNNING, "claimed")

    def run_turn(goal, **kw):
        return types.SimpleNamespace(text="second", cost_usd=2.0,
                                     duration_ms=1, session_id="s")
    _bot_func("execute_task", tasks=T, task_store=st, store=tmp_store(),
              roles=roles, Path=Path, run_turn=run_turn,
              review_branch=lambda t: "", worktrees=__import__("worktrees"),
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
              )(st.get(rec["id"]))
    got = st.get(rec["id"])
    check("the second run's cost is added to the first's",
          (got.get("result") or {}).get("cost") == 3.5,
          str({k: got.get(k) for k in ("state", "result")}))



# --- reviews have their own lane -------------------------------------------------

def _review_roles():
    """REVIEW_ROLES as bot.py defines it, evaluated rather than retyped."""
    tree = ast.parse((BASE / "bot.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "REVIEW_ROLES" for t in n.targets))
    return eval(compile(ast.Expression(node.value), "bot.py", "eval"))


def _lanes(review_workers=1):
    return bot_functions("lane_filter", REVIEW_WORKERS=review_workers,
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
    st.transition(parent["id"], T.DONE, "finished")
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


def _turn_runner(st, sessions, locks, RUNNING, RUNNING_TASKS, run_turn, resolve_review):
    """execute_task as the workers run it, sharing the real per-turn bookkeeping."""
    import roles
    import tasks as T
    release = bot_functions("release_turn", RUNNING=RUNNING, RUNNING_TASKS=RUNNING_TASKS,
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
    return bot_functions("_thread_lock", "_repo_lock", "repo_guard",
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


# --- the daily digest -----------------------------------------------------------

def _digest_store():
    """A real TaskStore holding one of each thing the digest has to report,
    with event times set relative to a fixed `now`."""
    import tasks as T
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "tasks.json")
    now = 1_790_000_000.0
    H = 3600

    def rec(title, project="", state=None, events=(), **fields):
        r = st.create(title, title=title, project=project, **fields)
        if state:
            # Straight to the state, the way a long-lived record reaches it;
            # the events are what the digest reads, and they are set below.
            with st._lock:
                st._data[r["id"]]["state"] = state
        st.update(r["id"], events=[{"at": now - ago * H, "kind": k, "detail": d}
                                   for ago, k, d in events])
        with st._lock:
            st._data[r["id"]]["created"] = now - 100 * H
            # As a live record would read: last written at its last event.
            st._data[r["id"]]["updated"] = now - min([e[0] for e in events] or [100]) * H
        return r["id"]

    ids = {}
    ids["landed"] = rec("Fix the <halt> & marker", "trader", T.DONE,
                        [(3, T.RUNNING, ""), (2, T.DONE, "")], attempts=1,
                        result={"cost": 2.0, "landed": "a" * 40,
                                "landing": {"landed": True, "stage": "landed",
                                            "at": now - 2 * H}})
    # Recorded before landings were dated: its last `done` says when.
    ids["landed_undated"] = rec("Older-style landing", "trader", T.DONE,
                                [(5, T.RUNNING, ""), (4, T.DONE, "")], attempts=1,
                                result={"cost": 1.0, "landed": "b" * 40,
                                        "landing": {"landed": True, "stage": "landed"}})
    ids["landed_old"] = rec("Landed last week", "trader", T.DONE,
                            [(170, T.RUNNING, ""), (169, T.DONE, "")], attempts=1,
                            result={"cost": 9.0, "landed": "c" * 40,
                                    "landing": {"landed": True, "stage": "landed",
                                                "at": now - 169 * H}})
    ids["refused"] = rec("Rebase me", "silkworm", T.DONE,
                         [(6, T.RUNNING, ""), (5, T.DONE, "")], attempts=1,
                         result={"cost": 0.5, "landing": {
                             "eligible": True, "landed": False, "stage": "rebase",
                             "detail": "conflict in bot.py", "at": now - 5 * H}})
    ids["dropped"] = rec("Dropped on purpose", "silkworm", T.DONE,
                         [(6, T.RUNNING, ""), (5, T.DONE, "")], attempts=1,
                         result={"cost": 0.25, "landing": {
                             "landed": False, "stage": "dropped", "at": now - 5 * H}})
    ids["merging"] = rec("Merging right now", "silkworm", T.DONE,
                         [(1, T.RUNNING, ""), (0.5, T.DONE, "")], attempts=1,
                         result={"cost": 0.0, "landing": {
                             "eligible": True, "landed": False, "stage": "in-progress",
                             "detail": "merging", "at": now - 0.1 * H}})
    ids["nothing"] = rec("Already on main", "silkworm", T.DONE,
                         [(1, T.RUNNING, ""), (0.5, T.DONE, "")], attempts=1,
                         result={"cost": 0.0, "landing": {
                             "eligible": False, "landed": False,
                             "stage": "nothing-to-land", "at": now - 0.5 * H}})
    # Refused a week ago, before landings were dated, and touched since.
    ids["old_refusal"] = rec("Refused last week", "silkworm", T.CANCELLED,
                             [(170, T.RUNNING, ""), (169, T.DONE, ""), (1, T.CANCELLED, "")],
                             attempts=1, result={"cost": 0.0, "landing": {
                                 "eligible": True, "landed": False, "stage": "rebase"}})
    blocker = rec("Review still running", "", T.RUNNING, [(1, T.RUNNING, "")],
                  role="reviewer", attempts=1, result={"cost": 0.0})
    ids["waits"] = rec("Waits on its review", "trader", T.BLOCKED,
                       [(20, T.BLOCKED, "awaiting review")], blocked_on=[blocker],
                       attempts=1, result={"cost": 0.0})
    gone = rec("Ended blocker", "", T.CANCELLED, [(19, T.CANCELLED, "")])
    ids["stranded"] = rec("Waits on nothing", "trader", T.BLOCKED,
                          [(20, T.BLOCKED, "awaiting review")], blocked_on=[gone],
                          attempts=1, result={"cost": 0.0})
    ids["held"] = rec("Add photo crop", "cadence", T.NEEDS_INPUT,
                      [(1, T.NEEDS_INPUT, "not run: cadence needs a test command")])
    ids["restart"] = rec("Killed by a restart", "silkworm", T.QUEUED,
                         [(3, T.RUNNING, ""), (2, T.FAILED, "interrupted by restart"),
                          (2, T.QUEUED, "requeued")], attempts=1,
                         result={"cost": None})
    impl = rec("Reviewed work", "cadence", T.AWAITING_APPROVAL,
               [(9, T.RUNNING, ""), (8, T.BLOCKED, ""), (3, T.AWAITING_APPROVAL, "")],
               attempts=1, result={"cost": 3.0})
    ids["review"] = rec("Review: Reviewed work", "", T.FAILED,
                        [(4, T.RUNNING, ""), (3, T.FAILED, "reviewer errored")],
                        role="reviewer", parent=impl, attempts=1, result={"cost": 0.75})
    ids["rerun"] = rec("Flaky thing", "trader", T.DONE,
                       [(8, T.RUNNING, ""), (7, T.QUEUED, ""), (6.9, T.RUNNING, ""),
                        (5, T.DONE, "")], attempts=3, result={"cost": 4.0})
    ids["stuck"] = rec("Queued forever", "trader", T.QUEUED, [(20, T.QUEUED, "")])
    ids["fresh"] = rec("Just queued", "trader", T.QUEUED, [(1, T.QUEUED, "")])
    ids["sleeping"] = rec("Wakes tomorrow", "trader", T.BLOCKED, [(30, T.BLOCKED, "defer")],
                          retry_at=now + 5 * H)
    rec("Reviewed under a full board", "silkworm", T.DONE,
        result={"review": {"ok": True, "held": ["Retry the push once on a timeout",
                                                "Name the sweeper keep set"],
                           "held_at": now - 3 * H}})
    rec("Reviewed long ago", "silkworm", T.DONE,
        result={"review": {"ok": True, "held": ["A finding held last week"],
                           "held_at": now - 100 * H}})
    for i in range(2):
        rec(f"Idea {i}", "cadence", T.PROPOSED)
    rec("Chat in a DM", "", T.DONE, [(2, T.RUNNING, ""), (1, T.DONE, "")],
        attempts=1, source="slack", result={"cost": 0.4})
    rec("Quiet project", "odin", T.DONE, [(200, T.RUNNING, ""), (199, T.DONE, "")],
        attempts=1, result={"cost": 1.0})
    return st, now, ids


def test_daily_digest_renders_from_a_real_store():
    import digest
    import tasks as T
    st, now, ids = _digest_store()
    rows = [{"project": "trader", "commits": 3, "title": "a"},
            {"project": "trader", "commits": 2, "title": "b"}]
    text = digest.render(st.all().values(), now, branch_rows=rows,
                         released={"cadence": ["ios/v1.2.0"]})
    print("\n".join("      " + l for l in text.splitlines()))
    blocks, cur = {}, None
    for line in text.splitlines()[1:]:
        if line.startswith("*"):
            cur = line.split("*")[1]
            blocks[cur] = [line]
        elif cur:
            blocks[cur].append(line)
    trader, cadence, silk = (("\n".join(blocks.get(p, []))) for p in ("trader", "cadence", "silkworm"))

    check("digest: landed counts both landings in the window, titles escaped",
          "landed 2: Fix the &lt;halt&gt; &amp; marker; Older-style landing" in trader, trader)
    check("digest: a landing from last week is not today's news", "Landed last week" not in text)
    check("digest: review findings the cap held are counted and named",
          "review findings held at the proposal cap 2: Retry the push once on a "
          "timeout; Name the sweeper keep set" in silk, silk)
    check("digest: a finding held last week is not today's news",
          "A finding held last week" not in text)
    check("digest: a refused landing names its stage and why",
          "landing refused: Rebase me (rebase: conflict in bot.py)" in silk, silk)
    check("digest: a branch dropped on purpose is not a refusal", "Dropped on purpose" not in text)
    check("digest: a landing still under way is not a refusal", "Merging right now" not in text)
    check("digest: nothing to land (never eligible) is not a refusal", "Already on main" not in text)
    check("digest: an undated refusal is not passed off as today's", "Refused last week" not in text)
    tree = ast.parse((BASE / "bot.py").read_text())
    underway = next(n.value.value for n in tree.body if isinstance(n, ast.Assign)
                    and any(getattr(t, "id", "") == "LANDING_UNDERWAY" for t in n.targets))
    check("digest: knows the bot's real in-progress stage", underway in digest.NOT_REFUSALS,
          underway)
    check("digest: a held task says why it was held",
          "Add photo crop — held: not run: cadence needs a test command" in cadence, cadence)
    check("digest: a restart kill says so, and what became of it",
          "Killed by a restart — failed: interrupted by restart, now queued" in silk, silk)
    check("digest: a reviewer's failure is filed under its parent's project",
          "Review: Reviewed work — failed: reviewer errored" in cadence, cadence)
    check("digest: tasks run more than once are named with their run count",
          "ran more than once 1: Flaky thing (3 runs)" in trader, trader)
    check("digest: a task queued far longer than usual is stuck",
          "Queued forever — queued 20h" in trader, trader)
    check("digest: one queued an hour, or blocked until a set wake-up, is not",
          "Just queued" not in text and "Wakes tomorrow" not in text)
    check("digest: blocked on a blocker still open is not stuck", "Waits on its review" not in text)
    check("digest: blocked on one that ended is", "Waits on nothing — blocked 20h" in trader, trader)
    check("digest: 'stuck' scales with how long that state usually takes",
          digest.stuck_limit(T.QUEUED, {T.QUEUED: 10 * 3600}) == 30 * 3600
          and digest.stuck_limit(T.QUEUED, {T.QUEUED: 60}) == digest.STUCK_FLOOR_S[T.QUEUED])
    check("digest: what is waiting on you is counted per project",
          "waiting on you: 2 proposed · 1 awaiting approval · 1 needs input" in cadence, cadence)
    check("digest: unmerged branches use branches.line",
          "unmerged: 2 finished tasks on unmerged branches (5 commits)" in trader, trader)
    check("digest: release tags are listed", "released: ios/v1.2.0" in cadence, cadence)
    # Read by costs.by_project's rules: spend dated by when the run ended, and
    # anything that ran in the window with no cost recorded (the held task, the
    # restart kill) makes its project's figure a floor. The blocked wake-up ran
    # with no cost too, but last did anything 30h ago: not today's spend.
    check("digest: cost per project, from the runs in the window",
          trader.splitlines()[0] == "*trader* · $7.00", trader.splitlines()[:1])
    check("digest: cost with a part unknown is a floor, not a total",
          silk.startswith("*silkworm* · $0.75+"), silk.splitlines()[:1])
    check("digest: a reviewer's cost counts under its parent's project",
          cadence.startswith("*cadence* · $3.75+"), cadence.splitlines()[:1])
    # 7 (trader) + 0.75 (silkworm) + 3.75 (cadence) + 0.4 (the unfiled DM chat).
    check("digest: the total includes unfiled conversations, and is a floor",
          "· $11.90+ total" in text.splitlines()[0], text.splitlines()[0])
    check("digest: a week-old landing's cost is not today's spend", "$20" not in text)
    check("digest: a project where nothing happened is left out", "odin" not in text)
    check("digest: empty sections are left out, not printed empty",
          "stuck" not in silk and "released" not in trader and "landed" not in cadence)
    check("digest: nothing for an empty section header",
          not any(l.rstrip().endswith(": ") or l.rstrip() == "•" for l in text.splitlines()))

    quiet = digest.render([r for r in st.all().values() if r["title"] == "Quiet project"],
                          now, when=datetime(2026, 10, 2, 8, 0))
    check("digest: a day where nothing happened is one short line",
          "\n" not in quiet and "nothing happened" in quiet, quiet)
    check("digest: the empty day still names the date", "Fri 2 Oct" in quiet, quiet)


def test_daily_digest_schedule_once_a_day_with_catch_up():
    import digest
    import jsonstore as J
    D = datetime
    check("digest due: not before the time", not digest.due(D(2026, 10, 2, 7, 59), "08:00", ""))
    check("digest due: at the time", digest.due(D(2026, 10, 2, 8, 0), "08:00", ""))
    check("digest due: caught up when the bot was down at 08:00",
          digest.due(D(2026, 10, 2, 15, 30), "08:00", "2026-10-01"))
    check("digest due: never twice a day",
          not digest.due(D(2026, 10, 2, 23, 59), "08:00", "2026-10-02"))
    check("digest due: tomorrow again", digest.due(D(2026, 10, 3, 8, 1), "08:00", "2026-10-02"))
    check("digest due: off means never", not digest.due(D(2026, 10, 2, 12, 0), "", ""))

    path = Path(tempfile.mkdtemp()) / "digest.json"
    s = digest.Schedule(path, "08:00")
    s.mark(D(2026, 10, 2, 8, 0))
    check("digest schedule: once marked, not due again today", not s.due(D(2026, 10, 2, 8, 5)))
    again = digest.Schedule(path, "08:00")          # a restart at 08:05
    check("digest schedule: a restart remembers today's post",
          not again.due(D(2026, 10, 2, 8, 5)), again.last_on)
    check("digest schedule: and posts tomorrow", again.due(D(2026, 10, 3, 9, 0)))
    bad = Path(tempfile.mkdtemp()) / "digest.json"
    bad.write_text("{not json")
    check("digest schedule: an unreadable marker does not stop the digest",
          digest.Schedule(bad, "08:00").due(D(2026, 10, 2, 9, 0)))
    check("digest: '0' is midnight, not off", "0" not in digest.OFF)

    # The post went out but the marker file could not be written: this process
    # must still not post again.
    s2 = digest.Schedule(Path(tempfile.mkdtemp()) / "d.json", "08:00")
    real = J.save
    J.save = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    try:
        try:
            s2.mark(D(2026, 10, 2, 8, 0))
        except OSError:
            pass
    finally:
        J.save = real
    check("digest schedule: a failed marker write still stops a second post",
          not s2.due(D(2026, 10, 2, 9, 0)))


def test_daily_digest_posts_to_the_dm_never_the_board():
    import digest
    import tasks as T

    class Client:
        def __init__(self):
            self.posts = []

        def chat_postMessage(self, **kw):
            self.posts.append(kw)
            return {"ok": True, "ts": "1.1"}

    c = Client()
    check("digest post: to the DM", digest.post(c, "D123", "hi", refuse=("G9", "silkworm-board"))["ok"]
          and c.posts[-1]["channel"] == "D123")
    check("digest post: refused at the board's id",
          not digest.post(c, "G9", "hi", refuse=("G9", "silkworm-board"))["ok"])
    check("digest post: refused at the board's name",
          not digest.post(c, "#silkworm-board", "hi", refuse=("G9", "silkworm-board"))["ok"])
    check("digest post: nothing sent to the board either way", len(c.posts) == 1)

    # The bot's own send path, run for real against fakes.
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    client = Client()
    board = types.SimpleNamespace(channel=lambda cl: "G9", channel_name="silkworm-board")
    home = {"ch": "D777"}
    import digest as digest_mod
    ns = bot_functions("send_digest", digest=digest_mod, task_store=st,
                       app=types.SimpleNamespace(client=client), BOARD=board,
                       home_channel=lambda: home["ch"], datetime=datetime,
                       digest_inputs=lambda recs: ([], {}))
    sched = digest_mod.Schedule(Path(tempfile.mkdtemp()) / "d.json", "08:00")
    ns["send_digest"](sched, datetime(2026, 10, 2, 8, 0))
    check("send_digest: posted to the home DM", [p["channel"] for p in client.posts] == ["D777"],
          str(client.posts))
    check("send_digest: and marked the day", sched.last_on == "2026-10-02")
    home["ch"] = "G9"
    sched2 = digest_mod.Schedule(Path(tempfile.mkdtemp()) / "d.json", "08:00")
    ns["send_digest"](sched2, datetime(2026, 10, 2, 8, 0))
    check("send_digest: a home channel that is the board gets nothing",
          len(client.posts) == 1, str(client.posts))
    check("send_digest: and the day stays unmarked, so it is retried", sched2.last_on == "")

    tree = ast.parse((BASE / "bot.py").read_text())
    started = {n.args[0].id for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "start" and isinstance(n.func.value, ast.Name)
               and n.func.value.id == "daemons" and n.args and isinstance(n.args[0], ast.Name)}
    check("the digest scheduler is started as a daemon", "_digest_scheduler" in started)
    sched_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "_digest_scheduler")
    calls = {n.func.id for n in ast.walk(sched_fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    check("and it posts through send_digest, which uses home_channel", "send_digest" in calls)


def test_release_tags_created_today():
    import releases
    repo = Path(tempfile.mkdtemp())
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    def git(*a, **extra):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,
                       env={**env, **extra})
    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "one")
    git("tag", "-a", "ios/v1.0.0", "-m", "old", GIT_COMMITTER_DATE="2026-01-01T00:00:00")
    git("tag", "-a", "ios/v1.1.0", "-m", "new")
    git("tag", "-a", "backend/v2.0.1", "-m", "new")
    git("tag", "not-a-release")
    got = releases.tagged_since(repo, time.time() - 86400)
    check("tagged_since: today's release tags only",
          sorted(got) == ["backend/v2.0.1", "ios/v1.1.0"], str(got))
    check("tagged_since: nothing from the future", releases.tagged_since(repo, time.time() + 60) == [])
    check("tagged_since: not a repo reads as none",
          releases.tagged_since(tempfile.mkdtemp(), 0) == [])


def test_a_landing_outcome_is_dated():
    import tasks as T
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    t = st.create("x", project="p")
    ns = bot_functions("record_landing", task_store=st)
    before = time.time()
    ns["record_landing"](t, {"eligible": True, "landed": False, "stage": "rebase",
                             "detail": "conflict"})
    at = (st.get(t["id"])["result"]["landing"] or {}).get("at") or 0
    check("record_landing: the outcome carries when it happened", at >= before, str(at))
    check("and `at` survives compaction (it lives inside result.landing)",
          "landing" in T.RESULT_KEEPS)



# --- the implementor git guard -------------------------------------------------
# On 2026-10-02 a Cadence implementor merged its own commit into the checkout's
# main and pushed it to origin before verification or review had run. The
# prompt said not to; nothing enforced it. These hooks make git refuse it, for
# an implementor's turn only -- the user and the bot's own landing must not
# notice they exist.

def test_implementor_git_guard():
    import git_guard as GG
    import merge as M
    print("\nimplementor git guard: git refuses what only landing may do")
    root = Path(tempfile.mkdtemp())
    origin, repo = root / "origin.git", root / "repo"
    tid = "tsk_guard1"
    clean = GG.strip(dict(os.environ))           # the user, the bot's landing
    agent = {**clean, **GG.env_for("implementor", tid)}

    def git(cwd, *a, env=None):
        return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a],
                              cwd=str(cwd), capture_output=True, text=True,
                              env=env if env is not None else clean)

    def sha(ref, cwd=repo):
        return git(cwd, "rev-parse", "-q", "--verify", ref).stdout.strip()

    check("this git has reference-transaction (2.28+)", GG.git_version() >= GG.MIN_GIT,
          str(GG.git_version()))
    git(root, "init", "-q", "--bare", "-b", "main", str(origin))
    git(root, "init", "-q", "-b", "main", str(repo))
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "a")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")

    # Hooks already in place, which must be chained rather than lost: each
    # records that it ran, and what it was given.
    hooks = repo / ".git" / "hooks"
    log_ = root / "chained.log"
    (hooks / "pre-push").write_text(f'#!/bin/sh\necho "pre-push $1" >> "{log_}"\ncat >/dev/null\n')
    (hooks / "reference-transaction").write_text(
        f'#!/bin/sh\nwhile read -r o n r; do echo "rt $1 $r" >> "{log_}"; done\n')
    for h in ("pre-push", "reference-transaction"):
        (hooks / h).chmod(0o755)
    before_rt = (hooks / "reference-transaction").read_text()

    res = GG.install(repo)
    check("install: both hooks go in", res["ok"] and sorted(res["changed"]) == sorted(GG.HOOKS),
          str(res))
    check("install: an existing hook is moved aside and chained, not replaced",
          sorted(res["chained"]) == sorted(GG.HOOKS)
          and (hooks / ("reference-transaction" + GG.CHAINED_SUFFIX)).read_text() == before_rt,
          str(res))
    again = GG.install(repo)
    check("install is idempotent: a second run changes nothing and chains nothing again",
          again["ok"] and not again["changed"] and not again["chained"]
          and (hooks / ("reference-transaction" + GG.CHAINED_SUFFIX)).read_text() == before_rt,
          str(again))
    check("status reads it as installed", GG.state(repo)[0], GG.state(repo)[1])

    wt = root / "wt"
    git(repo, "worktree", "add", "-q", "-b", f"silkworm/{tid}", str(wt), "main")
    log_.write_text("")

    # --- an implementor, in its own worktree ---------------------------------
    (wt / "b.txt").write_text("b\n")
    git(wt, "add", "-A", env=agent)
    r = git(wt, "commit", "-q", "-m", "b", env=agent)
    check("implementor: committing to its own branch works", r.returncode == 0, r.stderr[-200:])
    check("and the existing reference-transaction hook still ran (chained)",
          f"rt committed refs/heads/silkworm/{tid}" in log_.read_text(), log_.read_text()[-300:])
    r = git(wt, "commit", "-q", "--amend", "-m", "b2", env=agent)
    check("implementor: amending works", r.returncode == 0, r.stderr[-200:])
    (repo / "c.txt").write_text("c\n")
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "c")      # the user, on main
    r = git(wt, "rebase", "-q", "main", env=agent)
    check("implementor: rebasing its own branch onto the base works",
          r.returncode == 0 and (wt / "c.txt").exists(), r.stderr[-200:])
    r = git(wt, "fetch", "-q", "origin", env=agent)
    check("implementor: fetch works (remote-tracking refs are allowed)", r.returncode == 0,
          r.stderr[-200:])
    r = git(wt, "pack-refs", "--all", env=agent)
    r2 = git(wt, "gc", "-q", env=agent)
    check("implementor: pack-refs / gc work (they move no ref)",
          r.returncode == 0 and r2.returncode == 0, (r.stderr + r2.stderr)[-200:])

    main_at, origin_at = sha("main"), sha("main", origin)
    r = git(wt, "update-ref", "refs/heads/main", "HEAD", env=agent)
    check("implementor: moving main directly is refused",
          r.returncode != 0 and sha("main") == main_at and "silkworm" in r.stderr, r.stderr[-200:])
    r = git(repo, "merge", "-q", "--no-edit", f"silkworm/{tid}", env=agent)
    check("implementor: merging into main (in the main checkout) is refused",
          r.returncode != 0 and sha("main") == main_at, r.stderr[-200:])
    git(repo, "merge", "--abort")
    git(repo, "reset", "-q", "--hard", "main")
    r = git(wt, "push", "origin", "HEAD:main", env=agent)
    check("implementor: pushing to the remote's main is refused",
          r.returncode != 0 and sha("main", origin) == origin_at
          and "may not push" in r.stderr, r.stderr[-300:])
    r = git(wt, "push", "origin", f"silkworm/{tid}", env=agent)
    check("implementor: pushing even its own branch is refused",
          r.returncode != 0 and not sha(f"refs/heads/silkworm/{tid}", origin), r.stderr[-200:])
    r = git(wt, "tag", "v9", env=agent)
    check("implementor: tagging is refused", r.returncode != 0 and not sha("refs/tags/v9"),
          r.stderr[-200:])
    r = git(wt, "branch", "other", env=agent)
    check("implementor: making another branch is refused",
          r.returncode != 0 and not sha("refs/heads/other"), r.stderr[-200:])
    # A tag pushed to origin mid-task (release CI) is followed into refs/tags
    # by any fetch or pull. That is the remote's tag, not the task's.
    side = root / "side"
    git(root, "clone", "-q", str(origin), str(side))
    git(side, "commit", "-q", "--allow-empty", "-m", "elsewhere")
    git(side, "tag", "v7")
    git(side, "push", "-q", "origin", "HEAD:refs/heads/side", "v7")
    log_.write_text("")
    r = git(wt, "fetch", "origin", env=agent)
    check("implementor: fetch still works when origin has a new tag to follow",
          r.returncode == 0 and sha("refs/tags/v7"), r.stderr[-300:])
    check("and the chained hook was still handed the state, not the command line",
          "rt prepared refs/tags/v7" in log_.read_text(), log_.read_text()[-300:])
    git(repo, "tag", "-d", "v7")
    r = git(wt, "update-ref", "-m", "fix pack-refs", "refs/heads/main", "HEAD", env=agent)
    check("implementor: only pack-refs itself is exempt, not a command mentioning it",
          r.returncode != 0 and sha("main") == main_at, r.stderr[-200:])
    r = git(wt, "-c", "x.y=z pack-refs", "update-ref", "refs/heads/main", "HEAD", env=agent)
    check("implementor: a command disguised as pack-refs on ps's command line is refused",
          r.returncode != 0 and sha("main") == main_at, r.stderr[-200:])
    r = git(wt, "-c", "x.y=fetch", "update-ref", "refs/tags/v8", "HEAD", env=agent)
    check("implementor: and only fetch itself may follow tags, not a command mentioning it",
          r.returncode != 0 and not sha("refs/tags/v8"), r.stderr[-200:])
    other = {**clean, **GG.env_for("implementor", "tsk_someone_else")}
    tip = sha(f"refs/heads/silkworm/{tid}")
    (wt / "d.txt").write_text("d\n")
    git(wt, "add", "-A", env=other)
    r = git(wt, "commit", "-q", "-m", "d", env=other)
    check("implementor: another task's branch is not its own",
          r.returncode != 0 and sha(f"refs/heads/silkworm/{tid}") == tip, r.stderr[-200:])
    git(wt, "reset", "-q", "--hard", env=clean)
    for role in ("reviewer", "assistant"):
        r = git(wt, "tag", f"t-{role}", env={**clean, **GG.env_for(role, tid)})
        check(f"the guard is the implementor's only: a {role} is not refused",
              r.returncode == 0, r.stderr[-200:])
        git(repo, "tag", "-d", f"t-{role}")

    # --- the user, and the bot's own landing: untouched --------------------
    log_.write_text("")
    r = git(repo, "tag", "u1")
    check("unset: the user can tag", r.returncode == 0, r.stderr[-200:])
    r = git(repo, "branch", "scratch")
    check("unset: the user can make a branch", r.returncode == 0, r.stderr[-200:])
    r = git(repo, "push", "-q", "origin", "main")
    check("unset: the user can push, and the existing pre-push hook still ran",
          r.returncode == 0 and sha("main", origin) == sha("main")
          and "pre-push origin" in log_.read_text(), r.stderr[-200:] + log_.read_text()[-200:])
    # The real landing, with publish, as the bot runs it: in-process, with
    # whatever this process's environment is -- stripped, exactly as bot.py
    # strips its own at import. Run by an implementor, this suite inherits the
    # role, and that is the point of stripping it.
    saved = {k: os.environ.pop(k) for k in (GG.ROLE_VAR, GG.ID_VAR) if k in os.environ}
    try:
        lr = M.land(wt, repo, f"silkworm/{tid}", "main", lambda cwd: {"ok": True, "ran": True},
                    publish=True)
    finally:
        os.environ.update(saved)
    check("unset: Silkworm's own landing merges and publishes through the hooks",
          lr.get("landed") and lr.get("published") is True
          and sha("main") == sha(f"refs/heads/silkworm/{tid}") == sha("main", origin),
          f"{lr.get('stage')}: {str(lr.get('detail'))[:200]}")
    r = git(repo, "update-ref", "refs/heads/main", "main~1")
    check("unset: the user can move main", r.returncode == 0, r.stderr[-200:])

    # --- core.hooksPath, and a hook we must not clobber ----------------------
    r2 = root / "r2"
    git(root, "init", "-q", "-b", "main", str(r2))
    custom = root / "myhooks"
    custom.mkdir()
    (custom / "pre-push").write_text("#!/bin/sh\nexit 0\n"); (custom / "pre-push").chmod(0o755)
    git(r2, "config", "core.hooksPath", str(custom))
    res = GG.install(r2)
    check("core.hooksPath: installs where git will look, chaining what is there",
          res["ok"] and (custom / "reference-transaction").exists()
          and (custom / ("pre-push" + GG.CHAINED_SUFFIX)).read_text() == "#!/bin/sh\nexit 0\n"
          and not (r2 / ".git" / "hooks" / "pre-push").exists(), str(res))
    (custom / "pre-push").write_text("#!/bin/sh\necho someone-elses\n")
    res = GG.install(r2)
    check("a foreign hook with the chained name already taken is refused, not overwritten",
          not res["ok"] and (custom / "pre-push").read_text() == "#!/bin/sh\necho someone-elses\n"
          and (custom / ("pre-push" + GG.CHAINED_SUFFIX)).read_text() == "#!/bin/sh\nexit 0\n",
          str(res))
    check("and status reports it unguarded", not GG.state(r2)[0], GG.state(r2)[1])

    def fresh(name):
        r_ = root / name
        git(root, "init", "-q", "-b", "main", str(r_))
        git(r_, "commit", "-q", "--allow-empty", "-m", "a")
        return r_
    r3 = fresh("r3")
    (r3 / ".githooks").mkdir()
    git(r3, "config", "core.hooksPath", ".githooks")
    res = GG.install(r3)
    check("a relative core.hooksPath is refused (each worktree would read its own)",
          not res["ok"] and "relative" in res["error"]
          and not list((r3 / ".githooks").iterdir()) and not GG.state(r3)[0], str(res))
    git(r3, "config", "core.hooksPath", str(r3 / ".githooks"))
    res = GG.install(r3)
    check("a hooks directory inside the work tree is refused (it would dirty the base)",
          not res["ok"] and "work tree" in res["error"]
          and not list((r3 / ".githooks").iterdir()) and not GG.state(r3)[0], str(res))
    r4 = fresh("r4")
    husky = '#!/bin/sh\n. "$(dirname "$0")/husky.sh"\nrun "$(basename "$0")"\n'
    (r4 / ".git" / "hooks" / "reference-transaction").write_text(husky)
    res = GG.install(r4)
    check("a hook that dispatches on its own name ($0) is not renamed out from under itself",
          not res["ok"] and "$0" in res["error"]
          and (r4 / ".git" / "hooks" / "reference-transaction").read_text() == husky
          and not (r4 / ".git" / "hooks" / ("reference-transaction" + GG.CHAINED_SUFFIX)).exists()
          and not (r4 / ".git" / "hooks" / "pre-push").exists(), str(res))
    import threading as _th
    r5 = fresh("r5")
    mine = "#!/bin/sh\necho mine\n"
    (r5 / ".git" / "hooks" / "pre-push").write_text(mine)
    (r5 / ".git" / "hooks" / "pre-push").chmod(0o755)
    outs = []

    class Slow:                  # widens the check-then-rename window to 200ms
        def search(self, text):
            time.sleep(0.2)
            return None
    real_re, GG._SELF_REFERENCE = GG._SELF_REFERENCE, Slow()
    try:
        ts = [_th.Thread(target=lambda: outs.append(GG.install(r5))) for _ in range(4)]
        [t.start() for t in ts]; [t.join() for t in ts]
    finally:
        GG._SELF_REFERENCE = real_re
    check("concurrent installs chain the hook once and lose nothing",
          (r5 / ".git" / "hooks" / ("pre-push" + GG.CHAINED_SUFFIX)).read_text() == mine
          and GG.state(r5)[0] and sum(len(o["chained"]) for o in outs) == 1
          and not list((r5 / ".git" / "hooks").glob(".*silkworm-tmp*")), str(outs)[:300])

    # --- status --------------------------------------------------------------
    pj = root / "projects.json"
    pj.write_text(json.dumps({
        "ready": {"slug": "ready", "scope": {"cwd": str(repo)}, "test_cmd": "true",
                  "auto_merge": True},
        "loose": {"slug": "loose", "scope": {"cwd": str(r2)}, "test_cmd": "", "auto_merge": False},
    }))
    from importlib.machinery import SourceFileLoader
    from importlib.util import module_from_spec, spec_from_loader
    loader = SourceFileLoader("silkworm_cli", str(BASE / "bin" / "silkworm"))
    cli = module_from_spec(spec_from_loader("silkworm_cli", loader))
    loader.exec_module(cli)
    with contextlib.redirect_stdout(io.StringIO()) as out:
        rows = cli.check_git_guards(pj)
    check("status: reports the guard on each ready project, and only those",
          rows == [("ready", True, rows[0][2] if rows else "")]
          and "implementor git guard on ready" in out.getvalue(), out.getvalue())
    status_fn = next(n for n in ast.parse((BASE / "bin" / "silkworm").read_text()).body
                     if isinstance(n, ast.FunctionDef) and n.name == "do_status")
    check("status: `silkworm status` runs the guard check",
          any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
              and c.func.id == "check_git_guards" for c in ast.walk(status_fn)))
    (hooks / "pre-push").unlink()
    with contextlib.redirect_stdout(io.StringIO()):
        rows = cli.check_git_guards(pj)
    check("status: a missing hook reads as unguarded", rows and rows[0][1] is False, str(rows))


def test_guard_env_reaches_task_turns_only():
    import git_guard as GG
    import roles as R_
    print("\nimplementor git guard: who carries the role")
    ns = bot_functions("claude_env", git_guard=GG, os=os, APPROVAL_PORT=1,
                       CLAUDE_APPROVAL_MODE="skip", APPROVAL_TIMEOUT=1)
    saved = {k: os.environ.get(k) for k in (GG.ROLE_VAR, GG.ID_VAR)}
    # As if this process had itself been started from inside a task turn.
    os.environ[GG.ROLE_VAR], os.environ[GG.ID_VAR] = "implementor", "tsk_leak"
    try:
        task = ns["claude_env"]("C:1", 0, role="implementor", task_id="tsk_abc")
        slack = ns["claude_env"]("C:1")
        bare = ns["claude_env"]()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    check("a task turn's env names its role and id",
          task.get(GG.ROLE_VAR) == "implementor" and task.get(GG.ID_VAR) == "tsk_abc", str(
              {k: task.get(k) for k in (GG.ROLE_VAR, GG.ID_VAR)}))
    check("a Slack turn, and every helper run, carries neither -- not even inherited",
          all(GG.ROLE_VAR not in e and GG.ID_VAR not in e for e in (slack, bare)))

    tree = ast.parse((BASE / "bot.py").read_text())
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    def env_calls(fn):
        return [c for c in ast.walk(fns[fn]) if isinstance(c, ast.Call)
                and isinstance(c.func, ast.Name) and c.func.id == "claude_env"]
    ex = env_calls("execute_task")
    check("execute_task hands its task id to the turn's env",
          ex and all(any(k.arg == "task_id" and isinstance(k.value, ast.Name)
                         and k.value.id == "tid" for k in c.keywords) for c in ex), f"{len(ex)} call(s)")
    others = [c for f in fns if f != "execute_task" for c in env_calls(f)]
    check("and nothing else does: no other claude_env call names a task id",
          others and not any(k.arg == "task_id" for c in others for k in c.keywords))
    stripped = [n for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                and ast.unparse(n.value) == "git_guard.strip(os.environ)"]
    check("the bot strips the role from its own environment at import, so landing never has it",
          len(stripped) == 1)
    main = next(n for n in tree.body if isinstance(n, ast.If)
                and "__main__" in ast.unparse(n.test))
    check("the bot installs the guard at startup",
          any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
              and c.func.id == "install_git_guards" for c in ast.walk(main)))
    sweeper = fns["_worktree_sweeper"]
    check("and re-installs it on the sweeper's round, so a project that became ready is covered",
          any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
              and c.func.id == "install_git_guards" for c in ast.walk(sweeper)))
    for action in ("test_cmd=cmd", "auto_merge=on"):
        block = (BASE / "bot.py").read_text().split(f"project_store.ensure(slug, {action})", 1)
        check(f"setting {action.split('=')[0]} from the dashboard re-installs at once",
              len(block) == 2 and "install_git_guards(slug)" in block[1][:200])

    p = R_.IMPLEMENTOR_SYSTEM.lower()
    check("the implementor is told it must not push or move the base branch",
          "do not push" in p and "base branch" in p and "hook" in p)
    check("and that 'done when it is on main' is met by reporting it ready to land",
          "ready to land" in p and "on main" in p)


def test_conflicting_landings_go_back_to_their_implementor():
    """A branch that cannot catch up with its base goes back to the implementor
    that wrote it, with the conflict attached -- by either door.

    Of the nine Silkworm branches done but unmerged on 2026-10-02, four were
    held up only or mainly by being 27-37 commits behind and conflicting; each
    repair then came back as a separate proposal costing a decision, a session
    and a reviewer. Driven against a real repository with an origin remote, in
    which main and the task's branch genuinely conflict, through the real
    resolve_review, approve_task, land_or_drop, start_landing, land_if_ready
    and merge.land.
    """
    print("\nconflicting landings are sent back to their implementor")
    import contextlib
    import roles
    import tasks as T
    import worktrees as W
    import branches as B

    root = Path(tempfile.mkdtemp())
    origin, repo = root / "origin.git", root / "repo"
    saved_root, W.ROOT = W.ROOT, root / "wts"

    def git(cwd, *a):
        return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                               *a], cwd=str(cwd), capture_output=True, text=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    git(root, "clone", "-q", str(origin), str(repo))
    (repo / "shared.py").write_text("VALUE = 1\n")
    (repo / "other.py").write_text("x = 1\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "base")
    git(repo, "push", "-q", "origin", "main")

    store = T.TaskStore(root / "t.json")
    projects_ = {"ready": {"slug": "ready", "test_cmd": "true", "auto_merge": True},
                 "manual": {"slug": "manual", "test_cmd": "true"}}
    said, posted = [], []

    def task_state(tid, state, detail=""):
        try:
            store.transition(tid, state, detail)
        except T.InvalidTransition:
            pass

    class SyncThread:
        """start() runs the landing in place, so its outcome can be read."""
        def __init__(self, target, **kw):
            self.target = target
        def start(self):
            self.target()
    import threading as _th
    ns = {"task_store": store, "tasks": T, "roles": roles, "merge": __import__("merge"),
          "projects": __import__("projects"), "branches": B, "worktrees": W,
          "verify": __import__("verify"), "discard": __import__("discard"),
          "holding": __import__("holding"), "subprocess": subprocess, "time": time,
          "log": logging.getLogger("test"), "task_state": task_state,
          "threading": types.SimpleNamespace(Lock=_th.Lock, Thread=SyncThread),
          "project_store": types.SimpleNamespace(
              get=lambda slug: projects_.get(slug),
              scope_for=lambda slug: dict((projects_.get(slug) or {}).get("scope") or {})),
          "app": types.SimpleNamespace(client=types.SimpleNamespace(
              chat_postMessage=lambda **kw: posted.append(kw["text"]))),
          "tell_thread": lambda key, text: said.append((key, text)),
          "repo_guard": lambda *a, **k: contextlib.nullcontext(),
          "file_followups": lambda *a, **k: [], "REVISION": {"sha": ""},
          "BASE_DIR": root / "elsewhere", "Path": Path}
    got = _bot_fns({"resolve_review", "rework_flagged_review", "rework_conflict",
                    "rework_finished", "conflict_addendum", "send_back",
                    "review_addendum", "_unsupervised", "MAX_REVIEW_REWORKS",
                    "MAX_CONFLICT_REWORKS", "CONFLICT_STAGES", "land_and_record",
                    "land_if_ready", "landing_enabled", "record_landing",
                    "_is_own_checkout", "start_landing", "approve_task",
                    "land_or_drop", "LANDING_UNDERWAY", "_landing_now",
                    "_landing_guard"}, ns)
    check("the gate, both landing doors and the rework were lifted out of bot.py",
          {"resolve_review", "rework_conflict", "rework_finished", "start_landing",
           "approve_task", "land_or_drop"} <= got, str(sorted(got)))

    def tip(ref):
        return git(repo, "rev-parse", "--verify", "-q", ref).stdout.strip()

    def worked(project, state=T.BLOCKED):
        """An implementor's branch off main, then main moving under it in the
        same lines -- a conflict nothing can resolve mechanically."""
        t = store.create("Make VALUE two", title="Value", project=project,
                         role="implementor", driver="queue", isolate=True,
                         thread="C1:1.0", scope={"cwd": str(repo), "branch": "main"})
        tid = t["id"]
        store.transition(tid, T.RUNNING, "claimed")
        wt = W.create(repo, tid, fetch=False, base="main")
        (wt / "shared.py").write_text(f"VALUE = 2  # {tid}\n")
        git(wt, "commit", "-qam", f"the work of {tid}")
        W.release(wt)
        n = len(store.all())
        (repo / "shared.py").write_text(f"VALUE = {100 + n}  # main moved on\n")
        git(repo, "commit", "-qam", f"main moves ({n})")
        git(repo, "push", "-q", "origin", "main")
        store.update(tid, branch=W.BRANCH_PREFIX + tid, verified=True, blocked_on=["rev"])
        store.transition(tid, T.BLOCKED, "awaiting review")
        if state != T.BLOCKED:
            store.update(tid, blocked_on=[])
            store.transition(tid, state, "parked")
        return tid

    def passing_review(tid):
        said.clear()
        rev = store.create("review it", role="reviewer", parent=tid)
        ns["resolve_review"](rev, "reviewer", '```json\n{"ok": true, "summary": '
                             '"fine", "findings": []}\n```', "C1", "1.0")
        return store.get(tid)

    try:
        # --- (a) a passing review whose landing conflicts ----------------------
        tid = worked("ready")
        main_before, work_before = tip("main"), tip(W.BRANCH_PREFIX + tid)
        t = passing_review(tid)
        check("a passing review whose landing conflicts is sent back, not parked",
              t["state"] == T.QUEUED and t.get("conflict_reworks") == 1,
              f"state {t['state']}, reworks {t.get('conflict_reworks')}")
        landing = (t.get("result") or {}).get("landing") or {}
        check("the refusal on the record names the conflicting files and the base",
              landing.get("stage") == "rebase" and landing.get("conflicts") == ["shared.py"]
              and landing.get("base") == "main" and landing.get("onto") == main_before,
              str(landing))
        check("and the goal it is sent back with carries them",
              "  shared.py" in t["goal"] and "git rebase main" in t["goal"]
              and main_before[:12] in t["goal"] and "CONFLICT" in t["goal"]
              and "run the full test suite" in t["goal"], t["goal"][-900:])
        check("on its own branch, untouched, and main untouched",
              tip(W.BRANCH_PREFIX + tid) == work_before and tip("main") == main_before)
        check("with the gates reset, so it is verified and reviewed again",
              t.get("blocked_on") == [] and t.get("verified") is None)

        # The rerun does not manage it: the second refusal is for a person.
        store.transition(tid, T.RUNNING, "claimed")
        store.update(tid, verified=True, blocked_on=["rev"])
        store.transition(tid, T.BLOCKED, "awaiting review")
        t = passing_review(tid)
        check("a second conflicting landing parks the task for a person",
              t["state"] == T.AWAITING_APPROVAL and t.get("conflict_reworks") == 1
              and not said, f"state {t['state']}, said {said}")

        # --- (b) Approve: done is terminal, so the work moves to a new task -----
        uid = worked("ready", T.AWAITING_APPROVAL)
        old = W.BRANCH_PREFIX + uid
        git(repo, "push", "-q", "origin", old)          # a remote copy, too
        work_tip, main_before = tip(old), tip("main")
        r = ns["approve_task"]({"id": uid, "by": "test"})
        u = store.get(uid)
        sid = ((u.get("result") or {}).get("landing") or {}).get("reworked_by") or ""
        s = store.get(sid) or {}
        check("approving closes the task, as it always has",
              r.get("ok") and u["state"] == T.DONE, str(r))
        check("and a conflicting landing is not stranded: a new task takes it over",
              s.get("state") == T.QUEUED and s.get("role") == "implementor"
              and s.get("driver") == "queue" and s.get("isolate") is True,
              f"reworked_by {sid!r}: {s.get('state')}")
        check("on the same commits, under the one name the guard lets it move",
              tip(W.BRANCH_PREFIX + sid) == work_tip and not tip(old)
              and s.get("branch") == W.BRANCH_PREFIX + sid,
              f"{W.BRANCH_PREFIX + sid} at {tip(W.BRANCH_PREFIX + sid)[:8]}, "
              f"{old} at {tip(old)[:8] or 'gone'}")
        check("told the base, the conflicting file and git's output",
              "  shared.py" in s.get("goal", "") and "git rebase main" in s["goal"]
              and "CONFLICT" in s["goal"] and s["goal"].startswith("Make VALUE two"),
              s.get("goal", "")[-600:])
        check("counted, in its own thread, pointing back at the original",
              s.get("conflict_reworks") == 1 and s.get("thread") == "C1:1.0"
              and s.get("source_ref") == uid and s.get("root") == uid)
        check("and the thread is told", any(sid in text for _, text in said), str(said))
        check("the original leaves the unmerged survey, remote copy and all",
              not [row for row in B.survey([store.get(uid)]) if row["id"] == uid],
              str(B.survey([store.get(uid)])))
        check("and Land on it refuses, naming who has the work",
              sid in (ns["land_or_drop"]("land", {"id": uid}).get("error") or ""))
        check("main untouched throughout", tip("main") == main_before)

        # The new task catches up, as an implementor would, and lands normally.
        here = W.attach(repo, sid, W.BRANCH_PREFIX + sid, label="")
        check("(its first run reattaches the branch execute_task looks for)", bool(here))
        store.transition(sid, T.RUNNING, "claimed")
        git(here, "rebase", "main")
        (here / "shared.py").write_text("VALUE = 2\n")
        git(here, "add", "shared.py")
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "-c", "core.editor=true", "rebase", "--continue"],
                       cwd=str(here), capture_output=True, text=True)
        W.release(here)
        store.update(sid, verified=True, blocked_on=["rev"])
        store.transition(sid, T.BLOCKED, "awaiting review")
        s = passing_review(sid)
        check("once caught up, it lands through the ordinary review path",
              s["state"] == T.DONE and (repo / "shared.py").read_text() == "VALUE = 2\n"
              and (s.get("result") or {}).get("landed") == tip("main"),
              f"{s['state']}: {((s.get('result') or {}).get('landing') or {})}")

        # --- the new task's own second conflict parks; approving it stops ------
        vid = worked("ready", T.AWAITING_APPROVAL)
        ns["approve_task"]({"id": vid, "by": "test"})
        wid = ((store.get(vid).get("result") or {}).get("landing") or {}).get("reworked_by")
        store.transition(wid, T.RUNNING, "claimed")      # and does nothing useful
        store.update(wid, verified=True, blocked_on=["rev"])
        store.transition(wid, T.BLOCKED, "awaiting review")
        w = passing_review(wid)
        check("the taken-over work's second conflict parks it for a person",
              w["state"] == T.AWAITING_APPROVAL, w["state"])
        before = len(store.all())
        ns["approve_task"]({"id": wid, "by": "test"})
        w = store.get(wid)
        check("and approving that is not sent round again",
              w["state"] == T.DONE and len(store.all()) == before
              and not ((w.get("result") or {}).get("landing") or {}).get("reworked_by")
              and tip(W.BRANCH_PREFIX + wid),
              f"{len(store.all()) - before} new task(s)")

        # --- Land on finished work takes the same route -------------------------
        xid = worked("ready", T.AWAITING_APPROVAL)
        store.transition(xid, T.DONE, "closed without landing")
        r = ns["land_or_drop"]("land", {"id": xid})
        yid = ((store.get(xid).get("result") or {}).get("landing") or {}).get("reworked_by")
        check("Land on a done task that conflicts hands it on too",
              r.get("ok") and yid and store.get(yid)["state"] == T.QUEUED
              and tip(W.BRANCH_PREFIX + yid), str(r))

        # --- a branch that cannot be taken over stays where it is ---------------
        real_run = subprocess.run
        def refusing(cmd, *a, **k):
            if cmd[:3] == ["git", "branch", "-m"]:
                return types.SimpleNamespace(returncode=128, stdout="", stderr="no")
            return real_run(cmd, *a, **k)
        ns["subprocess"] = types.SimpleNamespace(run=refusing)
        zid = worked("ready", T.AWAITING_APPROVAL)
        before, ztip = len(store.all()), tip(W.BRANCH_PREFIX + zid)
        try:
            ns["approve_task"]({"id": zid, "by": "test"})
        finally:
            ns["subprocess"] = subprocess
        zl = (store.get(zid).get("result") or {}).get("landing") or {}
        check("a rename git refuses files nothing and takes back the hand-over",
              len(store.all()) == before and tip(W.BRANCH_PREFIX + zid) == ztip
              and zl.get("stage") == "rebase" and not zl.get("reworked_by"), str(zl))

        # --- a project not taking unsupervised work: exactly as before -----------
        mid = worked("manual", T.AWAITING_APPROVAL)
        before, mtip = len(store.all()), tip(W.BRANCH_PREFIX + mid)
        ns["approve_task"]({"id": mid, "by": "test"})
        m = store.get(mid)
        check("on a project that does not take unsupervised work, Approve parks as today",
              m["state"] == T.DONE and len(store.all()) == before
              and tip(W.BRANCH_PREFIX + mid) == mtip
              and (m.get("result") or {}).get("landing", {}).get("stage") == "rebase")
    finally:
        W.ROOT = saved_root

    # --- and nothing else still calls the original stuck -----------------------
    import digest
    vz = (BASE / "visualizer.py").read_text()
    landjs = vz[vz.index("function landing(t)"):vz.index("function threadLink(")]
    probe = (landjs + "\nconst esc = s => String(s);\nprocess.stdout.write(landing("
             "{result: {landing: {eligible: true, landed: false, stage: 'rebase', "
             "branch: 'silkworm/tsk_h', reworked_by: 'tsk_n'}}}))")
    out = subprocess.run(["node", "-e", probe], capture_output=True, text=True)
    check("the dashboard says who has the work rather than 'waiting for you'",
          "handed to tsk_n" in out.stdout and "waiting for you" not in out.stdout,
          out.stdout or out.stderr[:300])
    handed = {"id": "tsk_h", "title": "Handed", "project": "ready", "state": T.DONE,
              "created": time.time(), "result": {"landing": {
                  "eligible": True, "landed": False, "stage": "rebase",
                  "reworked_by": "tsk_n", "at": time.time()}}}
    def refused(rec):
        return (digest.sections([rec], time.time()).get("ready") or {}).get("refused")
    plain = dict(handed, result={"landing": {
        k: v for k, v in handed["result"]["landing"].items() if k != "reworked_by"}})
    check("and the digest does not list it as refused",
          refused(plain) and not refused(handed), str((refused(plain), refused(handed))))

    # --- the reviewer is told staleness is not a finding -----------------------
    sysp = roles.REVIEWER_SYSTEM
    check("the review prompt says being behind the base is not a finding on its own",
          "behind the base branch is not a finding" in sysp
          and "do not ask for a rebase" in sysp.lower()
          and "clashes with what the base now does" in sysp, sysp[:400])


# python-dotenv, which the bot loads .env with, accepts `export NAME=...`.
# bin/silkworm read .env with a bare NAME= prefix match, so the same file set
# a token or port for the bot that the CLI never saw.

def test_cli_reads_exported_env_lines():
    print("\nbin/silkworm reads .env the way the bot does, export lines included")
    cli = _load_cli()
    root = Path(tempfile.mkdtemp())
    (root / ".env").write_text(
        "# VIZ_TOKEN=commented-out\n"
        "export VIZ_TOKEN='tok-1'\n"
        "export  APPROVAL_PORT=9911\n"
        "LEARNINGS_FILE=/tmp/first.json\n"
        "export LEARNINGS_FILE=\"/tmp/last.json\"\n"
        "EXPORTED_THING=x\n")
    cli.REPO = root
    names = ("VIZ_TOKEN", "APPROVAL_PORT", "LEARNINGS_FILE", "VIZ_BIND", "THING")
    saved = {n: os.environ.pop(n) for n in names if n in os.environ}
    try:
        check("an exported VIZ_TOKEN is read, quotes stripped",
              cli.env_setting("VIZ_TOKEN") == "tok-1", repr(cli.env_setting("VIZ_TOKEN")))
        check("an exported APPROVAL_PORT is the bot port",
              cli.bot_port() == "9911", cli.bot_port())
        check("the last assignment wins, as with dotenv",
              cli.learnings_file() == Path("/tmp/last.json"), str(cli.learnings_file()))
        check("env_has sees an exported variable",
              cli.env_has("APPROVAL_PORT"))
        check("a name is matched whole, not as a suffix of another",
              not cli.env_has("THING") and cli.env_file_value("THING") is None)
        check("an unset name still falls back to the default",
              cli.env_setting("VIZ_BIND") == "" and not cli.env_has("VIZ_BIND"))
    finally:
        os.environ.update(saved)



# session_hook.py and visualizer.py each hand-parsed APPROVAL_PORT from .env
# with split("=")[1].strip(), keeping the quotes: APPROVAL_PORT="9911" (which
# dotenv reads as 9911) gave them the port '"9911"', so the hook posted to a
# URL that can't exist and the dashboard lost the bot. All three readers now
# share envfile.value.

def test_every_env_reader_strips_quotes_like_dotenv():
    print("\n.env readers outside the bot agree with dotenv on quoted values")
    import http.server
    import shutil
    import subprocess
    import threading
    import envfile
    import visualizer as V
    root = Path(tempfile.mkdtemp())
    (root / ".env").write_text('export APPROVAL_PORT="9911"\nVIZ_TOKEN=\'tok\'\n')
    check("envfile strips double quotes", envfile.value(root / ".env", "APPROVAL_PORT") == "9911")
    check("envfile strips single quotes", envfile.value(root / ".env", "VIZ_TOKEN") == "tok")
    check("envfile: a missing file is None", envfile.value(root / "nope", "X") is None)
    saved = V.BASE_DIR
    try:
        V.BASE_DIR = root
        check("visualizer's bot port drops the quotes", V._bot_port() == "9911", V._bot_port())
    finally:
        V.BASE_DIR = saved
    cli = _load_cli()
    cli.REPO = root
    saved_env = os.environ.pop("APPROVAL_PORT", None)
    try:
        check("the CLI's bot port drops the quotes", cli.bot_port() == "9911", cli.bot_port())
    finally:
        if saved_env is not None:
            os.environ["APPROVAL_PORT"] = saved_env

    # The hook itself, run as Claude Code runs it: a script next to its .env,
    # under the system python3 (3.9 on macOS), posting to the configured port.
    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    hook_dir = Path(tempfile.mkdtemp())
    for f in ("session_hook.py", "envfile.py"):
        shutil.copy(BASE / f, hook_dir / f)
    (hook_dir / ".env").write_text(f'APPROVAL_PORT="{srv.server_address[1]}"\n')
    py = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
    env = {k: v for k, v in os.environ.items() if k != "SILKWORM_BOT"}
    r = subprocess.run([py, str(hook_dir / "session_hook.py")], env=env,
                       input='{"hook_event_name": "SessionStart", "session_id": "s1"}',
                       capture_output=True, text=True, timeout=10)
    srv.server_close()
    check("the hook runs cleanly under the system python", r.returncode == 0 and not r.stderr,
          r.stderr[-400:])
    check("the hook posts to the quoted port", [p for p, _ in got] == ["/session-event"], repr(got))

    # One reader: nothing outside envfile hand-splits APPROVAL_PORT lines.
    for f in ("session_hook.py", "visualizer.py", "bin/silkworm"):
        src = (BASE / f).read_text()
        check(f"{f} doesn't parse .env lines itself",
              'startswith("APPROVAL_PORT=")' not in src and 'split("=", 1)' not in src)


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
        got = _bot_fns({"land_if_ready", "landing_enabled", "verify_work",
                        "LANDING_UNDERWAY"}, ns)
        check("the landing and the verification were lifted out of bot.py",
              {"land_if_ready", "verify_work"} <= got, str(sorted(got)))
        task = {"id": "tsk_lands", "project": "cadence", "verified": True,
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
        _bot_fns({"land_if_ready", "landing_enabled", "verify_work",
                  "LANDING_UNDERWAY"}, ns)
        r = ns["land_if_ready"]({"id": "tsk_flaky", "project": "cadence",
                                 "verified": True,
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
    ub = bot_functions("upload_outbox", ARTIFACTS_ROOT=arch, artifacts=artifacts, Path=Path,
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
    ns = bot_functions("_sweep_pass", store=st, task_store=types.SimpleNamespace(
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
    got = _bot_fns({"land_if_ready", "landing_enabled"}, ns)
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
    _bot_fns({"task_base"}, ns2)
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
    _bot_fns({"unmerged_survey"}, ns3)
    check("which measures a stale-base task against the current base",
          [r["id"] for r in ns3["unmerged_survey"]([again])] == []
          and [r["id"] for r in B.survey([again])] == [again["id"]])
    board = ast.unparse(next(n for n in tree.body if isinstance(n, ast.Assign)
                             and any(getattr(t, "id", "") == "BOARD" for t in n.targets)))
    check("so does the board's unmerged line",
          "scope_for=project_store.scope_for" in board)


# --- a turn costs its own increase, not the session's running total ---------------

def test_a_resumed_turn_records_its_own_cost():
    """`claude -p --resume` reports the session's running total since 2.1.277.

    Measured 2026-10-06 on haiku: $0.016031 fresh, $0.018464 resumed (that
    turn cost 0.002433), $0.020879 next (0.002415). Each figure was recorded
    as the turn's cost and added to the thread, so every resumed turn
    re-counted all earlier ones: $12,234 recorded in a week against ~$912.
    """
    print("\na resumed turn records its own cost")
    st = tmp_store()
    got = [st.add_cost("C:1", r, "s1") for r in (0.016031, 0.018464, 0.020879)]
    check("a new session's turn costs what it reported",
          abs(got[0] - 0.016031) < 1e-9, str(got))
    check("each resumed turn costs the increase in the session's total",
          abs(got[1] - 0.002433) < 1e-9 and abs(got[2] - 0.002415) < 1e-9, str(got))
    entry = st.get("C:1")
    check("so the thread's total is the session's total, not their sum",
          abs(entry["cost"] - 0.020879) < 1e-9 and entry["turns"] == 3, str(entry["cost"]))
    check("and its per-turn history holds per-turn figures",
          entry["costs"] == [0.016031, 0.002433, 0.002415], str(entry["costs"]))
    check("the session's last reported total is kept on the thread",
          entry.get("session_totals") == {"s1": 0.020879}, str(entry.get("session_totals")))
    # A new session on the same thread (a reset, a lost transcript, a fresh
    # reviewer) is never differenced against the old one.
    check("a different session on the same thread is not differenced",
          st.add_cost("C:1", 0.5, "s2") == 0.5)
    check("nor is a session whose total went down",
          st.add_cost("C:1", 0.001, "s1") == 0.001)
    check("nor a turn with no session id",
          st.add_cost("C:1", 0.25) == 0.25)
    # Durable: the previous total survives a restart.
    again = SessionStore(st._path)
    check("the difference survives a restart",
          abs(again.add_cost("C:1", 0.004, "s1") - 0.003) < 1e-9)
    check("and a session resumed from another thread is measured from where it was",
          abs(again.add_cost("C:other", 0.010, "s1") - 0.006) < 1e-9)
    for i in range(60):
        again.add_cost("C:1", 1.0, f"x{i}")
    check("the remembered sessions are bounded, newest kept",
          len(again.get("C:1")["session_totals"]) == 50
          and "x59" in again.get("C:1")["session_totals"])
    # Reviewers run a fresh session on their parent's thread every round:
    # however many, the session the thread resumes must not be forgotten.
    busy = tmp_store()
    busy.update("C:busy", session_id="main")
    busy.add_cost("C:busy", 1.0, "main")
    for i in range(80):
        busy.add_cost("C:busy", 0.1, f"review{i}")
    check("the thread's own session outlives any number of fresh ones",
          abs(busy.add_cost("C:busy", 1.25, "main") - 0.25) < 1e-9,
          str(sorted(busy.get("C:busy")["session_totals"])[:3]))


def test_execute_task_records_a_runs_own_cost():
    """A task sent back resumes its session; its second run reported the
    session's total, and that total was added to the first run's."""
    print("\na task's rerun records its own cost")
    import threading
    import roles
    import tasks as T
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    sessions = tmp_store()
    root = Path(tempfile.mkdtemp())
    rec = st.create("answer twice", role="assistant", project="p", thread="C1:1.0",
                    driver="queue", isolate=False, scope={"cwd": str(root)})
    reported = iter([1.5, 3.5])
    footers = []

    def run_turn(goal, **kw):
        return types.SimpleNamespace(text="done", cost_usd=next(reported),
                                     duration_ms=1, session_id="s")
    fn = _bot_func("execute_task", tasks=T, task_store=st, store=sessions,
                   roles=roles, Path=Path, run_turn=run_turn,
                   review_branch=lambda t: "", worktrees=__import__("worktrees"),
                   OUTBOX_ROOT=root / "outbox", SILKWORM_BIN="/x/silkworm",
                   permission_args=lambda: [], log=logging.getLogger("test"),
                   task_thread=lambda t: ("C1", "1.0"), task_key=lambda t: "C1:1.0",
                   task_state=lambda tid, state, detail="": st.transition(tid, state, detail),
                   _thread_lock=lambda key: threading.Lock(),
                   repo_guard=lambda *a, **k: contextlib.nullcontext(),
                   render_block=lambda _: "", chunk=lambda text: [text],
                   to_mrkdwn=lambda text: text, resolve_review=lambda *a, **k: False,
                   upload_outbox=lambda *a, **k: [], RUNNING={}, RUNNING_TASKS={},
                   COST_NOTE="(list)", ClaudeStopped=ClaudeStopped, ClaudeError=ClaudeError)
    progress = fn.__globals__["ProgressMessage"]
    for _ in range(2):
        st.transition(rec["id"], T.RUNNING, "claimed")
        fn(st.get(rec["id"]))
        if st.get(rec["id"])["state"] != T.DONE:
            break
        with st._lock:               # sent back: done is terminal, so by hand
            st._data[rec["id"]]["state"] = T.QUEUED
    for c in progress.return_value.finalize.call_args_list:
        footers.append(c.args[0])
    got = (st.get(rec["id"]).get("result") or {})
    check("the second run adds its increase, not the session total",
          got.get("cost") == 3.5 and [r[1] for r in got.get("cost_runs") or []] == [1.5, 2.0],
          str(got))
    check("and the reported total is kept beside it",
          got.get("cost_reported_total") == 3.5, str(got))
    check("the footer shows the run's own cost",
          any("$2.0000 · thread total $3.50 (list)" in f for f in footers), str(footers))


def test_every_reported_cost_goes_through_the_session_difference():
    """`result.cost_usd` is a session total; anywhere it is used as a turn's
    cost re-counts every earlier turn. It may only be handed to add_cost --
    which differences it -- or kept as the raw `cost_reported_total`."""
    print("\nevery reported cost is differenced")
    tree = ast.parse((BASE / "bot.py").read_text())
    ok_ids, uses = set(), []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "store.add_cost":
            if len(n.args) >= 3 and ast.unparse(n.args[2]) == "result.session_id":
                ok_ids.add(id(n.args[1]))
        if isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and k.value == "cost_reported_total":
                    ok_ids.add(id(v))
        if isinstance(n, ast.Attribute) and n.attr == "cost_usd":
            uses.append(n)
    stray = [n.lineno for n in uses if id(n) not in ok_ids]
    check("a reported cost is only differenced or kept raw", uses and not stray,
          f"used directly at lines {stray}")
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == "store.add_cost"]
    check("every turn's add_cost names its session",
          calls and all(len(c.args) >= 3 for c in calls), str([ast.unparse(c) for c in calls]))
    footers = [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)
               and "thread total" in ast.unparse(n)]
    check("every footer says the figure is a list-price equivalent",
          len(footers) == 2 and all("COST_NOTE" in ast.unparse(f) for f in footers),
          str(len(footers)))


def _cost_fixture(cut):
    """A thread with a long conversation across the CLI change, a reworked
    implementor, a reviewer run twice, and a turn recorded by the fixed code."""
    def rec(tid, sid, at, cost, role="assistant", runs=None, **extra):
        r = {"id": tid, "session_id": sid, "thread": "C:1", "role": role,
             "state": "done", "created": at - 100, "updated": at,
             "events": [{"kind": "running", "at": at - 50}, {"kind": "done", "at": at}],
             "result": {"text": "x", "cost": cost}}
        if runs:
            r["result"]["cost_runs"] = runs
        r["result"].update(extra)
        return r
    recs = [
        rec("t1", "conv", cut - 100, 2.0),      # before the change: own costs
        rec("t2", "conv", cut - 50, 3.0),
        rec("t3", "conv", cut + 100, 1.0),      # after: running totals
        rec("t4", "conv", cut + 200, 1.5),
        rec("t5", "conv", cut + 300, 2.5),
        rec("t6", "conv", cut + 400, 0.4),      # total went down: reset
        rec("t7", "other", cut + 250, 0.7),     # a new session, same thread
        rec("i1", "impl", cut + 600, 4.369, role="implementor",
            runs=[[cut + 500, 1.7], [cut + 600, 2.669]]),
        rec("r1", "rev2", cut + 600, 1.1, role="reviewer",
            runs=[[cut + 500, 0.5], [cut + 600, 0.6]]),
        # Its own 0.5, the session then at 0.9: differencing it again would
        # make it 0.1.
        rec("n1", "conv", cut + 900, 0.5, cost_reported_total=0.9),
    ]
    return recs


def test_recorded_costs_are_corrected_once():
    """The records written while resumed turns reported running totals are
    corrected at startup: each post-change turn becomes its increase over the
    session's previous post-change turn. Turns before the change reported
    their own cost and are left alone; originals are kept."""
    print("\nrecorded costs are corrected once")
    import copy
    import tasks as T
    import turncost
    cut = 1_000_000.0
    d = Path(tempfile.mkdtemp())
    raw = {r["id"]: r for r in _cost_fixture(cut)}
    jsonstore = __import__("jsonstore")
    jsonstore.save(d / "t.json", raw)
    ts = T.TaskStore(d / "t.json")
    ss = SessionStore(d / "s.json")
    # The thread's history has one figure per turn, so a reworked task's
    # runs are there separately.
    recorded = [usd for r in sorted(raw.values(), key=lambda r: r["updated"])
                for _, usd in (r["result"].get("cost_runs") or [(0, r["result"]["cost"])])]
    ss.update("C:1", title="a thread", turns=len(recorded))
    with ss._lock:
        ss._data["C:1"].update(cost=round(sum(recorded), 6) + 10.0,  # +10 before tasks existed
                               costs=[round(c, 6) for c in recorded],
                               # n1 was recorded by the fixed code, which kept
                               # its session's total: newer than any in the records.
                               session_totals={"conv": 0.9})
        ss._save()
    before = copy.deepcopy(ts.all())
    s = turncost.correct(ts, ss, cutoff=cut)
    get = lambda tid: ts.get(tid)["result"]
    check("turns before the change keep their own cost",
          get("t1")["cost"] == 2.0 and get("t2")["cost"] == 3.0
          and "cost_uncorrected" not in get("t1"), str(get("t1")))
    check("the first turn after it keeps what it reported",
          get("t3")["cost"] == 1.0, str(get("t3")))
    check("later turns become the increase over the previous one",
          get("t4")["cost"] == 0.5 and get("t5")["cost"] == 1.0,
          str([get(t)["cost"] for t in ("t4", "t5")]))
    check("a total that went down is not differenced",
          get("t6")["cost"] == 0.4, str(get("t6")))
    check("nor is another session in the same thread",
          get("t7")["cost"] == 0.7, str(get("t7")))
    check("a reworked task's runs are corrected run by run",
          get("i1")["cost_runs"] == [[cut + 500, 1.7], [cut + 600, 0.969]]
          and get("i1")["cost"] == 2.669, str(get("i1")))
    check("a fresh role's earlier run was another session and is left alone",
          get("r1")["cost"] == 1.1 and "cost_uncorrected" not in get("r1"), str(get("r1")))
    check("a turn the fixed code recorded is not touched",
          get("n1") == before["n1"]["result"], str(get("n1")))
    check("originals are kept beside the corrections",
          get("t5")["cost_uncorrected"] == 2.5
          and get("i1")["cost_runs_uncorrected"] == [[cut + 500, 1.7], [cut + 600, 2.669]]
          and get("i1")["cost_uncorrected"] == 4.369, str(get("i1")))
    check("correcting a figure is not activity",
          all(ts.get(t)["updated"] == before[t]["updated"] for t in before))
    thread = ss.get("C:1")
    task_sum = sum(r["result"]["cost"] for r in ts.all().values())
    check("the thread's total is recomputed from its corrected turns",
          abs(thread["cost"] - (task_sum + 10.0)) < 1e-6, f"{thread['cost']} vs {task_sum + 10}")
    check("its per-turn history carries the corrected figures",
          thread["costs"] == [2.0, 3.0, 1.0, 0.5, 0.7, 1.0, 0.4, 1.7, 0.969, 0.5, 0.6, 0.5],
          str(thread["costs"]))
    check("the summary logs the before and after",
          s["tasks_changed"] == 3 and s["tasks_before"] > s["tasks_after"], str(s))
    check("and each session's last total is recorded for the next turn, "
          "never over a newer one",
          thread["session_totals"] == {"conv": 0.9, "impl": 2.669, "rev2": 0.6, "other": 0.7},
          str(thread.get("session_totals")))
    # The next resumed turn of the conversation is measured from there.
    check("so the next resumed turn is its own increase",
          abs(ss.add_cost("C:1", 1.1, "conv") - 0.2) < 1e-9)
    # Run again: nothing moves.
    snap_t, snap_s = copy.deepcopy(ts.all()), copy.deepcopy(ss.all())
    again = turncost.correct(ts, ss, cutoff=cut)
    check("a second correction changes nothing", again["tasks_changed"] == 0
          and ts.all() == snap_t and ss.all() == snap_s, str(again))
    # A corrected turn sent back for another run: the rerun rewrites its
    # result, and what the correction kept must survive -- or a later pass
    # loses that turn from its session and re-measures the next one.
    import costs as C
    rerun = C.add_run(ts.get("t4"), 0.2, now=cut + 950)
    ts.update("t4", result={"text": "again", **rerun, "cost_reported_total": 2.7})
    check("a rerun keeps what the correction preserved",
          get("t4")["cost_uncorrected"] == 1.5 and get("t4")["cost"] == 0.7, str(get("t4")))
    snap_t, snap_s = copy.deepcopy(ts.all()), copy.deepcopy(ss.all())
    third = turncost.correct(ts, ss, cutoff=cut)
    check("and correcting again after it still changes nothing",
          third["tasks_changed"] == 0 and ts.all() == snap_t and ss.all() == snap_s,
          f"{third} t5={get('t5')}")
    ts.update("t4", result=dict(get("t4"), cost=0.5, cost_runs=None))
    # Costs read the same everywhere: the week's total is the tasks'.
    import costs
    recs = [dict(r, id=k) for k, r in ts.all().items()]
    week = costs.by_project(recs, cut + 1000, days=1000 / 86400, unfiled="all")
    post = sum(get(t)["cost"] for t in ("t3", "t4", "t5", "t6", "t7", "n1")) + 0.969 + 1.7 + 0.5 + 0.6
    check("the week's spend reads the corrected figures",
          abs(week["all"]["usd"] - post) < 1e-6, f"{week} vs {post}")


def test_a_correction_interrupted_part_way_finishes_on_retry():
    """tasks.json is saved before the threads are; a failure between them must
    leave the retry with the threads still to move, not with nothing left."""
    print("\na correction interrupted part-way finishes on retry")
    import tasks as T
    import turncost
    cut = 1_000_000.0
    d = Path(tempfile.mkdtemp())
    raw = {r["id"]: r for r in _cost_fixture(cut)}
    __import__("jsonstore").save(d / "t.json", raw)
    ts = T.TaskStore(d / "t.json")
    ss = SessionStore(d / "s.json")
    original = round(sum(r["result"]["cost"] for r in raw.values()), 6)
    ss.update("C:1", cost=original)
    real = ss.correct_costs
    ss.correct_costs = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    try:
        turncost.correct(ts, ss, cutoff=cut)
    except OSError:
        pass
    ss.correct_costs = real
    check("the tasks were corrected and the thread was not",
          ts.get("t5")["result"]["cost"] == 1.0 and ss.get("C:1")["cost"] == original)
    turncost.correct(ts, ss, cutoff=cut)
    total = round(sum(r["result"]["cost"] for r in ts.all().values()), 6)
    check("the retry moves the thread to agree with its tasks",
          abs(ss.get("C:1")["cost"] - total) < 1e-6, f"{ss.get('C:1')['cost']} vs {total}")
    turncost.correct(ts, ss, cutoff=cut)
    check("and once there it stays", abs(ss.get("C:1")["cost"] - total) < 1e-6)


def test_the_correction_runs_once_at_startup():
    """Through the stores, before anything records a turn, and marked."""
    print("\nthe cost correction runs once at startup")
    import tasks as T
    import turncost
    cut = turncost.CUTOFF
    d = Path(tempfile.mkdtemp())
    __import__("jsonstore").save(d / "t.json", {r["id"]: r for r in _cost_fixture(cut)})
    ts = T.TaskStore(d / "t.json")
    ss = SessionStore(d / "s.json")
    ns = {"task_store": ts, "store": ss, "turncost": turncost,
          "jsonstore": __import__("jsonstore"), "COST_CORRECTION_FILE": d / "marker.json",
          "log": logging.getLogger("test"), "time": time, "Path": Path}
    _bot_fns({"correct_costs_once"}, ns)
    first = ns["correct_costs_once"]()
    check("the first start corrects", first and first["tasks_changed"] == 3, str(first))
    check("and marks that it has", (d / "marker.json").exists())
    ts.update("t5", result=dict(ts.get("t5")["result"], cost=99.0))
    check("a later start does not run it again",
          ns["correct_costs_once"]() is None and ts.get("t5")["result"]["cost"] == 99.0)
    tree = ast.parse((BASE / "bot.py").read_text())
    main = next(n for n in tree.body if isinstance(n, ast.If)
                and "__main__" in ast.unparse(n.test))
    first_call = ast.unparse(main.body[0])
    check("it is the first thing the bot does", first_call == "correct_costs_once()", first_call)
    broken = dict(ns, turncost=types.SimpleNamespace(
        correct=lambda *a: (_ for _ in ()).throw(OSError("disk")), CUTOFF=0))
    broken["COST_CORRECTION_FILE"] = d / "other.json"
    broken["log"] = logging.getLogger("test.expected-failure")
    broken["log"].disabled = True
    _bot_fns({"correct_costs_once"}, broken)
    check("a failure does not stop the bot, and is retried next start",
          broken["correct_costs_once"]() is None and not (d / "other.json").exists())


def test_a_failed_correction_still_seeds_session_totals():
    """If the correction fails at startup the bot runs on; each session's last
    total must still be recorded, or every thread's first resumed turn is
    charged that session's whole running total until the next restart."""
    print("\na failed correction still seeds session totals")
    import tasks as T
    import turncost
    cut = turncost.CUTOFF
    d = Path(tempfile.mkdtemp())
    raw = {r["id"]: r for r in _cost_fixture(cut)}
    __import__("jsonstore").save(d / "t.json", raw)
    ts = T.TaskStore(d / "t.json")
    ss = SessionStore(d / "s.json")
    ss.update("C:1", title="a thread", cost=5.0)
    with ss._lock:
        ss._data["C:1"]["session_totals"] = {"conv": 0.9}   # newer, from the fixed code
        ss._save()
    check("the seeds are the ones the correction plans",
          turncost.seeds(ts.all(), cut)
          == {k: th["seeds"] for k, th in turncost.plan(ts.all(), cut)["threads"].items()},
          str(turncost.seeds(ts.all(), cut)))
    failing = types.SimpleNamespace(
        correct=lambda *a: (_ for _ in ()).throw(OSError("disk")),
        seed=turncost.seed, CUTOFF=cut)
    ns = {"task_store": ts, "store": ss, "turncost": failing,
          "jsonstore": __import__("jsonstore"), "COST_CORRECTION_FILE": d / "marker.json",
          "log": logging.getLogger("test.expected-failure"), "time": time, "Path": Path}
    ns["log"].disabled = True
    _bot_fns({"correct_costs_once"}, ns)
    check("the failure is not marked as done",
          ns["correct_costs_once"]() is None and not (d / "marker.json").exists())
    thread = ss.get("C:1")
    check("each session's last total is recorded, never over a newer one",
          thread["session_totals"] == {"conv": 0.9, "impl": 2.669, "rev2": 0.6, "other": 0.7},
          str(thread.get("session_totals")))
    check("no cost was moved",
          thread["cost"] == 5.0 and not thread.get("cost_corrections"), str(thread))
    check("so the next resumed turn is its own increase",
          abs(ss.add_cost("C:1", 2.9, "impl") - 0.231) < 1e-9)
    bad = dict(raw, junk={"id": "junk", "session_id": "x", "thread": "C:1",
                          "result": {"cost": 1.0, "cost_runs": [[1, 2.0]],
                                     "cost_uncorrected": 1.0, "cost_runs_uncorrected": 7}})
    try:
        turncost.plan(bad, cut)
        planned_raises = False
    except Exception:
        planned_raises = True
    check("(the bad record really is one the correction cannot read)", planned_raises)
    check("one unreadable record does not cost the others their seeds",
          "conv" in turncost.seeds(bad, cut).get("C:1", {}))


def test_costs_say_they_are_list_price():
    """The bot runs on a subscription token: its costs are what the API would
    charge at list price, not a bill, and every place showing one says so."""
    print("\ncosts say they are list price")
    import costs
    import digest
    line = costs.week_line({"p": {"usd": 3.0, "complete": True, "tasks": 1}})
    check("the board's week says so", "API list price" in line, line)
    rec = {"id": "a", "project": "p", "state": "done", "created": 100, "updated": 200,
           "result": {"cost": 1.0}}
    text = digest.render([rec], 300)
    check("the digest says so", "API list price" in text, text)
    src = (BASE / "digest.py").read_text()
    check("on a busy day as well as a quiet one",
          src.count("{costs.NOTE}") == 2, str(src.count("{costs.NOTE}")))
    viz = (BASE / "visualizer.py").read_text()
    check("the dashboard's total and task costs say so",
          "total spend (API list price)" in viz
          and "(API list-price equivalent, not billed)" in viz)


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
    ns = bot_functions("new_project_from_thread", "github_for_thread_project",
                       "handle_projects",
                       store=st, project_store=ps, workspaces=fake_ws, GITHUB_OWNER="me",
                       repos=__import__("repos"), Path=Path, projects=projects,
                       install_git_guards=lambda *a: None, RUNNING={"C:busy": object()})
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
    ns = bot_functions("file_existing_by_directory", "file_by_directory",
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


# --- per-project boards and the projects overview ------------------------------
# The task panel answers "what needs me"; nothing answered "where is this
# project". board.py builds both views from the records, with the slow inputs
# (branch survey, release plan) cached; handle_tasks serves them; the page
# renders them. Driven here against real stores, real git and the page's own
# javascript under node.

def _board_fixture():
    """A TaskStore and ProjectStore in a temp dir, one task in every column."""
    import tasks as T
    import projects as P
    d = Path(tempfile.mkdtemp())
    ts = T.TaskStore(d / "tasks.json")
    ps = P.ProjectStore(d / "projects.json")
    ps.ensure("Alpha", test_cmd="./bin/test", auto_merge=True, scope={"cwd": str(d / "alpha")})
    ps.ensure("Beta")
    ps.ensure("Gone")
    ps.set_archived("gone", True)
    now = time.time()
    mk = lambda goal, **kw: ts.create(goal, driver="queue", **kw)
    ids = {}
    ids["proposed"] = mk("Propose a thing\n\nwith a long case about <b>it</b>",
                         project="alpha", state=T.PROPOSED, role="implementor")["id"]
    ids["queued"] = mk("Queue a thing", project="alpha", state=T.QUEUED,
                       role="implementor")["id"]
    ids["running"] = mk("Run a thing", project="alpha", state=T.RUNNING,
                        role="implementor", thread="C1:1.2")["id"]
    impl = mk("Under review", project="alpha", state=T.BLOCKED, role="implementor")
    rev = mk("Review: Under review", state=T.RUNNING, role="reviewer", parent=impl["id"])
    ts.update(impl["id"], blocked_on=[rev["id"]])
    ts.update(rev["id"], result={"cost": 1.5})
    ids["review"], ids["reviewer"] = impl["id"], rev["id"]
    ids["waiting"] = mk("Quota wait", project="alpha", state=T.BLOCKED,
                        role="implementor")["id"]
    ids["awaiting"] = mk("Awaits you", project="alpha", state=T.AWAITING_APPROVAL,
                         role="implementor")["id"]
    ts.update(ids["awaiting"], result={"cost": 2.0, "cost_runs": [[now, 2.0]],
              "review": {"ok": False, "summary": "two problems",
                         "findings": ["first finding", "second finding"],
                         "followups": ["a followup"]}})
    ids["failed"] = mk("Broke", project="alpha", state=T.FAILED, role="implementor")
    ts.update(ids["failed"]["id"], events=[{"at": now, "kind": "failed",
                                            "detail": "claude exited 1: NO WORKTREE"}])
    ids["failed"] = ids["failed"]["id"]
    ids["landed"] = mk("Landed it", project="alpha", state=T.DONE, role="implementor")["id"]
    ts.update(ids["landed"], result={"landed": "abcdef1234567890",
              "landing": {"landed": True, "stage": "done", "head": "abcdef1234567890",
                          "at": now - 3600}})
    ids["refused"] = mk("Refused", project="alpha", state=T.DONE, role="implementor")["id"]
    ts.update(ids["refused"], result={"landing": {"eligible": True, "landed": False,
              "stage": "rebase", "branch": "silkworm/" + ids["refused"],
              "detail": "CONFLICT in x.py", "at": now - 7200}})
    ids["stranded"] = mk("Stranded", project="alpha", state=T.DONE, role="implementor")["id"]
    ids["old"] = mk("Old work", project="alpha", state=T.DONE, role="implementor")["id"]
    ts.update(ids["old"], result={"landed": "0123456789",
              "landing": {"landed": True, "head": "0123456789", "at": now - 20 * 86400}})
    ids["cancelled"] = mk("Dropped", project="alpha", state=T.CANCELLED)["id"]
    ids["beta"] = mk("Beta work", project="beta", state=T.QUEUED, role="assistant")["id"]
    ids["unfiled"] = mk("Nobody's work", state=T.QUEUED, role="implementor")["id"]
    survey = [{"id": ids["stranded"], "project": "alpha", "branch": "silkworm/" + ids["stranded"],
               "commits": 3, "local": True, "remote": ""},
              {"id": ids["refused"], "project": "alpha", "branch": "silkworm/" + ids["refused"],
               "commits": None, "local": True, "remote": ""}]
    return ts, ps, ids, survey, d


def test_board_columns():
    import board as B
    print("\nthe per-project board")
    ts, ps, ids, survey, _ = _board_fixture()
    recs = list(ts.all().values())
    b = B.board(recs, "alpha", unmerged_rows=survey)
    where = {c["id"]: col for col, cards in b["columns"].items() for c in cards}

    check("the columns are the five asked for, in order",
          tuple(b["columns"]) == ("backlog", "running", "review", "needs", "done"))
    check("proposed and queued are backlog",
          where.get(ids["proposed"]) == "backlog" and where.get(ids["queued"]) == "backlog")
    # In the order the runner will take it: the queue first (oldest first
    # while nothing is pinned), then what waits to go round again, then the
    # proposals nothing runs until they are accepted.
    check("backlog is the queue first, then what waits, then proposals",
          [c["id"] for c in b["columns"]["backlog"]]
          == [ids["queued"], ids["waiting"], ids["proposed"]],
          [c["id"] for c in b["columns"]["backlog"]])
    check("running is running", where.get(ids["running"]) == "running")
    check("a task blocked on its reviewer is in review", where.get(ids["review"]) == "review")
    check("one blocked on anything else waits in the backlog",
          where.get(ids["waiting"]) == "backlog")
    # blocked_on outlives the review it named; a finished reviewer is not a wait.
    old = ts.create("Old review", state="done", role="reviewer", parent=ids["waiting"])
    ts.update(ids["waiting"], blocked_on=[old["id"]])
    b2 = B.board(list(ts.all().values()), "alpha", unmerged_rows=survey)
    check("blocked on a reviewer that already finished is backlog, not in review",
          ids["waiting"] in {c["id"] for c in b2["columns"]["backlog"]})
    check("the reviewer itself is never a card", ids["reviewer"] not in where)
    check("awaiting approval and failed need you",
          where.get(ids["awaiting"]) == "needs" and where.get(ids["failed"]) == "needs")
    check("recently finished work is done",
          all(where.get(ids[k]) == "done" for k in ("landed", "refused", "stranded")))
    check("work finished over fourteen days ago has left the board", ids["old"] not in where)
    check("cancelled work is not on it", ids["cancelled"] not in where)
    check("another project's work is not on it",
          ids["beta"] not in where and ids["unfiled"] not in where)
    check("done is newest first",
          [c["id"] for c in b["columns"]["done"]][:2] == [ids["stranded"], ids["landed"]]
          or [c["id"] for c in b["columns"]["done"]][0] == ids["stranded"],
          [c["title"] for c in b["columns"]["done"]])

    cards = {c["id"]: c for cs in b["columns"].values() for c in cs}
    rv = cards[ids["awaiting"]]["review"]
    check("a card carries its review verdict, summarised",
          rv == {"ok": False, "summary": "two problems", "findings": 2, "followups": 1}, rv)
    check("and its cost, its reviews' included",
          cards[ids["review"]]["cost_total"] == {"usd": 1.5, "complete": False},
          cards[ids["review"]]["cost_total"])
    check("a landed card names its commit",
          cards[ids["landed"]]["landing"]["head"].startswith("abcdef12"))
    check("a refused one its stage",
          cards[ids["refused"]]["landing"]["stage"] == "rebase")
    check("stranded work carries what the cached survey said, unknown kept unknown",
          cards[ids["stranded"]]["unmerged"]["commits"] == 3
          and cards[ids["refused"]]["unmerged"]["commits"] is None)
    check("a failed card says why", "NO WORKTREE" in cards[ids["failed"]]["why"])
    check("a card is small: the goal is a snippet",
          all(len(c["goal"]) <= B.GOAL_SNIPPET for c in cards.values()))

    # Filters
    only = lambda **kw: {c["id"] for cs in B.board(recs, **kw)["columns"].values() for c in cs}
    check("all projects is every project's work, unfiled included",
          {ids["beta"], ids["unfiled"], ids["queued"]} <= only(project=""))
    check("the unfiled lane is work under no project",
          only(project=B.UNFILED) == {ids["unfiled"]}, only(project=B.UNFILED))
    check("filter by role", only(project="", role="assistant") == {ids["beta"]})
    check("filter by state", only(project="alpha", state="failed") == {ids["failed"]})
    check("search reads the goal, not only the title",
          only(project="", q="LONG CASE") == {ids["proposed"]})
    check("search reads the title", ids["running"] in only(project="", q="run a"))
    check("the role filter offers the roles on the board",
          B.board(recs, "")["roles"] == ["assistant", "implementor"],
          B.board(recs, "")["roles"])

    d = B.detail(ts.get(ids["awaiting"]), recs)
    check("the detail has the whole goal and the full review",
          d["goal"] == "Awaits you" and d["result"]["review"]["findings"][1] == "second finding")
    d = B.detail(ts.get(ids["running"]), recs)
    check("and a thread link built by slacklinks",
          d["thread_link"] == __import__("slacklinks").for_key("C1:1.2"))
    d = B.detail(ts.get(ids["review"]), recs)
    check("and names its reviewers", [r["id"] for r in d["reviews"]] == [ids["reviewer"]])


def _release_repo():
    d = Path(tempfile.mkdtemp())
    g = lambda *a: subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True,
                                  check=True)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (d / ".silkworm").mkdir()
    (d / ".silkworm" / "release.toml").write_text(
        '[targets.backend]\nship = "command"\npaths = ["db/"]\n'
        'commands = ["touch SHIPPED"]\npreview = ["touch PREVIEWED"]\n\n'
        '[targets.app]\nship = "tag"\npaths = ["app/"]\nafter = ["backend"]\n')
    (d / "db").mkdir(); (d / "db" / "a.sql").write_text("x")
    g("add", "-A"); g("commit", "-qm", "db")
    g("tag", "backend/v1.0.0")
    (d / "app").mkdir(); (d / "app" / "a.swift").write_text("y")
    g("add", "-A"); g("commit", "-qm", "app one")
    (d / "app" / "b.swift").write_text("z")
    g("add", "-A"); g("commit", "-qm", "app two")
    return d


def test_projects_overview():
    import board as B
    print("\nthe projects overview")
    ts, ps, ids, survey, _ = _board_fixture()
    repo = _release_repo()
    rel = B.release_status(repo)
    ov = B.overview(ps.all(include_archived=False), ts.all().values(), unmerged_rows=survey,
                    releases_by_slug={"alpha": rel})
    rows = {p["slug"]: p for p in ov["projects"]}
    check("one card per active project, archived ones left out",
          set(rows) == {"alpha", "beta"}, sorted(rows))
    a = rows["alpha"]
    check("counts by state, reviewers not counted as work",
          a["counts"].get("running") == 1 and a["counts"].get("done") == 4
          and a["counts"].get("blocked") == 2, a["counts"])
    check("needs is what is waiting on you", a["needs"] == 2, a["needs"])
    bd = B.board(ts.all().values(), "alpha", unmerged_rows=survey)
    check("and agrees with the board's Needs you column",
          a["needs"] == bd["counts"]["needs"], (a["needs"], bd["counts"]))
    check("what is running now", [r["id"] for r in a["running"]] == [ids["running"]])
    check("the last landing is the newest one",
          a["last_landed"]["id"] == ids["landed"] and a["last_landed"]["head"] == "abcdef12")
    check("unmerged branches from the survey, an uncountable one flagged",
          a["unmerged"] == {"branches": 2, "commits": 3, "unknown": 1}, a["unmerged"])
    check("readiness: test command, auto-merge, publish",
          a["readiness"]["test_cmd"] == "./bin/test" and a["readiness"]["auto_merge"]
          and not a["readiness"]["publish"] and a["readiness"]["unready"] == "")
    check("a project that is not ready says why",
          "test command" in rows["beta"]["readiness"]["unready"])
    check("this week's cost, the reviewer counted under its parent's project",
          a["cost_week"] and a["cost_week"]["usd"] == 3.5, a["cost_week"])
    check("costs are labelled as list-price equivalents", "list price" in ov["note"])
    check("the unfiled lane has its own card",
          ov["unfiled"]["slug"] == B.UNFILED and ov["unfiled"]["counts"].get("queued") == 1,
          ov["unfiled"])

    tg = {t["target"]: t for t in (rel or {}).get("targets", [])}
    check("release: per target, in dependency order",
          [t["target"] for t in rel["targets"]] == ["backend", "app"], rel)
    check("what is pending, and the version it would become",
          tg["app"]["commits"] == 2 and tg["app"]["version"]
          and tg["backend"]["commits"] == 0 and tg["backend"]["version"] is None, tg)
    check("and reading it ran no preview and shipped nothing",
          not (repo / "PREVIEWED").exists() and not (repo / "SHIPPED").exists())
    check("a project without release.toml has no release row",
          B.release_status(Path(tempfile.mkdtemp())) is None)
    (repo / ".silkworm" / "release.toml").write_text("[targets.bad]\nship = 'boat'\n")
    check("a broken release.toml is reported, not raised",
          "ship" in (B.release_status(repo) or {}).get("error", ""))

    src = (BASE / "board.py").read_text()
    calls = {f"{n.func.value.id}.{n.func.attr}" for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name)}
    check("board.py never calls a releases function that runs anything",
          not calls & {"releases.preview", "releases.release", "releases.ready",
                       "releases._run"}, sorted(calls))


def test_board_cache():
    import board as B
    print("\nthe board's cache")
    clock = [1000.0]
    c = B.TTLCache(ttl=60, clock=lambda: clock[0])
    n = []
    compute = lambda: n.append(1) or len(n)
    c.get("k", compute); c.get("k", compute)
    check("a second read inside the window asks nothing", len(n) == 1)
    clock[0] += 61
    c.get("k", compute)
    check("past it, it asks again", len(n) == 2)
    def boom():
        raise RuntimeError("git timed out")
    try:
        c.get("j", boom)
    except RuntimeError:
        pass
    check("a failure is not cached", c.get("j", compute) == 3)
    for i in range(5):
        clock[0] += 61
        c.get(("survey", i), compute)
    check("expired entries are dropped, so changing keys cannot grow it",
          len(c._data) == 1, len(c._data))

    recs = [{"id": "a", "state": "done", "updated": 1}, {"id": "b", "state": "running",
                                                          "updated": 1}]
    k1 = B.survey_key(recs)
    recs[1]["updated"] = 2
    check("a running task rewriting itself does not invalidate the survey",
          B.survey_key(recs) == k1)
    recs[0]["updated"] = 2
    check("a finished one changing (landed, dropped) does", B.survey_key(recs) != k1)


def test_board_routes_through_handle_tasks():
    import board as B, tasks as T, costs, branches, worktrees, holding, roles
    print("\nthe board's routes, and its actions, through handle_tasks")
    ts, ps, ids, survey, _ = _board_fixture()
    surveyed = []
    ns = {"task_store": ts, "project_store": ps, "board": B, "tasks": T, "costs": costs,
          "branches": branches, "worktrees": worktrees, "holding": holding, "roles": roles,
          "projects": __import__("projects"), "scoping": __import__("scoping"),
          "unmerged_survey": lambda recs: surveyed.append(1) or survey,
          "tell_thread": lambda *a: None, "stop_task": lambda tid: True,
          "log": logging.getLogger("test"), "time": time}
    found = _bot_fns({"handle_tasks", "board_survey", "board_release", "_board_survey",
                      "_board_releases", "_with_costs", "_spend"}, ns)
    check("the routes and their caches are in bot.py",
          {"board_survey", "board_release", "_board_survey", "_board_releases"} <= found)
    ht = ns["handle_tasks"]

    r = ht({"action": "board", "project": "alpha"})
    where = {c["id"]: col for col, cs in r["columns"].items() for c in cs}
    check("board answers with the project's columns",
          r["ok"] and where.get(ids["awaiting"]) == "needs" and where.get(ids["review"]) == "review")
    ht({"action": "board", "project": "alpha", "q": "thing"})
    check("polling does not re-survey git while nothing finished", len(surveyed) == 1,
          len(surveyed))
    r = ht({"action": "board", "project": "alpha", "role": "implementor", "state": "queued"})
    check("filters reach the board",
          [c["id"] for cs in r["columns"].values() for c in cs] == [ids["queued"]])
    r = ht({"action": "board", "project": B.UNFILED})
    check("and the unfiled lane",
          [c["id"] for cs in r["columns"].values() for c in cs] == [ids["unfiled"]])

    ov = ht({"action": "overview"})
    check("overview answers one card per active project",
          ov["ok"] and {p["slug"] for p in ov["projects"]} == {"alpha", "beta"})
    check("a project whose directory is not a repository has no release row",
          all(p["release"] is None for p in ov["projects"]))
    d = ht({"action": "task", "id": ids["awaiting"]})
    check("task answers the full record", d["ok"] and d["task"]["goal"] == "Awaits you")
    check("and refuses an unknown id", not ht({"action": "task", "id": "tsk_nope"})["ok"])
    n = len(surveyed)
    sd = ht({"action": "task", "id": ids["stranded"]})["task"]
    sc = next(c for cs in ht({"action": "board", "project": "alpha"})["columns"].values()
              for c in cs if c["id"] == ids["stranded"])
    check("a stranded task's detail carries the same survey row as its card",
          sd.get("unmerged") and sd["unmerged"] == sc["unmerged"], sd.get("unmerged"))
    check("and the detail reads the cached survey rather than asking git again",
          len(surveyed) == n, len(surveyed))
    check("a task with no unmerged branch has none in its detail",
          d["task"].get("unmerged") is None)

    # The actions on a card are handle_tasks' own; drive them and watch the
    # board follow.
    check("retry moves a failed card out of Needs you",
          ht({"action": "retry", "id": ids["failed"]})["ok"])
    check("accept keeps a proposal in the backlog, now queued",
          ht({"action": "accept", "id": ids["proposed"]})["ok"])
    check("cancel takes a queued card off", ht({"action": "cancel", "id": ids["queued"]})["ok"])
    r = ht({"action": "board", "project": "alpha"})
    where = {c["id"]: (col, c["state"]) for col, cs in r["columns"].items() for c in cs}
    check("and the board shows it",
          where.get(ids["failed"]) == ("backlog", "queued")
          and where.get(ids["proposed"]) == ("backlog", "queued")
          and ids["queued"] not in where, where)
    check("an illegal action is still refused",
          not ht({"action": "accept", "id": ids["landed"]})["ok"])
    before = len(surveyed)
    ts.update(ids["stranded"], result={"landing": {"stage": "dropped", "landed": False}})
    ht({"action": "board", "project": "alpha"})
    check("a finished task changing (a drop, a landing) refreshes the survey",
          len(surveyed) == before + 1)

    # A project with a release.toml gets its plan, cached.
    repo = _release_repo()
    ps.ensure("Rel", scope={"cwd": str(repo)})
    ov = ht({"action": "overview"})
    rel = next(p for p in ov["projects"] if p["slug"] == "rel")["release"]
    check("a project with release.toml shows what is ready per target",
          rel and [t["target"] for t in rel["targets"]] == ["backend", "app"], rel)
    (repo / "app" / "c.swift").write_text("w")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "app three"], check=True)
    ov = ht({"action": "overview"})
    rel2 = next(p for p in ov["projects"] if p["slug"] == "rel")["release"]
    check("and reuses it inside the window rather than asking git every poll",
          rel2 == rel)


BOARD_DRIVER = r"""
const fs = require("fs");
const [src, fixture] = [fs.readFileSync(process.argv[2], "utf8"),
                        JSON.parse(fs.readFileSync(process.argv[3], "utf8"))];
function el(id) {
  return {id, innerHTML: "", textContent: "", value: "", title: "", className: "",
          style: {}, disabled: false, dataset: {}, children: [],
          classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
          appendChild() {}, removeChild() {}, remove() {}, addEventListener() {},
          insertAdjacentHTML(_, h) { this.innerHTML += h; }, focus() {}, scrollIntoView() {},
          querySelector() { return null; }, querySelectorAll() { return []; }};
}
const els = {};
const byId = id => els[id] || (els[id] = el(id));
globalThis.document = {getElementById: byId, createElement: () => el("new"),
                       querySelector: () => null, querySelectorAll: () => [],
                       addEventListener() {}, body: el("body")};
globalThis.window = {addEventListener() {}, location: {search: ""}};
globalThis.localStorage = {getItem: () => null, setItem() {}};
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
const calls = [];
globalThis.fetch = async (url, opts) => {
  const body = opts && opts.body ? JSON.parse(opts.body) : {};
  let out = {ok: true};
  if (url.startsWith("/api/sessions")) out = {bot_online: true, sessions: fixture.sessions, slack: {}};
  else if (url.startsWith("/api/stats")) out = {total_cost: 0, cache_rate: null, threads: 0, days: [], models: []};
  else if (url.startsWith("/api/projects")) out = {ok: true, projects: []};
  else if (url.startsWith("/api/tasks")) {
    calls.push(body);
    if (body.action === "board") out = fixture.boards[body.project] || {ok: true, columns: {}};
    else if (body.action === "overview") out = fixture.overview;
    else if (body.action === "task") out = {ok: true, task: fixture.detail};
    else if (body.action === "list") out = {ok: true, tasks: [], counts: {}};
    else if (body.action === "roles") out = {ok: true, roles: []};
  }
  return {json: async () => out, text: async () => ""};
};
(0, eval)(src + `
;globalThis.__b = {setBoardProject, renderBoard, openCard, toggleBoard, loadList,
                   get project() { return boardProject; }};`);
(async () => {
  const B = globalThis.__b, out = {};
  await B.loadList();
  byId("boardmodal").style.display = "none";
  B.toggleBoard();
  await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r));
  out.open = byId("boardmodal").style.display;
  out.overview = byId("bover").innerHTML;
  out.picker = byId("bproj").innerHTML;
  B.setBoardProject("alpha");
  await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r));
  out.board = byId("bboard").innerHTML;
  out.overviewAfter = byId("bover").innerHTML;
  out.roles = byId("brole").innerHTML;
  B.setBoardProject(fixture.unfiled);
  await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r));
  out.unfiled = byId("bboard").innerHTML;
  await B.openCard(fixture.detail.id);
  out.detail = byId("bdetail").innerHTML;
  out.detailShown = byId("bdetail").style.display;
  B.setBoardProject(null);
  await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r));
  byId("bq").value = "needle";
  B.renderBoard();
  await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r));
  out.searchCall = calls.filter(c => c.action === "board").slice(-1)[0];
  out.searchProject = B.project;
  B.setBoardProject(null);
  await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r));
  out.backToOverview = {project: B.project, q: byId("bq").value,
                        shown: byId("bover").innerHTML.includes('data-slug="alpha"')};
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_board_renders_in_the_page():
    import re
    import board as B
    sys.argv = ["x"]
    import visualizer as V
    print("\nthe projects view and the board, rendered by the page's own javascript")
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    check("the page's unfiled lane is board.UNFILED",
          f'const UNFILED = "{B.UNFILED}";' in js)
    node = shutil.which("node")
    if not node:
        print("  … node not installed — cannot run the page's own javascript")
        check("the board renders cards through taskButtons()",
              "taskButtons(t)" in js[js.index("function cardButtons"):])
        return
    ts, ps, ids, survey, _ = _board_fixture()
    recs = list(ts.all().values())
    repo = _release_repo()
    ov = B.overview(ps.all(include_archived=False), recs, unmerged_rows=survey,
                    releases_by_slug={"alpha": B.release_status(repo)})
    # A title the page must escape, not render.
    ts.update(ids["queued"], title="<img src=x onerror=alert(1)>")
    recs = list(ts.all().values())
    fixture = {
        "unfiled": B.UNFILED,
        "overview": {"ok": True, **ov},
        "boards": {"alpha": {"ok": True, **B.board(recs, "alpha", unmerged_rows=survey)},
                   B.UNFILED: {"ok": True, **B.board(recs, B.UNFILED)},
                   "": {"ok": True, **B.board(recs, "", q="needle")}},
        "detail": B.detail(ts.get(ids["awaiting"]), recs),
        "sessions": [{"key": "D1:1.1", "title": "A loose conversation", "kind": "thread",
                      "project": "", "updated": time.time(), "turns": 1},
                     {"key": "D1:2.2", "title": "Filed conversation", "kind": "thread",
                      "project": "alpha", "updated": time.time(), "turns": 1},
                     {"key": "D1:3.3", "title": "A task run", "kind": "task",
                      "project": "", "updated": time.time(), "turns": 1}],
    }
    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    (d / "fx.json").write_text(json.dumps(fixture))
    (d / "drive.js").write_text(BOARD_DRIVER)
    p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js"), str(d / "fx.json")],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        check("the board's javascript runs", False, p.stderr.strip()[-600:])
        return
    out = json.loads(p.stdout)

    o = out["overview"]
    check("the board opens on the projects overview", out["open"] == "flex"
          and 'data-slug="alpha"' in o and 'data-slug="beta"' in o)
    check("an overview card shows counts, running, last landed and unmerged",
          "running 1" in o and "Run a thing" in o and "abcdef12" in o
          and "2 branches · 3+? commits" in o, o[:1500])
    check("readiness and this week's cost",
          "✓ tests" in o and "✓ auto-merge" in o and "✗ publish" in o and "$3.50" in o)
    check("what is ready to release per target",
          "<b>app</b> 2 commits" in o and "backend nothing pending" in o)
    check("the unfiled lane has a card, counting threads with no project",
          f'data-slug="{B.UNFILED}"' in o and "1 with no project" in o)
    check("the picker lists the projects and the unfiled lane",
          'value="alpha"' in out["picker"] and f'value="{B.UNFILED}"' in out["picker"])

    b = out["board"]
    cols = {m.group(1): m.group(2) for m in re.finditer(
        r'<div class="bcol" data-col="(\w+)">(.*?)(?=<div class="bcol"|$)', b, re.S)}
    check("opening a project draws its five columns",
          list(cols) == ["backlog", "running", "review", "needs", "done"], list(cols))
    check("the overview gives way to the board", out["overviewAfter"] == "")
    inside = lambda col, tid: f'data-id="{tid}"' in cols.get(col, "")
    check("each card is in its column",
          inside("backlog", ids["proposed"]) and inside("running", ids["running"])
          and inside("review", ids["review"]) and inside("needs", ids["awaiting"])
          and inside("needs", ids["failed"]) and inside("done", ids["landed"]))
    card = lambda tid: next((c for c in b.split('<div class="bcard"') if f'"{tid}"' in c), "")
    check("a proposal offers Accept and Dismiss",
          "'accept')" in card(ids["proposed"]) and "'dismiss')" in card(ids["proposed"]))
    check("awaiting approval offers Approve and Send back",
          "'approve')" in card(ids["awaiting"]) and "sendBack(" in card(ids["awaiting"]))
    check("failed offers Retry", "'retry')" in card(ids["failed"]))
    check("running offers Stop", "stopTask(" in card(ids["running"]))
    check("its Thread button closes the board, not the task panel",
          "closeBoard();jumpTo('C1:1.2')" in card(ids["running"])
          and "toggleTasks()" not in b)
    check("stranded finished work offers Land and Drop",
          "'land')" in card(ids["stranded"]) and "'drop')" in card(ids["stranded"]))
    check("work that landed offers neither",
          "'land')" not in card(ids["landed"]) and "landed abcdef12" in card(ids["landed"]))
    check("a refusal names its stage", "not landed (rebase)" in card(ids["refused"]))
    check("a card shows its review verdict",
          "review flagged · 2 findings, 1 follow-up" in card(ids["awaiting"]))
    check("its role, age and cost",
          "implementor" in card(ids["awaiting"]) and "ago" in card(ids["awaiting"])
          and "$2.00" in card(ids["awaiting"]))
    check("a failed card says why", "NO WORKTREE" in card(ids["failed"]))
    check("titles are escaped", "&lt;img src=x" in b and "<img src=x" not in b)
    check("the card's buttons do not also open its detail",
          b.count('class="acts" onclick="event.stopPropagation()"') == b.count('<div class="bcard"'))
    check("the role filter is filled from the board", 'value="implementor"' in out["roles"])

    u = out["unfiled"]
    check("the unfiled lane has work under no project",
          f'data-id="{ids["unfiled"]}"' in u and f'data-id="{ids["queued"]}"' not in u)
    check("and conversations under no project, but not filed ones or task runs",
          "A loose conversation" in u and "Filed conversation" not in u and "A task run" not in u)

    dt = out["detail"]
    check("clicking a card opens its detail", out["detailShown"] == "block")
    check("with the full goal, review findings and followups",
          "Awaits you" in dt and "second finding" in dt and "a followup" in dt)
    check("its events and its actions",
          "events ·" in dt and "'approve')" in dt)
    check("a search typed on the overview searches every project",
          out["searchCall"].get("q") == "needle" and out["searchCall"].get("project") == ""
          and out["searchProject"] == "", out["searchCall"])
    check("and Overview clears the search and goes back to the cards",
          out["backToOverview"] == {"project": None, "q": "", "shown": True},
          out["backToOverview"])



# --- tasks and projects are edited from the boards ----------------------------
# The boards could read everything and change almost nothing: a task could be
# filed only from the old panel's form, never edited, never moved up the
# queue, and of a project's settings only auto-merge, publish and the nightly
# time were reachable. These drive the shipped routes with real stores.

def _edit_routes(tmp=None):
    """handle_tasks and handle_projects out of bot.py, over a real TaskStore
    and ProjectStore in a temp dir. Alpha is ready, beta is not, gone is
    archived."""
    import board as B, tasks as T, projects as P, costs, branches, worktrees
    import holding, roles, scoping
    d = Path(tmp or tempfile.mkdtemp())
    ts = T.TaskStore(d / "tasks.json")
    ps = P.ProjectStore(d / "projects.json")
    ps.ensure("Alpha", test_cmd="./bin/test", auto_merge=True, scope={"cwd": str(d / "alpha")})
    ps.ensure("Beta", scope={"cwd": str(d / "beta")})
    ps.ensure("Gone")
    ps.set_archived("gone", True)
    guards = []
    ns = {"task_store": ts, "project_store": ps, "board": B, "tasks": T, "costs": costs,
          "branches": branches, "worktrees": worktrees, "holding": holding, "roles": roles,
          "projects": P, "scoping": scoping, "workspaces": __import__("workspaces"),
          "unmerged_survey": lambda recs: [], "tell_thread": lambda *a: None,
          "stop_task": lambda tid: True, "install_git_guards": guards.append,
          "unregistered_repos": lambda: [], "GITHUB_OWNER": "",
          "CLAUDE_CWD": d / "scratch", "log": logging.getLogger("test"), "time": time}
    found = _bot_fns({"handle_tasks", "handle_projects", "edit_task", "held_reason",
                      "_task_title", "board_survey", "board_release", "_board_survey",
                      "_board_releases", "_with_costs", "_spend"}, ns)
    return ns, ts, ps, found


def test_run_next_is_honoured_by_claim():
    import tasks as T
    print("\nrun next: claim takes a pinned task first, ties oldest first")
    ts = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    mk = lambda goal, **kw: ts.create(goal, driver="queue", state=T.QUEUED,
                                      role=kw.pop("role", "implementor"), **kw)
    a, b, c = mk("oldest"), mk("middle"), mk("newest")
    for i, t in enumerate((a, b, c)):               # created stamps a < b < c
        ts._data[t["id"]]["created"] = 1000 + i
    check("unpinned, the oldest is claimed first", ts.next_queued()["id"] == a["id"])
    pinned = ts.run_next(c["id"], by="you")
    check("run next pins it above everything", pinned["priority"] == 1)
    ev = (pinned["events"] or [{}])[-1]
    check("and records who asked",
          ev.get("kind") == "priority" and "you" in ev.get("detail", ""))
    ts.run_next(b["id"])
    check("the most recent run next is the next run",
          ts.get(b["id"])["priority"] == 2 and ts.next_queued()["id"] == b["id"])
    got = ts.claim()
    check("claim honours it", got["id"] == b["id"])
    check("and spends it: it asked for this run, not every run",
          got["priority"] == 0 and ts.get(b["id"])["priority"] == 0)
    ts.transition(b["id"], T.DONE)
    check("then the next pinned", ts.claim()["id"] == c["id"])
    ts.transition(c["id"], T.DONE)
    d_, e_ = mk("tie old"), mk("tie new")
    ts._data[d_["id"]]["created"] = 2000; ts._data[e_["id"]]["created"] = 2001
    for t in (e_, d_, a):
        ts._data[t["id"]]["priority"] = 5 if t is not a else 0
    check("equal priorities go oldest first", ts.claim()["id"] == d_["id"])
    ts.transition(d_["id"], T.DONE)
    check("and unpinned work still runs after", [ts.claim()["id"], ts.claim()["id"]]
          == [e_["id"], a["id"]])

    # Unpin, and the refusals.
    f = mk("unpin me"); g = mk("plain")
    ts._data[f["id"]]["created"] = 3001; ts._data[g["id"]]["created"] = 3000
    ts.run_next(f["id"]); ts.run_next(f["id"], on=False)
    check("unpin puts it back in its turn",
          ts.get(f["id"])["priority"] == 0 and ts.next_queued()["id"] == g["id"])
    for state in (T.PROPOSED, T.RUNNING, T.DONE):
        t = ts.create("not in the queue", state=state, driver="queue", role="implementor")
        try:
            ts.run_next(t["id"])
            check(f"run next refuses a {state} task", False, "it was pinned")
        except T.NotEditable as e:
            check(f"run next refuses a {state} task", state in str(e), str(e))

    h = mk("pinned then parked")
    ts.run_next(h["id"]); ts.transition(h["id"], T.BLOCKED); ts.transition(h["id"], T.QUEUED)
    check("a pin is spent by leaving the queue any other way too",
          ts.get(h["id"])["priority"] == 0)
    ts.transition(h["id"], T.CANCELLED)

    # The review lane is not reordered by it.
    rv_old = mk("Review: old", role="reviewer"); rv_new = mk("Review: new", role="reviewer")
    ts._data[rv_old["id"]]["created"] = 4000; ts._data[rv_new["id"]]["created"] = 4001
    try:
        ts.run_next(rv_new["id"])
        check("a review cannot be pinned", False)
    except T.NotEditable:
        check("a review cannot be pinned", True)
    ts.run_next(g["id"])
    check("a pinned task does not jump the review lane",
          ts.claim(only_roles={"reviewer"})["id"] == rv_old["id"])
    check("nor does the task lane take a review",
          ts.claim(except_roles={"reviewer"})["id"] == g["id"])


def test_tasks_are_filed_and_edited_from_the_board():
    import tasks as T, scoping
    print("\nfiling and editing a task from the board")
    ns, ts, ps, found = _edit_routes()
    check("the routes are in bot.py", {"edit_task", "held_reason", "_task_title"} <= found)
    ht = ns["handle_tasks"]

    r = ht({"action": "create", "goal": "too short", "project": "alpha"})
    check("a goal under the filing minimum is refused like a conversation's",
          not r["ok"] and str(scoping.MIN_GOAL_CHARS) in r["error"], r)
    check("and leaves nothing behind", ts.all() == {})
    r = ht({"action": "create", "goal": "x" * (scoping.MAX_GOAL_CHARS + 1), "project": "alpha"})
    check("so is one over the maximum", not r["ok"] and "split" in r["error"])
    r = ht({"action": "create", "goal": "Build the importer for the new feed format",
            "project": "alpha", "role": "implementor", "title": "  Importer  ", "by": "you"})
    check("a board filing is queued under its project with its title",
          r["ok"] and r["task"]["state"] == T.QUEUED and r["task"]["project"] == "alpha"
          and r["task"]["title"] == "Importer", r)
    check("on a ready project it is not held", r["held"] == "")
    alpha_task = r["task"]["id"]
    r = ht({"action": "create", "goal": "Build the importer for the beta feed",
            "project": "beta", "role": "implementor"})
    check("an implementor on a project that is not ready is filed, and said to be held",
          r["ok"] and "beta" in r["held"] and "Needs you" in r["held"], r.get("held"))
    r = ht({"action": "create", "goal": "Summarise what the beta feed contains",
            "project": "beta", "role": "assistant", "state": "proposed"})
    check("an assistant there is not held, and a proposal waits",
          r["ok"] and r["held"] == "" and r["task"]["state"] == T.PROPOSED)
    proposal = r["task"]["id"]
    check("the roles route says which roles are held",
          {x["name"]: x["held_if_unready"] for x in ht({"action": "roles"})["roles"]}
          == {"implementor": True, "assistant": False})
    r = ht({"action": "create", "goal": "Build the importer for the new feed format",
            "project": "alpha", "title": "t" * (scoping.MAX_TITLE_CHARS + 1)})
    check("an overlong title is refused", not r["ok"] and "title" in r["error"])

    # --- edit ---
    r = ht({"action": "edit", "id": alpha_task, "goal": "Build the importer for the v2 feed format",
            "title": "Importer v2", "by": "you"})
    rec = ts.get(alpha_task)
    check("a queued task's goal and title can be edited",
          r["ok"] and rec["goal"].endswith("v2 feed format") and rec["title"] == "Importer v2", r)
    ev = (rec["events"] or [{}])[-1]
    check("recorded as an event naming who and what",
          ev.get("kind") == "edited" and "you" in ev.get("detail", "")
          and "title" in ev.get("detail", "") and "goal" in ev.get("detail", ""), ev)
    n = len(ts.get(alpha_task)["events"])
    r = ht({"action": "edit", "id": alpha_task, "title": "Importer v2"})
    check("an edit that changes nothing writes no event",
          r["ok"] and len(ts.get(alpha_task)["events"]) == n)
    r = ht({"action": "edit", "id": alpha_task, "goal": "short"})
    check("an edited goal keeps the filing rules", not r["ok"] and "at least" in r["error"])
    r = ht({"action": "edit", "id": alpha_task, "role": "admin"})
    check("so does an edited role", not r["ok"] and "admin" in r["error"])
    r = ht({"action": "edit", "id": alpha_task, "role": "reviewer"})
    check("an internal role cannot be edited in", not r["ok"])

    r = ht({"action": "edit", "id": proposal, "project": "alpha", "role": "implementor", "by": "you"})
    rec = ts.get(proposal)
    check("a proposal can move project; its scope follows the new project",
          r["ok"] and rec["project"] == "alpha" and rec["role"] == "implementor"
          and rec["scope"] == ps.scope_for("alpha"), rec["scope"])
    check("the move is in the event",
          "project beta → alpha" in (rec["events"] or [{}])[-1].get("detail", ""))
    r = ht({"action": "edit", "id": proposal, "project": "gone"})
    check("not into an archived project", not r["ok"] and "archived" in r["error"])
    r = ht({"action": "edit", "id": proposal, "project": "nowhere"})
    check("nor an unknown one", not r["ok"] and "unknown project" in r["error"])
    r = ht({"action": "edit", "id": proposal, "project": ""})
    check("unfiling it gives it the scratch directory",
          r["ok"] and ts.get(proposal)["project"] == ""
          and ts.get(proposal)["scope"] == {"cwd": str(ns["CLAUDE_CWD"])})

    # Refused past queued, and racing the claim.
    got = ts.claim()
    check("(the runner claimed the queued one)", got["id"] == alpha_task)
    r = ht({"action": "edit", "id": alpha_task, "goal": "Build the importer for the v3 feed format"})
    check("an edit is refused while it runs",
          not r["ok"] and "running" in r["error"] and "send it back" in r["error"], r)
    check("and changed nothing", ts.get(alpha_task)["goal"].endswith("v2 feed format"))
    for state in (T.BLOCKED, T.AWAITING_APPROVAL, T.DONE):
        t = ts.create("Some work that is past editing", state=state, driver="queue",
                      role="implementor", project="alpha")
        r = ht({"action": "edit", "id": t["id"], "title": "new"})
        check(f"refused while {state}", not r["ok"] and state in r["error"], r)
    homes = []
    real_home = ps.home
    ps.home = lambda slug, create=False: homes.append(slug) or real_home(slug, create=create)
    r = ht({"action": "edit", "id": alpha_task, "project": "beta"})
    ps.home = real_home
    check("a refused move makes nothing for the project it named",
          not r["ok"] and homes == [], homes)
    rv = ts.create("Review: the branch silkworm/tsk_x", state=T.QUEUED, driver="queue",
                   role="reviewer", parent=alpha_task)
    for change in ({"role": "implementor"}, {"goal": "Review something else entirely"},
                   {"title": "renamed"}):
        r = ht({"action": "edit", "id": rv["id"], **change})
        check(f"a queued review cannot be edited ({', '.join(change)})",
              not r["ok"] and "review" in r["error"], r)
    check("and is unchanged", ts.get(rv["id"])["role"] == "reviewer"
          and ts.get(rv["id"])["goal"] == "Review: the branch silkworm/tsk_x")
    derived = ts.create("Derive my title from this goal line\nmore", state=T.QUEUED,
                        driver="queue", role="implementor", project="alpha")
    ht({"action": "edit", "id": derived["id"], "goal": "A different first line now\nmore"})
    check("a title taken from the goal follows an edited goal",
          ts.get(derived["id"])["title"] == "A different first line now")
    ht({"action": "edit", "id": derived["id"], "title": "Chosen"})
    ht({"action": "edit", "id": derived["id"], "goal": "Yet another first line here"})
    check("a chosen title does not", ts.get(derived["id"])["title"] == "Chosen")
    r = ht({"action": "edit", "id": "tsk_nope", "title": "x"})
    check("an unknown task is refused", not r["ok"] and "unknown" in r["error"])

    # Queued shapes that already have something behind them.
    resumed = ts.create("Resume this interrupted piece of work", state=T.QUEUED,
                        driver="queue", role="implementor", project="alpha")
    ts.update(resumed["id"], checkpoint={"session_id": "abc", "at": time.time()})
    r = ht({"action": "edit", "id": resumed["id"], "goal": "Something else entirely now"})
    check("a task waiting to resume a session cannot have its goal changed",
          not r["ok"] and "resume" in r["error"], r)
    reworked = ts.create("Reworked work that has a branch already", state=T.QUEUED,
                         driver="queue", role="implementor", project="alpha")
    ts.update(reworked["id"], branch="silkworm/" + reworked["id"], commits=2)
    r = ht({"action": "edit", "id": reworked["id"], "project": "beta"})
    check("one with a branch cannot change project", not r["ok"] and "branch" in r["error"], r)
    r = ht({"action": "edit", "id": reworked["id"], "goal": "Reworked work, with more detail on it"})
    check("but its goal can still be clarified", r["ok"])

    # --- run next through the route ---
    q1 = ts.create("First queued work for the queue", state=T.QUEUED, driver="queue",
                   role="implementor", project="alpha")
    q2 = ts.create("Second queued work for the queue", state=T.QUEUED, driver="queue",
                   role="implementor", project="alpha")
    r = ht({"action": "run-next", "id": q2["id"], "by": "you"})
    check("run next through the route", r["ok"] and r["task"]["priority"] > 0)
    b = ht({"action": "board", "project": "alpha"})
    backlog = [c for c in b["columns"]["backlog"] if c["state"] == T.QUEUED]
    # Positions count the whole queue -- beta's queued work is in it too --
    # so they rise down the column without being consecutive.
    pos = [c["queue_pos"] for c in backlog]
    check("the board's backlog shows the queue in claim order",
          backlog[0]["id"] == q2["id"] and pos[0] == 1 and pos == sorted(pos)
          and len(set(pos)) == len(pos) and q1["id"] in [c["id"] for c in backlog],
          [(c["id"], c["queue_pos"]) for c in backlog])
    check("and the claim agrees with it", ts.claim()["id"] == q2["id"])
    r = ht({"action": "run-next", "id": q2["id"]})
    check("run next on a running task is refused", not r["ok"] and "queued" in r["error"])
    check("the board names the project and whether it is ready",
          b["info"]["title"] == "Alpha" and b["info"]["unready"] == ""
          and "auto-merge" in ht({"action": "board", "project": "beta"})["info"]["unready"])


def _base_repo():
    """main, plus `feature` (a commit main lacks) and `merged` (already in main)."""
    d = Path(tempfile.mkdtemp())
    g = lambda *a: subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True,
                                  check=True)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (d / "a").write_text("a"); g("add", "-A"); g("commit", "-qm", "one")
    g("branch", "merged")
    (d / "b").write_text("b"); g("add", "-A"); g("commit", "-qm", "two")
    g("checkout", "-qb", "feature")
    (d / "c").write_text("c"); g("add", "-A"); g("commit", "-qm", "feature work")
    g("checkout", "-q", "main")
    return d


def test_project_settings_from_the_board():
    import branches
    print("\nproject settings and archiving, through /projects")
    ns, ts, ps, found = _edit_routes()
    hp, ht = ns["handle_projects"], ns["handle_tasks"]
    repo = _base_repo()
    ps.ensure("alpha", scope={**ps.scope_for("alpha"), "cwd": str(repo)})

    g = hp({"action": "get", "slug": "alpha"})
    check("get answers the record, its base and its readiness",
          g["ok"] and g["project"]["test_cmd"] == "./bin/test" and g["base"] == ""
          and g["unready"] == "" and g["repo"] is True, g)
    check("and refuses an unknown project", not hp({"action": "get", "slug": "nope"})["ok"])
    lst = {p["slug"]: p for p in hp({"action": "list"})["projects"]}
    check("the list says which projects would hold unattended work",
          lst["alpha"]["unready"] == "" and "test command" in lst["beta"]["unready"])

    r = hp({"action": "title", "slug": "alpha", "title": "  Alpha  Feed "})
    check("a project is renamed, not re-keyed",
          r["ok"] and ps.get("alpha")["title"] == "Alpha Feed" and ps.get("alpha-feed") is None)
    check("a blank name is refused", not hp({"action": "title", "slug": "alpha", "title": " "})["ok"])

    # The base branch, checked against the repository.
    r = hp({"action": "base", "slug": "alpha", "branch": "no-such-branch"})
    check("a base that does not exist is refused",
          not r["ok"] and "no branch" in r["error"] and not ps.scope_for("alpha").get("branch"), r)
    r = hp({"action": "base", "slug": "alpha", "branch": "bad..name"})
    check("so is one that is not a branch name", not r["ok"] and "valid" in r["error"])
    r = hp({"action": "base", "slug": "alpha", "branch": "feature"})
    check("a branch with its own work is set without a warning",
          r["ok"] and r["warning"] == "" and ps.scope_for("alpha")["branch"] == "feature", r)
    check("and the rest of the scope is kept", ps.scope_for("alpha")["cwd"] == str(repo))
    c = hp({"action": "base", "slug": "alpha", "branch": "merged", "check": True})
    check("a branch already merged into the default branch is warned about",
          c["ok"] and "already merged into main" in c["warning"], c)
    check("asking does not write", ps.scope_for("alpha")["branch"] == "feature")
    r = hp({"action": "base", "slug": "alpha", "branch": "merged"})
    check("and not set without saying so after seeing the warning",
          not r["ok"] and r.get("needs_force") and ps.scope_for("alpha")["branch"] == "feature", r)
    r = hp({"action": "base", "slug": "alpha", "branch": "merged", "force": True})
    check("set when it is", r["ok"] and ps.scope_for("alpha")["branch"] == "merged"
          and r["warning"])
    r = hp({"action": "base", "slug": "alpha", "branch": "main"})
    check("the default branch itself is no warning", r["ok"] and r["warning"] == "")
    r = hp({"action": "base", "slug": "alpha", "branch": ""})
    check("clearing it goes back to the default",
          r["ok"] and ps.scope_for("alpha")["branch"] == "")
    r = hp({"action": "base", "slug": "beta", "branch": "feature"})
    check("a project with no repository has no branch to choose",
          not r["ok"] and "no repository" in r["error"])
    check("check_base agrees from the helper itself",
          branches.check_base(repo, "merged")["warning"]
          and not branches.check_base(repo, "feature")["warning"]
          and not branches.check_base(repo, "nope")["ok"])

    # Archive hides, unarchive restores; nothing is deleted.
    t = ts.create("Work filed under alpha before archiving", project="alpha", state="queued",
                  driver="queue", role="implementor")
    ov = ht({"action": "overview"})
    check("an active project is on the overview", "alpha" in {p["slug"] for p in ov["projects"]})
    check("an archived one is listed apart", [a["slug"] for a in ov["archived"]] == ["gone"])
    r = hp({"action": "archive", "slug": "alpha"})
    ov = ht({"action": "overview"})
    check("archiving takes it off the overview",
          r["ok"] and "alpha" not in {p["slug"] for p in ov["projects"]}
          and "alpha" in [a["slug"] for a in ov["archived"]], ov.get("archived"))
    check("and out of the pickers",
          "alpha" not in {p["slug"] for p in hp({"action": "list"})["projects"]})
    check("its tasks keep their label", ts.get(t["id"])["project"] == "alpha")
    check("its settings stay", ps.get("alpha")["test_cmd"] == "./bin/test")
    r = hp({"action": "unarchive", "slug": "alpha"})
    ov = ht({"action": "overview"})
    check("unarchiving brings it back",
          r["ok"] and "alpha" in {p["slug"] for p in ov["projects"]}
          and "alpha" not in [a["slug"] for a in ov["archived"]])
    check("archiving an unknown project is refused",
          not hp({"action": "archive", "slug": "nope"})["ok"])


BOARD_EDIT_DRIVER = r"""
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
// Redrawing the modal replaces the elements inside it, as a browser would:
// whatever was written into the old ones (an error message) is gone.
const box = el("fbox");
let boxHtml = "";
Object.defineProperty(box, "innerHTML", {
  get() { return boxHtml; },
  set(h) { boxHtml = h; for (const m of h.matchAll(/id="([^"]+)"/g)) delete els[m[1]]; }});
els.fbox = box;
const keyHandlers = [];
globalThis.document = {getElementById: byId, createElement: () => el("new"),
                       querySelector: () => null, querySelectorAll: () => [],
                       addEventListener(kind, fn) { if (kind === "keydown") keyHandlers.push(fn); },
                       body: el("body")};
globalThis.window = {addEventListener() {}, location: {search: ""}};
globalThis.localStorage = {getItem: () => null, setItem() {}};
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
const confirms = [];
let answer = true;
globalThis.confirm = msg => { confirms.push(msg); return answer; };
globalThis.prompt = () => null;
const toasts = [];
const calls = [];
const replies = {};
globalThis.fetch = async (url, opts) => {
  const body = opts && opts.body ? JSON.parse(opts.body) : {};
  let out = {ok: true};
  if (url.startsWith("/api/sessions")) out = {bot_online: true, sessions: [], slack: {}};
  else if (url.startsWith("/api/stats")) out = {total_cost: 0, cache_rate: null, threads: 0, days: [], models: []};
  else {
    calls.push({url, ...body});
    const key = `${url}:${body.action}`;
    if (replies[key]) out = replies[key](body);
    else if (body.action === "list") out = {ok: true, projects: [
      {slug: "alpha", title: "Alpha", unready: ""},
      {slug: "beta", title: "Beta", unready: "needs a test command and auto-merge before it can take unsupervised work"}]};
    else if (body.action === "roles") out = {ok: true, goal_min: 15, goal_max: 4000, roles: [
      {name: "implementor", hint: "changes code, is tested, reviewed and merged", default: true, held_if_unready: true},
      {name: "assistant", hint: "investigates or answers, nothing is merged", default: false, held_if_unready: false}]};
    else if (body.action === "create") out = {ok: true, task: {id: "tsk_new", state: body.state}, held: ""};
    else if (body.action === "new") out = {ok: true, created: {slug: "my-thing", title: body.name, cwd: "/w/my-thing"}};
    else if (body.action === "board") out = {ok: true, columns: {backlog: []}, roles: [],
      info: {slug: body.project, title: body.project === "beta" ? "Beta" : "Alpha", unready: ""}};
    else if (body.action === "overview") out = {ok: true, projects: [], archived: []};
  }
  return {json: async () => out, text: async () => ""};
};
(0, eval)(src + `
;toast = m => globalThis.__toasts.push(m);
globalThis.__b = {newTaskForm, editTaskForm, submitTaskForm, taskFormWarn, pickRole, goalCheck,
  runNext, closeModal, modalIsOpen, newProjectForm, projectFormWhere, submitProjectForm, adoptRepo,
  projectSettings, saveBase, setAutoMerge, setPublish, archiveProject, saveTestCmd,
  saveIdeate, setBoardProject, renderBoard, cardButtons, boardCard, renderOverview,
  set project(p) { boardProject = p; }, get project() { return boardProject; },
  get form() { return boardForm; }};`);
const tick = async () => { for (let i = 0; i < 4; i++) await new Promise(r => setImmediate(r)); };
const last = (action) => calls.filter(c => c.action === action).slice(-1)[0];
const count = (action) => calls.filter(c => c.action === action).length;
const shown = () => byId("fmodal").style.display === "flex";
(async () => {
  const B = globalThis.__b, out = {};
  globalThis.__toasts = toasts;
  byId("boardmodal").style.display = "flex";
  B.project = "alpha";
  await B.renderBoard(); await tick();
  out.boardHtml = byId("bboard").innerHTML;
  out.overviewHtml = B.renderOverview({projects: [], archived: []});

  // New task: a modal on the open project.
  await B.newTaskForm();
  out.opened = shown();
  out.newForm = byId("fbox").innerHTML;
  B.taskFormWarn(); out.warnReady = byId("tfwarn").textContent;
  byId("tfgoal").value = "too short";
  await B.submitTaskForm(); await tick();
  out.shortWhy = byId("tfgoalwhy").textContent;
  out.shortCalls = count("create");
  byId("tfgoal").value = "  Build the importer for the feed  ";
  byId("tftitle").value = ""; byId("tfpropose").checked = true;
  B.pickRole("assistant");
  await B.submitTaskForm(); await tick();
  out.create = last("create");
  out.closedAfter = {form: B.form, shown: shown(), toast: toasts.slice(-1)[0]};

  // A refusal stays in the form, in the route's words.
  await B.newTaskForm();
  replies["/api/tasks:create"] = () => ({ok: false, error: "that goal is over 4000 characters; split it"});
  byId("tfgoal").value = "A goal long enough to be sent"; byId("tfpropose").checked = false;
  await B.submitTaskForm(); await tick();
  out.refused = byId("tferr").textContent;
  out.stillOpen = shown() && !!B.form;
  delete replies["/api/tasks:create"];

  // Cancel, Esc: nothing sent.
  const before = calls.length;
  B.closeModal();
  out.cancel = {shown: shown(), sent: calls.length - before};
  await B.newTaskForm();
  const opened = calls.length;
  for (const h of keyHandlers) h({key: "Escape", target: el("body"), preventDefault() {}});
  out.esc = {shown: shown(), sent: calls.length - opened, handlers: keyHandlers.length};

  // Unready project: the warning.
  B.project = "beta";
  await B.newTaskForm();
  B.pickRole("implementor"); B.taskFormWarn(); out.warnBeta = byId("tfwarn").textContent;
  B.pickRole("assistant"); B.taskFormWarn(); out.warnAssistant = byId("tfwarn").textContent;
  B.closeModal();
  B.project = "alpha";

  // Edit sends only what changed.
  replies["/api/tasks:task"] = () => ({ok: true, task: {id: "tsk_1", state: "queued",
    title: "Old", goal: "Old goal text here", project: "alpha", role: "implementor"}});
  await B.editTaskForm("tsk_1");
  out.editForm = byId("fbox").innerHTML;
  byId("tfproj").value = "alpha";
  byId("tftitle").value = " New   title "; byId("tfgoal").value = "Old goal text here  ";
  await B.submitTaskForm(); await tick();
  out.edit = last("edit");
  replies["/api/tasks:task"] = () => ({ok: true, task: {id: "tsk_2", state: "queued",
    title: "Two words", goal: "Old goal text here", project: "alpha", role: "implementor"}});
  await B.editTaskForm("tsk_2");
  byId("tfproj").value = "alpha";
  byId("tftitle").value = "Two    words"; byId("tfgoal").value = "Old goal text here";
  await B.submitTaskForm(); await tick();
  out.spacesEdit = last("edit");
  B.closeModal();

  // Card buttons.
  out.queuedButtons = B.cardButtons({id: "tsk_q", state: "queued", role: "implementor", priority: 0});
  out.pinnedButtons = B.cardButtons({id: "tsk_p", state: "queued", role: "implementor", priority: 3});
  out.runningButtons = B.cardButtons({id: "tsk_r", state: "running", role: "implementor"});
  out.reviewButtons = B.cardButtons({id: "tsk_v", state: "queued", role: "reviewer"});
  out.proposedButtons = B.cardButtons({id: "tsk_o", state: "proposed", role: "implementor"});
  out.card = B.boardCard({id: "tsk_p", title: "T", state: "queued", role: "implementor",
    priority: 3, queue_pos: 1, created: Date.now() / 1000, attempts: 0});
  await B.runNext("tsk_q", true); out.runNext = last("run-next");

  // New project.
  B.newProjectForm();
  out.projectForm = byId("fbox").innerHTML;
  byId("npname").value = "My Thing!"; B.projectFormWhere();
  out.where = byId("npwhere").textContent;
  out.pathHidden = byId("nppathrow").style.display;
  byId("npexisting").checked = true; B.projectFormWhere();
  out.pathShown = byId("nppathrow").style.display;
  await B.submitProjectForm(); await tick();
  out.noPath = {err: byId("tferr").textContent, sent: count("new")};
  byId("npexisting").checked = false; B.projectFormWhere();
  byId("nppurpose").value = " Track the things "; byId("npgithub").checked = true;
  replies["/api/projects:new"] = () => ({ok: false, error: "there is already a project `my-thing`"});
  await B.submitProjectForm(); await tick();
  out.projectRefused = {err: byId("tferr").textContent, open: shown()};
  delete replies["/api/projects:new"];
  await B.submitProjectForm(); await tick();
  out.newProject = last("new");
  out.afterProject = {shown: shown(), project: B.project};
  B.adoptRepo("/w/old-repo");
  out.adopt = {html: byId("fbox").innerHTML, existing: byId("npexisting").checked,
               path: byId("nppath").value};
  byId("npexisting").checked = true; byId("npname").value = "old-repo"; byId("nppath").value = "/w/old-repo";
  await B.submitProjectForm(); await tick();
  out.adoptSent = last("new");
  B.closeModal();

  // Settings.
  replies["/api/projects:get"] = () => ({ok: true, project: {slug: "alpha", title: "Alpha",
    test_cmd: "./t", auto_merge: false, publish: false, ideate_at: "", scope: {cwd: "/x"}},
    unready: "", base: "", repo: true});
  await B.projectSettings("alpha");
  out.settings = byId("fbox").innerHTML;
  out.settingsInModal = shown();

  answer = false; confirms.length = 0;
  byId("psauto").checked = true;
  await B.setAutoMerge("alpha", true); await tick();
  out.autoCancelled = {asked: confirms.length, called: count("auto-merge"), box: byId("psauto").checked};
  answer = true;
  await B.setAutoMerge("alpha", true); await tick();
  out.autoOn = last("auto-merge");
  await B.setAutoMerge("alpha", false); await tick();
  out.autoOffConfirms = confirms.length;
  confirms.length = 0;
  await B.setPublish("alpha", true); await tick();
  out.publish = {call: last("publish"), asked: confirms.length};
  replies["/api/projects:publish"] = () => ({ok: false, error: "turn auto-merge on first — there is nothing to publish"});
  await B.setPublish("alpha", true); await tick();
  out.publishRefused = byId("tferr").textContent;
  delete replies["/api/projects:publish"];

  replies["/api/projects:base"] = b => b.check
    ? {ok: true, warning: "merged is already merged into main", branch: b.branch}
    : {ok: true, warning: "merged is already merged into main", project: {}};
  confirms.length = 0;
  byId("psbase").value = "merged";
  await B.saveBase("alpha"); await tick();
  out.base = {calls: calls.filter(c => c.action === "base").slice(-2), confirm: confirms.slice(-1)[0]};
  replies["/api/projects:base"] = b => ({ok: false, error: "there is no branch 'nope'"});
  await B.saveBase("alpha"); await tick();
  out.baseRefused = byId("tferr").textContent;

  confirms.length = 0; answer = false;
  await B.archiveProject("alpha", true); await tick();
  out.archiveCancelled = {asked: confirms.length, called: count("archive")};
  answer = true;
  await B.archiveProject("alpha", true); await tick();
  out.archive = {call: last("archive"), confirm: confirms.slice(-1)[0]};
  confirms.length = 0;
  await B.archiveProject("gone", false); await tick();
  out.unarchive = {call: last("unarchive"), asked: confirms.length};
  out.overview = B.renderOverview({projects: [{slug: "alpha", title: "Alpha", counts: {}}],
                                   archived: [{slug: "gone", title: "Gone", tasks: 2}]});
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_board_edits_in_the_page():
    import re
    sys.argv = ["x"]
    import visualizer as V
    print("\nthe board's modal forms, driven through the page's own javascript")
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    defined = set(re.findall(r"(?:async\s+)?function\s+([A-Za-z_]\w*)", js))
    for name in ("newTaskForm", "editTaskForm", "submitTaskForm", "taskFormWarn", "runNext",
                 "projectSettings", "settingsCall", "saveProjectTitle", "saveTestCmd",
                 "setAutoMerge", "setPublish", "saveIdeate", "saveBase", "archiveProject",
                 "archivedSection", "boardHead", "projectCall", "openModal", "closeModal",
                 "modalIsOpen", "formError", "goalCheck", "roleChoices", "pickedRole",
                 "newProjectForm", "projectFormWhere", "submitProjectForm", "adoptRepo"):
        check(f"{name}() is defined", name in defined)
    handlers = set(re.findall(r'on(?:click|change|input)="(?:event\.stopPropagation\(\);)?([a-zA-Z_]\w*)\(', js))
    missing = sorted(h for h in handlers if h not in defined
                     and h not in {"if", "confirm", "prompt", "alert", "event"})
    check("every onclick, onchange and oninput handler is defined", not missing, f"missing: {missing}")

    # One way to do each thing: the Tasks panel's inline rows are gone.
    panel = V.PAGE[V.PAGE.index('<div id="taskmodal"'):V.PAGE.index('<div id="boardmodal"')]
    for gone in ('id="tgoal"', 'id="tcwd"', 'id="tnewproj"', 'id="trole"', "working dir",
                 'onclick="addTask()"', 'onclick="newProject()"'):
        check(f"the Tasks panel no longer has {gone}", gone not in V.PAGE, gone)
    check("it has a + New project button that opens the modal",
          'onclick="newProjectForm()">+ New project' in panel)
    check("the modal closes on a click outside it",
          "<div id=\"fmodal\" onclick=\"if(event.target.id==='fmodal')closeModal()\">" in V.PAGE)

    node = shutil.which("node")
    if not node:
        print("  … node not installed — cannot run the page's own javascript")
        return
    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    (d / "drive.js").write_text(BOARD_EDIT_DRIVER)
    p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js")],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        check("the board's form javascript runs", False, p.stderr.strip()[-800:])
        return
    o = json.loads(p.stdout)

    bh = o["boardHtml"]
    cols_at = bh.find('<div class="bcols">')
    check("the board header offers + New task and Settings",
          '<div class="bhead">' in bh and 'newTaskForm()">+ New task' in bh
          and 0 <= bh.find("newTaskForm()") < cols_at and "projectSettings('alpha')" in bh)
    check("and the columns do not repeat it", cols_at >= 0 and "newTaskForm()" not in bh[cols_at:])
    ov = o["overviewHtml"]
    check("the overview header offers + New project",
          'newProjectForm()">+ New project' in ov
          and 0 <= ov.find("New project") < ov.find('class="pcards"'))

    f = o["newForm"]
    check("+ New task opens a modal titled with the project", o["opened"] and "New task in Alpha" in f)
    check("the project is shown, not typed, and there is no directory field",
          'id="tfproj"' not in f and '<div class="fixed">Alpha</div>' in f
          and "directory" not in f.lower() and "cwd" not in f.lower())
    check("labelled fields: title, what should it do, role, when",
          all(x in f for x in ("title <span", "What should it do?", "<label>role</label>",
                                "<label>when</label>")))
    check("the two roles are described choices from the route",
          "<b>Implementor</b>: changes code, is tested, reviewed and merged" in f
          and "<b>Assistant</b>: investigates or answers, nothing is merged" in f)
    check("queue now or propose for later",
          "<b>Queue now</b>" in f and "<b>Propose for later</b>" in f and 'id="tfqueue" checked' in f)
    check("Create task and Cancel",
          ">Create task</button>" in f and 'onclick="closeModal()">Cancel' in f)
    check("no warning for a ready project", o["warnReady"] == "")
    check("a short goal is explained inline and nothing is sent",
          "6 more characters needed" in o["shortWhy"] and o["shortCalls"] == 0, o["shortWhy"])
    c = o["create"]
    check("the form files exactly the payload the route reads",
          c == {"url": "/api/tasks", "action": "create", "role": "assistant",
                "goal": "Build the importer for the feed", "project": "alpha", "by": "you",
                "state": "proposed"}, c)
    check("success closes the modal and confirms",
          o["closedAfter"]["form"] is None and o["closedAfter"]["shown"] is False
          and o["closedAfter"]["toast"] == "Proposed", o["closedAfter"])
    check("a refusal is shown in the form, which stays open",
          "split it" in o["refused"] and o["stillOpen"])
    check("Cancel closes it and sends nothing", o["cancel"] == {"shown": False, "sent": 0})
    check("so does Esc", o["esc"]["shown"] is False and o["esc"]["sent"] == 0
          and o["esc"]["handlers"] >= 1, o["esc"])
    check("an implementor for an unready project is said to be held",
          "beta" in o["warnBeta"] and "Needs you" in o["warnBeta"], o["warnBeta"])
    check("an assistant there is not", o["warnAssistant"] == "")

    check("the edit modal is prefilled, with the project changeable",
          'value="Old"' in o["editForm"] and "Old goal text here" in o["editForm"]
          and 'id="tfproj"' in o["editForm"] and ">Save</button>" in o["editForm"])
    check("an edit sends only what changed",
          o["edit"] == {"url": "/api/tasks", "action": "edit", "id": "tsk_1", "by": "you",
                        "title": "New   title"}, o["edit"])
    check("spacing the route would collapse is not an edit",
          o["spacesEdit"] == {"url": "/api/tasks", "action": "edit", "id": "tsk_2", "by": "you"},
          o["spacesEdit"])
    check("a queued card offers Edit and Run next",
          "editTaskForm('tsk_q')" in o["queuedButtons"] and "runNext('tsk_q',true)" in o["queuedButtons"])
    check("a pinned one offers Unpin", "runNext('tsk_p',false)" in o["pinnedButtons"])
    check("a proposal can be edited but not run next",
          "editTaskForm(" in o["proposedButtons"] and "runNext(" not in o["proposedButtons"])
    check("a running card offers neither",
          "editTaskForm(" not in o["runningButtons"] and "runNext(" not in o["runningButtons"])
    check("a queued review offers neither",
          "editTaskForm(" not in o["reviewButtons"] and "runNext(" not in o["reviewButtons"])
    check("a queued card shows its place in the queue", "📌 #1 in queue" in o["card"])
    check("run next sends the right payload",
          o["runNext"] == {"url": "/api/tasks", "action": "run-next", "id": "tsk_q", "on": True,
                           "by": "you"}, o["runNext"])

    pf = o["projectForm"]
    check("the new-project modal has name, purpose, location, folder and GitHub",
          all(f'id="{i}"' in pf for i in ("npname", "nppurpose", "npwhere", "npexisting",
                                         "nppath", "npgithub"))
          and "What is it for?" in pf and "CLAUDE.md" in pf
          and "Use an existing folder instead" in pf and "Create a private GitHub repo" in pf
          and ">Create project</button>" in pf)
    check("the location is shown live from the name", o["where"] == "~/workspace/my-thing", o["where"])
    check("the folder field appears only when asked for",
          o["pathHidden"] == "none" and o["pathShown"] == "block")
    check("an existing folder with no path is refused in the form, unsent",
          "which folder" in o["noPath"]["err"] and o["noPath"]["sent"] == 0)
    check("a refused project keeps the form open with the route's words",
          "already a project" in o["projectRefused"]["err"] and o["projectRefused"]["open"])
    check("it sends exactly what the route reads",
          o["newProject"] == {"url": "/api/projects", "action": "new", "name": "My Thing!",
                              "purpose": "Track the things", "path": "", "adopt": False,
                              "github": True}, o["newProject"])
    check("and then opens the new project's board",
          o["afterProject"] == {"shown": False, "project": "my-thing"}, o["afterProject"])
    check("an unregistered repo opens the same form, adopting it",
          'id="npexisting" checked' in o["adopt"]["html"]
          and 'value="/w/old-repo"' in o["adopt"]["html"]
          and 'value="old-repo"' in o["adopt"]["html"], o["adopt"]["html"][:600])

    check("submitted, it adopts that folder",
          o["adoptSent"] == {"url": "/api/projects", "action": "new", "name": "old-repo",
                             "purpose": "", "path": "/w/old-repo", "adopt": True,
                             "github": False}, o["adoptSent"])

    st = o["settings"]
    check("settings open in the same modal",
          o["settingsInModal"] and all(f'id="{i}"' in st for i in
                                       ("pstitle", "pstest", "psauto", "pspublish", "psideate", "psbase")))
    check("with a note that readiness needs the test command", "Readiness needs it" in st)
    check("and Archive", "archiveProject('alpha',true)" in st)
    check("cancelling auto-merge's confirmation changes nothing and unticks the box",
          o["autoCancelled"] == {"asked": 1, "called": 0, "box": False}, o["autoCancelled"])
    check("confirmed, it is sent",
          (o["autoOn"] or {}).get("on") is True and (o["autoOn"] or {}).get("url") == "/api/projects")
    check("turning it off does not ask", o["autoOffConfirms"] == 2, o["autoOffConfirms"])
    check("publishing on asks first", o["publish"]["asked"] == 1
          and (o["publish"]["call"] or {}).get("on") is True)
    check("a refused toggle keeps the route's reason on screen after the redraw",
          "turn auto-merge on first" in o["publishRefused"], o["publishRefused"])
    b = o["base"]
    second = (b["calls"] + [{}, {}])[1]
    check("a base branch is checked, then confirmed with the warning, then forced",
          [x.get("check") for x in b["calls"]] == [True, None]
          and second.get("force") is True and second.get("branch") == "merged"
          and "already merged" in (b.get("confirm") or ""), b)
    check("a refused base shows the route's error", "no branch 'nope'" in o["baseRefused"])
    check("archiving asks, and cancelling sends nothing",
          o["archiveCancelled"] == {"asked": 1, "called": 0})
    check("the confirmation says what archiving does",
          "hidden from the overview" in (o["archive"]["confirm"] or "")
          and "Nothing is deleted" in (o["archive"]["confirm"] or "")
          and (o["archive"]["call"] or {}).get("slug") == "alpha")
    check("unarchiving does not ask", o["unarchive"]["asked"] == 0
          and (o["unarchive"]["call"] or {}).get("slug") == "gone")
    check("archived projects are a collapsed section with Unarchive",
          '<details class="parch"><summary>Archived · 1</summary>' in o["overview"]
          and "archiveProject('gone',false)" in o["overview"])

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


def _task_state_for(st):
    """task_state as shipped, against a real store."""
    import tasks as T
    return _bot_func("task_state", task_store=st, tasks=T, log=logging.getLogger("t"))


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

    line = json.dumps(home.render([rec], now=now, compact=True), ensure_ascii=False)
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
    check("the Release button confirms, then sends only the task id",
          "confirm(" in rs and 'taskAction(id, "release")' in rs)


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
    ns = bot_functions("handle_tasks", "approve_task", task_store=st, tasks=T,
                       holding=__import__("holding"), task_state=state,
                       release_command=release_command, RELEASE_STARTED=":rocket:",
                       LANDING_UNDERWAY="in-progress", merge=__import__("merge"),
                       roles=__import__("roles"),
                       tell_thread=lambda key, text: said.append(text),
                       stop_task=lambda tid: False,
                       start_landing=lambda tid, **k: landings.append((tid, k)))
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
    ns = bot_functions("handle_tasks", "approve_task", task_store=st, tasks=T,
                       holding=__import__("holding"), stop_task=lambda tid: True,
                       start_landing=lambda tid: None, tell_thread=lambda *a: None)
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


# --- releasing from the dashboard and the Slack board -----------------------------
# `!release` was the only way to ship, typed. These are the same release behind
# one confirmed click: a plan with its previews, the exact production effects,
# and a start that carries the fingerprint of what was confirmed.

def _release_fixture():
    """A project repo with a bare origin and fake targets: backend deploys by
    appending to a log outside the checkout and previews with an echo; ios is
    a versioned tag target after backend; web fails with exit 3."""
    import threading as _th
    import releases as R, projects as P, worktrees as W, board as B
    root = Path(tempfile.mkdtemp())
    origin, repo = root / "origin.git", root / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], capture_output=True)
    g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                                   "-c", "user.name=t", *a], capture_output=True, text=True)
    def commit(path, text, msg, push=True):
        p = repo / path; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)
        g("add", path); g("commit", "-q", "-m", msg)
        if push:
            g("push", "-q", "origin", "main")
    log_ = root / "deploy.log"
    commit(".silkworm/release.toml", f'''
[targets.backend]
paths = ["supabase/"]
ship = "command"
commands = ["echo deployed >> {log_}"]
preview = ["echo would-apply-0002"]

[targets.ios]
paths = ["ios/"]
ship = "tag"
version = {{ file = "ios/project.yml", key = "CFBundleShortVersionString" }}
after = ["backend"]
describe = "CI builds the tag for TestFlight"

[targets.web]
paths = ["web/"]
ship = "command"
commands = ["exit 3"]
''', "config", push=False)
    commit("ios/project.yml", 'CFBundleShortVersionString: "1.1.0"\n', "ios", push=False)
    commit("supabase/migrations/0001.sql", "x\n", "migration one", push=False)
    commit("supabase/migrations/0002.sql", "x\n", "migration two", push=False)
    commit("supabase/functions/f/index.ts", "x\n", "a function", push=False)
    commit("web/index.html", "x\n", "site")
    ps = P.ProjectStore(root / "projects.json")
    ps.ensure("Alpha", scope={"cwd": str(repo)})
    posts = []

    class Client:
        def chat_postMessage(self, channel, text, thread_ts=None, **kw):
            posts.append((channel, thread_ts, text))
            return {"ts": f"9.{len(posts)}"}
    ns = {"releases": R, "project_store": ps, "worktrees": W, "threading": _th,
          "log": logging.getLogger("test"), "time": time, "json": json,
          "repo_guard": lambda cwd: contextlib.nullcontext(),
          "ALLOWED_USERS": {"U_ME"}, "home_channel": lambda: "D1", "board": B,
          "app": types.SimpleNamespace(client=Client()),
          "board_release": lambda slug: B.release_status(ps.scope_for(slug).get("cwd"))}
    _bot_fns({"release_command", "release_plan", "run_release", "_releasing",
              "_releasing_guard", "release_steps", "_release_runs", "release_allowed",
              "_release_names", "release_offer", "start_release", "release_thread",
              "releasable_projects", "handle_releases", "_board_releases",
              "release_checkout"}, ns)
    return types.SimpleNamespace(ns=ns, repo=repo, origin=origin, root=root, g=g,
                                 commit=commit, log=log_, posts=posts, ps=ps)


def _wait_release(hr, slug, secs=60):
    end = time.time() + secs
    while time.time() < end:
        st = hr({"action": "status", "slug": slug})
        if st["run"] and st["run"]["finished"] and not st["running"]:
            return st
        time.sleep(0.05)
    return hr({"action": "status", "slug": slug})


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
          "2 in supabase/migrations" in eff and "1 in supabase/functions" in eff, eff)
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
        def views_publish(self, **kw):
            self.calls.append(("publish", kw))

    import tasks as T
    store = T.TaskStore(fx.root / "t.json")
    hub = home.Home(store=store, call=lambda p: {"ok": True}, allowed_users={"U_ME"},
                    releasable=lambda: hr({"action": "list"})["projects"], release_call=hr)
    hub._spawn = lambda fn: fn()
    view = hub.view_for("U_ME")
    dumped = json.dumps(view)
    check("a project with something to release is on the board",
          "Ready to release" in dumped and '"release|alpha|"' in dumped
          and "backend 3" in dumped, dumped[-600:])
    rb = home.release_blocks([{"slug": f"p{i}", "title": "P", "targets": []} for i in range(9)])
    check("with a menu entry Release…, the rest counted",
          sum(1 for x in rb if x.get("accessory")) == home.MAX_RELEASES
          and "4 more" in json.dumps(rb))
    big = home.render([store.create(f"t{i}", state=T.PROPOSED) for i in range(60)], now=time.time(),
                      compact=True, max_blocks=50, max_attention=40,
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
    check("and a Release button carrying the plan's fingerprint",
          v["submit"]["text"] == "Release" and meta == {"slug": "alpha",
          "confirm": hr({"action": "plan", "slug": "alpha"})["confirm"]})
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
    lax = home.Home(store=store, call=lambda p: {"ok": True}, allowed_users={"U_ME"},
                    release_call=lambda p: asked.append(p) or {"ok": True})
    lax._spawn = lambda fn: fn()
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
    hub.on_release(lambda **k: None, body("U_ME"), s, view_(meta))
    check("confirmed, it releases", "Releasing" in hub._notices["U_ME"][0], hub._notices)
    st = _wait_release(hr, "alpha")
    check("and runs to the end", st["run"]["released"] == ["backend/v1.0.0", "ios/v1.1.1"]
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



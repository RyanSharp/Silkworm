"""Turns, sessions and recovery: a turn's history, its child process, its cost, and what survives a restart or an outage.

Run by tests/run.py with every other tests/test_*.py; see tests/harness.py."""

from harness import *  # noqa: F401,F403


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


# --- redelivery watermark -----------------------------------------------------

def test_watermark():
    print("\nredelivery watermark")
    store = tmp_store()
    handled = lift("_already_handled", store=store)["_already_handled"]

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
    env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-" + "oat01-fake"}
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
    # Bound to `guard` first (the operator's read-only turns take none; see
    # test_operator_turns_do_not_hold_the_checkout...), then taken inside the
    # thread lock at both sites.
    check("taken inside the thread lock, so the order cannot deadlock",
          bot.count("with lock, guard:") == 2
          and bot.count("else repo_guard(cwd, progress))") == 2)


# --- a turn may run as long as it is still working -------------------------------
# The 900s wall clock killed three real turns in three days -- a strategy
# backtest and two game-dev iterations -- because elapsed time cannot tell
# progress from a wedge. Only silence can.

def test_turn_deadline_is_idleness():
    import inspect
    import threading
    import claude_runner as CR
    print("\nturns end on silence, not on the clock")

    # Time here is a fake clock the test owns, not the wall. The real child's
    # output is what advances it: each line it prints moves "now" on by one
    # second. So a turn that keeps printing really does live for far longer
    # than its idle limit -- and a machine that stalls the child for a few real
    # seconds (a concurrent suite, a slow interpreter start) moves nothing, so
    # it cannot pass for silence. Only the test decides how much time passes.
    #
    # Lockstep, so nothing races: after moving the clock, the reader waits for
    # the watchdog to look at the new time before taking the next line. Every
    # instant the test creates is judged, so a deadline measured from the start
    # instead of from the last output cannot slip past between two checks.
    class Clock:
        def __init__(self):
            self.now = 0.0
            self.seen = 0.0     # the latest time the watchdog has looked at
            self.cv = threading.Condition()
            self.dog = None     # the watchdog thread, once it first looks
            self.lost = False   # no watchdog ever read this clock

        def __call__(self):
            with self.cv:
                if threading.current_thread().name == "turn-watchdog":
                    self.dog = threading.current_thread()
                    self.seen = self.now
                    self.cv.notify_all()
                return self.now

        def advance(self, seconds):
            with self.cv:
                # Not before the watchdog has taken its starting reading: a
                # watchdog slow to start would otherwise read "started" after
                # time had moved, and judge the cap from the wrong instant. If
                # it never reads this clock at all, do not wait on it line by
                # line -- run() reports that rather than stalling the suite.
                if self.lost or not self.cv.wait_for(lambda: self.dog is not None,
                                                     timeout=10):
                    self.lost = True
                    self.now += seconds
                    return
                self.now += seconds
                # Wait for the watchdog to judge the new time -- unless it has
                # already ended the turn and will never look again. The real-
                # time bound is only so a broken watchdog cannot hang the
                # suite; it is never what the outcome rests on.
                give_up = time.monotonic() + 30
                while (self.seen < self.now and time.monotonic() < give_up
                       and not (self.dog and not self.dog.is_alive())):
                    self.cv.wait(0.05)

    # A stand-in for the real binary: it must be executable and ignore the
    # flags run_turn always passes (-p, --output-format ...), so parameters
    # come through the environment instead of argv. TICKS is how many lines it
    # prints; QUIET is how long it then goes silent, in real time.
    #
    # Plain sh, not a Python interpreter, so the fake starts in milliseconds.
    # (Since the deadlines run on the test's clock, a slow start can no longer
    # pass for silence; sh just keeps each run short.)
    fake = Path(tempfile.mkdtemp()) / "fakeclaude"
    fake.write_text(
        "#!/bin/sh\n"
        "cat > /dev/null\n"
        "i=0\n"
        'while [ "$i" -lt "$TICKS" ]; do\n'
        "  printf '%s\\n' '{\"type\": \"system\", \"subtype\": \"init\", "
        "\"session_id\": \"fake-session\"}'\n"
        "  i=$((i + 1))\n"
        "done\n"
        'sleep "$QUIET"\n'
        "printf '%s\\n' '{\"type\": \"result\", \"result\": \"done\", "
        "\"session_id\": \"fake-session\"}'\n")
    fake.chmod(0o755)

    def run(lines, quiet, step, **kw):
        clock = Clock()
        env = {**os.environ, "TICKS": str(lines), "QUIET": str(quiet)}
        try:
            r = CR.run_turn("go", binary=str(fake), cwd=str(BASE),
                            permission_args=[], env=env, clock=clock, poll=0.01,
                            on_init=lambda _sid: clock.advance(step), **kw)
        finally:
            run.elapsed = clock.now
            # When the watchdog last looked: the moment it stopped the turn, if
            # it did. Lines already in the pipe are still read after a kill and
            # move the clock on, so elapsed alone overstates a killed turn.
            run.judged = clock.seen
            watched.append(clock.dog is not None)
        return r
    watched = []

    # Busy for far longer than the idle limit: must survive. Twelve lines a
    # second apart is twelve seconds of work under a three-second idle limit.
    try:
        text = run(12, 0, 1, timeout=0, idle_timeout=3).text
    except CR.ClaudeTimeout as e:
        text = f"killed: {e}"
    check("a turn that keeps working outlives the idle limit",
          text == "done" and run.elapsed >= 12,
          f"ran {run.elapsed:.0f}s under a 3s idle limit, got {text[:60]!r}")

    # Quiet for longer than the idle limit: must die, and say why. One line,
    # then four seconds pass with nothing more (the child really sleeps on).
    try:
        run(1, 30, 4, timeout=0, idle_timeout=3)
        check("a silent turn is stopped", False, "it was allowed to hang")
    except CR.ClaudeTimeout as e:
        check("a silent turn is stopped", True)
        check("and the error names silence, not elapsed time",
              "no output" in str(e) and "as long as it likes" in str(e), str(e)[:90])

    # The absolute cap still exists for anyone who wants one, and it holds
    # against a child that never stops talking: forty seconds of steady output
    # against a three-second cap. (Forty, not forever, so a missing cap fails
    # here rather than hanging the suite.)
    try:
        run(40, 0, 1, timeout=3, idle_timeout=0)
        check("an explicit absolute cap still applies", False, "it ran past the cap")
    except CR.ClaudeTimeout as e:
        check("an explicit absolute cap still applies",
              "timed out after 3s" in str(e) and run.judged <= 4,
              f"{str(e)[:80]} at {run.judged:.0f}s")

    check("every deadline above was judged on the test's clock",
          len(watched) == 3 and all(watched), f"watchdog read it: {watched}")
    # The real deadlines read the wall, not a test's clock.
    check("left alone, the deadlines run on the monotonic clock",
          inspect.signature(CR.run_turn).parameters["clock"].default is time.monotonic)

    check("a timeout is still not treated as a dead session",
          issubclass(CR.ClaudeTimeout, CR.ClaudeError))

    src = (BASE / "claude_runner.py").read_text()
    check("any output counts as alive, even lines we cannot parse",
          src.index("last_seen[0] = clock()") < src.index("line = line.strip()"),
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
          'not tasks.is_wakeup(r)' in watch,
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
          'SILKWORM_BIN = str(BASE_DIR / "bin" / "silkworm")' in bot
          and "BASE_DIR = Path(os.environ.get(\"SILKWORM_HOME\") or CODE_DIR).expanduser().resolve()" in bot,
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
    should = lift("should_handle", "HANDLED_SUBTYPES", BOT_USER_ID="UBOT")["should_handle"]

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

    # The note is webthread.attachment_note, shared with dashboard uploads;
    # what it says is checked in test_webthread_files_and_note.
    check("images are pointed out as viewable, not just listed",
          "use Read to look at it" in (BASE / "webthread.py").read_text()
          and "prompt += webthread.attachment_note(attached)" in src)
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

    ns = lift("_sweep_pass", "_sweeper", store=Store(), task_store=Tasks(),
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
        ns = lift(
            "handle_status", store=types.SimpleNamespace(all=lambda: {}), RUNNING={},
            RUNNER_HOLD=__import__("retry").Hold(), DRAIN=__import__("drain").Drain(),
            credentials=types.SimpleNamespace(state=lambda has_token: {"mode": "token"}),
            slack=types.SimpleNamespace(status=lambda now: {}),
            revision=types.SimpleNamespace(state=lambda *a: {}),
            BASE_DIR=BASE, REVISION={}, os=os, daemons=daemons,
            task_store=types.SimpleNamespace(late_wakeups=lambda now: []))
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

    # A reset hours from now, whenever this runs. It was a fixed "resets 6pm",
    # which just before 6pm is a hold of seconds -- and "the runner is held,
    # not merely slowed" then failed on the clock, not on the code.
    at = datetime.now() + timedelta(hours=3)
    resets = f"{at.hour % 12 or 12}{'am' if at.hour < 12 else 'pm'}"

    def run_turn(goal, **kw):
        calls.append(goal)
        if "does work" in goal:
            kw["on_activity"]("Bash", {"command": "make"})
        raise ClaudeError(f"You've hit your session limit · resets {resets}")

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
    lift({"send_back_uncommitted", "send_back_for_tests",
              "MAX_COMMIT_ATTEMPTS", "MAX_VERIFY_ATTEMPTS"}, ns=ns)
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
    got = lift({"resolve_review", "rework_flagged_review", "rework_conflict",
                    "conflict_addendum", "send_back", "review_addendum", "_unsupervised",
                    "MAX_REVIEW_REWORKS", "MAX_CONFLICT_REWORKS", "CONFLICT_STAGES"}, ns=ns,
               absent=('CLAUDE_CWD',))["__lifted__"]
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
    lift({"file_followups"}, ns=ns)
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
    lift({"file_followups"}, ns=ns2)
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
    compact = home.render(board, now=now)
    text = json.dumps(compact)
    row = next(b for b in compact["blocks"] if b.get("block_id") == f"t:{impl['id']}")
    check("the board shows a task's cost with its reviews",
          "$3.25+" in json.dumps(row), json.dumps(row)[:300])
    check("and the project week in its summary",
          "last 7d" in text and "proj $3.25+" in text)
    row = next(b for b in compact["blocks"] if b.get("block_id") == f"t:{impl['id']}")
    check("a compact task is still one line of title and one of meta",
          row["text"]["text"].count("\n") == 1, row["text"]["text"])
    check("the summary with the week is still where the board lands",
          "last 7d" in json.dumps(compact["blocks"][-3:]))

    # --- the dashboard ----------------------------------------------------------
    handle = _bot_func("handle_tasks", task_store=ts, tasks=T,
                       log=logging.getLogger("t"),
                       _with_costs=None, _spend=None)
    ns = {"task_store": ts, "costs": costs, "board": __import__("board"),
          "research": __import__("research"), "slacklinks": __import__("slacklinks"),
          "tasks": T}
    lift({"_with_costs", "_spend"}, ns=ns)
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


def _run_task_seeing_marker(role, thread_sid, task_sid=None):
    """Drive execute_task once and say what the thread's in-flight marker and
    the runner were told while the run was going."""
    import threading
    import roles
    import recovery
    import tasks as T
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    sessions = tmp_store()
    if thread_sid:
        sessions.update("C1:1.0", session_id=thread_sid)
    root = Path(tempfile.mkdtemp())
    rec = st.create("look", role=role, project="p", thread="C1:1.0",
                    driver="queue", isolate=False, scope={"cwd": str(root)})
    if task_sid:
        st.update(rec["id"], session_id=task_sid)
    seen = {}
    from unittest.mock import MagicMock
    progress = MagicMock()
    progress.return_value.ts = "p1"          # the marker is saved as JSON

    def run_turn(goal, **kw):
        seen["marker"] = dict((sessions.get("C1:1.0") or {}).get("pending") or {})
        seen["kw"] = kw
        named = kw.get("session_id") or kw.get("new_session_id")
        kw["on_init"](named)
        seen["after_init"] = dict((sessions.get("C1:1.0") or {}).get("pending") or {})
        return types.SimpleNamespace(text="done", cost_usd=0.1, duration_ms=1,
                                     session_id=named)
    fn = _bot_func("execute_task", tasks=T, task_store=st, store=sessions,
                   roles=roles, recovery=recovery, Path=Path, run_turn=run_turn,
                   uuid=__import__("uuid"), time=time,
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
                   COST_NOTE="(list)", ClaudeStopped=ClaudeStopped, ClaudeError=ClaudeError,
                   ProgressMessage=progress)
    st.transition(rec["id"], T.RUNNING, "claimed")
    fn(st.get(rec["id"]))
    return seen


def test_a_running_task_run_names_its_session_on_its_marker():
    """The dashboard follows a running turn by its marker's session id: the
    transcript to stream and the claude process to look for. A queue run
    never wrote one, so every running task run read as stalled."""
    print("\na running task run's marker names its session")
    import visualizer as V
    for role, thread_sid, why in (("reviewer", "parent-sid",
                                   "a review on its implementor's thread"),
                                  ("implementor", None, "a fresh implementor")):
        seen = _run_task_seeing_marker(role, thread_sid)
        sid = seen.get("marker", {}).get("session_id")
        check(f"{why}: the marker names a session while it runs", bool(sid), str(seen.get("marker")))
        check(f"{why}: the one the runner is told to name it",
              sid and seen["kw"].get("new_session_id") == sid
              and not seen["kw"].get("session_id"), str(seen.get("kw", {}).get("new_session_id")))
        check(f"{why}: not the thread's earlier session", sid != "parent-sid")
        check(f"{why}: and Claude reporting it leaves it so",
              seen.get("after_init", {}).get("session_id") == sid)
        started = seen.get("marker", {}).get("started")
        entry = {"session_id": thread_sid, "pending": {"started": started, "session_id": sid}}
        check(f"{why}: so, writing, it reads as running",
              V.turn_health(entry, {sid}, written=lambda s: time.time())["stalled"] is False)

    seen = _run_task_seeing_marker("assistant", "thread-sid")
    check("a resumed run's marker names the session it resumes",
          seen.get("marker", {}).get("session_id") == "thread-sid"
          and seen["kw"].get("session_id") == "thread-sid", str(seen.get("marker")))


def test_every_turn_start_names_the_session_it_runs():
    """handle_prompt and execute_task both hand run_turn the session id the
    marker was written with -- the same write, not a later one."""
    print("\nevery turn start names its session on the marker")
    tree = ast.parse((BASE / "bot.py").read_text())
    for name in ("handle_prompt", "execute_task"):
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        bound = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign)
                 and isinstance(n.value, ast.Call)
                 and ast.unparse(n.value.func) == "recovery.mark_pending"
                 for t in n.targets if isinstance(t, ast.Name)}
        passed = {ast.unparse(k.value) for n in ast.walk(fn) if isinstance(n, ast.Call)
                  for k in n.keywords if k.arg == "new_session_id"}
        check(f"{name} passes mark_pending's session to the run",
              bool(bound) and bound <= passed, f"{bound} vs {passed}")
    # handle_prompt's retry after a lost transcript starts a fresh session:
    # under the lost id, it would take that session back (its cost, its name).
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "handle_prompt")
    retry = next(h for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler)
                 and "session-lost" in ast.unparse(h))
    src = ast.unparse(retry)
    renamed = src.find("kwargs['new_session_id'] = str(uuid.uuid4())")
    check("the lost-session retry names a new session, and the marker follows",
          -1 < renamed < src.find("run_turn(prompt, session_id=None")
          and "recovery.note_session(store, key, kwargs['new_session_id'], turn=turn_id)" in src,
          src[-400:])


def test_note_session_corrects_only_its_own_turns_marker():
    """A review starting on its implementor's thread writes its own marker;
    the earlier turn's late init must not repoint it at the earlier session."""
    print("\nnote_session corrects only its own turn's marker")
    st = tmp_store()
    sid = recovery.mark_pending(st, "C:1", msg_ts=None, progress_ts=None,
                                session_id=None, prompt="p", turn="review")
    check("a fresh marker gets a session id of its own", len(sid) == 36, sid)
    recovery.note_session(st, "C:1", "implementor-sid", turn="implementor")
    check("another turn's init leaves it",
          st.get("C:1")["pending"]["session_id"] == sid)
    recovery.note_session(st, "C:1", "reported", turn="review")
    check("its own turn's init corrects it",
          st.get("C:1")["pending"]["session_id"] == "reported")
    check("a resumed marker keeps the session it resumes",
          recovery.mark_pending(st, "C:2", msg_ts=None, progress_ts=None,
                                session_id="old", turn="t") == "old")

def test_run_turn_names_a_fresh_session_as_told():
    print("\nrun_turn names a fresh session as told")
    import claude_runner as CR
    got = []

    def popen(cmd, **kw):
        got.append(cmd)
        raise FileNotFoundError
    saved = CR.subprocess.Popen
    CR.subprocess.Popen = popen
    try:
        for kw in ({"new_session_id": "chosen"}, {"session_id": "old", "new_session_id": "chosen"}, {}):
            try:
                CR.run_turn("hi", binary="claude", cwd="/nonexistent-30de", permission_args=[], **kw)
            except CR.ClaudeError:
                pass
    finally:
        CR.subprocess.Popen = saved
    check("fresh: --session-id is the chosen one",
          got[0][got[0].index("--session-id") + 1] == "chosen", str(got[0]))
    check("resumed: --resume, the chosen one ignored",
          "--resume" in got[1] and "chosen" not in got[1], str(got[1]))
    check("none chosen: one is made up",
          len(got[2][got[2].index("--session-id") + 1]) == 36, str(got[2]))


def test_turn_health_reads_silence_not_age():
    print("\nturn health reads transcript silence, not age")
    import visualizer as V

    def entry(mins, sid="s"):
        started = (datetime.now(timezone.utc) - timedelta(minutes=mins)
                   ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        return {"pending": {"started": started, "session_id": sid}}
    now = time.time()
    check("just started, nothing written yet: running",
          V.turn_health(entry(1), {"s"}, written=lambda s: None)["stalled"] is False)
    check("three hours in, wrote ten seconds ago: running",
          V.turn_health(entry(180), {"s"}, written=lambda s: now - 10)["stalled"] is False)
    h = V.turn_health(entry(180), {"s"}, written=lambda s: now - 2 * 3600)
    check("three hours in, silent for two: stalled", h["stalled"] is True and h["silent_s"] >= 7000, str(h))
    check("an earlier session's write does not count for this turn",
          V.turn_health(entry(180), {"s"}, written=lambda s: now - 5 * 3600)["silent_s"] <= 180 * 60 + 5)
    proj = Path(tempfile.mkdtemp())
    main = proj / "-w" / "sid-sub.jsonl"
    (main.parent / "sid-sub" / "subagents").mkdir(parents=True)
    main.write_text("{}\n")
    sub = main.parent / "sid-sub" / "subagents" / "agent-1.jsonl"
    sub.write_text("{}\n")
    os.utime(main, (now - 7200, now - 7200))
    os.utime(sub, (now - 30, now - 30))
    saved = V.find_transcript
    V.find_transcript = lambda s: main if s == "sid-sub" else None
    try:
        check("a subagent writing counts: the turn is waiting on it",
              abs(V.last_write("sid-sub") - (now - 30)) < 2
              and V.turn_health(entry(180, "sid-sub"), {"sid-sub"})["stalled"] is False)
    finally:
        V.find_transcript = saved
    check("no session on the marker: stalled, it has no process to find",
          V.turn_health({"pending": entry(1)["pending"] | {"session_id": None}}, {"s"},
                        written=lambda s: now)["stalled"] is True)

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
    lift({"correct_costs_once"}, ns=ns)
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
    lift({"correct_costs_once"}, ns=broken)
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
    lift({"correct_costs_once"}, ns=ns)
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

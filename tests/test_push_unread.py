"""Web push, unread and notification settings.

Run by tests/run.py with every other tests/test_*.py; see tests/harness.py."""

from harness import *  # noqa: F401,F403


# --- web push to the installed dashboard (push.py) ----------------------------------
# Notifications on the owner's phone: a task that needs them, a reply in a thread.
# The payload is encrypted to the subscription (RFC 8291) and signed for (RFC 8292);
# what sends is decided from task records, once per task and state; and sending is
# never allowed to cost the move or the turn that triggered it.


class _FakePushService:
    """Stands in for push.post: records each request, answers per endpoint."""

    def __init__(self):
        self.calls, self.answers, self.raises = [], {}, set()
        self.lock = __import__("threading").Lock()

    def __call__(self, endpoint, body, authorization, urgency="normal", timeout=10):
        with self.lock:
            self.calls.append({"endpoint": endpoint, "body": body,
                               "auth": authorization, "urgency": urgency})
        if endpoint in self.raises:
            raise OSError("connection refused")
        return self.answers.get(endpoint, 201)


def _push_rig(tmp: Path, settle=0.05, subs=1):
    import push
    import tasks as T
    store = push.PushStore(tmp / "push.json")
    service = _FakePushService()
    ts = T.TaskStore(tmp / "tasks.json")
    notifier = push.Notifier(store, tmp / "vapid", current=ts.get, sender=service,
                             settle=settle)
    ts.on_move = notifier.task_moved
    push.ensure_keys(tmp / "vapid")
    browsers = []
    for _ in range(subs):
        priv, sub = _ua_subscription()
        store.add(sub)
        browsers.append((priv, sub))
    return types.SimpleNamespace(push=push, T=T, store=store, service=service, ts=ts,
                                 notifier=notifier, browsers=browsers)


def _read_pushes(rig):
    """Every notification delivered so far, decrypted as the phone would."""
    out = []
    for call in list(rig.service.calls):
        for priv, sub in rig.browsers:
            if sub["endpoint"] == call["endpoint"]:
                out.append(json.loads(_ua_decrypt(call["body"], priv, sub["keys"]["auth"])))
    return out


def test_push_payload_encryption_matches_rfc8291():
    import push
    from cryptography.hazmat.primitives.asymmetric import ec
    print("\nweb push payloads are encrypted exactly as RFC 8291 says")
    v = push.unb64u
    # RFC 8291, section 5 and appendix A: the worked example, byte for byte.
    as_priv = ec.derive_private_key(int.from_bytes(
        v("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
    body = push.encrypt(
        v("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"),
        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
        "BTBZMqHH6r4Tts7J_aSIgg", as_private=as_priv, salt=v("DGv6ra1nlYgDCS1FRnbzlw"))
    header = v("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
               "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8")
    cipher = v("8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ")
    check("the 86-octet header is the RFC's", body[:86] == header, push.b64u(body[:86]))
    check("and so is the ciphertext", body[86:] == cipher, push.b64u(body[86:]))
    # And a real one, read back by the receiving side.
    priv, sub = _ua_subscription()
    a = push.encrypt(b'{"title":"x"}', sub["keys"]["p256dh"], sub["keys"]["auth"])
    b = push.encrypt(b'{"title":"x"}', sub["keys"]["p256dh"], sub["keys"]["auth"])
    check("a browser holding the subscription's key reads the message",
          _ua_decrypt(a, priv, sub["keys"]["auth"]) == b'{"title":"x"}')
    check("every message gets its own salt and key", a[:16] != b[:16] and a[21:86] != b[21:86])
    try:
        push.encrypt(b"x" * 5000, sub["keys"]["p256dh"], sub["keys"]["auth"])
        refused = False
    except ValueError:
        refused = True
    check("a message too big for one record is refused, not truncated", refused)


def test_push_vapid_jwt_is_es256_raw_and_scoped():
    import push
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    print("\neach push request carries a VAPID JWT the push service can verify")
    d = Path(tempfile.mkdtemp()) / "vapid"
    pub = push.ensure_keys(d)
    h = push.vapid_header("https://web.push.apple.com/QXabc?x=1", d, now=1_000_000)
    check("the header is 'vapid t=<jwt>, k=<public key>'",
          h.startswith("vapid t=") and h.endswith(f", k={pub}"), h[:40])
    jwt = h[len("vapid t="):h.index(", k=")]
    head, claims, sig = jwt.split(".")
    check("ES256, typed JWT", json.loads(push.unb64u(head)) == {"typ": "JWT", "alg": "ES256"})
    c = json.loads(push.unb64u(claims))
    check("audience is the push service's origin only, not the endpoint path",
          c["aud"] == "https://web.push.apple.com", str(c))
    check("it expires within 24h (12h)", c["exp"] == 1_000_000 + 12 * 3600, str(c))
    check("it says who is sending (sub), as Apple requires",
          c["sub"].startswith(("mailto:", "https://")), str(c))
    raw = push.unb64u(sig)
    check("the signature is raw 64-byte r||s (JWS), not DER", len(raw) == 64, str(len(raw)))
    key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), push.unb64u(pub))
    try:
        key.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"),
                                        int.from_bytes(raw[32:], "big")),
                   f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
        ok = True
    except Exception:
        ok = False
    check("and it verifies against the public key the page subscribed with", ok)


def test_push_keys_made_once_and_kept_secret():
    import stat
    import push
    print("\nthe VAPID keypair is made once, outside the repository, readable only by its owner")
    check("the default key directory is outside any repository (~/.silkworm/vapid)",
          push.DEFAULT_KEY_DIR == Path.home() / ".silkworm" / "vapid"
          and BASE not in push.DEFAULT_KEY_DIR.parents, str(push.DEFAULT_KEY_DIR))
    saved = os.environ.get("SILKWORM_VAPID_DIR")
    os.environ["SILKWORM_VAPID_DIR"] = "/elsewhere/vapid"
    try:
        check("SILKWORM_VAPID_DIR is read when asked (the bot loads .env after imports)",
              push.key_dir_default() == Path("/elsewhere/vapid"))
    finally:
        if saved is None:
            os.environ.pop("SILKWORM_VAPID_DIR")
        else:
            os.environ["SILKWORM_VAPID_DIR"] = saved
    d = Path(tempfile.mkdtemp()) / "nested" / "vapid"
    check("no keys before the first ask", not push.keys_present(d) and push.public_key(d) == "")
    records = []

    class Grab(logging.Handler):
        def emit(self, r):
            records.append(r.getMessage())
    grab = Grab(level=logging.DEBUG)
    lg = logging.getLogger("silkworm.push")
    old_level = lg.level
    lg.setLevel(logging.DEBUG)
    lg.addHandler(grab)
    try:
        pub = push.ensure_keys(d)
        again = push.ensure_keys(d)
        status = push.handle({"action": "status"}, push.PushStore(d.parent / "p.json"),
                             push.Notifier(None, d), key_dir=d)
    finally:
        lg.removeHandler(grab)
        lg.setLevel(old_level)
    check("made once: a second ask returns the same key", pub == again and push.keys_present(d))
    priv = (d / push.PRIVATE_FILE)
    check("the private key is mode 600", stat.S_IMODE(priv.stat().st_mode) == 0o600,
          oct(priv.stat().st_mode))
    check("in a mode 700 directory", stat.S_IMODE(d.stat().st_mode) == 0o700,
          oct(d.stat().st_mode))
    pem = priv.read_text()
    secret_lines = [ln for ln in pem.splitlines() if ln and not ln.startswith("-----")]
    said = "\n".join(records) + json.dumps(status)
    check("the private key never appears in the log or in status",
          all(ln not in said for ln in secret_lines), said[:300])
    check("status says where the keys are, not what they are",
          status["keys"] is True and status["key_dir"] == str(d)
          and "BEGIN" not in json.dumps(status), str(status))
    import shutil as _sh
    _sh.copy(priv, d.parent / "kept.pem")
    (d / push.PUBLIC_FILE).unlink()
    check("a lost public file is rebuilt from the private key, not a new pair",
          push.ensure_keys(d) == pub and (d / push.PRIVATE_FILE).read_text() == pem)


def test_push_subscriptions_stored_and_pruned():
    import push
    print("\nsubscriptions are checked, kept atomically, and dropped when the service says gone")
    tmp = Path(tempfile.mkdtemp())
    store = push.PushStore(tmp / "push.json")
    priv, sub = _ua_subscription()
    for bad, why in (({**sub, "endpoint": "http://web.push.apple.com/x"}, "https"),
                     ({**sub, "endpoint": "https://127.0.0.1:8787/web-message"}, "not a push service"),
                     ({**sub, "endpoint": "https://evil.example/push.apple.com"}, "not a push service"),
                     ({**sub, "endpoint": "https://notpush.apple.com.evil.example/x"},
                      "not a push service"),
                     ({**sub, "endpoint": "https://evilpush.apple.com/x"}, "not a push service"),
                     ({**sub, "keys": {"p256dh": "AAAA", "auth": sub["keys"]["auth"]}}, "P-256"),
                     ({**sub, "keys": {"p256dh": sub["keys"]["p256dh"], "auth": "AAAA"}}, "16 bytes"),
                     ("nope", "no subscription")):
        try:
            store.add(bad)
            got = "taken"
        except push.BadSubscription as e:
            got = str(e)
        check(f"refused ({why})", why in got, got)
    check("nothing refused was kept", store.count() == 0)
    for host in ("web.push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com"):
        check(f"a {host} subscription is taken", bool(store.add(_ua_subscription(host)[1])))
    store.add(sub, "iPhone Safari")
    store.add(sub, "iPhone Safari")
    check("the same subscription twice is one", store.count() == 4, str(store.count()))
    again = push.PushStore(tmp / "push.json")
    check("they are on disk, and read back", again.count() == 4
          and any(s["endpoint"] == sub["endpoint"] and s["agent"] == "iPhone Safari"
                  for s in again.subscriptions()))
    check("saved through jsonstore (atomic, with a .prev backup)",
          (tmp / "push.json.prev").exists())
    src = (BASE / "push.py").read_text()
    check("push.py never writes its state file in place",
          "write_text(json" not in src and "jsonstore.save(self._path" in src)

    # Sending: one gone (410), one unknown (404), one failing (500), one raising.
    rig = _push_rig(Path(tempfile.mkdtemp()), subs=5)
    eps = [s["endpoint"] for _, s in rig.browsers]
    rig.service.answers = {eps[0]: 410, eps[1]: 404, eps[2]: 500}
    rig.service.raises = {eps[3]}
    out = rig.notifier.send_all({"title": "t", "body": "b", "url": "/", "tag": "x"})
    check("410 and 404 are pruned, the rest counted",
          out == {"sent": 1, "pruned": 2, "failed": 2, "skipped": ""}, str(out))
    left = {s["endpoint"] for s in push.PushStore(Path(rig.store._path)).subscriptions()}
    check("the gone ones are dropped from push.json; a 500 or a network error is not",
          left == {eps[2], eps[3], eps[4]}, str(left))
    check("forget-all drops the rest", push.handle({"action": "forget-all"}, rig.store,
                                                    rig.notifier)["forgot"] == 3
          and rig.store.count() == 0)
    for _ in range(push.MAX_SUBSCRIPTIONS + 3):
        rig.store.add(_ua_subscription()[1])
    check("subscriptions are capped", rig.store.count() == push.MAX_SUBSCRIPTIONS)


def test_push_needs_you_notifies_once_per_task_and_state():
    print("\na task needing you notifies once per state, with a short title and no goal text")
    rig = _push_rig(Path(tempfile.mkdtemp()))
    T, ts = rig.T, rig.ts
    t = ts.create("SECRET-GOAL-TEXT do the thing with token sk-abc", title="Fix the header",
                  project="cadence")
    ts.transition(t["id"], T.RUNNING)
    ts.transition(t["id"], T.AWAITING_APPROVAL, "reviewed")
    _wait(lambda: len(rig.service.calls) >= 1)
    got = _read_pushes(rig)
    check("awaiting_approval sends one notification", len(got) == 1, str(got))
    check("titled by what it needs, with its project", got and got[0]["title"]
          == "Ready for your approval · cadence", str(got))
    check("its line is the task's title; nothing of its goal",
          got and got[0]["body"] == "Fix the header"
          and "SECRET" not in json.dumps(got) and "sk-abc" not in json.dumps(got), str(got))
    check("a tap opens that task", got and got[0]["url"] == f"/?task={t['id']}"
          and got[0]["tag"] == f"task-{t['id']}", str(got))
    check("sent urgently, with a VAPID header",
          rig.service.calls[0]["urgency"] == "high"
          and rig.service.calls[0]["auth"].startswith("vapid t="))

    # A loop: sent back, fails, retried, fails again, ... notifies once.
    ts.transition(t["id"], T.QUEUED, "sent back")
    for _ in range(3):
        ts.transition(t["id"], T.RUNNING)
        ts.transition(t["id"], T.FAILED, "claude exited 1")
        time.sleep(0.12)
        ts.transition(t["id"], T.QUEUED, "retry")
    ts.transition(t["id"], T.RUNNING)
    ts.transition(t["id"], T.FAILED, "claude exited 1")
    _wait(lambda: len(rig.service.calls) >= 2)
    time.sleep(0.4)
    states = [p["title"] for p in _read_pushes(rig)]
    check("four failures in a loop: one 'Failed' notification",
          states.count("Failed · cadence") == 1, str(states))
    ts.transition(t["id"], T.QUEUED)
    ts.transition(t["id"], T.RUNNING)
    ts.transition(t["id"], T.AWAITING_APPROVAL)
    time.sleep(0.4)
    check("and a state it already notified for is not sent again",
          len(rig.service.calls) == 2, str([p["title"] for p in _read_pushes(rig)]))
    ts.transition(t["id"], T.NEEDS_INPUT, "yours to do: !release cadence")
    _wait(lambda: len(rig.service.calls) >= 3)
    check("a different state of the same task is its own notification",
          _read_pushes(rig)[-1]["title"] == "Needs your input · cadence")
    check("the record of what was sent survives a restart",
          not rig.push.PushStore(Path(rig.store._path)).claim(f"task:{t['id']}:failed"))

    for state in (T.QUEUED, T.RUNNING, T.BLOCKED, T.DONE, T.CANCELLED, T.PROPOSED):
        check(f"{state} never notifies", rig.push.needs_you({"id": "x", "state": state}) is None)
    check("exactly the needs-you states notify, proposals aside (they are filed, not reached)",
          set(rig.push.NEEDS_YOU) == set(T.NEEDS_ATTENTION) - {T.PROPOSED})


def test_push_never_for_transient_or_requeue():
    print("\nquota/overload parking, a restart's requeue, and a task that moved on send nothing")
    rig = _push_rig(Path(tempfile.mkdtemp()))
    T, ts = rig.T, rig.ts
    hold = types.SimpleNamespace(close=lambda *a: None, open=lambda: None)
    fail_or_retry = _bot_func("fail_or_retry", task_store=ts, tasks=T,
                              retry=__import__("retry"), RUNNER_HOLD=hold)
    quota = ts.create("q", title="quota hit")
    ts.transition(quota["id"], T.RUNNING)
    parked = fail_or_retry(quota["id"], "Claude usage limit reached; resets 3pm", started=True)
    over = ts.create("o", title="overloaded")
    ts.transition(over["id"], T.RUNNING)
    parked2 = fail_or_retry(over["id"], "API Error: 529 overloaded_error", started=True)
    time.sleep(0.4)
    check("a quota limit and an overload are parked to retry (blocked)",
          parked and parked2 and ts.get(quota["id"])["state"] == T.BLOCKED
          and ts.get(over["id"])["state"] == T.BLOCKED)
    check("and neither notifies", rig.service.calls == [], str(_read_pushes(rig)))

    real = ts.create("r", title="really broken")
    ts.transition(real["id"], T.RUNNING)
    if not fail_or_retry(real["id"], "claude exited 1: no such file", started=True):
        ts.transition(real["id"], T.FAILED, "claude exited 1: no such file")
    _wait(lambda: rig.service.calls)
    check("a real failure does", [p["title"] for p in _read_pushes(rig)] == ["Failed"],
          str(_read_pushes(rig)))

    n = len(rig.service.calls)
    q = ts.create("queue work", title="interrupted", driver="queue")
    ts.transition(q["id"], T.RUNNING)
    moved = ts.requeue_interrupted()
    time.sleep(0.4)
    check("a restart's failed-then-requeued is not a failure anyone hears about",
          moved >= 1 and ts.get(q["id"])["state"] == T.QUEUED and len(rig.service.calls) == n,
          str(_read_pushes(rig)[n:]))
    # Each guard on its own, not only together: the store says the restart's
    # "failed" is not a real ending, and the notifier listens to that even
    # when the task would still read as failed.
    told = []
    ts2 = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    ts2.on_move = lambda rec, prev, settled: told.append((rec["state"], settled))
    q2 = ts2.create("queue work", title="interrupted", driver="queue")
    ts2.transition(q2["id"], T.RUNNING)
    ts2.requeue_interrupted()
    check("requeue_interrupted tells on_move its 'failed' is not settled",
          (T.FAILED, False) in told and (T.QUEUED, True) in told, str(told))
    lone = _push_rig(Path(tempfile.mkdtemp()))
    lone.notifier.current = lambda tid: {"state": T.FAILED}
    lone.notifier.task_moved({"id": "tsk_x", "state": T.FAILED, "title": "x"}, T.RUNNING, False)
    lone.notifier.task_moved({"id": "tsk_y", "state": T.FAILED, "title": "y"}, T.RUNNING, True)
    _wait(lambda: lone.service.calls)
    time.sleep(0.3)
    check("an unsettled move is not sent even if the task still reads as failed",
          [p["body"] for p in _read_pushes(lone)] == ["y"], str(_read_pushes(lone)))
    r = ts.create("r2", title="retried at once")
    ts.transition(r["id"], T.RUNNING)
    ts.transition(r["id"], T.FAILED, "boom")
    ts.transition(r["id"], T.QUEUED, "retry")     # within the settle window
    time.sleep(0.4)
    check("a failure already retried by the time it would send is not sent",
          len(rig.service.calls) == n, str(_read_pushes(rig)[n:]))


def test_push_failure_never_affects_a_transition():
    import push
    import tasks as T
    print("\nnotifying can fail any way it likes: the move stands and nothing raises")
    tmp = Path(tempfile.mkdtemp())
    ts = T.TaskStore(tmp / "tasks.json")

    def boom(*a, **k):
        raise RuntimeError("push exploded")
    ts.on_move = boom
    t = ts.create("g", title="x")
    ts.transition(t["id"], T.RUNNING)
    try:
        moved = ts.transition(t["id"], T.FAILED, "real")
        raised = None
    except Exception as e:
        moved, raised = None, e
    check("an on_move that raises does not fail the transition",
          raised is None and moved and moved["state"] == T.FAILED, repr(raised))
    check("and the move is on disk", T.TaskStore(tmp / "tasks.json").get(t["id"])["state"] == T.FAILED)

    class BrokenStore:
        def count(self):
            raise OSError("disk gone")
    n = push.Notifier(BrokenStore(), tmp / "vapid")
    try:
        n.task_moved({"id": "a", "state": T.FAILED}, T.RUNNING, True)
        n.reply("D1:1.1", "tsk_1", "t")
        ok = True
    except Exception:
        ok = False
    check("the notifier's entry points never raise", ok)

    rig = _push_rig(Path(tempfile.mkdtemp()))
    rig.service.raises = {s["endpoint"] for _, s in rig.browsers}
    t = rig.ts.create("g", title="unlucky")
    rig.ts.transition(t["id"], T.RUNNING)
    start = time.time()
    moved = rig.ts.transition(t["id"], T.AWAITING_APPROVAL)
    took = time.time() - start
    _wait(lambda: rig.service.calls)
    check("a push service that fails does not fail the move",
          moved["state"] == T.AWAITING_APPROVAL and rig.service.calls, str(moved["state"]))
    check("and the move does not wait on sending (it is queued, off the hot path)",
          took < 0.05, f"{took:.3f}s")
    slow = _push_rig(Path(tempfile.mkdtemp()), settle=0)
    gate = __import__("threading").Event()
    slow.notifier.sender = lambda *a, **k: gate.wait(5) and 201
    t = slow.ts.create("g", title="slow")
    slow.ts.transition(t["id"], T.RUNNING)
    start = time.time()
    slow.ts.transition(t["id"], T.FAILED)
    took = time.time() - start
    gate.set()
    check("even a push service that hangs", took < 0.05, f"{took:.3f}s")
    held = _push_rig(Path(tempfile.mkdtemp()))
    with held.store._lock:                   # a push.json save under way
        t = held.ts.create("g", title="mid-save")
        held.ts.transition(t["id"], T.RUNNING)
        start = time.time()
        done = []
        th = __import__("threading").Thread(
            target=lambda: done.append(held.ts.transition(t["id"], T.FAILED)))
        th.start(); th.join(1.0)
        took = time.time() - start
    check("nor a push.json save in progress", done and took < 0.5, f"{took:.3f}s")
    check("an unreadable push.json turns push off, not the bot",
          "push_store = None" in (BASE / "bot.py").read_text()
          and push.handle({"action": "test"}, None, push.Notifier(None))["ok"] is False)


def test_push_reply_on_finished_turn_not_quiet_wake():
    import push
    import defer
    import tasks as T
    print("\na finished reply notifies; a scheduled check that found nothing does not")
    m = push.reply_message("D1:5.5", "tsk_1", "Kitchen plans", "here is the plan", "slack")
    check("a reply names the thread, opens it, and carries none of its text",
          m and m["body"] == "Kitchen plans" and m["url"] == "/?thread=D1:5.5"
          and m["tag"] == "thread-D1:5.5" and "plan" not in m["title"], str(m))
    check("one per turn", m["dedupe"] == "reply:tsk_1")
    check("a wake-up that found nothing sends nothing",
          push.reply_message("D1:5.5", "tsk_2", "x", f"  {defer.QUIET}\n", T.WAKE_SOURCE) is None)
    check("a wake-up with news does",
          push.reply_message("D1:5.5", "tsk_3", "x", "the build finished", T.WAKE_SOURCE))

    tree = ast.parse((BASE / "bot.py").read_text())
    ex = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "execute_task")
    quiet = [n for n in ast.walk(ex) if isinstance(n, ast.If) and "defer.QUIET" in ast.unparse(n.test)]
    check("execute_task has its quiet-wake branch", len(quiet) == 1)
    if quiet:
        body = ast.Module(body=quiet[0].body, type_ignores=[])
        check("which notifies nothing and returns",
              "PUSH" not in ast.unparse(body) and isinstance(quiet[0].body[-1], ast.Return))
    calls = [n for n in ast.walk(ex) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == "PUSH.reply"]
    guard = [n for n in ast.walk(ex) if isinstance(n, ast.If)
             and "WAKE_SOURCE" in ast.unparse(n.test)
             and any(ast.unparse(c.func) == "PUSH.reply" for c in ast.walk(n)
                     if isinstance(c, ast.Call))]
    check("execute_task notifies a reply only for a wake-up, passing its text and source",
          len(calls) == 1 and len(guard) == 1
          and {k.arg for k in calls[0].keywords} >= {"text", "source"}, ast.unparse(ex)[:0])
    check("and only one that ended done: one that parked for you has its needs-you instead",
          guard and "tasks.DONE" in ast.unparse(guard[0].test), guard and ast.unparse(guard[0].test))

    # handle_prompt, as shipped: a finished turn notifies once; a failed one
    # is the failed task's notification, not a reply.
    tmp = Path(tempfile.mkdtemp())
    bot = _web_bot(tmp)
    key = "D1:700.1"
    bot.store.update(key, session_id="sid-x", cwd=str(tmp), title="Garden")
    r = bot.web({"key": key, "text": "what next"})
    _wait(lambda: bot.pushed)
    check("a finished turn in any thread notifies a reply, naming the thread",
          len(bot.pushed) == 1 and bot.pushed[0]["args"][0] == key
          and bot.pushed[0]["args"][2] == "Garden", str(bot.pushed))
    task = [t for t in bot.tasks.by_thread(key) if t["source_ref"] == r["ref"]]
    check("for its own task", task and bot.pushed[0]["args"][1] == task[0]["id"], str(task)[:200])
    n = len(bot.pushed)
    r2 = bot.web({"key": key, "text": "make it fail"})
    _wait(lambda: any(t["state"] == T.FAILED for t in bot.tasks.by_thread(key)))
    time.sleep(0.2)
    check("a failed turn does not notify a reply", len(bot.pushed) == n, str(bot.pushed[n:]))
    src = (BASE / "bot.py").read_text()
    check("the task store tells the notifier of every move",
          "task_store.on_move = PUSH.task_moved" in src)


def test_viz_push_route_is_gated():
    import threading
    import urllib.error
    import urllib.request
    import visualizer as V
    print("\n/api/push is behind the tailnet identity gate and the cross-site rules")
    saved_env = os.environ.pop("VIZ_TAILSCALE_LOGIN", None)
    saved = (V.LOOPBACK, V.TOKEN, V.ENV_FILE, V.bot_call)
    sent = []
    srv = None
    try:
        V.LOOPBACK, V.TOKEN = True, ""
        V.ENV_FILE = Path(tempfile.mkdtemp()) / "no.env"
        V.bot_call = lambda path, payload, timeout=1.0: sent.append((path, payload, timeout)) or {"ok": True}
        os.environ["VIZ_TAILSCALE_LOGIN"] = "owner@example.com"
        srv = V.make_server("127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]

        def post(body, **headers):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/push", method="POST",
                                         data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", **headers})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()
        serve = {"X-Forwarded-For": "100.64.0.2", "X-Forwarded-Host": "mini.tail1.ts.net"}
        sub = {"action": "subscribe", "subscription": {"endpoint": "https://web.push.apple.com/x"}}
        code, _ = post(sub, **serve, **{"Tailscale-User-Login": "intruder@example.com"})
        check("another tailnet login cannot subscribe", code == 403 and not sent, f"{code} {sent}")
        code, _ = post(sub, **serve)
        check("nor can a proxied request with no identity", code == 403 and not sent)
        code, _ = post(sub, **serve, **{"Tailscale-User-Login": "owner@example.com",
                                        "Sec-Fetch-Site": "cross-site"})
        check("nor a cross-site page in the owner's own browser", code == 403 and not sent)
        code, _ = post(sub, **serve, **{"Tailscale-User-Login": "owner@example.com",
                                        "Content-Type": "text/plain"})
        check("nor a no-preflight text/plain POST", code == 403 and not sent)
        code, body = post(sub, **serve, **{"Tailscale-User-Login": "owner@example.com",
                                           "Sec-Fetch-Site": "same-origin",
                                           "Origin": "https://mini.tail1.ts.net"})
        check("the owner's own page subscribes, relayed to the bot's /push",
              code == 200 and sent and sent[-1][0] == "/push" and sent[-1][1] == sub, f"{code} {sent}")
        post({"action": "test"})
        check("a test is given time to reach every device", sent[-1][2] >= 30, str(sent[-1]))
        check("the route is one the gate tests enumerate",
              "/api/push" in _viz_routes("do_POST"))
    finally:
        V.LOOPBACK, V.TOKEN, V.ENV_FILE, V.bot_call = saved
        if srv is not None:
            srv.shutdown(); srv.server_close()
        if saved_env is None:
            os.environ.pop("VIZ_TAILSCALE_LOGIN", None)
        else:
            os.environ["VIZ_TAILSCALE_LOGIN"] = saved_env


def test_viz_send_timeout_is_unknown_not_offline():
    import socket
    import threading
    import visualizer as V
    print("\n/api/send tells 'the bot did not answer in time' from 'the bot is offline'")
    saved = V.BOT_PORT
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(5)
    held = []

    def accept():                           # takes the request, never answers
        while True:
            try:
                c, _ = lsock.accept()
            except OSError:
                return
            held.append(c)
    threading.Thread(target=accept, daemon=True).start()
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()
    try:
        V.BOT_PORT = str(lsock.getsockname()[1])
        slow = V.send_answer(V.bot_call("/web-message", {"text": "x"}, timeout=0.3))
        check("a bot that took it and did not answer in time: outcome unknown",
              slow.get("ok") is False and slow.get("unknown") is True
              and "offline" not in slow.get("error", ""), str(slow))
        V.BOT_PORT = str(dead_port)
        off = V.send_answer(V.bot_call("/web-message", {"text": "x"}, timeout=0.3))
        check("a bot that is not there: offline, definitely not sent",
              off == {"ok": False, "error": "bot is offline"}, str(off))
        check("an answer is passed through", V.send_answer({"ok": True, "key": "k"}) == {"ok": True, "key": "k"})
        # Reached, then the connection dropped with no answer: it may have acted.
        drop = socket.socket()
        drop.bind(("127.0.0.1", 0))
        drop.listen(5)

        def hang_up():
            while True:
                try:
                    c, _ = drop.accept()
                except OSError:
                    return
                c.recv(65536)
                c.close()
        threading.Thread(target=hang_up, daemon=True).start()
        V.BOT_PORT = str(drop.getsockname()[1])
        dropped = V.send_answer(V.bot_call("/web-message", {"text": "x"}, timeout=2))
        drop.close()
        check("a connection dropped after the bot took it: unknown, not offline",
              dropped.get("unknown") is True, str(dropped))
        empty = socket.socket()
        empty.bind(("127.0.0.1", 0))
        empty.listen(5)

        def answer_empty():
            while True:
                try:
                    c, _ = empty.accept()
                except OSError:
                    return
                c.recv(65536)
                c.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
                          b"Content-Length: 2\r\n\r\n{}")
                c.close()
        threading.Thread(target=answer_empty, daemon=True).start()
        V.BOT_PORT = str(empty.getsockname()[1])
        blank = V.send_answer(V.bot_call("/web-message", {"text": "x"}, timeout=2))
        empty.close()
        check("an empty answer (the bot's handler raised) is unknown too",
              blank.get("unknown") is True, str(blank))
        check("/api/send relays with the shared wait and answers through send_answer",
              "bot_call(\"/web-message\", payload, timeout=SEND_WAIT_S)" in (BASE / "visualizer.py").read_text()
              and "self._json(send_answer(answer))" in (BASE / "visualizer.py").read_text())
    finally:
        V.BOT_PORT = saved
        lsock.close()
        for c in held:
            c.close()


def test_dashboard_resend_makes_one_thread():
    import threading
    import webthread
    print("\nsending the same message again under its client id makes one thread, not two")
    tmp = Path(tempfile.mkdtemp())
    bot = _web_bot(tmp)
    real_post = bot.client.chat_postMessage
    slow = threading.Event()

    def slow_post(channel, text, thread_ts=None, **kw):
        if thread_ts is None:                # the new thread's parent: Slack is slow
            slow.wait(3)
        return real_post(channel, text, thread_ts=thread_ts, **kw)
    bot.client.chat_postMessage = slow_post
    payload = {"new": True, "text": "plan the garden", "client_id": "c1d2e3f4-aaaa"}
    answers = []
    first = threading.Thread(target=lambda: answers.append(bot.web(dict(payload))))
    first.start()
    time.sleep(0.2)                          # the dashboard gave up; the page sends again
    second = threading.Thread(target=lambda: answers.append(bot.web(dict(payload))))
    second.start()
    time.sleep(0.2)
    slow.set()
    first.join(5); second.join(5)
    parents = [p for p in bot.posted if p.get("channel") == "D9" and p.get("thread_ts") is None]
    check("both answers name the same thread", len(answers) == 2 and answers[0]["ok"]
          and answers[1]["ok"] and answers[0]["key"] == answers[1]["key"]
          and answers[0]["ref"] == answers[1]["ref"], str(answers))
    check("the repeat is marked as one", sum(bool(a.get("duplicate")) for a in answers) == 1,
          str(answers))
    _wait(lambda: bot.prompts)
    time.sleep(0.3)
    check("Slack got one parent, and one turn ran", len(parents) == 1 and len(bot.prompts) == 1,
          f"{len(parents)} parents, {len(bot.prompts)} turns")
    later = bot.web(dict(payload))
    check("a repeat after it finished is answered from it, posting nothing",
          later["key"] == answers[0]["key"] and later.get("duplicate")
          and len([p for p in bot.posted if p.get("channel") == "D9"
                   and "thread_ts" in p and p["thread_ts"] is None]) == 1, str(later))

    # Continuing a thread: the same.
    key = answers[0]["key"]
    n = len(bot.prompts)
    one = {"key": key, "text": "and the shed", "client_id": "zz-continue-1"}
    a, b = bot.web(dict(one)), bot.web(dict(one))
    _wait(lambda: len(bot.prompts) > n)
    time.sleep(0.3)
    check("a reply sent twice is one message and one turn",
          a["ref"] == b["ref"] and len(bot.prompts) == n + 1
          and sum("_via visualizer:_ and the shed" in (p.get("text") or "") for p in bot.posted) == 1,
          f"{a} {b} {len(bot.prompts) - n}")
    # Refused, then tried again: a failure is not remembered as the answer.
    bot.channel["home"] = ""
    fail = bot.web({"new": True, "text": "x", "client_id": "retry-after-fail"})
    bot.channel["home"] = "D9"
    retry = bot.web({"new": True, "text": "x", "client_id": "retry-after-fail"})
    check("a send that failed is tried again under the same id, not answered from the failure",
          fail["ok"] is False and retry["ok"] is True and not retry.get("duplicate"),
          f"{fail} {retry}")
    # Broke partway, after Slack had the thread: kept as unknown, so a repeat
    # cannot start a second one.
    real_add = bot.store.add_file
    bot.store.add_file = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    import base64 as _b64
    files = [{"name": "a.png", "type": "image/png", "data": _b64.b64encode(b"PNG").decode()}]
    parents = lambda: len([p for p in bot.posted if p.get("channel") == "D9"
                           and "thread_ts" in p and p["thread_ts"] is None])
    n_par = parents()
    broke = bot.web({"new": True, "text": "z", "files": files, "client_id": "broke-partway"})
    bot.store.add_file = real_add
    again = bot.web({"new": True, "text": "z", "files": files, "client_id": "broke-partway"})
    check("a send that broke partway is unknown, and a repeat does not start another thread",
          broke.get("unknown") is True and again.get("unknown") is True
          and again.get("duplicate") and parents() == n_par + 1, f"{broke} {again} {parents() - n_par}")
    check("without an id every send is new, as before",
          bot.web({"new": True, "text": "y"})["key"] != bot.web({"new": True, "text": "y"})["key"])
    check("an id that is not one is refused",
          "client_id" in bot.web({"new": True, "text": "y", "client_id": "../x"}).get("error", ""))
    # Still under way past the wait: said so, not guessed.
    sends = webthread.Sends()
    first, slot = sends.begin("abcdefgh")
    again, slot2 = sends.begin("abcdefgh")
    check("a repeat while the first is still going waits, then says pending",
          first and not again and webthread.Sends.wait(slot2, 0.05) is None)
    sends.finish("abcdefgh", slot, {"ok": True, "key": "D1:1.1"})
    check("and is given the first answer once there is one",
          webthread.Sends.wait(slot2, 0.05) == {"ok": True, "key": "D1:1.1"})
    old = webthread.Sends(ttl=10)
    old.begin("abcdefgh", now=100)
    check("ids are forgotten after their time", old.begin("abcdefgh", now=200)[0] is True)


SW_PUSH_HARNESS = r"""
const listeners = {}, shown = [], opened = [], posted = [], focused = [];
let wins = [];
globalThis.self = {
  addEventListener: (k, f) => { listeners[k] = f; },
  skipWaiting() {},
  registration: {showNotification: async (title, opts) => { shown.push({title, opts}); }},
  clients: {
    claim: async () => {},
    matchAll: async opts => { globalThis.__matchOpts = opts; return wins; },
    openWindow: async url => { opened.push(url); return {}; },
  },
};
globalThis.caches = {keys: async () => [], delete: async () => true};
""" + "%SW%" + r"""
async function push(data) {
  let w = null;
  listeners.push({data: data === undefined ? null : {json: () => JSON.parse(data), text: () => data},
                  waitUntil: p => { w = p; }});
  await w;
}
async function click(url) {
  let w = null, closed = false;
  listeners.notificationclick({notification: {data: {url}, close() { closed = true; }},
                               waitUntil: p => { w = p; }});
  await w;
  return closed;
}
(async () => {
  const out = {};
  await push(JSON.stringify({title: "Ready for your approval · cadence", body: "Fix the header",
                             url: "/?task=tsk_1", tag: "task-tsk_1"}));
  await push("not json at all");
  await push(undefined);
  await push(JSON.stringify({title: "x", url: "https://evil.example/"}));
  await push(JSON.stringify({title: "x", url: "//evil.example/"}));
  await push(JSON.stringify({title: "x", url: "javascript:alert(1)"}));
  await push(JSON.stringify({title: "x", url: "/\\evil.example"}));
  out.shown = shown;
  // A tap with the app open: focused and told; nothing new opened.
  wins = [{postMessage: m => posted.push(m), focus: async () => { focused.push(1); }}];
  out.closedOpen = await click("/?thread=D1:5.5");
  out.whenOpen = {posted: posted.slice(), focused: focused.length, opened: opened.slice(),
                  match: globalThis.__matchOpts};
  // A tap with it closed: opened on the thread.
  wins = [];
  out.closedCold = await click("/?thread=D1:5.5");
  out.whenClosed = {opened: opened.slice()};
  await click("https://evil.example/");
  out.evilOpened = opened[opened.length - 1];
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


def test_sw_push_and_notificationclick():
    import visualizer as V
    print("\nthe service worker shows a push and a tap opens or focuses the app on its thread or task")
    sw = V.SERVICE_WORKER.decode()
    check("the worker's only cache write is the offline page, into this build's cache",
          sw.count(".put(") == 1 and "put(OFFLINE_KEY, offlinePage())" in sw
          and sw.count("caches.open(") == 1 and "caches.open(CACHE)" in sw and "addAll(" not in sw)
    node = shutil.which("node")
    if not node:
        print("  … node not installed — the worker's push handlers checked by text only")
        return
    r = subprocess.run([node, "-e", SW_PUSH_HARNESS.replace("%SW%", sw)],
                       capture_output=True, text=True, timeout=60)
    check("the worker's push handlers run under a stub scope", r.returncode == 0, r.stderr[-600:])
    if r.returncode:
        return
    o = json.loads(r.stdout)
    first = o["shown"][0]
    check("a push is shown with its title, line and tag",
          first["title"] == "Ready for your approval · cadence"
          and first["opts"]["body"] == "Fix the header" and first["opts"]["tag"] == "task-tsk_1"
          and first["opts"]["data"]["url"] == "/?task=tsk_1", str(first))
    check("every push shows something, even one that is not JSON or is empty (iOS requires it)",
          len(o["shown"]) == 7 and o["shown"][1]["opts"]["body"] == "not json at all"
          and o["shown"][2]["title"] == "Silkworm", str(o["shown"][1:3]))
    check("a url that leaves the dashboard becomes /",
          [s["opts"]["data"]["url"] for s in o["shown"][3:]] == ["/", "/", "/", "/"],
          str([s["opts"]["data"] for s in o["shown"][3:]]))
    w = o["whenOpen"]
    check("tapped with the app open: it is focused and told which thread to open",
          w["posted"] == [{"type": "open", "url": "/?thread=D1:5.5"}] and w["focused"] == 1
          and w["opened"] == [] and o["closedOpen"] is True, str(w))
    check("every window is looked at, controlled or not",
          w["match"] == {"type": "window", "includeUncontrolled": True}, str(w["match"]))
    check("tapped with it closed: the app opens on that thread",
          o["whenClosed"]["opened"] == ["/?thread=D1:5.5"] and o["closedCold"] is True, str(o))
    check("and never anywhere else", o["evilOpened"] == "/", str(o["evilOpened"]))


PAGE_PUSH_DRIVER = r"""
// The page's notification control, the deep links a notification opens, and
// a send whose outcome is unknown -- against a stub DOM.
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const scenario = process.argv[3];
process.on("unhandledRejection", e => { console.error(e); process.exit(1); });
function el(id) {
  return {id, textContent: "", value: "", title: "", className: "", style: {}, dataset: {},
    children: [], _inner: "", open: false, flashed: false,
    get innerHTML() { return this._inner; }, set innerHTML(v) { this._inner = v; },
    classList: {add(c) { if (c === "flash") this.owner.flashed = true; }, remove() {}, toggle() {},
                contains() { return false; }},
    appendChild(c) { this.children.push(c); return c; }, remove() {}, addEventListener() {},
    insertAdjacentHTML(_, h) { this._inner += h; }, focus() {}, scrollIntoView() {},
    querySelector(sel) { return sel === "details" ? this._details || null : null; },
    querySelectorAll() { return []; }, getBoundingClientRect() { return {bottom: 0}; }};
}
const els = {};
const byId = id => { if (!els[id]) { els[id] = el(id); els[id].classList.owner = els[id]; } return els[id]; };
const rows = {};
globalThis.document = {getElementById: byId, createElement: () => el("new"), hidden: false,
  querySelector: sel => { const m = /data-task="([^"]+)"/.exec(sel); return m ? rows[m[1]] || null : null; },
  querySelectorAll: () => [], addEventListener() {}, body: el("body")};
globalThis.CSS = {escape: s => s};
const nav = {userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)", platform: "iPhone",
             maxTouchPoints: 5, standalone: false};
let swMessage = null, subscribed = null, existing = null;
const swReg = {pushManager: {
  getSubscription: async () => existing,
  subscribe: async opts => { subscribed = opts; return {toJSON: () => ({endpoint: "https://web.push.apple.com/z", keys: {p256dh: "p", auth: "a"}})}; }}};
nav.serviceWorker = {register: async () => swReg, ready: Promise.resolve(swReg),
                     addEventListener: (k, f) => { if (k === "message") swMessage = f; }};
Object.defineProperty(globalThis, "navigator", {value: nav, configurable: true, writable: true});
let perm = "default", asked = 0;
const win = {addEventListener() {}, PushManager: function () {}};
globalThis.window = win;
globalThis.Notification = {get permission() { return perm; },
  // As a browser does: once denied, asking again answers "denied" unprompted.
  requestPermission: async () => { asked++; if (perm !== "denied") perm = "granted"; return perm; }};
let standaloneApp = false;
globalThis.matchMedia = q => ({matches: q === "(display-mode: standalone)" ? standaloneApp : false});
globalThis.history = {replaceState(s, t, u) { globalThis.__replaced = u; }, state: null};
win.history = globalThis.history;
globalThis.location = {search: scenario === "cold" ? "?thread=D1%3A5.5" : "",
                       href: scenario === "cold" ? "https://mini.ts.net/?thread=D1%3A5.5" : "https://mini.ts.net/"};
globalThis.setInterval = () => 0;
globalThis.setTimeout = (f) => { setImmediate(f); return 0; };
globalThis.FileReader = class {};
const sessions = [{key: "D1:5.5", title: "garden", kind: "thread", session_id: "s1", updated: 1,
                   turns: 1, cost: 0, resume_cmd: "x", slack_link: "y"},
                  {key: "A:1", title: "alpha", kind: "thread", session_id: "s2", updated: 1,
                   turns: 1, cost: 0, resume_cmd: "x", slack_link: "y"}];
const sends = [], pushes = [], reads = [];
let sendAnswers = [];
let liveNow = {ok: true, turns: []};
globalThis.fetch = async (url, opts) => {
  let out = {ok: true};
  if (url.startsWith("/api/send")) {
    sends.push(JSON.parse(opts.body));
    out = sendAnswers.length ? sendAnswers.shift() : {ok: true, ref: "r9", replies: []};
    if (out === "throw") throw new TypeError("Load failed");
  } else if (url.startsWith("/api/push")) {
    const b = JSON.parse(opts.body); pushes.push(b);
    out = b.action === "key" ? {ok: true, key: "BAAA"} : {ok: true, subscriptions: 1};
  } else if (url.startsWith("/api/session?")) {
    reads.push(url);
    out = {session_id: "s1", messages: [], cursor: 1, reset: false, live: liveNow};
  } else if (url.startsWith("/api/sessions")) out = {bot_online: true, sessions, slack: {}};
  else if (url.startsWith("/api/tasks")) {
    const b = JSON.parse(opts.body); taskAsks.push(b);
    out = b.action === "page" ? {ok: true, page: {id: b.id, title: "the task", state: "needs_input",
                                                 state_words: "waiting", role: "implementor", project: "",
                                                 created: 1, timeline: [], cost: {}, item: null}}
        : {ok: true, tasks: [], counts: {}};
  }
  else if (url.startsWith("/api/stats"))
    out = {total_cost: 1, cache_rate: 90, threads: 2, days: [], models: []};
  else if (url.startsWith("/api/artifacts")) out = [];
  else if (url.startsWith("/api/projects")) out = {ok: true, projects: []};
  return {status: 200, headers: {get: () => null}, json: async () => out};
};
const taskAsks = [];
(0, eval)(src + `;globalThis.__p = {enableNotifications, pushSupport, sendReply, sendText, recheckSend,
  openTarget, newThreadForm, submitNewThread, loadList, loadTranscript,
  getActive: () => active, setActive(k) { active = lastSessions.find(s => s.key === k); },
  sent: k => sentFor(k)};`);
const tick = async (n) => { for (let i = 0; i < (n || 20); i++) await new Promise(r => setImmediate(r)); };
(async () => {
  const P = globalThis.__p, out = {};
  await tick();
  if (scenario === "cold") {
    await tick(40);
    out.cold = {active: P.getActive() && P.getActive().key, read: reads[reads.length - 1] || "",
                replaced: globalThis.__replaced};
    process.stdout.write(JSON.stringify(out)); return;
  }
  // iPhone, in Safari: not installed. Said, and nothing is asked.
  out.inSafari = {support: P.pushSupport(), btn: byId("notifybtn").title};
  await P.enableNotifications();
  out.inSafari.toast = byId("toast").textContent;
  out.inSafari.asked = asked;
  // Installed: the tap asks, subscribes with the bot's key, hands it over.
  standaloneApp = true;
  out.installed = {support: P.pushSupport()};
  await P.enableNotifications(); await tick();
  out.installed.asked = asked;
  out.installed.key = subscribed && Array.from(subscribed.applicationServerKey);
  out.installed.userVisible = subscribed && subscribed.userVisibleOnly;
  out.installed.pushes = pushes.map(p => p.action);
  out.installed.sub = pushes.find(p => p.action === "subscribe");
  out.installed.btn = byId("notifybtn").title;
  // Denied in Settings: said how to fix it.
  perm = "denied";
  await P.enableNotifications();
  out.denied = {toast: byId("toast").textContent, support: P.pushSupport()};
  perm = "granted";

  // A notification tapped while the app is open: the page opens the thread.
  await P.loadList();
  swMessage({data: {type: "open", url: "/?thread=D1:5.5"}}); await tick();
  out.openThread = {active: P.getActive() && P.getActive().key, read: reads[reads.length - 1] || ""};
  // ...or the task, on its own page (over the board's sheet).
  swMessage({data: {type: "open", url: "/?task=tsk_9"}}); await tick(40);
  out.openTask = {board: byId("boardmodal").style.display,
                  asked: taskAsks.filter(b => b.action === "page").map(b => b.id),
                  drawn: byId("bover").innerHTML.includes("the task")};

  // A send whose outcome is unknown, every time: reconciled under the same
  // id, then left unconfirmed -- and the text is not put back.
  P.setActive("A:1");
  await P.loadTranscript(true); await tick();
  sendAnswers = [{ok: false, unknown: true, error: "no answer from the bot in time"},
                 {ok: false, pending: true, error: "still being sent"},
                 "throw",
                 {ok: false, unknown: true, error: "x"}, {ok: false, unknown: true, error: "x"}];
  byId("reply").value = "add the shed";
  const before = sends.length;
  await P.sendReply(); await tick();
  const mine = sends.slice(before);
  out.unknown = {tries: mine.length, ids: [...new Set(mine.map(s => s.client_id))],
                 box: byId("reply").value, live: byId("tlive").innerHTML};
  // Still shown after the thread is read again (pruning keeps it).
  await P.loadTranscript(false); await tick();
  out.unknown.afterRead = byId("tlive").innerHTML.includes("not confirmed");
  // Check again: it had gone through after all.
  sendAnswers = [{ok: true, ref: "r7", replies: [], duplicate: true}];
  await P.recheckSend(0); await tick();
  out.recheck = {id: sends[sends.length - 1].client_id, live: byId("tlive").innerHTML,
                 box: byId("reply").value};
  // Unknown, then the repeat finds it: sent, nothing left over.
  sendAnswers = [{ok: false, unknown: true, error: "x"}, {ok: true, ref: "r8", replies: [], duplicate: true}];
  byId("reply").value = "and the gate";
  const b2 = sends.length;
  await P.sendReply(); await tick();
  out.reconciled = {tries: sends.length - b2, sameId: sends[b2].client_id === sends[b2 + 1].client_id,
                    box: byId("reply").value, live: byId("tlive").innerHTML};
  // Unknown, then Check again finds it was refused: now the words come back.
  sendAnswers = Array(5).fill({ok: false, unknown: true, error: "x"});
  byId("reply").value = "the fence";
  await P.sendReply(); await tick();
  const idx = P.sent("A:1").findIndex(i => i.unconfirmed);
  sendAnswers = [{ok: false, error: "no such thread"}];
  await P.recheckSend(idx); await tick();
  out.recheckRefused = {box: byId("reply").value, live: byId("tlive").innerHTML.includes("the fence")};
  byId("reply").value = "";
  // An unconfirmed command outlives a later turn finishing.
  sendAnswers = Array(5).fill({ok: false, unknown: true, error: "x"});
  await P.sendText("!brief"); await tick();
  sendAnswers = [{ok: true, ref: "r20", replies: []}];
  await P.sendText("next one"); await tick();
  liveNow = {ok: true, turns: [{ref: "r20", state: "done"}]};
  await P.loadTranscript(false); await tick();
  out.commandKept = byId("tlive").innerHTML.includes("!brief");
  liveNow = {ok: true, turns: []};
  // A definite refusal still gives the words back.
  sendAnswers = [{ok: false, error: "bot is offline"}];
  byId("reply").value = "later";
  await P.sendReply(); await tick();
  out.refused = byId("reply").value;

  // A new thread whose start is unconfirmed: a second tap asks after the same id.
  await P.newThreadForm();
  byId("nttext").value = "plan the week"; byId("ntproj").value = "";
  sendAnswers = Array(5).fill({ok: false, unknown: true, error: "x"});
  const b3 = sends.length;
  await P.submitNewThread(); await tick();
  out.ntUnknown = {err: byId("tferr").textContent, modal: byId("fmodal").style.display};
  sendAnswers = [{ok: true, key: "D9:1.1", ref: "1.1", replies: [], duplicate: true}];
  await P.submitNewThread(); await tick();
  out.ntIds = [...new Set(sends.slice(b3).map(s => s.client_id))];
  out.ntOpened = P.getActive() && P.getActive().key;
  process.stdout.write(JSON.stringify(out));
})();
"""


def test_dashboard_notifications_and_reconcile():
    import re
    sys.argv = ["x"]
    import visualizer as V
    print("\nthe page: Enable notifications, notification deep links, and unknown sends reconciled")
    check("the header has the notifications control",
          'id="notifybtn" onclick="enableNotifications()"' in V.PAGE)
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    fn = js[js.index("async function enableNotifications()"):]
    fn = fn[:fn.index("\n}\n")]
    before = re.sub(r"//[^\n]*", "", fn[:fn.index("Notification.requestPermission()")]
                    ).rsplit("await", 1)[0]
    check("the permission is asked before anything else is awaited (iOS needs the tap)",
          "await" not in before and "requestPermission" in fn, before)
    node = shutil.which("node")
    if not node:
        print("  … node not installed — cannot run the page's own javascript")
        return
    d = Path(tempfile.mkdtemp())
    (d / "page.js").write_text(js)
    (d / "drive.js").write_text(PAGE_PUSH_DRIVER)
    outs = {}
    for scenario in ("main", "cold"):
        p = subprocess.run([node, str(d / "drive.js"), str(d / "page.js"), scenario],
                           capture_output=True, text=True, timeout=60)
        check(f"the page's javascript runs ({scenario})", p.returncode == 0, p.stderr.strip()[-800:])
        if p.returncode:
            return
        outs[scenario] = json.loads(p.stdout)
    o = outs["main"]
    s = o["inSafari"]
    check("an iPhone not running the installed app is told to Add to Home Screen first",
          s["support"] == "install" and "Add to Home Screen first" in s["toast"]
          and "Add to Home Screen first" in s["btn"], str(s))
    check("and is not asked for a permission it cannot grant", s["asked"] == 0, str(s))
    i = o["installed"]
    check("installed, a tap asks once and subscribes with the bot's key, user-visible",
          i["support"] == "ask" and i["asked"] == 1 and i["key"] == [4, 0, 0]
          and i["userVisible"] is True, str(i))
    check("then hands the subscription to the bot",
          i["pushes"][:2] == ["key", "subscribe"] and i["sub"]["subscription"]["endpoint"]
          == "https://web.push.apple.com/z", str(i))
    check("and says notifications are on", i["btn"] == "Notifications are on", i["btn"])
    check("denied in Settings: told where to turn them on",
          o["denied"]["support"] == "denied" and "Settings" in o["denied"]["toast"], str(o["denied"]))
    check("a tapped notification opens its thread in the app already open",
          o["openThread"]["active"] == "D1:5.5" and "key=D1%3A5.5" in o["openThread"]["read"],
          str(o["openThread"]))
    check("or its task, on its own page",
          o["openTask"] == {"board": "flex", "asked": ["tsk_9"], "drawn": True},
          str(o["openTask"]))
    c = outs["cold"]["cold"]
    check("a cold start on /?thread= opens that thread, and leaves / in the address bar",
          c["active"] == "D1:5.5" and "key=D1%3A5.5" in c["read"] and c["replaced"] == "/", str(c))
    u = o["unknown"]
    check("an unknown outcome is sent again under the same id, a few times",
          u["tries"] == 5 and len(u["ids"]) == 1 and len(u["ids"][0]) >= 8, str(u)[:300])
    check("and if still unknown, the text is NOT put back: it is shown unconfirmed",
          u["box"] == "" and "not confirmed" in u["live"] and "add the shed" in u["live"]
          and "Check again" in u["live"], str(u)[:400])
    check("an unconfirmed message stays shown when the thread is read again", u["afterRead"])
    check("an unconfirmed command too, after a later turn has finished", o["commandKept"])
    check("Check again that finds it refused gives the words back",
          o["recheckRefused"]["box"] == "the fence" and not o["recheckRefused"]["live"],
          str(o["recheckRefused"]))
    r = o["recheck"]
    check("Check again asks under the same id, and a found send settles",
          r["id"] == u["ids"][0] and "not confirmed" not in r["live"] and r["box"] == "", str(r)[:300])
    rc = o["reconciled"]
    check("an unknown that the repeat finds is simply sent",
          rc["tries"] == 2 and rc["sameId"] and rc["box"] == "" and "not confirmed" not in rc["live"],
          str(rc)[:300])
    check("a definite refusal still puts the words back", o["refused"] == "later", o["refused"])
    check("an unconfirmed new thread says so and keeps the form",
          "Not confirmed" in o["ntUnknown"]["err"] and o["ntUnknown"]["modal"] != "none",
          str(o["ntUnknown"]))
    check("tapping Start again asks after the same message: one id for the form",
          len(o["ntIds"]) == 1 and o["ntOpened"] == "D9:1.1", f"{o['ntIds']} {o['ntOpened']}")


def test_silkworm_push_subcommand():
    import importlib.machinery
    import importlib.util
    print("\nsilkworm push status | test | forget-all, through the bot")
    loader = importlib.machinery.SourceFileLoader("silkworm_cli_push", str(BASE / "bin" / "silkworm"))
    spec = importlib.util.spec_from_loader("silkworm_cli_push", loader)
    cli = importlib.util.module_from_spec(spec)
    sys.argv = ["silkworm"]
    loader.exec_module(cli)
    calls = []

    def bot(answer):
        def call(path, payload, timeout=3):
            calls.append((path, payload, timeout))
            return answer
        return call
    for argv, answer, code in (
            ([], {"ok": True, "available": True, "keys": True, "key_dir": "/k",
                  "subscriptions": 1, "services": ["web.push.apple.com"]}, 0),
            (["test"], {"ok": True, "sent": 1, "pruned": 0, "failed": 0}, 0),
            (["test"], {"ok": False, "error": "no subscriptions"}, 1),
            (["forget-all"], {"ok": True, "forgot": 2}, 0),
            (["status"], {}, 3)):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            got = cli.do_push(argv, call=bot(answer))
        check(f"silkworm push {' '.join(argv) or '(status)'} exits {code}", got == code,
              f"{got}: {buf.getvalue()[-200:]}")
    check("each asks the bot's /push for that action",
          [c[1]["action"] for c in calls] == ["status", "test", "test", "forget-all", "status"]
          and all(c[0] == "/push" for c in calls), str(calls))
    check("a test is given time to reach every device", calls[1][2] >= 60, str(calls[1]))
    with contextlib.redirect_stderr(io.StringIO()):
        check("an unknown action is a usage error", cli.do_push(["nuke"], call=bot({})) == 2)
    check("the subcommand is dispatched and documented",
          'cmd == "push"' in (BASE / "bin" / "silkworm").read_text()
          and "silkworm push [status|test|forget-all]" in cli.__doc__)
    check("requirements name cryptography, which push.py needs",
          "cryptography" in (BASE / "requirements.txt").read_text())


def test_silkworm_test_runs_on_the_bots_interpreter():
    import importlib.machinery
    import importlib.util
    print("\nsilkworm test runs the suite on the bot's venv, from a task worktree too")
    loader = importlib.machinery.SourceFileLoader("silkworm_cli_venv", str(BASE / "bin" / "silkworm"))
    spec = importlib.util.spec_from_loader("silkworm_cli_venv", loader)
    cli = importlib.util.module_from_spec(spec)
    sys.argv = ["silkworm"]
    loader.exec_module(cli)
    tmp = Path(tempfile.mkdtemp())
    main, wt, bare = tmp / "main", tmp / "wt", tmp / "bare"
    main.mkdir()
    git = lambda *a, cwd=main: subprocess.run(["git", *a], cwd=cwd, capture_output=True,
                                              text=True, check=True)
    git("init", "-q", "-b", "main")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
    git("worktree", "add", "-q", "-b", "task", str(wt))
    venv = main / ".venv" / "bin" / "python"
    venv.parent.mkdir(parents=True)
    venv.write_text("")
    check("a checkout with its own .venv uses it", cli.venv_python(main) == str(venv))
    check("a task worktree without one uses the main checkout's, the bot's own",
          Path(cli.venv_python(wt)).resolve() == venv.resolve(), cli.venv_python(wt))
    (wt / ".venv" / "bin").mkdir(parents=True)
    (wt / ".venv" / "bin" / "python").write_text("")
    check("but a worktree's own .venv comes first",
          cli.venv_python(wt) == str(wt / ".venv" / "bin" / "python"))
    bare.mkdir()
    check("with neither, the interpreter running the CLI", cli.venv_python(bare) == sys.executable)
    src = (BASE / "bin" / "silkworm").read_text()
    check("silkworm test and the deploy preflight's suite both ask venv_python",
          src.count("py = venv_python()") == 2
          and "if VENV_PY.exists() else sys.executable" not in src)


# --- unread ---------------------------------------------------------------------
# A thread's unread count is the replies not the owner's that arrived after the
# owner last read it at its bottom on the dashboard (seen.json, on the mini) or
# last wrote in it (owner_said, stamped by the bot) -- unread.py.

def _uline(role, ts, text="", tool=False, result=False, meta=False):
    if role == "assistant":
        content = ([{"type": "text", "text": text}] if text else []) + (
            [{"type": "tool_use", "id": "t" + ts[-6:-1], "name": "Bash", "input": {"command": "ls"}}] if tool else [])
    else:
        content = ([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}] if result
                   else [{"type": "text", "text": text or "hello"}])
    d = {"type": role, "timestamp": ts, "message": {"role": role, "content": content}}
    if meta:
        d["isMeta"] = True
    return json.dumps(d) + "\n"


def _uts(sec):
    return datetime.fromtimestamp(1795000000 + sec, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def test_unread_counts_other_authors_after_the_watermark():
    import unread as U
    print("\nunread: replies not the owner's, after the later of last read and the owner's last word")
    tmp = Path(tempfile.mkdtemp())
    t = tmp / "s.jsonl"
    T0 = 1795000000
    t.write_text(_uline("user", _uts(0), "owner asks")
                 + _uline("assistant", _uts(10), "first reply")
                 + _uline("assistant", _uts(20), tool=True)              # a tool call: not a reply
                 + _uline("user", _uts(21), result=True)                 # its result: not the owner
                 + _uline("assistant", _uts(30), "second reply", tool=True)
                 + _uline("user", _uts(31), "a reminder", meta=True)
                 + _uline("assistant", _uts(32), "meta", meta=True))
    os.utime(t, (T0 + 40, T0 + 40))
    idx = U.ReplyIndex()
    check("the replies are the assistant's messages with words, in time order",
          idx.times(t) == [T0 + 10.0, T0 + 30.0], idx.times(t))
    check("nothing read yet: both replies are unread", U.unread(t, 0.0, idx) == 2)
    check("read down to the first: one", U.unread(t, U.watermark(T0 + 10, None), idx) == 1)
    check("the owner's own messages never count (one after the watermark changes nothing)",
          U.unread(t, U.watermark(T0 + 1, None), idx) == 2)
    check("the owner writing after both clears them, read or not",
          U.unread(t, U.watermark(0.0, T0 + 35), idx) == 0)
    check("...and the later of the two moments is the one used",
          U.watermark(T0 + 35, T0 + 5) == T0 + 35 and U.watermark(T0 + 5, T0 + 35) == T0 + 35)
    check("an owner_said recorded as Slack's ts string reads the same",
          U.watermark(0.0, str(T0 + 35)) == T0 + 35)
    # Appended: read from where the last read stopped; a half-written line waits.
    with open(t, "a") as f:
        f.write(_uline("user", _uts(50), "owner again") + _uline("assistant", _uts(60), "third"))
        f.write(_uline("assistant", _uts(70), "still being written")[:-20])
    os.utime(t, (T0 + 75, T0 + 75))
    check("new replies are found incrementally, a partial line not yet",
          idx.times(t) == [T0 + 10.0, T0 + 30.0, T0 + 60.0], idx.times(t))
    check("an owner reply in the middle clears only what came before it",
          U.unread(t, U.watermark(0.0, T0 + 50), idx) == 1)
    # A transcript not written since the watermark is never opened.
    other = tmp / "untouched.jsonl"
    other.write_text(_uline("assistant", _uts(5), "old"))
    os.utime(other, (T0 + 6, T0 + 6))
    fresh = U.ReplyIndex()
    check("a transcript older than the watermark is not read at all",
          U.unread(other, T0 + 7, fresh) == 0 and not fresh._files, fresh._files)
    check("no transcript: none", U.unread(None, 0.0, idx) == 0)
    check("replies before tracking began are dropped, not counted",
          U.unread(t, U.watermark(T0 + 1, None), U.ReplyIndex(), since=T0 + 1) == 3
          and U.ReplyIndex().times(t, floor=T0 + 15) == [T0 + 30.0, T0 + 60.0])
    t.write_text(_uline("assistant", _uts(90), "replaced"))
    os.utime(t, (T0 + 95, T0 + 95))
    check("a transcript replaced by a shorter one is read again from the top",
          idx.times(t) == [T0 + 90.0], idx.times(t))


def test_unread_watermark_survives_a_restart():
    import unread as U
    print("\nunread: seen.json holds the owner's watermarks across a restart")
    tmp = Path(tempfile.mkdtemp())
    path = tmp / U.SEEN_NAME
    now = [1795001000.0]
    s = U.Seen(path, clock=lambda: now[0])
    check("first use begins tracking now: everything before is read",
          s.since() == 1795001000.0 and path.exists() and s.get("D1:1") == 1795001000.0)
    check("a watermark is stored", s.mark("D1:1", 1795001500.0 - 1000) == 1795001000.0)
    now[0] = 1795002000.0
    check("...and moves forward", s.mark("D1:1", 1795001900.0) == 1795001900.0)
    check("never back", s.mark("D1:1", 1795001100.0) == 1795001900.0)
    check("never past now (a page's clock running fast)",
          s.mark("D2:2", 1795009999.0) <= now[0] + U.FUTURE_SLACK_S)
    again = U.Seen(path, clock=lambda: 1795003000.0)       # the dashboard restarted
    check("a restart reads the same watermarks", again.get("D1:1") == 1795001900.0, again.all())
    check("...and the same start: tracking does not begin again",
          again.since() == 1795001000.0)
    check("written as JSON through jsonstore (with its backup)",
          json.loads(path.read_text())["threads"]["D1:1"] == 1795001900.0
          and (tmp / (U.SEEN_NAME + ".prev")).exists())
    path.write_text("{not json"); (tmp / (U.SEEN_NAME + ".prev")).write_text("{nor this")
    broken = U.Seen(path)
    check("both copies unreadable: no counts, and nothing written over them",
          broken.mark("D1:1", 1795002500.0) is None and broken.error
          and path.read_text() == "{not json", broken.error)
    check("the file is not committed", subprocess.run(
        ["git", "check-ignore", "-q", U.SEEN_NAME], cwd=BASE).returncode == 0)


def test_unread_mark_all_and_the_idle_baseline():
    import threading
    import urllib.request
    import unread as U
    import visualizer as V
    print("\nunread: Mark all read, and threads idle two days read once on deploy")
    tmp = Path(tempfile.mkdtemp())
    path = tmp / U.SEEN_NAME
    T0 = 1795000000.0
    now = [T0]
    path.write_text(json.dumps({"since": T0 - 30 * 86400, "threads": {"fresh": T0 - 100}}))
    s = U.Seen(path, clock=lambda: now[0])
    entries = {"old": {"updated": T0 - 3 * 86400}, "fresh": {"updated": T0 - 3600},
               "edge": {"updated": T0 - 2 * 86400 + 60}, "noupdated": {}, "odd": {"updated": "x"}}
    check("the baseline marks only threads idle over two days, now",
          s.baseline_idle(entries) == 1 and s.get("old") == T0 and s.get("fresh") == T0 - 100
          and s.get("edge") == s.since() and s.get("noupdated") == s.since(), s.all())
    check("...a thread whose last use is not known is left as it was", "noupdated" not in s.all()
          and "odd" not in s.all())
    now[0] = T0 + 10 * 86400
    check("...once: recorded in seen.json, so it never runs again",
          s.baseline_idle(entries) == 0 and U.Seen(path).baseline_idle(entries) == 0
          and json.loads(path.read_text())["idle_baseline"] == T0)
    check("Mark all read: since moves to now, so every thread is read as of now",
          s.mark_all() == now[0] and all(s.get(k) == now[0] for k in ("old", "fresh", "never-seen")))
    check("...stored, so another device (another Seen on the same file) agrees",
          U.Seen(path).get("fresh") == now[0])
    now[0] = T0
    check("...and never back", s.mark_all() == T0 + 10 * 86400)
    now[0] = T0 + 20 * 86400
    check("as of when the page's list was counted, not later: a reply since stays unread",
          s.mark_all(T0 + 15 * 86400) == T0 + 15 * 86400)
    check("...and never past now (a page's clock running fast)", s.mark_all(T0 + 99 * 86400) == now[0])
    path.write_text("{not json"); (tmp / (U.SEEN_NAME + ".prev")).write_text("{nor")
    broken = U.Seen(path)
    check("unreadable: nothing marked, nothing written",
          broken.mark_all() is None and broken.baseline_idle(entries) == 0 and path.read_text() == "{not json")
    # Through the route and the sessions list, on a copy shaped like the live store.
    saved = (V.LOOPBACK, V.TOKEN, V.SESSIONS_FILE, V.PROJECTS_DIR, V.ENV_FILE, V.bot_call)
    tmp = Path(tempfile.mkdtemp())
    proj = tmp / "projects" / "-w"
    proj.mkdir(parents=True)
    for sid in ("sa", "sb", "sc"):
        (proj / f"{sid}.jsonl").write_text(_uline("user", _uts(0), "go") + _uline("assistant", _uts(10), "one"))
    (tmp / "sessions.json").write_text(json.dumps({
        "D1:1.0": {"session_id": "sa", "title": "A", "updated": time.time()},
        "D1:2.0": {"session_id": "sb", "title": "B", "updated": time.time()},
        "D1:3.0": {"session_id": "sc", "title": "idle", "kind": "task", "updated": time.time() - 3 * 86400}}))
    (tmp / "seen.json").write_text(json.dumps({"since": T0 - 100, "threads": {}}))
    srv = None
    try:
        V.LOOPBACK, V.TOKEN = True, ""
        V.SESSIONS_FILE, V.PROJECTS_DIR = tmp / "sessions.json", tmp / "projects"
        V.ENV_FILE = tmp / "no.env"
        V._seen.clear()
        V.bot_call = lambda path, payload, timeout=1.0: {}
        rows = {r["key"]: r["unread"] for r in V.load_sessions()["sessions"]}
        check("the first read after deploy baselines the idle one; two in use stay unread",
              rows == {"D1:1.0": 1, "D1:2.0": 1, "D1:3.0": 0}, rows)
        srv = V.make_server("127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/seen", method="POST",
                                     data=json.dumps({"action": "all", "ts": time.time()}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            ans = json.loads(r.read())
        check("POST /api/seen {action: all} is stored", ans.get("ok") and ans.get("since"), ans)
        V._seen.clear()                          # the other device's dashboard, as it were
        rows = {r["key"]: r["unread"] for r in V.load_sessions()["sessions"]}
        check("...and every thread is read, read back from seen.json",
              rows == {"D1:1.0": 0, "D1:2.0": 0, "D1:3.0": 0}, rows)
        with open(proj / "sa.jsonl", "a") as f:
            f.write(_uline("assistant", datetime.fromtimestamp(time.time() + 1, timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", "new"))
        os.utime(proj / "sa.jsonl", (time.time() + 2, time.time() + 2))
        rows = {r["key"]: r["unread"] for r in V.load_sessions()["sessions"]}
        check("a new reply after it: that thread is unread by one",
              rows == {"D1:1.0": 1, "D1:2.0": 0, "D1:3.0": 0}, rows)
    finally:
        V.LOOPBACK, V.TOKEN, V.SESSIONS_FILE, V.PROJECTS_DIR, V.ENV_FILE, V.bot_call = saved
        V._seen.clear()
        if srv is not None:
            srv.shutdown(); srv.server_close()


def test_unread_in_the_sessions_list_and_the_seen_route():
    import threading
    import urllib.error
    import urllib.request
    import visualizer as V
    print("\nunread: /api/sessions counts it; /api/seen moves it, behind the dashboard's write gates")
    saved_env = os.environ.pop("VIZ_TAILSCALE_LOGIN", None)
    saved = (V.LOOPBACK, V.TOKEN, V.SESSIONS_FILE, V.PROJECTS_DIR, V.ENV_FILE, V.bot_call)
    tmp = Path(tempfile.mkdtemp())
    proj = tmp / "projects" / "-w"
    proj.mkdir(parents=True)
    T0 = 1795000000
    (proj / "sa.jsonl").write_text(_uline("user", _uts(0), "go") + _uline("assistant", _uts(10), "one")
                                   + _uline("assistant", _uts(20), "two"))
    (proj / "sb.jsonl").write_text(_uline("user", _uts(0), "go") + _uline("assistant", _uts(10), "one")
                                   + _uline("user", _uts(30), "thanks"))
    (tmp / "sessions.json").write_text(json.dumps({
        "D1:1.0": {"session_id": "sa", "title": "A"},
        "D1:2.0": {"session_id": "sb", "title": "B", "owner_said": T0 + 30},
        "D1:3.0": {"session_id": "none", "title": "C"}}))
    (tmp / "seen.json").write_text(json.dumps({"since": T0 - 100, "threads": {}}))
    srv = None
    try:
        V.LOOPBACK, V.TOKEN = True, ""
        V.SESSIONS_FILE, V.PROJECTS_DIR = tmp / "sessions.json", tmp / "projects"
        V.ENV_FILE = tmp / "no.env"
        V._seen.clear()
        V.bot_call = lambda path, payload, timeout=1.0: {}
        rows = {s["key"]: s for s in V.load_sessions()["sessions"]}
        check("each row carries its count: two replies unread",
              rows["D1:1.0"]["unread"] == 2, {k: r["unread"] for k, r in rows.items()})
        check("a thread the owner wrote in after the reply has none", rows["D1:2.0"]["unread"] == 0)
        check("a thread with no transcript has none", rows["D1:3.0"]["unread"] == 0)
        tag = V.sessions_tag(V.load_sessions())
        srv = V.make_server("127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]

        def post(body, ctype="application/json", **headers):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/seen", method="POST",
                                         data=json.dumps(body).encode(),
                                         headers={"Content-Type": ctype, **headers})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()
        held = lambda: json.loads((tmp / "seen.json").read_text())["threads"]
        serve = {"X-Forwarded-For": "100.64.0.2", "X-Forwarded-Host": "mini.tail1.ts.net"}
        body = {"key": "D1:1.0", "ts": _uts(10)}
        code, _ = post(body, **serve, **{"Tailscale-User-Login": "owner@example.com"})
        check("login unset: a proxied write is refused and nothing is stored", code == 403 and not held())
        os.environ["VIZ_TAILSCALE_LOGIN"] = "owner@example.com"
        code, _ = post(body, **serve)
        check("no tailnet identity: refused", code == 403 and not held())
        code, _ = post(body, **serve, **{"Tailscale-User-Login": "intruder@example.com"})
        check("another login: refused", code == 403 and not held())
        code, _ = post(body, **serve, **{"Tailscale-User-Login": "owner@example.com",
                                         "Sec-Fetch-Site": "cross-site"})
        check("a cross-site page in the owner's browser: refused", code == 403 and not held())
        code, _ = post(body, "text/plain", **serve, **{"Tailscale-User-Login": "owner@example.com"})
        check("a no-preflight text/plain POST: refused", code == 403 and not held())
        code, _ = post(body, **serve, **{"Tailscale-User-Login": "owner@example.com",
                                         "Origin": "https://evil.example"})
        check("a POST from another origin: refused", code == 403 and not held())
        V.LOOPBACK, V.TOKEN = False, "s3cret"
        code, _ = post(body)
        check("bound wider, without the token: refused", code == 401 and not held())
        code, r = post(body, **{"X-Silkworm-Token": "s3cret"})
        check("...with it: stored", code == 200 and r.get("ok") and held().get("D1:1.0") == T0 + 10, r)
        V.LOOPBACK, V.TOKEN = True, ""
        code, r = post(body, **serve, **{"Tailscale-User-Login": "owner@example.com",
                                         "Sec-Fetch-Site": "same-origin", "Origin": "https://mini.tail1.ts.net"})
        check("the owner's own page through serve is let in", code == 200 and r.get("ok"), r)
        rows = {s["key"]: s for s in V.load_sessions()["sessions"]}
        check("read down to the first reply: one left", rows["D1:1.0"]["unread"] == 1)
        check("...and the list's ETag moved, so a poll gets it (not a 304)",
              V.sessions_tag(V.load_sessions()) != tag)
        code, r = post({"key": "D1:9.9", "ts": _uts(10)})
        check("a thread that is not one is refused", code == 200 and r.get("ok") is False
              and "D1:9.9" not in held(), r)
        code, r = post({"key": "D1:1.0", "ts": "yesterday"})
        check("a time that is not one is refused", r.get("ok") is False, r)
        post({"key": "D1:1.0", "ts": _uts(20)})
        V._seen.clear()                          # the dashboard restarted
        rows = {s["key"]: s for s in V.load_sessions()["sessions"]}
        check("after a restart it is still read", rows["D1:1.0"]["unread"] == 0, rows["D1:1.0"]["unread"])
        check("the route is one the gate tests enumerate", "/api/seen" in _viz_routes("do_POST"))
        V._seen.clear()
        (tmp / "seen.json").write_text("[]"); (tmp / "seen.json.prev").write_text("[]")
        rows = {s["key"]: s for s in V.load_sessions()["sessions"]}
        check("unreadable read state is unknown (null), not zero", rows["D1:1.0"]["unread"] is None)
    finally:
        V.LOOPBACK, V.TOKEN, V.SESSIONS_FILE, V.PROJECTS_DIR, V.ENV_FILE, V.bot_call = saved
        V._seen.clear()
        if srv is not None:
            srv.shutdown(); srv.server_close()
        if saved_env is None:
            os.environ.pop("VIZ_TAILSCALE_LOGIN", None)
        else:
            os.environ["VIZ_TAILSCALE_LOGIN"] = saved_env


def test_owner_writing_marks_a_thread_read():
    import ast
    import store as S
    print("\nunread: the owner writing in a thread (Slack or the dashboard) marks what came before read")
    tmp = Path(tempfile.mkdtemp())
    st = S.SessionStore(tmp / "sessions.json")
    st.update("D1:1.0", session_id="s1")
    before = st.get("D1:1.0")["updated"]
    time.sleep(0.01)
    check("noted", st.note_owner("D1:1.0", 1795000100.0) and st.get("D1:1.0")["owner_said"] == 1795000100.0)
    check("not activity: the thread does not move up the list", st.get("D1:1.0")["updated"] == before)
    st.note_owner("D1:1.0", 1795000050.0)
    check("only forward", st.get("D1:1.0")["owner_said"] == 1795000100.0)
    check("a Slack ts is read as its time", st.note_owner("D1:1.0", "1795000200.000100")
          and st.get("D1:1.0")["owner_said"] == 1795000200.0001)
    st.note_owner("D1:1.0", 9999999999.0)
    check("never later than now", st.get("D1:1.0")["owner_said"] <= time.time())
    st.update("D1:2.0", session_id="s2")
    t0 = time.time(); st.note_owner("D1:2.0"); st.note_owner("D1:2.0", "not a ts")
    check("none, or nothing readable, is now", t0 <= st.get("D1:2.0")["owner_said"] <= time.time())
    check("a thread not on record is not made", not st.note_owner("D9:9.9", 1.0) and st.get("D9:9.9") is None)
    check("persisted", S.SessionStore(tmp / "sessions.json").get("D1:1.0")["owner_said"]
          == st.get("D1:1.0")["owner_said"] > 1795000200)
    import schema
    check("a declared field, unknown (None) when absent", schema.default("owner_said") is None)
    # Through the shipped handle_prompt: a message from the dashboard, and one
    # from Slack, each stamp the thread.
    bot = _web_bot(Path(tempfile.mkdtemp()))
    bot.store.update("D1:111.222", session_id="sid-old", cwd=str(tmp))
    t0 = time.time()
    r = bot.web({"key": "D1:111.222", "text": "thanks, carry on"})
    check("a reply from the dashboard notes the owner writing, at once",
          r.get("ok") and _wait(lambda: (bot.store.get("D1:111.222") or {}).get("owner_said", 0) >= t0),
          (r, bot.store.get("D1:111.222")))
    _wait(lambda: not bot.running)
    bot.hooks["prompt"]({"channel": "D1", "ts": "1999999999.000200", "thread_ts": "111.222",
                         "user": "U1", "text": "!help"}, lambda **k: None, bot.client)
    check("so does a Slack !command, never later than now",
          t0 <= bot.store.get("D1:111.222")["owner_said"] <= time.time())
    tree = ast.parse((BASE / "bot.py").read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "handle_prompt")
    lines = lambda pred: [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call) and pred(n)]
    noted = lines(lambda n: isinstance(n.func, ast.Attribute) and n.func.attr == "note_owner"
                  and isinstance(n.func.value, ast.Name) and n.func.value.id == "store")
    allow = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.If) and "ALLOWED_USERS" in ast.unparse(n.test)]
    cmds = lines(lambda n: isinstance(n.func, ast.Name) and n.func.id in ("handle_command", "approve_command"))
    check("handle_prompt notes it once, after the allowlist and before any command or prompt runs",
          len(noted) == 1 and allow and cmds and allow[0] < noted[0] < min(cmds), (noted, allow, cmds))


UNREAD_DRIVE = r"""
const sess = (key, unread, extra) => Object.assign({key, title: "T " + key, kind: "thread", project: "", summary: "",
  session_id: "s" + key, turns: 3, cost: 0, updated: 1795000000, files: 0, events: [], running: false,
  resume_cmd: "", slack_link: "", hidden: false, unread}, extra || {});
let rows = [sess("A", 3), sess("B", 0), sess("C", 2, {hidden: true}), sess("D", 1, {updated: 1794000000}),
            sess("R", 40, {kind: "task", updated: 1793000000})];
let counts = {};
const badge = [];
Object.defineProperty(globalThis, "navigator", {value: {vibrate() { return true; },
  setAppBadge(n) { badge.push(["set", n]); return Promise.resolve(); },
  clearAppBadge() { badge.push(["clear"]); return Promise.resolve(); }}, configurable: true});
answer = (url, b) => url.startsWith("/api/sessions") ? {bot_online: true, sessions: rows.map(r => Object.assign({}, r)), slack: {}}
  : url === "/api/seen" ? {ok: true}
  : b && b.action === "counts" ? {ok: true, counts} : BASE(url, b);
const flushAsync = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r)); };
const card = key => M.cards.get(key);
const order = () => byId("list").children.filter(c => M.cards.has && [...M.cards.values()].includes(c))
  .map(c => [...M.cards.entries()].find(([k, v]) => v === c)[0]);
(async () => {
  const out = {};
  await M.loadList(); await flushAsync();
  out.dotA = card("A").innerHTML.includes('class="unread"') && card("A").innerHTML.includes(">3</span>");
  out.dotB = card("B").innerHTML.includes('class="unread"');
  out.dotInTitle = card("A").innerHTML.includes('<span class="title"><span class="unread"');
  out.clsA = card("A").className; out.clsB = card("B").className;
  out.title = document.title; out.badge = badge.slice();
  out.filterBar = byId("kindfilter").innerHTML;
  out.orderAll = order();
  M.setUnreadOnly(true); await flushAsync();
  out.orderUnread = order();
  M.setUnreadOnly(false); await flushAsync();
  M.setUnreadFirst(true); await flushAsync();
  out.orderFirst = order();
  out.remembered = localStorage.getItem("sw-unread-first");
  M.setUnreadFirst(false); await flushAsync();
  // Read: the open thread, at its bottom.
  M.setActive("A", [{role: "user", ts: "2026-11-21T10:00:00.000Z", blocks: []},
                    {role: "assistant", ts: "2026-11-21T10:05:00.000Z", blocks: []}], true);
  M.markSeen(); await flushAsync();
  out.seenPosts = calls.filter(c => c.url === "/api/seen").map(c => c.body);
  out.dotAfterRead = card("A").innerHTML.includes('class="unread"');
  out.titleAfterRead = document.title;
  M.markSeen(); await flushAsync();
  out.seenPostsAgain = calls.filter(c => c.url === "/api/seen").length;
  // The next poll is a 304 of the same answer (the server still says 3): read here stays read.
  await M.loadList(); await flushAsync();
  out.dotAfterSamePoll = card("A").innerHTML.includes('class="unread"');
  // A new reply since: the server's count moves, and it is the server's again.
  rows[0].unread = 4; await M.loadList(); await flushAsync();
  out.dotAfterNewReply = card("A").innerHTML.includes(">4</span>");
  rows[0].unread = 3;
  // A panel over the thread: not read, however far down.
  M.setActive("D", [{role: "assistant", ts: "2026-11-21T12:00:00.000Z", blocks: []}], true);
  byId("boardmodal").style.display = "flex";
  M.markSeen(); await flushAsync();
  out.postsUnderPanel = calls.filter(c => c.url === "/api/seen").length;
  byId("boardmodal").style.display = "none";
  // The mini refuses it: the row is unread again, and the next read asks again.
  const was = answer;
  answer = (url, b) => url === "/api/seen" ? {ok: false, error: "no"} : was(url, b);
  M.markSeen(); await flushAsync();
  out.dotAfterRefused = card("D").innerHTML.includes('class="unread"');
  answer = was;
  M.markSeen(); await flushAsync();
  out.postsAfterRefused = calls.filter(c => c.url === "/api/seen").length;
  // Scrolled up: not read.
  M.setActive("B", [{role: "assistant", ts: "2026-11-21T11:00:00.000Z", blocks: []}], false);
  M.markSeen(); await flushAsync();
  out.postsWhileUp = calls.filter(c => c.url === "/api/seen").length;
  M.clearActive();
  // Needs you counts too: two awaiting approval, one failed.
  counts = {awaiting_approval: 2, failed: 1, running: 5};
  await M.loadList(); await flushAsync();
  out.titleWithNeeds = document.title;
  // A new reply in a conversation: up by one; another in a task run: no change.
  rows[1].unread = 1; await M.loadList(); await flushAsync();
  out.titleNewReply = document.title;
  rows[4].unread = 41; await M.loadList(); await flushAsync();
  out.titleTaskReply = document.title;
  // Mark all read: every row clears at once, posted once as {action: "all"}.
  const allBefore = calls.filter(c => c.url === "/api/seen").length;
  out.markAllBtn = /markAllRead\(\)/.test(byId("kindfilter").innerHTML);
  M.markAllRead(); await flushAsync();
  out.markAllPosts = calls.filter(c => c.url === "/api/seen").slice(allBefore).map(c => c.body);
  out.titleAllRead = document.title;
  out.dotsAllRead = [...M.cards.values()].some(c => c.innerHTML.includes('class="unread"'));
  // Refused: the counts come back.
  const was2 = answer;
  answer = (url, b) => url === "/api/seen" ? {ok: false, error: "no"} : was2(url, b);
  rows[1].unread = 2; await M.loadList(); await flushAsync();
  M.markAllRead(); await flushAsync();
  out.titleAllRefused = document.title;
  answer = was2;
  counts = {};
  rows = rows.map(r => Object.assign(r, {unread: 0}));
  badge.length = 0;
  await M.loadList(); await flushAsync();
  out.badgeCleared = badge.slice(); out.titleCleared = document.title;
  out.filterCleared = byId("kindfilter").innerHTML;
  out.unreadOnlyCleared = /setUnreadOnly/.test(out.filterCleared);
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_unread_dot_filter_and_badge_in_the_page():
    import re
    import visualizer as V
    print("\nunread on the page: a dot and count, the Unread filter, unread first, the badge and title")
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    check("the app badge is set and cleared through navigator.setAppBadge / clearAppBadge",
          "nav.setAppBadge(n)" in js and "nav.clearAppBadge()" in js)
    check("read state is posted to /api/seen as JSON (the write gates' content type)",
          'fetch("/api/seen", {method: "POST", headers: {"Content-Type": "application/json"}' in js)
    check("no new per-message fetch: counts come with /api/sessions; the only posts are a read and Mark all read",
          js.count('"/api/seen"') == 2)
    node = shutil.which("node")
    if not node:
        print("  … node not installed — the unread page code checked by text only")
        return
    dom = MOTION_HARNESS[:MOTION_HARNESS.index("(0, eval)(SRC")]
    expose = """
;globalThis.__m = {loadList, markSeen, markAllRead, setUnreadOnly, setUnreadFirst, renderList, cards: cardEls,
  setActive(key, msgs, atBottom) { active = lastSessions.find(x => x.key === key);
    tview = {key, sid: "", cursor: 1, busy: false, error: false, msgs, files: []}; pinned = atBottom; },
  clearActive() { active = null; }};"""
    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    (d / "drive.js").write_text(dom + "(0, eval)(SRC + " + json.dumps(expose) + ");\nconst M = globalThis.__m;\n"
                                + UNREAD_DRIVE)
    p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js")], capture_output=True, text=True, timeout=90)
    if p.returncode != 0:
        check("the unread harness runs the page's script", False, p.stderr.strip()[-900:])
        return
    o = json.loads(p.stdout)
    check("an unread thread's row has the dot and its count", o["dotA"], o)
    check("the dot sits inside the title, which a phone sizes to the row less its ×", o["dotInTitle"])
    check("...and reads as unread; a read one has neither",
          "isunread" in o["clsA"] and not o["dotB"] and "isunread" not in o["clsB"], (o["clsA"], o["clsB"]))
    check("the title counts conversations with unread replies, not replies, not hidden ones, not task runs",
          o["title"] == "(2) Silkworm — Sessions", o["title"])
    check("the app icon's badge is the same total (cleared before, while the list was empty)",
          o["badge"][-1] == ["set", 2] and ["set", 2] not in o["badge"][:-1]
          and all(b == ["clear"] for b in o["badge"][:-1]), o["badge"])
    check("the Unread filter counts the unread threads shown", "Unread <b>3</b>" in o["filterBar"], o["filterBar"])
    check("the list is newest first", o["orderAll"] == ["A", "B", "D"], o["orderAll"])
    check("Unread narrows it to the unread", o["orderUnread"] == ["A", "D"], o["orderUnread"])
    check("unread first puts them on top, newest first within", o["orderFirst"] == ["A", "D", "B"], o["orderFirst"])
    check("...and is remembered on the device", o["remembered"] == "1")
    check("read at the bottom: the newest message drawn is posted for that thread",
          o["seenPosts"] == [{"key": "A", "ts": "2026-11-21T10:05:00.000Z"}], o["seenPosts"])
    check("...the row clears at once, and the title with it",
          not o["dotAfterRead"] and o["titleAfterRead"] == "(1) Silkworm — Sessions", o["titleAfterRead"])
    check("nothing new since: not posted again", o["seenPostsAgain"] == 1)
    check("a poll answered 304 with the old count still shows it read (the cached answer is not edited)",
          not o["dotAfterSamePoll"])
    check("...until the server's count moves: a new reply shows again", o["dotAfterNewReply"])
    check("a thread under an open panel is not read", o["postsUnderPanel"] == 1, o["postsUnderPanel"])
    check("a refused post puts the row back, and the next read asks again",
          o["dotAfterRefused"] and o["postsAfterRefused"] == 3, (o["dotAfterRefused"], o["postsAfterRefused"]))
    check("scrolled up the thread is not read", o["postsWhileUp"] == 3, o["postsWhileUp"])
    check("Needs you adds to it (awaiting approval and failed; running does not)",
          o["titleWithNeeds"] == "(4) Silkworm — Sessions", o["titleWithNeeds"])
    check("a new reply in a conversation puts it up by one", o["titleNewReply"] == "(5) Silkworm — Sessions", o["titleNewReply"])
    check("...one in a task run does not", o["titleTaskReply"] == "(5) Silkworm — Sessions", o["titleTaskReply"])
    check("Mark all read is offered while something is unread", o["markAllBtn"])
    check("...posts once, {action: all}, as of when the list on screen was read",
          len(o["markAllPosts"]) == 1 and o["markAllPosts"][0]["action"] == "all"
          and abs(o["markAllPosts"][0]["ts"] - time.time()) < 120, o["markAllPosts"])
    check("...and every row clears at once: only Needs you is left",
          o["titleAllRead"] == "(3) Silkworm — Sessions" and not o["dotsAllRead"], o["titleAllRead"])
    check("...refused, what it cleared comes back (B, with a new reply; the rest were read by the first)",
          o["titleAllRefused"] == "(4) Silkworm — Sessions", o["titleAllRefused"])
    check("nothing unread: the badge is cleared and the title is plain",
          o["badgeCleared"][-1:] == [["clear"]] and o["titleCleared"] == "Silkworm — Sessions", (o["badgeCleared"], o["titleCleared"]))
    check("...and the Unread filter and unread first are gone: nothing to narrow",
          not o["unreadOnlyCleared"] and "unread first" not in o["filterCleared"], o["filterCleared"])


PIN_DRIVE = r"""
const old = 1795000000 - 30 * 86400;
const sess = (key, extra) => Object.assign({key, title: "T " + key, kind: "thread", project: "", summary: "",
  session_id: "s" + key, turns: 3, cost: 0, updated: 1795000000, files: 0, events: [], running: false,
  resume_cmd: "", slack_link: "", hidden: false, unread: 0}, extra || {});
// O, the operator's home: oldest, hidden, a task run, nothing unread -- every
// filter and sort would otherwise put it last or leave it out.
let rows = [sess("A", {unread: 2}), sess("B"), sess("D", {kind: "task", updated: old}),
            sess("O", {kind: "task", hidden: true, updated: old - 1})];
let home = "O";
Object.defineProperty(globalThis, "navigator", {value: {vibrate() { return true; }}, configurable: true});
answer = (url, b) => url.startsWith("/api/sessions")
  ? {bot_online: true, sessions: rows.map(r => Object.assign({}, r)), slack: {}, operator_home: home}
  : url.startsWith("/api/session?") ? {messages: [], cursor: 1, files: [], live: null}
  : url.startsWith("/api/artifacts") ? [] : BASE(url, b);
const flushAsync = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r)); };
const order = () => byId("list").children.filter(c => [...M.cards.values()].includes(c))
  .map(c => [...M.cards.entries()].find(([k, v]) => v === c)[0]);
const look = () => ({order: order(),
  pinned: [...M.cards.entries()].filter(([k, c]) => c.className.split(" ").includes("pinned")).map(([k]) => k),
  labelled: [...M.cards.entries()].filter(([k, c]) => c.innerHTML.includes("📌 Operator")).map(([k]) => k),
  hideBtnOnPin: home && M.cards.get(home) ? M.cards.get(home).innerHTML.includes("hidebtn") : null});
(async () => {
  const out = {};
  await M.loadList(); await flushAsync();
  out.first = look();          // whatever the device opens on
  M.setKind("all"); await flushAsync();
  out.all = look();
  for (const k of ["thread", "task"]) { M.setKind(k); await flushAsync(); out[k] = look(); }
  M.setKind("all"); await flushAsync();
  M.setUnreadOnly(true); await flushAsync(); out.unreadOnly = look();
  M.setUnreadOnly(false); await flushAsync();
  M.setUnreadFirst(true); await flushAsync(); out.unreadFirst = look();
  M.setUnreadFirst(false); await flushAsync();
  M.toggleHidden(); await flushAsync(); out.showHidden = look();
  M.toggleHidden(); await flushAsync();
  out.filterBar = byId("kindfilter").innerHTML;
  // Not hidden, the home is still an old task run: tidy would never touch it, so it is not counted.
  rows[3].hidden = false; await M.loadList(); await flushAsync();
  out.filterBarShown = byId("kindfilter").innerHTML;
  rows[3].hidden = true; await M.loadList(); await flushAsync();
  // A deep link to another thread opens it; the pin stays on top.
  await M.openTarget("/?thread=D"); await flushAsync();
  out.deepActive = M.activeKey(); out.deep = look();
  M.clearActive();
  // !operator in B: the home moves, and the pin with it.
  home = "B"; M.setKind("thread"); await flushAsync();
  out.moved = look();
  M.setKind("all"); await flushAsync(); out.movedAll = look();
  // No home: no pin, the plain newest-first order.
  home = ""; await M.loadList(); await flushAsync();
  out.none = look();
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_operator_home_is_pinned_first_in_the_thread_list():
    import re
    import visualizer as V
    print("\nthe operator's home thread: pinned first under every filter and sort, moves with the home")
    # The server: whichever thread operator.json names, and only one that is a thread.
    d = Path(tempfile.mkdtemp())
    (d / "operator.json").write_text(json.dumps({"thread": "C1:2"}))
    was = (V.BASE_DIR, V.raw_sessions, V.bot_call)
    try:
        V.BASE_DIR = d
        V.bot_call = lambda *a, **k: {}
        V.raw_sessions = lambda: {"C1:1": {"updated": 2}, "C1:2": {"updated": 1}}
        check("/api/sessions names the operator's home, read from operator.json",
              V.load_sessions()["operator_home"] == "C1:2")
        (d / "operator.json").write_text(json.dumps({"thread": "C1:9"}))
        check("...not a home that is no thread here", V.load_sessions()["operator_home"] == "")
        (d / "operator.json").unlink()
        check("...and none without an operator", V.load_sessions()["operator_home"] == "")
        check("...reading it never writes it", not (d / "operator.json").exists())
    finally:
        V.BASE_DIR, V.raw_sessions, V.bot_call = was
    node = shutil.which("node")
    if not node:
        print("  … node not installed — the pin checked on the server only")
        return
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    dom = MOTION_HARNESS[:MOTION_HARNESS.index("(0, eval)(SRC")]
    expose = """
;globalThis.__m = {loadList, setKind, setUnreadOnly, setUnreadFirst, toggleHidden, openTarget, cards: cardEls,
  activeKey() { return active && active.key; }, clearActive() { active = null; }};"""
    t = Path(tempfile.mkdtemp())
    (t / "dash.js").write_text(js)
    (t / "drive.js").write_text(dom + "(0, eval)(SRC + " + json.dumps(expose) + ");\nconst M = globalThis.__m;\n"
                                + PIN_DRIVE)
    p = subprocess.run([node, str(t / "drive.js"), str(t / "dash.js")], capture_output=True, text=True, timeout=90)
    if p.returncode != 0:
        check("the pin harness runs the page's script", False, p.stderr.strip()[-900:])
        return
    o = json.loads(p.stdout)
    for name in ("first", "all", "thread", "task", "unreadOnly", "unreadFirst", "showHidden", "deep"):
        v = o[name]
        check(f"{name}: the home is first, once, marked pinned and labelled Operator",
              v["order"][:1] == ["O"] and v["order"].count("O") == 1
              and v["pinned"] == ["O"] and v["labelled"] == ["O"], v)
    check("...the rest keep their order beneath it",
          o["all"]["order"] == ["O", "A", "B", "D"] and o["thread"]["order"] == ["O", "A", "B"]
          and o["unreadFirst"]["order"] == ["O", "A", "B", "D"]
          and o["task"]["order"] == ["O", "D"] and o["unreadOnly"]["order"] == ["O", "A"]
          and o["showHidden"]["order"] == ["O", "A", "B", "D"], o)
    check("the pinned row has no × (no hide puts it away)", o["all"]["hideBtnOnPin"] is False)
    check("...and is not counted hidden, nor as an old task run to tidy",
          "hidden <b>" not in o["filterBar"] and "tidy <b>1</b>" in o["filterBar"]
          and "tidy <b>1</b>" in o["filterBarShown"], (o["filterBar"], o["filterBarShown"]))
    check("a ?thread= link still opens its thread", o["deepActive"] == "D", o["deepActive"])
    check("the home moved: the pin moves with it, and the old home is an ordinary row again",
          o["moved"]["order"] == ["B", "A"] and o["moved"]["pinned"] == ["B"]
          and o["movedAll"]["order"] == ["B", "A", "D"] and o["movedAll"]["labelled"] == ["B"], (o["moved"], o["movedAll"]))
    check("no home: no pin, newest first",
          o["none"]["order"] == ["A", "B", "D"] and o["none"]["pinned"] == [] and o["none"]["labelled"] == [], o["none"])


QUICK_DRIVE = r"""
const sess = (key, extra) => Object.assign({key, title: "T " + key, kind: "thread", project: "", summary: "",
  session_id: "s" + key, turns: 3, cost: 0, updated: 1795000000, files: 0, events: [], running: false,
  resume_cmd: "", slack_link: "", hidden: false, unread: 0}, extra || {});
let home = "O";
answer = (url, b) => url.startsWith("/api/sessions")
  ? {bot_online: true, sessions: [sess("A"), sess("O")], slack: {}, operator_home: home}
  : url.startsWith("/api/session?") ? {messages: [], cursor: 1, files: [], live: null}
  : url === "/api/send" ? {ok: true, ref: "9.9", replies: [], finished: true}
  : url.startsWith("/api/artifacts") ? [] : BASE(url, b);
const flushAsync = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r)); };
const row = byId("quick"), reply = byId("reply");
let focused = null;
// innerHTML is not parsed here: the row's buttons are made from what was drawn.
row.onHTML = v => {
  for (const m of v.matchAll(/<button type="button" class="qr" data-qi="(\d+)"[^>]*>([^<]*)<\/button>/g)) {
    const b = E("BUTTON"); b.className = "qr"; b.dataset.qi = m[1]; b.textContent = m[2];
    b.focus = () => { focused = b; };
    row.appendChild(b);
  }
};
(0, eval)(SRC + `
;globalThis.__m = {loadList, openSession, drawQuick, quickSend, sendReply,
  activeKey() { return active && active.key; }};`);
const M = globalThis.__m, out = {};
const look = () => ({hidden: !!row.hidden, labels: row.children.map(c => c.textContent)});
const sends = () => calls.filter(c => c.url === "/api/send").map(c => ({key: c.body.key, text: c.body.text}));
const input = () => (reply.listeners.input || []).forEach(f => f({target: reply}));
const click = b => row.listeners.click.forEach(f => f({target: b}));
const key = (b, k) => { const ev = {key: k, target: b, prevented: false, preventDefault() { this.prevented = true; }};
                        row.listeners.keydown.forEach(f => f(ev)); return ev; };
(async () => {
  await M.loadList(); await flushAsync();
  M.openSession("A"); await flushAsync();
  out.other = look();
  M.openSession("O"); await flushAsync();
  out.home = look();
  out.markup = row.innerHTML;
  // A tap sends exactly the chip's words, into this thread, leaving the draft alone.
  reply.value = "half a thought";
  PHONE = false; input();
  out.desktopDraft = look();
  click(row.children[3]); await flushAsync();
  out.sent = sends();
  out.draftKept = reply.value;
  click(row.children[0]); await flushAsync();
  out.sent2 = sends().slice(-1);
  // The phone: keyboard up with a draft hides the row; either alone does not.
  PHONE = true;
  document.body.classList.add("kb"); input(); out.kbDraft = look();
  reply.value = "  "; input(); out.kbBlank = look();
  reply.value = ""; input(); out.kbEmpty = look();
  document.body.classList.remove("kb");
  reply.value = "typing"; input(); out.noKbDraft = look();
  // Sent from the box: the draft is gone, the row is back with the keyboard up.
  document.body.classList.add("kb"); input(); out.beforeSend = look().hidden;
  await M.sendReply(); await flushAsync();
  out.afterSend = look();
  out.typedSend = sends().slice(-1);
  document.body.classList.remove("kb");
  // Keys: arrows move along the row and wrap; others are left to the browser.
  const bs = row.children;
  out.right = key(bs[0], "ArrowRight").prevented && focused === bs[1];
  out.wrap = key(bs[0], "ArrowLeft").prevented && focused === bs[4];
  out.enterLeft = !key(bs[0], "Enter").prevented;
  // The home moved: the chips go with it.
  home = "A"; await M.loadList(); await flushAsync();
  out.moved = look();
  const n = sends().length; M.quickSend(0); await flushAsync();
  out.sentOutsideHome = sends().length - n;
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_operator_thread_has_quick_reply_chips():
    import re
    import visualizer as V
    print("\nquick replies in the operator's thread: one tap sends the words, as typed")
    page = V.PAGE
    check("the composer has the row, above the files and the box, as a labelled toolbar",
          re.search(r'<div id="quick" role="toolbar" aria-label="[^"]+" hidden></div>\s*<div id="chips">', page)
          is not None)
    check("on a phone it scrolls sideways on one line rather than wrapping",
          re.search(r"#quick \{[^}]*overflow-x: auto;[^}]*flex-wrap: nowrap;", page) is not None)
    check("a chip has a visible keyboard focus", "#quick button.qr:focus-visible" in page)
    js = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
    check("the keyboard opening or closing redraws it",
          re.search(r'classList\.toggle\("kb"[^\n]*\n\s*drawQuick\(\);', js) is not None)
    check("a chip sends through sendText, as Send does",
          re.search(r"function quickSend\(i\) \{[^}]*sendText\(t\);", js) is not None
          and "sendText(text, files)" in js)
    node = shutil.which("node")
    if not node:
        print("  … node not installed — the chips checked by text only")
        return
    dom = MOTION_HARNESS[:MOTION_HARNESS.index("(0, eval)(SRC")]
    t = Path(tempfile.mkdtemp())
    (t / "dash.js").write_text(js)
    (t / "drive.js").write_text(dom + QUICK_DRIVE)
    p = subprocess.run([node, str(t / "drive.js"), str(t / "dash.js")], capture_output=True, text=True, timeout=90)
    if p.returncode != 0:
        check("the quick-reply harness runs the page's script", False, p.stderr.strip()[-900:])
        return
    o = json.loads(p.stdout)
    want = ["yes", "next", "not now", "accept all", "tell me more"]
    check("not in another thread", o["other"] == {"hidden": True, "labels": []}, o["other"])
    check("in the operator's home: the five, in order", o["home"] == {"hidden": False, "labels": want}, o["home"])
    check("each a real button (Tab reaches it, Enter and Space press it)",
          o["markup"].count('<button type="button" class="qr"') == 5, o["markup"][:400])
    check("a tap sends exactly its words, to the operator's thread",
          o["sent"] == [{"key": "O", "text": "accept all"}] and o["sent2"] == [{"key": "O", "text": "yes"}],
          (o["sent"], o["sent2"]))
    check("...leaving what was being typed in the box", o["draftKept"] == "half a thought", o["draftKept"])
    check("desktop: shown with a draft (no keyboard covers anything)", o["desktopDraft"]["hidden"] is False)
    check("phone: hidden while the keyboard is up and the draft is not empty", o["kbDraft"]["hidden"] is True)
    check("...shown with the keyboard up and nothing (or only spaces) typed",
          o["kbBlank"]["hidden"] is False and o["kbEmpty"]["hidden"] is False)
    check("...shown with a draft once the keyboard is down", o["noKbDraft"]["hidden"] is False)
    check("sending the typed draft brings the row back",
          o["beforeSend"] is True and o["afterSend"] == {"hidden": False, "labels": want}
          and o["typedSend"] == [{"key": "O", "text": "typing"}], (o["afterSend"], o["typedSend"]))
    check("arrow keys move along the row, wrapping", o["right"] and o["wrap"])
    check("...and Enter is left to the button", o["enterLeft"])
    check("the operator moved threads: no chips in the old home, and none sent from it",
          o["moved"] == {"hidden": True, "labels": []} and o["sentOutsideHome"] == 0, o["moved"])


def test_tidy_never_hides_the_operator_home():
    print("\ntidy keeps the operator's home thread")
    st = tmp_store()
    for k in ("C:1", "C:2"):
        st.update(k, title="old task run " + k, kind="task")
        st._data[k]["updated"] = time.time() - 30 * 86400
    hidden = st.hide_older_than(14, kinds=("task",), keep={"C:2"})
    check("a bulk tidy hides old task runs but not a kept one",
          hidden == ["C:1"] and not st.get("C:2").get("hidden"), hidden)
    bot = (BASE / "bot.py").read_text()
    check("the bot's tidy keeps the operator's home",
          "hide_older_than(days, kinds=kinds, keep={OPERATOR.thread()}" in bot)
    # The route itself, lifted, against a store and an operator home.
    import operator_role
    d = Path(tempfile.mkdtemp())
    op = operator_role.State(d / "operator.json")
    op.set_thread("C:2")
    fn = lift("handle_hide", store=st, OPERATOR=op, log=__import__("logging").getLogger("t"))["handle_hide"]
    st.set_hidden("C:1", False)
    r = fn({"action": "bulk", "days": 14, "kinds": ["task"]})
    check("/hide bulk leaves the home shown", r["keys"] == ["C:1"] and not st.get("C:2").get("hidden"), r)
    r = fn({"action": "hide", "key": "C:2"})
    check("/hide refuses the home itself", r["ok"] is False and not st.get("C:2").get("hidden"), r)
    check("...but hides any other thread", fn({"action": "hide", "key": "C:1"})["ok"] is True)


# --- notification settings: kinds, projects, quiet hours, urgent ----------------

def _ns_at(text, tz="America/Los_Angeles"):
    """The epoch seconds of a local wall time in `tz`."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    return _dt.fromisoformat(text).replace(tzinfo=ZoneInfo(tz)).timestamp()


def _ns_rig(clock_at="2026-11-22T12:00"):
    rig = _push_rig(Path(tempfile.mkdtemp()))
    now = {"t": _ns_at(clock_at)}
    rig.notifier.clock = lambda: now["t"]
    rig.now = now
    return rig


def test_push_settings_suppress_by_project_and_kind():
    print("\nnotification settings switch kinds and projects off, in push.py's send path")
    rig = _ns_rig()
    push = rig.push
    rig.notifier.current = None
    st = rig.store.settings()
    check("by default every kind is on and no project is off",
          all(st["kinds"].values()) and set(st["kinds"]) == set(push.KINDS) and st["projects"] == {},
          str(st))
    check("the kinds are the four the screen offers",
          list(push.KINDS) == ["needs_you", "reply", "health", "results"])
    task = {"id": "tsk_a", "state": rig.T.AWAITING_APPROVAL, "title": "Fix it", "project": "cadence"}
    m_need = push.needs_you(task)
    m_reply = push.reply_message("D1:1.1", "tsk_r", "Garden", project="mondays")
    m_health = push.health_message({"project": "saga", "check": "ci", "summary": "main is red",
                                    "fingerprint": "f1"}, {"id": "tsk_h"})
    m_land = push.landing_message({"id": "tsk_l", "project": "cadence", "title": "Land me"},
                                  {"landed": True, "stage": "landed", "head": "abc"})
    m_deploy = push.deploy_message(":rocket: Deployed: the bot is running `abc`.\nLog: x", "", "D1:2.2")
    check("each message knows its kind and project",
          [(m["kind"], m["project"]) for m in (m_need, m_reply, m_health, m_land, m_deploy)]
          == [("needs_you", "cadence"), ("reply", "mondays"), ("health", "saga"),
              ("results", "cadence"), ("results", "")])
    hitem = push.needs_you({"id": "tsk_h2", "state": rig.T.NEEDS_INPUT, "title": "saga ci: red",
                            "project": "saga", "source": "health", "source_ref": "saga/ci@fp"})
    check("a health item's own needs-you move counts as a health failure, not a needs-you",
          hitem["kind"] == "health" and hitem["signals"] == ["saga/ci"], str(hitem))
    check("a landing under way, or a project that does not land, says nothing",
          push.landing_message({"id": "t"}, {"stage": "in-progress"}) is None
          and push.landing_message({"id": "t"}, {"stage": "not-enabled"}) is None)
    check("a deploy says its first line, unmarked", m_deploy["body"].startswith("rocket: Deployed")
          or m_deploy["body"].startswith(":rocket: Deployed"), m_deploy["body"])

    rig.store.set_settings({"kinds": {"reply": False}, "projects": {"cadence": False}})
    outs = [rig.notifier.deliver(dict(m)) for m in (m_need, m_reply, m_health, m_land, m_deploy)]
    check("a kind switched off is not sent", outs[1]["skipped"] == "reply is switched off", str(outs[1]))
    check("a project switched off is not sent, whatever its kind",
          outs[0]["skipped"] == "cadence is switched off" and outs[3]["skipped"] == "cadence is switched off",
          str(outs))
    check("the rest are", outs[2]["sent"] == 1 and outs[4]["sent"] == 1 and len(rig.service.calls) == 2,
          str(outs))
    check("something without a project follows its kind alone",
          outs[4]["skipped"] == "" and outs[4]["sent"] == 1)
    rig.store.set_settings({})
    again = rig.notifier.deliver(dict(m_need))
    check("switched off is not 'already sent': switched back on, the same move goes out",
          again["sent"] == 1, str(again))
    check("what was sent is still sent once", rig.notifier.deliver(dict(m_health))["skipped"]
          == "already sent")
    rig.store.set_settings({"kinds": {"results": False}})
    check("deploy and landing results are one kind", rig.notifier.deliver(dict(m_deploy))["skipped"]
          == "results is switched off")
    check("the test notification ignores the settings (it is asked for)",
          push.handle({"action": "test"}, rig.store, rig.notifier)["sent"] == 1)


def test_push_quiet_hours_hold_and_flush():
    print("\nquiet hours hold everything, in the owner's zone, and send one summary when they end")
    rig = _ns_rig("2026-11-22T23:30")
    push, ts, T = rig.push, rig.ts, rig.T
    q = {"on": True, "start": "22:00", "end": "07:00", "tz": "America/Los_Angeles"}
    rig.store.set_settings({"quiet": q})
    st = rig.store.settings()
    for wall, want in (("2026-11-22T21:59", False), ("2026-11-22T22:00", True),
                       ("2026-11-23T00:30", True), ("2026-11-23T06:59", True),
                       ("2026-11-23T07:00", False), ("2026-11-23T12:00", False)):
        check(f"{wall[11:]} LA is {'quiet' if want else 'not quiet'} (past midnight is handled)",
              push.in_quiet(st, _ns_at(wall)) is want)
    check("the zone is the owner's: 23:30 in LA is 07:30 UTC, not quiet in a UTC window of the same hours",
          not push.in_quiet(dict(st, quiet=dict(q, tz="UTC")), _ns_at("2026-11-22T23:30")))
    day = dict(q, start="09:00", end="17:00")
    check("a window within one day", push.in_quiet({"quiet": day}, _ns_at("2026-11-23T10:00"))
          and not push.in_quiet({"quiet": day}, _ns_at("2026-11-23T08:00")))
    check("switched off, never quiet", not push.in_quiet({"quiet": dict(q, on=False)},
                                                         _ns_at("2026-11-22T23:30")))

    a = ts.create("g", title="Approve me", project="cadence")
    ts.transition(a["id"], T.RUNNING)
    ts.transition(a["id"], T.AWAITING_APPROVAL)
    b = ts.create("g", title="Moves on", project="cadence")
    ts.transition(b["id"], T.RUNNING)
    ts.transition(b["id"], T.NEEDS_INPUT)
    outs = [rig.notifier.deliver(dict(push.needs_you(ts.get(x["id"])), at=0)) for x in (a, b)]
    outs.append(rig.notifier.deliver(push.reply_message("D1:1.1", "tsk_r", "Garden", project="x")))
    check("during quiet hours nothing is sent; each is held",
          rig.service.calls == [] and all(o["skipped"] == "held for quiet hours" for o in outs), str(outs))
    check("held messages are claimed: the same move does not queue twice",
          rig.notifier.deliver(dict(push.needs_you(ts.get(a["id"])), at=0))["skipped"] == "already sent")
    check("and are on disk: a restart keeps them",
          len(push.PushStore(Path(rig.store._path)).held()) == 3)
    check("while it is still quiet, flushing sends nothing", rig.notifier.flush() is None
          and len(rig.store.held()) == 3)
    ts.transition(b["id"], T.QUEUED, "answered in the night")
    rig.now["t"] = _ns_at("2026-11-23T07:01")
    out = rig.notifier.flush()
    got = _read_pushes(rig)
    check("once they end: one notification, not three", len(got) == 1 and out["sent"] == 1, str(got))
    check("a summary of what is still news (the task that moved on is left out)",
          out["held"] == 3 and out["summarised"] == 2
          and got[0]["title"] == "2 notifications from quiet hours"
          and "Ready for your approval · cadence" in got[0]["body"] and "Silkworm replied" in got[0]["body"]
          and got[0]["url"] == "/?view=inbox", str(got))
    check("sent urgently, since it carries a task", rig.service.calls[0]["urgency"] == "high")
    check("and nothing is held after", rig.store.held() == []
          and push.PushStore(Path(rig.store._path)).held() == [])
    check("flushing again sends nothing", rig.notifier.flush() is None and len(rig.service.calls) == 1)

    rig.now["t"] = _ns_at("2026-11-23T23:00")
    rig.notifier.deliver(push.reply_message("D1:1.1", "tsk_r2", "Garden"))
    rig.now["t"] = _ns_at("2026-11-24T08:00")
    rig.notifier.flush()
    one = _read_pushes(rig)[-1]
    check("one held is sent as itself", one["title"] == "Silkworm replied" and one["url"] == "/?thread=D1:1.1",
          str(one))
    rig.now["t"] = _ns_at("2026-11-24T23:00")
    rig.notifier.deliver(push.reply_message("D1:1.1", "tsk_r3", "Garden", project="mondays"))
    rig.store.set_settings({"quiet": q, "projects": {"mondays": False}})
    rig.now["t"] = _ns_at("2026-11-25T08:00")
    n = len(rig.service.calls)
    left = rig.notifier.flush()
    check("a project switched off during the night stays off at the flush",
          left["skipped"] == "nothing left to say" and len(rig.service.calls) == n, str(left))
    # The network down when they end: kept, and sent at the next try.
    rig.store.set_settings({"quiet": q})
    rig.now["t"] = _ns_at("2026-11-25T23:00")
    rig.notifier.deliver(push.reply_message("D1:1.1", "tsk_r4", "Garden"))
    rig.notifier.deliver(push.reply_message("D1:1.1", "tsk_r5", "Garden"))
    rig.now["t"] = _ns_at("2026-11-26T08:00")
    ep = rig.browsers[0][1]["endpoint"]
    rig.service.raises = {ep}
    down = rig.notifier.flush()
    check("a summary nobody took is kept, not lost", down["sent"] == 0
          and len(rig.store.held()) == 2, str(down))
    rig.service.raises = set()
    up = rig.notifier.flush()
    check("and sent at the next try", up["sent"] == 1 and rig.store.held() == []
          and _read_pushes(rig)[-1]["title"] == "2 notifications from quiet hours", str(up))
    # A health failure in the night that passes again before morning.
    h = ts.create("h", title="saga ci", project="saga", state=T.NEEDS_INPUT, source="health")
    rig.now["t"] = _ns_at("2026-11-26T23:00")
    held = rig.notifier.deliver(push.health_message(
        {"project": "saga", "check": "ci", "summary": "red", "fingerprint": "x"}, h))
    ts.transition(h["id"], T.DONE, "the ci check is ok again")
    rig.now["t"] = _ns_at("2026-11-27T08:00")
    n = len(rig.service.calls)
    gone = rig.notifier.flush()
    check("a health failure that passed again overnight is not in the morning's summary",
          held["skipped"] == "held for quiet hours" and gone["skipped"] == "nothing left to say"
          and len(rig.service.calls) == n, f"{held} {gone}")
    many = push.summary_message([{"title": f"t{i}"} for i in range(7)])
    check("a long night is a count and the first few", many["title"] == "7 notifications from quiet hours"
          and many["body"] == "t0; t1; t2; and 4 more", many["body"])


def test_push_quiet_hours_flush_from_the_sender_and_after_restart():
    print("\nthe sender itself sends the summary once quiet hours end, restart or not")
    tmp = Path(tempfile.mkdtemp())
    rig = _push_rig(tmp)
    push = rig.push
    now = {"t": _ns_at("2026-11-22T23:30")}
    rig.store.set_settings({"quiet": {"on": True, "start": "22:00", "end": "07:00",
                                      "tz": "America/Los_Angeles"}})
    night = now["t"]
    # The process before the restart: it never sees the morning.
    rig.notifier.clock = lambda: night
    rig.notifier.poll = 0.05
    rig.notifier.reply("D1:1.1", "tsk_q", "Garden")
    _wait(lambda: rig.store.held())
    time.sleep(0.2)
    check("queued at night: held, nothing sent", len(rig.store.held()) == 1 and rig.service.calls == [])
    # A restart in the night: a new store and notifier on the same file.
    store2 = push.PushStore(tmp / "push.json")
    n2 = push.Notifier(store2, tmp / "vapid", sender=rig.service, clock=lambda: now["t"], poll=0.05)
    n2.resume()
    check("resume starts the sender when something is held", n2._worker is not None and n2._worker.is_alive())
    idle = push.Notifier(push.PushStore(Path(tempfile.mkdtemp()) / "p.json"), tmp / "vapid")
    idle.resume()
    check("and starts nothing when nothing is", idle._worker is None)
    now["t"] = _ns_at("2026-11-23T07:00")
    _wait(lambda: rig.service.calls)
    time.sleep(0.2)
    check("when they end, the restarted sender sends it on its own, once",
          len(rig.service.calls) == 1 and store2.held() == [], str(len(rig.service.calls)))
    src = (BASE / "bot.py").read_text()
    tree = ast.parse(src)
    main = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func) == "PUSH.resume"]
    check("the bot resumes held notifications at startup", len(main) == 1)


def test_push_urgent_bypasses_quiet_hours():
    print("\nurgent needs-you items (a trader halt, the Gateway down) come through quiet hours, by setting")
    rig = _ns_rig("2026-11-22T23:30")
    push = rig.push
    rig.notifier.current = None
    q = {"on": True, "start": "22:00", "end": "07:00", "tz": "America/Los_Angeles"}
    rig.store.set_settings({"quiet": q})
    check("the urgent list says which, and the defaults are the money-losing three",
          set(push.URGENT) == {"trader-halt", "gateway", "dispatch", "credentials"}
          and rig.store.settings()["urgent"] == {"bypass": True,
                                                 "checks": ["trader-halt", "gateway", "dispatch"]})

    def health(check_, evidence, fp):
        return push.health_message({"project": "trader", "check": check_, "summary": "failing",
                                    "evidence": evidence, "fingerprint": fp}, {"id": "tsk_" + fp})
    halt = health("healthcheck", ["FAIL halt: TRADING HALTED for momentum", "run 2026-11-22"], "h1")
    stale = health("healthcheck", ["FAIL universe: 140 days old"], "u1")
    gw = health("gateway", ["gateway: not listening"], "g1")
    missed = health("healthcheck", ["FAIL dispatch: no heartbeat for the open slot"], "d1")
    check("a halt is told apart from another healthcheck failure by its own check's name",
          "trader/healthcheck:halt" in halt["signals"] and "trader/healthcheck:halt" not in stale["signals"],
          str(halt["signals"]))
    check("a halt item's own move knows it is a halt, from its fingerprint",
          "trader/healthcheck:halt" in push._ref_signals("trader/healthcheck@halt,universe:abc123")
          and push._ref_signals("trader/gateway@g1") == ["trader/gateway"])
    o_halt = rig.notifier.deliver(halt)
    o_gw = rig.notifier.deliver(gw)
    o_stale = rig.notifier.deliver(stale)
    o_missed = rig.notifier.deliver(missed)
    check("a trader halt is sent at night", o_halt["sent"] == 1, str(o_halt))
    check("so is the Gateway down or logged out", o_gw["sent"] == 1, str(o_gw))
    check("so is a missed dispatch the trader's own healthcheck reports (its check is named dispatch)",
          o_missed["sent"] == 1 and "trader/healthcheck:dispatch" in missed["signals"], str(o_missed))
    check("and all are sent as urgent to the push service, so an idle phone wakes",
          [c["urgency"] for c in rig.service.calls] == ["high", "high", "high"], str(rig.service.calls))
    bare = push.health_message({"project": "trader", "check": "gateway", "summary": "down",
                                "fingerprint": "nb"}, None)
    rig.notifier.deliver(bare)
    check("urgent because it bypasses, even with no item to carry a task",
          "task" not in bare and rig.service.calls[-1]["urgency"] == "high", rig.service.calls[-1]["urgency"])
    check("a stale universe waits for morning", o_stale["skipped"] == "held for quiet hours", str(o_stale))
    check("an ordinary needs-you waits too", rig.notifier.deliver(push.needs_you(
        {"id": "tsk_n", "state": rig.T.FAILED, "title": "x", "project": "trader"}))["skipped"]
          == "held for quiet hours")
    rig.store.set_settings({"quiet": q, "urgent": {"bypass": True, "checks": ["gateway"]}})
    check("an item unticked waits", rig.notifier.deliver(health("healthcheck", ["FAIL halt: x"], "h2"))
          ["skipped"] == "held for quiet hours")
    check("a ticked one still comes", rig.notifier.deliver(health("gateway", [], "g2"))["sent"] == 1)
    rig.store.set_settings({"quiet": q, "urgent": {"bypass": False}})
    check("with the bypass off, nothing comes through", rig.notifier.deliver(health("gateway", [], "g3"))
          ["skipped"] == "held for quiet hours")
    rig.store.set_settings({"quiet": q, "kinds": {"health": False}})
    check("urgent does not override switching its kind off",
          rig.notifier.deliver(health("gateway", [], "g4"))["skipped"] == "health is switched off")
    rig.store.set_settings({"quiet": q, "projects": {"trader": False}})
    check("nor its project", rig.notifier.deliver(health("gateway", [], "g5"))["skipped"]
          == "trader is switched off")
    item = push.needs_you({"id": "tsk_hi", "state": rig.T.NEEDS_INPUT, "title": "trader gateway",
                           "project": "trader", "source": "health", "source_ref": "trader/gateway@fp"})
    rig.store.set_settings({"quiet": q})
    check("a Gateway item reaching needs-you by a move comes through too",
          rig.notifier.deliver(item)["sent"] == 1)


def test_push_settings_persist_and_are_checked():
    print("\nnotification settings are kept in push.json, checked, and answered over /push")
    tmp = Path(tempfile.mkdtemp())
    rig = _push_rig(tmp)
    push = rig.push
    want = {"kinds": {"reply": False}, "projects": {"cadence": False, "saga": True},
            "quiet": {"on": True, "start": "23:15", "end": "06:45", "tz": "Europe/London"},
            "urgent": {"bypass": True, "checks": ["gateway", "credentials"]}}
    r = push.handle({"action": "save-settings", "settings": want}, rig.store, rig.notifier)
    check("saved over /push", r["ok"] and r["settings"]["quiet"]["start"] == "23:15", str(r))
    back = push.PushStore(tmp / "push.json").settings()
    check("read back after a restart, exactly", back["kinds"]["reply"] is False
          and back["kinds"]["needs_you"] is True and back["projects"] == {"cadence": False}
          and back["quiet"] == want["quiet"] and back["urgent"]["checks"] == ["gateway", "credentials"],
          str(back))
    check("subscriptions are untouched by it", push.PushStore(tmp / "push.json").count() == 1)
    got = push.handle({"action": "settings"}, rig.store, rig.notifier)
    check("the screen is told the kinds and urgent items with their words",
          got["ok"] and [k["id"] for k in got["kinds"]] == list(push.KINDS)
          and [u["id"] for u in got["urgent"]] == list(push.URGENT)
          and all(u["label"] for u in got["urgent"]) and got["held"] == 0 and "quiet_now" in got)
    for bad, why in (({"kinds": {"pings": False}}, "kinds are"),
                     ({"quiet": {"on": True, "start": "25:00", "end": "07:00"}}, "HH:MM"),
                     ({"quiet": {"on": True, "start": "7pm", "end": "07:00"}}, "HH:MM"),
                     ({"quiet": {"on": True, "start": "07:00", "end": "07:00"}}, "same time"),
                     ({"quiet": {"tz": "Mars/Olympus"}}, "time zone"),
                     ({"urgent": {"checks": ["everything"]}}, "urgent items"),
                     ("nope", "object")):
        r = push.handle({"action": "save-settings", "settings": bad}, rig.store, rig.notifier)
        check(f"refused: {why}", not r["ok"] and why in r["error"], str(r))
    check("and a refusal changes nothing", push.PushStore(tmp / "push.json").settings()["quiet"]
          == want["quiet"])
    raw = json.loads((tmp / "push.json").read_text())
    raw["settings"] = {"kinds": {"bogus": 1}}
    (tmp / "push.json").write_text(json.dumps(raw))
    fallback = push.PushStore(tmp / "push.json")
    check("an unreadable settings block reads as the defaults, subscriptions kept",
          fallback.settings() == push.default_settings() and fallback.count() == 1)
    check("a push.json from before settings existed reads as the defaults",
          push.PushStore(Path(tempfile.mkdtemp()) / "push.json").settings() == push.default_settings())
    check("the held list is capped", push.MAX_HELD >= 50)


def test_push_settings_wired_into_every_send():
    print("\nevery kind reaches the one send path: health, landing, deploy, replies with their project")
    src = (BASE / "bot.py").read_text()
    tree = ast.parse(src)
    fn = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    ah = ast.unparse(fn["announce_health"])
    check("a health check's first failure is pushed, before the Slack channel is even asked",
          "PUSH.event(push.health_message(rec, item))" in ah
          and ah.index("PUSH.event") < ah.index("home_channel()"))
    land = ast.unparse(fn["land_and_record"])
    check("every landing outcome is pushed, Slack thread or not",
          "PUSH.event(push.landing_message(" in land and land.index("PUSH.event") < land.index("if channel"))
    tell = ast.unparse(fn["handle_tell"])
    check("a deploy's report through /tell is pushed as a result",
          "payload.get('kind') == 'deploy'" in tell and "push.deploy_message(" in tell)
    cli = (BASE / "bin" / "silkworm").read_text()
    check("and `silkworm deploy` marks its report as one",
          '"/tell", {"key": thread, "text": text, "kind": "deploy"}' in cli)
    replies = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func) == "PUSH.reply"]
    check("both reply notifications carry their thread's project",
          len(replies) == 2 and all("project" in {k.arg for k in c.keywords} for c in replies))
    psrc = (BASE / "push.py").read_text()
    deliver = ast.unparse(next(n for n in ast.walk(ast.parse(psrc))
                               if isinstance(n, ast.FunctionDef) and n.name == "deliver"))
    check("deliver applies the settings: suppress, then claim, then hold or send",
          deliver.index("suppressed(") < deliver.index("claim(") < deliver.index("in_quiet(")
          < deliver.index("self.send_all("))
    # A health item opening notifies through the bot's own announce_health.
    tmp = Path(tempfile.mkdtemp())
    rig = _push_rig(tmp)
    sent = []
    rig.notifier.event = lambda m: sent.append(m)
    announce = _bot_func("announce_health", PUSH=rig.notifier, push=rig.push,
                         home_channel=lambda: "", task_store=rig.ts)
    announce({"project": "trader", "check": "gateway", "summary": "down"}, {"id": "tsk_1"}, True)
    announce({"project": "trader", "check": "gateway", "summary": "up"}, {"id": "tsk_1"}, False)
    check("opening pushes once, with no home channel; passing again does not",
          len(sent) == 1 and sent[0]["kind"] == "health" and "trader/gateway" in sent[0]["signals"],
          str(sent))


NOTIFY_HARNESS_TAIL = r"""
const FIX = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
let saveAnswer = null;
answer = (url, b) => {
  if (url === "/api/push" && b && b.action === "settings") return FIX.settings;
  if (url === "/api/push" && b && b.action === "save-settings")
    return saveAnswer || {ok: true, settings: b.settings};
  if (url === "/api/projects") return {ok: true, projects: FIX.projects};
  return BASE(url);
};
(0, eval)(SRC + `
;globalThis.__m = {notifySettings, saveNotifySettings, openLink, form: () => boardForm};`);
const M = globalThis.__m, out = {};
const flushAsync = async () => { for (let i = 0; i < 12; i++) await new Promise(r => setImmediate(r)); };
// The modal's inputs as the browser would hold them, read from its markup.
function sync(html) {
  for (const m of html.matchAll(/<input type="(\w+)" id="([\w-]+)"([^>]*)>/g)) {
    const el = byId(m[2]);
    if (m[1] === "checkbox") el.checked = / checked/.test(m[3]);
    else { const v = /value="([^"]*)"/.exec(m[3]); el.value = v ? v[1] : ""; }
  }
}
const toasts = [];
globalThis.toast = t => toasts.push(t);
(async () => {
  await M.notifySettings(); await flushAsync();
  out.html = byId("fbox").innerHTML;
  out.open = byId("fmodal").style.display;
  sync(out.html);
  // Unticked: thread replies, the second project; quiet hours on; the
  // credentials item ticked; the time changed.
  byId("nsk_reply").checked = false;
  byId("nsp_1").checked = false;
  byId("nsq_on").checked = true;
  byId("nsq_start").value = "21:30";
  byId("nsu_credentials").checked = true;
  byId("nsu_gateway").checked = false;
  calls.length = 0;
  await M.saveNotifySettings(); await flushAsync();
  out.saved = calls.filter(c => c.url === "/api/push").map(c => c.body);
  out.closed = byId("fmodal").style.display;
  // A refusal stays open with the reason.
  await M.notifySettings(); await flushAsync();
  sync(byId("fbox").innerHTML);
  saveAnswer = {ok: false, error: "'Mars/Olympus' is not a time zone this machine knows"};
  await M.saveNotifySettings(); await flushAsync();
  out.refused = {err: byId("tferr").textContent, open: byId("fmodal").style.display};
  // The same time twice is caught before asking.
  saveAnswer = null;
  byId("nsq_on").checked = true; byId("nsq_start").value = "07:00"; byId("nsq_end").value = "07:00";
  calls.length = 0;
  await M.saveNotifySettings(); await flushAsync();
  out.same = {err: byId("tferr").textContent, asked: calls.some(c => c.body && c.body.action === "save-settings")};
  // Offline: a toast, no modal drawn over nothing.
  FIX.settings = {ok: false, error: "bot is offline"};
  byId("fbox").innerHTML = ""; byId("fmodal").style.display = "none";
  await M.notifySettings(); await flushAsync();
  out.offline = {open: byId("fmodal").style.display, toast: toasts[toasts.length - 1]};
  FIX.settings = FIX.settings2;
  await M.openLink(new URLSearchParams("modal=notify")); await flushAsync();
  out.link = byId("fbox").innerHTML.includes("Notification settings");
  out.toasts = toasts;
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_dashboard_notification_settings_screen():
    import re
    import push
    import visualizer as V
    print("\nthe notification settings screen: drawn from /push settings, saved as the form says")
    rig = _push_rig(Path(tempfile.mkdtemp()))
    rig.store.set_settings({"projects": {"oldproj": False},
                            "quiet": {"on": False, "start": "22:00", "end": "07:00", "tz": ""}})
    answer_ = push.handle({"action": "settings"}, rig.store, rig.notifier)
    fix = {"settings": answer_, "settings2": answer_,
           "projects": [{"slug": "cadence", "title": "Cadence"}, {"slug": "trader", "title": "trader"},
                        {"slug": "gone", "title": "Gone", "archived": True}]}
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    sect = js[js.index("// --- notification settings"):
              js.index("// Already allowed: keep the bot's copy")]
    check("the screen holds no copy of the kinds or urgent list: both come from the route",
          "needs_you" not in sect and "trader-halt" not in sect and "IB Gateway" not in sect)
    check("it reaches the bot only through /push", "pushCall(" in sect and "/api/push" not in sect)
    check("a header button opens it", 'onclick="notifySettings()"' in V.PAGE)
    node = shutil.which("node")
    if not node:
        print("  … node not installed — the settings screen checked by text only")
        return
    head = MOTION_HARNESS[:MOTION_HARNESS.index("(0, eval)(SRC")]
    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    (d / "drive.js").write_text(head + NOTIFY_HARNESS_TAIL)
    (d / "fix.json").write_text(json.dumps(fix))
    p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js"), "", str(d / "fix.json")],
                       capture_output=True, text=True, timeout=90)
    if p.returncode != 0:
        check("the settings harness runs the page's script", False, p.stderr.strip()[-900:])
        return
    o = json.loads(p.stdout)
    h = o["html"]
    check("opened in the shared modal", o["open"] == "flex" and "Notification settings" in h, h[:200])
    check("a box per kind, all on, in the route's words",
          all(f'id="nsk_{k}" checked' in h for k in push.KINDS)
          and "Deploy and landing results" in h and "Thread replies" in h, h[:900])
    check("a box per live project, archived ones left out, one already off still listed (and off)",
          'id="nsp_0" checked' in h and 'id="nsp_1" checked' in h and 'id="nsp_2">' in h
          and "Cadence" in h and "oldproj" in h and "Gone" not in h, h)
    check("quiet hours with a start, an end and the owner's zone (the browser's, when none is kept)",
          'id="nsq_on">' in h and 'id="nsq_start" value="22:00"' in h and 'id="nsq_end" value="07:00"' in h
          and re.search(r'id="nsq_tz" value="[A-Za-z_]+(/[A-Za-z_]+)*"', h), h)
    check("the urgent items, with the defaults ticked",
          'id="nsu_bypass" checked' in h and 'id="nsu_trader-halt" checked' in h
          and 'id="nsu_gateway" checked' in h and 'id="nsu_credentials">' in h
          and "IB Gateway down or logged out" in h and "Trader halted" in h, h)
    saved = [b for b in o["saved"] if b.get("action") == "save-settings"]
    s = saved[0]["settings"] if saved else {}
    check("Save sends exactly what the form says",
          len(saved) == 1 and s.get("kinds") == {"needs_you": True, "reply": False, "health": True,
                                                 "results": True}
          and s.get("projects") == {"trader": False, "oldproj": False}
          and s["quiet"]["on"] is True and s["quiet"]["start"] == "21:30" and s["quiet"]["end"] == "07:00"
          and s["quiet"]["tz"]
          and s["urgent"] == {"bypass": True, "checks": ["trader-halt", "dispatch", "credentials"]}, str(s))
    check("and what it sends the route takes as it is", push.check_settings(s)["urgent"]["checks"]
          == ["trader-halt", "dispatch", "credentials"] if s else False)
    check("saved, the modal closes and says so", o["closed"] != "flex"
          and any("quiet 21:30–07:00" in t for t in o["toasts"]), str(o["toasts"]))
    check("a refusal is shown in the modal, which stays open",
          "not a time zone" in o["refused"]["err"] and o["refused"]["open"] == "flex", str(o["refused"]))
    check("the same start and end is caught before asking", "same time" in o["same"]["err"]
          and not o["same"]["asked"], str(o["same"]))
    check("the bot offline: a toast, no empty modal", o["offline"]["open"] != "flex"
          and "offline" in (o["offline"]["toast"] or ""), str(o["offline"]))
    check("?modal=notify opens it", o["link"])

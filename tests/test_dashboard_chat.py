"""Conversation threads from the dashboard, and your word typed in its chat.

Run by tests/run.py with every other tests/test_*.py; see tests/harness.py."""

from harness import *  # noqa: F401,F403


# --- conversation threads from the dashboard --------------------------------------
# The dashboard used to be able to answer an existing thread and nothing more:
# no new thread, no files, a toast instead of the message, and a !command's
# reply only ever in Slack. A thread is still the Slack thread -- the same key,
# session and records whichever side speaks -- so what these check is that the
# dashboard goes through the very handlers Slack does (handle_prompt,
# handle_command), and that the pure parts (webthread.py) say what they should.

def _load_dashshots():
    from importlib.machinery import SourceFileLoader
    import importlib.util
    path = str(Path(__file__).resolve().parent.parent / "bin" / "dashshots")
    loader = SourceFileLoader("dashshots_under_test", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_dashshots_stand_in_is_read_only():
    import json as _json
    import threading as _th
    import urllib.request as _ur
    from http.server import ThreadingHTTPServer
    print("\nbin/dashshots' stand-in bot passes reads to the real bot and refuses every action")
    D = _load_dashshots()
    forwarded = []
    D.forward = lambda port, path, payload: forwarded.append((path, payload.get("action"))) or {"ok": True}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), D.stand_in("1"))
    _th.Thread(target=srv.serve_forever, daemon=True).start()

    def post(path, payload):
        req = _ur.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}",
                          data=_json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        with _ur.urlopen(req, timeout=5) as r:
            return _json.loads(r.read())
    try:
        writes = [("/tasks", {"action": "create", "goal": "x"}), ("/tasks", {"action": "accept"}),
                  ("/tasks", {"action": "land"}), ("/tasks", {"action": "run-next"}),
                  ("/web-message", {"text": "hi"}), ("/hide", {"action": "hide"}),
                  ("/titles", {}), ("/summaries", {}), ("/release", {}),
                  ("/releases", {"action": "start"}), ("/releases", {"action": "preview"}),
                  ("/projects", {"action": "new"}), ("/projects", {"action": "ideate"}),
                  ("/learnings", {"action": "add"}), ("/learnings", {"action": "harvest"}),
                  ("/push", {"action": "subscribe"})]
        refused = [post(p, b) for p, b in writes]
        check("every action is refused", all(r.get("ok") is False for r in refused), str(refused))
        check("and none of them reaches the real bot", forwarded == [], str(forwarded))
        reads = [("/tasks", {"action": "board"}), ("/tasks", {"action": "attention"}),
                 ("/tasks", {"action": "task", "id": "t"}), ("/projects", {"action": "list"}),
                 ("/learnings", {"action": "list"}), ("/status", {}),
                 ("/thread-state", {"key": "k"})]
        for p, b in reads:
            post(p, b)
        check("reads are passed through", [f[0] for f in forwarded] == [p for p, _ in reads],
              str(forwarded))
        check("its read actions are only the dashboard's own read actions",
              D.READ_TASKS <= __import__("visualizer").READ_TASK_ACTIONS)
        plan = post("/releases", {"action": "plan", "slug": "x"})
        check("a release plan is the fixed sample, never the real route (it fetches origin)",
              plan.get("confirm") == "sample" and ("/releases", "plan") not in forwarded)
    finally:
        srv.shutdown()
    check("it will not use the other project's simulator",
          "Cadence Test iPhone 17" in D.FORBIDDEN_DEVICES
          and all(name not in D.FORBIDDEN_DEVICES for name, _ in D.DEVICES.values()))


def test_deep_links_cover_every_shot():
    import re
    from urllib.parse import urlparse, parse_qsl
    import visualizer as V
    print("\nevery view bin/dashshots photographs opens from a deep link the page reads")
    D = _load_dashshots()
    js = V.PAGE[V.PAGE.index("async function openLink(q)"):]
    js = js[:js.index("\n}\n")]
    trigger = re.search(r"/\[\?&\]\(([a-z|]+)\)=/\.test\(location\.search", V.PAGE)
    check("the page reads deep links on load", trigger is not None)
    keys = set(trigger.group(1).split("|")) if trigger else set()
    shots = D.views({"thread": "k", "card": "c", "task": "t"}, "p")
    check("it photographs at least fifteen views", len(shots) >= 15, str(len(shots)))
    for name, path in shots:
        q = dict(parse_qsl(urlparse(path).query))
        if not q:
            continue
        ok = any(k in keys for k in q)
        for k, v in q.items():
            ok = ok and f'q.get("{k}")' in js
            if k in ("view", "modal"):
                ok = ok and (f'{k} === "{v}"' in js)
        check(f"{name}: {path} is handled by openLink", ok)
    check("the address is cleared once read, so a reload opens the list",
          'history.replaceState(null, "", "/")' in V.PAGE)


def test_webthread_files_and_note():
    import base64
    import webthread as W
    print("\nfiles from the phone: checked whole, written where Slack's go, and named to the turn")
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 40).decode()
    ok = W.decode_files([{"name": "shot.png", "type": "image/png", "data": png},
                         {"name": "../../etc/notes.txt", "data": "data:text/plain;base64,"
                          + base64.b64encode(b"hello").decode()}])
    check("two files decode", [f["name"] for f in ok] == ["shot.png", "notes.txt"], str(ok)[:200])
    check("a path in a name is reduced to its last part", ok[1]["name"] == "notes.txt")
    check("a data: URL is taken as its base64", ok[1]["data"] == b"hello")
    check("a missing type is guessed from the name", ok[1]["mimetype"] == "text/plain")
    dots = W.decode_files([{"name": "..", "data": png}])[0]["name"]
    check("a name of only dots becomes a generated one", dots.startswith("file-"), dots)

    def refused(files):
        try:
            W.decode_files(files)
        except W.Refused as e:
            return str(e)
        except Exception as e:                 # a crash is not a refusal
            return f"raised {type(e).__name__}"
        return ""
    check("a list is required", "list" in refused({"name": "x"}))
    check("more than ten files on one message are refused",
          "at most" in refused([{"name": f"{i}.txt", "data": png} for i in range(W.MAX_FILES + 1)]))
    check("exactly ten are taken", not refused([{"name": f"{i}.txt", "data": png}
                                               for i in range(W.MAX_FILES)]))
    check("data that is not base64 is refused", "base64" in refused([{"name": "a", "data": "@@@@"}]))
    check("an empty file is refused", "empty" in refused([{"name": "a", "data": ""}]))
    check("a file that is not an object is refused", "name" in refused(["a.png"]))
    saved_max = W.MAX_FILE_BYTES
    try:
        W.MAX_FILE_BYTES = 30
        check("a file over the size limit is refused",
              "over" in refused([{"name": "big.bin", "data": base64.b64encode(b"z" * 31).decode()}]))
        check("one at the limit is taken",
              not refused([{"name": "edge.bin", "data": base64.b64encode(b"z" * 30).decode()}]))
    finally:
        W.MAX_FILE_BYTES = saved_max
    check("one bad file refuses the whole message, before anything is written",
          "base64" in refused([{"name": "fine.txt", "data": png}, {"name": "bad", "data": "!!"}]))

    root = Path(tempfile.mkdtemp())
    out = W.write_files(root, "D1:111.222", ok + ok[:1], now=1700000000)
    where = root / "D1__111.222"
    check("written into uploads/<thread>, named <epoch>-<name> as Slack's are",
          Path(out[0]["path"]) == where / "1700000000-shot.png" and Path(out[0]["path"]).is_file()
          and Path(out[1]["path"]) == where / "1700000000-notes.txt", str(out)[:300])
    check("a second file of the same name that second does not overwrite the first",
          out[2]["path"] != out[0]["path"] and Path(out[2]["path"]).is_file()
          and Path(out[2]["path"]).parent == where, out[2]["path"])
    check("the record says what and how big", out[1]["size"] == 5 and out[0]["mimetype"] == "image/png")
    try:
        W.write_files(root, "D1:111.222", [{"name": "../escaped", "data": b"x", "mimetype": ""}])
        escaped = "written"
    except W.Refused:
        escaped = "refused"
    except Exception as e:
        escaped = f"raised {type(e).__name__}"
    check("a name that would leave the thread's directory is never written",
          escaped == "refused" and not (root / "escaped").exists()
          and not list(root.glob("*escaped")), escaped)
    note = W.attachment_note(out[:2])
    check("the prompt names each saved path", out[0]["path"] in note and out[1]["path"] in note)
    check("an image is named as one, to be Read",
          "an image; use Read to look at it" in note
          and "Read the image before answering" in note, note)
    plain = W.attachment_note([out[1]])
    check("files that are not images are offered, not insisted on",
          "Read them if they are relevant" in plain and "use Read to look" not in plain)
    check("no files, no note", W.attachment_note([]) == "")
    e = W.echo("look at this", ["shot.png"])
    check("Slack sees a dashboard message marked as one, with its files named",
          e.startswith(":globe_with_meridians: _via visualizer:_ look at this")
          and "shot.png" in e, e)


def test_dashboard_starts_and_continues_threads():
    import tasks as T
    print("\na thread starts from the dashboard (with or without a project) and any thread continues there")
    tmp = Path(tempfile.mkdtemp())
    bot = _web_bot(tmp)
    seen = []
    real_prompt = bot.hooks["prompt"]

    def watching(event, say, client):
        key = f"{event['channel']}:{event.get('thread_ts', event['ts'])}"
        e = bot.store.get(key) or {}
        seen.append({"event": dict(event), "project": e.get("project"), "cwd": e.get("cwd")})
        return real_prompt(event, say, client)
    bot.hooks["prompt"] = watching

    r = bot.web({"new": True, "text": "plan the week"})
    check("a new thread answers its key, ref and Slack link",
          r["ok"] and r["key"].startswith("D9:") and r["ref"] == r["key"].split(":", 1)[1]
          and "D9" in r["link"], str(r))
    anchor = bot.posted[0]
    check("its first message is posted in the home channel as the thread's parent",
          anchor["channel"] == "D9" and anchor["thread_ts"] is None
          and "_via visualizer:_ plan the week" in anchor["text"], str(anchor))
    _wait(lambda: bot.prompts)
    ev = seen[0]["event"]
    check("the turn is handle_prompt's, in that thread, as the dashboard",
          ev["channel"] == "D9" and ev["thread_ts"] == ev["ts"] == r["ref"] and ev["_web"]
          and ev["text"] == "plan the week", str(ev))
    check("and nobody files it: no project", not seen[0]["project"], str(seen[0]))
    check("its prompt is the message, with no Slack context read in",
          bot.prompts[0]["prompt"] == "plan the week", bot.prompts[0]["prompt"][:200])
    _wait(lambda: (bot.store.get(r["key"]) or {}).get("session_id"))
    check("the thread's record and session are the ordinary ones",
          (bot.store.get(r["key"]) or {}).get("session_id") == "sid-new")
    task = [t for t in bot.tasks.by_thread(r["key"])]
    check("its turn is a task like any other, ref'd by the message",
          len(task) == 1 and task[0]["source"] == "ui" and task[0]["source_ref"] == r["ref"]
          and _wait(lambda: bot.tasks.get(task[0]["id"])["state"] == T.DONE), str(task)[:300])

    home = tmp / "garden-home"
    bot.projects.ensure("Garden", initial={"scope": {"cwd": str(home)}})
    n = len(bot.prompts)
    r2 = bot.web({"new": True, "text": "what should I plant", "project": "garden"})
    check("a new thread with a project starts", r2["ok"], str(r2))
    check("filing it said so, as !project does", any("Filed under" in x for x in r2["replies"]),
          str(r2["replies"]))
    _wait(lambda: len(bot.prompts) > n)
    at_turn = next(s for s in seen if s["event"]["ts"] == r2["ref"])
    check("it was filed by !project before its first turn ran (project and directory)",
          at_turn["project"] == "garden" and at_turn["cwd"] == str(home), str(at_turn))
    check("so the first turn already runs where the project works",
          bot.prompts[-1]["cwd"] == str(home), bot.prompts[-1]["cwd"])
    check("and the filing reply went into the thread in Slack",
          any(p.get("thread_ts") == r2["ref"] and "Filed under" in (p.get("text") or "")
              for p in bot.posted))
    _wait(lambda: bot.tasks.by_thread(r2["key"]))
    check("its tasks inherit the project",
          all(t["project"] == "garden" for t in bot.tasks.by_thread(r2["key"])))

    before = len(bot.posted)
    for payload, why in (({"new": True, "text": "x", "project": "nope"}, "no project"),
                         ({"new": True, "text": "!reset"}, "start a thread"),
                         ({"key": "D1:5.5", "text": "x", "project": "garden"}, "chosen when"),
                         ({"key": "D1:5.5", "text": "!stop", "files": [{"name": "a", "data": "eA=="}]},
                          "no attachments"),
                         ({"key": "not a key", "text": "x"}, "unknown thread"),
                         # A key names the uploads directory, so it is a key or nothing.
                         ({"key": "D1:../../x", "text": "x"}, "unknown thread"),
                         ({"key": "D1:5.5", "text": "  "}, "empty"),
                         ({"key": "D1:5.5", "text": "x", "files": [{"name": "a", "data": "@@"}]},
                          "base64")):
        try:
            got = bot.web(payload)
        except Exception as e:
            got = {"raised": repr(e)}
        check(f"refused ({why}): {json.dumps(payload)[:60]}",
              got.get("ok") is False and why in got.get("error", ""), str(got))
    check("and nothing refused reached Slack", len(bot.posted) == before,
          str(bot.posted[before:])[:300])

    # A thread born in Slack, continued here: the same key, so the same
    # session, record and lock.
    key = "D1:111.222"
    bot.store.update(key, session_id="sid-slack", cwd=str(tmp), title="from slack", turns=4)
    n = len(bot.prompts)
    r3 = bot.web({"key": key, "text": "and one more thing"})
    _wait(lambda: len(bot.prompts) > n)
    echo = next((p for p in bot.posted if p.get("thread_ts") == "111.222"
                 and "_via visualizer:_ and one more thing" in (p.get("text") or "")), None)
    check("continuing a Slack-born thread posts the message into it in Slack", echo is not None
          and echo["channel"] == "D1", str(bot.posted[-3:]))
    check("and resumes its own session in its own directory",
          bot.prompts[-1]["session_id"] == "sid-slack" and bot.prompts[-1]["cwd"] == str(tmp),
          str(bot.prompts[-1]))
    check("its ref is the turn's task", r3["ok"] and _wait(lambda: any(
        t["source_ref"] == r3["ref"] for t in bot.tasks.by_thread(key))), str(r3))
    # And Slack, after: a reply in Slack to a dashboard-born thread is the
    # same key, so the same session.
    n = len(bot.prompts)
    channel, ts = r["key"].split(":")
    bot.hooks["prompt"]({"channel": channel, "ts": "2000.1", "thread_ts": ts, "user": "U1",
                         "text": "from my laptop"}, lambda **k: None, None)
    check("a Slack reply to a dashboard-born thread resumes the same session",
          bot.prompts[-1]["session_id"] == "sid-new" and len(bot.prompts) == n + 1,
          str(bot.prompts[-1]))


def test_dashboard_upload_reaches_the_turn():
    import base64
    print("\na photo sent from the phone is saved with Slack's uploads and the turn is told to Read it")
    tmp = Path(tempfile.mkdtemp())
    bot = _web_bot(tmp)
    key = "D1:300.1"
    bot.store.update(key, session_id="sid-x", cwd=str(tmp))
    img = b"\x89PNG\r\n\x1a\n" + b"p" * 2048
    r = bot.web({"key": key, "text": "what is wrong here?",
                 "files": [{"name": "IMG_0042.png", "type": "image/png",
                            "data": base64.b64encode(img).decode()}]})
    check("sent", r["ok"], str(r))
    _wait(lambda: bot.prompts)
    saved = list((tmp / "uploads" / "D1__300.1").glob("*IMG_0042.png"))
    check("written into uploads/<thread>, as a Slack upload is",
          len(saved) == 1 and saved[0].read_bytes() == img, str(saved))
    prompt = bot.prompts[0]["prompt"] if bot.prompts else ""
    check("the turn's prompt names the saved file", saved and str(saved[0]) in prompt, prompt[:300])
    check("and says it is an image to Read", "use Read to look at it" in prompt
          and "Read the image before answering" in prompt and "image/png" in prompt, prompt[-300:])
    check("after the message itself", prompt.startswith("what is wrong here?"), prompt[:80])
    files = (bot.store.get(key) or {}).get("files") or []
    check("the thread's file list has it, incoming (so the dashboard lists it)",
          any(f["direction"] == "in" and f["path"] == str(saved[0]) for f in files), str(files))
    check("Slack is told a file came with it",
          any("IMG_0042.png" in (p.get("text") or "") and p.get("thread_ts") == "300.1"
              for p in bot.posted))
    other = _web_bot(Path(tempfile.mkdtemp()))
    (other_root := Path(other.web.__globals__["deliver_web_message"].__globals__["UPLOADS_ROOT"])).write_text("not a directory")
    bad = other.web({"new": True, "text": "look", "files": [
        {"name": "a.png", "type": "image/png", "data": base64.b64encode(img).decode()}]})
    anchor = other.posted[0] if other.posted else {}
    check("a new thread whose files cannot be saved is taken back out of Slack",
          bad["ok"] is False and "could not save" in bad["error"]
          and any(p.get("deleted") == f"D9:{1001}.000100" for p in other.posted)
          and not other.prompts, f"{bad} {other.posted}")
    del other_root, anchor
    n = len(bot.prompts)
    r = bot.web({"key": key, "files": [{"name": "only.png", "type": "image/png",
                                        "data": base64.b64encode(img).decode()}]})
    _wait(lambda: len(bot.prompts) > n)
    check("a photo with no words is sent too", r["ok"] and len(bot.prompts) == n + 1
          and "only.png" in bot.prompts[-1]["prompt"], str(r))


def test_dashboard_message_queues_behind_a_running_turn():
    import tasks as T
    print("\na message sent mid-turn shows as queued, from either side, then runs")
    tmp = Path(tempfile.mkdtemp())
    bot = _web_bot(tmp)
    key = "D1:400.1"
    bot.store.update(key, session_id="sid-q", cwd=str(tmp))
    lock = bot.lock(key)
    lock.acquire()                         # a turn is running in this thread
    bot.running[key] = object()
    try:
        r = bot.web({"key": key, "text": "next, please"})
        check("the message is taken at once", r["ok"] and r["ref"], str(r))
        _wait(lambda: bot.state({"key": key})["queued"])
        s = bot.state({"key": key})
        check("the thread says it is running, with the message queued behind it",
              s["running"] and [q["ref"] for q in s["queued"]] == [r["ref"]]
              and s["queued"][0]["state"] == T.QUEUED, str(s))
        check("and nothing has run it yet", not bot.prompts)
        # The task exists before the progress line is posted: wait for that too.
        check("Slack said it is waiting its turn", _wait(lambda: any(
            "Queued behind an earlier message" in (p.get("update") or p.get("text") or "")
            for p in bot.posted)), str(bot.posted)[:400])
        # A message sent from Slack meanwhile is queued the same way.
        threading_ = __import__("threading")
        threading_.Thread(target=bot.hooks["prompt"], daemon=True, args=(
            {"channel": "D1", "ts": "401.5", "thread_ts": "400.1", "user": "U1",
             "text": "from slack too"}, lambda **k: None, None)).start()
        _wait(lambda: len(bot.state({"key": key})["queued"]) == 2)
        s = bot.state({"key": key})
        check("one sent from Slack meanwhile is queued too, in order",
              [q["ref"] for q in s["queued"]] == [r["ref"], "401.5"]
              and s["queued"][1]["source"] == "slack", str(s["queued"]))
    finally:
        bot.running.pop(key, None)
        lock.release()
    _wait(lambda: len(bot.prompts) == 2)
    _wait(lambda: not bot.state({"key": key})["queued"]
          and not bot.state({"key": key})["running"])
    s = bot.state({"key": key})
    check("once the turn ends both run, one at a time, and the thread is idle",
          [p["prompt"] for p in bot.prompts] == ["next, please", "from slack too"]
          and not s["queued"] and not s["running"], str(bot.prompts))
    check("their turns read as done, by ref",
          {t["ref"]: t["state"] for t in s["turns"]} == {r["ref"]: T.DONE, "401.5": T.DONE},
          str(s["turns"]))
    check("another thread's tasks are not this one's",
          bot.state({"key": "D1:999.9"})["turns"] == [])
    # A review the queue runs narrates into the same thread, but it is not a
    # message in the conversation.
    bot.tasks.create("review it", role="reviewer", driver="queue", thread=key,
                     source="ui", source_ref="rv.1")
    check("queued work the runner drives is not a queued message",
          all(t["ref"] != "rv.1" for t in bot.state({"key": key})["turns"]),
          str(bot.state({"key": key})["turns"]))
    check("a bad key is refused", bot.state({"key": "x"})["ok"] is False)


def test_dashboard_commands_use_the_shared_handler():
    print("\n!commands from the composer are handle_command's, and their replies come back")
    tree = ast.parse((BASE / "bot.py").read_text())
    # handle_web_message decides whether a send is new (its client_id);
    # deliver_web_message is what delivers it.
    outer = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name == "handle_web_message")
    check("handle_web_message delivers only through deliver_web_message",
          {ast.unparse(c.func) for c in ast.walk(outer) if isinstance(c, ast.Call)}
          & {"handle_prompt", "handle_command"} == set()
          and "deliver_web_message" in ast.unparse(outer))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == "deliver_web_message")
    calls = {ast.unparse(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)}
    check("deliver_web_message hands every message to handle_prompt",
          "handle_prompt" in calls, str(sorted(calls)))
    lits = {c.value for c in ast.walk(fn) if isinstance(c, ast.Constant)
            and isinstance(c.value, str) and c.value.startswith("!")}
    joined = [ast.unparse(c) for c in ast.walk(fn) if isinstance(c, ast.JoinedStr)]
    check("it handles no command itself: the only one it names is the !project a new "
          "thread is filed with, through handle_command",
          lits <= {"!", "!project "}
          and [j for j in joined if j.startswith(("f'!", 'f"!'))] == ["f'!project {project}'"]
          and "handle_command" in calls, f"{lits} {joined}")
    prompt = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == "handle_prompt")
    check("and handle_prompt sends a dashboard !command to handle_command, as a Slack one",
          any(isinstance(c, ast.Call) and ast.unparse(c.func) == "handle_command"
              for c in ast.walk(prompt)))

    from unittest.mock import MagicMock
    tmp = Path(tempfile.mkdtemp())
    bot = _web_bot(tmp)
    key = "D1:500.1"
    bot.store.update(key, session_id="sid-c", cwd=str(tmp))
    handle = MagicMock()
    bot.running[key] = handle
    r = bot.web({"key": key, "text": "!stop"})
    check("!stop stops the running turn, as from Slack", handle.stop.called, str(r))
    check("and its reply comes back to the page", r["ok"] and r["finished"]
          and any("Stopping" in x for x in r["replies"]), str(r))
    check("the reply is in the Slack thread too, and the command is not echoed as a prompt",
          any(p.get("thread_ts") == "500.1" and "Stopping" in (p.get("text") or "")
              for p in bot.posted)
          and not any("_via visualizer:_ !stop" in (p.get("text") or "") for p in bot.posted))
    bot.running.pop(key)
    r = bot.web({"key": key, "text": "!brief"})
    check("!brief in an unfiled thread says to file it first",
          any("isn't filed under a project" in x for x in r["replies"]), str(r))
    home = tmp / "kitchen"
    bot.projects.ensure("Kitchen", initial={"scope": {"cwd": str(home)}})
    r = bot.web({"key": key, "text": "!project kitchen"})
    check("!project files the thread through file_thread_under",
          (bot.store.get(key) or {}).get("project") == "kitchen"
          and any("Filed under" in x for x in r["replies"]), str(r))
    r = bot.web({"key": key, "text": "!brief"})
    check("then !brief answers for its project", any("kitchen" in x for x in r["replies"]),
          str(r))
    r = bot.web({"key": key, "text": "!reset"})
    check("!reset clears the session", bot.store.get(key) is None
          and any("Session cleared" in x for x in r["replies"]), str(r))
    check("no command ran a turn", not bot.prompts, str(bot.prompts))


def test_viz_reads_a_thread_by_its_key():
    import http.client
    import threading
    sys.argv = ["x"]
    import visualizer as V
    print("\nthe open thread is read by its key: its live session, from the cursor, with what it is doing")
    d, proj, sessions = _viz_fixture()
    sessions["D1:3.3"] = {"title": "new", "updated": time.time(),
                          "pending": {"started": "2026-10-14T10:00:00.000Z",
                                      "session_id": "sid-three", "prompt": "hi"}}
    sessions["D1:4.4"] = {"title": "fresh", "updated": time.time()}
    (d / "sessions.json").write_text(json.dumps(sessions))
    (proj / "-a" / "sid-one.jsonl").write_text(_tline("user", "hello") + _tline("assistant", "hi"))
    (proj / "-a" / "sid-three.jsonl").write_text(_tline("user", "first turn, live"))
    saved = (V.SESSIONS_FILE, V.PROJECTS_DIR, V.bot_call, V.INDEX_RECHECK_S)
    asked = []
    live = {"ok": True, "running": True, "queued": [{"ref": "9.9", "state": "queued"}], "turns": []}

    def fake_bot(path, payload, timeout=1.0):
        asked.append((path, dict(payload)))
        return dict(live) if path == "/thread-state" else {}
    try:
        V.SESSIONS_FILE, V.PROJECTS_DIR, V.bot_call, V.INDEX_RECHECK_S = (
            d / "sessions.json", proj, fake_bot, 0)
        a = V.load_thread("D1:1.1", "", 0)
        check("a first read is the whole transcript of the thread's session",
              a["session_id"] == "sid-one" and [m["role"] for m in a["messages"]]
              == ["user", "assistant"] and a["cursor"] > 0, str(a)[:300])
        check("with what the thread is doing, from the bot", a["live"] == live
              and asked[-1] == ("/thread-state", {"key": "D1:1.1"}), str(asked[-1:]))
        b = V.load_thread("D1:1.1", "sid-one", a["cursor"])
        check("the next is only what is new", b["messages"] == [] and not b["reset"])
        c = V.load_thread("D1:1.1", "sid-old", a["cursor"])
        check("a thread now on another session reads from the top, and says reset",
              c["reset"] is True and len(c["messages"]) == 2, str(c)[:200])
        t = V.load_thread("D1:3.3", "", 0)
        check("a first turn is followed live, from its pending session",
              t["session_id"] == "sid-three" and t["messages"][0]["blocks"][0]["text"]
              == "first turn, live", str(t)[:200])
        f = V.load_thread("D1:4.4", "", 0)
        check("a thread with no session yet is empty, not an error",
              "error" not in f and f["messages"] == [] and f["session_id"] == "", str(f))
        sessions["D1:4.4"]["pending"] = {"started": "2026-10-14T10:00:00.000Z",
                                         "session_id": "sid-unwritten"}
        (d / "sessions.json").write_text(json.dumps(sessions))
        g = V.load_thread("D1:4.4", "", 0)
        check("nor is a new session that has written nothing yet",
              "error" not in g and g["messages"] == [] and g["session_id"] == "sid-unwritten", str(g))
        V.bot_call = lambda path, payload, timeout=1.0: {}
        check("a bot that does not answer is said to be offline",
              V.load_thread("D1:1.1", "", 0)["live"] == {"offline": True})

        V.bot_call = fake_bot
        srv = V.make_server("127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/session?key=D1%3A1.1&id=&from=0")
        got = json.loads(conn.getresponse().read())
        check("GET /api/session?key= answers the thread", got.get("key") == "D1:1.1"
              and got["live"] == live, str(got)[:200])
        conn.request("GET", "/api/session?id=sid-one&from=0")
        old = json.loads(conn.getresponse().read())
        check("and ?id= alone still reads a session, as before", "live" not in old
              and len(old["messages"]) == 2, str(old)[:200])
        sent = len(asked)
        try:
            # Says it is huge and sends nothing: refused on the header alone.
            big = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            big.putrequest("POST", "/api/send")
            big.putheader("Content-Type", "application/json")
            big.putheader("Content-Length", str(V.MAX_POST_BYTES + 1))
            big.endheaders()
            resp = big.getresponse()
            status, body = resp.status, json.loads(resp.read())
            big.close()
        except Exception as e:
            status, body = None, {"error": repr(e)}
        check("a POST over the size limit is refused before it is read, never relayed",
              status == 413 and "too big" in body["error"] and len(asked) == sent,
              f"{status} {body}")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        payload = json.dumps({"key": "D1:1.1", "text": "hi"}).encode()
        conn.request("POST", "/api/send", body=payload,
                     headers={"Content-Type": "application/json"})
        conn.getresponse().read()
        check("one within it is relayed to /web-message whole",
              asked[-1] == ("/web-message", {"key": "D1:1.1", "text": "hi"}), str(asked[-1:]))
        srv.shutdown()
    finally:
        V.SESSIONS_FILE, V.PROJECTS_DIR, V.bot_call, V.INDEX_RECHECK_S = saved


COMPOSER_DRIVER = r"""
// The composer and the open thread's live end, against a stub DOM: a message
// shows at once, is followed to queued and running by its ref, gives way to the
// transcript, and a command's replies are drawn under it. An element's
// innerHTML is its own markup and its children's, as in a browser.
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
process.on("unhandledRejection", e => { console.error(e); process.exit(1); });
function el(id) {
  return {id, textContent: "", value: "", title: "", className: "", style: {}, dataset: {},
    children: [], _inner: "", parent: null,
    get innerHTML() { return this._inner + this.children.map(c => c.innerHTML).join(""); },
    set innerHTML(v) { this._inner = v; for (const c of this.children) c.parent = null; this.children.length = 0; },
    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
    appendChild(c) {
      if (c.parent) c.parent.children.splice(c.parent.children.indexOf(c), 1);
      c.parent = this; this.children.push(c); return c;
    },
    remove() {}, addEventListener() {}, insertAdjacentHTML(_, h) { this._inner += h; },
    focus() {}, scrollIntoView() {}, querySelector() { return null; },
    querySelectorAll() { return []; }, getBoundingClientRect() { return {bottom: 0}; }};
}
const els = {};
const byId = id => els[id] || (els[id] = el(id));
const listeners = {};
globalThis.document = {getElementById: byId, createElement: () => el("new"), hidden: false,
  querySelector: () => null, querySelectorAll: () => [],
  addEventListener(k, f) { (listeners[k] = listeners[k] || []).push(f); }, body: el("body")};
globalThis.window = {addEventListener() {}, location: {search: ""}};
let isPhone = false;
globalThis.matchMedia = q => ({matches: isPhone && q === "(max-width: 700px)"});
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
globalThis.FileReader = class {
  readAsDataURL(f) {
    this.result = "data:" + f.type + ";base64," + Buffer.from(f.body).toString("base64");
    setImmediate(() => this.onload());
  }
};
const sessions = [{key: "A:1", title: "alpha", kind: "thread", session_id: "s1", updated: 1,
                   turns: 1, cost: 0, resume_cmd: "x", slack_link: "y"}];
let live = {ok: true, running: false, queued: [], turns: []}, deliver = [], cursor = 10;
const sends = [], reads = [];
let gate = null, sendAnswer = null;
globalThis.fetch = async (url, opts) => {
  if (url.startsWith("/api/send")) {
    sends.push(JSON.parse(opts.body));
    if (gate) await gate;
    const a = sendAnswer; sendAnswer = null;
    return {status: 200, headers: {get: () => null}, json: async () => a || {ok: true, ref: "r?", replies: []}};
  }
  let out = {ok: true};
  if (url.startsWith("/api/session?")) {
    reads.push(url);
    const msgs = deliver; deliver = [];
    cursor += msgs.length;
    out = {session_id: "s1", messages: msgs, cursor, reset: false, live: JSON.parse(JSON.stringify(live))};
  } else if (url.startsWith("/api/sessions")) out = {bot_online: true, sessions, slack: {}};
  else if (url.startsWith("/api/stats"))
    out = {total_cost: 1.5, cache_rate: 91, threads: 7, days: [{date: "2026-10-14", cache_read: 5, fresh_in: 2, out: 1}], models: []};
  else if (url.startsWith("/api/artifacts")) out = [];
  else if (url.startsWith("/api/projects")) out = {ok: true, projects: [{slug: "garden", title: "Garden"}, {slug: "old", title: "Old", archived: true}]};
  else if (url.startsWith("/api/tasks")) out = {ok: true, roles: []};
  return {status: 200, headers: {get: () => null}, json: async () => out};
};
(0, eval)(src + `;globalThis.__c = {loadList, loadTranscript, loadStats, sendReply, sendText, addFiles,
  threadBusy, newThreadForm, submitNewThread, getActive: () => active,
  setActive(k) { active = lastSessions.find(s => s.key === k); }};`);
const tick = async (n) => { for (let i = 0; i < (n || 6); i++) await new Promise(r => setImmediate(r)); };
const user = text => ({role: "user", ts: "", blocks: [{type: "text", text}]});
const said = text => ({role: "assistant", ts: "", blocks: [{type: "text", text}]});
(async () => {
  const C = globalThis.__c, out = {};
  const tl = () => byId("tlive").innerHTML;
  await C.loadList();
  C.setActive("A:1");
  await C.loadTranscript(true);
  out.idle = {live: tl(), busy: C.threadBusy()};

  // Typed, with a photo; Send. Shown before the request has answered.
  byId("reply").value = "please fix the header";
  C.addFiles([{name: "shot.png", type: "image/png", size: 4, body: "PNG!"}], "reply");
  out.chips = byId("chips").innerHTML;
  let release; gate = new Promise(r => { release = r; });
  sendAnswer = {ok: true, ref: "r1", replies: []};
  const sending = C.sendReply();
  await tick();
  out.sending = {live: tl(), box: byId("reply").value, chips: byId("chips").innerHTML, busy: C.threadBusy()};
  release(); gate = null;
  await sending; await tick();
  out.body = sends[sends.length - 1];
  out.sent = tl();

  // A turn is running; the message waits behind it.
  live = {ok: true, running: true, queued: [{ref: "r1", state: "queued"}],
          turns: [{ref: "r0", state: "running"}, {ref: "r1", state: "queued"}]};
  await C.loadTranscript(false);
  out.queued = tl();
  // Its turn starts: the transcript has it, so the page stops drawing it.
  live = {ok: true, running: true, queued: [], turns: [{ref: "r0", state: "done"}, {ref: "r1", state: "running"}]};
  deliver = [user("please fix the header\n\n[The user attached 1 file(s)...]")];
  await C.loadTranscript(false);
  out.running = {live: tl(), msgs: byId("tmsgs").innerHTML, busy: C.threadBusy()};
  // The reply streams in, then the turn ends.
  deliver = [said("Fixed: the header now wraps.")];
  live = {ok: true, running: false, queued: [], turns: [{ref: "r1", state: "done"}]};
  await C.loadTranscript(false);
  out.done = {live: tl(), msgs: byId("tmsgs").innerHTML, busy: C.threadBusy()};

  // One that fails: said, and the thread stops being polled fast.
  sendAnswer = {ok: true, ref: "r3", replies: []};
  await C.sendText("this one fails"); await tick();
  live = {ok: true, running: false, queued: [], turns: [{ref: "r1", state: "done"},
          {ref: "r3", state: "failed", why: "boom"}]};
  await C.loadTranscript(false);
  out.failedMine = {live: tl(), busy: C.threadBusy()};
  // Sent again with the same words: an older message saying so does not
  // count as this one having arrived.
  sendAnswer = {ok: true, ref: "r4", replies: []};
  await C.sendText("this one fails"); await tick();
  live = {ok: true, running: true, queued: [], turns: [{ref: "r3", state: "failed", why: "boom"},
          {ref: "r4", state: "running"}]};
  deliver = [Object.assign(user("this one fails"), {ts: "2026-01-01T00:00:00Z"})];
  await C.loadTranscript(false);
  out.oldEcho = tl();
  deliver = [Object.assign(user("this one fails"), {ts: new Date().toISOString()})];
  live = {ok: true, running: true, queued: [], turns: [{ref: "r4", state: "running"}]};
  await C.loadTranscript(false);
  out.newEcho = tl();
  live = {ok: true, running: false, queued: [], turns: [{ref: "r4", state: "done"}], checked_out: true};
  await C.loadTranscript(false);
  out.checkedOut = tl();

  // A command: handle_command's replies, drawn under it.
  sendAnswer = {ok: true, ref: "r2", finished: true, replies: [":card_index_dividers: *garden* brief:\nGrow tomatoes."]};
  await C.sendText("!brief");
  await tick();
  out.command = {live: tl(), body: sends[sends.length - 1]};

  // Waiting messages sent from Slack, and a last turn that failed there.
  live = {ok: true, running: true, queued: [{ref: "s5", state: "queued"}],
          turns: [{ref: "s4", state: "failed", why: "claude exited 1: overloaded"},
                  {ref: "s5", state: "queued", title: "from my laptop", source: "slack"}]};
  await C.loadTranscript(false);
  out.fromSlack = tl();
  live = {ok: true, running: false, queued: [], turns: [{ref: "s4", state: "failed", why: "claude exited 1: overloaded"}]};
  await C.loadTranscript(false);
  out.failed = tl();

  // Not sent: what was typed is put back.
  sendAnswer = {ok: false, error: "bot is offline"};
  byId("reply").value = "try again later";
  await C.sendReply(); await tick();
  out.refused = {box: byId("reply").value, toast: byId("toast").textContent, live: tl()};

  // Return on a phone is a new line, not Send.
  isPhone = true;
  const n = sends.length;
  byId("reply").value = "line one";
  for (const f of listeners.keydown) f({target: {id: "reply"}, key: "Enter", shiftKey: false, preventDefault() {}});
  await tick();
  out.phoneEnter = sends.length - n;
  isPhone = false;
  for (const f of listeners.keydown) f({target: {id: "reply"}, key: "Enter", shiftKey: false, preventDefault() {}});
  await tick();
  out.desktopEnter = sends.length - n;

  // A new thread, filed under a project.
  await C.newThreadForm();
  out.form = byId("fbox").innerHTML;
  byId("nttext").value = "what should I plant";
  byId("ntproj").value = "garden";
  sendAnswer = {ok: true, key: "D9:77.1", ref: "77.1", link: "https://slack/x", replies: ["Filed under *Garden*"]};
  live = {ok: true, running: true, queued: [], turns: [{ref: "77.1", state: "running"}]};
  await C.submitNewThread(); await tick();
  out.newBody = sends[sends.length - 1];
  out.newActive = C.getActive().key;
  out.newRead = reads[reads.length - 1];
  out.newLive = tl();
  out.modal = byId("fmodal").style.display;

  // The stats, where a phone can reach them: atop the session list.
  await C.loadStats();
  out.stats = {phone: byId("dashphone").innerHTML.includes("cache hit rate"),
               desk: byId("dash").innerHTML.includes("cache hit rate"),
               summary: byId("dashsum").textContent};
  process.stdout.write(JSON.stringify(out));
})();
"""


def test_dashboard_composer_and_live_thread():
    import re
    sys.argv = ["x"]
    import visualizer as V
    print("\nthe composer: sent at once, followed to queued and running, replies and files, new threads")
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    for i in ("tlive", "chips", "attach", "dashm", "dashphone", "dashsum"):
        check(f"the page has #{i}", f'id="{i}"' in V.PAGE)
    check("the file picker takes several, of any kind (the phone offers photos and files)",
          re.search(r'<input type="file" id="attach" multiple onchange', V.PAGE) is not None)
    check("a button starts a new thread", 'onclick="newThreadForm()"' in V.PAGE)
    css = re.search(r"<style>(.*?)</style>", V.PAGE, re.S).group(1)
    phone = css[css.index("@media (max-width: 700px)"):]
    check("on a phone the stats sit atop the list, and not above the thread",
          "#dashm { display: block; }" in phone and "#dash { display: none; }" in phone
          and "#dashm { display: none;" in css[:css.index("@media (max-width: 700px)")])
    node = shutil.which("node")
    if not node:
        print("  … node not installed — cannot run the page's own javascript")
        return
    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    chk = subprocess.run([node, "--check", str(d / "dash.js")], capture_output=True, text=True)
    check("the page's script parses (node --check)", chk.returncode == 0, chk.stderr[-400:])
    (d / "drive.js").write_text(COMPOSER_DRIVER)
    p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js")],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        check("the composer's javascript runs", False, p.stderr.strip()[-800:])
        return
    o = json.loads(p.stdout)
    check("an idle thread shows no working indicator, and is not polled fast",
          "Working" not in o["idle"]["live"] and o["idle"]["busy"] is False, str(o["idle"]))
    check("a picked file shows as a chip", "shot.png" in o["chips"], o["chips"])
    s = o["sending"]
    check("the message appears at once, before the request answers",
          "please fix the header" in s["live"] and "sending…" in s["live"]
          and "📎 shot.png" in s["live"], s["live"][:400])
    check("the composer is cleared, files too", s["box"] == "" and s["chips"] == "", str(s))
    check("and the open thread is polled fast meanwhile", s["busy"] is True)
    b = o["body"]
    check("the photo goes as base64 with its name and type",
          b["key"] == "A:1" and b["text"] == "please fix the header"
          and b["files"] == [{"name": "shot.png", "type": "image/png", "data": "UE5HIQ=="}], str(b))
    check("once taken it is sent, awaiting its turn", "sent" in o["sent"], o["sent"][:300])
    check("sent mid-turn it shows as queued, with the turn working",
          "queued — runs when the turn in progress finishes" in o["queued"]
          and "Working" in o["queued"] and "■ Stop" in o["queued"], o["queued"][:500])
    r = o["running"]
    check("when the transcript has it, it is drawn once, from the transcript",
          "please fix the header" in r["msgs"] and "please fix the header" not in r["live"]
          and "Working" in r["live"] and r["busy"] is True, str(r)[:500])
    dn = o["done"]
    check("the reply appears with no refresh, and working ends",
          "Fixed: the header now wraps." in dn["msgs"] and "Working" not in dn["live"]
          and dn["busy"] is False, str(dn)[:400])
    fm = o["failedMine"]
    check("a turn that failed says so on its message, and the fast poll stops",
          "⚠ failed: boom" in fm["live"] and fm["busy"] is False, str(fm)[:400])
    check("an older message with the same words is not taken for this one",
          "starting…" in o["oldEcho"], o["oldEcho"][-400:])
    check("its own is, though the failed one said the same words",
          "starting…" not in o["newEcho"] and "Working" in o["newEcho"], o["newEcho"][:500])
    check("a thread checked out to the terminal says so, with the way back",
          "checked out to the terminal" in o["checkedOut"] and "!back" in o["checkedOut"],
          o["checkedOut"][-300:])
    c = o["command"]
    check("a command is sent as it is typed, under an id of its own",
          {k: v for k, v in c["body"].items() if k != "client_id"}
          == {"key": "A:1", "text": "!brief", "files": []}
          and len(c["body"].get("client_id") or "") >= 8, str(c["body"]))
    check("and its reply is drawn under it", "!brief" in c["live"] and "you · command" in c["live"]
          and "Grow tomatoes." in c["live"] and "silkworm" in c["live"], c["live"][:500])
    check("a message queued from Slack shows as queued too",
          "from my laptop" in o["fromSlack"] and "you · slack" in o["fromSlack"]
          and "queued — runs when" in o["fromSlack"], o["fromSlack"][:500])
    check("a last turn that failed says why", "The last turn failed: claude exited 1: overloaded"
          in o["failed"], o["failed"][:300])
    rf = o["refused"]
    check("a message the bot refuses is put back in the composer, and said",
          rf["box"] == "try again later" and "bot is offline" in rf["toast"]
          and "try again later" not in rf["live"], str(rf))
    check("Return on a phone is a new line; on a laptop it sends",
          o["phoneEnter"] == 0 and o["desktopEnter"] == 1, f"{o['phoneEnter']} {o['desktopEnter']}")
    check("the new thread form offers the live projects",
          'value="garden"' in o["form"] and 'value="old"' not in o["form"]
          and "(no project)" in o["form"], o["form"][:300])
    check("starting it sends new, the project and the message, under an id of its own",
          {k: v for k, v in o["newBody"].items() if k != "client_id"}
          == {"new": True, "text": "what should I plant", "project": "garden", "files": []}
          and len(o["newBody"].get("client_id") or "") >= 8, str(o["newBody"]))
    check("and opens the new thread, read by its key",
          o["newActive"] == "D9:77.1" and "key=D9%3A77.1" in o["newRead"] and o["modal"] == "none",
          f"{o['newActive']} {o['newRead']}")
    check("showing the message and that it was filed",
          "what should I plant" in o["newLive"] and "Filed under" in o["newLive"]
          and "Working" in o["newLive"], o["newLive"][:400])
    st = o["stats"]
    check("the stats are drawn atop the phone's list too, summed up in its line",
          st["phone"] and st["desk"] and "$1.50" in st["summary"] and "7 threads" in st["summary"],
          str(st))


# --- your word, typed in the dashboard's chat ----------------------------------
# 2026-11-17: the owner's dashboard instructions to the operator were relayed
# with no grant, so it could not accept, file on request or run a planned op,
# and asked for everything again in Slack. The dashboard now vouches for a
# message that is verifiably the owner's (dashowner.verify), and the bot opens
# the same grant a Slack message does -- recording which it was.

OWNER = "Owner@Example.com"


class _FakeProbe:
    """The machine, as dashowner.verify reads it: the mini's own tailnet
    addresses, who Tailscale says a peer is, and who opened the socket."""

    def __init__(self, mine=(MINI, "fd7a:115c:a1e0::1"), conn=None, whois=None):
        self.mine = frozenset(mine)
        self.conn = ({"pid": 321, "uid": 0, "name": "tailscaled",
                      "path": "/opt/tailscale/bin/tailscaled"} if conn is None else conn)
        self.who = {PHONE: {"login": "owner@example.com", "node": "phone1"},
                    MINI: {"login": "owner@example.com", "node": "mac-mini"}
                    } if whois is None else whois
        self.asked = []

    def cli_beside(self, path):
        self.asked.append(("cli", path))
        return path.rsplit("/", 1)[0] + "/tailscale" if path.startswith("/opt/") else ""

    def self_ips(self, ts):
        self.asked.append(("self", ts))
        return self.mine

    def connector(self, client, server):
        self.asked.append(("conn", client, server))
        return dict(self.conn)

    def whois(self, ip, ts):
        self.asked.append(("whois", ip, ts))
        return dict(self.who.get(ip) or {})


def _serve_headers(login=OWNER, peer=PHONE, **extra):
    h = {"X-Forwarded-For": peer, "X-Forwarded-Host": "mini.tail1234.ts.net",
         "X-Forwarded-Proto": "https", "Content-Type": "application/json",
         "Origin": "https://mini.tail1234.ts.net", "Sec-Fetch-Site": "same-origin"}
    if login is not None:
        h["Tailscale-User-Login"] = login
    h.update(extra)
    return {k: v for k, v in h.items() if v is not None}


def _verify(headers, probe=None, allowed=OWNER):
    """dashowner.verify, fed by the visualizer's own gates as _owner feeds it."""
    import dashowner as D
    import visualizer as V
    saved = os.environ.get("VIZ_TAILSCALE_LOGIN")
    os.environ["VIZ_TAILSCALE_LOGIN"] = allowed
    try:
        raw = headers.get("Tailscale-User-Login") or ""
        return D.verify(headers, ("127.0.0.1", 55072), ("127.0.0.1", 8790),
                        allowed=allowed, forged=V.cross_site_refusal(headers), proxied=V.proxied(headers),
                        login=V._decode_login(raw) if raw.strip() else "",
                        probe=probe or _FakeProbe())
    finally:
        if saved is None:
            os.environ.pop("VIZ_TAILSCALE_LOGIN", None)
        else:
            os.environ["VIZ_TAILSCALE_LOGIN"] = saved


def test_dashboard_vouches_only_for_the_owner_from_another_device():
    print("\nthe dashboard vouches for a message as yours only via serve, as you, "
          "from another tailnet device, on tailscaled's connection")
    owner, why = _verify(_serve_headers())
    check("your login, through serve, from your phone: vouched for, with its device",
          owner == {"login": OWNER, "peer": PHONE, "node": "phone1"} and not why,
          (owner, why))
    owner, why = _verify(_serve_headers(peer=MINI))
    check("the same login from the mini's own tailnet address is not (an agent's)",
          owner is None and "this machine itself" in why, why)
    owner, why = _verify(_serve_headers(peer="fd7a:115c:a1e0::1"))
    check("nor from its v6 address", owner is None and "this machine itself" in why, why)
    owner, why = _verify({"Content-Type": "application/json"})
    check("a loopback request with no Tailscale identity is not",
          owner is None and "not through tailscale serve" in why, why)
    owner, why = _verify(_serve_headers(login=None))
    check("nor a proxied one missing a login", owner is None and why, why)
    owner, why = _verify(_serve_headers(login="intruder@example.com"))
    check("nor a mismatched login", owner is None and why, why)
    owner, why = _verify(_serve_headers(), allowed="")
    check("nor anything while VIZ_TAILSCALE_LOGIN is unset", owner is None and why, why)
    for label, extra in (("not JSON", {"Content-Type": "text/plain"}),
                         ("cross-site", {"Sec-Fetch-Site": "cross-site"}),
                         ("from another origin", {"Origin": "https://evil.example"})):
        owner, why = _verify(_serve_headers(**extra))
        check(f"nor one that failed the cross-site checks ({label})",
              owner is None and why.startswith("Refused"), why)
    probe = _FakeProbe(conn={"pid": 4242, "uid": 501, "name": "python3",
                             "path": "/opt/tailscale/bin/python3"})
    owner, why = _verify(_serve_headers(), probe)
    check("nor one whose connection a user process opened (headers typed on the mini)",
          owner is None and "tailscaled" in why, why)
    owner, why = _verify(_serve_headers(), _FakeProbe(conn={
        "pid": 9, "uid": 501, "name": "tailscaled", "path": "/opt/tailscale/bin/tailscaled"}))
    check("nor a tailscaled not running as root", owner is None and "tailscaled" in why, why)
    owner, why = _verify(_serve_headers(), _FakeProbe(conn={}))
    check("nor one whose connection's owner can't be found",
          owner is None and "not opened by tailscaled" in why, why)
    probe = _FakeProbe(conn={"pid": 321, "uid": 0, "name": "tailscaled",
                             "path": "/Users/me/.local/bin/tailscaled"})
    owner, why = _verify(_serve_headers(), probe)
    check("nor one with no tailscale CLI beside the root tailscaled (never PATH's)",
          owner is None and "CLI beside" in why, why)
    probe = _FakeProbe()
    _verify(_serve_headers(), probe)
    check("whois and the mini's addresses are asked of the CLI beside tailscaled",
          ("self", "/opt/tailscale/bin/tailscale") in probe.asked
          and ("whois", PHONE, "/opt/tailscale/bin/tailscale") in probe.asked, probe.asked)
    owner, why = _verify(_serve_headers(), _FakeProbe(mine=()))
    check("nor any while the mini's own addresses can't be read",
          owner is None and "own tailnet addresses" in why, why)
    owner, why = _verify(_serve_headers(), _FakeProbe(whois={PHONE: {
        "login": "someone@else.com", "node": "theirs"}}))
    check("nor one Tailscale says is another user's device", owner is None and why, why)
    for peer in ("192.168.1.20", "127.0.0.1", f"{PHONE}, 100.64.0.1", ""):
        owner, why = _verify(_serve_headers(peer=peer or None))
        check(f"nor one whose peer is {peer or 'missing'!r}", owner is None and why, why)
    probe = _FakeProbe()
    _verify(_serve_headers(login="intruder@example.com"), probe)
    _verify(_serve_headers(peer="192.168.1.20"), probe)
    check("the machine is probed only for a request that is yours on its face "
          "(your login, from a tailnet address)", probe.asked == [], probe.asked)
    yes = _FakeProbe(whois=types.SimpleNamespace(
        get=lambda ip: {"login": "owner@example.com", "node": "anything"}))
    owner, why = _verify(_serve_headers(peer="192.168.1.20"), yes)
    check("a peer off the tailnet is refused even if whois would answer for it",
          owner is None and "tailnet peer" in why, why)


def test_dashboard_reads_the_socket_owner_from_netstat():
    import dashowner as D
    print("\ndashowner: the connection's opener, read off netstat -anv")
    out = (
        "Active Internet connections (including servers)\n"
        "Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)   "
        "rxbytes txbytes rhiwat shiwat process:pid state options\n"
        "tcp4       0      0  127.0.0.1.8790         127.0.0.1.55072        ESTABLISHED "
        "362 1442 408192 146988 Python:28252  02031 00000004 0000000000863884 00000080 01000800 2 0 000000\n"
        "tcp4       0      0  127.0.0.1.55072        127.0.0.1.8790         ESTABLISHED "
        "1442 362 408192 146988 tailscaled:321  00182 00000008 0000000000863883 00000080 04000900 2 0 000000\n"
        "tcp4       0      0  127.0.0.1.55099        127.0.0.1.8790         ESTABLISHED "
        "10 10 1 1 Google Chrome H:777  02031 00000004 0000000000863885 00000080 01000800 2 0 000000\n")
    check("the client end's pid, not the server's",
          D.socket_owner(out, ("127.0.0.1", 55072), ("127.0.0.1", 8790)) == 321)
    check("a name with spaces still gives its pid",
          D.socket_owner(out, ("127.0.0.1", 55099), ("127.0.0.1", 8790)) == 777)
    forged = ("tcp4 0 0 127.0.0.1.60000 127.0.0.1.8790 ESTABLISHED 0 461 408300 146988 "
              "c:321 d:4242 00182 00000008 0000000000863883 00000080 04000900 2 0 000000\n")
    check("a binary named to print a fake :pid first gives its own pid, not tailscaled's",
          D.socket_owner(forged, ("127.0.0.1", 60000), ("127.0.0.1", 8790)) == 4242)
    check("a connection in another state is not read",
          D.socket_owner(out.replace("ESTABLISHED", "CLOSE_WAIT"),
                         ("127.0.0.1", 55072), ("127.0.0.1", 8790)) == 0)
    check("no such connection: 0",
          D.socket_owner(out, ("127.0.0.1", 1), ("127.0.0.1", 8790)) == 0)
    calls = []

    def run(argv, timeout):
        calls.append(argv)
        if argv[0] == D.NETSTAT:
            return 0, out, ""
        return 0, "    0 /opt/homebrew/opt/tailscale/bin/tailscaled\n", ""
    got = D.Probe(run=run).connector(("127.0.0.1", 55072), ("127.0.0.1", 8790))
    check("Probe.connector names the process, its uid and path from ps",
          got == {"pid": 321, "uid": 0, "name": "tailscaled",
                  "path": "/opt/homebrew/opt/tailscale/bin/tailscaled"}
          and calls[-1] == [D.PS, "-o", "uid=,comm=", "-p", "321"], (got, calls))
    if True:
        def ts_run(argv, timeout):
            seen.append(argv)
            if argv[1] == "whois":
                return 0, json.dumps({"Node": {"Name": "phone1.tail.ts.net.",
                                               "ComputedName": "phone1"},
                                      "UserProfile": {"LoginName": "owner@example.com"}}), ""
            return 0, json.dumps({"Self": {"TailscaleIPs": [MINI, "not-an-ip"]}}), ""
        seen = []
        pr = D.Probe(run=ts_run)
        who = pr.whois(PHONE, "/x/tailscale")
        check("whois asks `tailscale whois --json <ip>` -- the flag before the address, "
              "which tailscale otherwise reads as a second peer",
              ["/x/tailscale", "whois", "--json", PHONE] in seen
              and who == {"login": "owner@example.com", "node": "phone1"}, (seen, who))
        check("and the mini's own addresses come from `tailscale status --json`",
              pr.self_ips("/x/tailscale") == frozenset({MINI}))
        check("a non-address is never handed to whois", pr.whois("-x", "/x/tailscale") == {}
              and not any("-x" in a for a in seen))
        odd = D.Probe(run=lambda a, t: (0, json.dumps({"Self": "x", "Node": [1],
                                                       "UserProfile": 3}), ""))
        check("odd JSON shapes answer nothing rather than raising",
              odd.self_ips("/x/tailscale") == frozenset() and odd.whois(PHONE, "/x/t") == {})
    tdir = Path(tempfile.mkdtemp())
    (tdir / "tailscaled").write_text("")
    (tdir / "tailscale").write_text("#!/bin/sh\n")
    (tdir / "tailscale").chmod(0o755)
    check("cli_beside finds the CLI next to the daemon, and nothing for a relative path",
          D.Probe.cli_beside(str(tdir / "tailscaled")) == str(tdir / "tailscale")
          and D.Probe.cli_beside(str(tdir / "sub" / "tailscaled")) == "")
    here = os.getcwd()
    try:
        os.chdir(tdir)       # a "tailscale" right here must not answer for a bare name
        check("nor for a relative daemon path, whatever is in the current directory",
              D.Probe.cli_beside("tailscaled") == "")
    finally:
        os.chdir(here)
    got = D.Probe(run=lambda a, t: (1, "", "boom")).connector(("127.0.0.1", 55072),
                                                               ("127.0.0.1", 8790))
    check("and nothing when netstat fails", got == {}, got)


def test_viz_send_vouches_for_the_owner_only():
    import threading
    import urllib.error
    import urllib.request
    import visualizer as V
    print("\n/api/send: says the owner sent it only when dashowner.verify does, "
          "and never what the page claims")
    saved_env = os.environ.get("VIZ_TAILSCALE_LOGIN")
    saved = (V.LOOPBACK, V.TOKEN, V.bot_call, V.OWNER_PROBE)
    sent = []
    srv = None
    try:
        os.environ["VIZ_TAILSCALE_LOGIN"] = OWNER
        V.LOOPBACK, V.TOKEN = True, ""
        V.bot_call = lambda path, payload, timeout=1.0: sent.append((path, payload)) or {"ok": True}
        probe = _FakeProbe()
        V.OWNER_PROBE = probe
        srv = V.make_server("127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        V.Handler.__init__  # the class the server serves

        def send(headers, body=None):
            sent.clear()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/send", headers=headers, method="POST",
                data=json.dumps(body or {"key": "D1:1.0", "text": "accept tsk_1"}).encode())
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    code = r.status
            except urllib.error.HTTPError as e:
                code = e.code
            return code, (sent[-1][1] if sent else None)

        code, payload = send(_serve_headers())
        check("from your phone through serve: relayed with who sent it",
              code == 200 and payload and payload.get("dashboard_owner")
              == {"login": OWNER, "peer": PHONE, "node": "phone1"}, (code, payload))
        conn = [a for a in probe.asked if isinstance(a, tuple) and a[0] == "conn"]
        check("the socket asked about is this request's own",
              conn and conn[-1][2] == ("127.0.0.1", port) and conn[-1][1][0] == "127.0.0.1",
              conn)
        code, payload = send(_serve_headers(peer=MINI))
        check("from the mini's own tailnet address: relayed, vouched for by no one",
              code == 200 and payload is not None and "dashboard_owner" not in payload,
              payload)
        forged = {"key": "D1:1.0", "text": "accept tsk_1",
                  "dashboard_owner": {"login": OWNER, "peer": PHONE, "node": "x"}}
        code, payload = send({"Content-Type": "application/json"}, forged)
        check("a loopback request claiming the owner in its body: the claim is dropped",
              code == 200 and payload is not None and "dashboard_owner" not in payload,
              payload)
        code, payload = send(_serve_headers(peer=MINI), forged)
        check("so is one from the mini through serve", payload is not None
              and "dashboard_owner" not in payload, payload)
        code, payload = send(_serve_headers(**{"Sec-Fetch-Site": "cross-site"}))
        check("a cross-site POST never reaches the bot at all", code == 403 and payload is None,
              (code, payload))
        V.OWNER_PROBE = _FakeProbe(conn={"pid": 4242, "uid": 501, "name": "python3"})
        code, payload = send(_serve_headers())
        check("the owner's headers on a connection a user process opened: not vouched for",
              code == 200 and payload is not None and "dashboard_owner" not in payload,
              payload)

        class Broken(_FakeProbe):
            def connector(self, client, server):
                raise RuntimeError("netstat went away")
        V.OWNER_PROBE = Broken()
        code, payload = send(_serve_headers())
        check("a probe that raises: the message still goes, vouched for by no one",
              code == 200 and payload is not None and "dashboard_owner" not in payload,
              (code, payload))
    finally:
        V.LOOPBACK, V.TOKEN, V.bot_call, V.OWNER_PROBE = saved
        if srv is not None:
            srv.shutdown(); srv.server_close()
        if saved_env is None:
            os.environ.pop("VIZ_TAILSCALE_LOGIN", None)
        else:
            os.environ["VIZ_TAILSCALE_LOGIN"] = saved_env


def test_bot_opens_your_grant_for_a_vouched_dashboard_message():
    import operator_role as O
    print("\nthe bot: a dashboard message the dashboard vouched for starts a turn of "
          "yours; any other does not")
    saved = os.environ.get("VIZ_TAILSCALE_LOGIN")
    os.environ["VIZ_TAILSCALE_LOGIN"] = OWNER
    try:
        ns = lift("user_started", "grant_starter", BOT_USER_ID="UBOT",
                           ALLOWED_USERS={"U_ME"})
        started, starter = ns["user_started"], ns["grant_starter"]
        vouched = {"login": "owner@example.com", "peer": PHONE, "node": "phone1"}
        ev = {"_web": True, "_web_owner": vouched}
        check("a vouched dashboard message starts a turn of yours", started(ev, ""))
        check("its grant is your login, from the dashboard, with the device",
              starter(ev, "") == ("owner@example.com", {
                  "via": "dashboard", "login": "owner@example.com", "peer": PHONE,
                  "node": "phone1"}), starter(ev, ""))
        check("a Slack message's grant says Slack",
              starter({"ts": "1.0"}, "U_ME") == ("U_ME", {"via": "slack"}))
        check("even one carrying the dashboard's field (only a dashboard message is vouched for)",
              starter({"ts": "1.0", "_web_owner": vouched}, "U_ME") == ("U_ME", {"via": "slack"}))
        check("an unvouched dashboard message does not start one",
              not started({"_web": True}, ""))
        check("nor one vouched for another login",
              not started({"_web": True, "_web_owner": dict(vouched, login="x@y.z")}, ""))
        check("nor one whose peer is not a tailnet address",
              not started({"_web": True, "_web_owner": dict(vouched, peer="10.0.0.2")}, ""))
        check("nor a Slack event carrying the dashboard's field",
              not started({"_web_owner": vouched}, "U_STRANGER"))
        os.environ["VIZ_TAILSCALE_LOGIN"] = ""
        check("nor any while VIZ_TAILSCALE_LOGIN is unset", not started(ev, ""))
    finally:
        if saved is None:
            os.environ.pop("VIZ_TAILSCALE_LOGIN", None)
        else:
            os.environ["VIZ_TAILSCALE_LOGIN"] = saved
    src = (BASE / "bot.py").read_text()
    tree = ast.parse(src)
    fn = lambda name: next(f for f in tree.body if isinstance(f, ast.FunctionDef)
                           and f.name == name)
    dw = ast.get_source_segment(src, fn("deliver_web_message"))
    check("the bot reads who sent it only from the dashboard's dashboard_owner field",
          'payload.get("dashboard_owner")' in dw and '"_web_owner": owner' in dw)
    hp = ast.get_source_segment(src, fn("handle_prompt"))
    check("handle_prompt opens the grant with grant_starter's who and provenance",
          "who, via = grant_starter(event, user)" in hp and "via=via" in hp)
    gs = O.Grants()
    t = gs.open("D1:1.0", "5.0", "owner@example.com", "accept tsk_1", "tsk_turn",
                via={"via": "dashboard", "login": "owner@example.com", "node": "phone1",
                     "peer": PHONE})
    g = gs.get(t, "D1:1.0", "tsk_turn")
    check("a dashboard grant records where it came from",
          g["via"] == "dashboard" and g["node"] == "phone1" and g["peer"] == PHONE
          and O.grant_source(g) == f"dashboard message 5.0 (owner@example.com on "
                                   f"phone1, {PHONE})", O.grant_source(g))
    t2 = gs.open("D1:1.0", "6.0", "U_ME", "accept tsk_1", "tsk_turn")
    check("a Slack one says so", gs.get(t2, "D1:1.0", "tsk_turn")["via"] == "slack"
          and O.grant_source(gs.get(t2, "D1:1.0", "tsk_turn")) == "Slack message 6.0")
    check("a page can't smuggle extra fields through provenance",
          O.provenance({"via": "dashboard", "login": "a", "key": "x", "task": "y"}).keys()
          == {"via", "login", "node", "peer"})


# 2026-11-18: /web-message is loopback with no other gate, and the bot took
# `dashboard_owner` from its body -- so any process on the mini (an agent's
# curl) could post one and start a turn on the owner's word. It now keeps one
# only on a connection the visualizer's launchd job opened.


def test_bot_takes_dashboard_owner_only_from_the_visualizer():
    import socket
    import urllib.error
    import urllib.request
    import dashowner as D
    from localserver import LocalServer
    print("\n/web-message: dashboard_owner counts only when the visualizer sent it")
    c, s = ("127.0.0.1", 55072), ("127.0.0.1", 8787)
    ok, _ = D.from_visualizer(c, s, _VizProbe())
    check("the visualizer's own connection is taken", ok)
    pr = _VizProbe(opener=9999)
    ok, why = D.from_visualizer(c, s, pr)
    check("any other process's is not, and says whose it was",
          not ok and "9999" in why and "4242" in why, why)
    check("it asks launchd for the visualizer's job by its label",
          ("job", D.VIZ_LABEL) in pr.asked and D.VIZ_LABEL == "com.silkworm.viz")
    pr = _VizProbe(job=0)
    ok, why = D.from_visualizer(c, s, pr)
    check("no visualizer job running: not taken, said so, and no netstat run for it",
          not ok and "not running" in why and pr.asked == [("job", D.VIZ_LABEL)], (why, pr.asked))
    check("opener unknown: not taken",
          not D.from_visualizer(c, s, _VizProbe(opener=0))[0])
    check("no connection to ask about: not taken",
          not D.from_visualizer(None, None, _VizProbe())[0]
          and not D.from_visualizer(("x",), s, _VizProbe())[0])

    plist = D.launch_agent(D.VIZ_LABEL)
    print_out = ("gui/501/com.silkworm.viz = {\n\tactive count = 1\n"
                 f"\tpath = {plist}\n\tstate = running\n"
                 "\tprogram = /x/python\n\tpid = 96925\n\tendpoints = {\n"
                 "\t\tpid = 1\n\t}\n}\n")
    check("the installed plist is ~/Library/LaunchAgents/com.silkworm.viz.plist",
          plist == str(Path.home() / "Library" / "LaunchAgents" / "com.silkworm.viz.plist"))
    check("launchctl print: the job's own pid line", D.job_pid_of(print_out, plist) == 96925)
    check("not a nested one",
          D.job_pid_of(print_out.replace("\tpid = 96925\n", ""), plist) == 0)
    check("no pid (not running): 0", D.job_pid_of(f"\tpath = {plist}\n\tstate = waiting\n",
                                                  plist) == 0)
    check("a job under the label loaded from another plist (bootstrap of the agent's own) is not it",
          D.job_pid_of(print_out.replace(plist, "/tmp/x.plist"), plist) == 0)
    check("nor one with no plist at all (launchctl submit -l)",
          D.job_pid_of(print_out.replace(f"\tpath = {plist}\n", ""), plist) == 0)
    seen = []
    got = D.Probe(run=lambda a, t: (seen.append(a), (0, print_out, ""))[1]).job_pid(D.VIZ_LABEL)
    check("Probe.job_pid runs launchctl by full path in this user's GUI domain",
          got == 96925 and seen == [["/bin/launchctl", "print",
                                     f"gui/{os.getuid()}/com.silkworm.viz"]], (got, seen))
    check("and answers 0 when launchctl fails",
          D.Probe(run=lambda a, t: (113, "", "no such")).job_pid(D.VIZ_LABEL) == 0)

    # The bot's route: the field dropped unless from_visualizer says so.
    delivered = []
    ns = lift("handle_web_message", dashowner=D,
                       deliver_web_message=lambda p: delivered.append(p) or {"ok": True},
                       WEB_SENDS=None, webthread=None, WEB_PEER_PROBE=_VizProbe(opener=777),
              absent=('WEB_REPEAT_WAIT_S',))
    vouched = {"login": "owner@example.com", "peer": PHONE, "node": "phone1"}
    ns["handle_web_message"]({"key": "D1:1.0", "text": "accept tsk_1",
                              "dashboard_owner": vouched}, c, s)
    check("a dashboard_owner posted by another process is dropped; the message still goes",
          delivered and "dashboard_owner" not in delivered[-1]
          and delivered[-1]["text"] == "accept tsk_1", delivered)
    ns["handle_web_message"]({"key": "D1:1.0", "text": "x", "dashboard_owner": vouched})
    check("as is one with no connection to check", "dashboard_owner" not in delivered[-1])
    ns["WEB_PEER_PROBE"] = _VizProbe()
    ns["handle_web_message"]({"key": "D1:1.0", "text": "x", "dashboard_owner": vouched}, c, s)
    check("one the visualizer sent is kept", delivered[-1].get("dashboard_owner") == vouched)
    asked = ns["WEB_PEER_PROBE"].asked = []
    ns["handle_web_message"]({"key": "D1:1.0", "text": "x"}, c, s)
    check("a message with no such field costs no probe", asked == [] and
          "dashboard_owner" not in delivered[-1])
    src = (BASE / "bot.py").read_text()
    check("the bot's server hands /web-message its connection",
          'server.route("/web-message", handle_web_message, peer=True)' in src)

    # LocalServer: a peer route gets the socket's two ends; others as before.
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    got = {}
    srv = LocalServer(port)
    srv.route("/p", lambda p, cl, sv: got.update(p=p, cl=cl, sv=sv) or {"ok": True}, peer=True)
    srv.route("/q", lambda p: {"q": p.get("n")})
    srv.start()

    def post(path, body, port=port):
        for _ in range(50):
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                             data=json.dumps(body).encode(), method="POST",
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    return json.loads(r.read())
            except (ConnectionRefusedError, urllib.error.URLError):
                time.sleep(0.05)
        return None
    check("a peer route gets (payload, client, server) from the socket",
          post("/p", {"n": 1}) == {"ok": True} and got["p"] == {"n": 1}
          and got["sv"] == ("127.0.0.1", port) and got["cl"][0] == "127.0.0.1"
          and isinstance(got["cl"][1], int), got)
    check("a plain route still gets the payload alone", post("/q", {"n": 2}) == {"q": 2})

    # Live: the real netstat names this very process as a loopback client,
    # so the bot's check reads the connection it is answering.
    real = D.Probe()
    seen_live = {}
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        lport = sk.getsockname()[1]
    lsrv = LocalServer(lport)

    def live(p, cl, sv):
        mine = types.SimpleNamespace(job_pid=lambda label: os.getpid(),
                                     connector=real.connector)
        other = types.SimpleNamespace(job_pid=lambda label: os.getpid() + 1,
                                      connector=real.connector)
        seen_live["mine"] = D.from_visualizer(cl, sv, mine)
        seen_live["other"] = D.from_visualizer(cl, sv, other)
        return {"ok": True}
    lsrv.route("/live", live, peer=True)
    lsrv.start()
    post("/live", {}, port=lport)
    check("live: netstat names this process as the opener of its own loopback request",
          seen_live.get("mine") == (True, ""), seen_live)
    check("live: and refuses when the job is someone else",
          seen_live.get("other", (True,))[0] is False, seen_live)


def _dash_say(bot, text, turn=None, ts="1763400000.000100"):
    """Open a grant as handle_prompt does for a dashboard message it vouched for."""
    return bot.ns["OPERATOR_GRANTS"].open(
        HOME, ts, "owner@example.com", text, turn or bot.turn,
        via={"via": "dashboard", "login": "owner@example.com", "node": "phone1",
             "peer": PHONE})


def test_dashboard_grant_accepts_and_records_where_it_came_from():
    import tasks as T
    print("\nyour word from the dashboard: accept goes through, and says it was the dashboard")
    bot = _said_bot()
    st = bot.store
    a = _proposal(st, "Add an appearance preference to Settings")
    g = _dash_say(bot, f"accept {a}")
    r = _as_sweep(bot, "accept", a, "--notes", "user said accept it")
    check("a sweep's accept is still refused", r.returncode == 1
          and st.get(a)["state"] == T.PROPOSED, r.stdout + r.stderr)
    r = _as_user(bot, g, "accept", a, "--notes", "user: accept it")
    rec = st.get(a) or {}
    res = rec.get("result") or {}
    ev = [e for e in rec.get("events") or [] if e.get("kind") == "decided"]
    check("your dashboard message lets the accept through",
          r.returncode == 0 and rec.get("state") != T.PROPOSED, r.stdout + r.stderr)
    check("the reply says whose word, from where",
          "dashboard message 1763400000.000100 (owner@example.com on phone1" in r.stdout,
          r.stdout)
    check("the task records decided_by, its message and the dashboard provenance",
          res.get("decided_by") == "user via operator"
          and res.get("decided_ts") == "1763400000.000100"
          and "dashboard message" in (res.get("decided_via") or "")
          and ev and "dashboard message" in ev[-1]["detail"]
          and "phone1" in ev[-1].get("via", ""), rec)
    check("compaction keeps where it came from", "decided_via" in T.RESULT_KEEPS)
    import costs
    st.update(a, result={"text": "done", **costs.add_run(st.get(a), 0.5)})
    check("and a run rewriting result keeps it",
          "dashboard message" in ((st.get(a).get("result") or {}).get("decided_via") or ""),
          st.get(a).get("result"))
    bot.ns["OPERATOR_GRANTS"].close(g)
    b = _proposal(st, "Cache the release plan per checkout")
    r = _as_user(bot, g, "accept", b, "--notes", "user: accept it")
    check("once the turn ends, its grant lifts nothing",
          r.returncode == 1 and st.get(b)["state"] == T.PROPOSED, r.stdout + r.stderr)


def test_dashboard_grant_files_on_request():
    import tasks as T
    print("\nyour word from the dashboard: a requested filing passes the cap, recorded as such")
    w = _requested_world()
    HOME_ = "D5:5.0"
    g = w.grants.open(HOME_, "1763400000.000200", "owner@example.com",
                      "file the universe rebuild", "tsk_turn",
                      via={"via": "dashboard", "login": "owner@example.com",
                           "node": "phone1", "peer": PHONE})
    out = w.file({"key": HOME_, "goal": "Rebuild the universe point-in-time", "project": "alpha",
                  "caller_role": "operator", "caller_task": "tsk_turn", "caller_grant": g,
                  "requested_by_user": True, "notes": "user: 'file the universe rebuild'"})
    rec = w.ts.get(out.get("id") or "") or {}
    rq = rec.get("requested_by_user") or {}
    check("filed straight to the queue past the full board",
          out.get("ok") and out.get("state") == T.QUEUED, out)
    check("the reply and the record name the dashboard message",
          "dashboard message 1763400000.000200" in (out.get("decided_via") or "")
          and rq.get("via") == "dashboard" and "phone1" in rq.get("source", "")
          and any("dashboard message" in e.get("detail", "") for e in rec.get("events") or []),
          (out, rec))
    out = w.file({"key": HOME_, "goal": "Another", "project": "alpha",
                  "caller_role": "operator", "caller_task": "tsk_sweep",
                  "requested_by_user": True, "notes": "user: 'file the universe rebuild'"})
    check("a sweep still can't claim it", not out.get("ok"), out)


def test_dashboard_grant_answers_an_ops_plan():
    import ops as OPS
    print("\nyour word from the dashboard: 'yes' runs the planned op; unplanned trader "
          "config is still refused")
    w = _ops_world()
    with _fake_path(w):
        _plan_record(w, "trader-env-set", "MOMENTUM_UNIVERSE", "pit")
        ts = f"{time.time() + 1:.6f}"
        g = w.grants.open("D1:1.0", ts, "owner@example.com", "yes", "tsk_turn_mine",
                          plan=w.state.answer_plan(ts, "yes"),
                          via={"via": "dashboard", "login": "owner@example.com",
                               "node": "phone1", "peer": PHONE})
        r = _ops_as_user(w, g, "trader-env-set", "MOMENTUM_UNIVERSE", "static",
                         "--notes", "user: yes")
        check("an op not in the plan, and not named, is still refused",
              r.returncode == 1 and _env_value(w, "MOMENTUM_UNIVERSE") is None, r.stderr)
        r = _plan_run(w, g, 1, "trader-env-set", "MOMENTUM_UNIVERSE", "pit")
        check("'yes' from the dashboard runs the entry",
              r.returncode == 0 and _env_value(w, "MOMENTUM_UNIVERSE") == "pit",
              r.stdout + r.stderr)
        runs = [x for x in OPS.read_log(w.log, 100) if x.get("plan_entry") and not x.get("refused")]
        check("the ops log records it was the dashboard, and which device",
              runs and "dashboard message" in runs[-1].get("decided_via", "")
              and "phone1" in runs[-1]["decided_via"], runs)
        r = _ops_cli(w, "log")
        check("and `silkworm ops log` shows it", "dashboard message" in r.stdout, r.stdout)
        r = _plan_run(w, "", 1, "trader-env-set", "MOMENTUM_UNIVERSE", "pit")
        check("a sweep (no grant) still can't", r.returncode == 1, r.stderr)


def test_digest_says_which_message_decided():
    import digest as D
    import tasks as T
    print("\nthe digest: what was done on your word, and whether by Slack or the dashboard")
    tmp = Path(tempfile.mkdtemp())
    st = T.TaskStore(tmp / "t.json")
    a = st.create("Add an appearance preference", project="alpha")["id"]
    b = st.create("Retry sync on 429", project="alpha")["id"]
    st.record_decision(a, "accept decided by owner@example.com (dashboard message 9.1 "
                       "(owner@example.com on phone1, 100.64.0.2)) via operator: yes",
                       "user via operator", "9.1",
                       via="dashboard message 9.1 (owner@example.com on phone1, 100.64.0.2)")
    st.record_decision(b, "dismiss decided by U_ME (Slack message 9.2) via operator: no",
                       "user via operator", "9.2", via="Slack message 9.2")
    text = D.render(st.all().values(), time.time())
    check("each is listed under what was done on your word, with where it came from",
          "done on your word" in text and "accept, dashboard message 9.1" in text
          and "phone1" in text and "dismiss, Slack message 9.2" in text, text)


# --- the chat's tool calls and files, served --------------------------------------
# The chat's one line per turn needs each call's id (to pair its result), a
# short argument and whether it failed; the thread's files are drawn inline
# -- thumbnails, a viewer -- which only images may be, never an .html or .svg
# that would run script as the dashboard.
def test_viz_tool_calls_and_inline_files():
    import random
    import struct
    import threading
    import urllib.error
    import urllib.request
    import zlib
    from http.server import ThreadingHTTPServer
    import visualizer as V
    print("\nthe transcript carries call ids and arguments; only images are served inline")
    blocks = V._block_list([
        {"type": "tool_use", "id": "toolu_1", "name": "Read",
         "input": {"file_path": "/Users/x/workspace/Silkworm/visualizer.py", "limit": 5}},
        {"type": "tool_use", "id": "toolu_2", "name": "Bash",
         "input": {"command": "make test\necho done", "description": "run them"}},
        {"type": "tool_result", "tool_use_id": "toolu_2", "is_error": True,
         "content": [{"type": "text", "text": "boom"}]},
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"},
    ])
    check("a call carries its id and a short argument (a path by its last two parts)",
          blocks[0]["id"] == "toolu_1" and blocks[0]["arg"] == "Silkworm/visualizer.py", str(blocks[0]))
    check("a command is its first line", blocks[1]["arg"] == "make test", str(blocks[1]))
    check("a command is what ran, not the cd before it",
          V.tool_arg("Bash", {"command": 'cd "/a b" && cd c; make test'}) == "make test"
          and V.tool_arg("Bash", {"command": "cd /x"}) == "cd /x"
          and V.tool_arg("Bash", {"command": "cd /x/y\ngit init -q && ls"}) == "git init -q && ls")
    check("a result names its call and says whether it failed",
          blocks[2]["for"] == "toolu_2" and blocks[2]["error"] is True and blocks[2]["text"] == "boom"
          and blocks[3]["for"] == "toolu_1" and blocks[3]["error"] is False, str(blocks[2:]))
    check("an unknown tool shows its first string field; none, nothing",
          V.tool_arg("Zap", {"n": 2, "q": "x" * 100}) == "x" * 79 + "…" and V.tool_arg("Zap", {}) == "")

    # What Claude Code writes as the user (isMeta: reminders, a skill's body)
    # is neither a bubble nor a new turn; its results still count.
    tdir = Path(tempfile.mkdtemp())
    tr = tdir / "s.jsonl"
    tr.write_text("\n".join(json.dumps(e) for e in [
        {"type": "user", "timestamp": "t0", "message": {"content": "hi"}},
        {"type": "user", "isMeta": True, "timestamp": "t1",
         "message": {"content": "<system-reminder>skill body</system-reminder>"}},
        {"type": "user", "isMeta": True, "timestamp": "t2",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "a", "content": "r"},
                                 {"type": "text", "text": "noise"}]}},
    ]) + "\n")
    saved_find = V.find_transcript
    V.find_transcript = lambda sid: tr
    try:
        got = V.load_transcript("s")["messages"]
    finally:
        V.find_transcript = saved_find
    check("Claude Code's own user-side text is dropped; its tool results kept",
          [[b["type"] for b in m["blocks"]] for m in got] == [["text"], ["tool_result"]], str(got))

    def png(w, h, noise):
        rows = b"".join(b"\0" + (bytes(random.getrandbits(8) for _ in range(w * 3)) if noise
                                 else b"\x80" * (w * 3)) for _ in range(h))
        chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))

    tmp = Path(tempfile.mkdtemp())
    small, big = tmp / "small.png", tmp / "big.png"
    small.write_bytes(png(8, 8, False))
    big.write_bytes(png(400, 300, True))
    page, svg = tmp / "page.html", tmp / "pic.svg"
    page.write_text("<script>alert(1)</script>")
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    sessions = tmp / "sessions.json"
    files = [{"name": f.name, "path": str(f), "direction": "out", "ts": 1.0} for f in (small, big, page, svg)]
    sessions.write_text(json.dumps({"D1:1.0": {"files": files}}))
    saved = (V.SESSIONS_FILE, V.LOOPBACK, V.THUMB_DIR)
    V.SESSIONS_FILE, V.LOOPBACK, V.THUMB_DIR = sessions, True, tmp / "thumbs"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), V.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    from urllib.parse import quote

    def get(path):
        try:
            with urllib.request.urlopen(base + path, timeout=30) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, b""
    try:
        listed = {f["name"]: f for f in V.artifacts("D1:1.0")}
        check("the thread's files say which are images",
              listed["big.png"]["image"] is True and listed["page.html"]["image"] is False
              and listed["pic.svg"]["image"] is False, str(listed))
        st, h, body = get("/download?inline=1&path=" + quote(str(big)))
        check("an image asked for inline is served as an image, inline",
              st == 200 and h["Content-Type"] == "image/png" and h["Content-Disposition"] == "inline"
              and h["X-Content-Type-Options"] == "nosniff" and body == big.read_bytes(), f"{st} {dict(h)}")
        for f in (page, svg):
            st, h, _ = get("/download?inline=1&path=" + quote(str(f)))
            check(f"{f.suffix} is never inline: always a download, never sniffed",
                  st == 200 and h["Content-Type"] == "application/octet-stream"
                  and h["Content-Disposition"].startswith("attachment")
                  and h["X-Content-Type-Options"] == "nosniff", dict(h))
            st, _, _ = get("/thumb?path=" + quote(str(f)))
            check(f"{f.suffix} has no thumbnail", st == 404, str(st))
        st, _, _ = get("/thumb?path=" + quote(str(tmp / "elsewhere.png")))
        check("a path the bot never recorded has no thumbnail either", st == 404, str(st))
        st, h, body = get("/thumb?path=" + quote(str(small)))
        check("a small image is its own thumbnail", st == 200 and body == small.read_bytes()
              and h["Content-Type"] == "image/png", str(st))
        st, h, body = get("/thumb?path=" + quote(str(big)))
        if Path("/usr/bin/sips").exists():
            check("a large image's thumbnail is a small JPEG, made once and kept",
                  st == 200 and h["Content-Type"] == "image/jpeg" and body[:2] == b"\xff\xd8"
                  and len(body) < big.stat().st_size // 4
                  and len(list((tmp / "thumbs").glob("*.jpg"))) == 1, f"{st} {len(body)}")
        else:
            check("without sips a large image is served as itself", st == 200 and body == big.read_bytes())
    finally:
        srv.shutdown()
        V.SESSIONS_FILE, V.LOOPBACK, V.THUMB_DIR = saved


#: Real goals filed through scoping.validate in the 60 days to 2026-11-18,
#: from tasks.json: the two shortest the dashboard ever took, the opening of
#: the commonest kinds (the nightly pass, a conversation's, a review's
#: follow-up, the operator's), and ones that start with a lookup verb or name
#: task ids without being lookups. Every one must still be filed.
REAL_GOALS = (
    "Release latest changes to test flight",
    "Test flight build doesn't have latest updates to the code base. We should release these.",
    "Look over the Trader project and propose work worth doing.\n\nRead what is actually there before suggesting anything.",
    "Make SupabaseSyncService.push() testable, and stop it swallowing every error it hits",
    "Keep an ops plan open until the user actually answers it",
    "From review: Land silkworm/tsk_76e96e9d9c (stops the daemon writing halt markers) once main has it",
    "Decide whether to drop silkworm/tsk_343f6af22a and silkworm/tsk_7b290f1a2c after tsk_5ea770d699 lands",
    "cadence: give the widget a fallback card when there is nothing due today",
    "Show the streak count on the widget",
    "Open the invite page in Safari when the app is not installed",
    "List every Supabase function whose deployed source differs from main",
    # Half ids is still an instruction; only more than half is refused.
    "Land tsk_aaaa1111, tsk_bbbb2222 and tsk_cccc3333 tonight",
)


def test_a_mistyped_lookup_is_not_filed():
    import scoping as S
    print("\nfiling: a lookup typed at `silkworm task` is refused, with where it belonged")
    w = _requested_world()
    before = dict(w.ts.all())
    # The exact filing of 2026-11-18 02:05: Cadence's nightly ideator ran
    # `silkworm task show tsk_ad4681bd92` and got a proposal back.
    out = w.file({"key": "D7:7.0", "goal": "show tsk_ad4681bd92", "propose": True,
                  "project": "alpha", "caller_role": "ideator", "caller_task": "tsk_night"})
    check("the 2026-11-18 input is refused", not out["ok"], out)
    check("as a lookup, pointing at the command that reads a task",
          "reads as a lookup" in out.get("error", "")
          and "silkworm tasks show <id>" in out.get("error", "")
          and "silkworm tasks [state]" in out.get("error", "")
          and "silkworm status --json" in out.get("error", ""), out)
    check("and files nothing", w.ts.all() == before)

    # The CLI says the bot's refusal and exits 1: the same check, not its own.
    cli = _load_cli()
    sent = []
    cli.bot_call = lambda path, payload, timeout=3: (sent.append((path, payload)) or
                                                     w.file(dict(payload, key="D7:7.0")))
    env = {"SILKWORM_THREAD": "D7:7.0", "SILKWORM_ROLE": "ideator"}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                cli.do_file_task(["show", "tsk_ad4681bd92"])
                code = 0
            except SystemExit as e:
                code = e.code
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    check("from the CLI it goes to the shared route and is refused there",
          sent and sent[0][0] == "/file-task" and code == 1
          and "silkworm tasks show <id>" in buf.getvalue(), (code, buf.getvalue()))
    check("still nothing filed", w.ts.all() == before)

    # Every way of filing asks the same check: the dashboard's create and an
    # edit go through scoping.validate as /file-task does.
    ns, ts, ps, found = _edit_routes()
    r = ns["handle_tasks"]({"action": "create", "goal": "view tsk_ad4681bd92", "project": "alpha"})
    check("the dashboard's create refuses it too", not r["ok"]
          and "reads as a lookup" in r["error"] and ts.all() == {}, r)


def test_goal_shape_rules_each_refuse_their_own_case():
    import scoping as S
    print("\nfiling: each goal-shape rule refuses its own case, and real goals pass")
    for goal in REAL_GOALS:
        check(f"a real goal is filed: {goal[:50]!r}", S.validate(goal) == "", S.validate(goal))
    # Each case is caught by its own rule and said in that rule's words, so
    # disabling any one rule fails exactly its own checks.
    for goal in ("show tsk_ad4681bd92", "describe tsk_aaaa1111 tsk_bbbb2222",
                 "STATUS: tsk_ad4681bd92", "list", "help", "cat silkworm/tsk_ad4681bd92",
                 "open dashboard"):
        check(f"lookup {goal!r} refused as a lookup",
              "reads as a lookup" in S.validate(goal), S.validate(goal))
    for goal in ("Look at tsk_aaaa1111 tsk_bbbb2222 tsk_cccc3333 tsk_dddd4444",
                 "tsk_aaaa1111, tsk_bbbb2222, silkworm/tsk_cccc3333 and these"):
        check(f"{goal[:40]!r} refused as mostly ids",
              "mostly task ids" in S.validate(goal), S.validate(goal))
    check("a goal under the character floor is refused (enough words)",
          f"at least {S.MIN_GOAL_CHARS} characters" in S.validate("Fix a b c d e"),
          S.validate("Fix a b c d e"))
    check("a goal under the word floor is refused (enough characters)",
          f"at least {S.MIN_GOAL_CHARS} characters" in S.validate("Refactor everything-in-the-importer now"),
          S.validate("Refactor everything-in-the-importer now"))
    check("the length refusal also says where a lookup belongs",
          "silkworm tasks show <id>" in S.validate("Fix a b c d e"))
    exact = "word " * (S.MIN_GOAL_WORDS - 1) + "x" * (S.MIN_GOAL_CHARS - 5 * (S.MIN_GOAL_WORDS - 1))
    check("exactly the floors is filed", len(exact) == S.MIN_GOAL_CHARS
          and len(exact.split()) == S.MIN_GOAL_WORDS and S.validate(exact) == "", exact)
    check("the floors sit below the shortest real goal",
          S.MIN_GOAL_CHARS <= len(REAL_GOALS[0]) and S.MIN_GOAL_WORDS <= len(REAL_GOALS[0].split()))


def test_tasks_show_prints_one_task_and_changes_nothing():
    import roles as R
    print("\ntasks show: one task's title, state, goal and result, read-only")
    ns, ts, ps, found = _edit_routes()
    rec = ts.create("Build the importer for the new feed format, with tests",
                    title="Importer", project="alpha", role="implementor", driver="queue")
    ts.update(rec["id"], result={"text": "Wrote importer.py and its tests; 12 pass.",
                                 "review": {"ok": True, "summary": "does what it says",
                                            "findings": []},
                                 "landed": "a" * 40})
    path = Path(ts._path)
    raw = path.read_bytes() if path and path.exists() else None
    snapshot = json.dumps(ts.all(), sort_keys=True)
    cli = _load_cli()
    sent = []
    cli.bot_call = lambda p, payload, timeout=3: (sent.append((p, dict(payload))) or
                                                  ns["handle_tasks"](dict(payload)))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.do_tasks_show([rec["id"]])
    out = buf.getvalue()
    check("it exits 0", code == 0, out)
    check("it prints the title, state, goal and the result summary",
          "Importer" in out and "state: queued" in out
          and "Build the importer for the new feed format, with tests" in out
          and "review passed: does what it says" in out and "landed: " + "a" * 40 in out
          and "12 pass" in out, out)
    check("through the read action the dashboard opens a card with",
          [p for p, _ in sent] == ["/tasks"] and sent[0][1].get("action") == "task", sent)
    check("and nothing about the task changed",
          json.dumps(ts.all(), sort_keys=True) == snapshot
          and (raw is None or path.read_bytes() == raw))
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        code = cli.do_tasks_show(["tsk_nope000000"])
    check("an unknown id is refused, exit 1", code == 1 and "unknown task" in buf.getvalue())
    with contextlib.redirect_stderr(io.StringIO()):
        check("no id is a usage error, exit 2", cli.do_tasks_show([]) == 2)
    r = subprocess.run([sys.executable, str(BASE / "bin" / "silkworm"), "tasks", "show"],
                       capture_output=True, text=True, timeout=30)
    check("the CLI routes `tasks show` to it, not to a list of state 'show'",
          r.returncode == 2 and "usage: silkworm tasks show" in r.stderr, r.stdout + r.stderr)
    for role in ("ideator", "reviewer"):
        tools = R.permission_args(role, [], bin="/x/bin/silkworm")[1]
        check(f"a restricted {role} may run it", "Bash(/x/bin/silkworm tasks show:*)" in tools,
              tools)
    check("the ideator is told it is the way to read a task",
          "/x/bin/silkworm tasks show <id>" in R.system_prompt("ideator", "/x/bin/silkworm"))

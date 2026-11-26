"""The Slack board, thread links and the daily digest.

Run by tests/run.py with every other tests/test_*.py; see tests/harness.py."""

from harness import *  # noqa: F401,F403


# --- the Slack board: the task board, away from the local network ------------
# The dashboard binds loopback, so decisions that waited on it waited until you
# were home. The Slack board is the same board in Slack (it began as the app's
# Home tab, now gone). What must not happen is
# for the two to disagree about what a click may do, for the board to leak to
# someone off the allowlist, or for an oversized backlog to make Slack refuse
# the whole view and show nothing.

class _BoardSlack:
    """Just enough Slack for a channel Board: a private channel holding only
    the bot and the allowlist. Each redraw lands in `published` as a view."""
    def __init__(self, members=("U_ME",)):
        self.members = list(members) + ["B_BOT"]
        self.published, self.opened, self.ephemeral, self.n = [], [], [], 0
    def conversations_info(self, channel, **kw):
        return {"channel": {"id": channel, "is_private": True}}
    def conversations_members(self, channel, **kw):
        return {"members": self.members, "response_metadata": {}}
    def chat_postMessage(self, channel, text, blocks):
        self.n += 1
        self.published.append((channel, {"blocks": blocks}))
        return {"ts": f"1.{self.n}"}
    def chat_update(self, channel, ts, text, blocks):
        self.published.append((channel, {"blocks": blocks}))
    def chat_postEphemeral(self, channel, user, text):
        self.ephemeral.append((user, text))
    def views_open(self, trigger_id, view):
        self.opened.append(view)
        return {"view": {"id": "V1"}}
    def views_update(self, view_id, view):
        self.opened.append(view)


def test_home_tab():
    import home
    import tasks as T
    print("\nthe Slack board: rendering and clicks")

    def texts(view):
        return json.dumps(view, ensure_ascii=False)

    def buttons(view, tid):
        """The actions in a task's menu, Open thread aside."""
        for b in view["blocks"]:
            if b.get("block_id") == f"t:{tid}":
                opts = (b.get("accessory") or {}).get("options") or []
                return [o["value"].split("|")[0] for o in opts
                        if not o["value"].startswith("thread|")]
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
        mine = {a for _, a in home.BUTTONS.get(st, [])
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
    # Every action in the menu opens a window first (on_menu); the detail
    # that decides it is in that window, not on the one-line row.
    check("approve asks before landing", "approve" in home.DIRECT
          and home.confirm_modal(board[1], "approve")["callback_id"] == home.CONFIRM_CALLBACK)
    aw_detail = texts(home.confirm_modal(board[1], "approve"))
    check("the review is shown before you approve", "looks close" in aw_detail
          and all(f in aw_detail for f in ("one", "two", "three", "four")))
    check("why it failed is shown", "code 143" in texts(home.confirm_modal(board[3], "retry")))
    s = texts(view)
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
    ns = lift("handle_tasks", "approve_task", task_store=store, tasks=T,
                       holding=__import__("holding"),
                       stop_task=lambda tid: False, start_landing=lambda tid: None,
              absent=TASK_ROUTES + STEP_ROUTES)
    prop = store.create("a proposal worth accepting", state=T.PROPOSED)["id"]
    need = store.create("a task that asked a question", state=T.QUEUED)["id"]
    store.transition(need, T.RUNNING); store.transition(need, T.NEEDS_INPUT)
    calls = []
    def call(p):
        calls.append(p)
        return ns["handle_tasks"](p)

    h = _board_for(store, call, {"U_ME"})
    c = _BoardSlack()
    acked = []
    ack = lambda *a, **k: acked.append(k)
    def pick(user, action, tid, seen=""):
        h.on_menu(ack, {"user": {"id": user}, "trigger_id": "trig",
                        "actions": [{"action_id": "home_menu",
                                     "selected_option": {"value": f"{action}|{tid}|{seen}"}}]}, c)
    def confirm(user, action, tid, seen=""):
        h.on_confirm(ack, {"user": {"id": user}}, c, {"private_metadata": json.dumps(
            {"action": action, "id": tid, "seen": seen})})
    def click(user, action, tid, seen=""):
        """Pick it from the menu, then confirm the window that opens."""
        n = len(c.opened)
        pick(user, action, tid, seen)
        if len(c.opened) > n:
            h.on_confirm(ack, {"user": {"id": user}}, c, c.opened[-1])

    click("U_STRANGER", "accept", prop)
    check("someone off the allowlist cannot act", not calls
          and store.get(prop)["state"] == T.PROPOSED)
    check("and is shown nothing of the board, only told why, privately",
          not c.published and c.ephemeral and c.ephemeral[-1][0] == "U_STRANGER"
          and "a proposal worth" not in c.ephemeral[-1][1])

    click("U_ME", "accept", prop)
    check("Accept moves the real task to queued", store.get(prop)["state"] == T.QUEUED)
    check("attributed to the Slack user", "slack:U_ME" in json.dumps(store.get(prop)["events"]))
    check("the result is shown at the top of the board",
          "Accepted" in texts(c.published[-1][1]))

    confirm("U_ME", "accept", prop)                              # already queued
    confirm("U_ME", "dismiss", "tsk_nope")
    check("a refused click says why rather than failing silently",
          "Couldn't dismiss" in texts(c.published[-1][1]))

    pick("U_ME", "answer", need)
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
    shown = home.render([dict(r, id=k) for k, r in store.all().items()], now=time.time())
    retry = next(o["value"] for b in shown["blocks"] if b.get("block_id") == f"t:{fl}"
                 for o in b["accessory"]["options"] if o["value"].startswith("retry|"))
    store.transition(fl, T.QUEUED); store.transition(fl, T.RUNNING)   # retried elsewhere
    before = len(calls)
    _, _, rest = retry.partition("|")
    click("U_ME", "retry", *rest.split("|", 1))
    check("a stale button does not act on a task that has moved on",
          len(calls) == before and store.get(fl)["state"] == T.RUNNING)
    check("and says so", "moved on" in texts(c.published[-1][1]))
    aw = store.create("work to approve", state=T.QUEUED)["id"]
    store.transition(aw, T.RUNNING); store.transition(aw, T.AWAITING_APPROVAL)
    shown = home.render([dict(r, id=k) for k, r in store.all().items()], now=time.time())
    rework = next(o["value"] for b in shown["blocks"] if b.get("block_id") == f"t:{aw}"
                  for o in b["accessory"]["options"] if o["value"].startswith("rework|"))
    _, _, rest = rework.partition("|")
    pick("U_ME", "rework", *rest.split("|", 1))
    form = c.opened[-1]
    store.transition(aw, T.DONE)                                      # approved elsewhere
    before = len(calls)
    h.on_submit(ack, {"user": {"id": "U_ME"}}, c,
                {"private_metadata": form["private_metadata"],
                 "state": {"values": {"notes": {"notes": {"value": "redo it"}}}}})
    check("a send-back form opened before it was approved does not reopen it",
          len(calls) == before and store.get(aw)["state"] == T.DONE)

    # The Home tab is gone, not merely unregistered: nothing can draw there,
    # and nothing but the channel Board can be wired to Slack.
    src = (BASE / "home.py").read_text()
    check("nothing publishes a Home view or answers app_home_opened",
          "views_publish(" not in src and "(\"app_home_opened\")" not in src
          and not hasattr(home.Home, "on_opened") and not hasattr(home.Home, "view_for"))
    try:
        home.Home(store=store, call=call, allowed_users={"U_ME"}).publish(c, "U_ME")
        drew = True
    except NotImplementedError:
        drew = False
    check("a bare Home draws nowhere", not drew)
    # Nor is the full layout kept around: the board is always the one-line
    # form, whose every action goes through the menu (on_menu), so the button
    # rows and the handlers their clicks went to are gone with it.
    check("no per-task button rows, nor handlers for their clicks",
          not hasattr(home, "_task_blocks") and not hasattr(home.Home, "on_direct")
          and not hasattr(home.Home, "on_modal") and not hasattr(home.Home, "on_link")
          and "compact" not in home.render.__code__.co_varnames)
    check("and render draws a message's blocks, not a Home view",
          "type" not in home.render([], now=now))
    try:
        home.register(types.SimpleNamespace(), home.Home(store=store, call=call,
                                                         allowed_users={"U_ME"}))
        wired = True
    except TypeError:
        wired = False
    check("and register() refuses anything but the Board", not wired)


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
    ns = lift("handle_tasks", "approve_task", task_store=store, tasks=T,
                       stop_task=lambda tid: False, start_landing=lambda tid: None,
              absent=TASK_ROUTES + STEP_ROUTES)
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
    def accept(user, seen):
        """Accept picked from the task's menu, then its confirmation submitted."""
        b5.on_menu(lambda *a, **k: None, {"user": {"id": user}, "trigger_id": "t",
                   "actions": [{"action_id": "home_menu", "selected_option":
                                {"value": f"accept|{tid}|{seen}"}}]}, s4)
        b5.on_confirm(lambda *a, **k: None, {"user": {"id": user}}, s4,
                      {"private_metadata": json.dumps({"action": "accept", "id": tid,
                                                       "seen": seen})})
    accept("U_STEPH", "proposed")
    check("a click from off the allowlist does nothing but say so, privately",
          store.get(tid)["state"] == T.PROPOSED
          and s4.calls[-1][0] == "ephemeral" and s4.calls[-1][2] == "U_STEPH")
    # The result goes into the board, not under it. An ephemeral reply is
    # invisible to the channel's history but not to the person who clicked:
    # each one pushed the board up their screen.
    n = len(s4.calls)
    accept("U_ME", "proposed")
    after = [c for c in s4.calls[n:] if c[0] != "modal"]
    check("Accept works from the board message", store.get(tid)["state"] == T.QUEUED)
    check("nothing is posted under the board", not [c for c in after if c[0] in ("ephemeral", "post")],
          str([c[0] for c in after]))
    drawn = json.dumps(after[-1][-1], ensure_ascii=False) if after and after[-1][0] == "update" else ""
    check("the result is in the board itself", "Accepted" in drawn)
    check("at the bottom, where Slack opens", "Accepted" in json.dumps(after[-1][-1][-4:], ensure_ascii=False)
          if after else False)
    n = len(s4.calls)
    accept("U_ME", "proposed")
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
    v = home.render(board, now=now, base_url="https://x.slack.com",
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
                       max_blocks=50, max_attention=12)
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
    ns = lift("handle_tasks", "approve_task", task_store=store, tasks=T,
                       stop_task=lambda tid: False, start_landing=lambda tid, **kw: None,
                       holding=__import__("holding"), tell_thread=lambda *a: None,
              absent=TASK_ROUTES + STEP_ROUTES)
    aw = store.create("work to approve", state=T.QUEUED, role="implementor")["id"]
    store.transition(aw, T.RUNNING); store.transition(aw, T.AWAITING_APPROVAL)
    store.update(aw, result={"review": {"summary": "ok-ish",
                                        "findings": [f"issue number {i}" for i in range(7)]}})
    client = _BoardSlack()
    opened = client.opened
    h = _board_for(store, ns["handle_tasks"], {"U_ME"})
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
    ns = lift("send_digest", digest=digest_mod, task_store=st,
                       app=types.SimpleNamespace(client=client), BOARD=board,
                       home_channel=lambda: home["ch"], datetime=datetime,
                       digest_inputs=lambda recs: ([], {}),
              absent=('project_store', 'unregistered_repos'))
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
    ns = lift("record_landing", task_store=st)
    before = time.time()
    ns["record_landing"](t, {"eligible": True, "landed": False, "stage": "rebase",
                             "detail": "conflict"})
    at = (st.get(t["id"])["result"]["landing"] or {}).get("at") or 0
    check("record_landing: the outcome carries when it happened", at >= before, str(at))
    check("and `at` survives compaction (it lives inside result.landing)",
          "landing" in T.RESULT_KEEPS)

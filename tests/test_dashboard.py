"""The dashboard: classifiers, boards, the polled page and the action CLI.

Run by tests/run.py with every other tests/test_*.py; see tests/harness.py."""

from harness import *  # noqa: F401,F403


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


# --- dashboard exposure -------------------------------------------------------

def test_viz_bind_requires_token():
    src = (BASE / "visualizer.py").read_text()
    print("\ndashboard refuses to be exposed without auth")
    check("non-loopback bind without a token is refused",
          "if not LOOPBACK and not TOKEN:" in src and "raise SystemExit" in src)
    check("token comparison is constant-time", "secrets.compare_digest" in src)
    check("both GET and POST are gated", src.count("if not self._authed(url)") >= 2)


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
    check("and read once rather than per request", "FAVICON = (CODE_DIR" in viz)
    check("a missing file does not break the dashboard", "FAVICON = b\"\"" in viz)


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
// A thread filed under an archived project is put away with the hidden ones.
const SH = [{key: "e", kind: "thread", shelved: true}, {key: "f", kind: "thread"}];
out.shelvedOff = SH.filter(matchesKind).map(s => s.key);
_bar.innerHTML = ""; renderKindFilter(SH); out.shelvedBar = _bar.innerHTML;
toggleHidden();
out.shelvedShown = SH.filter(matchesKind).map(s => s.key);
toggleHidden();
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
    check("a thread under an archived project is off the list by default",
          out["shelvedOff"] == ["f"], str(out["shelvedOff"]))
    check("counted under hidden, and shown with them",
          "hidden <b>1</b>" in out["shelvedBar"] and out["shelvedShown"] == ["e", "f"],
          out["shelvedBar"])


def test_archived_projects_leave_the_slack_board_and_thread_list():
    import home, tasks as T, projects as P
    sys.argv = ["x"]
    import visualizer as V
    print("\narchived projects: off the Slack board, and their threads put away")
    ts = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    old = ts.create("Proposal under an old project", project="old", state="proposed",
                    role="implementor")
    rev = ts.create("Review: old", state="queued", role="reviewer", parent=old["id"])
    ts.transition(rev["id"], "running"); ts.transition(rev["id"], "failed")
    queued = ts.create("Queued under the old project", project="old", state="queued",
                       role="implementor", driver="queue")
    live = ts.create("Proposal under a live one", project="live", state="proposed",
                     role="implementor")
    shelved = {"old"}
    b = _board_for(ts, lambda p: {"ok": True}, shelved=lambda: shelved)
    ids = {r["id"] for r in b.listed()}
    check("the Slack board lists the live project's work, not the archived one's",
          live["id"] in ids and old["id"] not in ids, sorted(ids))
    check("queued work stays: archiving does not stop the queue", queued["id"] in ids)
    check("a reviewer is counted under its parent's project",
          rev["id"] not in ids and P.project_of(ts.get(rev["id"]), ts.all()) == "old")
    text = json.dumps(b.blocks())
    check("and so does what it draws", "live one" in text and "old project" not in text)
    shelved.clear()
    check("unarchived, it is back", old["id"] in {r["id"] for r in b.listed()})
    check("a board with no archive source shows everything",
          old["id"] in {r["id"] for r in _board_for(ts, lambda p: {"ok": True}).listed()})

    d = Path(tempfile.mkdtemp())
    ps = P.ProjectStore(d / "projects.json")
    ps.ensure("Old"); ps.ensure("Live"); ps.set_archived("old", True)
    was = V.BASE_DIR
    try:
        V.BASE_DIR = d
        check("the thread list reads the archived projects from projects.json",
              V.archived_projects() == {"old"}, V.archived_projects())
        (d / "projects.json").write_text("not json")
        for extra in d.glob("projects.json.*"):
            extra.unlink()                  # no backup to recover from either
        check("and an unreadable file archives nothing rather than failing",
              V.archived_projects() == set())
    finally:
        V.BASE_DIR = was


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
  else if (url.startsWith("/api/tasks")) {
    asked.push(body);
    out = (body.action === "list" || body.action === "attention")
      ? {ok: true, tasks, counts: {}}
      // Opening a row asks for the task itself.
      : body.action === "task" ? {ok: true, task: tasks.find(t => t.id === body.id)}
      : {ok: true};
  }
  return {json: async () => out, text: async () => ""};
};
const asked = [];

(0, eval)(src + "\n;globalThis.__renderTasks = renderTasks; globalThis.__openMore = openMore;");
(async () => {
  await globalThis.__renderTasks();
  const list = byId("tlist").innerHTML, more = {};
  // Open every row that has something behind its title, as a click would.
  for (const m of list.matchAll(/id="more-([^"]+)"/g)) {
    await globalThis.__openMore(m[1]);
    more[m[1]] = byId("more-" + m[1]).innerHTML;
  }
  process.stdout.write(JSON.stringify({list, more, asked}));
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
    out = json.loads(p.stdout)
    html, opened_rows = out["list"], out["more"]
    behind = opened_rows.get("tsk_proposed1", "")

    # The goal is fetched when the row is opened (openMore), not carried by
    # every row of a list the page used to poll every five seconds.
    check("opening the proposal shows its full goal",
          deep in behind and "FINAL LINE OF THE CASE" in behind,
          "the panel still shows only the truncated title")
    check("asked for the whole task, reply untruncated",
          {"action": "task", "id": "tsk_proposed1", "full": True} in out["asked"],
          str(out["asked"]))
    check("a failed row says why it failed, unopened",
          "NO WORKTREE, BRANCH ALREADY EXISTS" in html,
          "Retry and Dismiss with no reason on offer")
    check("the goal is escaped, not injected",
          "&lt;script&gt;alert(1)&lt;/script&gt;" in behind
          and "<script>alert(1)</script>" not in behind + html)

    # Scannable by default: fifty rows of three thousand characters is only a
    # different way of being unreadable.
    opened = _re.findall(r"<details[^>]*>", html)
    check("every row opens shut", opened and not any("open" in o for o in opened),
          f"found: {opened}")
    # Scope this to the row itself: a window of characters would reach back
    # into the proposal above it and find that row's disclosure instead.
    short = next(r for r in html.split('<div class="task"') if "tsk_short1" in r)
    check("a goal that fits its title gets no disclosure at all",
          "<details" not in short, "an empty expander is worse than none")


# --- per-project boards and the projects overview ------------------------------
# The task panel answers "what needs me"; nothing answered "where is this
# project". board.py builds both views from the records, with the slow inputs
# (branch survey, release plan) cached; handle_tasks serves them; the page
# renders them. Driven here against real stores, real git and the page's own
# javascript under node.


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


def test_filing_a_thread_never_rewrites_a_projects_scope():
    print("\nprojects: `!project <name>` files a thread without touching an existing project's scope")
    import projects
    root = Path(os.path.realpath(tempfile.mkdtemp()))
    saved_root = projects.PROJECT_ROOT
    projects.PROJECT_ROOT = root / "projects"
    try:
        ps = projects.ProjectStore(root / "projects.json")
        st = SessionStore(root / "s.json")
        ns = lift("file_thread_under", store=st, project_store=ps, Path=Path)
        # Production, 2026-10-09: `!project Silkworm` in a fresh DM thread
        # turned silkworm's scope into {cwd: ~/workspace/projects/silkworm}.
        repo = root / "Silkworm"
        (repo / ".git").mkdir(parents=True)
        want = {"cwd": str(repo), "repo": "github.com/me/Silkworm", "branch": "x"}
        ps.ensure("Silkworm", scope=dict(want), test_cmd="./bin/silkworm test",
                  auto_merge=True)
        before = ps.get("silkworm")
        elsewhere = root / "elsewhere"
        elsewhere.mkdir()
        st.update("C:fresh", project="", unfiled=True)
        st.update("C:other", cwd=str(elsewhere), session_id="sess-1")
        for k in ("C:fresh", "C:other"):
            reply = ns["file_thread_under"](k, "Silkworm")
            after = ps.get("silkworm")
            check(f"{k}: the existing project's scope is exactly what it was",
                  after["scope"] == want, str(after["scope"]))
            check(f"{k}: and nothing else about it changed either",
                  {f: after[f] for f in projects.FIELDS if f != "updated"}
                  == {f: before[f] for f in projects.FIELDS if f != "updated"}, str(after))
            check(f"{k}: no projects/<slug> folder is made for it",
                  not (projects.PROJECT_ROOT / "silkworm").exists())
            e = st.get(k)
            check(f"{k}: the thread is filed and works in the real checkout",
                  e["project"] == "silkworm" and e["cwd"] == str(repo)
                  and e["unfiled"] is False and "Filed under" in reply, str(e))
        e = st.get("C:other")
        check("a session from the old directory is retired, its id kept",
              e["session_id"] is None and e["previous_sessions"] == ["sess-1"], str(e))

        # Only a brand-new project takes its first directory from the thread.
        st.update("C:new", cwd=str(elsewhere))
        ns["file_thread_under"]("C:new", "Fresh Thing")
        check("a new project starts from the thread's directory",
              ps.scope_for("fresh-thing") == {"cwd": str(elsewhere)}, str(ps.get("fresh-thing")))
        st.update("C:again", cwd=str(repo))
        ns["file_thread_under"]("C:again", "Fresh Thing")
        check("and a second thread filing under it does not move it",
              ps.scope_for("fresh-thing") == {"cwd": str(elsewhere)}
              and st.get("C:again")["cwd"] == str(elsewhere), str(ps.get("fresh-thing")))

        # Repo-less projects keep their own home, as before.
        st.update("C:trip", project="")
        ns["file_thread_under"]("C:trip", "Asia Trip")
        home = projects.PROJECT_ROOT / "asia-trip"
        check("a repo-less project with no directory gets its home, and the thread goes there",
              home.is_dir() and ps.scope_for("asia-trip") == {"cwd": str(home)}
              and st.get("C:trip")["cwd"] == str(home), str(st.get("C:trip")))
    finally:
        projects.PROJECT_ROOT = saved_root

    bot = (BASE / "bot.py").read_text()
    tree = ast.parse(bot)
    cmd = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
               and n.name == "handle_command")
    calls = {c.func.id for c in ast.walk(cmd) if isinstance(c, ast.Call)
             and isinstance(c.func, ast.Name)}
    check("`!project <name>` goes through file_thread_under",
          "file_thread_under" in calls)
    # Every ensure() handing over a scope must either start from the record's
    # own scope or only seed a new project; a bare thread directory as `scope=`
    # is the overwrite.
    bare = [c.lineno for c in ast.walk(tree) if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute) and c.func.attr == "ensure"
            and any(k.arg == "scope" and isinstance(k.value, (ast.Dict, ast.IfExp))
                    and not (isinstance(k.value, ast.Dict) and None in k.value.keys)
                    for k in c.keywords)]
    check("no ensure() call in bot.py replaces a scope with a literal one",
          not bare, f"lines {bare}")


def test_a_renamed_project_keeps_one_record():
    print("\nprojects: renaming a project never spawns a ghost duplicate")
    import projects
    root = Path(os.path.realpath(tempfile.mkdtemp()))
    saved_root = projects.PROJECT_ROOT
    projects.PROJECT_ROOT = root / "projects"
    try:
        ps = projects.ProjectStore(root / "projects.json")
        ps.ensure("Asia Trip")
        check("a repo-less project starts with no directory",
              not ps.scope_for("asia-trip").get("cwd"))
        # The dashboard's rename: slug kept, title changed.
        ps.ensure("asia-trip", title="Japan 2027")
        home = ps.home("asia-trip", create=True)
        slugs = [p["slug"] for p in ps.all()]
        check("making its home after a rename leaves exactly one project",
              slugs == ["asia-trip"], str(slugs))
        check("and that project holds the directory",
              ps.scope_for("asia-trip").get("cwd") == str(home)
              and home == projects.PROJECT_ROOT / "asia-trip", str(ps.get("asia-trip")))
        ps.set_brief("asia-trip", "Two weeks in October.")
        check("a brief written after the rename goes to the same project",
              [p["slug"] for p in ps.all()] == ["asia-trip"]
              and ps.brief_for("asia-trip") == "Two weeks in October.")
        check("home() reads back the stored directory",
              ps.home("asia-trip") == home)

        # A second rename, then lookups by the new title.
        ps.ensure("asia-trip", title="Japan 2028")
        r = ps.ensure("Japan 2028")
        check("ensure(<new title>) finds the renamed project",
              r["slug"] == "asia-trip" and [p["slug"] for p in ps.all()] == ["asia-trip"],
              str([p["slug"] for p in ps.all()]))
        check("case and spacing do not matter",
              ps.ensure("  japan   2028 ")["slug"] == "asia-trip"
              and ps.slug_for("JAPAN 2028") == "asia-trip")
        check("the title is not written over by the lookup",
              ps.get("asia-trip")["title"] == "Japan 2028")
        check("its slug still names it after the rename",
              ps.slug_for("Asia Trip") == "asia-trip")
        check("a genuinely new name still makes a new project",
              ps.ensure("Peru")["slug"] == "peru" and ps.get("peru") is not None)
        # A slug that exists wins over another project's title.
        ps.ensure("peru", title="Chile")
        ps.ensure("Chile Two")
        check("an existing slug wins over a title match",
              ps.slug_for("Chile Two") == "chile-two" and ps.slug_for("chile") == "peru")

        # A title that is another project's slug: looked up by title, the
        # directory went to the other project.
        ps.ensure("Lima")
        ps.ensure("Oslo")
        ps.ensure("oslo", title="Lima")
        oslo_home = ps.home("oslo", create=True)
        check("a renamed project's home is recorded on it, not on the project its title names",
              ps.scope_for("oslo").get("cwd") == str(oslo_home)
              and not ps.scope_for("lima").get("cwd"),
              f"oslo={ps.scope_for('oslo')} lima={ps.scope_for('lima')}")

        # A stale copy of the record is not merged back over a newer write.
        ps.ensure("Kyoto")
        ps.ensure("kyoto", test_cmd="make test")
        ps.home("kyoto", create=True)
        check("recording the directory keeps fields written meanwhile",
              ps.get("kyoto")["test_cmd"] == "make test"
              and ps.scope_for("kyoto").get("cwd"))

        # Creating a project under a renamed one's title is refused, and the
        # renamed project's scope is untouched.
        import workspaces
        repo = root / "trader"
        (repo / ".git").mkdir(parents=True)
        ps.ensure("Trader", scope={"cwd": str(repo), "repo": "github.com/x/trader"})
        ps.ensure("trader", title="Trading Bot")
        before = ps.scope_for("trader")
        try:
            workspaces.create("Trading Bot", ps, root=root / "workspace",
                              gh=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no gh")))
            refused = ""
        except workspaces.Refused as e:
            refused = str(e)
        check("a new project named after a renamed one's title is refused",
              "`trader`" in refused and ps.scope_for("trader") == before
              and not (root / "workspace" / "trading-bot").exists(), refused)

        # The dashboard rename refuses a name another project already has.
        ns = lift("handle_projects", project_store=ps, projects=projects,
                           scoping=__import__("scoping"), absent=PROJECT_ROUTES)
        r = ns["handle_projects"]({"action": "title", "slug": "kyoto", "title": "trading  BOT"})
        check("renaming to another project's name is refused",
              not r["ok"] and "trader" in r["error"] and ps.get("kyoto")["title"] == "Kyoto", r)
        r = ns["handle_projects"]({"action": "title", "slug": "kyoto", "title": "Lima"})
        check("so is renaming to another project's slug",
              not r["ok"] and ps.get("kyoto")["title"] == "Kyoto", r)
        r = ns["handle_projects"]({"action": "title", "slug": "kyoto", "title": "KYOTO"})
        check("re-casing a project's own name is fine",
              r["ok"] and ps.get("kyoto")["title"] == "KYOTO", r)
    finally:
        projects.PROJECT_ROOT = saved_root


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
    found = lift({"handle_tasks", "board_survey", "board_release", "_board_survey",
                      "_board_releases", "_with_costs", "_spend"}, ns=ns,
                 absent=TASK_ROUTES + STEP_ROUTES)["__lifted__"]
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
          == {"implementor": True, "assistant": False, "researcher": False})
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

    # Archived means off every all-projects list, not only the pickers.
    idle = ts.create("An idea under alpha", project="alpha", state="proposed", role="implementor")
    hp({"action": "archive", "slug": "alpha"})
    run = ts.create("Running under alpha", project="alpha", state="queued",
                    driver="queue", role="implementor")
    ts.transition(run["id"], "running")
    ids = lambda r: {x["id"] for x in r["tasks"]}
    lst = ht({"action": "list"})
    check("an archived project's tasks leave the all-projects list",
          idle["id"] not in ids(lst), sorted(ids(lst)))
    check("but running and queued work stays (it spends; deploy's wait reads this list)",
          run["id"] in ids(lst) and t["id"] in ids(lst))
    check("asking for the project by name still shows them",
          idle["id"] in ids(ht({"action": "list", "project": "alpha"})))
    check("and so does asking for archived ones",
          idle["id"] in ids(ht({"action": "list", "archived": True})))
    check("the badge counts leave it out too",
          ht({"action": "counts"})["counts"].get("proposed", 0) == 0
          and ht({"action": "counts", "archived": True})["counts"].get("proposed") == 1,
          ht({"action": "counts"}))
    brd = ht({"action": "board"})
    on = {c["id"] for col in brd["columns"].values() for c in col}
    check("the all-projects board leaves it out; its own board does not",
          idle["id"] not in on and idle["id"] in {c["id"] for col in ht(
              {"action": "board", "project": "alpha"})["columns"].values() for c in col})
    # Narrowed lists (one state, attention) do not hold a reviewer's parent.
    ts.transition(t["id"], "running"); ts.transition(t["id"], "blocked")
    frev = ts.create("Review: under alpha", state="queued", role="reviewer", parent=t["id"],
                     driver="queue")
    ts.transition(frev["id"], "running"); ts.transition(frev["id"], "failed")
    check("a failed review of archived work leaves the attention list, its parent unlisted",
          frev["id"] not in ids(ht({"action": "attention"}))
          and frev["id"] not in ids(ht({"action": "list", "state": "failed"})))
    ts.transition(run["id"], "done")
    hp({"action": "unarchive", "slug": "alpha"})
    check("unarchived, they are back", idle["id"] in ids(ht({"action": "list"}))
          and frev["id"] in ids(ht({"action": "attention"})))
    ts.transition(frev["id"], "cancelled"); ts.transition(t["id"], "queued")
    ts.transition(idle["id"], "cancelled")

    # Retire: archive and dismiss the open work; preview first, refused while running.
    prop = ts.create("A proposal under alpha", project="alpha", state="proposed", role="implementor")
    rev = ts.create("Review: alpha", state="queued", role="reviewer", parent=t["id"], driver="queue")
    turn = ts.create("a message waiting for its thread", project="alpha", state="queued",
                     source="slack", source_ref="D1:1.0")
    finished = ts.create("Landed work", project="alpha", state="queued", role="implementor")
    ts.transition(finished["id"], "running"); ts.transition(finished["id"], "done")
    pv = hp({"action": "retire-plan", "slug": "alpha"})
    check("the preview names what it would close, and changes nothing",
          pv["ok"] and {x["id"] for x in pv["close"]} == {t["id"], prop["id"]}
          and not ps.get("alpha").get("archived") and ts.get(t["id"])["state"] == "queued", pv)
    busy = ts.create("Busy under alpha", project="alpha", state="queued", role="implementor")
    ts.transition(busy["id"], "running")
    r = hp({"action": "retire", "slug": "alpha"})
    check("retiring is refused while one of its tasks runs, and nothing moves",
          not r["ok"] and "running" in r["error"] and not ps.get("alpha").get("archived")
          and ts.get(prop["id"])["state"] == "proposed", r)
    ts.transition(busy["id"], "done")
    rrev = ts.create("Review: running", state="queued", role="reviewer", parent=prop["id"],
                     driver="queue")
    ts.transition(rrev["id"], "running")
    r = hp({"action": "retire", "slug": "alpha"})
    check("and while one of its reviews runs (cancelling the parent would not stop it)",
          not r["ok"] and "running" in r["error"] and ts.get(prop["id"])["state"] == "proposed", r)
    ts.transition(rrev["id"], "done")
    ns["operator_refused"] = lambda *a: None       # the operator's guard lets it through...
    r = hp({"action": "retire", "slug": "alpha", "caller_role": "operator", "caller_thread": "D1:9.0"})
    check("and from inside a turn: dismissing is yours",
          not r["ok"] and "yours" in r["error"] and ts.get(prop["id"])["state"] == "proposed", r)
    r = hp({"action": "retire", "slug": "alpha"})
    check("retiring archives it and dismisses its open tasks",
          r["ok"] and ps.get("alpha")["archived"] and set(r["closed"]) == {t["id"], prop["id"]}
          and ts.get(t["id"])["state"] == "cancelled" and ts.get(prop["id"])["state"] == "cancelled", r)
    check("their reviews end with them", ts.get(rev["id"])["state"] == "cancelled",
          ts.get(rev["id"])["state"])
    check("a turn waiting for its thread is left alone",
          ts.get(turn["id"])["state"] == "queued")
    check("done work is left alone", ts.get(finished["id"])["state"] == "done")
    check("its settings stay, so unarchiving restores it",
          ps.get("alpha")["test_cmd"] == "./bin/test"
          and hp({"action": "unarchive", "slug": "alpha"})["ok"] and not ps.get("alpha")["archived"])
    check("retiring an unknown project is refused",
          not hp({"action": "retire", "slug": "nope"})["ok"]
          and not hp({"action": "retire-plan", "slug": "nope"})["ok"])


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
    cols_at = bh.find('<div class="bcols"')          # it carries the phone's data-show
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

# --- the dashboard, polled through a tunnel ------------------------------------
# Measured 2026-10-12: /api/sessions took 0.95s (a 410-directory glob per
# thread) and returned 458KB every five seconds; the task badge polled `list`
# for 940KB of whole goals and replies; /api/stats read every transcript in
# full, 2.4s, every thirty seconds; the open transcript was refetched whole
# every four. Over an SSH tunnel that was over a megabyte per five seconds, and
# the page redrew everything each time.


def test_dashboard_reads_answer_304():
    import gzip
    import threading
    import urllib.error
    import urllib.request
    sys.argv = ["x"]
    import visualizer as V
    print("\nthe dashboard's reads: 304 when unchanged, gzip through the tunnel")
    d, proj, sessions = _viz_fixture()
    (proj / "-a" / "sid-one.jsonl").write_text(_tline("user", "hello"))
    saved = (V.SESSIONS_FILE, V.PROJECTS_DIR, V.bot_call, V.procs.alive_sessions, V.STALL_AFTER_S)
    bot_asked = []

    def fake_bot(path, payload, timeout=1.0):
        bot_asked.append((path, dict(payload)))
        if path == "/tasks":
            if payload.get("action") == "board":
                return {"ok": True, "columns": {"backlog": []}, "n": len(bot_asked) * 0}
            return {"ok": True, "did": payload.get("action")}
        return {}

    srv = None
    try:
        V.SESSIONS_FILE, V.PROJECTS_DIR = d / "sessions.json", proj
        V.bot_call = fake_bot
        V.procs.alive_sessions = lambda sids: {"sid-two"}       # the turn is alive
        srv = V.make_server("127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def get(path, tag=None, method="GET", body=None, gz=False):
            h = {"Content-Type": "application/json"}
            if tag:
                h["If-None-Match"] = tag
            if gz:
                h["Accept-Encoding"] = "gzip"
            req = urllib.request.Request(base + path, headers=h, method=method,
                                         data=json.dumps(body).encode() if body else None)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, dict(r.headers), r.read()
            except urllib.error.HTTPError as e:
                return e.code, dict(e.headers), e.read()

        code, hdr, raw = get("/api/sessions")
        tag = hdr.get("ETag")
        check("a full answer carries an ETag", code == 200 and bool(tag), str(hdr))
        code2, _, raw2 = get("/api/sessions", tag)
        check("asking again with it is a 304 and no body", code2 == 304 and raw2 == b"",
              f"{code2} {len(raw2)}B")
        check("the 200 is not a 304 for a page holding nothing",
              get("/api/sessions", '"stale"')[0] == 200)

        # A running turn's age moves every second; it must not defeat the 304.
        time.sleep(1.2)
        code3, _, _ = get("/api/sessions", tag)
        check("a running turn ageing a second is still unchanged", code3 == 304, code3)
        # Crossing into stalled is a change.
        V.STALL_AFTER_S = 1
        code4, hdr4, raw4 = get("/api/sessions", tag)
        stalled = next(s for s in json.loads(raw4)["sessions"] if s["key"] == "D1:2.2")
        check("a turn going stalled is a new answer",
              code4 == 200 and hdr4.get("ETag") != tag and stalled["turn"]["stalled"])
        V.STALL_AFTER_S = saved[4]
        tag = get("/api/sessions")[1].get("ETag")
        sessions["D1:1.1"]["title"] = "renamed"
        (d / "sessions.json").write_text(json.dumps(sessions))
        code5, hdr5, raw5 = get("/api/sessions", tag)
        check("a renamed thread is a new answer", code5 == 200 and hdr5.get("ETag") != tag
              and "renamed" in raw5.decode())

        code, hdr, zipped = get("/api/sessions", gz=True)
        # Compared by validator: a running turn's age may tick between the two.
        check("gzipped when the browser accepts it",
              hdr.get("Content-Encoding") == "gzip"
              and V.sessions_tag(json.loads(gzip.decompress(zipped)))
              == V.sessions_tag(json.loads(raw5)),
              str(hdr))
        check("and well under the plain size", len(zipped) < len(raw5) / 2,
              f"{len(zipped)} vs {len(raw5)}")

        for path in ("/api/stats", "/api/artifacts?key=D1:1.1",
                     "/api/session?id=sid-one&from=0"):
            t = get(path)[1].get("ETag")
            check(f"{path.split('?')[0]} answers 304 unchanged", bool(t) and get(path, t)[0] == 304)

        board = {"action": "board", "project": "alpha"}
        c1, h1, b1 = get("/api/tasks", method="POST", body=board)
        c2, _, b2 = get("/api/tasks", h1.get("ETag"), "POST", board)
        check("a board read answers 304 unchanged", c1 == 200 and c2 == 304 and b2 == b"",
              f"{c1} {c2}")
        n = len(bot_asked)
        c3, _, b3 = get("/api/tasks", h1.get("ETag"), "POST", {"action": "accept", "id": "t1"})
        c4, _, b4 = get("/api/tasks", etag_of_accept := V.etag_for({"ok": True, "did": "accept"}),
                        "POST", {"action": "accept", "id": "t1"})
        check("an action never answers 304, even matching what was sent before",
              c3 == 200 and c4 == 200 and json.loads(b4)["did"] == "accept"
              and len(bot_asked) == n + 2, f"{c3} {c4}")
        check("every read action is named, and no action that changes anything is",
              {"accept", "dismiss", "approve", "rework", "cancel", "retry", "land", "drop",
               "create", "edit", "release", "close", "dig", "resolve",
               "run-next"}.isdisjoint(V.READ_TASK_ACTIONS))
        V.bot_call = lambda *a, **k: {}
        c5, h5, b5 = get("/api/tasks", method="POST", body=board)
        check("bot offline is said, not cached", c5 == 200 and "ETag" not in h5
              and json.loads(b5)["ok"] is False)
    finally:
        (V.SESSIONS_FILE, V.PROJECTS_DIR, V.bot_call, V.procs.alive_sessions,
         V.STALL_AFTER_S) = saved
        if srv:
            srv.shutdown()


def test_transcript_index_and_cursor():
    sys.argv = ["x"]
    import visualizer as V
    print("\ntranscripts: found without a glob per thread, read from where the page stopped")
    d, proj, _ = _viz_fixture()
    saved = (V.PROJECTS_DIR, V.INDEX_RECHECK_S)
    try:
        V.PROJECTS_DIR, V.INDEX_RECHECK_S = proj, 0
        tp = proj / "-a" / "sid-one.jsonl"
        tp.write_text(_tline("user", "first") + _tline("assistant", "second"))
        check("found", V.find_transcript("sid-one") == tp)
        check("an unknown session is not", V.find_transcript("nope") is None)
        (proj / "-c").mkdir()
        (proj / "-c" / "sid-new.jsonl").write_text(_tline("user", "hi"))
        check("a session in a project dir made since is found",
              V.find_transcript("sid-new") == proj / "-c" / "sid-new.jsonl")
        (proj / "-b" / "sid-late.jsonl").write_text(_tline("user", "hi"))
        check("so is a new transcript in a dir already indexed",
              V.find_transcript("sid-late") is not None)
        (proj / "-b" / "sid-late.jsonl").unlink()
        check("a deleted one is gone", V.find_transcript("sid-late") is None)
        (proj / "-b" / "sid-b.jsonl").write_text(_tline("user", "hi"))
        real_scan, flaky = V.os.scandir, {"left": 1}

        def scan(path):
            if str(path).endswith("-b") and flaky["left"]:
                flaky["left"] -= 1
                raise OSError(24, "Too many open files")
            return real_scan(path)
        V.os.scandir = scan
        try:
            missed = V.find_transcript("sid-b")
            found = V.find_transcript("sid-b")
        finally:
            V.os.scandir = real_scan
        check("a directory that could not be read is read again, not hidden",
              missed is None and found == proj / "-b" / "sid-b.jsonl", f"{missed} {found}")
        calls = []
        real = V.os.scandir
        V.os.scandir = lambda p: calls.append(p) or real(p)
        V.INDEX_RECHECK_S = 60
        try:
            for _ in range(400):
                V.find_transcript("sid-one")
        finally:
            V.os.scandir = real
        check("four hundred lookups in a second scan nothing", len(calls) <= 1, len(calls))
        V.INDEX_RECHECK_S = 0

        full = V.load_transcript("sid-one")
        first = V.load_transcript("sid-one", 0)
        check("from 0, the same messages as the whole read",
              first["messages"] == full["messages"] and len(first["messages"]) == 2)
        check("with a cursor at the end of the file", first["cursor"] == tp.stat().st_size)
        with open(tp, "a") as f:
            f.write(_tline("user", "third") + _tline("assistant", "fourth")[:25])
        nxt = V.load_transcript("sid-one", first["cursor"])
        check("next, only what was written since",
              [m["blocks"][0]["text"] for m in nxt["messages"]] == ["third"],
              str(nxt["messages"]))
        check("a line still being written is left for later",
              nxt["cursor"] < tp.stat().st_size and not nxt["reset"])
        with open(tp, "a") as f:
            f.write(_tline("assistant", "fourth")[25:])
        last = V.load_transcript("sid-one", nxt["cursor"])
        check("and read whole once finished",
              [m["blocks"][0]["text"] for m in last["messages"]] == ["fourth"])
        none = V.load_transcript("sid-one", last["cursor"])
        check("nothing new is nothing", none["messages"] == [] and none["cursor"] == last["cursor"])
        tp.write_text(_tline("user", "replaced"))
        again = V.load_transcript("sid-one", last["cursor"])
        check("a file replaced underneath says reset and starts over",
              again["reset"] and [m["blocks"][0]["text"] for m in again["messages"]] == ["replaced"])
    finally:
        V.PROJECTS_DIR, V.INDEX_RECHECK_S = saved


def test_stats_read_incrementally():
    sys.argv = ["x"]
    import visualizer as V
    print("\n/api/stats reads each transcript once, then only what is appended")
    d, proj, sessions = _viz_fixture()
    saved = (V.SESSIONS_FILE, V.PROJECTS_DIR, V.INDEX_RECHECK_S, V._complete_lines)
    today = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
    u = lambda c, i, o: {"cache_read_input_tokens": c, "input_tokens": i,
                         "cache_creation_input_tokens": 0, "output_tokens": o}
    try:
        V.SESSIONS_FILE, V.PROJECTS_DIR, V.INDEX_RECHECK_S = d / "sessions.json", proj, 0
        V._usage.clear()
        (proj / "-a" / "sid-one.jsonl").write_text(
            _tline("user", "q", today) + _tline("assistant", "a", today, u(900, 100, 50))
            + _tline("assistant", "b", "2020-01-01T00:00:00Z", u(0, 0, 7), "claude-old"))
        (proj / "-b" / "sid-two.jsonl").write_text(
            _tline("assistant", "c", today, u(0, 1000, 5)))
        reads = []
        V._complete_lines = lambda p, s: reads.append(str(p)) or saved[3](p, s)
        st = V.load_stats()
        day = st["days"][-1]
        check("today's tokens, summed across transcripts",
              day == {"date": day["date"], "cache_read": 900, "fresh_in": 1100, "out": 55}, str(day))
        check("the cache rate counts every line, not only the last fortnight's",
              st["cache_rate"] == round(100 * 900 / 2000, 1), st["cache_rate"])
        check("output by model, every line", dict(st["models"]) ==
              {"claude-x": 55, "claude-old": 7}, str(st["models"]))
        n = len(reads)
        check("unchanged, nothing is read again", V.load_stats() == st and len(reads) == n)
        with open(proj / "-b" / "sid-two.jsonl", "a") as f:
            f.write(_tline("assistant", "d", today, u(100, 0, 1)))
        st2 = V.load_stats()
        check("an appended line is added, and only that file read",
              st2["days"][-1]["cache_read"] == 1000 and st2["days"][-1]["out"] == 56
              and reads[n:] == [str(proj / "-b" / "sid-two.jsonl")], str(reads[n:]))
        (proj / "-b" / "sid-two.jsonl").write_text(_tline("assistant", "e", today, u(1, 1, 1)))
        st3 = V.load_stats()
        check("a transcript replaced by a shorter one is recounted, not added to",
              st3["days"][-1] == {"date": day["date"], "cache_read": 901, "fresh_in": 101,
                                  "out": 51}, str(st3["days"][-1]))
    finally:
        V.SESSIONS_FILE, V.PROJECTS_DIR, V.INDEX_RECHECK_S, V._complete_lines = saved
        V._usage.clear()


# --- the action CLI: the dashboard's buttons, as commands an agent can run -------
# An agent driving the board needs commands it can be allowlisted for, not raw
# curl. Each must take the same handler and transition() as the button, so a
# refusal is the same refusal -- and must exit non-zero on one, or a script
# reads "refused" as done.

def _action_cli_bot():
    """A stand-in bot on a free localhost port, serving the real handlers.

    /tasks is handle_tasks lifted from bot.py over a real TaskStore (with
    decide_findings, land_or_drop and approve_task as shipped); /status is
    handle_status lifted the same way. Only what talks to Slack, git or the
    Keychain is faked.
    """
    import socket as _socket
    import board, costs, drain, research, revision, retry, slacklinks
    import tasks as T
    from localserver import LocalServer
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    landings, survey_rows, broken = [], [], {}

    def survey(recs, scope_for=None):
        if broken.get("survey"):
            raise RuntimeError("git went away")
        return list(survey_rows)
    branches_fake = types.SimpleNamespace(
        survey=survey,
        line=lambda rows: f"{len(rows)} branch(es)")
    hold, dr = retry.Hold(), drain.Drain()
    ns = lift(
        "handle_tasks", "approve_task", "decide_findings", "land_or_drop",
        "_with_costs", "_spend", "handle_status",
        tasks=T, task_store=st, research=research, holding=__import__("holding"),
        task_state=_task_state_for(st), merge=__import__("merge"),
        roles=__import__("roles"), board=board, costs=costs, slacklinks=slacklinks,
        defer=__import__("defer"), scoping=__import__("scoping"), projects=__import__("projects"),
        branches=branches_fake,
        project_store=types.SimpleNamespace(scope_for=lambda slug: None,
                                            get=lambda slug: None),
        tell_thread=lambda *a: None, stop_task=lambda tid: False,
        start_landing=lambda tid, **k: landings.append(tid) or True,
        # /status: the real revision check, against a boot sha that is not HEAD.
        store=tmp_store(), RUNNING={}, revision=revision, BASE_DIR=BASE,
        REVISION={"sha": "0" * 40, "dirty": False},
        slack=types.SimpleNamespace(status=lambda now: {"connected": True}),
        credentials=types.SimpleNamespace(state=lambda has_token: {"mode": "token"}),
        daemons=types.SimpleNamespace(status=lambda: {"count": 1, "alive": 1}),
        RUNNER_HOLD=hold, DRAIN=dr, os=os,
        absent=TASK_ROUTES + STEP_ROUTES + ('record_landing', 'repo_guard'))
    with _socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = LocalServer(port)
    server.route("/tasks", ns["handle_tasks"])
    server.route("/status", ns["handle_status"])
    server.start()
    return types.SimpleNamespace(port=port, store=st, landings=landings,
                                 survey_rows=survey_rows, drain=dr, broken=broken)


def _run_cli(port, *argv):
    # As you would run it: outside any turn, so not stamped as one (the
    # operator's tests run it as one on purpose; see _op_cli).
    env = {k: v for k, v in os.environ.items()
           if k not in ("SILKWORM_ROLE", "SILKWORM_THREAD", "SILKWORM_TASK_ID",
                        "SILKWORM_TASK_ROLE")}
    env["SILKWORM_BOT_PORT"] = str(port)
    return subprocess.run([sys.executable, str(BASE / "bin" / "silkworm"), *argv],
                          capture_output=True, text=True, env=env, timeout=60)


def test_action_cli():
    print("\nthe action CLI takes the dashboard's paths, and a refusal exits non-zero")
    import research, tasks as T
    bot = _action_cli_bot()
    st = bot.store

    prop = st.create("Add an appearance preference to Settings", state=T.PROPOSED,
                     role="implementor", project="alpha")["id"]
    r = _run_cli(bot.port, "accept", prop)
    check("accept queues a proposal and exits 0",
          r.returncode == 0 and st.get(prop)["state"] == T.QUEUED, r.stdout + r.stderr)
    check("with one line naming what it did",
          r.stdout.strip() == f"ok: accept {prop} -> queued", repr(r.stdout))
    check("the event log says it came from the CLI, through transition()",
          st.get(prop)["events"][-1]["detail"] == "accept via cli",
          str(st.get(prop)["events"][-1]))

    busy = st.create("Rename the sync engine's retry helper", role="implementor")["id"]
    st.transition(busy, T.RUNNING)
    r = _run_cli(bot.port, "accept", busy)
    check("accepting a running task is the bot's refusal, exit 1, on stderr",
          r.returncode == 1 and T.RUNNING_BUSY in r.stderr and not r.stdout
          and st.get(busy)["state"] == T.RUNNING, r.stdout + r.stderr)

    r = _run_cli(bot.port, "edit", prop, "--title", "Appearance setting")
    check("edit changes unrun work",
          r.returncode == 0 and st.get(prop)["title"] == "Appearance setting", r.stderr)
    r = _run_cli(bot.port, "edit", prop, "--goal", "short")
    check("an edit the route refuses is refused here, in its words",
          r.returncode == 1 and "at least" in r.stderr, r.stderr)
    r = _run_cli(bot.port, "edit", prop)
    check("an edit that changes nothing is a usage error", r.returncode == 2, r.stderr)

    r = _run_cli(bot.port, "land", prop)
    check("land on unfinished work is refused as the button is",
          r.returncode == 1 and "only finished work" in r.stderr and not bot.landings,
          r.stderr)
    done = st.create("Fix the countdown rounding in DeadlineView", role="implementor",
                     project="alpha")["id"]
    st.transition(done, T.RUNNING); st.transition(done, T.DONE)
    r = _run_cli(bot.port, "land", done)
    check("land on finished work starts the same landing",
          r.returncode == 0 and bot.landings == [done] and "landing under way" in r.stdout,
          r.stdout + r.stderr)

    review = st.create("Tidy the sync retry loop in SyncEngine", role="implementor")["id"]
    st.transition(review, T.RUNNING); st.transition(review, T.AWAITING_APPROVAL)
    r = _run_cli(bot.port, "rework", review)
    check("rework without notes is a usage error, and sends nothing",
          r.returncode == 2 and st.get(review)["state"] == T.AWAITING_APPROVAL, r.stderr)
    r = _run_cli(bot.port, "rework", review, "--notes", "Keep the backoff capped at 60s")
    check("rework sends it back with the notes, as the person's words",
          r.returncode == 0 and st.get(review)["state"] == T.QUEUED
          and "From cli:\nKeep the backoff capped at 60s" in st.get(review)["goal"],
          r.stdout + r.stderr)
    r = _run_cli(bot.port, "dismiss", review, "--by", "operator")
    check("dismiss cancels, and --by names who in the log",
          r.returncode == 0 and st.get(review)["state"] == T.CANCELLED
          and "dismiss via operator" in st.get(review)["events"][-1]["detail"], r.stderr)

    rt = _research_parked(st)
    r = _run_cli(bot.port, "dig", rt)
    check("dig with no question is the bot's refusal",
          r.returncode == 1 and "say what to dig into" in r.stderr, r.stderr)
    r = _run_cli(bot.port, "close", rt, "--round", "stale-round")
    check("a stale --round is refused, as a stale click is",
          r.returncode == 1 and st.get(rt)["state"] == T.NEEDS_INPUT, r.stderr)
    rnd = research.round_of(st.get(rt))
    r = _run_cli(bot.port, "dig", rt, "--notes", "and the CDN?", "--round", rnd)
    check("dig sends it back with the question",
          r.returncode == 0 and st.get(rt)["state"] == T.QUEUED, r.stdout + r.stderr)
    r = _run_cli(bot.port, "close", prop)
    check("close on a task with no findings is refused",
          r.returncode == 1 and "only a research task" in r.stderr, r.stderr)

    for cmd, extra in (("accept", ()), ("dismiss", ()), ("land", ()), ("drop", ()),
                       ("edit", ("--title", "x")), ("rework", ("--notes", "x")),
                       ("close", ()), ("dig", ("--notes", "x"))):
        r = _run_cli(bot.port, cmd, "tsk_nosuchthing", *extra)
        check(f"{cmd} on an unknown id exits 1 saying so",
              r.returncode == 1 and "unknown task" in r.stderr, r.stderr)

    r = _run_cli(bot.port, "accept")
    check("a missing id is a usage error, not the subcommand read as the id",
          r.returncode == 2 and "usage: silkworm accept <task id>" in r.stderr, r.stderr)
    r = _run_cli(bot.port, "accept", prop, "--bogus", "1")
    check("an unknown option is a usage error", r.returncode == 2, r.stderr)
    r = _run_cli(bot.port, "rework", prop, "--notes", "  ")
    check("blank notes are a usage error, not an empty send-back",
          r.returncode == 2 and st.get(prop)["state"] == T.QUEUED
          and "Sent back" not in st.get(prop)["goal"], r.stderr)
    r = _run_cli(bot.port, "accept", prop, "--by", "")
    check("a blank --by is a usage error", r.returncode == 2, r.stderr)

    with __import__("socket").socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    r = _run_cli(dead, "accept", prop)
    check("an unreachable bot is exit 3, not a refusal",
          r.returncode == 3 and "did not answer" in r.stderr, r.stderr)

    # --- status --json ------------------------------------------------------------
    failed = st.create("Make the widget refresh at midnight", role="implementor",
                       project="beta")["id"]
    st.transition(failed, T.RUNNING); st.transition(failed, T.FAILED, "boom")
    running = st.create("Add the archive screen to Mondays", role="implementor",
                        project="beta")["id"]
    st.transition(running, T.RUNNING)
    wake = st.create("check whether the deploy finished", source="defer",
                     state=T.QUEUED, thread="C1:2.0")["id"]
    st.transition(wake, T.RUNNING)
    st.transition(wake, T.BLOCKED, "waiting")
    st.update(wake, retry_at=time.time() + 600)
    bot.survey_rows.append({"id": done, "title": "Fix the countdown", "project": "alpha",
                            "branch": f"silkworm/{done}", "base": "main", "commits": 2,
                            "landing": {"stage": "rebase", "detail": "conflict in a.py"}})
    bot.drain.start(120, why="deploy")
    r = _run_cli(bot.port, "status", "--json")
    try:
        s = json.loads(r.stdout)
    except ValueError:
        s = {}
    check("status --json is one JSON document, exit 0",
          r.returncode == 0 and s.get("ok") is True and len(r.stdout.strip().splitlines()) == 1,
          r.stdout[:300] + r.stderr)
    check("its top-level shape is the documented one",
          set(s) == {"ok", "errors", "bot", "needs_you", "tasks", "unlanded", "watching",
                     "live", "outages", "outage_open"},
          str(sorted(s)))
    ny = s.get("needs_you") or {}
    check("needs-you items are grouped by project, then state",
          [x["id"] for x in ny.get("beta", {}).get("failed", [])] == [failed]
          and rt not in json.dumps(ny), json.dumps(ny)[:300])
    check("each item carries id, title and state",
          set(ny["beta"]["failed"][0]) >= {"id", "title", "state"})
    check("queued, running and blocked tasks are listed",
          prop in [t["id"] for t in s["tasks"]["queued"]]
          and running in [t["id"] for t in s["tasks"]["running"]]
          and wake in [t["id"] for t in s["tasks"]["blocked"]], json.dumps(s["tasks"])[:300])
    ul = (s.get("unlanded") or {}).get("alpha") or [{}]
    check("unlanded branches per project carry the last landing's stage and detail",
          ul[0].get("id") == done and ul[0].get("stage") == "rebase"
          and ul[0].get("detail") == "conflict in a.py" and ul[0].get("commits") == 2,
          json.dumps(s.get("unlanded")))
    rev = s["bot"]["revision"]
    check("the running revision is set against the checkout's HEAD",
          rev["state"] == "stale" and rev["running"] == "0" * 40
          and rev["head"] == subprocess.run(["git", "-C", str(BASE), "rev-parse", "HEAD"],
                                            capture_output=True, text=True).stdout.strip(),
          json.dumps(rev))
    check("drain state is reported",
          s["bot"]["drain"].get("draining") is True
          and s["bot"]["drain"].get("reason") == "deploy", json.dumps(s["bot"]["drain"]))
    check("scheduled wake-ups are listed",
          [w["id"] for w in s["watching"]] == [wake] and 0 < s["watching"][0]["in_s"] <= 600,
          json.dumps(s["watching"]))

    bot.broken["survey"] = True
    r = _run_cli(bot.port, "status", "--json")
    s = json.loads(r.stdout or "{}")
    check("a part the bot could not answer is an error, ok false, exit 1 -- the rest kept",
          r.returncode == 1 and s.get("ok") is False
          and any(e.startswith("unlanded") for e in s.get("errors") or [])
          and s.get("unlanded") == {} and s["bot"]["online"] is True
          and wake in [w["id"] for w in s["watching"]], r.stdout[:400])
    bot.broken.clear()

    r = _run_cli(dead, "status", "--json")
    s = json.loads(r.stdout or "{}")
    check("status --json with no bot says so as JSON, exit 3",
          r.returncode == 3 and s.get("ok") is False and s["bot"]["online"] is False, r.stdout)


def _cli_status_summary(ask):
    """status_summary lifted out of the CLI, over a stub `ask`."""
    src = (BASE / "bin" / "silkworm").read_text()
    names = {"status_summary", "_row", "BotUnreachable"}
    nodes = [n for n in ast.parse(src).body
             if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names]
    ns: dict = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<cli>", "exec"), ns)
    return ns["status_summary"](ask(ns["BotUnreachable"])), ns


def test_status_json_keeps_what_it_got():
    print("\nstatus --json: one slow part is that part unasked, not the bot offline")

    def ask(unreachable):
        def call(path, payload, timeout=30):
            if path == "/status":
                return {"online": True, "revision": {"state": "current"}, "drain": {}}
            if payload.get("action") == "unmerged":
                raise unreachable("timed out")
            if payload.get("action") == "watching":
                return {"ok": True, "watching": [{"id": "tsk_w", "in_s": 5}]}
            return {"ok": True, "tasks": [{"id": "tsk_a", "title": "A", "state": "failed",
                                           "project": "p"}]}
        return call
    try:
        out, _ = _cli_status_summary(ask)
    except Exception as e:
        check("one part timing out does not throw the summary away", False, repr(e))
        return
    check("the timed-out part is named in errors and ok is false",
          out["ok"] is False and out["errors"] == ["unlanded: timed out"], str(out["errors"]))
    check("and what did answer is kept, with the bot still online",
          out["bot"]["online"] is True and out["watching"][0]["id"] == "tsk_w"
          and out["needs_you"]["p"]["failed"][0]["id"] == "tsk_a")


def test_survey_rows_carry_the_last_landing():
    print("\nan unmerged branch's row says what its last landing did")
    import branches as B
    repo = Path(tempfile.mkdtemp())
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True,
                                    text=True, check=True)
    git("init", "-q", "-b", "main")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
    git("checkout", "-q", "-b", "silkworm/tsk_land1")
    (repo / "a.txt").write_text("work\n")
    git("add", "a.txt")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "work")
    git("checkout", "-q", "main")
    rec = {"id": "tsk_land1", "title": "t", "state": "done", "project": "p",
           "scope": {"cwd": str(repo), "branch": "main"},
           "result": {"landing": {"stage": "rebase", "detail": "conflict in a.txt"}}}
    rows = B.survey([rec])
    check("the real survey finds the branch",
          len(rows) == 1 and rows[0]["branch"] == "silkworm/tsk_land1", str(rows))
    check("and carries the landing's stage and detail",
          rows and rows[0].get("landing") == {"stage": "rebase", "detail": "conflict in a.txt"},
          str(rows and rows[0].get("landing")))
    rows = B.survey([{**rec, "result": {}}])
    check("a branch never landed has empty ones, not missing",
          rows and rows[0].get("landing") == {"stage": "", "detail": ""})


def test_action_cli_documented():
    print("\nthe action commands are in the usage text and the README")
    cli = (BASE / "bin" / "silkworm").read_text()
    readme = (BASE / "README.md").read_text()
    for cmd in ("accept", "dismiss", "land", "drop", "edit", "rework", "close", "dig",
                "status --json"):
        check(f"usage text names silkworm {cmd}", f"silkworm {cmd} " in cli)
        check(f"README names silkworm {cmd}", f"silkworm {cmd} " in readme)


# --- conversation turns off the board -------------------------------------------

def _turn_records():
    """Representative records of every kind, shaped as production files them:
    {name: (record, is it a conversation turn)}."""
    return {
        "slack turn": ({"role": "assistant", "source": "slack", "driver": "inline",
                        "source_ref": "1784068538.1"}, True),
        "slack turn handed to the runner": ({"role": "assistant", "source": "slack",
                                             "driver": "queue", "source_ref": "1784068538.2"},
                                            True),
        "operator's Slack turn": ({"role": "operator", "source": "slack", "driver": "inline",
                                   "source_ref": "1784068538.3"}, True),
        "web chat turn": ({"role": "assistant", "source": "ui", "driver": "inline",
                           "source_ref": "1784068538.4"}, True),
        "operator's web chat turn": ({"role": "operator", "source": "ui",
                                      "driver": "inline", "source_ref": "1784068538.5"}, True),
        "scheduled wake-up": ({"role": "assistant", "source": "defer", "driver": "queue",
                               "source_ref": ""}, True),
        "operator sweep": ({"role": "operator", "source": "defer", "driver": "queue",
                            "source_ref": "operator-sweep"}, True),
        "a turn the model titled like work": ({"role": "assistant", "source": "slack",
                                               "title": "Implement the release button",
                                               "source_ref": "1784068538.6"}, True),
        "scoped implementor": ({"role": "implementor", "source": "scoped",
                                "driver": "queue"}, False),
        "scoped assistant": ({"role": "assistant", "source": "scoped", "driver": "queue"},
                             False),
        "reviewer": ({"role": "reviewer", "source": "review", "driver": "queue"}, False),
        "rework": ({"role": "implementor", "source": "rework", "driver": "queue"}, False),
        "proposal": ({"role": "implementor", "source": "ideation", "driver": "queue"}, False),
        "nightly ideator": ({"role": "ideator", "source": "ideation", "driver": "queue"}, False),
        "research": ({"role": "researcher", "source": "scoped", "driver": "queue"}, False),
        "research next step": ({"role": "implementor", "source": "research",
                                "driver": "queue"}, False),
        "dashboard form": ({"role": "assistant", "source": "dashboard", "driver": "queue"},
                           False),
        "old dashboard form under ui": ({"role": "assistant", "source": "ui",
                                         "driver": "queue", "source_ref": ""}, False),
        "old form implementor under ui": ({"role": "implementor", "source": "ui",
                                           "driver": "queue"}, False),
        "an implementor role on a slack source": ({"role": "implementor", "source": "slack",
                                                   "source_ref": "1784068538.7"}, False),
        "a researcher on a wake-up source": ({"role": "researcher", "source": "defer"}, False),
        "health fix": ({"role": "assistant", "source": "health", "driver": "queue"}, False),
        "github issue": ({"role": "assistant", "source": "github", "driver": "inline"}, False),
        "a failing base's item": ({"role": "assistant", "source": "base-red",
                                   "driver": "queue", "source_ref": "silkworm"}, False),
    }


def test_conversation_turn_predicate():
    import tasks as T
    print("\nwhat a conversation turn is: one predicate, from recorded fields")
    for name, (rec, want) in _turn_records().items():
        check(f"{name}: {'a turn' if want else 'work'}", T.is_turn(rec) is want, rec)
    check("no record at all is not a turn", T.is_turn({}) is False and T.is_turn(None) is False)
    turn = _turn_records()["slack turn"][0]
    work = _turn_records()["scoped implementor"][0]
    for state in T.STATES:
        shown = T.on_board(dict(turn, state=state))
        check(f"a turn {state} is {'shown' if shown else 'left to its thread'}",
              shown is (state in (T.NEEDS_INPUT, T.FAILED)))
        check(f"work {state} is always shown", T.on_board(dict(work, state=state)) is True)
        check(f"and with turns asked for, a turn {state} is too",
              T.on_board(dict(turn, state=state), turns=True) is True)
    # One definition: the nightly pass and the web thread read it, not lists
    # of their own.
    import scoping as S, webthread as W
    check("the web thread's sources are the predicate's",
          W.CONVERSATION_SOURCES is T.TURN_SOURCES)
    check("scoping.CONVERSATION_SOURCES is gone, not a second copy",
          not hasattr(S, "CONVERSATION_SOURCES"))
    for name, (rec, want) in _turn_records().items():
        if rec.get("role") not in S.MACHINERY_ROLES:
            check(f"is_work agrees for {name}", S.is_work(rec) is (not want))


def _turns_fixture():
    """The board fixture plus a conversation turn in every state that matters,
    all on alpha's thread C1:9.9. {name: id} of the turns."""
    import tasks as T
    ts, ps, ids, survey, d = _board_fixture()
    turn = lambda goal, **kw: ts.create(goal, title=goal, project="alpha", thread="C1:9.9",
                                        **kw)["id"]
    t = {}
    t["running"] = turn("hi, what's the paper book doing", role="assistant",
                        source="slack", source_ref="1784068538.1", state=T.RUNNING)
    t["queued"] = turn("and the rejects?", role="assistant", source="ui",
                       source_ref="1784068538.2", state=T.QUEUED)
    t["done"] = turn("thanks", role="assistant", source="slack",
                     source_ref="1784068538.3", state=T.DONE)
    t["wake"] = turn("look again in two hours", role="assistant", source="defer",
                     driver="queue", state=T.BLOCKED)
    ts.update(t["wake"], retry_at=time.time() + 7200)
    t["sweep"] = turn("Operator sweep", role="operator", source="defer",
                      source_ref="operator-sweep", driver="queue", state=T.DONE)
    t["asks"] = turn("release it", role="assistant", source="slack",
                     source_ref="1784068538.4", state=T.NEEDS_INPUT)
    ts.update(t["asks"], needs_user={"action": "!release alpha all", "why": "yours"})
    t["failed"] = turn("why did that fail", role="assistant", source="ui",
                       source_ref="1784068538.5", state=T.FAILED)
    return ts, ps, ids, survey, d, t


def test_conversation_turns_off_the_board():
    import board as B, tasks as T, home, digest
    print("\nconversation turns are left to their threads, unless they need you")
    ts, ps, ids, survey, _, t = _turns_fixture()
    recs = list(ts.all().values())
    hidden = {t[k] for k in ("running", "queued", "done", "wake", "sweep")}
    needy = {t["asks"], t["failed"]}

    def on(answer):
        return {c["id"]: col for col, cs in answer["columns"].items() for c in cs}

    for proj in ("alpha", ""):
        where = on(B.board(recs, proj, unmerged_rows=survey))
        check(f"board {proj or 'all'}: no turn that does not need you",
              not (hidden & set(where)), sorted(hidden & set(where)))
        check(f"board {proj or 'all'}: a turn waiting on you is in Needs you",
              all(where.get(i) == "needs" for i in needy), where)
        check(f"board {proj or 'all'}: every piece of work is still there",
              {ids["proposed"], ids["queued"], ids["running"], ids["awaiting"],
               ids["failed"], ids["landed"]} <= set(where))
        where = on(B.board(recs, proj, unmerged_rows=survey, turns=True))
        check(f"board {proj or 'all'}, turns shown: every turn is on it",
              (hidden - {t["wake"]}) | needy <= set(where)
              and where.get(t["running"]) == "running", where)
    check("and a role filter offers no role only turns carry",
          "operator" not in B.board(recs, "alpha")["roles"]
          and "operator" in B.board(recs, "alpha", turns=True)["roles"])

    ov = B.overview(ps.all(include_archived=False), recs, unmerged_rows=survey)
    alpha = next(p for p in ov["projects"] if p["slug"] == "alpha")
    check("overview: a running turn is not a running card",
          t["running"] not in {r["id"] for r in alpha["running"]}
          and ids["running"] in {r["id"] for r in alpha["running"]}, alpha["running"])
    check("overview: turns waiting on you count as needing you",
          alpha["needs"] == 4, alpha["counts"])     # awaiting, failed, asks, failed turn
    ov_all = B.overview(ps.all(include_archived=False), recs, unmerged_rows=survey, turns=True)
    alpha_all = next(p for p in ov_all["projects"] if p["slug"] == "alpha")
    check("overview, turns shown: the running turn is listed",
          t["running"] in {r["id"] for r in alpha_all["running"]})
    check("overview, turns shown: and counted",
          sum(alpha_all["counts"].values()) - sum(alpha["counts"].values()) == len(hidden))

    # The Slack board message.
    view = json.dumps(home.render(recs, now=time.time()))
    check("Slack board: no turn that does not need you",
          "the paper book" not in view and "and the rejects?" not in view
          and "look again in two hours" not in view, view[:300])
    check("Slack board: a turn waiting on you is listed",
          "release it" in view and "why did that fail" in view)

    # One predicate, so every count agrees: the dashboard's, the overview's,
    # the Slack board's, the digest's and the CLI's.
    ht, _ = _turns_route(ts, ps, survey)
    for turns in (False, True):
        counts = ht({"action": "counts", **({"turns": True} if turns else {})})["counts"]
        listed = ht({"action": "list", **({"turns": True} if turns else {})})["tasks"]
        by_state = {}
        for r in listed:
            by_state[r["state"]] = by_state.get(r["state"], 0) + 1
        check(f"counts agree with what is listed (turns={turns})", counts == by_state,
              (counts, by_state))
        want = {s: n for s, n in by_state.items()}
        check(f"and with the store's own counts (turns={turns})",
              ts.counts(turns) == want)
    alpha_list = ht({"action": "list", "project": "alpha"})["tasks"]
    per = {}
    for r in alpha_list:
        per[r["state"]] = per.get(r["state"], 0) + 1
    check("the overview counts what the project's list lists", per == alpha["counts"],
          (per, alpha["counts"]))
    attention = ht({"action": "attention"})["tasks"]
    check("needs you lists the turns waiting on you, and no other turn",
          needy <= {r["id"] for r in attention}
          and not (hidden & {r["id"] for r in attention}))
    hv = home.render(recs, now=time.time())
    head = next(b for b in hv["blocks"] if b.get("type") == "context"
                and "need you" in json.dumps(b))
    head = head["elements"][0]["text"]
    running = sum(1 for r in recs if r["state"] == T.RUNNING and T.on_board(r))
    queued = sum(1 for r in recs if r["state"] == T.QUEUED and T.on_board(r))
    check("the Slack board counts as the dashboard does",
          f"*{len(attention)}* need you" in head and f"*{running}* running" in head
          and f"*{queued}* queued" in head, head)
    sec = digest.sections(recs, time.time())
    waiting = " ".join((sec.get("alpha") or {}).get("waiting") or [])
    check("the digest counts waiting as the dashboard does",
          f"{per.get(T.NEEDS_INPUT, 0)} needs input" in waiting
          and f"{per.get(T.AWAITING_APPROVAL, 0)} awaiting approval" in waiting, waiting)
    # Where the predicate changes the digest: a turn sitting in a waiting state
    # it cannot be waiting on you in, and a turn that ran twice in the window.
    now = time.time()
    odd = dict(ts.get(t["done"]), id="tsk_oddturn", state=T.AWAITING_APPROVAL,
               title="a turn parked awaiting approval")
    twice = dict(ts.get(t["done"]), id="tsk_twice", title="a turn run twice", attempts=2,
                 events=[{"at": now - 60, "kind": T.RUNNING}])
    work_twice = dict(ts.get(ids["landed"]), id="tsk_worktwice", title="work run twice",
                      attempts=2, events=[{"at": now - 60, "kind": T.RUNNING}])
    sec2 = (digest.sections(recs + [odd, twice, work_twice], now).get("alpha") or {})
    check("the digest does not count a turn as awaiting your approval",
          f"{per.get(T.AWAITING_APPROVAL, 0)} awaiting approval"
          in " ".join(sec2.get("waiting") or []), sec2.get("waiting"))
    reran = " ".join(sec2.get("reran") or [])
    check("nor list a turn as run more than once, while work still is",
          "a turn run twice" not in reran and "work run twice" in reran, reran)

    import projects as P
    picker = {r["slug"]: r for r in P.summarise(ps.all(include_archived=False), recs,
                                                T.NEEDS_ATTENTION)}
    check("the Tasks panel's project picker counts as the list does",
          picker["alpha"]["total"] == len(alpha_list)
          and picker["alpha"]["needs"] == sum(1 for r in alpha_list
                                              if r["state"] in T.NEEDS_ATTENTION),
          (picker["alpha"], len(alpha_list)))
    only_turns = dict(ts.get(t["done"]), id="tsk_lone", project="chatty")
    lone = {r["slug"]: r for r in P.summarise([], [only_turns], T.NEEDS_ATTENTION)}
    check("and a project holding only turns is still listed, with nothing open",
          lone.get("chatty", {}).get("total") == 0 and lone["chatty"]["open"] == 0, lone)

    cli = _load_cli()
    check("silkworm tasks asks for no turns", "turns" not in cli.tasks_query([]))
    check("silkworm tasks --all asks for them",
          cli.tasks_query(["--all"]) == {"action": "attention", "turns": True})
    check("with a state too, either side of it",
          cli.tasks_query(["running", "--all"]) == cli.tasks_query(["--all", "running"])
          == {"action": "list", "state": "running", "turns": True})
    out = io.StringIO()
    cli.bot_call = lambda path, payload, timeout=10: ht(payload)
    with contextlib.redirect_stdout(out):
        rc = cli.do_tasks(["running"])
    printed = out.getvalue()
    check("silkworm tasks running lists the running work and not the turn",
          rc == 0 and ids["running"] in printed and t["running"] not in printed, printed)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.do_tasks(["running", "--all"])
    check("silkworm tasks running --all lists the turn too", t["running"] in out.getvalue())
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.do_tasks([])
    check("silkworm tasks says the same number the dashboard does",
          f"— {len(attention)} task(s)" in out.getvalue(), out.getvalue())
    # Deploy's wait and status --json must see every running turn.
    src = (BASE / "bin" / "silkworm").read_text()
    check("deploy waits on running turns too",
          '{"action": "list", "state": "running", "turns": True}' in src)
    check("status --json lists turns too",
          '"slim": True, "turns": True}' in src)


def _turns_route(ts, ps, survey):
    import board as B, tasks as T, costs, branches, worktrees, holding, roles
    ns = {"task_store": ts, "project_store": ps, "board": B, "tasks": T, "costs": costs,
          "branches": branches, "worktrees": worktrees, "holding": holding, "roles": roles,
          "projects": __import__("projects"), "scoping": __import__("scoping"),
          "unmerged_survey": lambda recs: survey,
          "tell_thread": lambda *a: None, "stop_task": lambda tid: True,
          "research": __import__("research"), "slacklinks": __import__("slacklinks"),
          "log": logging.getLogger("test"), "time": time}
    lift({"handle_tasks", "board_survey", "board_release", "_board_survey",
              "_board_releases", "_with_costs", "_spend"}, ns=ns,
         absent=TASK_ROUTES + STEP_ROUTES)
    return ns["handle_tasks"], ns


def test_conversation_turns_through_the_route():
    import tasks as T
    print("\n/tasks hides conversation turns unless asked")
    ts, ps, ids, survey, _, t = _turns_fixture()
    ht, _ = _turns_route(ts, ps, survey)
    for action in ("board", "overview"):
        plain = json.dumps(ht({"action": action, "project": "alpha"}))
        shown = json.dumps(ht({"action": action, "project": "alpha", "turns": True}))
        check(f"{action}: the running turn is off by default", t["running"] not in plain)
        check(f"{action}: and on when asked", t["running"] in shown)
    plain = {r["id"] for r in ht({"action": "list"})["tasks"]}
    shown = {r["id"] for r in ht({"action": "list", "turns": True})["tasks"]}
    check("list: turns only when asked, bar the ones that need you",
          set(t.values()) & plain == {t["asks"], t["failed"]}
          and set(t.values()) <= shown)
    route = (BASE / "bot.py").read_text()
    body = route[route.index("def handle_tasks("):route.index("def handle_projects(")]
    check("the route asks tasks.on_board, not a rule of its own",
          "tasks.on_board(t, turns)" in body and "counts(turns, shelved)" in body)


TURNS_DRIVER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const seed = process.argv[3];          // what this device's storage already holds
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
globalThis.document = {getElementById: byId, createElement: () => el("new"),
                       querySelector: () => null, querySelectorAll: () => [],
                       addEventListener() {}, body: el("body")};
globalThis.window = {addEventListener() {}, location: {search: ""}};
const store = seed === "-" ? {} : {"silkworm.showTurns": seed};
globalThis.localStorage = {getItem: k => (k in store ? store[k] : null),
                           setItem(k, v) { store[k] = String(v); }};
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
const calls = [];
const card = (id, title, state) => ({id, title, state, role: "assistant", project: "alpha",
                                     goal: title, created: 1, updated: 1});
globalThis.fetch = async (url, opts) => {
  const body = opts && opts.body ? JSON.parse(opts.body) : {};
  let out = {ok: true};
  if (url.startsWith("/api/sessions")) out = {bot_online: true, sessions: [], slack: {}};
  else if (url.startsWith("/api/stats")) out = {total_cost: 0, cache_rate: null, threads: 0, days: [], models: []};
  else if (url.startsWith("/api/projects")) out = {ok: true, projects: []};
  else if (url.startsWith("/api/tasks")) {
    calls.push(body);
    if (body.action === "board") out = {ok: true, columns: {
        backlog: [], review: [], needs: [], done: [],
        running: [card("tsk_work", "Real work", "running")].concat(
          body.turns ? [card("tsk_turn", "Just a chat turn", "running")] : [])},
      counts: {}, roles: ["assistant"]};
    else if (body.action === "overview") out = {ok: true, projects: [], unfiled: null, archived: []};
    else if (body.action === "counts") out = {ok: true, counts: body.turns ? {failed: 2} : {failed: 1}};
    else if (body.action === "list" || body.action === "attention") out = {ok: true, tasks: [], counts: {}};
    else if (body.action === "roles") out = {ok: true, roles: []};
  }
  return {json: async () => out, text: async () => ""};
};
(0, eval)(src + `
;globalThis.__t = {setBoardProject, toggleBoard, setShowTurns, refreshTaskBadge,
                   get on() { return showTurns; }};`);
const tick = async () => { for (let i = 0; i < 4; i++) await new Promise(r => setImmediate(r)); };
(async () => {
  const P = globalThis.__t, out = {};
  out.startsOn = P.on;
  byId("boardmodal").style.display = "none";
  P.toggleBoard(); await tick();
  out.overviewCall = calls.filter(c => c.action === "overview").slice(-1)[0];
  P.setBoardProject("alpha"); await tick();
  out.boxBefore = byId("bturns").checked;
  out.boardCallBefore = calls.filter(c => c.action === "board").slice(-1)[0];
  out.boardBefore = byId("bboard").innerHTML;
  // Ticking the box: what the checkbox's onchange calls.
  byId("bturns").checked = true;
  P.setShowTurns(byId("bturns").checked); await tick();
  out.stored = store["silkworm.showTurns"];
  out.boardCallAfter = calls.filter(c => c.action === "board").slice(-1)[0];
  out.countsCallAfter = calls.filter(c => c.action === "counts").slice(-1)[0];
  out.boardAfter = byId("bboard").innerHTML;
  out.badgeAfter = byId("taskbadge").textContent;
  P.setShowTurns(false); await tick();
  out.storedOff = store["silkworm.showTurns"];
  out.boxOff = byId("bturns").checked;
  out.boardOff = byId("bboard").innerHTML;
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_show_conversation_turns_toggle():
    import re
    sys.argv = ["x"]
    import visualizer as V
    print("\nthe board's 'Show conversation turns' toggle, in the page's own javascript")
    box = re.search(r'<input type="checkbox" id="bturns"[^>]*>', V.PAGE)
    check("the board has the toggle", bool(box) and "Show conversation turns" in V.PAGE)
    check("and it is off in the markup", box and "checked" not in box.group(0).replace(
        "this.checked", ""), box and box.group(0))
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    node = shutil.which("node")
    if not node:
        print("  … node not installed — cannot run the page's own javascript")
        return
    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    (d / "drive.js").write_text(TURNS_DRIVER)

    def run(seed):
        p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js"), seed],
                           capture_output=True, text=True, timeout=60)
        check(f"the page runs (storage {seed!r})", p.returncode == 0, p.stderr[-800:])
        return json.loads(p.stdout or "{}")

    fresh = run("-")
    check("off by default on a new device", fresh.get("startsOn") is False)
    check("the overview does not ask for turns", "turns" not in (fresh.get("overviewCall") or {"turns": 1}))
    check("nor the board", "turns" not in (fresh.get("boardCallBefore") or {"turns": 1}),
          fresh.get("boardCallBefore"))
    check("the box shows unticked", fresh.get("boxBefore") is False)
    check("the board shows the work and not the turn",
          "Real work" in fresh.get("boardBefore", "")
          and "Just a chat turn" not in fresh.get("boardBefore", ""))
    check("ticking it is remembered on this device", fresh.get("stored") == "1")
    check("and asks the board for turns", (fresh.get("boardCallAfter") or {}).get("turns") is True)
    check("and the badge's counts too", (fresh.get("countsCallAfter") or {}).get("turns") is True)
    check("the turn is drawn", "Just a chat turn" in fresh.get("boardAfter", ""))
    check("unticking is remembered too, and the turn goes",
          fresh.get("storedOff") == "0" and fresh.get("boxOff") is False
          and "Just a chat turn" not in fresh.get("boardOff", ""))
    again = run("1")
    check("a device that ticked it starts with it on", again.get("startsOn") is True)
    check("asks for turns from the first draw",
          (again.get("overviewCall") or {}).get("turns") is True
          and (again.get("boardCallBefore") or {}).get("turns") is True)
    check("and shows the box ticked", again.get("boxBefore") is True)
    off = run("0")
    check("a device that unticked it starts with it off", off.get("startsOn") is False)


TRADER_ERRPATH_DRIVER = r"""// Draws every Trader tab from what trader's own scripts/trader_status.py
// printed, and from that document bent down each error path the script has:
// a missed slot, each section absent or _unknown, a stopped or unfindable
// daemon, an unreadable plist, a git failure, an unused gate, no C1 report,
// odd launchd schedules, an option position, and strings that would be markup.
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const doc = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
function el(id) {
  return {id, textContent: "", innerHTML: "", style: {}, dataset: {}, className: "", attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; },
    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
    appendChild(c) { return c; }, addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; }};
}
const els = {};
globalThis.document = {getElementById: id => els[id] || (els[id] = el(id)), createElement: () => el("new"), hidden: false,
  querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, body: el("body")};
globalThis.window = {addEventListener() {}, location: {search: ""}, innerWidth: 1280};
globalThis.setInterval = () => 0; globalThis.setTimeout = () => 0;
process.on("unhandledRejection", e => { console.error(e); process.exit(1); });
globalThis.fetch = async url => {
  let out = {ok: true};
  if (url.startsWith("/api/sessions")) out = {sessions: [], slack: {}};
  if (url.startsWith("/api/stats")) out = {total_cost: 0, cache_rate: null, threads: 0, days: [], models: []};
  return {status: 200, headers: {get: () => null}, json: async () => out};
};
(0, eval)(src + `;globalThis.__t = {traderHtml};`);
const T = globalThis.__t, now = Date.parse(doc.generated_at) / 1000;
const clone = x => JSON.parse(JSON.stringify(x));
const TABS = ["summary", "sleeves", "activity", "plumbing", "shadow"];
const out = {live: {}, skipped: "", without: {}, escaped: {}};
for (const t of TABS) out.live[t] = T.traderHtml({ok: true, doc}, now, t);
const sk = clone(doc);
sk.sections.dispatches.missed_slots = [{session: "2026-11-13", timing: "close",
  slot: "2026-11-13T16:00:00-05:00", logged_as: null}];
out.skipped = T.traderHtml({ok: true, doc: sk}, now, "activity");
// A section trader_status.py can't build is its _unknown shape, never an
// absent key; both are drawn.
out.unknownSec = {};
for (const k of Object.keys(doc.sections)) {
  const d = clone(doc); delete d.sections[k];
  out.without[k] = TABS.map(t => T.traderHtml({ok: true, doc: d}, now, t)).join("\n");
  const u = clone(doc);
  u.sections[k] = {status: "unknown", reason: k + " could not read: ImportError boom", source: null, as_of: null};
  // Only the tab that owns the section: the summary repeats every reason.
  const own = {summary: "summary", sleeves: "sleeves", gate: "sleeves", universe: "sleeves", dispatches: "activity",
    fills: "activity", jobs: "plumbing", freshness: "plumbing", health: "plumbing", host: "plumbing", env: "plumbing",
    shadow: "shadow"}[k] || "summary";
  out.unknownSec[k] = T.traderHtml({ok: true, doc: u}, now, own);
}
// The error paths the script has (the review's cases).
const V = {};
const vary = (tab, f) => { const d = clone(doc); f(d.sections); return T.traderHtml({ok: true, doc: d}, now, tab); };
const job = (S, name) => S.jobs.jobs.find(j => j.label.endsWith("." + name));
V.daemonDown = vary("plumbing", S => { job(S, "papertrade").pid = null; job(S, "papertrade").status = "bad";
  job(S, "papertrade").reason = "KeepAlive job has no running PID"; S.host.daemon = {pid: null, started: null, checkout: null, error: null}; });
V.lookupFailed = vary("plumbing", S => { S.host.daemon = {pid: null, started: null, checkout: null, error: "RuntimeError: launchctl exited 1"}; });
V.pidAndError = vary("plumbing", S => { S.host.daemon.error = "RuntimeError: ps exited 1"; });
V.plistBad = vary("plumbing", S => { S.jobs.jobs.push({plist: "/x/com.traderjoe.zzz.plist", label: "com.traderjoe.zzz",
  status: "unknown", reason: "plist unreadable: ValueError"}); });
V.failingNull = vary("plumbing", S => { S.health.last_scheduled_run.failing = null; });
V.gitError = vary("plumbing", S => { S.host.running_vs_disk = {head: "abc1234 2026-11-19T10:00:00-08:00 x",
  git_error: "RuntimeError: git exited 128", env_newer_than_daemon: false}; });
V.noGateSleeve = vary("sleeves", S => { S.gate = {status: "ok", reason: "no deployed sleeve uses the credit gate", source: null, as_of: null}; });
V.noReport = vary("shadow", S => { delete S.shadow.vm_multi_asset; });
V.schedules = vary("plumbing", S => { const j = S.jobs.jobs;
  j[0].schedule = {StartInterval: 30}; j[1].schedule = {Minute: 0}; j[2].schedule = [{Hour: 9}, {Weekday: 1, Hour: 10}];
  j[3].schedule = [{Weekday: 1, Hour: 8, Minute: 0}, {Weekday: 5, Hour: 16, Minute: 0}]; j[4].schedule = {StartInterval: 90}; });
V.option = vary("sleeves", S => { S.sleeves.sleeves[1].positions = [{symbol: "SPY 2026-12-18 600C", quantity: 2, avg_cost: 5.0,
  price: 6.0, market_value: 1200.0}]; });
out.variants = V;
const evil = '<img src=x onerror=alert(1)>"', bad = clone(doc), S = bad.sections;
S.sleeves.sleeves[1].positions[0].symbol = evil; S.universe.loaded.mode = evil; S.fills.recent[0].symbol = evil;
S.jobs.jobs[0].label = evil; S.health.checks[0].message = evil; S.env.values.BROKER = evil;
S.host.gateway.detail = evil; S.host.running_vs_disk.commits_since_start[0] = evil; S.dispatches.recent[0].timing = evil;
S.freshness.bars.message = evil;
for (const t of TABS) out.escaped[t] = T.traderHtml({ok: true, doc: bad}, now, t);
console.log(JSON.stringify(out));
"""


def test_trader_tabs_follow_trader_status_down_its_error_paths():
    import re
    sys.argv = ["x"]
    import visualizer as V
    print("\nthe Trader tabs say what trader_status.py says, on its error paths too")
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    node = shutil.which("node")
    if not node:
        print("  … node not installed — cannot run the page's own javascript")
        return
    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    (d / "drive.js").write_text(TRADER_ERRPATH_DRIVER)
    p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js"), str(TRADER_REAL_FIXTURE)],
                       capture_output=True, text=True, timeout=60, env=dict(os.environ, TZ="America/Los_Angeles"))
    if p.returncode != 0:
        check("the trader tabs' javascript runs down trader_status.py's error paths", False, p.stderr.strip()[-800:])
        return
    o = json.loads(p.stdout)
    words = lambda h: re.sub(r"\s+", " ", re.sub(r"<title>.*?</title>|<[^>]+>", " ", h))
    L = {k: words(v) for k, v in o["live"].items()}
    va = {k: words(v) for k, v in o["variants"].items()}
    # NaN only ever in the words "NaN date(s)" / "NaN row(s)" / "NaN rows on".
    stray = lambda h: re.search(r"NaN(?! (dates?|rows?)\b)", words(h))
    for t, h in o["live"].items():
        check(f"the {t} tab prints no undefined, null or stray NaN", "undefined" not in h and "null" not in h and not stray(h), t)
    for k, h in o["without"].items():
        check(f"without its {k} section every tab still draws, with no undefined, null or NaN",
              "undefined" not in h and "null" not in h and not stray(h) and h.count("tr-") > 50, k)
    for k, h in o["unknownSec"].items():
        check(f"an unknown {k} section shows its own reason on its own tab",
              f"{k} could not read: ImportError boom" in h and "undefined" not in h and "null" not in h and not stray(h), k)
    pl, sh, a = L["plumbing"], L["shadow"], L["activity"]
    check("dispatches run newest first, by real time across offsets",
          a.index("Nov 19 06:33 open") < a.index("Nov 18 13:00 close") < a.index("Nov 16 13:00 close"))
    sk = words(o["skipped"])
    check("a missed slot keeps its own time and sits in time order",
          "Nov 13 13:00 close ✕ SKIPPED no dispatch heartbeat for this NYSE session slot" in sk
          and sk.index("Nov 16 13:00 close") < sk.index("Nov 13 13:00 close"), sk[:400])
    check("a KeepAlive daemon with no PID is the script's bad, not a green exit 0",
          "papertrade ! KeepAlive job has no running PID KeepAlive Thu Nov 19 06:33 ✕ exit 0 n/a" in va["daemonDown"]
          and "daemon ✕ NOT RUNNING" in va["daemonDown"])
    check("a failed daemon lookup is unknown, not 'not running' or 'not reported'",
          "daemon ? lookup failed RuntimeError: launchctl exited 1" in va["lookupFailed"]
          and "NOT RUNNING" not in va["lookupFailed"] and "daemon ? not reported" not in va["lookupFailed"])
    check("a PID with a lookup error keeps the error",
          "PID 84984, up since Sun Nov 15 08:00 ? lookup failed RuntimeError: ps exited 1" in va["pidAndError"])
    check("an unreadable plist shows the script's reason", "zzz ! plist unreadable: ValueError" in va["plistBad"])
    check("a git error is shown, not hidden behind HEAD", "? git failed RuntimeError: git exited 128" in va["gitError"])
    check("a gate no sleeve uses says so, with no 'state unknown' beside an OK",
          "✓ OK no deployed sleeve uses the credit gate" in va["noGateSleeve"] and "state unknown" not in va["noGateSleeve"])
    check("interval and KeepAlive jobs can't miss a slot: n/a, not unknown",
          "gatewaywatch every 2m Wed Nov 18 23:46 ✓ exit 0 n/a" in pl and "papertrade KeepAlive Thu Nov 19 06:33 ✓ running n/a" in pl
          and "? unknown" not in pl[pl.index("Launchd jobs"):pl.index("Health checks")])
    check("a job's exit badge is the script's own status (exit 1 is its warn)",
          "paperhealth ! last exited 1 wkdays 06:15 12:50 13:15 Thu Nov 19 06:15 ! exit 1 no" in pl)
    check("launchd schedules in words, as launchd reads them",
          "every 30s" in va["schedules"] and "every 90s" in va["schedules"] and "gatewaywatch hourly at :00" in va["schedules"]
          and "daily 09:**; Mon 10:**" in va["schedules"] and "Mon 08:00; Fri 16:00" in va["schedules"]
          and "newsfwd wkdays 13:50" in pl and "every 0m" not in va["schedules"])
    check("the equity snapshot has its own freshness row, and says it is behind",
          "equity snapshot — — 2026-11-16 ✕ behind" in pl)
    check("two sessions behind is within trader's SHADOW_MAX_SESSIONS_BEHIND",
          "Blend shadow ✓ 2 session(s) behind" in sh)
    check("with no C1 report the recorded curve's freshness still shows",
          "vm_multi_asset_recorded newest 2026-11-13, 3 sessions behind" in va["noReport"])
    check("an option's uPnL carries the multiplier its market value shows",
          "SPY 2026-12-18 600C 2 5.00 6.00 +200" in va["option"])
    n = sum(h.count("&lt;img src=x onerror=alert(1)&gt;&quot;") for h in o["escaped"].values())
    check("strings in trader_status.py's own fields are escaped, never markup",
          all("<img" not in h for h in o["escaped"].values()) and n >= 10, n)


# A thread's box, laid out: main scrolls a column of the sticky header, the
# messages (#tmsgs, each an element of a given height), the live end (#tlive)
# and the sticky composer. Rects are measured from it, scrollTop is clamped
# as a browser clamps it, a scroll event follows a scroll (delivered when a
# check says so), and a ResizeObserver fires when layout() is called -- as a
# browser does after any size change.
SCROLL_HARNESS = r"""
const HEAD = 50, COMPOSER = 70;
let vvh = 800, liveH = 0, pendingScroll = false;
const listeners = {}, roCallbacks = [];
function plain(id) {
  const cls = new Set();
  const e = {id, style: {setProperty(k, v) { if (k === "--vvh") vvh = parseFloat(v); }},
    dataset: {}, value: "", children: [], _inner: "", attrs: {},
    classList: {add: c => cls.add(c), remove: c => cls.delete(c), contains: c => cls.has(c),
                toggle: (c, on) => { if (on === undefined ? !cls.has(c) : on) cls.add(c); else cls.delete(c); }},
    addEventListener(){}, querySelectorAll: () => [], querySelector: () => null,
    insertAdjacentHTML(){}, appendChild(c) { this.children.push(c); c.parent = this; },
    setAttribute(k, v) { this.attrs[k] = v; }, focus(){}, blur(){}, scrollIntoView(){}};
  Object.defineProperty(e, "innerHTML", {get() { return this._inner; },
    set(v) { this._inner = v; if (this.id !== "tmsgs") this.children.length = 0; }});
  return e;
}
const els = {};
const byId = id => els[id] || (els[id] = plain(id));
const tmsgs = byId("tmsgs");
// The phone's thread header: sticky, over the top HEAD px of what is shown.
byId("chathead").getBoundingClientRect = () => ({top: 0, bottom: HEAD});
// A message: only its height matters; it is where the column puts it.
function item(h) {
  const it = {h, isConnected: true, classList: plain("").classList,
    getBoundingClientRect() {
      const i = tmsgs.children.indexOf(this);
      let y = HEAD;
      for (let k = 0; k < i; k++) y += tmsgs.children[k].h;
      return {top: y - main.scrollTop, bottom: y + this.h - main.scrollTop};
    }};
  return it;
}
const content = () => HEAD + tmsgs.children.reduce((a, c) => a + c.h, 0) + liveH + COMPOSER;
const main = plain("__main");
let top_ = 0;
Object.defineProperty(main, "clientHeight", {get: () => vvh});
Object.defineProperty(main, "scrollHeight", {get: () => Math.max(content(), vvh), configurable: true});
Object.defineProperty(main, "scrollTop", {get: () => top_, set(v) {
  const t = Math.max(0, Math.min(v, main.scrollHeight - main.clientHeight));
  if (t !== top_) { top_ = t; pendingScroll = true; }
}});
main.getBoundingClientRect = () => ({top: 0, bottom: vvh});
main.addEventListener = (k, f) => { listeners[k] = f; };
const flush = () => { if (pendingScroll) { pendingScroll = false; listeners.scroll(); } };
const layout = () => { for (const f of roCallbacks) f([]); flush(); };
if (!__NORO__) globalThis.ResizeObserver = class { constructor(f) { roCallbacks.push(f); } observe(){} };
const bodyCls = new Set();
globalThis.document = {getElementById: byId, addEventListener(){}, querySelectorAll: () => [],
  querySelector: s => s === "main" ? main : null, createElement: () => plain(""), hidden: false,
  documentElement: plain("html"),
  body: {classList: {add: c => bodyCls.add(c), remove: c => bodyCls.delete(c), contains: c => bodyCls.has(c),
                     toggle: (c, on) => { if (on === undefined ? !bodyCls.has(c) : on) bodyCls.add(c); else bodyCls.delete(c); }}}};
const vvListeners = {};
const vv = {height: 800, scale: 1, offsetTop: 0, addEventListener: (k, f) => { vvListeners[k] = f; }};
globalThis.history = {state: null, replaceState(){}, pushState(){}, back(){}};
globalThis.window = {addEventListener(){}, history: globalThis.history, location: {search: ""},
                     innerHeight: 800, visualViewport: vv, scrollTo(){}};
globalThis.matchMedia = q => ({matches: q === "(max-width: 700px)"});
Object.defineProperty(globalThis, "navigator", {value: {}, configurable: true});
globalThis.localStorage = {getItem: () => null, setItem(){}};
globalThis.setInterval = () => 0; globalThis.setTimeout = () => 0;
globalThis.requestAnimationFrame = () => 0;
globalThis.fetch = () => new Promise(() => {});
globalThis.getComputedStyle = () => ({});
(0, eval)(__JS__ + `
;globalThis.__p = {keepEnd, toEnd, atEnd, onScroll, isPinned: () => pinned, setActive: a => { active = a; }};`);
const P = globalThis.__p, out = {};
// No thread open: main holds only the stats, taller than the window, read
// from the top; the panel loading or redrawing (the observer firing) leaves
// them there -- there is no end to follow.
{
  let stats = 1600;
  const drawn = tmsgs.children.length;
  tmsgs.children.length = 0;
  const sh = Object.getOwnPropertyDescriptor(main, "scrollHeight");
  Object.defineProperty(main, "scrollHeight", {get: () => Math.max(stats, vvh), configurable: true});
  main.scrollTop = 0; flush();
  const was = P.isPinned();
  layout(); stats = 2400; layout();
  out.noThread = {top: main.scrollTop, pinnedBefore: was};
  Object.defineProperty(main, "scrollHeight", sh);
  tmsgs.children.length = drawn;
}
P.setActive({key: "T", kind: "thread"});                // a thread open from here on
// The keyboard, as iOS opens it: the visual viewport shrinks and says so.
function keyboard(open) { vv.height = open ? 460 : 800; vvListeners.resize(); layout(); }
// Thirty messages; read at the end, or scrolled up to the middle.
function fresh(mode) {
  if (vv.height !== 800) keyboard(false);
  tmsgs.children.length = 0; liveH = 0;
  for (let i = 0; i < 30; i++) tmsgs.children.push(item(80 + (i % 4) * 30));
  P.keepEnd(() => {}, true); layout();                  // opened: at the end
  if (mode === "up") { main.scrollTop = 900; flush(); }
}
const gap = () => main.scrollHeight - main.scrollTop - main.clientHeight;
const last = () => tmsgs.children[tmsgs.children.length - 1];
// The last message ends above the composer, inside what is shown.
const latestShown = () => last().getBoundingClientRect().bottom <= vvh - COMPOSER + 1;
// Where the message at the top of what is shown is, to see it did not move:
// by its place in the thread, so a redraw's new element for it is measured.
function topOf() {
  const i = tmsgs.children.findIndex(c => c.getBoundingClientRect().bottom > 0);
  return {i, at: tmsgs.children[i].getBoundingClientRect().top};
}
const moved = t => Math.round(tmsgs.children[t.i].getBoundingClientRect().top - t.at);
// The message just above the one on screen: what changes there moves the reader.
const above = () => tmsgs.children[Math.max(0, topOf().i - 1)];
const pill = () => byId("jumppill").classList.contains("show");
function trial(name, mode, act) {
  fresh(mode);
  const t = topOf(), top0 = main.scrollTop;
  act();
  out[name + ":" + mode] = mode === "bottom"
    ? {gap: gap(), latestShown: latestShown(), pinned: P.isPinned()}
    : {moved: moved(t), top0, top: main.scrollTop, pinned: P.isPinned(), pill: pill()};
}
for (const mode of ["bottom", "up"]) {
  // The composer focused: the keyboard opens.
  trial("keyboard-open", mode, () => keyboard(true));
  // ...and a scroll event the browser sends once the box has shrunk, before
  // the page has heard of the keyboard, is not the reader scrolling up.
  trial("keyboard-then-scroll-event", mode, () => {
    vvh = 460; pendingScroll = true; flush();
    vv.height = 460; vvListeners.resize(); layout();
  });
  trial("keyboard-close", mode, () => { keyboard(true); keyboard(false); });
  // A poll brings new messages.
  trial("new-messages", mode, () => P.keepEnd(() => { tmsgs.children.push(item(140), item(90)); }));
  // A turn's tools line rewritten in place (more calls counted, a line
  // wrapping): one above what is read, and the last.
  trial("tool-line-updates", mode, () => P.keepEnd(() => { above().h += 40; last().h += 40; }));
  // A tools line unfolding (one opened before, the live line's chain growing):
  // a CSS transition after the draw, seen by the observer.
  trial("tool-line-expands", mode, () => { above().h += 220; layout(); });
  // A thumbnail (or a font) arriving after the draw, above the reader.
  trial("media-loads-late", mode, () => { tmsgs.children[2].h += 120; tmsgs.children[16].h += 120; layout(); });
  // A transcript read draws the live end inside its own draw: what grows
  // above the reader in the first part is still put right.
  trial("poll-grows-above", mode, () => P.keepEnd(() => { above().h += 100; P.keepEnd(() => { liveH = 30; }); }));
  // The working indicator under the last message, appearing and going.
  trial("working-appears", mode, () => P.keepEnd(() => { liveH = 60; }));
  trial("working-disappears", mode, () => { P.keepEnd(() => { liveH = 60; }); P.keepEnd(() => { liveH = 0; }); layout(); });
  // The transcript drawn again from the top (another session, an error read
  // recovered): new elements, the same heights. A guard, not a fix -- it held
  // before too: never thrown to the top.
  trial("redrawn-from-top", mode, () => P.keepEnd(() => {
    const hs = tmsgs.children.map(c => c.h);
    for (const c of tmsgs.children) c.isConnected = false;
    tmsgs.children.length = 0;
    for (const h of hs) tmsgs.children.push(item(h));
  }));
}
// A message wholly under the sticky header grows (a tools line redrawn): what
// is read below the header -- the first message showing past it -- stays put.
fresh("up");
{
  const k = 10, it = tmsgs.children[k];
  main.scrollTop += it.getBoundingClientRect().bottom - (HEAD - 10); flush();
  const shown = () => tmsgs.children.find(c => c.getBoundingClientRect().bottom > HEAD);
  const was = shown(), at = was.getBoundingClientRect().top;
  P.keepEnd(() => { it.h += 100; }); layout();
  out.underHeader = {sameMessage: shown() === was, moved: Math.round(was.getBoundingClientRect().top - at)};
}
// Read above the messages (a laptop's stats, the thread's details): what is
// above them growing leaves the reader at what they were reading.
fresh("up");
let stats = 200;                                         // the stats, above the first message
tmsgs.children[0].getBoundingClientRect = function () {
  return {top: HEAD + stats - main.scrollTop, bottom: HEAD + stats + this.h - main.scrollTop}; };
main.scrollTop = 0; flush();
P.keepEnd(() => { stats = 500; }); layout();             // a draw grows them
out.aboveMessages = {top: main.scrollTop};
// Another thread opened: at its end, whatever was read where in the last one.
fresh("up");
P.keepEnd(() => { tmsgs.children.length = 0; for (let i = 0; i < 12; i++) tmsgs.children.push(item(100)); }, true);
layout();
out.switched = {gap: gap(), latestShown: latestShown(), pinned: P.isPinned(), pill: pill()};
process.stdout.write(JSON.stringify(out));
"""


def scroll_harness(js: str, resize_observer: bool = True) -> dict:
    import shutil
    import subprocess as _sp
    node = shutil.which("node")
    if not node:
        return None
    src = SCROLL_HARNESS.replace("__NORO__", "false" if resize_observer else "true")
    r = _sp.run([node, "-e", src.replace("__JS__", json.dumps(js))],
                capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise AssertionError("scroll harness: " + r.stderr[-800:])
    return json.loads(r.stdout)


def test_dashshots_scroll_probe_injects():
    import importlib.machinery
    import importlib.util
    import re
    import shutil
    import subprocess as _sp
    import tempfile
    print("\nbin/dashshots --scroll-probe: the probe goes into a copy's page, ahead of its script")
    root = Path(__file__).resolve().parent.parent
    loader = importlib.machinery.SourceFileLoader("dashshots_t", str(root / "bin" / "dashshots"))
    spec = importlib.util.spec_from_loader("dashshots_t", loader)
    D = importlib.util.module_from_spec(spec)
    loader.exec_module(D)
    with tempfile.TemporaryDirectory() as td:
        viz = Path(td) / "visualizer.py"
        shutil.copy2(root / "visualizer.py", viz)
        D.inject_probe(viz, 4321)
        page = viz.read_text()
    probe, own = page.find("const BOT = \"http://127.0.0.1:4321\""), page.find("function setHTML(")
    check("the probe is in the copy's page, before the page's own script, its bot filled in",
          0 < probe < own and "__BOT__" not in page[probe - 200:own])
    check("the real visualizer.py is untouched", "scrollprobe" not in (root / "visualizer.py").read_text())
    node = shutil.which("node")
    if node:
        r = _sp.run([node, "--check", str(root / "bin" / "scrollprobe.js")], capture_output=True, text=True)
        check("bin/scrollprobe.js parses", r.returncode == 0, r.stderr[-400:])
    check("the probe answers only to ?probe=, so a copy without it behaves as the dashboard",
          re.search(r"if \(!/\[\?&\]probe=/\.test\(location\.search\)\) return;",
                    (root / "bin" / "scrollprobe.js").read_text()) is not None)


def test_viz_chat_keeps_its_place():
    import re
    import visualizer as V
    print("\nthe chat keeps the reader's place: at the end it stays at the end; read further up it stays put")
    css = re.search(r"<style>(.*?)</style>", V.PAGE, re.S).group(1)
    check("main does its own anchoring, so Chrome's cannot correct a shift twice",
          re.search(r"\n  main \{[^}]*overflow-anchor: none;", css) is not None)
    check("thumbnails reserve their square before they load",
          re.search(r"\.athumb \{ width: 84px; height: 84px;", css) is not None
          and 'width="96" height="96"' in V.PAGE)
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    o = scroll_harness(js)
    if o is None:
        print("  … node not installed — the chat's scrolling not exercised")
        return
    triggers = ("keyboard-open", "keyboard-then-scroll-event", "keyboard-close", "new-messages",
                "poll-grows-above", "tool-line-updates", "tool-line-expands", "media-loads-late", "working-appears",
                "working-disappears", "redrawn-from-top")
    for t in triggers:
        b, u = o[t + ":bottom"], o[t + ":up"]
        check(f"{t}, read at the end: the latest message stays shown above the composer",
              b["gap"] <= 1 and b["latestShown"] and b["pinned"], b)
        check(f"{t}, read further up: the message on screen does not move",
              abs(u["moved"]) <= 1 and not u["pinned"] and u["top"] > 0, u)
    check("new messages while read further up: the pill says so",
          o["new-messages:up"]["pill"], o["new-messages:up"])
    check("another thread opens at its end, the pill gone",
          o["switched"]["gap"] <= 1 and o["switched"]["latestShown"] and not o["switched"]["pill"],
          o["switched"])
    check("a message hidden under the phone's sticky header growing moves nothing shown",
          o["underHeader"]["sameMessage"] and abs(o["underHeader"]["moved"]) <= 1, o["underHeader"])
    check("read above the messages (the stats), what grows there does not scroll the reader",
          o["aboveMessages"]["top"] == 0, o["aboveMessages"])
    check("no thread open: the stats panel loading or redrawing does not scroll main to its bottom",
          o["noThread"]["pinnedBefore"] and o["noThread"]["top"] == 0, o["noThread"])
    # A WebKit without ResizeObserver (before iOS 13.4): the keyboard is still
    # followed, from the visual viewport's own resize.
    n = scroll_harness(js, resize_observer=False)
    check("with no ResizeObserver, the keyboard opening still keeps the latest message shown",
          n["keyboard-open:bottom"]["gap"] <= 1 and n["keyboard-open:bottom"]["latestShown"]
          and abs(n["keyboard-open:up"]["moved"]) <= 1, (n["keyboard-open:bottom"], n["keyboard-open:up"]))


def _cli_file(cli, argv, w, key="D7:7.0", role="operator"):
    """do_file_task(argv) against the shared route; (exit code, output, sent)."""
    sent = []
    cli.bot_call = lambda path, payload, timeout=3: (sent.append((path, payload)) or
                                                     w.file(dict(payload, key=key)))
    env = {"SILKWORM_THREAD": key, "SILKWORM_ROLE": role, "SILKWORM_TASK_ID": ""}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                cli.do_file_task(argv)
                code = 0
            except SystemExit as e:
                code = e.code
            except Exception as e:  # a crash is a failure to check, not to raise
                code = f"raised {type(e).__name__}"
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return code, buf.getvalue(), sent


def test_task_cli_takes_by_and_refuses_unknown_options():
    print("\nfiling: `silkworm task --by operator` files the goal, not the flag")
    cli = _load_cli()
    goal = ("Executing the operator's recommendation: refile the trader status "
            "emitter under trader")
    # The shape of the 2026-11-19 04:10 filing that became tsk_8b1c77e711.
    w = _requested_world()
    code, out, sent = _cli_file(cli, ["--project", "beta", "--role", "implementor",
                                      "--propose", "--by", "operator", goal], w)
    check("--by is taken as an option", code == 0 and sent
          and sent[0][1]["goal"] == goal, (code, out, sent))
    filed = [t for t in w.ts.all().values() if t.get("goal") == goal]
    check("and the stored title and goal start with the work", filed
          and not filed[0]["title"].startswith("--")
          and filed[0]["goal"] == goal, filed)

    w = _requested_world()
    before = dict(w.ts.all())
    code, out, sent = _cli_file(cli, ["--projct", "alpha", goal], w)
    check("an unknown option is refused by name, exit 2", code == 2
          and "unknown option --projct" in out and not sent, (code, out))
    code, out, sent = _cli_file(cli, [goal, "--notes"], w)
    check("an option missing its value is refused, not filed as goal text",
          code == 2 and "--notes needs a value" in out and not sent, (code, out))
    check("nothing filed by either", w.ts.all() == before)
    code, out, sent = _cli_file(cli, ["--propose", "--", "--dry-run mode for the "
                                      "importer, so it reports without writing"], w)
    check("after --, words that look like options go to the route as goal",
          sent and sent[0][1]["goal"].startswith("--dry-run"), (code, out))
    check("where the shared check refuses a goal that opens with an option",
          code == 1 and "an option rather than the work" in out, (code, out))


def test_a_goal_starting_with_an_option_is_refused_at_every_door():
    import scoping as S
    print("\nfiling: a goal that starts with a leaked --option is refused")
    for goal in ("--by operator Executing the operator's recommendation on tasks pending",
                 "--notes User said yes --by operator do the thing now please"):
        err = S.validate(goal)
        check(f"{goal[:30]!r} refused as an option", "an option rather than the work" in err, err)
    check("a single-dash list item is still work",
          S.validate("- sometimes when cropping an image, after saving it doesn't save") == "")
    check("an option later in the goal is fine",
          S.validate("Run git branch --show-current and report it back") == "")
    w = _requested_world()
    before = dict(w.ts.all())
    out = w.file({"key": "D7:7.0", "goal": "--by operator Make the plain-yes parser lenient",
                  "project": "alpha"})
    check("the shared route refuses it and files nothing",
          not out["ok"] and "an option" in out.get("error", "") and w.ts.all() == before, out)


def test_strip_leaked_flags_cleans_the_shapes_the_leak_made():
    import tasks as T
    from tasks import TaskStore
    print("\ntasks: a leaked --by is taken off stored titles and goals, once")
    path = Path(tempfile.mkdtemp()) / "t.json"
    ts = TaskStore(path)
    work = ts.create("--by operator Executing the operator's recommendation on tasks "
                     "pending the owner's input: refile it", driver="queue", project="trader")
    ts.transition(work["id"], T.RUNNING, "claimed")
    ts.transition(work["id"], T.DONE, "done")
    review = ts.create("Goal that was given:\n--by operator Executing the operator's "
                       "recommendation", title="Review: --by operator Executing the op",
                       driver="queue", role="reviewer")
    follow = ts.create("A review of tsk_8b1c77e711 (--by operator Executing the op…) "
                       "found this", title="From review: the goal names a missing file",
                       driver="queue")
    nested = ts.create("Goal that was given:\nA review of tsk_06b789e3ff (--by operator Keep "
                       "an ops plan open) found this", driver="queue", role="reviewer")
    notes = ts.create("--by operator --notes User 2026-10-26: 'You should be able to run "
                      "these yourself'. Give the operator a way", driver="queue")
    keep = ts.create("Run pwd and git branch --show-current, then sleep 30", driver="queue")
    keep2 = ts.create("- sometimes when cropping an image it doesn't save", driver="queue")
    keep_before = (ts.get(keep["id"]), ts.get(keep2["id"]))
    changed = ts.strip_leaked_flags()
    check("every leaked shape is changed, nothing else",
          sorted(changed) == sorted([work["id"], review["id"], follow["id"],
                                     nested["id"], notes["id"]]), changed)
    check("a review of a follow-up loses it inside both prefixes",
          ts.get(nested["id"])["goal"].startswith(
              "Goal that was given:\nA review of tsk_06b789e3ff (Keep"), ts.get(nested["id"]))
    check("a --notes leaked behind it goes too",
          ts.get(notes["id"])["goal"].startswith("User 2026-10-26:")
          and ts.get(notes["id"])["title"].startswith("User 2026-10-26:"), ts.get(notes["id"]))
    w = ts.get(work["id"])
    check("a finished task's goal and title lose the flag",
          w["goal"].startswith("Executing the operator's") and w["title"].startswith("Executing")
          and w["state"] == T.DONE, w)
    r = ts.get(review["id"])
    check("a review keeps its own prefix",
          r["title"] == "Review: Executing the op"
          and r["goal"].startswith("Goal that was given:\nExecuting"), r)
    f = ts.get(follow["id"])
    check("a follow-up's quoted parent loses it",
          f["goal"].startswith("A review of tsk_8b1c77e711 (Executing")
          and f["title"] == "From review: the goal names a missing file", f)
    check("the change is recorded as an event",
          w["events"][-1]["kind"] == "edited" and "--by" in w["events"][-1]["detail"])
    check("goals with options mid-text are untouched",
          (ts.get(keep["id"]), ts.get(keep2["id"])) == keep_before)
    check("saved to disk", TaskStore(path).get(work["id"])["goal"] == w["goal"])
    check("a second pass changes nothing", ts.strip_leaked_flags() == [])
    # A failed save leaves memory as it was, as every other store write does.
    ts.create("--by operator another leaked filing for the save test", driver="queue")
    real = ts._save
    ts._save = lambda: (_ for _ in ()).throw(OSError("disk full"))
    try:
        ts.strip_leaked_flags()
        raised = False
    except OSError:
        raised = True
    ts._save = real
    check("a failed save raises and restores the record", raised and any(
        t["goal"].startswith("--by operator another") for t in ts.all().values()))


def test_startup_cleans_leaked_flags():
    import ast
    print("\nstartup: the leaked --by repair runs with the other restart repairs")
    src = (BASE / "bot.py").read_text()
    calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "strip_leaked_flags"]
    check("bot.py calls task_store.strip_leaked_flags()", len(calls) == 1
          and isinstance(calls[0].func.value, ast.Name)
          and calls[0].func.value.id == "task_store")


def _motion_run(js, mode=""):
    node = shutil.which("node")
    if not node:
        return None
    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    (d / "drive.js").write_text(MOTION_HARNESS)
    p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js"), mode],
                       capture_output=True, text=True, timeout=90)
    if p.returncode != 0:
        return {"error": p.stderr.strip()[-900:]}
    return json.loads(p.stdout)


def test_dashboard_design_system():
    import re
    import visualizer as V
    print("\nthe dashboard has one design system: tokens for every measure, colour and motion")
    css = re.search(r"<style>(.*?)</style>", V.PAGE, re.S).group(1)
    root = css[:css.index(':root[data-theme="light"]')]
    light = css[css.index(':root[data-theme="light"]'):css.index("@media (prefers-color-scheme: light)")]
    auto = css[css.index("@media (prefers-color-scheme: light)"):css.index("* { box-sizing")]
    rules = css[css.index("* { box-sizing"):]
    defined = set(re.findall(r"(--[\w-]+):", root))
    want = ({f"--sp-{k}" for k in ("05", "1", "15", "2", "25", "3", "35", "4", "5", "6", "8")}
            | {f"--fs-{k}" for k in ("2xs", "xs", "sm", "md", "base", "lg", "xl", "2xl")}
            | {f"--r-{k}" for k in ("xs", "sm", "md", "lg", "xl", "pill")}
            | {"--el-1", "--el-2", "--el-3", "--accent", "--on-accent", "--accent-soft", "--accent-line",
               "--dur-1", "--dur-2", "--dur-3", "--ease-std", "--ease-out", "--ease-in"}
            | {f"--{h}{v}" for h in ("ok", "warn", "bad", "info") for v in ("", "-soft", "-line")})
    check("the root declares the spacing, type, radius, elevation, accent, semantic and motion tokens",
          want <= defined, sorted(want - defined))
    durs = [int(x) for x in re.findall(r"--dur-\d: (\d+)ms", root)]
    check("motion runs 120-240ms", durs and all(120 <= d <= 240 for d in durs), durs)
    light_vars = set(re.findall(r"(--[\w-]+):", light))
    auto_vars = set(re.findall(r"(--[\w-]+):", auto))
    check("light re-mixes the same tokens dark declares (and Auto is the same list as Light)",
          light_vars and light_vars <= defined and light_vars == auto_vars, sorted(light_vars ^ auto_vars))
    check("light is chosen (data-theme) or follows the system on Auto; dark stays the default",
          "color-scheme: dark;" in root and ':root[data-theme="auto"]' in auto
          and 'localStorage.getItem("sw-theme") || "dark"' in V.PAGE)
    # Everything after the token blocks is written in tokens: no colour, font
    # size or radius written out (the SVG chart's own label size excepted).
    values = re.findall(r"[\w-]+\s*:\s*([^;{}]+)", re.sub(r"[^{}]+\{", "{", rules))
    hexes = [h for v in values for h in re.findall(r"#[0-9A-Fa-f]{3,8}\b", v)]
    check("no colour is written out below the tokens", not hexes, hexes[:10])
    sizes = re.findall(r"font-size:\s*[\d.]+px", rules)
    check("no font size is written out below the tokens", not sizes, sizes[:10])
    fonts = [m for m in re.findall(r"([^{}]+)\{[^}]*font:\s*[\d.]+px", rules)
             if ".tr-chart" not in m and ".tr-hgrid" not in m]
    check("...the trader chart's SVG labels the only exception", not fonts, fonts[:5])
    radii = re.findall(r"border-radius:[^;]*\b\d+px", rules)
    check("no radius is written out below the tokens", not radii, radii[:10])
    weights = re.findall(r"font-weight:\s*\d+", rules)
    check("no font weight is written out below the tokens", not weights, weights[:10])
    inline = [m for m in re.findall(r'style="([^"$]*)"', V.PAGE) if re.search(r"\d+px|#[0-9A-Fa-f]{3}", m)]
    check("no markup carries a one-off inline size or colour", not inline, inline[:5])
    check("the stats chart's bars are not the segmented control (one .seg each)",
          'class="bar"' in V.PAGE and "\n  .bar {" in css and 'class="seg" style=' not in V.PAGE)
    # The theme is set on <html> before the stylesheet, by a script the page's
    # own extraction (the first bare <script>) never mistakes for the page's.
    head = V.PAGE[:V.PAGE.index("<style>")]
    check("the theme is applied before the first paint, from <head>", '<script id="themeinit">' in head)
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    check("...and the page's script is still the first bare <script>", "function toast(" in js and "cycleTheme" in js)
    check("⋯ offers Theme", 'id="themebtn" onclick="cycleTheme()"' in V.PAGE)
    check("a light theme launches the home-screen app with the dark-on-light status bar",
          'setAttribute("content", "default")' in head and head.index("apple-mobile-web-app-status-bar-style")
          < head.index('<script id="themeinit">'))
    def contrast(a, b):
        def lum(h):
            c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
            return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
        x, y = sorted((lum(a), lum(b)))
        return (y + 0.05) / (x + 0.05)
    tok = lambda block, name: re.search(rf"{name}: (#[0-9A-Fa-f]{{6}})\b", block).group(1)
    for theme, block in (("dark", root), ("light", light)):
        bg = tok(block, "--bg")
        low = {n: round(contrast(tok(block, n), bg), 2) for n in ("--ink", "--muted", "--accent", "--ok", "--warn", "--bad", "--info")}
        check(f"{theme}: text and every semantic colour read at 4.5:1 or better on the page",
              all(v >= 4.5 for v in low.values()), low)
    phone = css[css.index("@media (max-width: 700px)"):]
    check("on a phone the Trader's tabs are one sideways row of 44pt targets",
          re.search(r"\.tr-tabs \{ flex-wrap: nowrap; overflow-x: auto;", phone) is not None
          and re.search(r"\.tr-tab \{[^}]*min-height: var\(--tap\)", phone) is not None)
    check("a panel's ✕ is a tap target at every width", re.search(r"button\.xclose \{ min-width: var\(--tap\); min-height: var\(--tap\);", css) is not None)
    check("a panel's title row sticks to its top", re.search(r"\.box > h2 \{ position: sticky; top: 0;", css) is not None)


def test_dashboard_motion():
    import re
    import visualizer as V
    print("\nthe dashboard moves by transform and opacity: sheets, lists, skeletons -- and not at all under reduced motion")
    css = re.search(r"<style>(.*?)</style>", V.PAGE, re.S).group(1)
    frames = dict(re.findall(r"@keyframes ([\w-]+) \{(.*?\})\s*\}", css, re.S))
    props = {k: set(re.findall(r"([a-z-]+)\s*:", v)) for k, v in frames.items()}
    want = {"sheet-in", "sheet-out", "sheet-up", "sheet-down", "from-right", "to-right", "view-in", "item-in",
            "shimmer", "toast-in", "toast-out", "fade-in", "fade-out", "pop"}
    check("the sheets', lists', toasts' and skeletons' keyframes exist", want <= set(frames), sorted(want - set(frames)))
    check("every keyframe moves only transform and opacity",
          all(v <= {"transform", "opacity"} for v in props.values()), {k: v for k, v in props.items() if not v <= {"transform", "opacity"}})
    trans = re.findall(r"transition:\s*([^;]+);", css)
    bad = [t for t in trans for part in t.split(",")
           if part.strip().split()[0] not in ("transform", "opacity", "color", "background-color", "border-color",
                                               "box-shadow", "filter", "grid-template-rows", "visibility", "none")]
    check("transitions are transform/opacity (colours and the chat's fold aside), never layout", not bad, bad[:5])
    rm = css[css.rfind("@media (prefers-reduced-motion: reduce)"):]
    check("reduced motion zeroes every duration token and drops the closing copies",
          "--dur-1: 0ms; --dur-2: 0ms; --dur-3: 0ms;" in rm and ".sheet-ghost, #fxlayer > * { display: none !important; }" in rm
          and "animation: none !important" in rm)
    for sel in ("#taskmodal, #boardmodal, #learnmodal, #tradermodal, #fmodal, #alertsheet.open {\n    animation: fade-in",
                "#taskmodal .box, #boardmodal .box, #learnmodal .box, #tradermodal .box, #fmodal .fbox {\n    animation: sheet-in",
                "#bdetail { animation: from-right"):
        check(f"opening animates: {sel.split('{')[0].strip()[:40]}…", sel in css)
    phone = css[css.index("@media (max-width: 700px)"):]
    check("on a phone a panel is a sheet that slides up, and back down",
          "animation-name: sheet-up;" in phone and "animation-name: sheet-down !important;" in phone)
    check("on a phone the list and a thread slide in from their sides",
          "body.reading main { animation: view-in" in phone and "body.back .listcol {" in phone)
    check("the session list has a skeleton in the markup, until its first draw",
          '<nav id="list"><div id="listskel" class="skels" aria-hidden="true"><div class="skel skel-card">' in V.PAGE)
    check("so does the Trader view", '<div id="trbody"><div class="hint">Reading trader\'s status…</div><div class="skels"' in V.PAGE)
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    check("a thread opening draws the shape of a chat until its first read",
          'm0.innerHTML = skel("chat", 2);' in js
          and js.index('m0.innerHTML = skel("chat", 2);') < js.index('if (from === 0 || data.reset || want.error) {\n        msgs.innerHTML = "";'))
    check("a row's disclosure loads behind a skeleton", '<div class="hint">loading…</div>${skel("lines")}' in js)
    o = _motion_run(js)
    if o is None:
        print("  … node not installed — the motion and touch code checked by text only")
        return
    if "error" in o:
        check("the motion harness runs the page's script", False, o["error"])
        return
    check("a panel opens at once and shows a skeleton until its list arrives",
          o["sheetOpen"] == {"display": "flex", "skeleton": True} and o["skeletonReplaced"], o["sheetOpen"])
    check("closing, it is gone at once, and an inert copy of it slides away and is removed",
          o["sheetClose"] == {"display": "none", "ghost": True, "ghostCloned": True, "ghostInert": True}
          and o["ghostGone"], o["sheetClose"])
    check("a form sheet and the card's detail panel close the same way",
          o["formOpen"] == "flex" and o["formClose"] == {"display": "none", "ghost": True}
          and o["detailClose"] == {"display": "none", "ghost": True}, (o["formClose"], o["detailClose"]))
    check("a toast rises in, then leaves", o["toast"] == {"text": "Saved", "show": True, "display": "block"}
          and o["toastLeaving"] is True and o["toastGone"] == "none", (o["toast"], o["toastLeaving"]))
    check("the list's skeleton is there at load and gone at its first draw; the cards fade in",
          o["skelAtLoad"] and o["listDrawn"] == {"skelGone": True, "cards": 3, "fadedIn": 3}, o["listDrawn"])
    check("a keyed redraw glides what moved, raises what is new, leaves what stayed",
          o["flip"]["a"] == ['{"transform":"translate(0px, -50px)"}'] and o["flip"]["b"] == ['{"transform":"translate(0px, 50px)"}']
          and o["flip"]["c"] == [] and o["flip"]["d"] == ['{"opacity":0,"transform":"scale(.97)"}'], o["flip"])
    check("a redraw that changed nothing measures and animates nothing", o["flipSame"])
    check("the task panel's rows are keyed: a row left behind by a dismissal glides up",
          o["taskRowsFlip"] == ['{"transform":"translate(0px, 60px)"}'], o["taskRowsFlip"])
    check("an animation held at its first frame is finished (a spinner, or one under way, is left)",
          o["unstick"] == ["finished", "running", "running"], o["unstick"])
    check("skeletons fill only what is empty, hidden from assistive tech",
          o["skel"] == {"empty": 3, "full": "<p>x</p>", "hidden": True}, o["skel"])
    r = _motion_run(js, "reduced")
    if "error" in r:
        check("the motion harness runs under reduced motion", False, r["error"])
        return
    check("reduced motion: nothing is animated at all -- no sheet copy, no fade, no glide, no flight",
          r["animTotal"] == 0 and r["canAnimate"] is False and not r["sheetClose"]["ghost"]
          and not r["formClose"]["ghost"] and not r["detailClose"]["ghost"] and r["listDrawn"]["fadedIn"] == 0
          and r["flip"] == {"a": [], "b": [], "c": [], "d": []} and r["flewOut"] is False,
          {k: r[k] for k in ("animTotal", "canAnimate", "sheetClose", "flip", "flewOut")})
    check("reduced motion: a toast just appears and just goes",
          r["toast"]["show"] is False and r["toastLeaving"] == "none", r["toast"])
    check("reduced motion: every state is still reached -- a swipe still dismisses, sheets still close, skeletons still draw",
          r["afterSwipe"]["pending"] == "dismiss" and r["sheetClose"]["display"] == "none"
          and r["sheetOpen"]["skeleton"] and r["listDrawn"]["skelGone"], r["afterSwipe"])


def test_dashboard_swipe_undo_and_optimism():
    import re
    import visualizer as V
    print("\nthe dashboard: swipe a proposal, undo it; hide a thread at once; refusals put things back")
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    check("a proposal's card and row are swipeable by one rule; nothing else is",
          js.count("${swipeAttr(t)}") == 2 and js.count('data-swipe="1"') == 1)
    check("a swiped card is drawn by nobody while its Undo runs", "r.tasks = r.tasks.filter(notGone);" in js
          and "cols[k] = (v || []).filter(notGone);" in js)
    o = _motion_run(js)
    if o is None:
        print("  … node not installed — swipe and undo checked by text only")
        return
    if "error" in o:
        check("the swipe harness runs the page's script", False, o["error"])
        return
    check("a finger dragging a card left moves it and shows Dismiss underneath, armed past a third",
          o["during"] == {"transform": "translateX(-200px)", "dismissShown": True, "armed": True, "swiping": True}, o["during"])
    check("let go past the threshold, it flies out", o["flewOut"])
    check("...and nothing is sent yet: an Undo toast says what happened",
          o["afterSwipe"] == {"pending": "dismiss", "posted": 0, "toast": "Dismissed: Tidy the board Undo",
                              "undoable": True, "display": "flex"}, o["afterSwipe"])
    check("the tap a swipe ends with does not also open the card", o["clickSwallowed"])
    check("Undo: nothing was ever sent, and it is drawn again", o["afterUndo"] == {"pending": False, "posted": 0}, o["afterUndo"])
    check("a swipe right accepts -- sent once the undo window is up, not before",
          o["acceptPending"] == "accept" and o["beforeWindow"] == 0
          and o["afterWindow"] == {"posts": [{"action": "accept", "id": "tsk_b"}], "pending": False}, o["afterWindow"])
    check("refused by the server, the card comes back and the toast says why",
          o["refused"] == {"toast": "Not dismissed: already running — it is back", "bad": True, "pending": False}, o["refused"])
    check("put away before Undo ran out (the app hidden), the action is sent at once", o["sentOnHide"])
    check("an Undo tapped while the action is on its way says it is too late, and the action is sent once",
          o["inFlightUndo"] == {"toast": "Already sent — that one can't be undone here", "pendingWhileSending": True}
          and o["inFlightSent"] == 1, (o["inFlightUndo"], o["inFlightSent"]))
    check("a swiped card is out of the page before the redraw, so nothing fades it a second time",
          o["swipedCardRemoved"])
    check("once sent, its Undo toast goes; an Undo tapped after that says it was too late, and sends nothing more",
          o["undoToastGoesWithTheSend"] and o["lateUndo"] == {"toast": "Already sent — that one can't be undone here",
                                                               "bad": True, "posts": 1, "pending": False}, o["lateUndo"])
    check("a second swipe while the first's Undo is up joins it: one toast, 'Undo 2'",
          o["twoToast"] == {"text": "Accepted: Second (and 1 more) Undo 2", "undo": "Undo 2"}, o["twoToast"])
    check("...the first is not sent when its own 5s run out, since its Undo is still showing",
          o["twoBeforeWindow"] == {"posts": 0, "pending": True}, o["twoBeforeWindow"])
    check("...and 'Undo 2' brings both back with nothing sent", o["twoUndone"] == {"posts": 0, "pending": False}, o["twoUndone"])
    check("left alone, both are sent once the shared window is up, and the toast goes",
          o["twoSent"] == {"ids": ["tsk_s3", "tsk_s4"], "pending": 0, "toastGone": True}, o["twoSent"])
    check("another toast over a swipe's Undo: the swipe is not sent while it is covered",
          o["covered"] == {"text": "Hidden: Some thread Undo"}
          and o["coveredBeforeBack"] == {"posts": 0, "pending": True}, (o["covered"], o["coveredBeforeBack"]))
    check("...its Undo comes back once that toast goes, with a whole window, then it is sent",
          o["coveredBack"] == {"text": "Dismissed: Covered Undo", "display": "flex", "posts": 0}
          and o["coveredStillHeld"] == 0
          and o["coveredSent"] == {"ids": ["tsk_c1"], "pending": False},
          (o["coveredBack"], o["coveredStillHeld"], o["coveredSent"]))
    check("toasts piled on toasts still give it back; so does tapping the covering toast's own Undo",
          o["chainBack"] == "Accepted: Covered again Undo"
          and o["tappedBack"] == {"text": "Accepted: Covered again Undo", "posts": 0}
          and o["coveredUndone"] == {"posts": 0, "pending": False},
          (o["chainBack"], o["tappedBack"], o["coveredUndone"]))
    check("not far enough, it springs back and nothing is pending",
          o["short"] == {"pending": False, "transform": "", "swiping": False}, o["short"])
    check("a mostly vertical drag is a scroll, not a swipe", o["vertical"] == {"pending": False, "swiping": False})
    check("a mouse is not a finger (the buttons are its way); a drag from a button is the button's",
          o["mouse"] is False and o["onButton"] is False)
    check("while Undo runs, a poll's redraw leaves the card out and counts without it",
          o["redraw"] == {"hidesPending": True, "keepsOther": True, "count": True, "swipeMarked": True}, o["redraw"])
    check("a reviewer's proposal and queued work are not swipeable", o["reviewerNotSwiped"] and o["queuedNotSwiped"])
    check("the task list and the board swipe by one rule", o["sameRule"])
    check("hiding a thread takes its card away before the server answers", o["hiddenAtOnce"] == 2, o["hiddenAtOnce"])
    check("...then offers Undo, which shows it again",
          o["hideToast"] == {"text": "Hidden: Thread 2 Undo", "undoable": True, "sent": [{"action": "hide", "key": "D1:2.0"}]}
          and o["hideUndone"] == {"action": "unhide", "key": "D1:2.0"}, (o["hideToast"], o["hideUndone"]))
    check("a hide that cannot reach the server is rolled back and said",
          o["hideFailed"] == {"hidden": False, "toast": "Could not hide that thread — nothing changed", "bad": True, "cards": 3},
          o["hideFailed"])
    check("a button's row dims at the tap and comes back once the server answers",
          o["busy"] == {"during": True, "after": False}, o["busy"])
    check("pulled down past its top on a phone, the list fetches again -- not short of the mark, not scrolled, not on a laptop",
          o["pullFar"] == 1 and o["pullShort"] == 0 and o["pullScrolled"] == 0 and o["pullLaptop"] == 0,
          {k: o[k] for k in ("pullFar", "pullShort", "pullScrolled", "pullLaptop")})
    check("the ring shows as far as the finger has pulled, and is put back out of sight after every touch",
          o["ringBarelyPulled"] == "0" and o["ringAfterShort"] == "0" and o["ringRested"] == ["", ""]
          and o["ringAfterRefresh"] == ["", ""], {k: o[k] for k in ("ringBarelyPulled", "ringAfterShort", "ringRested", "ringAfterRefresh")})
    check("dragging a scroller inside the list (a long goal) is not a pull", o["pullNested"] == 0)
    check("⋯ › Theme cycles dark, light, Auto; it is kept on the device and the status bar follows",
          o["theme0"] == "dark" and o["themes"] == [["light", "light", "#F8F3F7"], ["auto", "auto", "#2C0E2E"],
                                                     ["dark", "dark", "#2C0E2E"]]
          and o["themeLabel"] == " Dark" and o["themeBad"] == "dark", o["themes"])
    check("a ?theme= link draws that theme for the view and leaves the device's choice alone",
          o["themeLink"] == ["light", "dark"], o["themeLink"])


def test_dashshots_perf_probe():
    import importlib.machinery
    import importlib.util
    import re
    import shutil
    import subprocess as _sp
    import tempfile
    print("\nbin/dashshots --perf: the performance probe goes into a copy's page, and only answers ?perf=")
    root = Path(__file__).resolve().parent.parent
    loader = importlib.machinery.SourceFileLoader("dashshots_p", str(root / "bin" / "dashshots"))
    spec = importlib.util.spec_from_loader("dashshots_p", loader)
    D = importlib.util.module_from_spec(spec)
    loader.exec_module(D)
    with tempfile.TemporaryDirectory() as td:
        viz = Path(td) / "visualizer.py"
        shutil.copy2(root / "visualizer.py", viz)
        D.inject_probe(viz, 4322, D.PERF_JS)
        page = viz.read_text()
    probe, own = page.find('const BOT = "http://127.0.0.1:4322"'), page.find("function setHTML(")
    check("the perf probe is in the copy's page, before the page's own script",
          0 < probe < own and "perfprobe" in str(D.PERF_JS) and "__BOT__" not in page[probe - 200:own])
    src = (root / "bin" / "perfprobe.js").read_text()
    check("it answers only to ?perf=", re.search(r"if \(!/\[\?&\]perf=/\.test\(location\.search\)\) return;", src) is not None)
    check("it reads paint timing and counts frames on the list, the task panel, a board and a thread",
          all(x in src for x in ('getEntriesByType("paint")', "scroll_list", "scroll_tasks", "scroll_board",
                                 "scroll_thread", "sheet_open")))
    check("it changes nothing: no POST but its own report to the stand-in bot",
          src.count("method: \"POST\"") == 1 and "taskCall" not in src and "taskAction" not in src)
    d = (root / "bin" / "dashshots").read_text()
    check("dashshots --perf runs it on the phone widths, both copies, alternating runs",
          'ap.add_argument("--perf"' in d and "def perf_phone(" in d and "perf-{width}.json" in d)
    node = shutil.which("node")
    if node:
        r = _sp.run([node, "--check", str(root / "bin" / "perfprobe.js")], capture_output=True, text=True)
        check("bin/perfprobe.js parses", r.returncode == 0, r.stderr[-400:])

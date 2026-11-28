"""Dashboard pages: search, the inbox, plan cards, project and spend pages, the Trader view.

Run by tests/run.py with every other tests/test_*.py; see tests/harness.py."""

from harness import *  # noqa: F401,F403


# --- search across threads and tasks ------------------------------------------
# One field finds a thread by its title, summary or messages and a task by its
# id, title, goal or result -- from an index held in memory, so a keystroke's
# search does not read every transcript again. Snippets never carry a secret.

def _search_fixture(tasks, sessions, transcripts):
    """A temp sessions.json, tasks.json and projects dir, the visualizer's
    search pointed at them with a fresh index. Returns (V, restore, d, proj)."""
    sys.argv = ["x"]
    import visualizer as V
    d = Path(tempfile.mkdtemp())
    proj = d / "projects"
    (proj / "-a").mkdir(parents=True)
    (d / "sessions.json").write_text(json.dumps(sessions))
    (d / "tasks.json").write_text(json.dumps({t["id"]: t for t in tasks}))
    for sid, lines in transcripts.items():
        (proj / "-a" / f"{sid}.jsonl").write_text("".join(lines))
    saved = (V.SESSIONS_FILE, V.TASKS_FILE, V.PROJECTS_DIR, V.INDEX_RECHECK_S, V.SEARCH_INDEX)
    V.SESSIONS_FILE, V.TASKS_FILE, V.PROJECTS_DIR = d / "sessions.json", d / "tasks.json", proj
    V.INDEX_RECHECK_S = 0
    V.SEARCH_INDEX = V.SearchIndex()

    def restore():
        (V.SESSIONS_FILE, V.TASKS_FILE, V.PROJECTS_DIR, V.INDEX_RECHECK_S,
         V.SEARCH_INDEX) = saved
    return V, restore, d, proj


def _hits(r, kind=None):
    return [h for g in r["groups"] if kind in (None, g["kind"]) for h in g["items"]]


def test_search_threads_and_tasks():
    print("\nsearch finds threads and tasks, grouped and ranked")
    now = time.time()
    tasks = [
        {"id": "tsk_abc1234567", "title": "Fix the landing gate", "goal": "rebase then test",
         "state": "done", "project": "silkworm", "updated": now - 100,
         "result": {"text": "Landed; the zebra suite passed", "cost": 1}},
        {"id": "tsk_abc9999999", "title": "Another", "goal": "mentions tsk_abc1234567 in passing",
         "state": "queued", "updated": now},
        {"id": "tsk_zzz0000000", "title": "Zebra crossing", "goal": "paint stripes",
         "state": "proposed", "updated": now - 500},
        {"id": "tsk_old0000000", "title": "old", "goal": "zebra crossing again",
         "state": "done", "updated": now - 9000, "result": None},
    ]
    sessions = {
        "D1:1.1": {"session_id": "sid-one", "title": "Kitchen tiles", "updated": now - 10,
                   "summary": "Picking a zebra-free grout colour"},
        "D1:2.2": {"session_id": "sid-two", "title": "Zebra talk", "updated": now - 20},
        "D1:3.3": {"session_id": "sid-three", "title": "Quiet", "updated": now,
                   "kind": "task"},
    }
    transcripts = {
        "sid-one": [_tline("user", "what grout goes with oak?"),
                    _tline("assistant", "Bone, probably.")],
        "sid-three": [_tline("user", "nothing here"),
                      _tline("assistant", "The zebra crossing is painted.\nDone."),
                      json.dumps({"type": "user", "isMeta": True, "message": {
                          "content": "meta zebra caveat"}}) + "\n",
                      # content a plain string, however the line is spaced
                      json.dumps({"type": "user", "message": {"content": "plain walrus words"}})
                      + "\n"],
    }
    V, restore, d, proj = _search_fixture(tasks, sessions, transcripts)
    try:
        r = V.search("tsk_abc1234567")
        ids = [h.get("id") or h.get("key") for h in _hits(r)]
        check("an exact task id comes first", ids[:1] == ["tsk_abc1234567"], str(ids))
        check("as an id match, marked whole",
              _hits(r)[0]["field"] == "id"
              and _hits(r)[0]["snippet"]["marks"] == [[0, 14]], str(_hits(r)[0]))
        check("a task merely mentioning the id is found after it",
              "tsk_abc9999999" in ids[1:], str(ids))
        r = V.search("TSK_ABC")
        ids = sorted(h["id"] for h in _hits(r, "tasks") if h["field"] == "id")
        check("an id prefix finds every task it starts, in any case",
              ids == ["tsk_abc1234567", "tsk_abc9999999"], str(ids))
        check("and not one it does not start", "tsk_zzz0000000" not in ids)
        r = V.search("tsk_nothing")
        check("an id that matches nothing finds nothing", r["total"] == 0, str(r))

        r = V.search("zebra")
        tk = [h["id"] for h in _hits(r, "tasks")]
        th = [h["key"] for h in _hits(r, "threads")]
        check("a task by title, goal and result",
              set(tk) == {"tsk_zzz0000000", "tsk_old0000000", "tsk_abc1234567"}, str(tk))
        check("a title match ranks above a goal or result match",
              tk[0] == "tsk_zzz0000000", str(tk))
        check("a goal match above a result match", tk.index("tsk_old0000000")
              < tk.index("tsk_abc1234567"), str(tk))
        check("a thread by title, summary and message text",
              set(th) == {"D1:1.1", "D1:2.2", "D1:3.3"}, str(th))
        check("thread title, then summary, then message",
              th == ["D1:2.2", "D1:1.1", "D1:3.3"], str(th))
        msg = next(h for h in _hits(r, "threads") if h["key"] == "D1:3.3")
        check("a message hit says whose it was, and it is the reply",
              msg["field"] == "message" and msg["role"] == "assistant", str(msg))
        check("the snippet is one line with the match marked",
              "\n" not in msg["snippet"]["text"]
              and [msg["snippet"]["text"][a:b] for a, b in msg["snippet"]["marks"]] == ["zebra"],
              str(msg["snippet"]))
        check("an isMeta entry is not a message", msg["messages"] == 1, str(msg))
        check("groups: tasks and threads, the better group first",
              [g["kind"] for g in r["groups"]] == ["tasks", "threads"]
              and r["groups"][0]["items"][0]["score"] >= r["groups"][1]["items"][0]["score"],
              str([g["kind"] for g in r["groups"]]))
        check("one hit per task or thread", len(tk) == len(set(tk)) and len(th) == len(set(th)))

        r = V.search("zebra crossing")
        tk = [h["id"] for h in _hits(r, "tasks")]
        check("every word must be there", tk == ["tsk_zzz0000000", "tsk_old0000000"], str(tk))
        r = V.search("crossing zebra")
        tk = [h["id"] for h in _hits(r, "tasks")]
        check("in any order", set(tk) == {"tsk_zzz0000000", "tsk_old0000000"}, str(tk))
        check("each word marked", [_hits(r, "tasks")[0]["snippet"]["text"][a:b].lower()
                                   for a, b in _hits(r, "tasks")[0]["snippet"]["marks"]]
              == ["zebra", "crossing"], str(_hits(r, "tasks")[0]["snippet"]))
        sessions["D1:4.4"] = {"session_id": "sid-four", "title": "Zebra again",
                              "updated": now}
        (d / "sessions.json").write_text(json.dumps(sessions))
        th = [h["key"] for h in _hits(V.search("zebra"), "threads")]
        check("within a score, the newest first; a new thread is found at once",
              th[:2] == ["D1:4.4", "D1:2.2"], str(th))
        with open(proj / "-a" / "sid-one.jsonl", "a") as f:
            f.write(_tline("assistant", "Try the walrus grey."))
        th = [h["key"] for h in _hits(V.search("walrus"), "threads")]
        check("a message appended since is found, and string content is text",
              sorted(th) == ["D1:1.1", "D1:3.3"], str(th))
        tasks.append({"id": "tsk_new0000000", "title": "walrus", "state": "queued",
                      "updated": now})
        (d / "tasks.json").write_text(json.dumps({t["id"]: t for t in tasks}))
        check("and a task filed since", [h["id"] for h in _hits(V.search("walrus"), "tasks")]
              == ["tsk_new0000000"])
        sn = V.snippet("🚀🚀 deploy done", 3, ["deploy"])
        check("marks count as the page does (UTF-16): an emoji before a match is two",
              sn["marks"] == [[5, 11]], str(sn))
        sn = V.snippet("İİ Deploy", 3, ["deploy"])
        check("a letter that lowercases longer does not shift the mark",
              sn["marks"] == [[3, 9]], str(sn))
        check("a bare tsk_ is a word, not every task by id",
              all(h["field"] != "id" for h in _hits(V.search("tsk_"))))
        with open(proj / "-a" / "sid-one.jsonl", "a") as f:
            f.write(json.dumps({"type": "user", "message": "a string, not a dict"}) + "\n")
            f.write(json.dumps({"type": "user", "message": {"content": [7, {"type": "text", "text": "x"}]}}) + "\n")
            f.write(_tline("assistant", "the narwhal survives"))
        check("a line of an odd shape is skipped, not the rest of the read",
              [h["key"] for h in _hits(V.search("narwhal"))] == ["D1:1.1"])
        check("an empty query finds nothing and reads nothing",
              V.search("   ") == {"query": "", "groups": [], "total": 0, "truncated": False})
    finally:
        restore()


def test_search_reads_only_what_changed():
    print("\nsearch reads a transcript once, then only what is appended")
    now = time.time()
    sessions = {f"D1:{i}.{i}": {"session_id": f"sid-{i}", "title": f"t{i}", "updated": now}
                for i in range(3)}
    transcripts = {f"sid-{i}": [_tline("user", f"hello {i}")] for i in range(3)}
    V, restore, d, proj = _search_fixture([], sessions, transcripts)
    saved = V._complete_lines
    reads = []
    try:
        V._complete_lines = lambda p, s: reads.append(Path(p).name) or saved(p, s)
        V.search("hello")
        check("the first search reads each transcript", sorted(reads) ==
              ["sid-0.jsonl", "sid-1.jsonl", "sid-2.jsonl"], str(reads))
        n = len(reads)
        V.search("hello")
        check("unchanged, nothing is read again", len(reads) == n, str(reads[n:]))
        with open(proj / "-a" / "sid-1.jsonl", "a") as f:
            f.write(_tline("assistant", "goodbye"))
        r = V.search("goodbye")
        check("an append reads only that file", reads[n:] == ["sid-1.jsonl"], str(reads[n:]))
        check("and finds it", [h["key"] for h in _hits(r)] == ["D1:1.1"])
        (proj / "-a" / "sid-1.jsonl").write_text(_tline("user", "hi"))
        check("a transcript replaced by a shorter one is reread, not added to",
              V.search("goodbye")["total"] == 0 and V.search("hi")["total"] == 1)
    finally:
        V._complete_lines = saved
        restore()


def test_search_redacts():
    print("\nno search shows or matches a secret")
    tok = "sk-ant-" + "oat01" + "Q" * 30        # built in pieces: never a literal
    slack = "xoxb-" + "1234567890-abcdefghij"
    now = time.time()
    tasks = [{"id": "tsk_sec0000000", "title": "rotate the key", "state": "done",
              "updated": now, "goal": f"the old key was {tok} -- rotate it",
              "result": {"text": f"posted with {slack} fine"}}]
    sessions = {"D1:1.1": {"session_id": "sid-s", "title": "keys", "updated": now,
                           "summary": f"set CLAUDE token {tok}"}}
    transcripts = {"sid-s": [_tline("user", f"here it is: {tok} please rotate")]}
    V, restore, d, proj = _search_fixture(tasks, sessions, transcripts)
    try:
        everything = json.dumps(V.search("rotate")) + json.dumps(V.search("old key")) \
            + json.dumps(V.search("posted")) + json.dumps(V.search("token"))
        check("found, with the match", "rotate" in everything and "posted" in everything)
        check("no snippet carries a secret", tok not in everything and slack not in everything
              and "Q" * 20 not in everything, everything[:300])
        check("it is shown redacted", "[redacted]" in everything)
        check("searching the secret itself finds nothing",
              V.search(tok)["total"] == 0 and V.search("Q" * 20)["total"] == 0
              and V.search("abcdefghij")["total"] == 0)
        import redaction
        real = redaction.scrub
        try:
            def boom(text, known=()):
                raise RuntimeError("no scrubber")
            redaction.scrub = boom
            V.SEARCH_INDEX = V.SearchIndex()
            out = json.dumps(V.search("rotate"))
            check("without the scrubber nothing is indexed at all, rather than unscrubbed",
                  tok not in out and V.search("rotate")["total"] == 0, out[:200])
        finally:
            redaction.scrub = real
    finally:
        restore()


def test_search_scrubber_has_no_side_effects():
    print("\nthe scrubber search uses registers nothing anywhere")
    import subprocess as _sp
    # health_probes registers its checks into health.REGISTRY on import; a
    # scrubber taken from there made the dashboard's search change which
    # checks every later caller saw. redaction.py is the scrubber, alone.
    # In a process of its own: importing health_probes here would change the
    # registry for every test after this one -- the very leak under test.
    code = ("import sys; sys.argv=['x']; import health; before = sorted(health.REGISTRY); "
            "import visualizer; visualizer._redact('x'); "
            "print(before == sorted(health.REGISTRY), 'health_probes' in sys.modules); "
            "import health_probes, redaction; print(health_probes.scrub is redaction.scrub)")
    r = _sp.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                cwd=str(Path(__file__).resolve().parent.parent))
    lines = r.stdout.strip().splitlines()
    check("importing the dashboard and scrubbing registers no health check",
          lines[:1] == ["True False"], (r.stdout + r.stderr)[-300:])
    check("the health checks scrub with the same function", lines[1:2] == ["True"],
          (r.stdout + r.stderr)[-300:])
    import redaction
    gh = "ghp_" + "A1b2" * 6
    pat = "github_pat_" + "11ABC" * 5
    out = redaction.scrub(f"push with {gh} or {pat} and xapp-" + "1-A1234567890-" + "f" * 12)
    check("GitHub and Slack app tokens are scrubbed too",
          gh not in out and pat not in out and "f" * 12 not in out, out)


def test_search_bounded():
    print("\nsearch answers at most fifty, and says how many there were")
    now = time.time()
    tasks = [{"id": f"tsk_b{i:09d}", "title": f"needle {i}", "state": "done",
              "updated": now - i} for i in range(120)]
    # Threads match only in their summaries: every task's title outranks them.
    sessions = {f"D1:{i}.{i}": {"session_id": "", "title": f"thread {i}",
                                "summary": f"about a needle {i}", "updated": now - i}
                for i in range(30)}
    V, restore, d, proj = _search_fixture(tasks, sessions, {})
    try:
        r = V.search("needle")
        hits = _hits(r)
        check("fifty, no more", len(hits) == V.SEARCH_LIMIT == 50, str(len(hits)))
        check("with the true total, and said to be cut", r["total"] == 150 and r["truncated"],
              str((r["total"], r["truncated"])))
        counts = {g["kind"]: (len(g["items"]), g["count"]) for g in r["groups"]}
        counts = {"tasks": (0, 0), "threads": (0, 0), **counts}
        check("each group says how many it had in all",
              counts["tasks"][1] == 120 and counts["threads"][1] == 30
              and counts["tasks"][0] + counts["threads"][0] == 50, str(counts))
        check("threads that rank below every task still get their floor of ten",
              counts["threads"][0] == V.SEARCH_GROUP_FLOOR == 10 and counts["tasks"][0] == 40,
              str(counts))
        check("the newest of equals are the ones kept",
              [h["id"] for h in _hits(r, "tasks")][:2] == ["tsk_b000000000", "tsk_b000000001"])
        check("a smaller ask is honoured", len(_hits(V.search("needle", 5))) == 5)
        check("a narrower query is not cut", not V.search("needle 7")["truncated"])
    finally:
        restore()


def test_search_fast_at_scale():
    print("\nsearch at the real stores' size stays well under 300ms")
    import random
    rnd = random.Random(7)
    words = ("tailscale serve dashboard grout landing rebase review thread task "
             "zebra kitchen premium mondays trader universe sleeve the a of").split()
    say = lambda n: " ".join(rnd.choice(words) for _ in range(n))
    now = time.time()
    # About what the live machine holds (2026-11-22): 2,400 tasks in a 9.5MB
    # tasks.json, 670 threads, 170 transcripts with 5,400 text messages.
    tasks = [{"id": f"tsk_{i:010x}", "title": say(8), "goal": say(400), "state": "done",
              "updated": now - i, "result": {"text": say(150)} if i % 4 == 0 else {"cost": 1}}
             for i in range(2400)]
    sessions = {f"D1:{i}.{i}": {"session_id": f"sid-{i}" if i < 170 else "",
                                "title": say(6), "summary": say(60), "updated": now - i}
                for i in range(670)}
    transcripts = {f"sid-{i}": [_tline("user" if j % 2 else "assistant", say(80))
                                for j in range(32)] for i in range(170)}
    V, restore, d, proj = _search_fixture(tasks, sessions, transcripts)
    try:
        t0 = time.perf_counter()
        V.search("warm")
        cold = time.perf_counter() - t0
        worst = 0.0
        for q in ("tailscale", "the", "a", "zebra kitchen", "tsk_0000000", "nothing-here"):
            t0 = time.perf_counter()
            V.search(q)
            worst = max(worst, time.perf_counter() - t0)
        check("a warm search, common words included, is under 300ms", worst < 0.3,
              f"{worst * 1000:.0f}ms (cold build {cold * 1000:.0f}ms)")
        os.utime(d / "tasks.json")                 # rewritten, as the bot does every few seconds
        (d / "tasks.json").write_text((d / "tasks.json").read_text() + " ")
        t0 = time.perf_counter()
        V.search("tailscale")
        reread = time.perf_counter() - t0
        check("and so is one just after tasks.json changed", reread < 0.3,
              f"{reread * 1000:.0f}ms")
    finally:
        restore()


def test_search_route():
    import threading
    import urllib.request
    print("\n/api/search is a read, answered from the index")
    now = time.time()
    V, restore, d, proj = _search_fixture(
        [{"id": "tsk_route00000", "title": "route me", "state": "done", "updated": now}],
        {"D1:1.1": {"session_id": "", "title": "route thread", "updated": now}}, {})
    saved_bot = V.bot_call
    srv = None
    try:
        V.bot_call = lambda *a, **k: {}
        srv = V.make_server("127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}/api/search?q=route"
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = json.loads(resp.read())
        check("both groups come back over HTTP",
              sorted(g["kind"] for g in body["groups"]) == ["tasks", "threads"], str(body)[:200])
        req = urllib.request.Request(url, data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10)
            posted = True
        except urllib.error.HTTPError:
            posted = False
        check("and there is no POST /api/search", not posted)
        src = Path(V.__file__).read_text()
        body_src = src[src.index("class SearchIndex"):src.index("SEARCH_INDEX = SearchIndex()")]
        check("the index only reads: no store is saved from it",
              ".save(" not in body_src and "write_text" not in body_src
              and "repair=False" in body_src)
    finally:
        if srv:
            srv.shutdown()
        V.bot_call = saved_bot
        restore()


SEARCH_DRIVER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
process.on("unhandledRejection", () => {});
const els = {}, docListeners = {}, opened = [];
function el(id) {
  if (els[id]) return els[id];
  const l = {};
  return els[id] = {id, innerHTML: "", value: "", style: {}, dataset: {}, focused: false,
    classList: {_s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
                toggle(c, on) { (on === undefined ? !this._s.has(c) : on) ? this._s.add(c) : this._s.delete(c); },
                contains(c) { return this._s.has(c); }},
    appendChild() {}, focus() { this.focused = true; }, select() {}, blur() {},
    addEventListener(k, f) { (l[k] = l[k] || []).push(f); }, _l: l,
    querySelector() { return null; }, querySelectorAll() { return []; },
    getBoundingClientRect() { return {top: 0, bottom: 40}; }};
}
globalThis.document = {getElementById: el, body: el("body"),
  addEventListener(k, f) { (docListeners[k] = docListeners[k] || []).push(f); },
  createElement: () => el("_new"), querySelector: () => null, querySelectorAll: () => []};
globalThis.window = globalThis;
globalThis.location = {href: "http://x/", search: ""};
let lastFetch = "";
globalThis.fetch = async (u) => { lastFetch = String(u); return {ok: true, status: 200,
  json: async () => ({sessions: [], tasks: []}), text: async () => "", headers: {get: () => null}}; };
globalThis.setInterval = () => 0;
let timers = [];
globalThis.setTimeout = (f) => { timers.push(f); return timers.length; };
globalThis.clearTimeout = () => {};
globalThis.localStorage = {getItem: () => null, setItem() {}, removeItem() {}};
globalThis.matchMedia = () => ({matches: false, addEventListener() {}, addListener() {}});
globalThis.navigator = {};
(async () => {
eval(src);
openLink = async (q) => { opened.push(["link", q.toString()]); };
openTarget = async (h) => { opened.push(["target", h]); };
phone = () => false;
const R = {query: "zebra", total: 60, truncated: true, groups: [
  {kind: "tasks", count: 40, items: [
    {type: "task", id: "tsk_aaa", title: "Zebra <b>bold</b>", state: "done", project: "p",
     field: "goal", score: 80, snippet: {text: "a zebra & <script>x</script> zebra", marks: [[2, 7], [29, 34]]}}]},
  {kind: "threads", count: 20, items: [
    {type: "thread", key: "D1:1.1", title: "Kitchen", kind: "thread", field: "message",
     role: "assistant", ts: "", messages: 3, score: 10,
     snippet: {text: "the zebra grout", marks: [[4, 9]]}}]}]};
renderSearch(R);
const box = el("searchresults");
const out = {html: box.innerHTML, shown: box.style.display};
openSearchHit(0); renderSearch(R); openSearchHit(1);
out.opened = opened.slice();
out.cleared = el("search").value === "" && box.style.display === "none";
renderSearch({groups: [], total: 0});
out.empty = box.innerHTML;
// ⌘K and Ctrl-K focus the field; plain K does not
el("search").focused = false;
let prevented = 0;
const key = (o) => (docListeners.keydown || []).forEach(f => f(Object.assign(
  {key: "k", metaKey: false, ctrlKey: false, altKey: false, shiftKey: false,
   target: {id: "body"}, preventDefault() { prevented++; }}, o)));
key({}); out.plainK = el("search").focused;
key({metaKey: true}); out.cmdK = el("search").focused;
el("search").focused = false;
key({ctrlKey: true}); out.ctrlK = el("search").focused;
out.prevented = prevented;
// typing searches the server after a pause; Enter opens the first hit
el("search").value = "zebra";
el("search")._l.input.forEach(f => f({target: el("search")}));
timers.forEach(f => f());
out.fetched = lastFetch;
renderSearch(R); opened.length = 0;
el("search")._l.keydown.forEach(f => f({key: "Enter", target: el("search"), preventDefault() {}}));
out.enter = opened.slice();
out.marked = markSnippet({text: "<i>ab</i>", marks: [[3, 5], [99, 120]]});
// a group of many shows its first six, and the rest on a tap
const many = (n, kind) => Array.from({length: n}, (_, i) => kind === "tasks"
  ? {type: "task", id: "tsk_m" + i, title: "t" + i, field: "title", score: 100, snippet: {text: "t" + i, marks: []}}
  : {type: "thread", key: "K" + i, title: "k" + i, field: "message", score: 10, snippet: {text: "k", marks: []}});
const BIG = {total: 50, truncated: false, groups: [{kind: "tasks", count: 40, items: many(40, "tasks")},
                                                  {kind: "threads", count: 10, items: many(10, "threads")}]};
renderSearch(BIG);
out.firstRows = (box.innerHTML.match(/data-i=/g) || []).length;
out.moreRow = /Show 34 more tasks/.test(box.innerHTML) && /Show 4 more threads/.test(box.innerHTML);
out.threadsInView = box.innerHTML.indexOf("Threads") < box.innerHTML.indexOf("Show 34 more") + 400;
let stopped = false;
moreSearch({stopPropagation() { stopped = true; }}, "tasks");
out.afterMore = (box.innerHTML.match(/data-i=/g) || []).length;
out.stopped = stopped;
renderSearch(BIG);
out.reopened = (box.innerHTML.match(/data-i=/g) || []).length;
renderSearch({...BIG});
out.newAnswer = (box.innerHTML.match(/data-i=/g) || []).length;
// typed past what is shown: Return opens nothing stale
renderSearch(R); opened.length = 0;
el("search").value = "zebr";
el("search")._l.input.forEach(f => f({target: el("search")}));
el("search")._l.keydown.forEach(f => f({key: "Enter", target: el("search"), preventDefault() {}}));
out.staleEnter = opened.slice();
// a failed answer is said to have failed, not to have found nothing
globalThis.fetch = async () => ({ok: false, status: 500, json: async () => { throw new Error("x"); }});
timers = [];
el("search").value = "zebra";
el("search")._l.input.forEach(f => f({target: el("search")}));
timers.forEach(f => f());
await new Promise(ok => setImmediate(ok));
out.failed = box.innerHTML;
console.log(JSON.stringify(out));
})();
"""


def test_search_page():
    import re as _re
    import subprocess as _sp
    sys.argv = ["x"]
    import visualizer as V
    print("\nthe search box: grouped, marked, escaped, ⌘K, and a tap opens it")
    js = _re.search(r"<script>(.*?)</script>", V.PAGE, _re.S).group(1)
    tmp = Path(tempfile.mkdtemp())
    (tmp / "page.js").write_text(js)
    (tmp / "drive.js").write_text(SEARCH_DRIVER)
    r = _sp.run(["node", str(tmp / "drive.js"), str(tmp / "page.js")],
                capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not r.stdout.strip():
        check("the search runs in a browser-like context", False, (r.stderr or "")[-400:])
        return
    out = json.loads(r.stdout.strip().splitlines()[-1])
    html = out["html"]
    check("both groups are drawn, tasks first as the server ranked them",
          0 <= html.index("Tasks") < html.index("Threads"), html[:200])
    check("each with how many it shows and has", "<b>1</b>" in html and "of 40" in html
          and "of 20" in html)
    check("the matches are marked", html.count("<mark>zebra</mark>") == 3, html)
    check("and everything else escaped", "<script>" not in html and "&lt;script&gt;" in html
          and "&lt;b&gt;bold" in html, html)
    check("a cut answer says so", "best 2 of 60" in html, html[-200:])
    check("shown", out["shown"] == "block")
    check("a task opens its card; a thread opens the thread",
          out["opened"] == [["link", "card=tsk_aaa"], ["target", "/?thread=D1%3A1.1"]],
          str(out["opened"]))
    check("and the box is cleared behind it", out["cleared"])
    check("nothing found says so", "No threads or tasks match" in out["empty"])
    check("⌘K and Ctrl-K go to the search field; a plain k does not",
          out["cmdK"] and out["ctrlK"] and not out["plainK"] and out["prevented"] == 2,
          str(out))
    check("typing asks the server", out["fetched"] == "/api/search?q=zebra", out["fetched"])
    check("Return opens the first hit", out["enter"] == [["link", "card=tsk_aaa"]],
          str(out["enter"]))
    check("a mark out of range is dropped, not drawn",
          out["marked"] == "&lt;i&gt;<mark>ab</mark>&lt;/i&gt;", out["marked"])
    check("a long group shows six, and says how many more", out["firstRows"] == 12
          and out["moreRow"], str((out["firstRows"], out["moreRow"])))
    check("so the next group is in view", out["threadsInView"])
    check("a tap shows the rest of that group only, without closing the results",
          out["afterMore"] == 46 and out["stopped"], str(out["afterMore"]))
    check("it stays open while the same answer is redrawn; a new answer starts folded",
          out["reopened"] == 46 and out["newAnswer"] == 12,
          str((out["reopened"], out["newAnswer"])))
    check("typing past the results, Return opens nothing stale", out["staleEnter"] == [],
          str(out["staleEnter"]))
    check("a failed search says it failed", "Search failed" in out["failed"]
          and "No threads" not in out["failed"], out["failed"])


PANEL_DRIVER = r"""
// Draws the task panel twice through the page's own renderTasks: once from
// whole tasks (what `list` used to send) and once from board.panel_row's slim
// rows. Every slim row is a Proxy that records any field read that it lacks.
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const fx = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
process.on("unhandledRejection", e => { console.error(e); process.exit(1); });
function el(id) {
  return {id, innerHTML: "", textContent: "", value: "", title: "", className: "",
          style: {}, disabled: false, dataset: {}, children: [],
          classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
          appendChild() {}, removeChild() {}, remove() {}, addEventListener() {},
          insertAdjacentHTML() {}, focus() {}, scrollIntoView() {},
          querySelector() { return null; }, querySelectorAll() { return []; }};
}
const els = {};
const byId = id => els[id] || (els[id] = el(id));
globalThis.document = {getElementById: byId, createElement: () => el("new"),
                       querySelector: () => null, querySelectorAll: () => [],
                       addEventListener() {}, body: el("body")};
globalThis.window = {addEventListener() {}, location: {search: ""}};
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
let rows = fx.full;
const missing = new Set(), asked = [];
const watch = t => new Proxy(t, {get(o, k) {
  if (typeof k === "string" && !(k in o)) missing.add(k);
  return o[k];
}});
globalThis.fetch = async (url, opts) => {
  const body = opts && opts.body ? JSON.parse(opts.body) : {};
  let out = {ok: true};
  if (url.startsWith("/api/sessions")) out = {bot_online: true, sessions: [], slack: {}};
  else if (url.startsWith("/api/stats")) out = {total_cost: 0, cache_rate: null, threads: 0, days: [], models: []};
  else if (url.startsWith("/api/tasks")) {
    asked.push(body);
    if (body.action === "list" || body.action === "attention") out = {ok: true, tasks: rows, counts: {}};
    else if (body.action === "task") out = {ok: true, task: fx.detail[body.id]};
  }
  return {json: async () => out, text: async () => ""};
};
(0, eval)(src + "\n;globalThis.__p = {renderTasks, openMore};");
(async () => {
  const P = globalThis.__p, out = {};
  await P.renderTasks();
  out.full = byId("tlist").innerHTML;
  rows = fx.slim.map(watch);
  byId("tlist")._html = undefined;
  await P.renderTasks();
  out.slim = byId("tlist").innerHTML;
  out.missing = [...missing];
  out.listAsk = asked.filter(a => a.action === "list" || a.action === "attention").slice(-1)[0];
  await P.openMore(fx.open);
  out.opened = byId("more-" + fx.open).innerHTML;
  out.taskAsk = asked.filter(a => a.action === "task").slice(-1)[0];
  process.stdout.write(JSON.stringify(out));
})();
"""


def test_task_panel_slim_rows():
    import re
    import logging
    import board as B
    import tasks as T
    import costs
    sys.argv = ["x"]
    import visualizer as V
    print("\nthe task panel's slim rows draw exactly what the whole tasks drew")
    ts, ps, ids, survey, _ = _board_fixture()
    long_goal = "Make the thing\n\n" + ("evidence line, " * 400) + "THE LAST WORD"
    prop = ts.create(long_goal, title="Make the thing", state=T.PROPOSED, role="implementor",
                     project="alpha")
    res = ts.create("Look into it\n\nquestions", title="Look into it", role=__import__("research").ROLE,
                    state=T.RUNNING, project="alpha", thread="C1:9.9")
    ts.update(res["id"], result={"text": "FINDINGS " + "f" * 6000 + " END OF FINDINGS",
                                 "review": {"ok": True, "summary": "fine"}})
    ts.transition(res["id"], T.NEEDS_INPUT, "findings")
    ts.update(res["id"], needs_user={"action": "!release alpha app", "release": "alpha app",
                                     "why": "yours"})
    ts.create("child step", state=T.PROPOSED, parent=res["id"], role="implementor",
              project="alpha")
    ts.update(ids["awaiting"], needs_user={"action": "do it", "why": "because"})
    # A failure reason longer than a board card's cut: the row showed it whole.
    ts.update(ids["failed"], events=[{"at": time.time(), "kind": "failed", "detail":
              "claude exited 1: NO WORKTREE " + "z" * 400 + " AND THE REAL CAUSE"}])
    recs = list(ts.all().values())

    ns = {"task_store": ts, "costs": costs, "board": B,
          "research": __import__("research"), "slacklinks": __import__("slacklinks"),
          "tasks": __import__("tasks")}
    lift({"_with_costs"}, ns=ns)
    full = ns["_with_costs"](sorted(recs, key=lambda t: -(t.get("created") or 0)))
    slim = [B.panel_row(t) for t in full]
    big, small = len(json.dumps(full)), len(json.dumps(slim))
    check("slim rows are a fraction of the whole tasks", small < big / 3, f"{small} vs {big}")
    check("no goal and no reply text in a slim row",
          all("goal" not in r and "text" not in r["result"] and "events" not in r for r in slim))

    # The route: `slim` is what the panel asks for; `counts` is the badge's.
    handle = _bot_func("handle_tasks", task_store=ts, tasks=T, board=B,
                       log=logging.getLogger("t"), _with_costs=ns["_with_costs"],
                       _spend=lambda p="": {"spend": {}, "spend_line": ""})
    r = handle({"action": "list", "slim": True})
    check("list with slim answers panel rows", r["ok"] and r["tasks"]
          and all(set(t) == set(B.ROW_FIELDS) | {"result", "more", "why"} for t in r["tasks"]))
    check("list without it is unchanged", "goal" in handle({"action": "list"})["tasks"][0])
    c = handle({"action": "counts"})
    check("counts is the counts alone", c == {"ok": True, "counts": ts.counts()}, str(c)[:200])
    check("attention takes slim too",
          all("goal" not in t for t in handle({"action": "attention", "slim": True})["tasks"]))

    detail = {t["id"]: B.detail(ts.get(t["id"]), recs, full=True) for t in full}
    check("the detail a row opens keeps the whole reply",
          detail[res["id"]]["result"]["text"].endswith("END OF FINDINGS")
          and "FINDINGS f" in detail[res["id"]]["result"]["text"])
    check("while the board's detail still keeps its tail",
          B.detail(ts.get(res["id"]), recs)["result"]["text"].startswith("…"))

    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    node = shutil.which("node")
    if not node:
        print("  … node not installed — cannot run the page's own javascript")
        return
    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    chk = subprocess.run([node, "--check", str(d / "dash.js")], capture_output=True, text=True)
    check("the page's script parses (node --check)", chk.returncode == 0, chk.stderr[-400:])
    (d / "fx.json").write_text(json.dumps({"full": full, "slim": slim, "detail": detail,
                                           "open": res["id"]}))
    (d / "drive.js").write_text(PANEL_DRIVER)
    p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js"), str(d / "fx.json")],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        check("the panel's javascript runs", False, p.stderr.strip()[-600:])
        return
    out = json.loads(p.stdout)
    check("every field a row reads is in the slim row", out["missing"] == [], str(out["missing"]))
    check("slim rows draw the panel exactly as whole tasks did", out["slim"] == out["full"],
          next((f"{a!r} / {b!r}" for a, b in zip(out["slim"].split("<div class=\"task\">"),
                                                  out["full"].split("<div class=\"task\">"))
                if a != b), "lengths differ")[:600])
    check("and the fixture has rows of every kind", all(
        x in out["slim"] for x in ("Review flagged", "yours to do", "Close", "landed abcdef12",
                                   "NO WORKTREE", 'id="more-' + prop["id"] + '"')))
    check("the panel asks for slim rows", (out["listAsk"] or {}).get("slim") is True,
          str(out["listAsk"]))
    check("nothing behind a title is drawn until it is opened",
          "THE LAST WORD" not in out["slim"] and "END OF FINDINGS" not in out["slim"])
    check("opening research asks for it whole and shows the findings",
          out["taskAsk"] == {"action": "task", "id": res["id"], "full": True}
          and "END OF FINDINGS" in out["opened"] and "findings" in out["opened"],
          out["opened"][:300])


LIVE_DRIVER = r"""
// The session list, transcript and polling, against a stub DOM that keeps real
// children and counts writes -- the point being what a poll does NOT touch.
// As a browser's, an element's innerHTML is its own markup and its children's,
// and setting it drops the children.
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
process.on("unhandledRejection", e => { console.error(e); process.exit(1); });
let writes = 0, appends = 0;
function el(id) {
  const o = {id, textContent: "", value: "", title: "", className: "", style: {}, disabled: false,
    dataset: {}, children: [], _inner: "", parent: null,
    get innerHTML() { return this._inner + this.children.map(c => c.innerHTML).join(""); },
    set innerHTML(v) {
      writes++; this._inner = v;
      for (const c of this.children) c.parent = null;
      this.children.length = 0;
    },
    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
    appendChild(c) {
      appends++;
      if (c.parent) c.parent.children.splice(c.parent.children.indexOf(c), 1);
      c.parent = this; this.children.push(c); return c;
    },
    remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; },
    addEventListener() {}, insertAdjacentHTML(_, h) { this._inner += h; }, focus() {},
    scrollIntoView() {}, querySelector() { return null; }, querySelectorAll() { return []; }};
  return o;
}
const els = {};
const byId = id => els[id] || (els[id] = el(id));
const listeners = {};
globalThis.document = {getElementById: byId, createElement: () => el("new"), hidden: false,
                       querySelector: () => null, querySelectorAll: () => [],
                       addEventListener(k, f) { listeners[k] = f; }, body: el("body")};
globalThis.window = {addEventListener() {}, location: {search: ""}};
const timers = [];
globalThis.setInterval = (f, ms) => timers.push([f, ms]);
globalThis.setTimeout = () => 0;
let clock = 1e12;
Date.now = () => clock;
const sess = (key, title, extra) => Object.assign({key, title, kind: "thread", session_id: "s-" + key,
  updated: clock / 1000 - 3 * 86400, turns: 1, cost: 0, resume_cmd: "x", slack_link: "y"}, extra || {});
let sessions = [sess("A:1", "alpha"), sess("B:2", "beta",
  {turn: {age_s: 10, child_alive: true, stalled: false, prompt: "go"}})];
let tag = '"v1"';
const asked = [];
let transcript = {};
globalThis.fetch = async (url, opts) => {
  const h = (opts && opts.headers) || {};
  asked.push([url, h["If-None-Match"] || null]);
  if (url.startsWith("/api/sessions")) {
    if (h["If-None-Match"] === tag) return {status: 304, headers: {get: k => k === "ETag" ? tag : null}};
    const out = {bot_online: true, sessions: JSON.parse(JSON.stringify(sessions)), slack: {}};
    return {status: 200, headers: {get: k => k === "ETag" ? tag : null}, json: async () => out};
  }
  if (url.startsWith("/api/session?")) {
    const from = +new URL("http://x" + url).searchParams.get("from");
    return {status: 200, headers: {get: () => null}, json: async () => transcript[from]};
  }
  let out = {ok: true};
  if (url.startsWith("/api/stats")) out = {total_cost: 0, cache_rate: null, threads: 0, days: [], models: []};
  if (url.startsWith("/api/artifacts")) out = [];
  return {status: 200, headers: {get: () => null}, json: async () => out};
};
(0, eval)(src + `;globalThis.__l = {loadList, loadTranscript, openSession,
  setActive(k) { active = lastSessions.find(s => s.key === k); }};`);
const tick = () => new Promise(r => setImmediate(r));
(async () => {
  const L = globalThis.__l, out = {};
  await L.loadList();
  const nav = byId("list");
  out.first = nav.children.map(c => c._inner.includes("alpha") ? "alpha" : "beta");
  const before = nav.children.slice();
  writes = 0; appends = 0;
  await L.loadList();                                    // 304: nothing changed
  out.unchanged = {writes, appends, same: nav.children.every((c, i) => c === before[i]),
                   conditional: asked.filter(a => a[0] === "/api/sessions").slice(-1)[0][1]};
  clock += 65000;                                        // a minute later, still 304
  writes = 0;
  await L.loadList();
  out.aged = {writes, text: nav.children[1]._inner.match(/running [^<]*/)[0]};
  // The server's answer moved on by the same minute the page counted.
  sessions[0].title = "alpha renamed"; sessions[1].turn.age_s = 75; tag = '"v2"';
  writes = 0; appends = 0;
  await L.loadList();
  out.renamed = {writes, appends, same: nav.children.every((c, i) => c === before[i]),
                 text: nav.children[0]._inner.includes("alpha renamed")};
  sessions = [sessions[1], sessions[0]]; tag = '"v3"';
  await L.loadList();
  out.reordered = nav.children.map(c => c._inner.includes("alpha") ? "alpha" : "beta");
  out.reusedOnReorder = nav.children[0] === before[1];
  // The transcript: read from 0, then only what is new, appended.
  transcript = {0: {messages: [{role: "user", ts: "", blocks: [{type: "text", text: "first msg"}]}], cursor: 40},
                40: {messages: [{role: "assistant", ts: "", blocks: [{type: "text", text: "second msg"}]}], cursor: 90},
                90: {messages: [], cursor: 90}};
  L.setActive("A:1");
  await L.loadTranscript(true);
  const msgs = byId("tmsgs");
  out.t1 = msgs.innerHTML;
  await L.loadTranscript(false);
  out.t2 = msgs.innerHTML;
  const headWrites = writes;
  await L.loadTranscript(false);
  out.t3 = msgs.innerHTML;
  out.transcriptAsks = asked.filter(a => a[0].startsWith("/api/session?")).map(a => a[0]);
  out.headStill = writes === headWrites;
  // A read that fails replaces the messages; the next one starts over from 0.
  transcript = {90: {error: "no transcript found for this session"},
                0: {messages: [{role: "user", ts: "", blocks: [{type: "text", text: "first msg"}]},
                               {role: "assistant", ts: "", blocks: [{type: "text", text: "second msg"}]}],
                    cursor: 90}};
  await L.loadTranscript(false);
  out.errored = msgs.innerHTML;
  await L.loadTranscript(false);
  out.recovered = msgs.innerHTML;
  out.recoverAsk = asked.filter(a => a[0].startsWith("/api/session?")).slice(-1)[0][0];
  // A call made while a read is in flight is run after it, not dropped.
  const k = asked.length;
  const first = L.loadTranscript(false);
  await L.loadTranscript(false);
  await first; await tick(); await tick();
  out.queued = asked.slice(k).filter(a => a[0].startsWith("/api/session?")).length;
  // Hidden: the timers fire and nothing is fetched.
  document.hidden = true;
  const n = asked.length;
  for (const [f] of timers) await f();
  await tick();
  out.hiddenFetches = asked.length - n;
  document.hidden = false;
  listeners.visibilitychange();
  await tick(); await tick();
  out.backFetches = asked.slice(n).map(a => a[0].split("?")[0]);
  out.intervals = timers.map(t => t[1]);
  process.stdout.write(JSON.stringify(out));
})();
"""


def test_dashboard_polls_without_redrawing():
    import re
    sys.argv = ["x"]
    import visualizer as V
    print("\nthe dashboard redraws only what changed, and not while hidden")
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    for name in ("readJSON", "setHTML", "renderList", "cardHtml", "freshAges", "poller",
                 "taskRead", "openMore", "moreToggled", "rowMore", "rowWhy", "taskBody",
                 "transcriptHead", "messageHtml", "drawerHtml", "openSession"):
        check(f"{name}() is defined", re.search(rf"function\s+{name}\b", js) is not None)
    check("the transcript's parts are in the page",
          all(f'id="{i}"' in V.PAGE for i in ("thead", "tmsgs", "tlive", "attdrawer", "imgview")))
    node = shutil.which("node")
    if not node:
        print("  … node not installed — cannot run the page's own javascript")
        return
    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    (d / "drive.js").write_text(LIVE_DRIVER)
    p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js")],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        check("the dashboard's javascript runs", False, p.stderr.strip()[-800:])
        return
    o = json.loads(p.stdout)
    check("the list draws newest first", o["first"] == ["alpha", "beta"], o["first"])
    u = o["unchanged"]
    check("a poll asks with the ETag it holds", u["conditional"] == '"v1"', u)
    check("an unchanged poll writes nothing and moves nothing",
          u["writes"] == 0 and u["appends"] == 0 and u["same"], u)
    check("a running turn keeps counting through 304s",
          o["aged"]["text"].startswith("running 1m") and o["aged"]["writes"] == 1, o["aged"])
    r = o["renamed"]
    check("a renamed thread rewrites its own card only, in place",
          r["text"] and r["writes"] == 1 and r["appends"] == 0 and r["same"], r)
    check("a reorder moves the same cards",
          o["reordered"] == ["beta", "alpha"] and o["reusedOnReorder"], o["reordered"])
    # By the thread's key: the server answers for the session it is on now.
    check("the transcript reads from its cursor",
          o["transcriptAsks"] == ["/api/session?key=A%3A1&id=&from=0", "/api/session?key=A%3A1&id=&from=40",
                                  "/api/session?key=A%3A1&id=&from=90"], o["transcriptAsks"])
    check("new messages are appended to what is there",
          "first msg" in o["t1"] and "first msg" in o["t2"] and "second msg" in o["t2"]
          and o["t3"] == o["t2"])
    check("an unchanged header is not redrawn", o["headStill"])
    check("a failed read says so", "no transcript found" in o["errored"])
    check("and the next read starts over, showing every message once",
          o["recoverAsk"].endswith("&from=0") and o["recovered"].count("first msg") == 1
          and "second msg" in o["recovered"] and "no transcript" not in o["recovered"],
          o["recoverAsk"])
    check("a read asked for during another is run after it", o["queued"] == 2, o["queued"])
    check("a hidden tab fetches nothing", o["hiddenFetches"] == 0, o["hiddenFetches"])
    check("showing it again refreshes at once",
          "/api/sessions" in o["backFetches"] and "/api/stats" in o["backFetches"]
          and "/api/session" in o["backFetches"], o["backFetches"])
    # 1500: the open thread while something is under way in it (threadBusy);
    # 60000: the Trader view, which only reads while it is open. The third
    # 5000 is the Needs you inbox, the home screen (loadInbox); 2500 the
    # operator's plan card, read every fourth tick unless it is running.
    check("the polling cadence is unchanged", sorted(o["intervals"]) == [1500, 2500, 4000, 5000, 5000, 5000, 30000, 60000],
          o["intervals"])


# --- the Trader view: read-only, drawn from trader's own status document ------
# trader's scripts/trader_status.py prints one JSON document; /api/trader runs
# it (20s limit, cached 60s, ETag/304) and the page only draws it. A failed run
# says why beside the last good document -- never a blank page -- and nothing
# on the view posts anything.

TRADER_FIXTURE = BASE / "tests" / "fixtures" / "trader_status.json"


def _trader_reset(V):
    V._trader_cache.update(at=None, answer=None, good=None, good_at=None)


def _fake_trader(script: str) -> str:
    """A trader checkout whose .venv/bin/python3 is the shell `script`."""
    root = Path(tempfile.mkdtemp())
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "scripts" / "trader_status.py").write_text("# stand-in\n")
    py = root / ".venv" / "bin" / "python3"
    py.write_text("#!/bin/sh\n" + script + "\n")
    py.chmod(0o755)
    return str(root)


def test_trader_route_runs_caches_and_times_out():
    sys.argv = ["x"]
    import visualizer as V
    print("\n/api/trader runs trader's status script, caches it a minute, and times out")
    check("the limits: 20s to run, cached just under the page's 60s poll",
          V.TRADER_TIMEOUT_S == 20 and V.TRADER_TTL_S == 55)
    good = json.loads(TRADER_FIXTURE.read_text())
    # The real runner, against stand-in checkouts.
    doc, why = V.run_trader_status(_fake_trader(f"exec cat '{TRADER_FIXTURE}'"))
    check("a good run hands back the document", doc == good and why == "", why)
    argv_log = Path(tempfile.mkdtemp()) / "argv"
    cwd = _fake_trader(f"echo \"$PWD $*\" > '{argv_log}'; exec cat '{TRADER_FIXTURE}'")
    V.run_trader_status(cwd)
    # No --json: JSON is all trader 8d47517's script prints, and it refuses the flag.
    check("it runs <cwd>/.venv/bin/python3 scripts/trader_status.py, no flags, in the checkout",
          argv_log.read_text().split() == [str(Path(cwd).resolve()), "scripts/trader_status.py"]
          or argv_log.read_text().split() == [cwd, "scripts/trader_status.py"],
          argv_log.read_text())
    # A hang in a child of the script: the whole group goes, not just the script.
    t0 = time.monotonic()
    doc, why = V.run_trader_status(_fake_trader("sleep 30 & sleep 30; wait"), timeout=0.5)
    took = time.monotonic() - t0
    check("a run past the limit is a failure that says so",
          doc is None and why == "scripts/trader_status.py timed out after 0.5s", why)
    check("and returns at the limit, even with a child holding the pipe", took < 4, took)
    for script, want in [("echo boom >&2; exit 3", "exited 3: boom"),
                         ("echo not json", "did not print JSON"),
                         ("echo '{\"v\": 2, \"sections\": {}}'", "cannot read (v=2)")]:
        doc, why = V.run_trader_status(_fake_trader(script))
        check(f"a bad run is named: {want}", doc is None and want in why, why)
    doc, why = V.run_trader_status(str(Path(tempfile.mkdtemp())))
    check("a checkout without the script says it is not there yet",
          doc is None and "scripts/trader_status.py is not in" in why, why)
    doc, why = V.run_trader_status("")
    check("no trader project says so", doc is None and "projects.json" in why, why)
    doc, why = V.run_trader_status(_fake_trader("printf '\\377\\376' >&2; exit 1"))
    check("output that is not UTF-8 is a named failure, not a crash",
          doc is None and "exited 1" in why, why)
    saved_base = V.BASE_DIR
    try:
        for shape in ({"trader": {"scope": "x"}}, {"trader": "x"}, ["trader"],
                      {"trader": {"scope": {"cwd": 3}}}, {}):
            V.BASE_DIR = Path(tempfile.mkdtemp())
            (V.BASE_DIR / "projects.json").write_text(json.dumps(shape))
            check(f"an oddly shaped projects.json ({json.dumps(shape)}) is no folder, not a crash",
                  V.trader_cwd() == "")
        (V.BASE_DIR / "projects.json").write_text(json.dumps({"trader": {"scope": {"cwd": "/t"}}}))
        check("and the trader project's cwd is read from it", V.trader_cwd() == "/t")
    finally:
        V.BASE_DIR = saved_base

    # The cache: one run per minute, failures included, last good kept.
    runs, results, now = [], [], [1000.0]

    def fake_run(cwd):
        runs.append(cwd)
        return results.pop(0)
    _trader_reset(V)
    try:
        results[:] = [(good, ""), (None, "timed out after 20s"), (good, "")]
        a1 = V.trader_status(run=fake_run, clock=lambda: now[0])
        now[0] += 30
        a2 = V.trader_status(run=fake_run, clock=lambda: now[0])
        check("a second ask within the minute is the cached answer, not a second run",
              len(runs) == 1 and a2 is a1 and a1["ok"] and a1["doc"] == good, runs)
        now[0] += 26
        a3 = V.trader_status(run=fake_run, clock=lambda: now[0])
        check("after a minute it runs again", len(runs) == 2)
        check("a failed run carries its reason, the last good document and when it was fetched",
              not a3["ok"] and a3["error"] == "timed out after 20s"
              and a3["last_good"] == good and a3["last_good_at"] == 1000.0, a3)
        now[0] += 10
        V.trader_status(run=fake_run, clock=lambda: now[0])
        check("a failure is cached too: a broken script is not re-run on every poll", len(runs) == 2)
        now[0] += 60
        a5 = V.trader_status(run=fake_run, clock=lambda: now[0])
        check("and a later good run replaces it", len(runs) == 3 and a5["ok"])

        # Over HTTP: ETag and 304, like /api/sessions; GET only.
        import threading as _th
        import urllib.request as _u
        import urllib.error as _ue
        saved = V.run_trader_status
        V.run_trader_status = lambda cwd: (good, "")
        _trader_reset(V)
        srv = V.make_server("127.0.0.1", 0)
        _th.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            with _u.urlopen(base + "/api/trader", timeout=10) as r:
                tag, body = r.headers.get("ETag"), json.loads(r.read())
            check("GET /api/trader answers the document with an ETag",
                  body["ok"] and body["doc"] == good and tag, tag)
            req = _u.Request(base + "/api/trader", headers={"If-None-Match": tag})
            try:
                _u.urlopen(req, timeout=10)
                status = 200
            except _ue.HTTPError as e:
                status = e.code
            check("asked again with that ETag it is a 304", status == 304, status)
            req = _u.Request(base + "/api/trader", data=b"{}", method="POST",
                             headers={"Content-Type": "application/json"})
            try:
                _u.urlopen(req, timeout=10)
                status = 200
            except _ue.HTTPError as e:
                status = e.code
            check("there is no POST /api/trader", status == 404, status)
        finally:
            srv.shutdown()
            V.run_trader_status = saved
    finally:
        _trader_reset(V)
    tree = ast.parse((BASE / "visualizer.py").read_text())
    post = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "do_POST")
    check("do_POST names no trader route", "trader" not in ast.unparse(post))


TRADER_DRIVER = r"""
// Draws /api/trader answers with the page's own functions against a stub DOM,
// and records every request the view makes.
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const fixture = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
process.on("unhandledRejection", e => { console.error(e); process.exit(1); });
function el(id) {
  return {id, textContent: "", value: "", innerHTML: "", style: {}, dataset: {}, children: [],
    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
    appendChild(c) { return c; }, addEventListener() {}, querySelector() { return null; },
    querySelectorAll() { return []; }, focus() {}};
}
const els = {};
const byId = id => els[id] || (els[id] = el(id));
globalThis.document = {getElementById: byId, createElement: () => el("new"), hidden: false,
  querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, body: el("body")};
const winListeners = {};
globalThis.window = {addEventListener(k, f) { winListeners[k] = f; }, location: {search: ""}};
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
const asked = [];
let answer = null;
globalThis.fetch = async (url, opts) => {
  asked.push({url, method: (opts && opts.method) || "GET", body: !!(opts && opts.body)});
  if (url === "/api/trader") return {status: 200, headers: {get: () => null}, json: async () => answer};
  let out = {ok: true};
  if (url.startsWith("/api/sessions")) out = {sessions: [], slack: {}};
  if (url.startsWith("/api/stats")) out = {total_cost: 0, cache_rate: null, threads: 0, days: [], models: []};
  return {status: 200, headers: {get: () => null}, json: async () => out};
};
(0, eval)(src + `;globalThis.__t = {traderHtml, toggleTrader, refreshTrader, traderIsOpen, TRADER_WATCH, openLink};`);
const T = globalThis.__t, tick = () => new Promise(r => setImmediate(r));
const now = Date.parse("2026-11-18T15:10:00-08:00") / 1000;
const unknownDoc = {v: 1, generated_at: null, verdict: "unknown", sections: Object.fromEntries(
  ["summary", "sleeves", "gate", "universe", "dispatches", "fills", "jobs", "freshness", "health",
   "host", "shadow", "env"].map(k => [k, {status: "unknown", reason: k + " source unreadable", source: null, as_of: null}]))};
(async () => {
  const out = {};
  out.good = T.traderHtml({ok: true, doc: fixture, fetched_at: now}, now);
  out.unknown = T.traderHtml({ok: true, doc: unknownDoc, fetched_at: now}, now);
  out.empty = T.traderHtml({ok: true, doc: {v: 1, sections: {}}, fetched_at: now}, now);
  // Its age is the document's own (generated_at), three hours before.
  out.failedWithGood = T.traderHtml({ok: false, error: "scripts/trader_status.py timed out after 20s",
    last_good: fixture, last_good_at: now - 60}, Date.parse(fixture.generated_at) / 1000 + 3 * 3600);
  out.failedNoGood = T.traderHtml({ok: false, error: "scripts/trader_status.py is not in /x yet",
    last_good: null, last_good_at: null}, now);
  out.nothing = T.traderHtml(null, now);
  // A tile may also come from its section's own watch map.
  const sec = JSON.parse(JSON.stringify(fixture));
  sec.watch = sec.watch.filter(w => w.id !== "guards");
  sec.sections.sleeves.watch = {guards: {status: "bad", label: "halted", reason: "halt marker present"}};
  out.sectionWatch = T.traderHtml({ok: true, doc: sec}, now);
  // Document strings are text, never markup.
  const evil = '<img src=x onerror=alert(1)>"\'';
  const bad = JSON.parse(JSON.stringify(fixture));
  bad.verdict.headline = evil; bad.verdict.lines = [evil]; bad.watch[0].label = evil;
  bad.watch[0].reason = evil; bad.sections.sleeves.sleeves[0].label = evil;
  bad.sections.summary.doing = [evil];
  out.escaped = T.traderHtml({ok: true, doc: bad}, now) + T.traderHtml({ok: false, error: evil}, now);
  out.watchIds = T.TRADER_WATCH.map(w => w[0]);
  // Opened by its deep link, then polled and refocused: every request a read.
  answer = {ok: true, doc: fixture, fetched_at: now};
  for (let i = 0; i < 20; i++) await tick();          // the page's own first reads settle
  asked.length = 0;
  await T.openLink(new URLSearchParams("view=trader"));
  await tick(); await tick();
  out.openedByLink = T.traderIsOpen();
  out.drawn = byId("trbody").innerHTML.includes("What to watch");
  answer = {ok: false, error: "scripts/trader_status.py exited 1: boom", last_good: null};
  if (winListeners.focus) winListeners.focus();
  await tick(); await tick();
  out.afterFocus = byId("trbody").innerHTML;
  out.asked = asked.filter(a => a.url === "/api/trader" || a.method !== "GET" || a.body);
  console.log(JSON.stringify(out));
})();
"""


def test_trader_view_draws_the_document():
    import re
    sys.argv = ["x"]
    import visualizer as V
    print("\nthe Trader view draws trader's status document, and posts nothing")
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    for name in ("traderHtml", "toggleTrader", "refreshTrader", "traderIsOpen", "trBadge"):
        check(f"{name}() is defined", re.search(rf"function\s+{name}\b", js) is not None)
    check("the header has a Trader entry", 'onclick="toggleTrader()"' in V.PAGE
          and "Trader</span></button>" in V.PAGE)
    check("?view=trader opens it", 'view === "trader"' in js)
    check("it is polled every minute while open, and on focus and on showing the tab",
          "const TRADER_POLL_MS = 60000;" in js and "setInterval(pollTrader, TRADER_POLL_MS)" in js
          and "traderIsOpen() ? refreshTrader()" in js and 'addEventListener("focus"' in js
          and "pollBoard(); pollTrader(); pollInbox(); pollPlan(); }" in js)
    # Nothing on the view posts: its markup's buttons are its close button and
    # the five tabs (which only change what is drawn), and it has no form.
    modal = re.search(r'<div id="tradermodal".*?\n</div>\n', V.PAGE, re.S).group(0)
    handlers = re.findall(r'<button[^>]*onclick="([^"]*)"', modal)
    check("the view's markup has no form, no input and only its close button and tabs",
          "<form" not in modal and "<input" not in modal and "<select" not in modal
          and modal.count("<button") == 6 and handlers == ["toggleTrader()"] + [
              f"trTab('{t}')" for t in ("summary", "sleeves", "activity", "plumbing", "shadow")], modal)
    block = js[js.index("// --- trader ---"):js.index("// --- projects overview")]
    check("the view's script posts nothing and calls no action",
          "POST" not in block and "taskCall" not in block and "taskAction" not in block
          and "bot_call" not in block and block.count("fetch(") == 0
          and block.count('readJSON("/api/trader")') == 1, "")
    node = shutil.which("node")
    if not node:
        print("  … node not installed — cannot run the page's own javascript")
        return
    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    (d / "drive.js").write_text(TRADER_DRIVER)
    p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js"), str(TRADER_FIXTURE)],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        check("the trader view's javascript runs", False, p.stderr.strip()[-800:])
        return
    o = json.loads(p.stdout)
    g = o["good"]
    ICON = '<span aria-hidden="true">'
    check("the verdict banner is drawn with its icon and words",
          'class="tr-verdict warn"' in g and ICON + '!</span> look at</span> Trading normally, with 3 things to look at</h3>' in g
          and "<b>Look at:</b>" in g, g[:400])
    check("the book card shows the total and each sleeve's allocation, net liq, cash and holdings",
          "$196,999" in g and "Momentum" in g and "75% · 1.0x" in g and "$146,848" in g
          and "100%" in g and "all cash since Nov 2" in g and "91%" in g and "VNQ" in g)
    check("drawdown meters against the 40% halt, with the 34% target tick",
          "Drawdown vs the 40% halt" in g and "22.9% from peak $190,510" in g
          and "17.1pp to the halt" in g and "36.5pp to the halt" in g
          and g.count("<u style=\"left:68.0%\">") == 2 and g.count("<b style=\"left:80.0%\">") == 2, "")
    check("the plain-words card says what it is doing",
          "What it's doing" in g and "credit gate" in g and "Dec 1 rebalance" in g)
    tiles = re.findall(r'<div class="tr-w" data-watch="(\w+)">(.*?)</span></div>', g)
    check("all ten watch tiles, in the audit's order",
          [t[0] for t in tiles] == o["watchIds"] and len(tiles) == 10, [t[0] for t in tiles])
    icons = {"ok": "✓", "warn": "!", "bad": "✕", "unknown": "?"}
    labelled = all(re.search(r'class="tr-st (ok|warn|bad|unknown)"[^>]*><span aria-hidden="true">([✓!✕?])</span> \S', h)
                   and icons[re.search(r'class="tr-st (\w+)"', h).group(1)] in h for _, h in tiles)
    check("every tile has an icon and a label, never colour alone", labelled)
    check("tiles take their own entries", ICON + "!</span> restart pending" in g and ICON + "✕</span> trades.db 93d old" in g
          and ICON + "✓</span> none tripped" in g)
    check("each fixture tile has its own entry (none 'not reported')", "not reported" not in g)
    check("a tile can also come from its section's watch map",
          ICON + "✕</span> halted" in o["sectionWatch"] and "not reported" not in o["sectionWatch"])
    u = o["unknown"]
    check("an all-unknown document still draws every card and all ten tiles",
          "Status unknown" in u and u.count('data-watch="') == 10 and "Book" in u
          and "Drawdown vs the 40% halt" in u and "What it's doing" in u)
    check("its tiles say unknown with a ?, and what their section says",
          u.count('<span class="tr-st unknown"') >= 12 and u.count(ICON + "?</span> not reported") == 10
          and "host: host source unreadable" in u, "")
    check("unknown numbers are shown as unknown, never a default figure",
          "$0" not in u and "NaN" not in u and "undefined" not in u and "null" not in u
          and "—" in u and "no sleeve data" in u)
    check("an empty document draws too, with no NaN, undefined or null",
          all(x not in o["empty"] for x in ("NaN", "undefined", "null")) and o["empty"].count('data-watch="') == 10)
    f = o["failedWithGood"]
    check("a failed run says 'status unavailable: <reason>' and how old the last good document is",
          ICON + "?</span> status unavailable:</span> scripts/trader_status.py timed out after 20s" in f
          and "last good document, 3h old" in f and 'class="tr-old"' in f and "$196,999" in f, f[:600])
    n = o["failedNoGood"]
    check("with no good document yet it still says why, not a blank page",
          '<span aria-hidden="true">?</span> status unavailable:</span> scripts/trader_status.py is not in /x yet' in n
          and "No good document since the dashboard started" in n)
    check("even no answer at all is drawn", "status unavailable" in o["nothing"])
    check("document strings are escaped, never markup",
          "<img" not in o["escaped"] and o["escaped"].count("&lt;img src=x onerror=alert(1)&gt;&quot;") == 7,
          o["escaped"].count("&lt;img"))
    check("?view=trader opens the view and draws it", o["openedByLink"] and o["drawn"])
    check("focus refreshes it, keeping the last good document beside the failure",
          "exited 1: boom" in o["afterFocus"] and "$196,999" in o["afterFocus"], o["afterFocus"][:300])
    check("every request the view made was a GET of /api/trader, with no body",
          o["asked"] and all(a == {"url": "/api/trader", "method": "GET", "body": False} for a in o["asked"]),
          o["asked"])
    for html in (g, u, f, n):
        if "onclick" in html or "<button" in html or "<form" in html:
            check("a drawn document has no buttons, forms or handlers", False, html[:300])
            break
    else:
        check("a drawn document has no buttons, forms or handlers", True)


TRADER_TABS_DRIVER = r"""
// Draws each Trader tab with the page's own functions against a stub DOM:
// from the fixture, from a document missing sections, from an all-unknown
// one, and through the deep link and the tab buttons -- recording requests.
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const fixture = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
process.on("unhandledRejection", e => { console.error(e); process.exit(1); });
function el(id) {
  return {id, textContent: "", value: "", innerHTML: "", style: {}, dataset: {}, children: [], className: "",
    attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; },
    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
    appendChild(c) { return c; }, addEventListener(k, f) { (this.on = this.on || {})[k] = f; },
    querySelector() { return null; }, querySelectorAll() { return []; }, focus() {}};
}
const els = {};
const byId = id => els[id] || (els[id] = el(id));
globalThis.document = {getElementById: byId, createElement: () => el("new"), hidden: false,
  querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, body: el("body")};
globalThis.window = {addEventListener() {}, location: {search: ""}, innerWidth: 1280};
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
const asked = [];
globalThis.fetch = async (url, opts) => {
  asked.push({url, method: (opts && opts.method) || "GET", body: !!(opts && opts.body)});
  if (url === "/api/trader") return {status: 200, headers: {get: () => null}, json: async () => ({ok: true, doc: fixture})};
  let out = {ok: true};
  if (url.startsWith("/api/sessions")) out = {sessions: [], slack: {}};
  if (url.startsWith("/api/stats")) out = {total_cost: 0, cache_rate: null, threads: 0, days: [], models: []};
  return {status: 200, headers: {get: () => null}, json: async () => out};
};
(0, eval)(src + `;globalThis.__t = {traderHtml, trTab, trShowTip, openLink, traderIsOpen, TRADER_TABS};`);
const T = globalThis.__t, tick = () => new Promise(r => setImmediate(r));
const now = Date.parse("2026-11-18T15:10:00-08:00") / 1000;
const TABS = ["sleeves", "activity", "plumbing", "shadow"];
const clone = x => JSON.parse(JSON.stringify(x));
// Missing sections: the whole of gate, dispatches, jobs and shadow are gone,
// and the rest keep only their status and reason (no drill-down fields).
const missing = clone(fixture);
for (const k of ["gate", "dispatches", "jobs", "shadow"]) delete missing.sections[k];
for (const k of Object.keys(missing.sections)) {
  const s = missing.sections[k];
  missing.sections[k] = {status: s.status, reason: s.reason, source: s.source, as_of: s.as_of};
}
// ...except the sleeves themselves, which keep their summary figures but lose
// their history and positions.
missing.sections.sleeves.sleeves = fixture.sections.sleeves.sleeves.map(r => {
  const c = clone(r); delete c.history; delete c.positions; delete c.state_as_of; return c; });
const unknownDoc = {v: 1, generated_at: null, verdict: "unknown", sections: Object.fromEntries(
  ["summary", "sleeves", "gate", "universe", "dispatches", "fills", "jobs", "freshness", "health",
   "host", "shadow", "env"].map(k => [k, {status: "unknown", reason: k + " source unreadable", source: null, as_of: null}]))};
const evil = '<img src=x onerror=alert(1)>"';
const bad = clone(fixture), S = bad.sections;
S.sleeves.sleeves[0].history[0].date = evil; S.sleeves.sleeves[1].positions[0].symbol = evil;
S.universe.going_in[0] = evil; S.dispatches.events[0].text = evil; S.fills.rejections[0].text = evil;
S.jobs.jobs[0].label = evil; S.env.keys[0].value = evil; S.shadow.blend.reason = evil; S.health.checks[0] = evil;
(async () => {
  const out = {good: {}, missing: {}, unknown: {}, empty: {}, failed: {}, escaped: {}};
  for (const t of TABS) {
    out.good[t] = T.traderHtml({ok: true, doc: fixture}, now, t);
    out.missing[t] = T.traderHtml({ok: true, doc: missing}, now, t);
    out.unknown[t] = T.traderHtml({ok: true, doc: unknownDoc}, now, t);
    out.empty[t] = T.traderHtml({ok: true, doc: {v: 1, sections: {}}}, now, t);
    out.failed[t] = T.traderHtml({ok: false, error: "timed out", last_good: fixture}, now, t);
    out.escaped[t] = T.traderHtml({ok: true, doc: bad}, now, t);
  }
  out.bogusTab = T.traderHtml({ok: true, doc: fixture}, now, "nope");
  // Odd documents the review turned up.
  const noWin = clone(fixture); delete noWin.sections.gate.window;
  out.noWindow = T.traderHtml({ok: true, doc: noWin}, now, "sleeves");
  out.fieldGone = T.traderHtml({ok: true, doc: {v: 1, sections: {fills: {status: "ok", fills: []},
    dispatches: {status: "ok", slots: []}}}}, now, "activity");
  const odd = clone(fixture), sl = odd.sections.sleeves.sleeves;
  sl[0].history = [{date: "a", net_liq: 100, drawdown: 0.1}, {date: "b", net_liq: 101}];
  sl[1].history = [{date: "c", net_liq: null}, {date: "d", net_liq: null}];
  sl[1].positions = [{symbol: "Z", qty: 1, avg_cost: 1, mark: 1, upnl: -0.4}];
  out.odd = T.traderHtml({ok: true, doc: odd}, now, "sleeves");
  out.summary = T.traderHtml({ok: true, doc: fixture}, now);
  for (let i = 0; i < 20; i++) await tick();
  asked.length = 0;
  await T.openLink(new URLSearchParams("view=trader&tab=plumbing"));
  for (let i = 0; i < 4; i++) await tick();
  out.linkOpen = T.traderIsOpen();
  out.linkBody = byId("trbody").innerHTML;
  out.linkTabClass = byId("trtab-plumbing").className + "|" + byId("trtab-summary").className;
  const before = asked.length;
  T.trTab("shadow");
  out.afterTab = byId("trbody").innerHTML;
  out.tabAsked = asked.length - before;
  T.trTab("bogus");
  out.bogusAfter = byId("trbody").innerHTML;
  // The tooltip shows the words of the point under the pointer.
  const pt = el("pt"); pt.attrs["data-tip"] = "2026-11-16 · $146,848";
  const target = {closest: sel => sel === "[data-tip]" ? pt : null};
  T.trShowTip(target, 100, 200);
  out.tip = {text: byId("trtip").textContent, display: byId("trtip").style.display};
  T.trShowTip({closest: () => null}, 0, 0);
  out.tipHidden = byId("trtip").style.display;
  // A phone tap: no pointermove, pointerleave on lift, then click.
  const tb = byId("trbody");
  out.tapWired = !!(tb.on && tb.on.click);
  if (out.tapWired) {
    tb.on.pointerleave({pointerType: "touch"});
    tb.on.click({target, clientX: 50, clientY: 60});
    out.tapTip = {text: byId("trtip").textContent, display: byId("trtip").style.display};
    tb.on.pointerleave({pointerType: "touch"});
    out.tapStays = byId("trtip").style.display;
    tb.on.click({target: {closest: () => null}, clientX: 0, clientY: 0});
    out.tapOff = byId("trtip").style.display;
    T.trShowTip(target, 100, 200);
    tb.on.pointerleave({pointerType: "mouse"});
    out.mouseLeave = byId("trtip").style.display;
  }
  out.asked = asked.filter(a => a.url === "/api/trader" || a.method !== "GET" || a.body);
  console.log(JSON.stringify(out));
})();
"""


def test_trader_tabs_draw_each_tab():
    import re
    sys.argv = ["x"]
    import visualizer as V
    print("\nthe Trader view's four drill-down tabs draw from the same document, and act on nothing")
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    for name in ("trSleevesTab", "trActivityTab", "trPlumbingTab", "trShadowTab", "trChart", "trTab", "trShowTip"):
        check(f"{name}() is defined", re.search(rf"function\s+{name}\b", js) is not None)
    check("?view=trader&tab= picks the tab", 'traderTab = trTabName(q.get("tab") || "")' in js)
    check("bin/dashshots photographs each tab",
          all(f'("trader-{t}", "/?view=trader&tab={t}")' in (BASE / "bin" / "dashshots").read_text()
              for t in ("sleeves", "activity", "plumbing", "shadow")))
    node = shutil.which("node")
    if not node:
        print("  … node not installed — cannot run the page's own javascript")
        return
    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    (d / "drive.js").write_text(TRADER_TABS_DRIVER)
    # Times are drawn in the device's zone; pin it so the checks don't move.
    p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js"), str(TRADER_FIXTURE)],
                       capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, TZ="America/Los_Angeles"))
    if p.returncode != 0:
        check("the trader tabs' javascript runs", False, p.stderr.strip()[-800:])
        return
    o = json.loads(p.stdout)
    fx = json.loads(TRADER_FIXTURE.read_text())
    ICON = '<span aria-hidden="true">'
    g = o["good"]

    s = g["sleeves"]
    mom = fx["sections"]["sleeves"]["sleeves"][0]
    nans = sum(1 for h in mom["history"] if h["net_liq"] is None)
    check("sleeves: a net-liq and a drawdown chart per sleeve",
          s.count('<svg class="tr-chart"') == 5 and 'aria-label="Momentum net liq"' in s
          and 'aria-label="Trend drawdown"' in s, s.count("<svg"))
    momcard = s[s.index("<b>Momentum</b>"):s.index("<b>Trend</b>")]
    check("sleeves: NaN rows are marked, in the chart and as a badge",
          momcard.count('<rect class="nan"') == nans * 2 and f"{nans} NaN rows" in momcard
          and "NaN row (no value recorded)" in momcard, momcard.count('class="nan"'))
    check("sleeves: the drawdown chart draws the 40% halt line",
          s.count('<line class="ref"') == 2 and s.count("halt −40%") >= 2)
    check("sleeves: every point has a hover tooltip, and each chart a table",
          'data-tip="2026-11-16 · ' in s and "<title>" in s and s.count('<details class="tr-tbl"><summary>table</summary>') == 3)
    check("sleeves: the numbers, positions and an all-cash sleeve",
          "$146,848" in s and "−22.9%" in s and "no positions: all cash" in s
          and "<td>VNQ</td><td class=\"r\">45</td><td class=\"r\">95.19</td><td class=\"r\">95.36</td><td class=\"r\">+8</td>" in s)
    check("sleeves: the credit gate's chart, state and the date it last flipped",
          'aria-label="credit gate ratio"' in s and ICON + "✓</span> ON (risk-on)" in s and "since 2026-11-12" in s
          and "100-day avg" in s and '<path class="l2"' in s)
    check("sleeves: the universe, running vs disk, and the names in and out",
          ICON + "!</span> running ≠ disk" in s and "daemon loaded <b>static</b>" in s and "disk says <b>pit</b>" in s
          and "<b>16 in, 16 out</b>" in s and s.count('class="tr-chip in"') == 16 and s.count('class="tr-chip out"') == 16
          and "+MRVL" in s and "−SMCI" in s)

    a = g["activity"]
    check("activity: a row per dispatch slot",
          a[:a.index("Fills (journal)")].count('<tr><td class="tr-when">') == len(fx["sections"]["dispatches"]["slots"]) - 1
          and ICON + "✓</span> 62/62" in a and "<td class=\"tr-when\">Nov 18 13:00</td><td>close</td>" in a)
    check("activity: a skipped slot is its own red row, with words",
          a.count('<tr class="tr-skip">') == 1 and ICON + "✕</span> SKIPPED" in a)
    check("activity: journal fills, rejections and trims, and daemon events",
          "Fills (journal)" in a and "<td>DBC</td>" in a and "29.52" in a
          and ICON + "✕</span> rejected" in a and ICON + "!</span> trimmed" in a and "Insufficient buying power" in a
          and "Daemon events" in a and ICON + "!</span> WARNING" in a and "Runner started" in a)

    pl = g["plumbing"]
    check("plumbing: daemon, code, .env, gateway, host, backups and last page",
          "PID 84984" in pl and "restart pending" in pl and "not loaded" in pl
          and ICON + "✓</span> 127.0.0.1:4002 listening" in pl and "140 GB free" in pl
          and ICON + "✕</span> trades.db 93d" in pl and "none off-box" in pl and "via ntfy" in pl)
    check("plumbing: bar freshness split ETFs vs pool names",
          "ETFs (dataupdate)" in pl and "Momentum names (pooldata)" in pl and ICON + "!</span> 1 session late" in pl
          and "Integrity:" in pl)
    check("plumbing: the launchd job table with schedule, last ran, exit and missed",
          "<th>schedule</th><th>last ran</th><th>exit</th><th>missed</th>" in pl
          and ICON + "✕</span> exit 1" in pl and ICON + "✓</span> running" in pl and "wkdays 13:30" in pl
          and pl.count("<td>no</td>") >= 10 and "no log" in pl)
    runs = fx["sections"]["health"]["runs"]
    cells = len(runs) * len(fx["sections"]["health"]["checks"])
    check("plumbing: the health-check history grid, a glyph in every cell",
          pl.count('<span class="tr-hc ') == cells and '<span class="tr-hc bad"' in pl and "✕</span>" in pl
          and f"Last {len(runs)} runs" in pl, pl.count("tr-hc "))
    check("plumbing: .env shows non-secret values, secrets only as (set)",
          "MOMENTUM_UNIVERSE" in pl and ">pit<" in pl and pl.count(">(set)<") == 3)

    sh = g["shadow"]
    check("shadow: C1 vs its backtest, with its stats",
          'aria-label="C1 shadow vs backtest"' in sh and '<path class="l2"' in sh and "−2.5%" in sh
          and "16.7%" in sh and "full backtest 2008→" in sh and "correlation 0.98" in sh)
    check("shadow: the blend shadow's state, with words",
          ICON + "✕</span> crashing nightly since Oct 26" in sh and "Decimal/float" in sh)

    for kind in ("missing", "unknown", "empty"):
        for t, html in o[kind].items():
            check(f"a {kind} document draws the {t} tab, with no NaN, undefined, null or $0",
                  len(html) > 100 and all(x not in html for x in ("NaN<", ">NaN", "undefined", "null", "$0"))
                  and "not reported" in html, html[:300])
    m = o["missing"]
    check("missing sections say so, with the section's own reason",
          "gate not reported</span> no gate section" in m["sleeves"]
          and "dispatch slots not reported</span> no dispatches section" in m["activity"]
          and "launchd jobs not reported</span> no jobs section" in m["plumbing"]
          and "C1 shadow not reported</span> no shadow section" in m["shadow"]
          and "fills not reported</span> fills: " + fx["sections"]["fills"]["reason"] in m["activity"]
          and "history not reported" in m["sleeves"] and "positions not reported" in m["sleeves"])
    check("a failed run draws the tab from the last good document, dimmed",
          all("status unavailable" in o["failed"][t] and 'class="tr-old"' in o["failed"][t] for t in o["failed"])
          and "PID 84984" in o["failed"]["plumbing"])
    check("a gate without its window invents no 100-day figure",
          "100-day" not in o["noWindow"] and "moving avg (window not reported)" in o["noWindow"], "")
    check("a field missing from a present section is not called a missing section",
          "no fills section" not in o["fieldGone"] and "no dispatches section" not in o["fieldGone"]
          and "rejections and trims not reported</span> the fills section has no such field" in o["fieldGone"])
    od = o["odd"]
    mom_odd = od[od.index("<b>Momentum</b>"):od.index("<b>Trend</b>")]
    trend_odd = od[od.index("<b>Trend</b>"):od.index("Credit gate")]
    check("a row with net liq but no drawdown is not marked NaN",
          'class="nan"' not in mom_odd and "b · not reported" in mom_odd and "NaN row" not in mom_odd, "")
    check("all-NaN history says so without claiming red marks on a chart with none",
          "2 NaN rows</span></p>" in trend_odd and "marked red" not in trend_odd)
    check("a value rounding to zero shows no sign", '<td class="r">0</td></tr>' in trend_odd and "−0" not in od)
    check("an unknown tab name draws the summary", o["bogusTab"] == o["summary"] and "What to watch" in o["summary"])
    esc_ok = all("<img" not in h for h in o["escaped"].values())
    n_esc = sum(h.count("&lt;img src=x onerror=alert(1)&gt;&quot;") for h in o["escaped"].values())
    check("document strings are escaped on every tab, never markup", esc_ok and n_esc >= 9, n_esc)
    for t, html in g.items():
        if "onclick" in html or "<button" in html or "<form" in html or "<input" in html:
            check("a drawn tab has no buttons, forms, inputs or handlers", False, t)
            break
    else:
        check("a drawn tab has no buttons, forms, inputs or handlers", True)
    check("?view=trader&tab=plumbing opens that tab", o["linkOpen"] and "Launchd jobs" in o["linkBody"]
          and o["linkTabClass"] == "tr-tab on|tr-tab", o["linkTabClass"])
    check("a tab button redraws from the answer held, asking nothing",
          "Blend shadow" in o["afterTab"] and o["tabAsked"] == 0 and "What to watch" in o["bogusAfter"])
    check("the tooltip shows the point's words, and hides off a point",
          o["tip"] == {"text": "2026-11-16 · $146,848", "display": "block"} and o["tipHidden"] == "none", o["tip"])
    check("on a phone a tap shows the tip, the lift's pointerleave keeps it, tapping off hides it",
          o.get("tapWired") and o.get("tapTip") == {"text": "2026-11-16 · $146,848", "display": "block"}
          and o.get("tapStays") == "block" and o.get("tapOff") == "none",
          {k: o.get(k) for k in ("tapWired", "tapTip", "tapStays", "tapOff")})
    check("a mouse leaving the charts still hides the tip", o.get("mouseLeave") == "none", o.get("mouseLeave"))
    check("every request was a GET of /api/trader, with no body",
          o["asked"] and all(x == {"url": "/api/trader", "method": "GET", "body": False} for x in o["asked"]), o["asked"])


def test_dashshots_has_a_trader_view_fed_by_the_fixture():
    print("\nbin/dashshots photographs the trader view from the fixture")
    src = (BASE / "bin" / "dashshots").read_text()
    check("a trader view at ?view=trader", '("trader", "/?view=trader")' in src)
    check("fed by the real-shape capture through a stand-in checkout, another by --trader-fixture",
          'TRADER_FIXTURE = REPO / "tests" / "fixtures" / "trader_status_real.json"' in src
          and '"--trader-fixture"' in src
          and "def stand_in_trader" in src and "trader_projects(home, tmp, trader_live)" in src)
    doc = json.loads(TRADER_FIXTURE.read_text())
    sections = {"summary", "sleeves", "gate", "universe", "dispatches", "fills", "jobs",
                "freshness", "health", "host", "shadow", "env"}
    check("the fixture is a v1 document with every section the emitter promises",
          doc["v"] == 1 and set(doc["sections"]) == sections
          and all({"status", "reason", "source", "as_of"} <= set(s) for s in doc["sections"].values()))
    # The view's own names for every optional field, which the tabs' tests
    # draw; what trader really prints is trader_status_real.json.
    check("the fixture has every field the view reads",
          {"status", "headline", "lines", "look_at"} <= set(doc["verdict"])
          and isinstance(doc["sections"]["summary"]["book_total"], (int, float))
          and doc["sections"]["summary"]["doing"]
          and all({"name", "label", "alloc", "leverage", "net_liq", "cash", "holds", "drawdown", "peak"} <= set(x)
                  for x in doc["sections"]["sleeves"]["sleeves"])
          and doc["sections"]["sleeves"]["halt"] == 0.40 and doc["sections"]["sleeves"]["target"] == 0.34
          and [w["id"] for w in doc["watch"]] == ["daemon", "dispatch", "guards", "equity", "bars", "jobs",
                                                 "gateway", "alerts", "backups", "uptime"]
          and all({"id", "status", "label", "reason"} <= set(w) for w in doc["watch"]))


# --- the Trader view against what trader_status.py really prints ---------------
# tests/fixtures/trader_status_real.json is trader 8d47517's own output, captured
# from the live checkout. The cards were written against their own field names;
# trNormalize reads the real ones under them. Only what the document does not
# carry may show as missing.


TRADER_REAL_DRIVER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const doc = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const contract = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
function el(id) {
  return {id, textContent: "", value: "", innerHTML: "", style: {}, dataset: {}, children: [],
    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
    appendChild(c) { return c; }, addEventListener() {}, querySelector() { return null; },
    querySelectorAll() { return []; }, focus() {}, setAttribute() {}};
}
const els = {};
globalThis.document = {getElementById: id => els[id] || (els[id] = el(id)), createElement: () => el("n"),
  hidden: false, querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, body: el("body")};
globalThis.window = {addEventListener() {}, location: {search: ""}};
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
globalThis.fetch = async url => ({status: 200, headers: {get: () => null}, json: async () =>
  url.startsWith("/api/sessions") ? {sessions: [], slack: {}}
  : url.startsWith("/api/stats") ? {total_cost: 0, cache_rate: null, threads: 0, days: [], models: []} : {ok: true}});
(0, eval)(src + `;globalThis.__t = {traderHtml, trNormalize, TRADER_TABS};`);
const T = globalThis.__t, now = Date.parse(doc.generated_at) / 1000;
const out = {tabs: {}};
for (const [k] of T.TRADER_TABS) out.tabs[k] = T.traderHtml({ok: true, doc}, now, k);
out.failed = T.traderHtml({ok: false, error: "timed out", last_good: doc}, now + 3600);
// Filling names never overwrites: the view's own document comes back as it went in.
out.contractSame = JSON.stringify(T.trNormalize(contract)) === JSON.stringify(contract);
out.inputUntouched = JSON.stringify(doc) === fs.readFileSync(process.argv[3], "utf8").trim()
  || JSON.stringify(doc) === JSON.stringify(JSON.parse(fs.readFileSync(process.argv[3], "utf8")));
out.watch = T.trNormalize(doc).watch;
console.log(JSON.stringify(out));
"""


def test_trader_view_reads_what_trader_status_really_prints():
    import re
    sys.argv = ["x"]
    import visualizer as V
    print("\nthe Trader view draws trader_status.py's real document")
    doc = json.loads(TRADER_REAL_FIXTURE.read_text())
    check("the capture is trader's own shape: verdict a word, no watch list",
          doc["v"] == 1 and isinstance(doc["verdict"], str) and "watch" not in doc
          and "total_net_liq" in doc["sections"]["summary"]
          and "drawdown_from_state_peak" in doc["sections"]["sleeves"]["sleeves"][0])
    node = shutil.which("node")
    if not node:
        print("  … node not installed — cannot run the page's own javascript")
        return
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    (d / "drive.js").write_text(TRADER_REAL_DRIVER)
    p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js"), str(TRADER_REAL_FIXTURE),
                        str(TRADER_FIXTURE)], capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, TZ="America/Los_Angeles"))
    if p.returncode != 0:
        check("the view's javascript runs on the real document", False, p.stderr.strip()[-800:])
        return
    o = json.loads(p.stdout)
    ICON = '<span aria-hidden="true">'
    g = o["tabs"]["summary"]
    text = re.sub(r"<[^>]+>", " ", g)
    check("the verdict is the document's word, with a headline from its section counts",
          'class="tr-verdict bad"' in g and ICON + "✕</span> not OK</span> Not OK: 2 sections bad, 5 things to look at</h3>" in g
          and "<p>freshness: last equity snapshot 2026-11-16 — more sessions behind than the daily report leaves it</p>" in g
          and "<b>Look at:</b> universe; jobs; host; shadow; env</p>" in g, g[:700])
    check("the book card: total, then each sleeve's allocation, net liq, cash and holdings",
          "$196,999" in g and "75% · 1.0x" in g and "25% · 1.0x" in g and "$146,848" in g
          and "$50,152" in g and "100%" in g and "91%" in g and "all cash" in g and "holds 45 VNQ" in g, text[:900])
    check("drawdown meters from the state peak, against the 40% halt and 34% tick",
          "22.9% from peak $190,510" in g and "3.5% from peak $51,970" in g
          and "17.1pp to the halt" in g and "36.5pp to the halt" in g
          and g.count('<u style="left:68.0%">') == 2 and g.count('<b style="left:80.0%">') == 2)
    check("and says the halt and target lines are not from the document",
          "The halt and target lines are the design&#39;s" in g or "The halt and target lines are the design's" in g)
    check("what it is doing, in the document's own words",
          "CreditGatedMomentum: all cash" in g and "CrossAssetTrend: holds 45 VNQ" in g
          and "Credit gate: CLOSED — momentum rebalances to cash" in g and "Fills: 5 fills in the last 7 days" in g)
    want = {"daemon": ("warn", "restart pending"), "dispatch": ("ok", "open Thu Nov 19 06:33"),
            "guards": ("ok", "none tripped"), "equity": ("bad", "newest 2026-11-16 (3d)"),
            "bars": ("ok", "fresh"), "jobs": ("warn", "4 exited non-zero"),
            "gateway": ("ok", "port 4002 up"), "alerts": ("unknown", "not reported"),
            "backups": ("unknown", "not reported"), "uptime": ("ok", "up 16d19")}
    got = {w["id"]: (w["status"], w["label"]) for w in o["watch"]}
    check("all ten tiles come from the document, in the audit's order",
          [w["id"] for w in o["watch"]] == list(want) and got == want, got)
    tiles = re.findall(r'<div class="tr-w" data-watch="(\w+)">(.*?)</span></div>', g)
    check("every tile drawn with its icon and label",
          [t[0] for t in tiles] == list(want)
          and all(re.search(r'class="tr-st (ok|warn|bad|unknown)"[^>]*><span aria-hidden="true">[✓!✕?]</span> \S', h)
                  for _, h in tiles))
    check("only what the document lacks is 'not reported': alert delivery and backups",
          g.count("not reported") == 2 and "doesn&#39;t report alert delivery" in g.replace("'", "&#39;")
          and "No skipped slot in 10 sessions" in g and "PID 84984 up since Sun Nov 15 08:00" in g, text[-1500:])
    for k, h in o["tabs"].items():
        # "NaN rows" is the document's own words; a NaN figure is not.
        bad = [x for x in ("[object Object]", "$NaN", "NaN%", ">NaN<", "NaNpp", "undefined", ">null<", "Infinity") if x in h]
        check(f"the {k} tab draws the real document with no {', '.join(bad) or 'stray value'}", not bad)
    t = o["tabs"]
    check("Sleeves: positions, credit gate state and window, universe running vs disk",
          "VNQ" in t["sleeves"] and "95.19" in t["sleeves"] and "OFF (risk-off)" in t["sleeves"]
          and "100-day avg 0.7465" in t["sleeves"] and "running ≠ disk" in t["sleeves"])
    check("Activity: the dispatch slots and journal fills",
          t["activity"].count("62/62") >= 6 and "DBC" in t["activity"] and "positions not reported" not in t["activity"])
    check("Plumbing: launchd schedules in words, health checks, non-secret .env",
          "daily 18:30" in t["plumbing"] and "every 2m" in t["plumbing"] and "Mon 08:00" in t["plumbing"]
          and "snapshot" in t["plumbing"] and "MOMENTUM_UNIVERSE" in t["plumbing"] and "(set)" in t["plumbing"])
    check("Shadow: the C1 shadow's span and stats, the blend curve's state",
          "2026-10-26 → 2026-11-13" in t["shadow"] and "−2.5%" in t["shadow"] and "2026-11-16" in t["shadow"])
    check("a failed run draws the real last good document the same way",
          "status unavailable:" in o["failed"] and "$196,999" in o["failed"] and "restart pending" in o["failed"])
    check("filling names never overwrites: the view's own fixture comes back unchanged", o["contractSame"])


TRADER_C1_DRIVER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const doc = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
function el(id) {
  return {id, textContent: "", value: "", innerHTML: "", style: {}, dataset: {}, children: [],
    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
    appendChild(c) { return c; }, addEventListener() {}, querySelector() { return null; },
    querySelectorAll() { return []; }, focus() {}, setAttribute() {}};
}
const els = {};
globalThis.document = {getElementById: id => els[id] || (els[id] = el(id)), createElement: () => el("n"),
  hidden: false, querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, body: el("body")};
globalThis.window = {addEventListener() {}, location: {search: ""}};
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
globalThis.fetch = async url => ({status: 200, headers: {get: () => null}, json: async () =>
  url.startsWith("/api/sessions") ? {sessions: [], slack: {}}
  : url.startsWith("/api/stats") ? {total_cost: 0, cache_rate: null, threads: 0, days: [], models: []} : {ok: true}});
(0, eval)(src + `;globalThis.__t = {traderHtml, trNormalize};`);
const T = globalThis.__t, now = Date.parse(doc.generated_at) / 1000;
const vary = f => { const d = JSON.parse(JSON.stringify(doc)); f(d.sections.shadow.vm_multi_asset); return d; };
const draw = d => T.traderHtml({ok: true, doc: d}, now, "shadow");
console.log(JSON.stringify({
  c1: T.trNormalize(doc).sections.shadow.c1,
  real: draw(doc),
  missing: draw(vary(vm => { delete vm.series; vm.series_note = "recorded.csv not found"; })),
  nul: draw(vary(vm => { vm.series = null; delete vm.series_note; })),
  empty: draw(vary(vm => { vm.series = []; })),
  blank: draw(vary(vm => { vm.series = vm.series.map(x => ({date: x.date, shadow: null, backtest: null})); })),
  unreadable: draw(vary(vm => { vm.series = null; delete vm.series_note;
    vm.unknown_fields = {series: "recorded.csv unreadable: ValueError"}; })),
  nanDay: draw(vary(vm => { vm.series[3].shadow = null; vm.series[3].backtest = null; })),
  shadowOnly: draw(vary(vm => { vm.series.forEach(x => { x.backtest = null; });
    vm.series_note = "backtest.csv not found — shadow only"; })),
}));
"""


def test_trader_shadow_tab_draws_the_c1_curve():
    import re
    sys.argv = ["x"]
    import visualizer as V
    print("\nthe Shadow tab draws C1's shadow vs backtest curve from trader_status.py's series")
    doc = json.loads(TRADER_REAL_FIXTURE.read_text())
    ser = doc["sections"]["shadow"]["vm_multi_asset"]["series"]
    check("the capture carries trader_status.py's shadow.vm_multi_asset.series [{date, shadow, backtest}]",
          isinstance(ser, list) and len(ser) == 15
          and all(set(x) == {"date", "shadow", "backtest"} and isinstance(x["shadow"], (int, float)) for x in ser),
          ser[:2])
    node = shutil.which("node")
    if not node:
        print("  … node not installed — cannot run the page's own javascript")
        return
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    (d / "drive.js").write_text(TRADER_C1_DRIVER)
    p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js"), str(TRADER_REAL_FIXTURE)],
                       capture_output=True, text=True, timeout=60, env=dict(os.environ, TZ="America/Los_Angeles"))
    if p.returncode != 0:
        check("the view's javascript runs on the real document", False, p.stderr.strip()[-800:])
        return
    o = json.loads(p.stdout)
    check("trNormalize carries the series into shadow.c1 unchanged", o["c1"].get("series") == ser,
          str(o["c1"].get("series"))[:200])

    def chart(h):
        m = re.search(r'<svg class="tr-chart"[^>]*aria-label="C1 shadow vs backtest">(.*?)</svg>', h, re.S)
        if not m:
            return None
        paths = dict(re.findall(r'<path class="(l2?)" d="([^"]*)"', m.group(1)))
        pts = {k: len(re.findall(r"[ML]", v)) for k, v in paths.items()}
        return {"shadow": pts.get("l", 0), "backtest": pts.get("l2", 0),
                "tips": len(re.findall(r'class="hit"', m.group(1)))}

    h = o["real"]
    c = chart(h)
    check("the C1 chart draws a point for every day, shadow and backtest",
          c == {"shadow": 15, "backtest": 15, "tips": 15}, c)
    check("its tips read the first and last days in dollars, with the backtest beside",
          'data-tip="2026-10-26 · 199,964 · backtest 199,964"' in h and 'data-tip="2026-11-13 · ' in h,
          re.findall(r'data-tip="[^"]*"', h)[:2])
    check("no 'not reported' when the curve is there", "not reported" not in h.split("Blend shadow")[0])
    for k in ("missing", "nul", "empty", "blank"):
        g = o[k]
        c1 = g.split("Blend shadow")[0]
        check(f"a {k} series shows 'not reported', not an empty chart",
              chart(g) is None and "no points to draw" not in c1
              and '<span class="tr-st unknown"' in c1 and "not reported</span> shadow vs backtest curve" in c1
              and "2026-10-26 → 2026-11-13" in c1, c1[-500:])
    check("a missing series says why, in trader_status.py's own words",
          "shadow vs backtest curve: recorded.csv not found" in o["missing"])
    check("an unreadable recorded.csv says why, from unknown_fields",
          "shadow vs backtest curve: recorded.csv unreadable: ValueError" in o["unreadable"])
    check("an empty list doesn't pass the rebase note off as the reason",
          "rebased to the shadow" not in o["empty"].split("Blend shadow")[0])
    c = chart(o["nanDay"])
    check("a day with no figure stays: a NaN band and table row, the lines broken around it",
          c is not None and c["tips"] == 15 and c["shadow"] == 14 and c["backtest"] == 14
          and o["nanDay"].count('<rect class="nan"') == 1 and o["nanDay"].count('class="tr-nanrow"') == 1, c)
    c = chart(o["shadowOnly"])
    check("shadow only: the chart draws the shadow and says why the backtest is missing",
          c is not None and c["shadow"] == 15 and c["backtest"] == 0
          and "backtest.csv not found — shadow only" in o["shadowOnly"], c)
    check("a drawn chart carries trader's rebase note", "backtest rebased to the shadow on 2026-10-26" in h)


# --- the inbox: everything waiting on you, the dashboard's home -----------------
# The task panel's Needs you tab, the board's column, the operator's plan (in
# its thread) and a health check's "decide:" line each held a part of what is
# yours to do. inbox.py puts them in one list, which the page opens on.

def _inbox_fixture():
    """A real store holding one of everything the inbox lists, and some that
    it must not: (store, ids, built inbox, registry)."""
    import inbox as I
    import tasks as T
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    ids = {}

    def make(key, title, state, **kw):
        path = {T.PROPOSED: [], T.QUEUED: [], T.RUNNING: [T.RUNNING],
                T.AWAITING_APPROVAL: [T.RUNNING, T.AWAITING_APPROVAL],
                T.NEEDS_INPUT: [T.RUNNING, T.NEEDS_INPUT], T.FAILED: [T.RUNNING, T.FAILED],
                T.DONE: [T.RUNNING, T.DONE]}[state]
        first = T.PROPOSED if state == T.PROPOSED else T.QUEUED
        role = kw.pop("role", "implementor")
        t = st.create(kw.pop("goal", title + "\nWhat done looks like: it works."), title=title,
                      state=first, role=role, project=kw.pop("project", "alpha"),
                      source=kw.pop("source", "ideation"), driver="queue")
        for s in path:
            st.transition(t["id"], s, kw.pop("why", "") if s == state else "")
        if kw:
            st.update(t["id"], **kw)
        ids[key] = t["id"]
        return t["id"]
    make("proposal", "Cache the release plan", T.PROPOSED)
    make("approval", "Show sync errors in Settings", T.AWAITING_APPROVAL,
         result={"review": {"ok": True, "summary": "clean, tests added"}})
    make("question", "Ship the widget", T.NEEDS_INPUT, why="yours to do",
         needs_user={"action": "!release cadence ios", "why": "a release is yours"})
    make("findings", "Which simulator flakes", T.NEEDS_INPUT, role="researcher", why="findings")
    make("failed", "Fix the flaky clock test", T.FAILED, why="tests failed: test_clock", project="beta")
    make("landing", "Tidy the board", T.DONE,
         result={"landing": {"eligible": True, "landed": False, "stage": "rebase",
                             "detail": "CONFLICT in board.py"}})
    # Not yours: queued, running, landed, a refused landing with no branch here,
    # and a conversation turn that finished.
    make("queued", "Queued work", T.QUEUED)
    make("running", "Running work", T.RUNNING)
    make("landed", "Landed work", T.DONE, result={"landing": {"eligible": True, "landed": True}})
    make("gone", "Refused, branch gone", T.DONE,
         result={"landing": {"eligible": True, "landed": False, "stage": "rebase"}})
    make("turn", "a reply in a thread", T.DONE, source="slack", role="assistant")
    # A recommendation for the state it is in, and one written before it moved.
    rec, err = T.make_recommendation("approve", "review passed and the tests are green",
                                     "operator", T.AWAITING_APPROVAL, now=1.0)
    assert not err, err
    st.update(ids["approval"], recommendation=rec)
    stale, _ = T.make_recommendation("retry", "it was a quota blip", "operator", T.RUNNING, now=1.0)
    st.update(ids["failed"], recommendation=stale)
    mine, _ = T.make_recommendation("dismiss", "superseded by the widget task", "cli",
                                    T.PROPOSED, now=1.0, since=T.entered(st.get(ids["proposal"])))
    st.update(ids["proposal"], recommendation=mine)
    unmerged = [{"id": ids["landing"], "commits": 2, "branch": "silkworm/" + ids["landing"],
                 "local": True}]
    plan = {"id": "pl_1", "thread": "D1:1.0", "at": 50.0, "entries": [
        {"n": 1, "name": "tailscale-on", "args": []},
        {"n": 2, "name": "trader-refetch-revisions", "args": ["--apply"]},
        {"n": 3, "name": "tailscale-status", "args": [], "used": True}]}
    registry = {"app-store": types.SimpleNamespace(
        decision=lambda r: "submit 1.3 for review, or hold it")}
    health_rows = [{"project": "mondays", "check": "app-store", "status": "warn",
                    "summary": "1.3 is processed and not submitted", "last_changed": 40.0},
                   {"project": "alpha", "check": "ci", "status": "fail", "summary": "red"}]
    built = I.build(st.all().values(), unmerged_rows=unmerged, plan=plan,
                    health_rows=health_rows, operator_thread="D1:1.0",
                    project_titles={"alpha": "Alpha", "beta": "Beta", "mondays": "Mondays"},
                    registry=registry)
    return st, ids, built, registry


def test_inbox_lists_everything_waiting_on_you():
    import inbox as I
    import tasks as T
    print("\nthe inbox: one list of what waits on you, grouped by project, most urgent first")
    st, ids, built, registry = _inbox_fixture()
    items = {i["id"]: i for g in built["groups"] for i in g["items"]}
    want = {ids[k] for k in ("proposal", "approval", "question", "findings", "failed", "landing")}
    check("every proposal, approval, question, findings, failure and refused landing is listed",
          want <= set(items), sorted(set(items)))
    check("and nothing that is not yours: queued, running, landed, a gone branch, a turn",
          not {ids[k] for k in ("queued", "running", "landed", "gone", "turn")} & set(items))
    check("the operator's plan is one item, its steps not yet run",
          items.get("pl_1", {}).get("kind") == "plan"
          and items["pl_1"]["steps"] == ["tailscale-on", "trader-refetch-revisions --apply"]
          and items["pl_1"]["actions"] == ["run-all", "run-selected", "not-now"]
          and [e["n"] for e in items["pl_1"]["entries"]] == [1, 2] and "2 steps" in items["pl_1"]["title"],
          str(items.get("pl_1")))
    dec = items.get("health:mondays:app-store") or {}
    check("a health check naming your decision is listed as one, with Discuss in the operator's thread",
          dec.get("kind") == "decision" and dec.get("line") == "decide: submit 1.3 for review, or hold it"
          and dec.get("actions") == ["discuss"] and dec.get("thread") == "D1:1.0", str(dec))
    check("a failing check with no decision of yours is not (its fix is a task)",
          "health:alpha:ci" not in items)
    check("built from the plan entries alone, a spent plan is no item",
          I.plan_item({"id": "p", "entries": [{"n": 1, "name": "x", "used": True}]}) is None
          and I.plan_item(None) is None)
    acts = {k: items[ids[k]]["actions"] for k in
            ("proposal", "approval", "question", "findings", "failed", "landing")}
    check("each item offers exactly what it allows", acts == {
        "proposal": ["accept", "dismiss"], "approval": ["approve", "rework", "dismiss"],
        "question": ["resolve", "rework", "dismiss"], "findings": ["close", "dig", "dismiss"],
        "failed": ["retry", "dismiss"], "landing": ["land", "drop"]}, str(acts))
    check("in plain words: the step a run left you, a review's verdict, why it failed",
          items[ids["question"]]["line"] == "yours to do: !release cadence ios — a release is yours"
          and items[ids["approval"]]["line"] == "review passed: clean, tests added"
          and items[ids["failed"]]["line"] == "tests failed: test_clock"
          and items[ids["landing"]]["line"] == "not landed (rebase) — CONFLICT in board.py"
          and items[ids["proposal"]]["line"] == "What done looks like: it works.",
          str({k: items[ids[k]]["line"] for k in ("question", "approval", "failed", "landing", "proposal")}))
    check("the recommendation is on the item it was written for",
          items[ids["approval"]]["recommendation"] == {
              "action": "approve", "why": "review passed and the tests are green", "by": "operator"},
          str(items[ids["approval"]]["recommendation"]))
    check("one written before the task moved on is not shown",
          items[ids["failed"]]["recommendation"] is None)
    odd, _ = T.make_recommendation("land", "x", "operator", T.PROPOSED)
    lone = st.create("A proposal", state=T.PROPOSED, project="alpha")["id"]
    st.update(lone, recommendation=odd)
    got = [i for g in I.build(st.all().values())["groups"] for i in g["items"] if i["id"] == lone]
    check("nor one for a button its card does not have (Land on a proposal)",
          got and got[0]["recommendation"] is None, str(got))
    check("findings carry the round their buttons act on",
          items[ids["findings"]]["round"] == T.parked_round(st.get(ids["findings"]))
          and items[ids["findings"]]["round"])
    order = [g["project"] for g in built["groups"]]
    check("groups go by their most urgent item: a failure, then approvals and the plan, then decisions",
          order[0] == "beta" and order.index("alpha") < order.index("mondays")
          and order.index(I.OPERATOR) < order.index("mondays"), str(order))
    alpha = next(g for g in built["groups"] if g["project"] == "alpha")
    kinds = [i["kind"] for i in alpha["items"]]
    check("and within one, the most urgent first, proposals last",
          kinds[0] == "approval" and kinds[-1] == "proposal", str(kinds))
    check("a group is headed with the project's title", alpha["title"] == "Alpha")
    check("the count is every item", built["count"] == sum(len(g["items"]) for g in built["groups"]))
    # Newest first among equals.
    st2 = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    a = st2.create("older proposal", state=T.PROPOSED, project="p")["id"]
    b = st2.create("newer proposal", state=T.PROPOSED, project="p")["id"]
    st2._data[a].update(created=100.0, events=[]); st2._data[b].update(created=200.0, events=[])
    st2._data[a]["updated"] = 999.0                # a note written on it later
    got = [i["id"] for i in I.build(st2.all().values())["groups"][0]["items"]]
    check("among items as urgent, the newest first -- by when it came to need you, not its last write",
          got == [b, a], str(got))
    check("an answered plan is no item: no later message answers it",
          I.plan_item({"id": "p", "answered": "1.0", "entries": [{"n": 1, "name": "x"}]}) is None)
    check("with nothing waiting, nothing is listed",
          I.build([]) == {"groups": [], "count": 0, "shelved": 0})
    # An archived project's items are left out and counted, not shown.
    rec = {"id": "tsk_a1", "state": "proposed", "project": "old", "title": "x", "goal": "x",
           "role": "implementor"}
    out = I.build([rec, dict(rec, id="tsk_b1", project="live")], shelved={"old"})
    check("an archived project's items are counted, not listed",
          out["shelved"] == 1 and out["count"] == 1
          and [g["project"] for g in out["groups"]] == ["live"], str(out))
    # A reviewer is filed under no project: it goes in its parent's group.
    st3 = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    par = st3.create("parent", state=T.QUEUED, project="gamma")["id"]
    rv = st3.create("Review: parent", state=T.QUEUED, role="reviewer", project="", parent=par)["id"]
    st3.transition(rv, T.RUNNING); st3.transition(rv, T.FAILED, "reviewer crashed")
    g = I.build(st3.all().values())["groups"]
    check("a failed review is listed under its parent's project",
          [(x["project"], [i["id"] for i in x["items"]]) for x in g] == [("gamma", [rv])], str(g))


def test_recommendation_field_and_route():
    import tasks as T
    print("\nthe operator's recommendation: a field, set by its own route, deciding nothing")
    check("declared as a task field, empty by default",
          "recommendation" in T.FIELDS and T.default("recommendation") is None)
    rec, err = T.make_recommendation("Approve", "  clean  review ", "operator", "awaiting_approval", now=5.0)
    check("a recommendation is an action the inbox offers, a reason, who and for which state",
          not err and rec == {"action": "approve", "why": "clean review", "by": "operator",
                              "state": "awaiting_approval", "since": 0.0, "at": 5.0}, f"{rec} {err}")
    check("an action the inbox has no button for is refused",
          T.make_recommendation("merge", "x", "o", "proposed")[0] is None)
    check("and so is one with no reason",
          "--why" in T.make_recommendation("accept", "  ", "o", "proposed")[1])
    check("and one too long for a card",
          T.make_recommendation("accept", "x" * (T.RECOMMEND_WHY_MAX + 1), "o", "proposed")[0] is None)
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    tid = st.create("Cache the release plan", state=T.PROPOSED, project="alpha")["id"]
    handle = _bot_func("handle_tasks", task_store=st, tasks=T, log=logging.getLogger("test"),
                       recommend_task=_bot_func("recommend_task", task_store=st, tasks=T,
                                                merge=__import__("merge")))
    r = handle({"action": "recommend", "id": tid, "recommend": "accept",
                "why": "small, and the cache is measured", "by": "operator"})
    t = st.get(tid)
    check("the route writes it, stamped with the task's state, and moves nothing",
          r.get("ok") and t["state"] == T.PROPOSED and t["recommendation"]["action"] == "accept"
          and t["recommendation"]["state"] == T.PROPOSED and t["goal"] == "Cache the release plan",
          f"{r} {t.get('recommendation')}")
    check("it is shown while the task stands where it was written",
          T.recommendation_for(t) == {"action": "accept", "why": "small, and the cache is measured",
                                      "by": "operator"})
    st.transition(tid, T.QUEUED, "accepted")
    check("and not once it has moved on", T.recommendation_for(st.get(tid)) is None)
    r = handle({"action": "recommend", "id": tid, "recommend": "bogus", "why": "x"})
    check("a bad action is refused, naming the good ones", not r["ok"] and "approve" in r["error"], str(r))
    r = handle({"action": "recommend", "id": tid, "recommend": "clear"})
    check("clear takes it off", r["ok"] and st.get(tid)["recommendation"] is None, str(r))
    st.transition(tid, T.RUNNING); st.transition(tid, T.DONE)
    r = handle({"action": "recommend", "id": tid, "recommend": "land", "why": "x"})
    check("nothing is recommended on finished work", not r["ok"] and "done" in r["error"], str(r))
    st.update(tid, result={"landing": {"eligible": True, "landed": False, "stage": "rebase"}})
    r = handle({"action": "recommend", "id": tid, "recommend": "land", "why": "rebased cleanly by hand"})
    check("except a landing git refused, which is the inbox's Land and Drop",
          r.get("ok") and T.recommendation_for(st.get(tid))["action"] == "land", str(r))
    # The same state, a later stay in it: research dug deeper parks again.
    rs = st.create("Which simulator flakes", state=T.QUEUED, role="researcher")["id"]
    st.transition(rs, T.RUNNING); st.transition(rs, T.NEEDS_INPUT, "findings")
    st._data[rs]["events"][-1]["at"] -= 100        # parked a while ago
    r = handle({"action": "recommend", "id": rs, "recommend": "close", "why": "answered"})
    check("written on the first findings, it shows on them",
          r.get("ok") and (T.recommendation_for(st.get(rs)) or {}).get("action") == "close", str(r))
    st.transition(rs, T.QUEUED, "dig deeper"); st.transition(rs, T.RUNNING)
    st.transition(rs, T.NEEDS_INPUT, "findings")
    check("not on the findings a dig deeper brought, though the state is the same",
          st.get(rs)["state"] == T.NEEDS_INPUT and T.recommendation_for(st.get(rs)) is None)
    check("an unknown task is said", not handle({"action": "recommend", "id": "nope",
                                                   "recommend": "accept", "why": "x"})["ok"])


def test_operator_recommends_without_approval():
    import operator_role as O
    import tasks as T
    print("\nthe operator sets a recommendation with no approval: it decides nothing")
    for state in ("proposed", "awaiting_approval", "needs_input", "failed", "queued"):
        check(f"the guard asks nothing to recommend on {state} work",
              O.tasks_guard("recommend", {"id": "t", "recommend": "accept", "goal": "x"},
                            {"state": state, "role": "implementor"}) is None)
    check("while an edit of a proposal is still accepting it",
          O.tasks_guard("edit", {"id": "t", "title": "x"}, {"state": "proposed"}) == ("accept", "t"))
    bot = _operator_bot()
    st = bot.store
    p = st.create("Add an appearance preference to Settings", state=T.PROPOSED,
                  role="implementor", project="alpha")["id"]
    r = _op_cli(bot, "edit", p, "--recommend", "accept", "--why", "small and well specified")
    t = st.get(p)
    check("through the CLI, the operator's --recommend is written, exit 0",
          r.returncode == 0 and "recommends accept" in r.stdout
          and (t.get("recommendation") or {}).get("action") == "accept"
          and t["recommendation"]["why"] == "small and well specified"
          and t["recommendation"]["by"] == "operator", r.stdout + r.stderr)
    check("and the proposal is still a proposal: nothing was accepted",
          t["state"] == T.PROPOSED and t["title"] == "Add an appearance preference to Settings")
    check("with no approval asked for or recorded", "!approve" not in r.stderr
          and (not bot.statefile.exists() or "recommend" not in bot.statefile.read_text()))
    r = _op_cli(bot, "edit", p, "--recommend", "accept")
    check("--recommend without --why is refused before asking (usage, exit 2)",
          r.returncode == 2 and "--why" in r.stderr, r.stdout + r.stderr)
    r = _op_cli(bot, "edit", p, "--recommend", "accept", "--why", "x", "--title", "New")
    check("and not mixed with an edit of the goal or title", r.returncode == 2
          and st.get(p)["title"] == "Add an appearance preference to Settings", r.stderr)
    r = _op_cli(bot, "edit", p, "--recommend", "clear")
    check("--recommend clear takes it off", r.returncode == 0
          and st.get(p)["recommendation"] is None, r.stdout + r.stderr)
    r = _op_cli(bot, "edit", p, "--title", "A different title")
    check("while the operator's plain edit of a proposal is still refused",
          r.returncode == 1 and st.get(p)["title"] == "Add an appearance preference to Settings",
          r.stdout + r.stderr)
    cli = _load_cli()
    pay = cli.parse_action("edit", ["tsk_1", "--recommend", "approve", "--why", "green"])
    check("the CLI sends it as the recommend action", pay["action"] == "recommend"
          and pay["recommend"] == "approve" and pay["why"] == "green" and pay["id"] == "tsk_1", str(pay))


#: The inbox's harness: the motion harness's DOM, timers, fetch and toast,
#: driving the page's own inbox against what inbox.build makes of a real store.
def _inbox_harness():
    head = MOTION_HARNESS[:MOTION_HARNESS.index("(0, eval)(SRC")]
    return head + r"""
const FIX = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
let CONFIRM = true, PROMPT = "a note";
const asked = [];
globalThis.confirm = m => { asked.push(m); return CONFIRM; };
globalThis.prompt = m => { asked.push(m); return PROMPT; };
let inboxAnswer = FIX;
answer = (url, b) => (b && b.action === "inbox") ? inboxAnswer : BASE(url);
(0, eval)(SRC + `
;globalThis.__m = {loadInbox, drawInbox, inboxAct, setHome, pendingGone, homeTab: () => homeTab};`);
const M = globalThis.__m, out = {};
const flushAsync = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r)); };
const btn = (act, id, extra) => { const e = E("BUTTON"); Object.assign(e.dataset, {act, id, title: "T " + id}, extra || {}); return e; };
const posts = () => calls.filter(c => (c.url === "/api/tasks" || c.url === "/api/send") && c.body
  && c.body.action !== "inbox" && c.body.action !== "list" && c.body.action !== "attention"
  && c.body.action !== "counts" && c.body.action !== "holding" && c.body.action !== "unmerged")
  .map(c => Object.assign({url: c.url}, c.body));
(async () => {
  await M.loadInbox(); await flushAsync();
  out.desk = byId("inbox").innerHTML;
  out.phone = byId("inboxphone").innerHTML;
  out.count = byId("inboxcount").textContent;
  // --- each button's action ---
  const run = async (act, id, extra) => {
    const n = posts().length; await M.inboxAct(btn(act, id, extra)); await flushAsync();
    return posts().slice(n);
  };
  const ids = FIX.__ids;
  out.acceptNow = await run("accept", ids.proposal);
  out.acceptPending = (M.pendingGone.get(ids.proposal) || {}).action || null;
  out.hiddenWhilePending = !byId("inbox").innerHTML.includes(`data-id="${ids.proposal}"`);
  runTimers(now + 5100); await flushAsync();
  out.acceptSent = posts().filter(p => p.id === ids.proposal);
  // Dismiss on findings: undoable, and carries its round. Undone, nothing goes.
  await run("dismiss", ids.findings, {round: "r7"});
  const undoBtn = toastEl.children.find(c => c.className === "undo");
  out.undoOffered = !!undoBtn;
  if (undoBtn) undoBtn.onclick();
  runTimers(now + 6000); await flushAsync();
  out.undone = {posts: posts().filter(p => p.id === ids.findings), pending: M.pendingGone.has(ids.findings)};
  await run("dismiss", ids.findings, {round: "r7"});
  runTimers(now + 5100); await flushAsync();
  out.dismissSent = posts().filter(p => p.id === ids.findings);
  CONFIRM = false;
  out.approveDeclined = await run("approve", ids.approval);
  out.landDeclined = await run("land", ids.landing);
  out.refileDeclined = await run("refile", ids.landing);
  CONFIRM = true;
  out.refile = await run("refile", ids.landing);
  out.approve = await run("approve", ids.approval);
  out.land = await run("land", ids.landing);
  out.drop = await run("drop", ids.landing);
  out.close = await run("close", ids.findings, {round: "r7"});
  out.dig = await run("dig", ids.findings, {round: "r7"});
  out.rework = await run("rework", ids.approval, {state: "awaiting_approval"});
  out.resolve = await run("resolve", ids.question);
  out.retryNow = await run("retry", ids.failed);
  runTimers(now + 5100); await flushAsync();
  out.retrySent = posts().filter(p => p.id === ids.failed);
  PROMPT = null;
  out.reworkCancelled = await run("rework", ids.approval, {state: "awaiting_approval"});
  // --- the phone's tabs ---
  M.setHome("threads");
  out.threads = {cls: body.classList.contains("home-threads"), tab: M.homeTab(), on: byId("htthreads").className};
  M.setHome("inbox");
  out.inboxTab = {cls: body.classList.contains("home-threads"), on: byId("htinbox").className};
  // --- empty, and unreadable ---
  inboxAnswer = {ok: true, groups: [], count: 0};
  await M.loadInbox(); await flushAsync();
  out.empty = byId("inbox").innerHTML;
  out.emptyCount = byId("inboxcount").textContent;
  inboxAnswer = {ok: false, error: "bot is offline"};
  await M.loadInbox(); await flushAsync();
  out.offlineKeeps = byId("inbox").innerHTML;
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_dashboard_inbox_renders_and_acts():
    import re
    import visualizer as V
    print("\nthe dashboard's home: the Needs you inbox, drawn from inbox.build, every button its action")
    page = V.PAGE
    js = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
    check("the page has the inbox on the laptop's main and as the phone's first tab",
          '<div id="inbox" class="inbox"></div>' in page and 'id="inboxphone"' in page
          and 'id="hometabs"' in page and "setHome('threads')" in page)
    check("its read is a read: cached, and passed through by the screenshot stand-in",
          "inbox" in V.READ_TASK_ACTIONS)
    check("it is polled, and redrawn with the board's work", "pollInbox = poller(" in js
          and "if (inboxData) drawInbox(inboxData);" in js)
    check("the page names no role", "'assistant'" not in js[js.index("// --- the inbox"):js.index("// --- phone ---")]
          and '"implementor"' not in js[js.index("// --- the inbox"):js.index("// --- phone ---")])
    st, ids, built, _ = _inbox_fixture()
    fix = json.loads(json.dumps({"ok": True, **built}))
    fix["__ids"] = ids
    node = shutil.which("node")
    if not node:
        print("  … node not installed — the inbox checked by text only")
        return
    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    (d / "drive.js").write_text(_inbox_harness())
    (d / "fix.json").write_text(json.dumps(fix))
    p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js"), "", str(d / "fix.json")],
                       capture_output=True, text=True, timeout=90)
    if p.returncode != 0:
        check("the inbox harness runs the page's script", False, p.stderr.strip()[-900:])
        return
    o = json.loads(p.stdout)
    desk = o["desk"]

    def card(tid):
        m = re.search(r'<div class="icard[^"]*" data-id="%s".*?(?=<div class="icard|<div class="igroup|$)'
                      % re.escape(tid), desk, re.S)
        return m.group(0) if m else ""

    def acts(tid):
        return re.findall(r'data-act="([\w-]+)"', card(tid))
    check("drawn on both: the laptop's main and the phone's tab", desk and o["phone"] == desk)
    check("headed Needs you with the count, which the phone's tab shows too",
          "<h3>Needs you</h3>" in desk and o["count"] == str(built["count"]), o["count"])
    check("grouped by project under its title, in the order inbox.build gave",
          re.findall(r'data-project="([^"]+)"', desk) == [g["project"] for g in built["groups"]]
          and re.search(r"<h4><span class=\"ipl plink\"[^>]*openProjectPage\('alpha'\)\">Alpha</span>", desk))
    per = {k: acts(ids[k]) for k in ("proposal", "approval", "question", "findings", "failed", "landing")}
    check("each card draws exactly the buttons its item allows", per == {
        "proposal": ["accept", "dismiss"], "approval": ["approve", "rework", "dismiss"],
        "question": ["resolve", "rework", "dismiss"], "findings": ["close", "dig", "dismiss"],
        "failed": ["retry", "dismiss"], "landing": ["land", "drop"]}, str(per))
    check("in words people use", all(w in desk for w in (">Accept<", ">Dismiss<", ">Approve<", ">Land<",
                                                         ">Close<", ">Send back…<", ">Run all<")))
    check("the plan and the decision are cards too: the plan's own, with its three buttons and its ×",
          re.findall(r'data-pact="(\w+)"', card("pl_1")) == ["dismiss", "all", "selected", "decline"]
          and 'data-n="2"' in card("pl_1")
          and acts("health:mondays:app-store") == ["discuss"]
          and "decide: submit 1.3 for review, or hold it" in card("health:mondays:app-store"))
    a = card(ids["approval"])
    check("the recommendation is a chip saying what and why, its button marked",
          "Operator suggests <b>Approve</b> — review passed and the tests are green" in a
          and re.search(r'class="act rec" data-act="approve"', a), a[:600])
    check("and a card with none has no chip", "suggest" not in card(ids["failed"]).lower()
          and "suggest" not in card(ids["landing"]).lower())
    check("one you set yourself says so rather than crediting the operator",
          "Suggested by cli: <b>Dismiss</b> — superseded by the widget task" in card(ids["proposal"]),
          card(ids["proposal"])[:500])
    check("a question's send-back is its Answer…, as on the task panel",
          ">Answer…<" in card(ids["question"]) and ">Send back…<" not in card(ids["question"]))
    check("a laptop has a way home from an open thread, with the count",
          'id="homebtn" onclick="goHome()"' in page and 'id="homecount"' in page
          and "body.reading #inbox { display: none; }" in page)
    check("polled only while it can be seen", "poller(() => inboxVisible() ? loadInbox() : null)" in js)
    check("the title and the one line are on the card",
          "Show sync errors in Settings" in a and "review passed: clean, tests added" in a)
    check("a proposal's card takes a swipe (the board's rule); nothing else's does",
          'data-swipe="1"' in card(ids["proposal"]) and desk.count('data-swipe="1"') == 1)
    # The buttons.
    check("Accept goes at once with Undo: nothing sent, the card out of the list",
          o["acceptNow"] == [] and o["acceptPending"] == "accept" and o["hiddenWhilePending"], str(o["acceptNow"]))
    check("...sent once the Undo window is over, as the accept action",
          [(x["url"], x["action"]) for x in o["acceptSent"]] == [("/api/tasks", "accept")], str(o["acceptSent"]))
    check("Dismiss offers Undo, and undone sends nothing", o["undoOffered"]
          and o["undone"] == {"posts": [], "pending": False}, str(o["undone"]))
    check("Dismiss on findings carries their round",
          [(x["action"], x.get("round")) for x in o["dismissSent"]] == [("dismiss", "r7")], str(o["dismissSent"]))
    check("Approve and Land ask first; declined, nothing is sent",
          o["approveDeclined"] == [] and o["landDeclined"] == [])
    one = lambda xs: [(x["action"], x.get("notes", ""), x.get("round", "")) for x in xs]
    check("confirmed, Approve sends approve", one(o["approve"]) == [("approve", "", "")], str(o["approve"]))
    check("Land sends land, Drop sends drop", one(o["land"]) == [("land", "", "")]
          and one(o["drop"]) == [("drop", "", "")], f"{o['land']} {o['drop']}")
    check("Refile to land asks first; declined, nothing is sent; confirmed, it sends refile",
          o["refileDeclined"] == [] and one(o["refile"]) == [("refile", "", "")], str(o["refile"]))
    check("Close sends close with the round", one(o["close"]) == [("close", "", "r7")], str(o["close"]))
    check("Dig deeper sends your question with the round",
          one(o["dig"]) == [("dig", "a note", "r7")], str(o["dig"]))
    check("Send back sends rework with your note", one(o["rework"]) == [("rework", "a note", "")], str(o["rework"]))
    check("and cancelling its note sends nothing", o["reworkCancelled"] == [])
    check("Done sends resolve", one(o["resolve"]) == [("resolve", "", "")], str(o["resolve"]))
    check("Retry is undoable, then sent as retry", o["retryNow"] == []
          and [x["action"] for x in o["retrySent"]] == ["retry"], str(o["retrySent"]))
    check("the phone's tabs switch between Needs you and Threads",
          o["threads"] == {"cls": True, "tab": "threads", "on": "on"}
          and o["inboxTab"] == {"cls": False, "on": "on"}, f"{o['threads']} {o['inboxTab']}")
    check("with nothing waiting: Nothing needs you, and no count",
          "Nothing needs you" in o["empty"] and o["emptyCount"] == "", o["empty"])
    check("a failed read is not taken for an empty one: the last list is kept",
          o["offlineKeeps"] == o["empty"])


# --- the operator's plan, as a card with buttons -----------------------------------
# 2026-11-21: a plan of ops the operator recorded waited for a typed "yes". Its
# card (in the thread and on Needs you) answers it with Run all, Run selected or
# Not now -- the same once-only, day-long answer, on the owner's own device,
# never from a turn -- and the bot runs what was chosen, writing each step's
# outcome back for the card.

class _SyncThread:
    """threading.Thread, run where it is started: the card's results are
    there to read the moment the press returns."""
    def __init__(self, target=None, args=(), kwargs=None, **_):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        self.target(*self.args, **(self.kwargs or {}))


def _plan_card_world(**env):
    """_ops_world, with /plan as shipped (handle_plan and the runner it
    starts), a visualizer probe that vouches for the connection, and the
    guarded actions' routes stood in for."""
    import threading as _th
    import dashowner as DO
    import ops as OPS
    import operator_role as O
    w = _ops_world()
    # One list for what the thread is told, from /plan and /ops alike.
    told, tasks_called, projects_called, decided = w.told, [], [], []
    w.probe = _VizProbe()
    w.drain = types.SimpleNamespace(left=0)
    ns = lift("handle_plan", "run_plan_entries", "run_plan_entry", "plan_card_now",
              "dashboard_owner",
              ops=OPS, OPS_LOG=w.log, os=os, time=time, log=logging.getLogger("t"),
              git_guard=__import__("git_guard"), operator_role=O, OPERATOR=w.state,
              dashowner=DO, WEB_PEER_PROBE=w.probe, envfile=__import__("envfile"),
              BASE_DIR=w.tmp,
              threading=types.SimpleNamespace(Thread=_SyncThread, Lock=_th.Lock),
              DRAIN=types.SimpleNamespace(remaining=lambda: w.drain.left),
              tell_thread=lambda key, text: told.append((key, text)),
              handle_tasks=lambda p: tasks_called.append(p) or {"ok": True, "note": "landing under way"},
              handle_projects=lambda p: projects_called.append(p) or {"ok": True},
              task_store=types.SimpleNamespace(
                  record_decision=lambda *a, **k: decided.append((a, k))),
              ops_context=lambda: w.ctx, _plan_runs=set(), _plan_runs_guard=_th.Lock())
    w.pns, w.told, w.tasks_called, w.projects_called, w.decided = (
        ns, told, tasks_called, projects_called, decided)
    return w


#: What the visualizer vouches for (dashowner.verify) on a press from your phone.
_OWNER = {"login": "owner@example.com", "peer": "100.64.0.2", "node": "phone1"}
_VIZ_CONN = (("127.0.0.1", 55072), ("127.0.0.1", 8787))


def _press(w, action, plan_id, entries=None, owner=_OWNER, conn=_VIZ_CONN, **extra):
    payload = {"action": action, "plan_id": plan_id, **extra}
    if entries is not None:
        payload["entries"] = entries
    if owner is not None:
        payload["dashboard_owner"] = dict(owner)
    try:
        return w.pns["handle_plan"](payload, *(conn or (None, None)))
    except Exception as e:
        return {"ok": None, "error": f"raised {e!r}"}


def _card(w):
    return w.pns["handle_plan"]({"action": "show"})["plan"]


def test_ops_plan_card_shows_what_each_entry_changes():
    print("\nthe plan's card: each recorded entry numbered, said plainly, with what it changes")
    w = _plan_card_world()
    os.environ["VIZ_TAILSCALE_LOGIN"] = _OWNER["login"]
    with _fake_path(w):
        check("no plan recorded: no card", _card(w) is None)
        r = _plan_record(w, "trader-env-set", "MOMENTUM_UNIVERSE", "pit", "--op", "tailscale-on")
        check("the operator records a plan", r.returncode == 0, r.stdout + r.stderr)
        c = _card(w)
    ents = c["entries"]
    check("both entries, numbered as the thread shows them",
          [(e["n"], e["command"]) for e in ents]
          == [(1, "trader-env-set MOMENTUM_UNIVERSE pit"), (2, "tailscale-on")], str(ents))
    check("each with its op's plain description", "set one allowed key of trader's .env"
          in ents[0]["description"] and "serve the dashboard to the tailnet" in ents[1]["description"])
    check("and what it changes, from its arguments",
          ents[0]["changes"].startswith("sets MOMENTUM_UNIVERSE=pit in trader's .env")
          and "tailnet" in ents[1]["changes"], str([e["changes"] for e in ents]))
    check("waiting, runnable, its expiry a day after it was recorded, nothing run",
          c["status"] == "waiting" and c["runnable"] and not c["running"]
          and abs(c["expires_at"] - c["at"] - 24 * 3600) < 1
          and not any(e["used"] or e["result"] for e in ents), str(c))
    check("in the operator's thread", c["thread"] == "D1:1.0")
    import operator_role as O
    st = O.State(w.tmp / "g.json")
    st.set_thread("D1:1.0")
    st.set_plan([{"name": "land", "args": ["tsk_ab12"], "guard": "trader-config",
                  "summary": "lands silkworm/tsk_ab12 into main; changes trader's live config: config.py"}],
                by="operator", thread="D1:1.0")
    g = O.plan_card(st.plan_view())["entries"][0]
    check("a guarded entry says what it is and what changes live",
          g["description"] == "Land this task's branch into its base" and g["guard"] == "trader-config"
          and "config.py" in g["changes"], str(g))


def test_ops_plan_card_runs_only_the_entries_selected():
    import operator_role as O
    print("\nthe plan's card: Run selected answers the plan with those entries, and runs only them")
    w = _plan_card_world()
    os.environ["VIZ_TAILSCALE_LOGIN"] = _OWNER["login"]
    with _fake_path(w):
        _plan_record(w, "trader-env-set", "MOMENTUM_UNIVERSE", "pit", "--op", "tailscale-on")
        pid = _card(w)["id"]
        w.calls.write_text("")
        r = _press(w, "run", pid, [2])
        c = _card(w)
        e1, e2 = c["entries"]
        check("pressed with entry 2 ticked: the answer is taken", r.get("ok") and r["running"] == [2], str(r))
        check("entry 2 ran (tailscale serve), entry 1 did not (.env untouched)",
              any(x.startswith("tailscale serve --bg") for x in _calls(w))
              and _env_value(w, "MOMENTUM_UNIVERSE") is None and not e1["used"] and e2["used"],
              str(_calls(w)))
        check("its outcome is on the card: ok or failed, with the output's tail",
              e2["result"]["state"] in ("ok", "failed") and isinstance(e2["result"].get("tail"), str)
              and e1["result"] is None, str(e2))
        check("the plan is answered, from the dashboard, by you: nothing more is runnable",
              c["status"] == "answered" and not c["runnable"] and c["answered_via"] == "dashboard"
              and c["answered_by"] == _OWNER["login"] and [e["chosen"] for e in c["entries"]] == [False, True],
              str(c))
        import ops as OPS
        runs = [x for x in OPS.read_log(w.log, 50) if x.get("plan_entry") and not x.get("refused")]
        check("the ops log records the plan, the entry, and that you pressed it on your phone",
              [x["plan_entry"] for x in runs] == [2] and runs[0]["plan_id"] == pid
              and "dashboard (owner@example.com on phone1)" in runs[0]["decided_via"], str(runs))
        check("the operator's thread is told what ran", w.told and w.told[-1][0] == "D1:1.0"
              and "2. `tailscale-on`" in w.told[-1][1] and "1." not in w.told[-1][1].split("\n", 1)[1],
              str(w.told))
        ts = f"{time.time() + 1:.6f}"
        check("after it, a typed 'yes' answers nothing: entry 1 never runs",
              w.state.answer_plan(ts, "yes") is None)
        g = w.grants.open("D1:1.0", ts, "U_ME", "yes", "tsk_turn_mine", plan=None)
        r2 = _plan_run(w, g, 1, "trader-env-set", "MOMENTUM_UNIVERSE", "pit")
        check("nor can the operator run it on that yes", r2.returncode == 1
              and _env_value(w, "MOMENTUM_UNIVERSE") is None, r2.stderr)
        why = O.plan_refusal({"plan": {"id": pid}, "ts": ts, "text": "yes"}, 1, "trader-env-set",
                             ["MOMENTUM_UNIVERSE", "pit"], w.state.plan())
        check("and a turn holding a grant on that plan is told you answered it on the card",
              "card on the dashboard" in why and "nothing left for you to run" in why, why)
        r3 = _press(w, "run", pid, [1])
        check("and the card cannot be pressed again: answered once", not r3.get("ok")
              and "already been answered" in r3["error"] and _env_value(w, "MOMENTUM_UNIVERSE") is None,
              str(r3))

        # Run all: every entry not yet run, then the plan is spent.
        (w.trader / ".env").write_text(w.env_text)
        _plan_record(w, "trader-env-set", "MOMENTUM_UNIVERSE", "pit", "--op", "tailscale-on")
        pid = _card(w)["id"]
        r = _press(w, "run", pid, "all")
        c = _card(w)
        check("Run all runs every entry, in order", r.get("ok") and r["running"] == [1, 2]
              and _env_value(w, "MOMENTUM_UNIVERSE") == "pit"
              and [e["result"]["state"] for e in c["entries"]][0] == "ok", str(c["entries"]))
        check("and a plan whose every entry ran shows spent, not runnable",
              c["status"] == "spent" and not c["runnable"])

        # Ticked nothing; an entry that is not there.
        _plan_record(w, "tailscale-on")
        pid = _card(w)["id"]
        check("Run selected with nothing ticked is refused, the plan still waiting",
              not _press(w, "run", pid, []).get("ok") and _card(w)["status"] == "waiting")
        r = _press(w, "run", pid, [5])
        check("so is an entry the plan does not have", not r.get("ok") and "no entry 5" in r["error"]
              and _card(w)["status"] == "waiting", str(r))
        check("and a plan other than the live one", not _press(w, "run", "plan_old", "all").get("ok")
              and _card(w)["status"] == "waiting")

        # Not now.
        w.calls.write_text("")
        r = _press(w, "decline", pid)
        c = _card(w)
        check("Not now declines it: nothing runs, the plan is answered no",
              r.get("ok") and r["declined"] and not _calls(w) and c["status"] == "answered"
              and c["declined"] and not c["runnable"], str(c))
        check("and the thread is told", "Not now" in w.told[-1][1])
        check("after which a typed yes answers nothing",
              w.state.answer_plan(f"{time.time() + 1:.6f}", "yes") is None)


def test_ops_plan_card_clears_for_good():
    import operator_role as O
    print("\nthe plan's card: Not now and its × clear it for good, whatever state it is in")
    w = _plan_card_world()
    os.environ["VIZ_TAILSCALE_LOGIN"] = _OWNER["login"]
    with _fake_path(w):
        _plan_record(w, "tailscale-on")
        pid = _card(w)["id"]
        r = _press(w, "dismiss", pid, owner=None)
        check("not without you: refused, and still waiting", not r.get("ok") and r.get("refused") == "owner"
              and _card(w)["status"] == "waiting" and not _card(w)["dismissed"], str(r))
        r = _press(w, "dismiss", pid, caller_role="operator", caller_thread="D1:1.0")
        check("nor from inside a turn", not r.get("ok") and r.get("refused") == "turn"
              and not _card(w)["dismissed"], str(r))
        r = _press(w, "dismiss", "plan_old")
        check("nor for a plan other than the one recorded", not r.get("ok") and "read it again" in r["error"]
              and not _card(w)["dismissed"], str(r))
        w.calls.write_text("")
        r = _press(w, "dismiss", pid)
        c = _card(w)
        check("its × on a waiting plan declines it: nothing runs, the card cleared",
              r.get("ok") and r["declined"] and r["dismissed"] and not _calls(w)
              and c["status"] == "answered" and c["declined"] and c["dismissed"] and not c["runnable"]
              and c["answered_by"] == _OWNER["login"] and c["answered_via"] == "dashboard", str(c))
        check("the thread is told", "Not now" in w.told[-1][1] and pid in w.told[-1][1])
        check("a typed yes answers it no longer",
              w.state.answer_plan(f"{time.time() + 1:.6f}", "yes") is None)
        r = _press(w, "dismiss", pid)
        check("a second × is fine: already cleared", r.get("ok") and not r["declined"] and _card(w)["dismissed"])

        _plan_record(w, "tailscale-on")
        c = _card(w)
        check("a new plan is drawn again", c["id"] != pid and c["status"] == "waiting" and not c["dismissed"])
        pid = c["id"]
        r = _press(w, "decline", pid)
        check("Not now declines it and clears its card, so a reload does not bring it back",
              r.get("ok") and r["declined"] and _card(w)["dismissed"] and _card(w)["declined"], str(r))

        for label, setup in (
                ("spent", lambda p: w.state.use_plan_entry(p, 1)),
                ("expired", lambda p: _age_plan(w, O.PLAN_TTL_S + 60)),
                ("answered by a typed yes", lambda p: _plan_turn(w, "yes")),
                ("replaced", lambda p: w.state.set_thread("D2:2.0"))):
            w.state.set_thread("D1:1.0")
            _plan_record(w, "tailscale-on")
            pid = _card(w)["id"]
            setup(pid)
            before = _card(w)
            told = len(w.told)
            r = _press(w, "dismiss", pid)
            c = _card(w)
            check(f"{label}: its × clears it, its state left as it was, nobody told it was declined",
                  before["status"] != "waiting" and r.get("ok") and not r["declined"] and c["dismissed"]
                  and c["status"] == before["status"] and not c["declined"] and len(w.told) == told,
                  f"{before['status']} -> {r} {c['status']}")
        w.state.set_thread("D1:1.0")

        _plan_record(w, "tailscale-on")
        pid = _card(w)["id"]
        w.state.answer_plan_from_dashboard(pid, [1], by="owner@example.com")
        w.state.set_entry_result(pid, 1, {"state": "running"})
        r = _press(w, "dismiss", pid)
        check("not while what you chose is running: refused, its card kept",
              not r.get("ok") and "running" in r["error"] and not _card(w)["dismissed"], str(r))
        # The bot has claimed the run, its entries not yet marked queued: only
        # _plan_runs says it is running.
        w.state.set_entry_result(pid, 1, {})
        w.pns["_plan_runs"].add(pid)
        got = []
        t = __import__("threading").Thread(target=lambda: got.append(_press(w, "decline", pid)),
                                           daemon=True)
        t.start()
        t.join(10)
        check("(nor while the bot is running it -- refused at once, never stuck on its own guard)",
              got and not got[0].get("ok") and "running" in got[0]["error"]
              and got[0].get("plan", {}).get("id") == pid, str(got) or "no answer in 10s")
        if t.is_alive():
            return                     # a deadlocked guard: nothing after this can run
        w.pns["_plan_runs"].discard(pid)
        w.state.set_entry_result(pid, 1, {"state": "ok"})
        r = _press(w, "dismiss", "")
        check("a clear names its plan: none named, refused", not r.get("ok") and "plan_id" in r["error"]
              and not _card(w)["dismissed"], str(r))

        _plan_record(w, "tailscale-on")
        pid = _card(w)["id"]
        w.state.set_thread("D2:2.0")
        r = _press(w, "dismiss", pid)
        w.state.set_thread("D1:1.0")
        check("a cleared plan is over: its thread coming back does not make it live again",
              r.get("ok") and w.state.plan() is None and _card(w)["dismissed"]
              and not _press(w, "run", pid, "all").get("ok")
              and w.state.answer_plan(f"{time.time() + 1:.6f}", "yes") is None, str(r))


def _age_plan(w, by):
    data = json.loads(w.statefile.read_text())
    data["plan"]["at"] -= by
    w.statefile.write_text(json.dumps(data))


def test_ops_plan_decline_from_the_cli():
    print("\nsilkworm ops plan --decline: a stuck plan card can always be cleared")
    w = _plan_card_world()
    with _fake_path(w):
        r = _ops_cli(w, "plan", "--decline", role="", thread="")
        check("no plan recorded: refused, saying so", r.returncode == 1 and "no plan" in r.stderr,
              r.stdout + r.stderr)
        _plan_record(w, "tailscale-on")
        pid = _card(w)["id"]
        r = _ops_cli(w, "plan", "--decline", "plan_nope", role="", thread="")
        check("another plan's id: refused, and the plan left waiting",
              r.returncode == 1 and "read it again" in r.stderr and _card(w)["status"] == "waiting",
              r.stdout + r.stderr)
        r = _ops_cli(w, "plan", "--decline", "--sideways", role="", thread="")
        check("an unknown option is a usage error", r.returncode == 2 and "usage" in r.stderr, r.stderr)
        r = _ops_cli(w, "plan", "--decline", role="implementor", thread="D5:5.0", task="tsk_other")
        check("not from another task's turn: the user's answer stays theirs",
              r.returncode == 1 and "only the operator" in r.stderr and _card(w)["status"] == "waiting",
              r.stdout + r.stderr)
        r = _ops_cli(w, "plan", "--decline", thread="D9:9.0", task="tsk_elsewhere")
        check("nor from an operator turn outside its own thread",
              r.returncode == 1 and "only the operator" in r.stderr and _card(w)["status"] == "waiting",
              r.stdout + r.stderr)
        w.calls.write_text("")
        r = _ops_cli(w, "plan", "--decline", pid, role="", thread="")
        c = _card(w)
        check("from your terminal: the waiting plan is declined, nothing runs, its card cleared",
              r.returncode == 0 and f"declined plan {pid}" in r.stdout and not _calls(w)
              and c["declined"] and c["dismissed"] and c["status"] == "answered"
              and c["answered_by"] == "cli" and c["answered_via"] == "cli", r.stdout + r.stderr + str(c))
        check("the operator's thread is told", "declined" in w.told[-1][1] and pid in w.told[-1][1],
              str(w.told[-1:]))
        check("and no typed yes answers it after",
              w.state.answer_plan(f"{time.time() + 1:.6f}", "yes") is None)
        r = _ops_cli(w, "plan", "--decline", role="", thread="")
        check("again: already cleared, said so", r.returncode == 0 and "already cleared" in r.stdout,
              r.stdout + r.stderr)

        _plan_record(w, "tailscale-on")
        pid = _card(w)["id"]
        w.state.use_plan_entry(pid, 1)
        told = len(w.told)
        r = _ops_cli(w, "plan", "--decline", "--by", "operator", task="tsk_sweep")
        c = _card(w)
        check("a spent plan, from the operator's own turn: just cleared, not declined",
              r.returncode == 0 and f"cleared plan {pid}" in r.stdout and "spent" in r.stdout
              and c["dismissed"] and c["status"] == "spent" and not c["declined"]
              and len(w.told) == told, r.stdout + r.stderr)

        _plan_record(w, "tailscale-on")
        pid = _card(w)["id"]
        w.state.answer_plan_from_dashboard(pid, [1], by="owner@example.com")
        w.state.set_entry_result(pid, 1, {"state": "queued"})
        r = _ops_cli(w, "plan", "--decline", role="", thread="")
        check("not while what was chosen is still to run", r.returncode == 1 and "running" in r.stderr
              and not _card(w)["dismissed"], r.stdout + r.stderr)


def test_ops_plan_card_runs_guarded_entries_as_their_buttons():
    import operator_role as O
    print("\nthe plan's card: a land or publish entry is done as its own dashboard button would")
    w = _plan_card_world()
    os.environ["VIZ_TAILSCALE_LOGIN"] = _OWNER["login"]
    w.state.set_plan([{"name": "land", "args": ["tsk_ab12"], "guard": "trader-config", "summary": "x"},
                      {"name": "publish", "args": ["cadence"], "guard": "publish", "summary": "y"}],
                     by="operator", thread="D1:1.0")
    pid = _card(w)["id"]
    r = _press(w, "run", pid, "all")
    check("the landing goes through /tasks, the publish through /projects",
          r.get("ok") and w.tasks_called == [{"action": "land", "id": "tsk_ab12"}]
          and w.projects_called == [{"action": "publish", "slug": "cadence", "on": True}],
          f"{w.tasks_called} {w.projects_called}")
    a, k = w.decided[0]
    check("the task records that your press decided it, with the plan and entry",
          a[0] == "tsk_ab12" and "dashboard" in k["via"] and k["lift"]["plan_id"] == pid
          and k["lift"]["entry"] == 1 and k["lift"]["via"] == "dashboard", str(w.decided))
    check("each outcome is on the card", [e["result"]["state"] for e in _card(w)["entries"]] == ["ok", "ok"])


def test_ops_plan_card_refuses_without_you():
    print("\nthe plan's card: refused unless it is verifiably you, and never from a turn or a sweep")
    w = _plan_card_world()
    os.environ["VIZ_TAILSCALE_LOGIN"] = _OWNER["login"]
    with _fake_path(w):
        _plan_record(w, "trader-env-set", "MOMENTUM_UNIVERSE", "pit")
        pid = _card(w)["id"]
        cases = {
            "no owner vouched for": _press(w, "run", pid, "all", owner=None),
            "the owner claimed by a process that is not the visualizer":
                (setattr(w.probe, "opener", 9999), _press(w, "run", pid, "all"))[1],
            "no connection to check": (setattr(w.probe, "opener", 4242),
                                       _press(w, "run", pid, "all", conn=False))[1],
            "another login": _press(w, "run", pid, "all", owner=dict(_OWNER, login="someone@else")),
            "a peer not on the tailnet": _press(w, "run", pid, "all", owner=dict(_OWNER, peer="10.0.0.5")),
        }
        for what, r in cases.items():
            check(f"refused: {what}", r.get("ok") is False and r.get("refused") == "owner", str(r))
        sweep = _press(w, "run", pid, "all", caller_role="operator", caller_thread="D1:1.0",
                       caller_task="tsk_sweep")
        check("refused from inside a turn -- a sweep stamps its role, thread and task -- even with your owner",
              sweep.get("ok") is False and sweep.get("refused") == "turn" and "sweep" in sweep["error"],
              str(sweep))
        check("so is any turn that stamps only its task or grant",
              _press(w, "run", pid, "all", caller_task="tsk_wake").get("refused") == "turn"
              and _press(w, "decline", pid, caller_grant="g1").get("refused") == "turn")
        check("and Not now is held to the same", _press(w, "decline", pid, owner=None).get("refused") == "owner")
        check("none of them answered the plan or ran anything",
              _card(w)["status"] == "waiting" and _env_value(w, "MOMENTUM_UNIVERSE") is None)
        w.drain.left = 600
        r = _press(w, "run", pid, "all")
        check("while a deploy drains the bot, nothing starts", r.get("refused") == "drain"
              and _card(w)["status"] == "waiting", str(r))
        w.drain.left = 0
        r = _ops_cli(w, "run", "trader-env-set", "MOMENTUM_UNIVERSE", "pit", "--plan-entry", "1",
                     "--by", "operator", "--notes", "sweep", task="tsk_sweep")
        check("and the sweep's own way to run an entry is refused as before",
              r.returncode == 1 and _env_value(w, "MOMENTUM_UNIVERSE") is None, r.stderr)


def test_ops_plan_card_expired_or_answered_is_disabled():
    import operator_role as O
    print("\nthe plan's card: an expired, answered or spent plan shows, and can't be pressed")
    w = _plan_card_world()
    os.environ["VIZ_TAILSCALE_LOGIN"] = _OWNER["login"]
    with _fake_path(w):
        _plan_record(w, "trader-env-set", "MOMENTUM_UNIVERSE", "pit")
        pid = _card(w)["id"]
        data = json.loads(w.statefile.read_text())
        data["plan"]["at"] -= O.PLAN_TTL_S + 60
        w.statefile.write_text(json.dumps(data))
        c = _card(w)
        check("a day old and unanswered: shown expired, not runnable",
              c["status"] == "expired" and not c["runnable"], str(c))
        r = _press(w, "run", pid, "all")
        check("and a press is refused, saying it expired", not r.get("ok") and "expired" in r["error"]
              and _env_value(w, "MOMENTUM_UNIVERSE") is None, str(r))

        _plan_record(w, "trader-env-set", "MOMENTUM_UNIVERSE", "pit")
        pid = _card(w)["id"]
        g = _plan_turn(w, "yes")
        c = _card(w)
        check("answered by a typed yes: shown answered by your message, not runnable",
              c["status"] == "answered" and c["answered_via"] == "message" and not c["runnable"], str(c))
        r = _press(w, "run", pid, "all")
        check("and the card cannot answer it a second time",
              not r.get("ok") and "already been answered" in r["error"]
              and _env_value(w, "MOMENTUM_UNIVERSE") is None, str(r))
        r = _plan_run(w, g, 1, "trader-env-set", "MOMENTUM_UNIVERSE", "pit")
        check("while the typed yes still runs it, as before", r.returncode == 0
              and _env_value(w, "MOMENTUM_UNIVERSE") == "pit", r.stdout + r.stderr)
        c = _card(w)
        check("and once it has, the card shows it spent", c["status"] == "spent" and c["entries"][0]["used"])
        w.state.set_thread("D9:9.0")
        _plan_record(w, "tailscale-on", task="tsk_x")      # refused: not its thread now
        w.state.set_thread("D1:1.0")
        _plan_record(w, "tailscale-on")
        w.state.set_thread("D2:2.0")
        c = _card(w)
        check("the operator moved threads: shown replaced, not runnable",
              c["status"] == "replaced" and not c["runnable"], str(c))
        check("and refused", "another thread" in _press(w, "run", c["id"], "all").get("error", ""))


#: The plan card's harness: the page's own card and buttons, against cards
#: the bot made, with /api/plan answered as the test says.
def _plan_card_harness():
    head = MOTION_HARNESS[:MOTION_HARNESS.index("(0, eval)(SRC")]
    return head + r"""
const FIX = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
let CONFIRM = true;
const asked = [];
globalThis.confirm = m => { asked.push(m); return CONFIRM; };
let planAnswer = {ok: true, plan: FIX.waiting};
let pressAnswer = {ok: true, running: [2], plan: FIX.ran};
answer = (url, b) => url === "/api/plan" ? (b.action === "show" ? planAnswer : pressAnswer) : BASE(url);
(0, eval)(SRC + `
;globalThis.__m = {loadPlan, planCardHtml, planPress, planTickEntry, drawPlan, inboxHtml,
  setActive: a => { active = a; }, plan: () => planView, setPlan: p => { planView = p; }};`);
const M = globalThis.__m, out = {};
const flushAsync = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r)); };
const presses = () => calls.filter(c => c.url === "/api/plan" && c.body && c.body.action !== "show").map(c => c.body);
const btn = (pact, plan) => { const e = E("BUTTON"); Object.assign(e.dataset, {pact, plan}); return e; };
const box = (plan, n, on) => { const e = E("INPUT"); Object.assign(e.dataset, {plan, n: String(n)}); e.checked = on; return e; };
(async () => {
  out.waiting = M.planCardHtml(FIX.waiting, "thread");
  out.expired = M.planCardHtml(FIX.expired, "thread");
  out.spent = M.planCardHtml(FIX.spent, "thread");
  out.answered = M.planCardHtml(FIX.answered, "thread");
  out.ran = M.planCardHtml(FIX.ran, "thread");
  out.ranLong = M.planCardHtml(FIX.ranLong, "thread");
  out.replaced = M.planCardHtml(FIX.replaced, "thread");
  out.dismissed = M.planCardHtml(FIX.dismissed, "thread");
  out.dismissedInbox = M.planCardHtml(FIX.dismissed, "inbox");
  out.lapsed = M.planCardHtml(FIX.lapsed, "thread");
  out.inboxOf = {};
  for (const k of ["expired", "spent", "answered", "replaced", "lapsed"]) out.inboxOf[k] = M.planCardHtml(FIX[k], "inbox");
  // Needs you: the plan's item, against what the plan's own read says now.
  const item = {kind: "plan", id: FIX.waiting.id, thread: FIX.waiting.thread, at: FIX.waiting.at,
                title: "The operator's plan: 2 steps", entries: FIX.waiting.entries.map(e => ({n: e.n, command: e.command}))};
  const inbox = {groups: [{project: "__operator__", title: "The operator", items: [item]}]};
  const inboxWith = p => { M.setPlan(p); return M.inboxHtml(inbox); };
  out.inboxWaiting = inboxWith(FIX.waiting);
  out.inboxUnread = inboxWith(null);
  out.inboxSpent = inboxWith(FIX.spent);
  out.inboxExpired = inboxWith(FIX.expired);
  out.inboxDismissed = inboxWith(Object.assign({}, FIX.spent, {dismissed: true, entries: FIX.spent.entries}));
  out.inboxNewer = inboxWith(Object.assign({}, FIX.replaced, {at: FIX.waiting.at + 60}));
  M.setPlan(null);
  await M.loadPlan(); await flushAsync();
  M.setActive({key: FIX.waiting.thread}); M.drawPlan();
  out.inThread = byId("tplan").innerHTML;
  M.setActive({key: "C9:9.9"}); M.drawPlan();
  out.otherThread = byId("tplan").innerHTML;
  M.setActive({key: FIX.waiting.thread});
  const id = FIX.waiting.id;
  await M.planPress(btn("selected", id)); await flushAsync();
  out.nothingTicked = presses();
  M.planTickEntry(box(id, 2, true));
  out.afterTick = byId("tplan").innerHTML;
  CONFIRM = false;
  await M.planPress(btn("selected", id)); await flushAsync();
  out.declinedConfirm = presses();
  CONFIRM = true;
  await M.planPress(btn("selected", id)); await flushAsync();
  out.selected = presses();
  out.confirmText = asked[asked.length - 1];
  out.afterPress = byId("tplan").innerHTML;
  planAnswer = {ok: true, plan: FIX.waiting};
  pressAnswer = {ok: true, running: [1, 2], plan: FIX.ran};
  await M.loadPlan(); await flushAsync();
  await M.planPress(btn("all", id)); await flushAsync();
  out.all = presses().slice(-1);
  await M.planPress(btn("all", id)); await flushAsync();
  out.allAgain = presses().length;       // the card it drew is answered: nothing more goes
  await M.loadPlan(); await flushAsync();
  out.beforeDecline = byId("tplan").innerHTML;
  pressAnswer = {ok: true, declined: true, dismissed: true, plan: FIX.declined};
  const dp = M.planPress(btn("decline", id));
  out.declineAtOnce = byId("tplan").innerHTML;        // before the bot has answered
  await dp; await flushAsync();
  out.decline = presses().slice(-1);
  out.afterDecline = byId("tplan").innerHTML;
  out.declineToast = toastEl.text;
  planAnswer = {ok: true, plan: FIX.declined};          // a reload: the bot's own read
  await M.loadPlan(); await flushAsync();
  out.afterReload = byId("tplan").innerHTML;
  // The ×, on a plan that can no longer be pressed: refused, the line comes back.
  planAnswer = {ok: true, plan: FIX.answered};
  await M.loadPlan(); await flushAsync();
  out.lineShown = byId("tplan").innerHTML;
  pressAnswer = {ok: false, error: "refused: the bot is restarting"};
  const xp = M.planPress(btn("dismiss", FIX.answered.id));
  out.xAtOnce = byId("tplan").innerHTML;
  await xp; await flushAsync();
  out.xRefused = presses().slice(-1);
  out.xRolledBack = byId("tplan").innerHTML;
  out.xRefusedToast = toastEl.text;
  pressAnswer = {ok: true, dismissed: true, plan: Object.assign({}, FIX.answered, {dismissed: true})};
  await M.planPress(btn("dismiss", FIX.answered.id)); await flushAsync();
  out.xGone = byId("tplan").innerHTML;
  planAnswer = {ok: true, plan: Object.assign({}, FIX.answered, {dismissed: true})};
  await M.loadPlan(); await flushAsync();
  out.xAfterReload = byId("tplan").innerHTML;
  planAnswer = {ok: true, plan: FIX.waiting};
  await M.loadPlan(); await flushAsync();
  pressAnswer = {ok: false, refused: "owner", error: "refused: only you can answer the operator's plan"};
  await M.planPress(btn("all", id)); await flushAsync();
  out.refusedToast = toastEl.text;
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_dashboard_plan_card_renders_and_presses():
    import re
    import operator_role as O
    import visualizer as V
    print("\nthe dashboard's plan card: drawn from the bot's card, its buttons the plan's answer")
    page = V.PAGE
    js = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
    check("the thread has a slot for it", '<div id="tplan"></div>' in page)
    check("it is polled, and its route is /api/plan", "pollPlan = poller(" in js
          and '"/api/plan"' in js and 'elif url.path == "/api/plan":' in Path(V.__file__).read_text())
    check("the inbox's typed-yes Run plan is gone: the card answers the plan",
          '"run-plan"' not in js and "runPlan(" not in js)
    src = Path(V.__file__).read_text()
    route = src[src.index('elif url.path == "/api/plan":'):src.index('elif url.path == "/api/seen":')]
    check("the route vouches for the owner only on a press, as /api/send does, dropping one the page sent",
          'payload.pop("dashboard_owner", None)' in route and "self._owner()" in route
          and '!= "show"' in route)
    tmp = Path(tempfile.mkdtemp())
    st = O.State(tmp / "op.json")
    st.set_thread("D1:1.0")
    st.set_plan([{"name": "trader-env-set", "args": ["MOMENTUM_UNIVERSE", "pit"]},
                 {"name": "tailscale-on", "args": []}], by="operator", thread="D1:1.0")
    import ops as OPS
    desc = {d["name"]: d["description"] for d in OPS.describe()}
    card = lambda: dict(O.plan_card(st.plan_view(), desc), running=False)
    waiting = card()
    pid = waiting["id"]
    expired = dict(O.plan_card(st.plan_view(now=time.time() + O.PLAN_TTL_S + 5), desc), running=False)
    st.answer_plan_from_dashboard(pid, [2], by="owner@example.com")
    st.use_plan_entry(pid, 2, run_by="dashboard")
    st.set_entry_result(pid, 2, {"state": "failed", "exit": 3, "tail": "tailscale: <not connected>",
                                 "at": time.time()})
    ran = card()
    st.set_entry_result(pid, 2, {"state": "failed", "exit": 3, "tail": "tailscale: <not connected>",
                                 "at": time.time() - 3600})
    ran_long = card()
    st.set_entry_result(pid, 2, {"state": "ok", "tail": "serving"})
    st.use_plan_entry(pid, 1)
    spent = card()
    st2 = O.State(tmp / "op2.json"); st2.set_thread("D1:1.0")
    st2.set_plan([{"name": "tailscale-on", "args": []}], by="operator", thread="D1:1.0")
    st2.answer_plan_from_dashboard(st2.plan_view()["id"], [], by="owner@example.com")
    answered = dict(O.plan_card(st2.plan_view(), desc), running=False)
    st3 = O.State(tmp / "op3.json"); st3.set_thread("D1:1.0")
    st3.set_plan([{"name": "tailscale-on", "args": []}], by="operator", thread="D1:1.0")
    st3.set_thread("D2:2.0")
    replaced = dict(O.plan_card(st3.plan_view(), desc), running=False)
    st4 = O.State(tmp / "op4.json"); st4.set_thread("D1:1.0")
    st4.set_plan([{"name": "tailscale-on", "args": []}], by="operator", thread="D1:1.0")
    st4.use_plan_entry(st4.plan_view()["id"], 1)
    st4.dismiss_plan(by="owner@example.com")
    dismissed = dict(O.plan_card(st4.plan_view(), desc), running=False)
    # Read while it waited, and not since: its day has run out on the clock.
    lapsed = dict(waiting, at=waiting["at"] - O.PLAN_TTL_S - 60,
                  expires_at=waiting["expires_at"] - O.PLAN_TTL_S - 60)
    declined = dict(waiting, status="answered", runnable=False, declined=True, dismissed=True,
                    answered_via="dashboard", answered_by="owner@example.com")
    fix = {"waiting": waiting, "expired": expired, "ran": ran, "ranLong": ran_long, "spent": spent,
           "answered": answered, "replaced": replaced, "dismissed": dismissed, "lapsed": lapsed,
           "declined": declined}
    node = shutil.which("node")
    if not node:
        print("  … node not installed — the card checked by text only")
        return
    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    (d / "drive.js").write_text(_plan_card_harness())
    (d / "fix.json").write_text(json.dumps(fix))
    p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js"), "", str(d / "fix.json")],
                       capture_output=True, text=True, timeout=90)
    if p.returncode != 0:
        check("the plan card harness runs the page's script", False, p.stderr.strip()[-900:])
        return
    o = json.loads(p.stdout)
    wt = o["waiting"]
    boxes = re.findall(r'<input type="checkbox"[^>]*>', wt)
    check("waiting: a numbered entry each, with a checkbox, unticked and enabled",
          len(boxes) == 2 and not any("disabled" in b or "checked" in b for b in boxes)
          and "<b>1.</b>trader-env-set MOMENTUM_UNIVERSE pit" in wt and "<b>2.</b>tailscale-on" in wt, wt[:900])
    check("each with its plain description and what it changes",
          "set one allowed key of trader&#39;s .env" in wt or "set one allowed key of trader's .env" in wt)
    check("the change said from the entry's own arguments", "Changes: sets MOMENTUM_UNIVERSE=pit" in wt, wt[:1500])
    pacts = lambda h: [(m.group(1), " disabled" in m.group(0))
                       for m in re.finditer(r'<button[^>]*data-pact="(\w+)"[^>]*>', h)]
    btns = dict(pacts(wt))
    check("Run all, Not now and its × pressable; Run selected not until something is ticked",
          btns == {"dismiss": False, "all": False, "selected": True, "decline": False}, str(btns))
    check("it says how long it waits, and that a typed yes still works", "Waits for your answer until" in wt
          and "say “yes”" in wt)
    lines = {"expired": "expired unanswered", "spent": "all 2 steps have run",
             "answered": "you said not now", "replaced": "replaced — the operator moved thread",
             "lapsed": "expired unanswered", "ranLong": "answered on the dashboard · 1 failed"}
    for name, said in lines.items():
        h = o[name]
        check(f"{name}: not a card to press -- one line saying so, with only its ×",
              'class="icard k-plan opcard pline"' in h and said in h
              and pacts(h) == [("dismiss", False)] and "<input" not in h and "Run all" not in h
              and "Not now" not in h, h[:700])
        check(f"{name}: and nothing at all on Needs you", o["inboxOf"].get(name, "") == "",
              o["inboxOf"].get(name, "")[:300])
    check("cleared (its ×, Not now or ops plan --decline): drawn nowhere, for good",
          o["dismissed"] == "" and o["dismissedInbox"] == "")
    check("a run you pressed keeps its full card ten minutes, results showing, nothing left to press",
          "✗ failed (exit 3)" in o["ran"] and "tailscale: &lt;not connected&gt;" in o["ran"]
          and pacts(o["ran"]) == [("dismiss", False)] and "pline" not in o["ran"], o["ran"][:1500])
    check("Needs you shows the plan's card while it waits",
          'data-id="%s"' % pid in o["inboxWaiting"] and "Run all" in o["inboxWaiting"]
          and 'data-id="%s"' % pid in o["inboxUnread"])
    for k in ("inboxSpent", "inboxExpired", "inboxDismissed", "inboxNewer"):
        check(f"{k}: the item's plan, as the bot reads it now, is not drawn as something to answer",
              'data-pact="all"' not in o[k] and 'data-id="%s"' % pid not in o[k], o[k][:500])
        check(f"{k}: nor counted -- no empty operator's group, Needs you says nothing needs you",
              "__operator__" not in o[k] and "Nothing needs you" in o[k], o[k][:500])
    check("drawn in the operator's thread when it is open, and in no other",
          'data-id="%s"' % pid in o["inThread"] and o["otherThread"] == "")
    check("Run selected with nothing ticked sends nothing", o["nothingTicked"] == [])
    check("ticking an entry enables Run selected, counting it",
          ">Run selected (1)<" in o["afterTick"] and 'data-n="2" checked' in o["afterTick"], o["afterTick"][:1200])
    check("declining the confirmation sends nothing", o["declinedConfirm"] == [])
    check("Run selected sends a run of just the ticked entries, for this plan",
          o["selected"] == [{"action": "run", "plan_id": pid, "entries": [2]}], str(o["selected"]))
    check("having asked first, naming the step", "1 step" in o["confirmText"] and "2. tailscale-on" in o["confirmText"]
          and "1. trader-env-set" not in o["confirmText"], o["confirmText"])
    check("the result streams back into the card", "✗ failed (exit 3)" in o["afterPress"], o["afterPress"][:600])
    check("Run all sends entries all", o["all"] == [{"action": "run", "plan_id": pid, "entries": "all"}], str(o["all"]))
    check("once a press is answered, its drawn-disabled card sends nothing more",
          o["allAgain"] == 2, str(o["allAgain"]))
    check("Not now sends decline", o["decline"] == [{"action": "decline", "plan_id": pid}], str(o["decline"]))
    check("the waiting card was there to decline", 'data-id="%s"' % pid in o["beforeDecline"]
          and 'data-pact="decline"' in o["beforeDecline"], o["beforeDecline"][:300])
    check("and it goes at once, before the bot answers", o["declineAtOnce"] == "", o["declineAtOnce"][:300])
    check("and stays gone once it has, saying nothing in it runs",
          o["afterDecline"] == "" and "nothing in it runs" in o["declineToast"], o["declineToast"])
    check("and after a reload: the bot's own read says it is cleared", o["afterReload"] == "")
    check("a plan done with shows its line", "pline" in o["lineShown"] and "you said not now" in o["lineShown"])
    check("its × sends dismiss, for that plan",
          o["xRefused"] == [{"action": "dismiss", "plan_id": answered["id"]}], str(o["xRefused"]))
    check("the line goes at once", o["xAtOnce"] == "", o["xAtOnce"][:300])
    check("refused, it comes back, saying why",
          "pline" in o["xRolledBack"] and "the bot is restarting" in o["xRefusedToast"],
          o["xRolledBack"][:300] + " / " + o["xRefusedToast"])
    check("answered, it is gone, and stays gone after a reload", o["xGone"] == "" and o["xAfterReload"] == "")
    check("a refusal is said", "only you can answer" in o["refusedToast"], o["refusedToast"])


def test_ops_plan_card_runs_what_the_plan_recorded():
    import ops as OPS
    import operator_role as O
    print("\nthe plan's card: what runs is what the plan recorded as chosen, each once, and "
          "an entry that never started is not called run")
    w = _plan_card_world()
    os.environ["VIZ_TAILSCALE_LOGIN"] = _OWNER["login"]
    with _fake_path(w):
        _plan_record(w, "trader-env-set", "MOMENTUM_UNIVERSE", "pit", "--op", "tailscale-on")
        pid = _card(w)["id"]
        r = _press(w, "run", pid, [2, "1", 2])
        c = _card(w)
        check("a hand-made [2, '1', 2] runs 1 then 2, once each, as the plan recorded them",
              r.get("ok") and r["running"] == [1, 2] and _env_value(w, "MOMENTUM_UNIVERSE") == "pit"
              and [e["result"]["state"] for e in c["entries"]][0] == "ok"
              and "already run" not in (c["entries"][1]["result"]["tail"] or ""), str(c["entries"]))

        # The op already running: it never started, so it is not spent.
        (w.trader / ".env").write_text(w.env_text)
        _plan_record(w, "trader-env-set", "MOMENTUM_UNIVERSE", "pit")
        pid = _card(w)["id"]
        with OPS._running_guard:
            OPS._running.add(("trader-env-set", "trader"))
        try:
            r = _press(w, "run", pid, "all")
        finally:
            with OPS._running_guard:
                OPS._running.discard(("trader-env-set", "trader"))
        e = _card(w)["entries"][0]
        check("an op already running: failed, saying so, and not marked run",
              e["result"]["state"] == "failed" and "already running" in e["result"]["tail"]
              and not e["used"] and _card(w)["status"] == "answered"
              and _env_value(w, "MOMENTUM_UNIVERSE") is None, str(e))

        # Chosen in time, run after the day is up: still runs.
        _plan_record(w, "trader-env-set", "MOMENTUM_UNIVERSE", "pit", "--op", "tailscale-on")
        pid = _card(w)["id"]
        snap, why = w.state.answer_plan_from_dashboard(pid, [1], by="me")
        data = json.loads(w.statefile.read_text())
        data["plan"]["at"] -= O.PLAN_TTL_S + 60
        w.statefile.write_text(json.dumps(data))
        check("an entry the press chose is claimed by that press though the day ran out mid-run",
              w.state.use_plan_entry(pid, 1, answer=snap["answered"]))
        check("but not one it did not choose", not w.state.use_plan_entry(pid, 2, answer=snap["answered"]))
        check("nor by any other answer", not w.state.use_plan_entry(pid, 1, answer="dashboard:x"))


def test_ops_plan_card_restart_says_what_it_cut_off():
    import operator_role as O
    print("\nthe plan's card: entries a restart cut off are said to be, not left running")
    tmp = Path(tempfile.mkdtemp())
    st = O.State(tmp / "op.json")
    st.set_thread("D1:1.0")
    rec = st.set_plan([{"name": "tailscale-on"}, {"name": "project-test", "args": ["alpha"]},
                       {"name": "trader-launchd-retire", "args": ["com.x"]}], by="operator",
                      thread="D1:1.0")
    st.answer_plan_from_dashboard(rec["id"], [1, 2, 3], by="me")
    st.set_entry_result(rec["id"], 1, {"state": "ok", "tail": "done"})
    st.set_entry_result(rec["id"], 2, {"state": "running"})
    st.set_entry_result(rec["id"], 3, {"state": "queued"})
    check("the running and the waiting one are changed", st.clear_interrupted_results() == [2, 3])
    res = [e.get("result") for e in st.plan_view()["entries"]]
    check("each failed, saying the restart cut it off, the finished one untouched",
          res[0] == {"state": "ok", "tail": "done"}
          and res[1]["state"] == "failed" and "while it was running" in res[1]["tail"]
          and res[2]["state"] == "failed" and "never ran" in res[2]["tail"], str(res))
    check("and a second sweep changes nothing", st.clear_interrupted_results() == [])
    src = (BASE / "bot.py").read_text()
    check("the bot sweeps it at startup, beside interrupted landings",
          "OPERATOR.clear_interrupted_results()" in src
          and src.index("clear_interrupted_landings()\n    except")
          < src.index("OPERATOR.clear_interrupted_results()"))


# --- a project's own page ------------------------------------------------------------
# 2026-11-22: "where is this project" was spread over the overview card, the
# board, the inbox, the health brief and git. ?view=project&slug=<p> puts it on
# one read-only page: readiness, health with what is decided or done about each
# check, an app's App Store and TestFlight state (the stored probe results: no
# network call on load), the fortnight's landings and release tags, what waits
# on you, what is queued and running, and the unmerged branches.

#: Strings planted where a secret could be, and must never reach the page.
_PAGE_SECRETS = ("sk-ant-PLANTED-1", "ghp_PLANTEDTOKEN2", "PLANTED-APP-PASSWORD-3",
                 "PLANTED-PROBE-CACHE-4", "PLANTED-ENV-5")


def _project_page_world():
    """A real TaskStore with a code project's work and an app's, health rows
    for both, and records that carry planted secrets; the bot's own /tasks
    `project` route over them. (handle, ids, store)."""
    import board as B
    import health as HL
    import tasks as T
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    ids = {}
    now = time.time()

    def make(key, title, state, project="alpha", **kw):
        path = {T.PROPOSED: [], T.QUEUED: [], T.RUNNING: [T.RUNNING],
                T.AWAITING_APPROVAL: [T.RUNNING, T.AWAITING_APPROVAL],
                T.NEEDS_INPUT: [T.RUNNING, T.NEEDS_INPUT], T.FAILED: [T.RUNNING, T.FAILED],
                T.DONE: [T.RUNNING, T.DONE]}[state]
        t = st.create(title + "\nWhat done looks like: it works.", title=title,
                      state=T.PROPOSED if state == T.PROPOSED else T.QUEUED,
                      role=kw.pop("role", "implementor"), project=project,
                      source="ideation", driver="queue")
        for s in path:
            st.transition(t["id"], s, "")
        if kw:
            st.update(t["id"], **kw)
        ids[key] = t["id"]
        return t["id"]
    make("proposal", "Cache the release plan", T.PROPOSED)
    make("approval", "Show sync errors", T.AWAITING_APPROVAL,
         result={"review": {"ok": True, "summary": "clean"}})
    make("queued", "Queued work", T.QUEUED)
    make("queued2", "Second in line", T.QUEUED)
    make("running", "Running work", T.RUNNING)
    make("landed", "Landed lately", T.DONE,
         result={"landing": {"eligible": True, "landed": True, "head": "abcdef1234567",
                             "base": "main", "at": now - 3 * 86400, "published": False}})
    make("old", "Landed long ago", T.DONE,
         result={"landing": {"eligible": True, "landed": True, "head": "0011223344",
                             "at": now - 30 * 86400}})
    make("stranded", "Stranded branch", T.DONE)
    make("other", "Another project's work", T.QUEUED, project="beta")
    make("app_q", "Ship the paywall", T.QUEUED, project="mondays")
    make("app_l", "Fix the crop", T.DONE, project="mondays",
         result={"landing": {"eligible": True, "landed": True, "head": "feedbeef99",
                             "at": now - 86400}})
    unmerged = [{"id": ids["stranded"], "project": "alpha", "branch": "silkworm/" + ids["stranded"],
                 "commits": 3, "local": True, "remote": False, "title": "x"},
                {"id": ids["other"], "project": "beta", "branch": "silkworm/" + ids["other"],
                 "commits": 1, "local": True}]
    projects = {
        "alpha": {"slug": "alpha", "title": "Alpha", "test_cmd": "make test",
                  "auto_merge": True, "publish": False, "ci_gate": "all",
                  "scope": {"cwd": "/tmp/alpha", "env": {"TOKEN": _PAGE_SECRETS[4]}},
                  "mail": {"password": _PAGE_SECRETS[2]}, "brief": _PAGE_SECRETS[0]},
        "mondays": {"slug": "mondays", "title": "Mondays", "test_cmd": "",
                    "auto_merge": False, "publish": False, "scope": {"cwd": "/tmp/mondays"},
                    "main_failing": {"sha": "f181f85", "tests": ["testA"], "since": now - 3600}},
    }
    rows = [
        {"project": "alpha", "check": "ci", "status": "fail", "summary": "main is red: test_clock",
         "evidence": ["test_clock failed", "run 1234"], "link": "https://github.com/o/alpha/actions/runs/1",
         "last_changed": now - 7200, "checked_at": now - 600, "first_seen": now - 86400 * 9,
         "fingerprint": "ci@abc", "proposal": "", "attention": "",
         "token": _PAGE_SECRETS[1], "cache": {"logs": _PAGE_SECRETS[3]}},
        {"project": "mondays", "check": "app-store", "status": "warn",
         "summary": "1.3 is processed and not submitted", "last_changed": now - 86400,
         "checked_at": now - 900, "link": "https://appstoreconnect.apple.com/apps/1/appstore",
         "secret": _PAGE_SECRETS[0]},
        {"project": "mondays", "check": "testflight", "status": "ok",
         "summary": "build 47 VALID", "last_changed": now - 2 * 86400, "checked_at": now - 900},
        {"project": "mondays", "check": "backend-drift", "status": "unknown",
         "summary": "no credential", "last_known": {"status": "ok", "since": now - 10 * 86400},
         "last_changed": now - 3600, "checked_at": now - 900},
        {"project": "beta", "check": "ci", "status": "fail", "summary": "beta is red"},
    ]
    registry = {"app-store": types.SimpleNamespace(decision=lambda r: "submit 1.3 for review, or hold it",
                                                   short=None, identity=None)}
    tags = {"alpha": [{"tag": "app/v1.2.0", "at": now - 2 * 86400},
                      {"tag": "app/v1.1.0", "at": now - 40 * 86400}],
            "mondays": [{"tag": "ios/v1.3.0", "at": now - 86400}]}
    rel = {"alpha": {"targets": [{"target": "app", "ship": "tag", "commits": 2,
                                  "version": "1.2.1", "tag": "app/v1.2.1"}]}}
    reads = []
    unread = False

    def action_of(rec):
        reads.append(rec)
        return HL.action(rec, st, registry=registry)
    real_project_page = B.project_page
    board_ns = types.SimpleNamespace(**{k: getattr(B, k) for k in dir(B) if not k.startswith("__")})
    board_ns.project_page = lambda *a, **k: real_project_page(*a, **{**k, "registry": registry})
    handle = _bot_func(
        "handle_tasks", task_store=st, tasks=T, board=board_ns,
        health=types.SimpleNamespace(action=lambda rec, store, warn_after=3.0: action_of(rec)),
        project_store=types.SimpleNamespace(get=lambda s: projects.get(s)),
        board_survey=lambda: unmerged, health_rows=lambda: (None if unread else rows),
        OPERATOR=types.SimpleNamespace(thread=lambda: "D1:1.0"),
        board_release=lambda s: rel.get(s), board_release_tags=lambda s: tags.get(s, []),
        HEALTH_WARN_DAYS=3.0, log=logging.getLogger("test"))
    def set_unread(on):
        nonlocal unread
        unread = on
    handle.set_unread = set_unread
    return handle, ids, st, reads


def test_project_page_reads_one_project():
    print("\na project's page: its readiness, health, store state, landings, releases, "
          "needs-you items, queue and branches, read-only and with no secrets")
    import board as B
    handle, ids, st, reads = _project_page_world()
    before = json.dumps(st.all(), sort_keys=True)
    a = handle({"action": "project", "project": "alpha"})
    m = handle({"action": "project", "project": "mondays"})
    check("the route answers for a project", a.get("ok") and m.get("ok"), str(a)[:300])
    check("and refuses one it does not know, rather than an empty page",
          handle({"action": "project", "project": "nope"}).get("ok") is False
          and handle({"action": "project", "project": ""}).get("ok") is False)
    pa, pm = a["page"], m["page"]
    check("the header: title and readiness, from the project record",
          pa["title"] == "Alpha" and pa["readiness"] == {"test_cmd": "make test", "auto_merge": True,
                                                         "publish": False, "unready": ""}
          and pa["ci_gate"] == "all", str(pa["readiness"]))
    check("an unready app says why, and its red base says so",
          "needs a test command and auto-merge" in pm["readiness"]["unready"]
          and pm["main_failing"] and "mondays main is failing" in pm["main_failing"]["line"],
          str(pm["readiness"]))
    ci = pa["health"][0]
    check("its health: the stored result with status, summary, link and since",
          [c["check"] for c in pa["health"]] == ["ci"] and ci["status"] == "fail"
          and ci["summary"] == "main is red: test_clock" and ci["link"].endswith("/runs/1")
          and ci["since"] and ci["evidence"] == ["test_clock failed", "run 1234"], str(ci))
    check("with what is being done about it (health.action)",
          ci["action_kind"] == "eligible" and "fix may be proposed" in ci["action"], str(ci))
    store_ = {c["check"]: c for c in pm["app"] or []}
    check("an app's App Store and TestFlight state, in that order",
          [c["check"] for c in pm["app"]] == ["app-store", "testflight"]
          and store_["testflight"]["summary"] == "build 47 VALID", str(pm["app"]))
    check("with the owner's decision where the check names one",
          store_["app-store"]["action_kind"] == "decision"
          and store_["app-store"]["action"] == "decide: submit 1.3 for review, or hold it",
          str(store_["app-store"]))
    check("a code project has no store section", pa["app"] is None)
    drift = next(c for c in pm["health"] if c["check"] == "backend-drift")
    check("a check that cannot look says what it last saw",
          drift["status"] == "unknown" and drift["last_known"] == "ok", str(drift))
    check("an ok check is not asked what is done about it",
          all(r.get("status") != "ok" for r in reads))
    check("landings of the last fortnight, newest first, the older one left out",
          [l["id"] for l in pa["landed"]] == [ids["landed"]]
          and pa["landed"][0]["head"] == "abcdef12" and pa["landed"][0]["base"] == "main"
          and pa["landed"][0]["published"] is False, str(pa["landed"]))
    check("release tags of the fortnight with their dates, and what is pending",
          [t["tag"] for t in pa["releases"]] == ["app/v1.2.0"]
          and pa["release"]["targets"][0]["commits"] == 2
          and [t["tag"] for t in pm["releases"]] == ["ios/v1.3.0"], str(pa["releases"]))
    needs = {i["id"]: i["actions"] for i in pa["needs"]}
    check("what waits on you here: inbox.py's own items and buttons, only this project's",
          needs == {ids["proposal"]: ["accept", "dismiss"],
                    ids["approval"]: ["approve", "rework", "dismiss"]}, str(needs))
    dec = [i for i in pm["needs"] if i["kind"] == "decision"]
    check("and an app's decision is one of them, with Discuss in the operator's thread",
          dec and dec[0]["actions"] == ["discuss"] and dec[0]["thread"] == "D1:1.0", str(pm["needs"]))
    handle.set_unread(True)
    u = handle({"action": "project", "project": "mondays"})["page"]
    handle.set_unread(False)
    check("an unreadable health store is said, never taken for no checks",
          u["health_unread"] is True and u["health"] == [] and pm["health_unread"] is False)
    check("queued in the runner's order with each one's place; running apart",
          [t["id"] for t in pa["queued"]] == [ids["queued"], ids["queued2"]]
          and [t["queue_pos"] for t in pa["queued"]] == [1, 2]
          and [t["id"] for t in pa["running"]] == [ids["running"]], str(pa["queued"]))
    check("its unmerged branches, not another project's",
          [(u["id"], u["commits"], u["branch"]) for u in pa["unmerged"]]
          == [(ids["stranded"], 3, "silkworm/" + ids["stranded"])]
          and pa["unmerged"][0]["title"] == "Stranded branch", str(pa["unmerged"]))
    check("nothing of another project anywhere on it",
          ids["other"] not in json.dumps(pa) and "beta is red" not in json.dumps(pa))
    whole = json.dumps(a) + json.dumps(m)
    leaked = [s for s in _PAGE_SECRETS if s in whole]
    check("the read returns no secrets: nothing planted in the project or health records",
          not leaked, str(leaked))
    check("nor the project's checkout or environment",
          "/tmp/alpha" not in whole and '"scope"' not in whole and '"env"' not in whole)
    check("and reading it moved nothing", json.dumps(st.all(), sort_keys=True) == before)
    import visualizer as V
    check("the dashboard treats it as a read (cached, never an action)",
          "project" in V.READ_TASK_ACTIONS)
    dash = (BASE / "bin" / "dashshots").read_text()
    check("and the screenshot stand-in passes it through as one",
          '"inbox", "project", "page"}' in dash and 'f"/?view=project&slug={p}"' in dash
          and 'f"/?view=board&slug={p}"' in dash)
    check("it is pure over what it is handed: no probe module, no network",
          "health_probes" not in (BASE / "board.py").read_text()
          and "urllib" not in (BASE / "board.py").read_text())


def _project_page_harness():
    head = MOTION_HARNESS[:MOTION_HARNESS.index("(0, eval)(SRC")]
    return head + r"""
const FIX = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
let offline = false;
answer = (url, b) => (b && b.action === "project")
  ? (offline ? {ok: false, error: "bot is offline"} : (FIX.pages[b.project] || {ok: false, error: "unknown"}))
  : (b && b.action === "inbox") ? FIX.inbox : BASE(url);
(0, eval)(SRC + `
;globalThis.__m = {openProjectPage, openLink, cardHtml, boardCard, inboxHtml, renderDetail, setBoardProject,
                   chatHead, page: () => pageSlug, board: () => boardProject};`);
const M = globalThis.__m, out = {};
const flushAsync = async () => { for (let i = 0; i < 12; i++) await new Promise(r => setImmediate(r)); };
(async () => {
  M.openProjectPage("alpha"); await flushAsync();
  out.alpha = byId("bover").innerHTML;
  out.alphaBoard = byId("bboard").innerHTML;
  out.alphaOpen = byId("boardmodal").style.display;
  M.openProjectPage("mondays"); await flushAsync();
  out.mondays = byId("bover").innerHTML;
  offline = true;
  M.openProjectPage("alpha"); await flushAsync();
  out.offlineFresh = byId("bover").innerHTML;
  offline = false;
  M.setBoardProject(null);
  out.afterBack = {page: M.page(), board: M.board()};
  await M.openLink(new URLSearchParams("view=project&slug=mondays")); await flushAsync();
  out.link = {page: M.page(), html: byId("bover").innerHTML.includes("App Store")};
  await M.openLink(new URLSearchParams('view=project&slug=alpha&sec=a"b')); await flushAsync();
  out.badSec = {page: M.page(), drawn: byId("bover").innerHTML.includes("<h3>Alpha")};
  await M.openLink(new URLSearchParams("view=board&slug=alpha")); await flushAsync();
  out.boardLink = {page: M.page(), board: M.board()};
  // The links in.
  out.threadCard = M.cardHtml({key: "D1:1.0", title: "a thread", project: "alpha", turns: 2, updated: 1});
  out.chatHead = M.chatHead({key: "D1:1.0", title: "a thread", project: "alpha"}, null, 0, 0);
  M.setBoardProject("");
  out.taskCard = M.boardCard({id: "tsk_1", title: "work", project: "alpha", role: "implementor",
                              state: "queued", created: 1});
  out.detail = M.renderDetail({id: "tsk_1", title: "work", project: "alpha", role: "implementor",
                               state: "queued", created: 1, events: []});
  out.inbox = M.inboxHtml(FIX.inbox);
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_dashboard_project_page_renders():
    import re
    import visualizer as V
    print("\nthe project page, drawn from the route's answer for a code project and an app")
    handle, ids, _st, _ = _project_page_world()
    fix = {"pages": {s: handle({"action": "project", "project": s}) for s in ("alpha", "mondays")},
           "inbox": {"ok": True, "count": 2, "groups": [
               {"project": "alpha", "title": "Alpha", "items": [
                   {"kind": "proposal", "label": "Proposal", "id": "tsk_p", "state": "proposed",
                    "title": "a proposal", "line": "", "actions": ["accept"]}]},
               {"project": "__unfiled__", "title": "Unfiled", "items": [
                   {"kind": "failed", "label": "Failed", "id": "tsk_f", "state": "failed",
                    "title": "a failure", "line": "", "actions": ["dismiss"]}]}]}}
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    sect = js[js.index("// --- a project's own page"):js.index("// On a phone the board is one column")]
    check("the page names no role and no state rule of its own",
          "'assistant'" not in sect and '"implementor"' not in sect and "awaiting_approval" not in sect)
    check("its only buttons are ones the board and inbox already have",
          set(re.findall(r'onclick="(\w+)\(', sect)) <= {
              "setBoardProject", "releaseForm", "projectSettings", "openCard", "event"},
          str(set(re.findall(r'onclick="(\w+)\(', sect))))
    node = shutil.which("node")
    if not node:
        print("  … node not installed — the project page checked by text only")
        return
    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    (d / "drive.js").write_text(_project_page_harness())
    (d / "fix.json").write_text(json.dumps(fix))
    p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js"), "", str(d / "fix.json")],
                       capture_output=True, text=True, timeout=90)
    if p.returncode != 0:
        check("the project page harness runs the page's script", False, p.stderr.strip()[-900:])
        return
    o = json.loads(p.stdout)
    a, m = o["alpha"], o["mondays"]

    def sec(page, key):
        x = re.search(r'<section class="pgsec" data-sec="%s">(.*?)</section>' % key, page, re.S)
        return x.group(1) if x else ""
    check("opened over the board's sheet, the board itself not drawn",
          o["alphaOpen"] == "flex" and o["alphaBoard"] == "" and "<h3>Alpha" in a, a[:300])
    check("the header: readiness ticks, the test command, the CI gate, and the way to the board",
          "✓ tests" in a and "✓ auto-merge" in a and "✗ publish" in a and "make test" in a
          and "CI gate: all" in a and "setBoardProject('alpha')\">Board →" in a)
    check("its sections, in order",
          re.findall(r'data-sec="(\w+)"', a) == ["needs", "health", "running", "queued", "unmerged",
                                                 "landed", "releases"], str(re.findall(r'data-sec="(\w+)"', a)))
    check("needs you: the inbox's own cards and buttons, most urgent first",
          re.findall(r'data-act="(\w+)"', sec(a, "needs")) == ["approve", "rework", "dismiss", "accept", "dismiss"]
          and "Cache the release plan" in sec(a, "needs"), sec(a, "needs")[:400])
    h = sec(a, "health")
    check("health: icon, status, summary, link and what is being done",
          "🔴" in h and "<b>ci</b>" in h and "main is red: test_clock" in h
          and 'href="https://github.com/o/alpha/actions/runs/1"' in h and "fix may be proposed" in h
          and "evidence · 2" in h, h[:600])
    check("queued with each one's place, running, and the stranded branch",
          "#1 in queue" in sec(a, "queued") and "#2 in queue" in sec(a, "queued")
          and "Running work" in sec(a, "running")
          and "silkworm/" + ids["stranded"] in sec(a, "unmerged") and "3 commits" in sec(a, "unmerged"))
    check("the fortnight's landing and release tag, and what is pending release",
          "abcdef12" in sec(a, "landed") and "Landed long ago" not in a and "not pushed" in sec(a, "landed")
          and "app/v1.2.0" in sec(a, "releases") and "app/v1.1.0" not in a
          and "1.2.1" in sec(a, "releases"))
    check("each task row opens its card", f"openCard('{ids['queued']}')" in sec(a, "queued")
          and f"openCard('{ids['landed']}')" in sec(a, "landed"))
    check("a code project has no store section", "App Store" not in a)
    ms = sec(m, "app")
    check("an app: its App Store and TestFlight state, with the decision",
          re.findall(r'data-sec="(\w+)"', m)[:3] == ["needs", "app", "health"]
          and "<b>app-store</b>" in ms and "<b>testflight</b>" in ms and "build 47 VALID" in ms
          and "decide: submit 1.3 for review, or hold it" in ms and "🟡" in ms and "🟢" in ms, ms[:600])
    check("and not again under health, where the rest are",
          "app-store" not in sec(m, "health") and "backend-drift" in sec(m, "health")
          and "last known ok" in sec(m, "health"))
    check("its unready and red-base lines in the header",
          "needs a test command and auto-merge" in m and "mondays main is failing" in m)
    check("empty sections say so", "nothing queued" not in a and "nothing running" in m
          and "none" in sec(m, "unmerged"))
    check("a failed read says so rather than drawing an empty page",
          "bot is offline" in o["offlineFresh"] and "<h3>alpha" in o["offlineFresh"], o["offlineFresh"][:300])
    check("‹ Projects goes back to the overview", o["afterBack"] == {"page": None, "board": None},
          str(o["afterBack"]))
    check("?view=project&slug= opens the page; ?view=board&slug= the board",
          o["link"] == {"page": "mondays", "html": True}
          and o["boardLink"] == {"page": None, "board": "alpha"}, f"{o['link']} {o['boardLink']}")
    link = "openProjectPage('alpha')"
    check("linked from the thread list and the open thread's header",
          link in o["threadCard"] and "event.stopPropagation()" in o["threadCard"] and link in o["chatHead"])
    check("from task cards and a task's detail", link in o["taskCard"] and link in o["detail"])
    check("and from the Needs-you inbox's group heading, not the unfiled one's",
          link in o["inbox"] and "openProjectPage('__unfiled__')" not in o["inbox"])
    check("the overview's card opens the page", "openProjectPage('${esc(slug)}')" in js)
    check("a section key in the address that the page has not is ignored, not a selector error",
          o["badSec"] == {"page": "alpha", "drawn": True} and "PAGE_SECTIONS.includes(pageScrollTo)" in js,
          str(o["badSec"]))
    check("an unreadable health store is said on the page", "the health store could not be read" in js)


_TASK_SECRETS = ("sk-ant-TASKPAGE-1", "ghp_TASKPAGETOKEN2", "TASKPAGE-ENV-3", "TASKPAGE-SESSION-4")


def _task_page_world():
    """A real TaskStore holding a landed implementor with two runs, its two
    reviews, a follow-up it filed, its parent, an approval with a step left
    for you and a proposal -- records carrying planted secrets -- and the
    bot's own /tasks `page` route over them, its git changes a fixed answer.
    (handle, ids, store, changes_asked)."""
    import board as B
    import tasks as T
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    ids, now = {}, time.time()
    secret_scope = {"cwd": "/tmp/alpha-secret-checkout", "env": {"TOKEN": _TASK_SECRETS[2]}}

    def make(key, title, path, project="alpha", role="implementor", state0=T.QUEUED, **kw):
        t = st.create(title + "\nWhat done looks like: it works.", title=title, state=state0,
                      role=role, project=project if role != "reviewer" else "",
                      source="dashboard", driver="queue", scope=secret_scope,
                      parent=kw.pop("parent", None), thread=kw.pop("thread", "D9:100.5"))
        for s, why in path:
            st.transition(t["id"], s, why)
        if kw:
            st.update(t["id"], **kw)
        ids[key] = t["id"]
        return t["id"]
    make("parent", "Plan the dashboard", [(T.RUNNING, ""), (T.DONE, "")])
    make("main", "Draw the task page", [(T.QUEUED, "accept via ui"), (T.RUNNING, "claimed by the runner"),
                                        (T.BLOCKED, "waiting on its review"),
                                        (T.QUEUED, "sent back via review"), (T.RUNNING, "claimed by the runner"),
                                        (T.DONE, "landed")],
         state0=T.PROPOSED, parent=None,
         session_id=_TASK_SECRETS[3], checkpoint={"session_id": _TASK_SECRETS[3], "at": now},
         branch="silkworm/tsk_main", commits=2,
         result={"text": "## Done\n\nThe page **reads well**:\n- one\n- `two`\n\n" + _TASK_SECRETS[0][:0]
                 + "```\nmake test\n```",
                 "cost": 3.0, "cost_runs": [[now + 5, 1.25], [now + 10, 1.75]],
                 "review": {"ok": True, "summary": "clean", "findings": [], "followups": []},
                 "landing": {"eligible": True, "landed": True, "stage": "done", "base": "main",
                             "before": "1111111111", "head": "2222222222abc", "at": now + 20,
                             "published": False},
                 "decided_by": "U123", "decided_via": "dashboard message 9.9"})
    st.update(ids["main"], parent=ids["parent"])
    make("rev1", "Review: Draw the task page", [(T.RUNNING, ""), (T.DONE, "")], role="reviewer",
         parent=ids["main"], result={"cost": 0.4})
    make("rev2", "Review: Draw the task page (again)", [(T.RUNNING, ""), (T.DONE, "")], role="reviewer",
         parent=ids["main"], result={"cost": 0.35})
    make("kid", "Follow-up: cache the page", [], parent=ids["main"], state0=T.PROPOSED)
    make("approval", "Ship the release", [(T.RUNNING, ""), (T.AWAITING_APPROVAL, "")],
         needs_user={"action": "!release alpha all", "why": "the release is yours"},
         result={"review": {"ok": True, "summary": "fine"}})
    make("proposal", "A proposal", [], state0=T.PROPOSED)
    make("other", "Another project's", [(T.RUNNING, ""), (T.DONE, "")], project="beta")
    asked = []
    changes = {"branch": "silkworm/tsk_main", "base": "main", "range": "11111111..22222222",
               "landed": True, "commits": [{"sha": "2222222222", "subject": "Draw the task page", "at": now},
                                           {"sha": "1a1a1a1a1a", "subject": "Its tests", "at": now}],
               "commit_count": 2, "files": [{"path": "visualizer.py", "added": 120, "removed": 4},
                                            {"path": "assets/x.png", "added": None, "removed": None}],
               "file_count": 2, "added": 120, "removed": 4, "error": "",
               "released": {"tag": "app/v1.4.0", "at": now + 30}, "deployed": True,
               "deployed_at": now + 40}

    def task_changes(rec):
        asked.append(rec.get("id"))
        return changes if rec.get("id") == ids["main"] else None
    projects = {"alpha": {"slug": "alpha", "title": "Alpha", "scope": secret_scope,
                          "brief": _TASK_SECRETS[1]}}
    handle = _bot_func(
        "handle_tasks", task_store=st, tasks=T, board=B, task_changes=task_changes,
        project_store=types.SimpleNamespace(get=lambda s: projects.get(s),
                                            scope_for=lambda s: secret_scope),
        board_survey=lambda: [], log=logging.getLogger("test"))
    return handle, ids, st, asked


def test_task_page_reads_one_task():
    print("\na task's page: state in words, the step it needs, the result, its changes, "
          "cost with reviews and reruns, a timeline and its links, read-only with no secrets")
    handle, ids, st, asked = _task_page_world()
    before = json.dumps(st.all(), sort_keys=True)
    r = handle({"action": "page", "id": ids["main"]})
    check("the route answers for a task", r.get("ok"), str(r)[:300])
    check("and refuses one it does not know",
          handle({"action": "page", "id": "tsk_nope"}) == {"ok": False, "error": "unknown task"})
    pg = r["page"]
    check("title, state and the state in plain words",
          pg["title"] == "Draw the task page" and pg["state"] == "done"
          and pg["state_words"] == "Done, and landed onto main as 22222222.", pg["state_words"])
    check("the reply text whole, for the page to draw as markdown",
          pg["text"].startswith("## Done") and "**reads well**" in pg["text"] and not pg["text_cut"])
    check("its changes are task_changes' answer for it",
          pg["changes"]["commits"][0]["subject"] == "Draw the task page" and asked == [ids["main"]])
    c = pg["cost"]
    check("cost: its two runs and its two reviews, the total with both",
          [x["usd"] for x in c["runs"]] == [1.25, 1.75]
          and [x["usd"] for x in c["reviews"]] == [0.4, 0.35]
          and c["total"] == {"usd": 3.75, "complete": True}, str(c))
    labels = [e["label"] for e in pg["timeline"]]
    check("the timeline opens with its filing, then who accepted it and how",
          labels[0] == "Filed" and "from dashboard" in pg["timeline"][0]["detail"]
          and labels[1] == "Accepted via the dashboard", str(labels[:4]))
    check("each run numbered, the reruns among them",
          labels.count("Run 1 started") == 1 and labels.count("Run 2 started") == 1, str(labels))
    check("its reviews, the landing, the release and the deploy",
          "Review started" in labels and "Review done" in labels and "Landed onto main" in labels
          and "Released in app/v1.4.0" in labels and "Running in the bot" in labels, str(labels))
    check("in time order", [e["at"] for e in pg["timeline"]] == sorted(e["at"] for e in pg["timeline"]))
    check("links: its thread, project, parent, the follow-up it filed and its reviews",
          pg["thread"] == "D9:100.5" and pg["project"] == "alpha" and pg["project_title"] == "Alpha"
          and pg["parent"]["id"] == ids["parent"] and [k["id"] for k in pg["children"]] == [ids["kid"]]
          and [k["id"] for k in pg["reviews"]] == [ids["rev1"], ids["rev2"]], str(pg["children"]))
    check("a landed task waits on nobody: no buttons", pg["item"] is None)
    a = handle({"action": "page", "id": ids["approval"]})["page"]
    check("the step a task left for you, and the inbox's own buttons for it",
          a["needs_user"] == {"action": "!release alpha all", "why": "the release is yours"}
          and a["item"]["actions"] == ["approve", "rework", "dismiss"]
          and "left one step for you" in a["state_words"], str(a["item"]))
    p = handle({"action": "page", "id": ids["proposal"]})["page"]
    check("a proposal: Accept and Dismiss, and what proposed means",
          p["item"]["actions"] == ["accept", "dismiss"] and "until you accept it" in p["state_words"])
    rv = handle({"action": "page", "id": ids["rev1"]})["page"]
    check("a review belongs to its parent's project and links back to it",
          rv["project"] == "alpha" and rv["parent"]["id"] == ids["main"])
    whole = json.dumps([r, a, p, rv])
    leaked = [s for s in _TASK_SECRETS if s in whole]
    check("no secrets: not its scope's environment, the project's brief, nor its session",
          not leaked and "/tmp/alpha-secret-checkout" not in whole and '"scope"' not in whole
          and '"checkpoint"' not in whole and '"session_id"' not in whole, str(leaked))
    check("and reading it moved nothing", json.dumps(st.all(), sort_keys=True) == before)
    import visualizer as V
    check("the dashboard treats it as a read", "page" in V.READ_TASK_ACTIONS)
    dash = (BASE / "bin" / "dashshots").read_text()
    check("and so does the screenshot stand-in", '"project", "page"}' in dash and "local_task_page" in dash)


def test_task_page_odd_records():
    import board as B
    import merge as merge_mod
    import taskchanges as C
    print("\na task's page over records the happy path never makes: landings under way, "
          "caught-up branches, damaged fields, a task that never ran")

    def page(rec, others=()):
        return B.task_page({"id": "tsk_a", "title": "a", "created": 1, **rec}, [*others])
    for stage, words in (("in-progress", "landing under way"), ("ci-wait", "waiting for CI")):
        p = page({"state": "done", "result": {"landing": {"eligible": True, "landed": False, "stage": stage,
                                                          "at": 5}}})
        check(f"a landing {stage} is said as under way, never as waiting for you",
              words in p["state_words"] and "waits for you" not in p["state_words"]
              and not any("refused" in e["label"].lower() or "Not landed" in e["label"] for e in p["timeline"]),
              f"{p['state_words']} {[e['label'] for e in p['timeline']]}")
        check(f"and the timeline says it in the same words ({stage})",
              any(e["label"].lower() == words.lower() or e["label"].startswith(words[:1].upper() + words[1:])
                  for e in p["timeline"]) and not any("ci before" in e["label"] for e in p["timeline"]),
              str([e["label"] for e in p["timeline"]]))
    held = page({"state": "done", "result": {"landing": {"eligible": True, "landed": False,
                                                         "stage": merge_mod.BASE_FAILING, "at": 5}}})
    check("a landing held for another reason is not plain Done",
          held["state_words"] == f"Done, but not landed ({merge_mod.BASE_FAILING}).", held["state_words"])
    catch = {"id": "tsk_c", "title": "Catch up: a", "state": "queued", "source": "rework",
             "source_ref": "tsk_a", "created": 9, "result": {"cost": 0.5}, "attempts": 1}
    hand = page({"state": "done", "result": {"cost": 1.0, "landing": {
        "eligible": True, "landed": False, "stage": "rebase", "reworked_by": "tsk_c", "at": 8}}}, [catch])
    check("a branch handed to a catch-up task says so, links it, and counts its cost",
          "handed to tsk_c" in hand["state_words"] and [c["id"] for c in hand["catchups"]] == ["tsk_c"]
          and hand["cost"]["total"] == {"usd": 1.5, "complete": True}
          and any(e.get("id") == "tsk_c" for e in hand["timeline"]), str(hand["cost"]))
    bad = page({"state": "failed", "events": [{"at": "5", "kind": 7, "detail": "x"}, "junk",
                                               {"at": 3, "kind": "failed", "detail": "boom"}],
                "result": {"cost": 1.0, "cost_runs": [[1, 2, 3], {"a": 1}, [4], [6, None], [7, 0.5]]}})
    check("damaged events and runs are skipped or said unknown, never a crash",
          [r["usd"] for r in bad["cost"]["runs"]] == [None, 0.5]
          and [e["at"] for e in bad["timeline"]] == sorted(e["at"] for e in bad["timeline"]), str(bad["cost"]))
    blocked = page({"state": "blocked", "blocked_on": ["tsk_pruned"]})
    check("a blocker no longer on record is not waited on by a '?'",
          "?" not in blocked["state_words"], blocked["state_words"])
    noted = B._event_line({"kind": "cancelled", "detail": "dismiss via ui (checkout holds 2 uncommitted)"})
    check("who is said apart from the note that came with it",
          noted == ("Dismissed via the dashboard", "checkout holds 2 uncommitted"), str(noted))
    fresh = C.for_task({"id": "tsk_new", "state": "proposed", "scope": {"cwd": str(BASE)}})
    check("a task that never ran is said to have changed nothing, not to have lost its branch",
          fresh and fresh.get("not_run") and not fresh["error"], str(fresh))
    import visualizer as V
    js = V.PAGE[V.PAGE.index("async function openLink"):]
    js = js[:js.index("if (card) openCard(card);")]
    check("a board deep link leaves the task page", "taskPageId = null;" in js)
    check("a landing's `before` is kept, so its landed range is exact",
          '"head", "base", "before",' in (BASE / "bot.py").read_text())


def test_taskchanges_reads_git_read_only():
    import taskchanges as C
    print("\na task's changes, read from git through the operator's vetted read-only command")
    git = shutil.which("git")
    if not git:
        print("  … git not installed")
        return
    d = Path(tempfile.mkdtemp())

    def g(*a):
        return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=True).stdout.strip()
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@example.invalid"); g("config", "user.name", "T")
    (d / "a.txt").write_text("one\n"); g("add", "."); g("commit", "-qm", "base")
    base = g("rev-parse", "HEAD")
    g("checkout", "-qb", "silkworm/tsk_x")
    (d / "a.txt").write_text("one\ntwo\nthree\n"); g("commit", "-qam", "Add two lines")
    (d / "b.txt").write_text("new\n"); g("add", "."); g("commit", "-qm", "Add b")
    head = g("rev-parse", "HEAD")
    g("checkout", "-q", "main")
    r = C.read(d, branch="silkworm/tsk_x", bases=("refs/heads/main",))
    check("the branch's commits, newest first, with subjects",
          [c["subject"] for c in r["commits"]] == ["Add b", "Add two lines"] and r["commit_count"] == 2
          and not r["error"], str(r))
    check("and the files it touches, with lines added and removed",
          {f["path"]: (f["added"], f["removed"]) for f in r["files"]} == {"a.txt": (2, 0), "b.txt": (1, 0)}
          and r["added"] == 3 and r["removed"] == 0, str(r["files"]))
    g("merge", "-q", "--ff-only", "silkworm/tsk_x"); g("branch", "-qD", "silkworm/tsk_x")
    landed = C.read(d, landing={"landed": True, "before": base, "head": head, "base": "main"})
    check("once landed, exactly the range the landing recorded",
          landed["landed"] and len(landed["commits"]) == 2 and landed["file_count"] == 2
          and landed["range"] == f"{base[:8]}..{head[:8]}", str(landed))
    kept = C.read(d, landing={"landed": True, "head": head, "base": "main"}, count=2)
    one = C.read(d, landing={"landed": True, "head": head, "base": "main"})
    check("a stored landing keeps no `before`: the task's own commit count back from the head",
          [c["subject"] for c in kept["commits"]] == ["Add b", "Add two lines"]
          and kept["range"] == f"{head[:8]}~2..{head[:8]}"
          and [c["subject"] for c in one["commits"]] == ["Add b"], f"{kept['range']} {one['commits']}")
    gone = C.read(d, branch="silkworm/tsk_x", bases=("refs/heads/main",))
    check("a branch no longer there is said, not drawn as no changes",
          "not here" in gone["error"] and gone["commits"] is None, str(gone))
    g("tag", "app/v1.0.0", head)
    check("the release tag that shipped it", (C.released_in(d, head) or {}).get("tag") == "app/v1.0.0")
    check("whether a revision has it", C.contains(d, base, head) is True and C.contains(d, head, base) is False)
    check("a ref that reads as an option is refused before git is asked",
          C.read(d, branch="--output=/tmp/x", bases=("refs/heads/main",))["error"]
          and C.contains(d, "--output=x", head) is None)
    ran = []

    def spy(argv, **kw):
        ran.append(argv)
        return types.SimpleNamespace(returncode=0, stdout="")
    C.read(d, landing={"landed": True, "before": base, "head": head})
    C.read(d, landing={"landed": True, "before": base, "head": head}, run=spy)
    check("every call is the vetted command: no textconv, no external diff, no pager",
          ran and all(a[:2] == ["git", "--no-pager"] for a in ran)
          and any("--no-textconv" in a and "--no-ext-diff" in a for a in ran if "diff" in a), str(ran[:1]))

    def broken(argv, **kw):
        return types.SimpleNamespace(returncode=128, stdout="")
    b = C.read(d, landing={"landed": True, "before": base, "head": head}, run=broken)
    check("git refusing is said, the lists left unknown",
          b["error"] == "git could not read the changes" and b["commits"] is None and b["files"] is None)

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)
    check("and so is git timing out", C.read(d, landing={"landed": True, "before": base, "head": head},
                                             run=slow)["commits"] is None)


def _task_page_harness():
    head = MOTION_HARNESS[:MOTION_HARNESS.index("(0, eval)(SRC")]
    return head + r"""
const FIX = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
let offline = false;
globalThis.confirm = () => true;
answer = (url, b) => (b && b.action === "page")
  ? (offline ? {ok: false, error: "bot is offline"} : (FIX.pages[b.id] || {ok: false, error: "unknown task"}))
  : url.startsWith("/api/artifacts") ? FIX.files
  : (b && b.action === "project") ? {ok: false, error: "no project page in this fixture"}
  : (b && ["approve", "accept", "dismiss", "rework", "land"].includes(b.action)) ? {ok: true}
  : BASE(url);
(0, eval)(SRC + `
;globalThis.__m = {openLink, openTaskPage, closeTaskPage, renderDetail, inboxAct, sendAllDeferred,
                   page: () => taskPageId, proj: () => pageSlug};`);
const M = globalThis.__m, out = {};
const flushAsync = async () => { for (let i = 0; i < 14; i++) await new Promise(r => setImmediate(r)); };
// A button as the page drew it, pressed: its data-* from the markup.
function press(html, act) {
  const m = html.match(new RegExp('<button[^>]*data-act="' + act + '"[^>]*>'));
  if (!m) return false;
  const b = {dataset: {}};
  for (const [, k, v] of m[0].matchAll(/data-(\w+)="([^"]*)"/g)) b.dataset[k] = v;
  M.inboxAct(b);
  return true;
}
(async () => {
  await M.openLink(new URLSearchParams("task=" + FIX.ids.main)); await flushAsync();
  out.main = byId("bover").innerHTML;
  out.open = byId("boardmodal").style.display;
  out.board = byId("bboard").innerHTML;
  M.openTaskPage(FIX.ids.approval); await flushAsync();
  out.approval = byId("bover").innerHTML;
  calls.length = 0;
  out.pressedApprove = press(out.approval, "approve"); await flushAsync();
  out.approveCalls = calls.filter(c => c.url === "/api/tasks" && c.body && c.body.action === "approve").map(c => c.body);
  M.openTaskPage(FIX.ids.proposal); await flushAsync();
  out.proposal = byId("bover").innerHTML;
  calls.length = 0;
  out.pressedAccept = press(out.proposal, "accept"); await flushAsync();
  out.hiddenWhilePending = byId("bover").innerHTML.includes('data-act="accept"');
  M.sendAllDeferred(); await flushAsync();
  out.acceptCalls = calls.filter(c => c.url === "/api/tasks" && c.body && c.body.action === "accept").map(c => c.body);
  offline = true;
  M.openTaskPage("tsk_gone"); await flushAsync();
  out.offline = byId("bover").innerHTML;
  offline = false;
  M.openTaskPage(FIX.ids.main); await flushAsync();
  M.closeTaskPage(); await flushAsync();
  out.back = {page: M.page(), proj: M.proj()};
  out.detail = M.renderDetail({id: "tsk_1", title: "work", project: "alpha", role: "implementor",
                               state: "queued", created: 1, events: []});
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_dashboard_task_page_renders():
    import re
    import visualizer as V
    print("\nthe task page, drawn from the route's answer: every section, markdown, images, buttons")
    handle, ids, _st, _ = _task_page_world()
    pages = {ids[k]: handle({"action": "page", "id": ids[k]}) for k in ("main", "approval", "proposal")}
    created = pages[ids["main"]]["page"]["created"]
    files = [
        {"name": "shot-375.png", "path": "/a/1-shot-375.png", "direction": "out", "ts": created + 100,
         "exists": True, "image": True},
        {"name": "yours.png", "path": "/a/2-yours.png", "direction": "in", "ts": created + 100,
         "exists": True, "image": True},
        {"name": "old.png", "path": "/a/0-old.png", "direction": "out", "ts": created - 86400,
         "exists": True, "image": True},
        {"name": "notes.txt", "path": "/a/3-notes.txt", "direction": "out", "ts": created + 100,
         "exists": True, "image": False},
        {"name": "demo.mp4", "path": "/a/4-demo.mp4", "direction": "out", "ts": created + 120,
         "exists": True, "image": False, "video": True, "size": 3 * 1048576},
        {"name": "yours.mov", "path": "/a/5-yours.mov", "direction": "in", "ts": created + 120,
         "exists": True, "image": False, "video": True}]
    fix = {"pages": pages, "files": files, "ids": ids}
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    sect = js[js.index("// --- a task's own page"):js.index("// --- releasing a project, from its card")]
    check("the page names no role and no state rule of its own",
          "'assistant'" not in sect and '"implementor"' not in sect and "awaiting_approval" not in sect)
    check("its only buttons are the inbox's and the page's own links",
          set(re.findall(r'onclick="(\w+)\(', sect)) <= {
              "closeTaskPage", "openTaskPage", "openProjectPage", "tpThread"},
          str(set(re.findall(r'onclick="(\w+)\(', sect))))
    node = shutil.which("node")
    if not node:
        print("  … node not installed — the task page checked by text only")
        return
    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    (d / "drive.js").write_text(_task_page_harness())
    (d / "fix.json").write_text(json.dumps(fix))
    p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js"), "", str(d / "fix.json")],
                       capture_output=True, text=True, timeout=90)
    if p.returncode != 0:
        check("the task page harness runs the page's script", False, p.stderr.strip()[-900:])
        return
    o = json.loads(p.stdout)
    m = o["main"]

    def sec(page, key):
        x = re.search(r'<section class="pgsec[^"]*" data-sec="%s">(.*?)</section>' % key, page, re.S)
        return x.group(1) if x else ""
    check("?task= opens it over the board's sheet, the board not drawn",
          o["open"] == "flex" and o["board"] == "" and "<h3>Draw the task page</h3>" in m, m[:300])
    check("its sections, in order",
          re.findall(r'data-sec="(\w+)"', m) == ["state", "result", "changes", "images", "videos", "cost", "timeline",
                                              "links"],
          str(re.findall(r'data-sec="(\w+)"', m)))
    check("its state, in plain words", "Done, and landed onto main as 22222222." in sec(m, "state"))
    r = sec(m, "result")
    check("the result drawn as markdown, not raw",
          "<h4>Done</h4>" in r and "<b>reads well</b>" in r and "<li><code>two</code></li>" in r
          and "<pre><code>make test" in r and "**" not in r and "## Done" not in r, r[:500])
    check("with its review and its landing", "Review passed" in r and "landed 22222222" in r)
    ch = sec(m, "changes")
    check("what changed: branch, range, commits with subjects, files with + and −",
          "silkworm/tsk_main" in ch and "11111111..22222222" in ch and "Draw the task page" in ch
          and "Its tests" in ch and "visualizer.py" in ch and "+120" in ch and "−4" in ch
          and "2 commits" in ch and "+bin" in ch, ch[:700])
    im = sec(m, "images")
    check("its own images inline, from when it ran -- not yours, not older ones, not other files",
          "1-shot-375.png" in im and "<img " in im and "2-yours.png" not in im and "0-old.png" not in im
          and "notes.txt" not in im and "demo.mp4" not in im, im[:400])
    vi = sec(m, "videos")
    check("its own videos play on the page: a player with a still, seekable source and its Download kept",
          '<video controls playsinline preload="metadata"' in vi
          and 'poster="/thumb?path=%2Fa%2F4-demo.mp4"' in vi
          and 'src="/download?inline=1&amp;path=%2Fa%2F4-demo.mp4#t=0.1"' in vi
          and 'href="/download?path=%2Fa%2F4-demo.mp4" download="demo.mp4"' in vi
          and ">Download<" in vi and "3.0 MB" in vi
          and "5-yours.mov" not in vi and "notes.txt" not in vi, vi[:600])
    co = sec(m, "cost")
    check("cost: the total with its reviews, each run and each review",
          "$3.75" in co and "run 1" in co and "run 2" in co and "$1.75" in co and "$0.40" in co
          and "$0.35" in co, co[:500])
    tl = sec(m, "timeline")
    check("the timeline: filed, accepted by whom, runs, reviews, landing, release, deploy",
          all(w in tl for w in ("Filed", "Accepted via the dashboard", "Run 1 started", "Run 2 started",
                                "Review started", "Landed onto main", "Released in app/v1.4.0",
                                "Running in the bot")), tl[:800])
    ln = sec(m, "links")
    check("links: its thread, its project's page, its parent and children",
          'data-thread="D9:100.5"' in ln and "openProjectPage('alpha')" in ln
          and f"openTaskPage('{ids['parent']}')" in ln and f"openTaskPage('{ids['kid']}')" in ln
          and f"openTaskPage('{ids['rev1']}')" in ln, ln[:500])
    check("a landed task has no buttons to press", "data-act=" not in m)
    a = o["approval"]
    check("the step it needs from you, prominently, with the inbox's buttons",
          'class="tpneed"' in sec(a, "state") and "!release alpha all" in sec(a, "state")
          and re.findall(r'data-act="(\w+)"', a) == ["approve", "rework", "dismiss"], sec(a, "state")[:400])
    check("Approve calls the approve action for this task",
          o["pressedApprove"] and o["approveCalls"] == [{"action": "approve", "id": ids["approval"]}],
          str(o["approveCalls"]))
    check("Accept calls accept once its Undo runs out, the button gone meanwhile",
          o["pressedAccept"] and not o["hiddenWhilePending"]
          and [c.get("action") for c in o["acceptCalls"]] == ["accept"]
          and o["acceptCalls"][0]["id"] == ids["proposal"], str(o["acceptCalls"]))
    whole = json.dumps(o)
    check("no secrets drawn", not [s for s in _TASK_SECRETS if s in whole]
          and "alpha-secret-checkout" not in whole)
    check("a failed read says so rather than drawing an empty page",
          "bot is offline" in o["offline"] and "tsk_gone" in o["offline"], o["offline"][:300])
    check("‹ Back goes to its project's page", o["back"] == {"page": None, "proj": "alpha"}, str(o["back"]))
    check("a card's detail links to the page", "openTaskPage('tsk_1')" in o["detail"])
    check("a section key the page has not is ignored", "TP_SECTIONS.includes(pageScrollTo)" in js)


# --- the spend view ---------------------------------------------------------------

def _spend_world():
    """Records spread over the spend view's windows, `now` a fixed local noon.
    Every figure the tests expect is worked out by hand in their comments."""
    from datetime import datetime as _dt
    now = _dt(2026, 11, 22, 12, 0).timestamp()
    W = 7 * 86400

    def ev(*pairs):
        return [{"kind": k, "at": at} for k, at in pairs]
    recs = [
        # alpha: one run exactly on this week's start (this week: start is
        # inclusive), one a second before it (last week), one five seconds
        # into week 9, and its review an hour ago.
        {"id": "a1", "title": "Alpha work", "project": "alpha", "role": "implementor", "state": "done",
         "attempts": 2, "created": now - 90 * 86400, "updated": now - 100,
         "result": {"cost": 16.0, "cost_runs": [[now - W, 10.0], [now - W - 1, 4.0], [now - 3 * W + 5, 2.0]]}},
        {"id": "r1", "title": "Review: Alpha work", "project": "", "parent": "a1", "role": "reviewer",
         "state": "done", "attempts": 1, "created": now - 4000, "updated": now - 3600,
         "result": {"cost": 1.5, "cost_runs": [[now - 3600, 1.5]]}},
        # beta: a cost from before runs were dated, so dated by its run's end
        # (week 9); and a failed run with no cost recorded at all (this week).
        {"id": "b1", "title": "Beta research", "project": "beta", "role": "researcher", "state": "needs_input",
         "attempts": 1, "created": now - 2 * W - 500, "updated": now - 2 * W - 10,
         "events": ev(("running", now - 2 * W - 400), ("needs_input", now - 2 * W - 10)),
         "result": {"cost": 3.0}},
        {"id": "u1", "title": "Beta crash", "project": "beta", "role": "implementor", "state": "failed",
         "attempts": 1, "created": now - 7200, "updated": now - 3600,
         "events": ev(("running", now - 7000), ("failed", now - 3600)), "result": {}},
        # under no project: a conversation turn and an operator sweep.
        {"id": "t1", "title": "a conversation", "project": "", "role": "assistant", "state": "done",
         "attempts": 1, "created": now - 200, "updated": now - 100,
         "result": {"cost": 0.5, "cost_runs": [[now - 100, 0.5]]}},
        {"id": "o1", "title": "Morning sweep", "project": "", "role": "operator", "state": "done",
         "attempts": 1, "created": now - 2 * 86400 - 50, "updated": now - 2 * 86400,
         "result": {"cost": 2.0, "cost_runs": [[now - 2 * 86400, 2.0]]}},
        # never ran: no cost to know, so not "unknown" either.
        {"id": "p1", "title": "A proposal", "project": "alpha", "role": "implementor", "state": "proposed",
         "attempts": 0, "created": now - 50, "updated": now - 50, "result": {}},
        # spent before the twelve weeks: in none of them.
        {"id": "a2", "title": "Ancient", "project": "alpha", "role": "implementor", "state": "done",
         "attempts": 1, "created": now - 14 * W, "updated": now - 13 * W,
         "result": {"cost": 50.0, "cost_runs": [[now - 13 * W, 50.0]]}},
    ]
    return recs, now, W


def test_spend_view_aggregates():
    print("\nthe spend view: weeks, unknown costs, the trend, the month's top and roles, by hand")
    import costs
    import spend
    recs, now, W = _spend_world()
    v = spend.view(recs, now, unfiled="__unfiled__")
    wk = v["weeks"]
    check("twelve seven-day weeks, oldest first, the last ending now",
          len(wk) == 12 and wk[-1]["end"] == now and wk[-1]["start"] == now - W
          and all(wk[i]["end"] == wk[i + 1]["start"] for i in range(11)), str([w["start"] for w in wk]))
    this = wk[-1]["projects"]
    # alpha: 10 (on the boundary) + 1.5 (its review, under its parent's project).
    check("a run on a week's first second is that week's; a review counts under its parent's project",
          this["alpha"] == {"usd": 11.5, "complete": True, "tasks": 2}, str(this.get("alpha")))
    check("the second before is last week's", wk[10]["projects"] == {"alpha": {"usd": 4.0, "complete": True, "tasks": 1}},
          str(wk[10]["projects"]))
    # Week 9: alpha's 2.0, and beta's undated 3.0 dated by when its run ended.
    check("an undated cost is dated by when its run ended, not when it was last touched",
          wk[9]["projects"] == {"alpha": {"usd": 2.0, "complete": True, "tasks": 1},
                                "beta": {"usd": 3.0, "complete": True, "tasks": 1}}, str(wk[9]["projects"]))
    check("a run with no cost is unknown, not zero: beta's week is a floor of $0, drawn as $?",
          this["beta"] == {"usd": 0.0, "complete": False, "tasks": 1} and costs.fmt(this["beta"]) == "$?",
          str(this.get("beta")))
    check("a week with a known part and an unknown one is a floor, drawn with a +",
          wk[-1]["total"] == {"usd": 14.0, "complete": False} and costs.fmt(wk[-1]["total"]) == "$14.00+",
          str(wk[-1]["total"]))
    check("work under no project is counted, as unfiled", this["__unfiled__"]["usd"] == 2.5, str(this))
    check("a proposal that never ran is neither spend nor unknown, anywhere",
          all(w["projects"].get("alpha", {}).get("tasks", 0) <= 2 for w in wk)
          and "p1" not in [t["id"] for t in v["top"]["tasks"]])
    check("spend before the twelve weeks is in none of them",
          sum(w["total"]["usd"] for w in wk) == 11.5 + 2.5 + 4.0 + 5.0 and not any(w["projects"].get("alpha", {}).get("usd") == 50.0 for w in wk))
    # The four weeks before: 0, 0, 5, 4 -> 2.25; this week 14 -> (14 - 2.25) / 2.25.
    check("this week against the four before it, by hand",
          v["this_week"]["usd"] == 14.0 and v["avg"] == {"usd": 2.25, "complete": True}
          and abs(v["trend"] - (14 - 2.25) / 2.25) < 1e-3, f"{v['avg']} {v['trend']}")
    quiet = spend.view([r for r in recs if r["id"] in ("t1",)], now)
    check("with nothing in the four weeks before, there is no trend to state (not infinity, not 0%)",
          quiet["trend"] is None and quiet["avg"]["usd"] == 0.0)
    top = v["top"]
    check("the month's dearest: a task with its review counted in, the review not listed apart",
          [(t["id"], t["cost"]["usd"]) for t in top["tasks"]] == [("a1", 17.5), ("b1", 3.0), ("o1", 2.0), ("t1", 0.5)],
          str([(t["id"], t["cost"]) for t in top["tasks"]]))
    check("spend before the first of the month is not this month's", "a2" not in [t["id"] for t in top["tasks"]])
    check("a run this month with no cost is counted, not ranked", top["unknown"] == 1, str(top["unknown"]))
    check("each row has what its page link needs", top["tasks"][0]["title"] == "Alpha work"
          and top["tasks"][0]["project"] == "alpha" and top["tasks"][0]["role"] == "implementor")
    many = [{"id": f"x{i}", "title": f"x{i}", "project": "alpha", "role": "implementor", "state": "done",
             "attempts": 1, "created": now - 100, "updated": now - 100,
             "result": {"cost": float(i + 1), "cost_runs": [[now - 100, float(i + 1)]]}} for i in range(14)]
    t10 = spend.top_tasks(many, spend.month_start(now))["tasks"]
    check("at most ten, the dearest first", len(t10) == 10 and t10[0]["id"] == "x13" and t10[-1]["id"] == "x4")
    wk_roles = {r["role"]: r for r in v["roles"]["week"]}
    check("this week by role: the named roles in their order, unknown kept as a floor",
          [r["role"] for r in v["roles"]["week"]] == ["implementor", "reviewer", "operator", "assistant"]
          and wk_roles["implementor"] == {"role": "implementor", "usd": 10.0, "complete": False, "tasks": 2}
          and wk_roles["reviewer"]["usd"] == 1.5 and wk_roles["operator"]["usd"] == 2.0
          and wk_roles["assistant"]["usd"] == 0.5, str(v["roles"]["week"]))
    all_roles = v["roles"]["all"]
    check("over the twelve weeks every role, and roles add up to projects",
          [r["role"] for r in all_roles] == ["implementor", "reviewer", "researcher", "operator", "assistant"]
          and round(sum(r["usd"] for r in all_roles), 4) == round(sum(w["total"]["usd"] for w in wk), 4),
          str(all_roles))
    odd = spend.roles([{"id": "i1", "project": "z", "role": "ideator", "state": "done", "attempts": 1,
                        "created": now - 10, "updated": now - 10,
                        "result": {"cost": 1.0, "cost_runs": [[now - 10, 1.0]]}}], now - 100)
    check("a role outside the five still shows, after them", [r["role"] for r in odd] == ["ideator"], str(odd))
    check("the figures say they are list price", v["note"] == costs.NOTE and "list price" in v["note"])


def test_spend_view_agrees_with_the_digest():
    print("\nthe spend view's numbers are the digest's and the week line's for the same window")
    import costs
    import digest
    import spend
    recs, now, W = _spend_world()
    v = spend.view(recs, now, unfiled=digest.UNFILED)
    this = v["weeks"][-1]["projects"]
    check("this week is the panel's week line exactly", this == costs.by_project(recs, now, unfiled=digest.UNFILED),
          str(this))
    real = digest.WINDOW_S
    try:
        digest.WINDOW_S = W          # the digest asked about the same seven days
        secs = digest.sections(recs, now)
    finally:
        digest.WINDOW_S = real
    got = {p: s["cost"][0] for p, s in secs.items() if "cost" in s}
    check("and the digest, given the same seven days, prints the same figure per project",
          got == {p: costs.fmt(f) for p, f in this.items()}, f"{got} vs {this}")
    day = costs.by_project_between(recs, now - digest.WINDOW_S, unfiled=digest.UNFILED)
    secs = digest.sections(recs, now)
    check("and its own day too: the shared windowing gives the digest's 24h",
          {p: s["cost"][0] for p, s in secs.items() if "cost" in s} == {p: costs.fmt(f) for p, f in day.items()},
          str(day))
    whole = costs.by_project(recs, now, days=12 * 7, unfiled=digest.UNFILED)
    summed: dict = {}
    for w in v["weeks"]:
        for p, f in w["projects"].items():
            summed[p] = round(summed.get(p, 0.0) + f["usd"], 4)
    check("the twelve weeks add up to the twelve-week window: nothing counted twice or dropped",
          summed == {p: f["usd"] for p, f in whole.items()}, f"{summed} vs {whole}")
    probe = {"id": "q", "project": "p", "state": "done", "attempts": 1, "created": now - 10, "updated": now - 10,
             "result": {"cost": 2.0, "cost_runs": [[now - 10, 1.0], [now - W - 10, 1.0]]}}
    ahead = recs + [{"id": "f1", "title": "a clock ahead", "project": "alpha", "role": "assistant", "state": "done",
                     "attempts": 1, "created": now - 5, "updated": now + 5,
                     "result": {"cost": 0.25, "cost_runs": [[now + 5, 0.25]]}}]
    va = spend.view(ahead, now, unfiled=digest.UNFILED)
    check("a run stamped a moment after now is still this week's, as the week line has it",
          va["weeks"][-1]["projects"] == costs.by_project(ahead, now, unfiled=digest.UNFILED)
          and va["weeks"][-1]["projects"]["alpha"]["usd"] == 11.75, str(va["weeks"][-1]["projects"]))
    edge = {"id": "e", "project": "p", "state": "done", "attempts": 1, "created": now - W - 50,
            "updated": now - W, "result": {"cost": 5.0}}
    check("an undated cost ending on a week's last instant is the next week's, never both",
          costs.spent_between(edge, now - 2 * W, now - W) == 0.0 and costs.spent_between(edge, now - W, now) == 5.0)
    bad = {"id": "m", "project": "p", "state": "done", "attempts": 1, "created": now - 10, "updated": now - 10,
           "result": {"cost": 9.0, "cost_runs": [[now - 10, 1.0], [now - 10, 2.0, "x"], ["soon", 3.0], "junk",
                                                  [now - 10, "4"], [None, 0.5]]}}
    check("a malformed run entry is skipped, not a failure of every total it is in",
          costs.spent_between(bad, now - W) == 1.5 and spend.view([bad], now)["this_week"]["usd"] == 1.5)
    chain = [{"id": "P", "title": 7, "project": "p", "role": "implementor", "state": "done", "attempts": 1,
              "created": now - 99, "updated": now - 9, "result": {"cost": 1.0, "cost_runs": [[now - 9, 1.0]]}},
             {"id": "R1", "project": "", "parent": "P", "role": "reviewer", "state": "done", "attempts": 1,
              "created": now - 99, "updated": now - 9, "result": {"cost": 2.0, "cost_runs": [[now - 9, 2.0]]}},
             {"id": "R2", "project": "", "parent": "R1", "role": "reviewer", "state": "done", "attempts": 1,
              "created": now - 99, "updated": now - 9, "result": {"cost": 5.0, "cost_runs": [[now - 9, 5.0]]}}]
    tops = spend.top_tasks(chain, now - W)["tasks"]
    check("a review of a review counts in the task at the top of the chain, and a title is text",
          [(t["id"], t["cost"]["usd"], t["title"]) for t in tops] == [("P", 8.0, "7")], str(tops))
    check("spent_since is spent_between with no end", costs.spent_since(probe, now - W) == 1.0
          and costs.spent_between(probe, now - 2 * W, now - W) == 1.0
          and costs.spent_between(probe, now - 2 * W) == 2.0)


def test_spend_view_folds_projects_past_the_palette():
    print("\nthe spend view's colours: five projects of their own, the rest folded, slots by name")
    import spend
    rows = [{f"p{i}": {"usd": float(10 - i), "complete": True, "tasks": 1} for i in range(8)}]
    s = spend.series(rows)
    check("the five biggest get a slot, alphabetically, the rest are 'other'",
          [(x["key"], x["slot"]) for x in s] == [("p0", 1), ("p1", 2), ("p2", 3), ("p3", 4), ("p4", 5), ("other", 0)]
          and s[-1]["of"] == ["p5", "p6", "p7"], str(s))
    flipped = spend.series([{f"p{i}": {"usd": float(i + 1), "complete": True, "tasks": 1} for i in range(3)}])
    check("a colour follows the project, not its rank", [(x["key"], x["slot"]) for x in flipped]
          == [("p0", 1), ("p1", 2), ("p2", 3)], str(flipped))
    six = spend.series([{f"p{i}": {"usd": float(10 - i), "complete": True, "tasks": 1} for i in range(6)}])
    check("a sixth project folds into 'other': there are five series colours, never a slot without one",
          [x["slot"] for x in six] == [1, 2, 3, 4, 5, 0] and six[-1]["of"] == ["p5"], str(six))
    five = spend.series([{f"p{i}": {"usd": 1.0, "complete": True, "tasks": 1} for i in range(5)}])
    check("five fit without an 'other'", [x["slot"] for x in five] == [1, 2, 3, 4, 5], str(five))
    viz = (BASE / "visualizer.py").read_text()
    for theme in ("--series-1: #3987E5", "--series-1: #2A78D6"):
        check(f"the series colours are design tokens ({theme.split()[1]})", theme in viz)
    check("each theme block defines all six", viz.count("--series-0:") == 3 and viz.count("--series-5:") == 3)


def test_spend_route_reads_only():
    print("\nthe bot's /tasks spend action: the view over the real store, nothing moved")
    import board as B
    import spend
    import tasks as T
    st = T.TaskStore(Path(tempfile.mkdtemp()) / "t.json")
    t = st.create("Some work\nWhat done looks like: it works.", title="Some work", state=T.QUEUED,
                  role="implementor", project="alpha", source="ideation", driver="queue")
    st.transition(t["id"], T.RUNNING, "")
    st.update(t["id"], result={"cost": 3.25, "cost_runs": [[time.time() - 60, 3.25]]})
    before = json.dumps(st.all(), sort_keys=True)
    handle = _bot_func("handle_tasks", task_store=st, tasks=T, board=B, spend=spend,
                       log=logging.getLogger("test"))
    r = handle({"action": "spend"})
    check("it answers with the view", r.get("ok") and r["spend"]["this_week"] == {"usd": 3.25, "complete": True},
          str(r)[:300])
    check("its top task is the one that spent", [x["id"] for x in r["spend"]["top"]["tasks"]] == [t["id"]])
    check("nothing in the store changed", json.dumps(st.all(), sort_keys=True) == before)
    import visualizer as V
    check("the dashboard treats it as a read", "spend" in V.READ_TASK_ACTIONS)
    shots = (BASE / "bin" / "dashshots").read_text()
    check("and so does the screenshot harness, which shoots it", '"spend", "inbox", "project", "page"}' in shots and "?view=spend" in shots)


def _spend_harness():
    head = MOTION_HARNESS[:MOTION_HARNESS.index("(0, eval)(SRC")]
    return head + r"""
const FIX = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
let offline = false, asked = [];
answer = (url, b) => {
  if (b && b.action) asked.push(b.action);
  return (b && b.action === "spend") ? (offline ? {ok: false, error: "bot is offline"} : FIX.spend)
    : (b && b.action === "page") ? {ok: false, error: "the task page for " + b.id}
    : (b && b.action === "project") ? {ok: false, error: "the project page for " + b.project}
    : BASE(url);
};
(0, eval)(SRC + `
;globalThis.__m = {openSpend, openLink, setBoardProject, openTaskPage, renderOverview,
                   spend: () => spendOpen, task: () => taskPageId, page: () => pageSlug};`);
const M = globalThis.__m, out = {};
const flushAsync = async () => { for (let i = 0; i < 12; i++) await new Promise(r => setImmediate(r)); };
(async () => {
  M.openSpend(); await flushAsync();
  out.html = byId("bover").innerHTML;
  out.open = byId("boardmodal").style.display;
  out.boardDrawn = byId("bboard").innerHTML;
  out.overview = M.renderOverview({projects: [], archived: []});
  M.openTaskPage("a1"); await flushAsync();
  out.toTask = {spend: M.spend(), task: M.task(), html: byId("bover").innerHTML.includes("the task page for a1")};
  M.setBoardProject(null);
  out.back = M.spend();
  await M.openLink(new URLSearchParams("view=spend&sec=top")); await flushAsync();
  out.link = {spend: M.spend(), drawn: byId("bover").innerHTML.includes("Most expensive")};
  await M.openLink(new URLSearchParams("view=project&slug=alpha")); await flushAsync();
  out.leaves = {spend: M.spend(), page: M.page()};
  M.openSpend(); await flushAsync();
  await M.openLink(new URLSearchParams("view=board&slug=alpha")); await flushAsync();
  out.toBoard = M.spend();
  offline = true;
  M.openSpend(); await flushAsync();
  out.offlineKept = byId("bover").innerHTML.includes("Most expensive");
  out.offlineSaid = byId("bover").innerHTML.includes("bot is offline");
  out.asked = asked;
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_dashboard_spend_view_renders():
    import re
    import digest
    import spend
    import visualizer as V
    print("\nthe spend view, drawn from the route's answer: tiles, stacked weeks, top ten, roles")
    recs, now, W = _spend_world()
    view = spend.view(recs, now, unfiled="__unfiled__")
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    sect = js[js.index("// --- what was spent"):js.index("// --- creating and editing tasks")]
    check("read-only: its only buttons go somewhere else",
          set(re.findall(r'onclick="(\w+)\(', sect)) <= {"setBoardProject", "openTaskPage", "event"},
          str(set(re.findall(r'onclick="(\w+)\(', sect))))
    check("its colours are the design system's series tokens, no hex of its own",
          not re.search(r"#[0-9a-fA-F]{6}", sect))
    check("it names no role of its own", "implementor" not in sect and "'assistant'" not in sect)
    node = shutil.which("node")
    if not node:
        print("  … node not installed — the spend view checked by text only")
        return
    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    (d / "drive.js").write_text(_spend_harness())
    (d / "fix.json").write_text(json.dumps({"spend": {"ok": True, "spend": view}}))
    p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js"), "", str(d / "fix.json")],
                       capture_output=True, text=True, timeout=90)
    if p.returncode != 0:
        check("the spend harness runs the page's script", False, p.stderr.strip()[-900:])
        return
    o = json.loads(p.stdout)
    h = o["html"]

    def sec(key):
        x = re.search(r'<section class="pgsec" data-sec="%s">(.*?)</section>' % key, h, re.S)
        return x.group(1) if x else ""
    check("opened over the board's sheet, the board itself not drawn",
          o["open"] == "flex" and o["boardDrawn"] == "" and "<h3>Spend</h3>" in h, h[:300])
    check("it says the figures are list price, and what + and $? mean",
          "API list price, not billed" in h and "subscription" in h and "floor" in h)
    check("this week's total as a floor, the average, and the trend in words with an arrow",
          "$14.00+" in h and "$2.25" in h and "▲ 522% above the 4-week average" in h, h[:1500])
    check("its sections, in order", re.findall(r'data-sec="(\w+)"', h) == ["weeks", "top", "roles"])
    w = sec("weeks")
    svg = re.search(r"<svg class=\"spchart\".*?</svg>", w, re.S)
    check("a chart", bool(svg))
    svg = svg.group(0) if svg else ""
    bars = re.findall(r'<rect class="seg s(\d)" x="([\d.]+)" y="([\d.]+)" width="[\d.]+" height="([\d.]+)"', svg)
    # Segments: week 9 alpha+beta, week 10 alpha, this week alpha+beta(0, not drawn)+unfiled.
    check("one segment per project per week that spent, none for $0", len(bars) == 5, str(bars))
    slots = {x["key"]: x["slot"] for x in view["series"]}
    check("each project wears its own slot's class, the same in every week",
          {b[0] for b in bars} == {str(slots["alpha"]), str(slots["beta"]), str(slots["__unfiled__"])}, str(bars))
    tall = {round(float(b[3])) for b in bars}
    check("heights scale with the figure (alpha's 11.5 is the tallest segment)",
          max(float(b[3]) for b in bars) == max(float(b[3]) for b in bars if b[0] == str(slots["alpha"])), str(tall))
    check("a hover target per week, naming the week and each project's figure",
          svg.count('class="hit"') == 12 and "alpha $11.50" in svg and "beta $?" in svg)
    check("this week's total labelled on its bar, as the floor it is", 'class="now"' in svg and ">$14.00+</text>" in svg)
    check("a legend with every series and its twelve-week total, projects linked to their pages",
          "spsw s" in w and "openProjectPage('alpha')" in w and "$17.50" in w and ">unfiled<" in w, w[:800])
    check("and the table view, every week a row", w.count("<tr>") == 13 and "<summary>table</summary>" in w)
    t = sec("top")
    check("the month's dearest, each opening its task page", "openTaskPage('a1')" in t and "Alpha work" in t
          and t.index("a1") < t.index("b1") and "$17.50" in t
          and "1 more ran this month with no cost recorded" in t, t[:600])
    r = sec("roles")
    check("roles: this week and twelve weeks, unknown as a floor",
          "<td>implementor</td><td class=\"r\">$10.00+</td>" in r and "<td>researcher</td><td class=\"r\">–</td>" in r, r[:600])
    check("the overview links to it", "openSpend()" in o["overview"])
    check("a top task opens its own page, leaving the spend view",
          o["toTask"] == {"spend": False, "task": "a1", "html": True}, str(o["toTask"]))
    check("‹ Projects leaves it", o["back"] is False)
    check("?view=spend&sec= opens it", o["link"] == {"spend": True, "drawn": True}, str(o["link"]))
    check("a deep link elsewhere leaves it", o["leaves"] == {"spend": False, "page": "alpha"}, str(o["leaves"]))
    check("and so does one to a board", o["toBoard"] is False)
    check("a failed read draws the last view, not a blank, and says it could not refresh",
          o["offlineKept"] and o["offlineSaid"])
    writes = {"approve", "dismiss", "accept", "rework", "land", "drop", "edit", "priority", "create", "retry"}
    check("it only ever reads", "spend" in o["asked"] and not writes & set(o["asked"]), str(o["asked"]))

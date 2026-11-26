"""The dashboard on a laptop or a monitor (min-width: 1024px): the sidebar,
the list beside what it opened with the selection in the address, the keys,
the right-click menu, the resizable panes and the dialogs -- and none of it
reaching the phone.

Run by tests/run.py with every other tests/test_*.py; see tests/harness.py."""

from harness import *  # noqa: F401,F403


def _page():
    import re
    import visualizer as V
    css = re.search(r"<style>(.*?)</style>", V.PAGE, re.S).group(1)
    js = re.search(r"<script>(.*?)</script>", V.PAGE, re.S).group(1)
    return V, css, js


def _desk_block(css):
    """The desktop section: from its comment to the phone's."""
    return css[css.index("/* --- desktop (min-width: 1024px)"):css.index("/* --- phone (max-width: 700px)")]


def test_desktop_css_is_scoped_to_wide_screens():
    import re
    V, css, js = _page()
    print("\nthe laptop's layout is all inside min-width queries: the phone's page is untouched")
    block = _desk_block(css)
    # Outside the media queries the section only hides its own new pieces.
    outside = re.sub(r"@media \(min-width: \d+px\) \{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", block)
    rules = [r.strip() for r in re.findall(r"([^{}/]+)\{", re.sub(r"/\*.*?\*/", "", outside, flags=re.S))]
    check("outside its queries the section only hides the sidebar, grips, menu and laptop-only buttons",
          rules == ["#sidenav, .grip, #ctxmenu, .deskonly"], rules)
    check("...as display: none", "#sidenav, .grip, #ctxmenu, .deskonly { display: none; }" in block)
    queries = re.findall(r"@media \(([^)]*)\)", block)
    check("every query in it is a min-width of 1024px or more",
          queries and all(re.fullmatch(r"min-width: (\d+)px", q) and int(q.split()[1][:-2]) >= 1024
                          for q in queries), queries)
    check("it comes before the phone block, so the phone's rules still win below 700px",
          css.index("/* --- desktop (min-width: 1024px)") < css.index("@media (max-width: 700px)"))
    check("reduced motion is still the last word (after the desktop block)",
          css.rfind("@media (prefers-reduced-motion: reduce)") > css.index("/* --- desktop (min-width: 1024px)"))
    wide = block[block.index("@media (min-width: 1024px)"):]
    for rule in ("body { padding-left: var(--side-w); }",
                 "#sidenav { display: flex;",
                 "#taskmodal, #boardmodal, #learnmodal, #tradermodal { left: var(--side-w); top: var(--head-h);",
                 "body.tsplit #taskmodal { right: auto; width: var(--tlist-w);",
                 "body.tsplit #boardmodal { left: calc(var(--side-w) + var(--tlist-w)); }",
                 ".listcol { width: var(--list-w); }",
                 "main > #content, main > #composer, main > #chathead { width: 100%; max-width: var(--read-max);",
                 "#fmodal { align-items: center;",
                 "#bdetail { top: var(--head-h);",
                 ".bcols { grid-template-columns: repeat(5, minmax(0, 1fr));",
                 ".ksel { outline: 2px solid var(--accent);",
                 "#ctxmenu.open { display: flex;"):
        check(f"desktop rule: {rule[:60]}", rule in wide)
    # Motion: the desktop's moves are the same tokens, so reduced motion stops them.
    durs = re.findall(r"(\d+)ms", wide)
    check("its motion is the duration tokens, never a written-out time", not durs, durs)
    check("hover and focus: every clickable kind gets a ring and a hover",
          ":focus-visible" in wide and ".snitem:hover" in wide and ".task:hover" in wide
          and ".icard:hover" in wide and ".grip:hover::after" in wide)


def test_desktop_markup():
    import re
    V, css, js = _page()
    print("\nthe sidebar names every section; every icon-only button says what it does")
    side = V.PAGE[V.PAGE.index('<aside id="sidenav"'):V.PAGE.index("</aside>")]
    for sec in ("inbox", "threads", "projects", "trader", "tasks", "spend", "learnings"):
        check(f"the sidebar has {sec}", f'id="sn-{sec}" onclick="deskNav(\'{sec}\')"' in side)
    check("...with counts for Needs you, Threads and Tasks",
          all(f'id="snc-{k}"' in side for k in ("inbox", "threads", "tasks")))
    check("...and Search, Settings and Shortcuts at its foot",
          'onclick="deskSearch()"' in side and 'onclick="deskSettings()"' in side and 'onclick="deskKeys()"' in side)
    check("every sidebar item has a tooltip", side.count('class="snitem"') == len(re.findall(r'class="snitem"[^>]*title="', side)) == 10)
    check("the grips are separators a keyboard can reach",
          all(re.search(rf'id="{g}" class="grip" role="separator"[^>]*tabindex="0"', V.PAGE) for g in ("listgrip", "taskgrip")))
    body = V.PAGE[V.PAGE.index("<body>"):V.PAGE.index("<script>", V.PAGE.index("<body>"))]
    icon_only = [m for m in re.findall(r"<button([^>]*)>([^<]{1,3})</button>", body)
                 if not re.search(r"[A-Za-z]{2}", m[1]) and not ("title=" in m[0] or "aria-label=" in m[0])]
    check("no icon-only button in the markup is without a title or label", not icon_only, icon_only)
    check("one <nav> in the page (the thread list's): the sidebar is not caught by its styles",
          V.PAGE.count("<nav") == 1)


DESK_DRIVER = r"""
// --- the laptop: matchMedia says min-width 1024px (or not, for the phone) ---
// A task's page is not drawn here (its own tests do): asked for, the bot is "away".
answer = (url, b) => b && b.action === "page" ? {ok: false, error: "away"} : BASE(url, b);
let DESK = process.argv[3] !== "phone";
globalThis.matchMedia = q => ({matches: q === "(min-width: 1024px)" ? DESK
  : q === "(max-width: 700px)" ? !DESK : false});
html.style.setProperty = (k, v) => { html.style[k] = v; };
html.style.removeProperty = k => { delete html.style[k]; };
localStorage.removeItem = k => { delete store[k]; };
const urls = [];
globalThis.location = {pathname: "/", search: "", href: "http://x/"};
globalThis.history = {state: null, pushState() { urls.push("push"); }, back() {},
  replaceState(s, t, u) { if (u === undefined) return; urls.push(u); const i = u.indexOf("?");
    location.search = i < 0 ? "" : u.slice(i); }};
// the markup the desktop code reads
function el(tag, id, cls, parent) {
  const e = E(tag); if (id) { e.id = id; byIdMap[id] = e; } if (cls) e.className = cls;
  (parent || body).appendChild(e); return e;
}
const listcol = el("DIV", "", "listcol"); listcol.rect = {top: 0, left: 224, width: 340, height: 800};
for (const id of ["inboxcount", "taskbadge", "botdot", "sndot", "search", "reply", "inbox", "tlist", "bover", "bboard",
                  "ctxmenu", "listgrip", "taskgrip", "sidenav"]) byId(id);
for (const k of ["inbox", "threads", "projects", "trader", "tasks", "spend", "learnings"]) {
  byId("sn-" + k).className = "snitem"; byId("snc-" + k);
}
byId("search").tagName = "INPUT"; byId("reply").tagName = "TEXTAREA";
for (const id of ["listgrip", "taskgrip"]) { const g = byId(id);
  g.removeEventListener = (k, f) => { g.listeners[k] = (g.listeners[k] || []).filter(x => x !== f); }; }
let focused = null;
// a width kept from a wider screen, and this window's
store["sw-tlist-w"] = "2000"; window.innerWidth = 1280;
let confirmSays = false; const asked = [];
globalThis.confirm = m => { asked.push(m); return confirmSays; };
byId("search").focus = () => { focused = "search"; }; byId("search").select = () => {};
byId("taskmodal").rect = {top: 56, left: 224, width: 440, height: 800};
const clicked = [];
function btn(parent, text, title) {
  const b = el("BUTTON", "", "ghost", parent); b.textContent = text;
  if (title) b.setAttribute("title", title);
  b.click = () => clicked.push(text); return b;
}
function taskRow(id, title) {
  const r = el("DIV", "", "task", byId("tlist")); r.dataset.task = id;
  const t = el("SPAN", "", "tt", r); t.textContent = title;
  btn(r, "Open ›"); btn(r, "Accept"); btn(r, "Dismiss"); btn(r, "Thread");
  return r;
}
function threadCard(key) {
  const c = el("DIV", "", "card", byId("list")); c.dataset.key = key;
  c.click = () => clicked.push("open " + key);
  const x = el("BUTTON", "", "hidebtn", c); x.textContent = "×"; x.setAttribute("title", "hide");
  x.click = () => clicked.push("hide " + key);
  return c;
}
(0, eval)(SRC + `
;globalThis.__d = {deskSync, deskNav, deskQuery, deskSection, kMove, kCurrent, kList, openTaskPage, toggleTasks,
  closePanels, ctxRun, deskKeys, deskSettings, openLink, closeModal, boardIsOpen, pendingGone,
  get active() { return active; }, set active(v) { active = v; },
  get taskPageId() { return taskPageId; }, set boardProject(v) { boardProject = v; }};`);
const D = globalThis.__d, out = {};
out.tlistFit = {css: html.style["--tlist-w"], kept: store["sw-tlist-w"]};
const flushAsync = async () => { for (let i = 0; i < 12; i++) await new Promise(r => setImmediate(r)); };
const key = (k, target, more) => fire("keydown", Object.assign({key: k, target: target || body}, more || {}));
const shown = id => byId(id).style.display === "flex";
const on = () => ["inbox", "threads", "projects", "trader", "tasks", "spend", "learnings"]
  .filter(k => byId("sn-" + k).classList.contains("on"));

(async () => {
  // --- the sidebar ---
  byId("inboxcount").textContent = " 12"; byId("taskbadge").textContent = "8"; byId("botdot").className = "on";
  D.deskSync();
  out.home = {on: on(), inbox: byId("snc-inbox").textContent, tasks: byId("snc-tasks").textContent,
              dot: byId("sndot").className, url: location.search};
  D.deskNav("tasks"); await flushAsync();
  out.tasks = {on: on(), open: shown("taskmodal"), url: location.search};
  D.deskNav("trader"); await flushAsync();
  out.trader = {on: on(), open: shown("tradermodal"), tasksClosed: !shown("taskmodal"), url: location.search};
  D.deskNav("projects"); await flushAsync();
  out.projects = {on: on(), open: shown("boardmodal"), traderClosed: !shown("tradermodal"), url: location.search};
  D.deskNav("inbox"); await flushAsync();
  out.inbox = {on: on(), anyOpen: ["taskmodal", "boardmodal", "tradermodal", "learnmodal"].some(shown), url: location.search};

  // --- the split: a task opened from Tasks sits beside the list ---
  D.deskNav("tasks"); await flushAsync();
  const r1 = taskRow("tsk_one", "First"), r2 = taskRow("tsk_two", "Second");
  D.openTaskPage("tsk_two"); await flushAsync(); D.deskSync();
  out.split = {tasksOpen: shown("taskmodal"), board: shown("boardmodal"), tsplit: body.classList.contains("tsplit"),
               on: on(), url: location.search, marked: r2.classList.contains("open-page") && !r1.classList.contains("open-page")};
  // Esc closes the page beside the list, not the list
  key("Escape");
  out.splitEsc = {tasksOpen: shown("taskmodal"), board: shown("boardmodal"), tsplit: body.classList.contains("tsplit"),
                  url: location.search};
  // the address names the split, and opening that address makes it again
  D.closePanels(); D.deskSync();
  out.closedUrl = location.search;
  await D.openLink(new URLSearchParams("task=tsk_two&view=tasks")); await flushAsync(); D.deskSync();
  out.reopened = {tsplit: body.classList.contains("tsplit"), url: location.search, page: D.taskPageId};
  D.closePanels(); D.deskSync();
  // a thread, a project's board
  D.active = {key: "D1:1.0"}; D.deskSync();
  out.threadUrl = location.search; out.threadOn = on();
  D.active = null; D.boardProject = "alpha"; byId("boardmodal").style.display = "flex"; D.deskSync();
  out.boardUrl = location.search;
  D.closePanels(); D.deskSync();

  // --- the panes: dragged on the grip, kept, reset by a double-click ---
  const g = byId("listgrip");
  const call = (k, ev) => (g.listeners[k] || []).forEach(f => f(Object.assign({preventDefault() {}, button: 0, pointerId: 1}, ev)));
  call("pointerdown", {clientX: 564});
  out.dragging = body.classList.contains("resizing");
  call("pointermove", {clientX: 724});
  call("pointerup", {});
  out.listW = {css: html.style["--list-w"], kept: localStorage.getItem("sw-list-w"), done: !body.classList.contains("resizing")};
  call("pointerdown", {clientX: 0}); call("pointermove", {clientX: 230}); call("pointerup", {});
  out.listMin = html.style["--list-w"];
  call("keydown", {key: "ArrowRight"});
  out.listKey = html.style["--list-w"];
  call("pointerdown", {clientX: 564}); call("lostpointercapture", {});
  out.lostCapture = !body.classList.contains("resizing");
  call("dblclick", {});
  out.listReset = {css: html.style["--list-w"] === undefined, kept: localStorage.getItem("sw-list-w")};

  // --- keys ---
  D.active = {key: "D1:1.0"};
  const c1 = threadCard("D1:1.0"), c2 = threadCard("D2:1.0");
  c1.classList.add("active");
  const jd = key("j");
  out.j = {sel: c2.classList.contains("ksel"), prevented: jd.prevented};
  key("k"); out.k = c1.classList.contains("ksel");
  key("Enter"); out.enter = clicked.slice(-1)[0];
  // typing in the composer is typing: nothing moves, nothing is swallowed
  const nClicks = clicked.length;
  const typed = ["j", "k", "e", "d", "/", "?"].map(k => key(k, byId("reply")));
  key("Enter", byId("reply"));          // the composer's own: it sends (not this list's "open")
  out.typing = {sel: c1.classList.contains("ksel"), prevented: typed.some(e => e.prevented), modal: shown("fmodal"),
                focused, clicks: clicked.length - nClicks};
  const inSearch = key("j", byId("search"));
  out.inSearch = {prevented: inSearch.prevented, sel: c1.classList.contains("ksel")};
  key("k", body, {metaKey: true});
  out.metaK = c1.classList.contains("ksel") && focused === "search";     // ⌘K is search's, not "previous"
  focused = null;
  key("/"); out.slash = focused;
  key("?"); out.help = {open: shown("fmodal"), text: /Keyboard shortcuts/.test(byId("fbox").innerHTML)};
  // with a dialog up, j does not walk the list behind it
  key("j"); out.underModal = c1.classList.contains("ksel");
  key("Escape"); out.helpClosed = !shown("fmodal") || byId("fmodal").style.display === "none";
  D.closeModal();
  // a selection in a list that is no longer in front is let go (its row is not left lit)
  D.active = {key: "D1:1.0"}; key("j");
  const litBefore = c2.classList.contains("ksel") || c1.classList.contains("ksel");
  D.deskNav("tasks"); await flushAsync(); D.deskSync();
  out.leftBehind = {litBefore, lit: c1.classList.contains("ksel") || c2.classList.contains("ksel")};
  D.closePanels(); D.deskSync();
  // e and d: an accept or dismiss waits behind Undo, an approval asks first
  D.active = null; D.deskNav("tasks"); await flushAsync();
  for (const c of [...byId("tlist").children]) byId("tlist").removeChild(c);
  const ea = taskRow("tsk_ea", "Accept me"), eb = taskRow("tsk_eb", "Dismiss me");
  const ec = el("DIV", "", "task", byId("tlist")); ec.dataset.task = "tsk_ec";
  el("SPAN", "", "tt", ec).textContent = "Land me"; btn(ec, "Approve"); btn(ec, "Send back…");
  const before = clicked.length, sent = () => calls.filter(c => c.body && c.body.action === "approve").length;
  key("j"); key("e");
  key("j"); key("d");
  out.ed = {clicked: clicked.slice(before), a: (D.pendingGone.get("tsk_ea") || {}).action,
            b: (D.pendingGone.get("tsk_eb") || {}).action, undo: toastEl.classList.contains("undoable")};
  key("j"); confirmSays = false; key("e"); await flushAsync();
  out.approveNo = {asked: asked.length, sent: sent(), clicked: clicked.slice(before)};
  confirmSays = true; key("e"); await flushAsync();
  out.approveYes = {asked: asked.length, sent: sent(), says: /lands it/.test(asked[0] || "")};
  // a selection is its list's: the same task on the board is not picked
  const card = el("DIV", "", "bcard", byId("bboard")); card.dataset.id = "tsk_ec";
  D.closePanels(); D.boardProject = "alpha"; byId("boardmodal").style.display = "flex"; D.deskSync();
  out.boardSel = card.classList.contains("ksel");
  // Projects is the overview even with a board filter left set
  byId("bq").value = "left over";
  D.deskNav("projects"); await flushAsync(); D.deskSync();
  out.projectsFiltered = {url: location.search, bq: byId("bq").value};
  D.closePanels(); D.deskSync();

  // --- right-click: the row's own buttons ---
  const menu = byId("ctxmenu");
  D.deskNav("tasks"); await flushAsync();
  const cr = taskRow("tsk_ctx", "Right-clicked");
  const ev = fire("contextmenu", {target: cr.children[0], clientX: 300, clientY: 200});
  out.ctx = {prevented: ev.prevented, open: menu.classList.contains("open"),
             items: (menu.innerHTML.match(/role="menuitem"[^>]*>([^<]*)</g) || []).map(s => s.replace(/.*>/, "").replace(/<$/, ""))};
  const n = clicked.length; D.ctxRun(1);
  out.ctxRun = {clicked: clicked.slice(n), closed: !menu.classList.contains("open")};
  fire("contextmenu", {target: c2, clientX: 10, clientY: 10});
  out.ctxThread = (menu.innerHTML.match(/role="menuitem"[^>]*>([^<]*)</g) || []).map(s => s.replace(/.*>/, "").replace(/<$/, ""));
  key("Escape"); out.ctxEsc = !menu.classList.contains("open");

  // --- the dialogs ---
  D.deskSettings();
  out.settings = {open: shown("fmodal"), html: byId("fbox").innerHTML};
  D.closeModal();
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""

PHONE_DRIVER = r"""
// A task's page is not drawn here (its own tests do): asked for, the bot is "away".
answer = (url, b) => b && b.action === "page" ? {ok: false, error: "away"} : BASE(url, b);
let DESK = false;
globalThis.matchMedia = q => ({matches: q === "(max-width: 700px)"});
html.style.setProperty = (k, v) => { html.style[k] = v; };
const urls = [];
globalThis.location = {pathname: "/", search: "", href: "http://x/"};
globalThis.history = {state: null, pushState() {}, back() {}, replaceState(s, t, u) { if (u !== undefined) urls.push(u); }};
for (const id of ["inboxcount", "taskbadge", "botdot", "sndot", "search", "reply", "tlist", "ctxmenu", "list"]) byId(id);
const r = E("DIV"); r.className = "task"; r.dataset.task = "tsk_two"; byId("tlist").appendChild(r);
(0, eval)(SRC + `
;globalThis.__d = {deskSync, deskNav, openTaskPage, toggleTasks, get taskPageId() { return taskPageId; }};`);
const D = globalThis.__d, out = {};
(async () => {
  for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r));
  D.toggleTasks();
  D.openTaskPage("tsk_two");
  for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r));
  D.deskSync();
  out.tasksClosed = byId("taskmodal").style.display !== "flex";
  out.board = byId("boardmodal").style.display === "flex";
  out.tsplit = body.classList.contains("tsplit");
  out.urls = urls;
  const j = fire("keydown", {key: "j", target: body});
  const c = fire("contextmenu", {target: r, clientX: 1, clientY: 1});
  out.keysIgnored = !j.prevented && !c.prevented && !byId("ctxmenu").classList.contains("open");
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def _drive(driver, mode=""):
    import json
    import shutil
    import subprocess
    import tempfile
    from harness import MOTION_HARNESS
    node = shutil.which("node")
    if not node:
        return None
    V, css, js = _page()
    prelude = MOTION_HARNESS[:MOTION_HARNESS.index("(0, eval)(SRC")]
    d = Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js)
    (d / "drive.js").write_text(prelude + driver)
    p = subprocess.run([node, str(d / "drive.js"), str(d / "dash.js"), mode],
                       capture_output=True, text=True, timeout=90)
    if p.returncode != 0:
        return {"error": p.stderr.strip()[-1500:]}
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_desktop_sidebar_split_and_url():
    print("\nthe laptop: a sidebar of sections, Tasks beside a task's page, the selection in the address")
    o = _drive(DESK_DRIVER)
    if o is None:
        print("  … node not installed — cannot run the page's own javascript")
        return
    check("the page's script ran under the desktop harness", "error" not in o, o.get("error", ""))
    if "error" in o:
        return
    check("home: Needs you is the section, with its count; Tasks carries the badge's",
          o["home"]["on"] == ["inbox"] and o["home"]["inbox"] == "12" and o["home"]["tasks"] == "8", o["home"])
    check("the sidebar's dot is the bot's", o["home"]["dot"] == "on", o["home"])
    check("Tasks opens the task panel and marks Tasks; the address says so",
          o["tasks"] == {"on": ["tasks"], "open": True, "url": "?view=tasks"}, o["tasks"])
    check("Trader opens alone: Tasks is shut on the way", o["trader"]["open"] and o["trader"]["tasksClosed"]
          and o["trader"]["on"] == ["trader"] and o["trader"]["url"] == "?view=trader", o["trader"])
    check("Projects opens the overview", o["projects"]["open"] and o["projects"]["traderClosed"]
          and o["projects"]["on"] == ["projects"] and o["projects"]["url"] == "?view=projects", o["projects"])
    check("Needs you shuts every panel; the address is bare",
          o["inbox"] == {"on": ["inbox"], "anyOpen": False, "url": ""}, o["inbox"])
    s = o["split"]
    check("a task opened from Tasks: the list stays open, the page beside it (body.tsplit)",
          s["tasksOpen"] and s["board"] and s["tsplit"], s)
    check("...Tasks stays the section, the open row is marked, the address holds both",
          s["on"] == ["tasks"] and s["marked"] and s["url"] == "?task=tsk_two&view=tasks", s)
    e = o["splitEsc"]
    check("Esc closes the page, leaving the list", e["tasksOpen"] and not e["board"] and not e["tsplit"]
          and e["url"] == "?view=tasks", e)
    check("shut, the address is bare again", o["closedUrl"] == "", o["closedUrl"])
    check("opening that address makes the same split (a reload, a bookmark)",
          o["reopened"] == {"tsplit": True, "url": "?task=tsk_two&view=tasks", "page": "tsk_two"}, o["reopened"])
    check("an open thread is in the address, and Threads is the section",
          o["threadUrl"] == "?thread=D1%3A1.0" and o["threadOn"] == ["threads"], o["threadUrl"])
    check("a project's board is in the address", o["boardUrl"] == "?view=board&slug=alpha", o["boardUrl"])


def test_desktop_resizable_panes():
    print("\nthe thread list's width is dragged on its grip, kept, and reset by a double-click")
    o = _drive(DESK_DRIVER)
    if o is None or "error" in o:
        check("the page's script ran under the desktop harness", o is None, (o or {}).get("error", ""))
        return
    check("a drag marks the page as resizing while it lasts", o["dragging"] and o["listW"]["done"], o)
    check("the list is as wide as the pointer says, and kept",
          o["listW"]["css"] == "500px" and o["listW"]["kept"] == "500", o["listW"])
    check("...never narrower than its minimum", o["listMin"] == "260px", o["listMin"])
    check("→ on the focused grip widens it a step", o["listKey"] == "364px", o["listKey"])
    check("a lost pointer capture ends the drag", o["lostCapture"] is True)
    check("a width kept from a wider screen is held to this window, and kept as it was",
          o["tlistFit"] == {"css": "460px", "kept": "2000"}, o["tlistFit"])
    check("a double-click goes back to the default and forgets the width",
          o["listReset"] == {"css": True, "kept": None}, o["listReset"])


def test_desktop_keyboard_shortcuts():
    print("\nkeys on a laptop: j/k, Enter, e/d, /, ?, Esc -- and none while typing")
    o = _drive(DESK_DRIVER)
    if o is None or "error" in o:
        check("the page's script ran under the desktop harness", o is None, (o or {}).get("error", ""))
        return
    check("j moves to the next thread (from the open one)", o["j"] == {"sel": True, "prevented": True}, o["j"])
    check("k moves back", o["k"] is True)
    check("Enter opens the selected thread (its own click)", o["enter"] == "open D1:1.0", o["enter"])
    t = o["typing"]
    check("typing in the composer: j/k/e/d/?// do nothing and are not swallowed; Enter opens nothing",
          t["sel"] and not t["prevented"] and not t["modal"] and t["focused"] is None and t["clicks"] == 0, t)
    check("...and in the search field", o["inSearch"] == {"prevented": False, "sel": True}, o["inSearch"])
    check("⌘K is left to search (k with a modifier is not 'previous')", o["metaK"] is True)
    check("/ goes to search", o["slash"] == "search", o["slash"])
    check("? opens the shortcut list", o["help"] == {"open": True, "text": True}, o["help"])
    check("under a dialog, j does not walk the list behind it", o["underModal"] is True)
    check("Esc closes the dialog", o["helpClosed"] is True)
    e = o["ed"]
    check("e and d on a task row wait behind Undo (as a swipe), pressing nothing at once",
          e == {"clicked": [], "a": "accept", "b": "dismiss", "undo": True}, e)
    check("e on an Approve asks first, and a no sends nothing",
          o["approveNo"] == {"asked": 1, "sent": 0, "clicked": []}, o["approveNo"])
    check("...a yes approves (it lands the branch)", o["approveYes"] == {"asked": 2, "sent": 1, "says": True}, o["approveYes"])
    check("a task picked in Tasks is not picked on the board", o["boardSel"] is False)
    check("a thread picked with j is let go once Tasks is in front",
          o["leftBehind"] == {"litBefore": True, "lit": False}, o["leftBehind"])
    check("the sidebar's Projects is the overview even with a board filter left set",
          o["projectsFiltered"] == {"url": "?view=projects", "bq": ""}, o["projectsFiltered"])


def test_desktop_context_menu_and_dialogs():
    print("\nright-click offers a row's own buttons; Settings and the shortcut list are dialogs")
    o = _drive(DESK_DRIVER)
    if o is None or "error" in o:
        check("the page's script ran under the desktop harness", o is None, (o or {}).get("error", ""))
        return
    c = o["ctx"]
    check("right-click on a task row opens the menu instead of the browser's",
          c["prevented"] and c["open"], c)
    check("...listing Open and the row's own buttons, once each",
          c["items"] == ["Open", "Accept", "Dismiss", "Thread"], c["items"])
    check("a menu item presses that button and the menu closes",
          o["ctxRun"] == {"clicked": ["Accept"], "closed": True}, o["ctxRun"])
    check("on a thread card it offers Open and Hide (the × the phone taps)",
          o["ctxThread"] == ["Open", "Hide"], o["ctxThread"])
    check("Esc closes the menu", o["ctxEsc"] is True)
    st = o["settings"]
    check("Settings is a dialog of the phone's ⋯ items, through their own functions",
          st["open"] and all(f in st["html"] for f in ("cycleTheme()", "enableNotifications()", "notifySettings()",
                                                        "nameAllThreads()", "deskKeys()")), st)


def test_desktop_leaves_the_phone_alone():
    print("\non a phone none of it runs: Tasks still closes for a task's page, no address, no keys")
    o = _drive(PHONE_DRIVER, "phone")
    if o is None:
        print("  … node not installed — cannot run the page's own javascript")
        return
    check("the page's script ran under the phone harness", "error" not in o, o.get("error", ""))
    if "error" in o:
        return
    check("opening a task's page closes Tasks, as before", o["tasksClosed"] and o["board"], o)
    check("no split on a phone", o["tsplit"] is False)
    check("the address is never rewritten on a phone", o["urls"] == [], o["urls"])
    check("j and right-click do nothing on a phone", o["keysIgnored"] is True)

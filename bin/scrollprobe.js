// The chat's scroll probe: injected by `bin/dashshots --scroll-probe` into a
// copy of the dashboard (never the real one), ahead of the page's own script.
// It opens threads of its own -- their transcripts are made up here, served
// by wrapping fetch() -- and puts each thing that can move the reader through
// the page's own code paths, twice: once read at the bottom, once scrolled up.
// After each it measures where the reader ended up and reports it to the
// harness (the stand-in bot, __BOT__), which photographs the simulator at
// each phase.
//
// The keyboard is the simulator's own: focusing the composer opens it there
// (no tap needed), and the page sees the real window.visualViewport shrink.
// With &kb=stub it is simulated instead -- visualViewport replaced by one the
// probe shrinks, with a "resize" event, and the space it would cover drawn
// grey -- for a device that opens no keyboard without a tap.
(function () {
  if (!/[?&]probe=/.test(location.search)) return;
  const BOT = "__BOT__";
  const RUNS = (new URLSearchParams(location.search).get("runs") || "").split(",").filter(Boolean);
  const KB = Math.round(Math.min(336, window.innerHeight * 0.42));

  // --- a visual viewport the probe controls ---
  const vvT = new EventTarget();
  const vv = {height: window.innerHeight, width: window.innerWidth, scale: 1, offsetTop: 0,
              offsetLeft: 0, pageTop: 0, pageLeft: 0,
              addEventListener: (k, f, o) => vvT.addEventListener(k, f, o),
              removeEventListener: (k, f, o) => vvT.removeEventListener(k, f, o)};
  const STUB = /[?&]kb=stub/.test(location.search);
  let stubbed = false;
  if (STUB) try { Object.defineProperty(window, "visualViewport", {value: vv, configurable: true}); stubbed = window.visualViewport === vv; }
  catch (e) { /* tried below */ }
  if (STUB && !stubbed) {
    try { Object.defineProperty(Window.prototype, "visualViewport", {get: () => vv, configurable: true});
          stubbed = window.visualViewport === vv; } catch (e) { /* reported */ }
  }

  // --- threads the probe writes ---
  const threads = {};
  const ts = (n) => new Date(Date.UTC(2026, 10, 19, 9, 0, n)).toISOString();
  let clock = 0;
  const said = (role, text) => ({role, ts: ts(clock++), blocks: [{type: "text", text}]});
  const call = (name, id, arg) => ({role: "assistant", ts: ts(clock++),
                                     blocks: [{type: "tool", name, id, arg, input: arg}]});
  const result = (id, text) => ({role: "user", ts: ts(clock++), blocks: [{type: "tool_result", for: id, text}]});
  const LOREM = "The reader's place in the thread is what this checks: nothing that is drawn, " +
    "loaded or opened may move it. A line long enough to wrap twice on a phone, so items differ in height.";
  function baseline(key, opts) {
    const msgs = [];
    for (let i = 0; i < 14; i++) {
      msgs.push(said("user", `Question ${i}: ${i % 3 ? "short" : LOREM}` + (i === 5
        ? "\n\n[The user attached 1 file(s), saved locally:\n/nonexistent/probe-early.png]" : "")));
      msgs.push(call("Bash", `c${i}a`, "ls -la"), result(`c${i}a`, "ok"));
      msgs.push(call("Read", `c${i}b`, "visualizer.py"), result(`c${i}b`, "file"));
      msgs.push(said("assistant", `Answer ${i}. ${LOREM}${i % 2 ? "\n\n" + LOREM : ""}`));
    }
    const t = {msgs, files: [], live: {running: false, turns: []}};
    if (opts && opts.running) {
      t.msgs.push(said("user", "Keep going"), call("Grep", "live1", "scroll"));
      t.live = {running: true, turns: [{ref: "1795000000.000100", state: "running"}]};
    }
    threads[key] = t;
    return t;
  }
  const realFetch = window.fetch.bind(window);
  const answer = (o) => Promise.resolve(new Response(JSON.stringify(o),
                                                     {headers: {"Content-Type": "application/json"}}));
  window.fetch = function (url, opts) {
    let u;
    try { u = new URL(String(url), location.href); } catch (e) { return realFetch(url, opts); }
    const key = u.searchParams.get("key"), t = key && threads[key];
    if (t && u.pathname === "/api/session") {
      const from = parseInt(u.searchParams.get("from") || "0", 10) || 0;
      const held = u.searchParams.get("id") || "";
      const sid = "probe-" + key;
      const moved = held !== sid;
      return answer({key, session_id: sid, reset: moved && from > 0,
                     messages: t.msgs.slice(moved ? 0 : from), cursor: t.msgs.length,
                     live: JSON.parse(JSON.stringify(t.live))});
    }
    if (t && u.pathname === "/api/artifacts") return answer(t.files);
    return realFetch(url, opts);
  };

  // --- talking to the harness ---
  const log = (o) => realFetch(BOT + "/probe/log", {method: "POST", mode: "no-cors", body: JSON.stringify(o)})
    .catch(() => {});
  let go = 0;
  async function waitGo(n) {
    for (;;) {
      try {
        const r = await realFetch(BOT + "/probe/go", {cache: "no-store"});
        go = (await r.json()).go;
      } catch (e) { /* again */ }
      if (go >= n) return;
      await sleep(150);
    }
  }
  const sleep = (ms) => new Promise((ok) => setTimeout(ok, ms));
  let step = 0;
  async function phase(name, extra) {
    step++;
    log(Object.assign({phase: name, step}, extra || {}));
    await waitGo(step);
  }

  // --- measuring ---
  const main = () => document.querySelector("main");
  function latest() {
    const live = document.getElementById("tlive"), msgs = document.getElementById("tmsgs");
    const kids = [...(live ? live.children : [])].concat([]);
    if (kids.length) return kids[kids.length - 1];
    const m = msgs ? [...msgs.children] : [];
    return m[m.length - 1] || null;
  }
  // The item at the top of what is shown, and how far below main's top it sits.
  function topItem() {
    const m = main(), top = m.getBoundingClientRect().top;
    const head = document.getElementById("chathead");
    const under = head && head.offsetHeight && getComputedStyle(head).position === "sticky"
      ? head.getBoundingClientRect().bottom : top;
    for (const el of document.querySelectorAll("#tmsgs > .citem")) {
      const r = el.getBoundingClientRect();
      if (r.bottom > under + 4) return {el, at: r.top - top};
    }
    return null;
  }
  function measure() {
    const m = main(), r = m.getBoundingClientRect();
    const comp = document.getElementById("composer").getBoundingClientRect();
    const last = latest(), lr = last ? last.getBoundingClientRect() : null;
    const visibleBottom = Math.min(comp.top, view().offsetTop + view().height);
    return {top: Math.round(m.scrollTop), sh: m.scrollHeight, ch: m.clientHeight,
            gap: Math.round(m.scrollHeight - m.scrollTop - m.clientHeight),
            lastBottom: lr ? Math.round(lr.bottom) : null, composerTop: Math.round(comp.top),
            mainTop: Math.round(r.top), viewport: Math.round(view().height), vvTop: Math.round(view().offsetTop), scrollY: Math.round(window.scrollY),
            lastVisible: !!lr && lr.bottom <= visibleBottom + 2 && lr.bottom > r.top,
            pill: document.getElementById("jumppill").classList.contains("show")};
  }

  // --- the keyboard ---
  const view = () => window.visualViewport || vv;
  const kbShade = document.createElement("div");
  kbShade.textContent = "keyboard (simulated)";
  kbShade.style.cssText = "position:fixed;left:0;right:0;bottom:0;z-index:99;background:#5a5f66;" +
    "color:#e8e8e8;font:600 15px -apple-system,sans-serif;display:none;align-items:center;" +
    "justify-content:center;opacity:.96";
  async function keyboard(open) {
    const reply = document.getElementById("reply");
    if (!STUB) {
      if (open) reply.focus(); else reply.blur();
      // until the keyboard has finished moving
      let last = -1;
      for (let i = 0; i < 30; i++) {
        await sleep(100);
        const h = Math.round(view().height);
        if (h === last && (open ? h < window.innerHeight - 100 : h > window.innerHeight - 100)) break;
        last = h;
      }
      return;
    }
    vv.height = open ? window.innerHeight - KB : window.innerHeight;
    kbShade.style.height = KB + "px";
    kbShade.style.display = open ? "flex" : "none";
    if (open && reply && reply.focus) reply.focus({preventScroll: false});
    if (!open && reply && reply.blur) reply.blur();
    vvT.dispatchEvent(new Event("resize"));
  }

  // --- the runs ---
  async function open(key) {
    openTarget("/?thread=" + encodeURIComponent(key));
    for (let i = 0; i < 60; i++) {
      await sleep(100);
      const msgs = document.getElementById("tmsgs");
      if (msgs && msgs.children.length && typeof tview !== "undefined" && tview.key === key) break;
    }
    await sleep(900);
  }
  const poll = () => loadTranscript(false);
  const TRIGGERS = {
    "keyboard-open": async () => keyboard(true),
    "keyboard-close": async () => { await keyboard(true); await phase("kb-open", {run: current.name, mode: current.mode}); await keyboard(false); },
    "new-message": async (k) => { threads[k].msgs.push(said("assistant", "A new reply arrived. " + LOREM)); await poll(); },
    "tool-line-updates": async (k) => {
      const t = threads[k];
      t.msgs.push(result("live1", "found"), call("Read", "live2", "a.py"), result("live2", "x"),
                  call("Bash", "live3", "make test"));
      await poll();
    },
    "tool-line-expands": async () => {
      const m = main(), top = m.getBoundingClientRect().top, bottom = top + m.clientHeight;
      const lines = [...document.querySelectorAll("#tmsgs > .ctools")];
      const shown = lines.filter((l) => { const r = l.getBoundingClientRect(); return r.top > top + 60 && r.bottom < bottom - 60; });
      const pick = shown[shown.length - 1] || lines[lines.length - 1];
      if (pick && pick._ctl) pick._ctl.click();
    },
    "thumbnails-load": async (k) => {
      const t = threads[k];
      const path = "/nonexistent/probe-" + k + ".png";
      t.msgs.push(said("user", "a photo\n\n[The user attached 1 file(s), saved locally:\n" + path + "]"));
      t.files = [{name: "probe.png", path, direction: "in", ts: Date.parse(t.msgs[t.msgs.length - 1].ts) / 1000,
                  size: 1000, exists: true}];
      await poll();
    },
    // A poll that draws a file onto an earlier message (above the reader):
    // the transcript read's draw, with the live end's drawn inside it.
    "attachment-above": async (k) => {
      const t = threads[k], m = t.msgs.find((x) => x.blocks[0].text && x.blocks[0].text.includes("probe-early"));
      t.files = [{name: "early.png", path: "/nonexistent/probe-early.png", direction: "in",
                  ts: Date.parse(m.ts) / 1000, size: 1000, exists: true}];
      t.msgs.push(said("assistant", "And another reply. " + LOREM));
      await poll();
    },
    "working-appears": async (k) => {
      threads[k].live = {running: true, turns: [{ref: "1795000000.000100", state: "running"}]};
      await poll();
    },
    "working-disappears": async (k) => {
      const t = threads[k];
      t.msgs.push(said("assistant", "Done. " + LOREM));
      t.live = {running: false, turns: [{ref: "1795000000.000100", state: "done"}]};
      await poll();
    },
    "thread-switch": async (k) => { baseline(k + "-other"); await open(k + "-other"); },
    "late-layout-growth": async () => {
      // Something above the reader grows after it was drawn, outside any draw
      // the page makes -- an image decoding, a font arriving: the first item.
      const first = document.querySelector("#tmsgs > .citem");
      if (first) first.style.paddingTop = "160px";
    },
  };
  const RUNNING = new Set(["tool-line-updates", "working-disappears"]);
  const current = {};
  async function run(name, mode, n) {
    Object.assign(current, {name, mode});
    const key = `D0PROBE:${n}.${Date.now() % 100000}`;
    baseline(key, {running: RUNNING.has(name)});
    await open(key);
    const m = main();
    if (mode === "up") {
      m.scrollTop = Math.max(0, Math.round(m.scrollHeight * 0.45 - m.clientHeight / 2));
      await sleep(500);
    }
    const anchor = topItem();
    const before = measure();
    await phase("before", {run: name, mode, before});
    await TRIGGERS[name](key);
    await sleep(name.startsWith("keyboard") ? 400 : 200);
    await phase("burst", {run: name, mode});
    await sleep(600);
    const after = measure();
    const shift = anchor && anchor.el.isConnected
      ? Math.round(anchor.el.getBoundingClientRect().top - main().getBoundingClientRect().top - anchor.at) : null;
    const switched = name === "thread-switch";
    // Opening a tools line is reading it: the line stays where it was tapped.
    const ok = name === "tool-line-expands" ? shift === 0
      : mode === "bottom" || switched ? after.lastVisible && after.gap <= 2
      : shift === 0;
    await phase("after", {run: name, mode, before, after, shift, ok});
    if (name.startsWith("keyboard")) await keyboard(false);
    await sleep(300);
  }
  async function main_() {
    document.body.appendChild(kbShade);
    await sleep(1500);
    log({phase: "start", stubbed: STUB ? stubbed : "real keyboard", kb: STUB ? KB : "real", inner: window.innerHeight, ua: navigator.userAgent,
         standalone: !!navigator.standalone});
    const want = RUNS;
    let n = 0;
    for (const name of Object.keys(TRIGGERS)) {
      if (want.length && !want.includes(name)) continue;
      for (const mode of ["bottom", "up"]) await run(name, mode, n++);
    }
    log({phase: "done"});
  }
  window.addEventListener("load", () => { main_().catch((e) => log({phase: "error", error: String(e && e.stack || e)})); });
})();

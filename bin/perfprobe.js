// The dashboard's performance probe: injected by `bin/dashshots --perf` into a
// copy of the dashboard (never the real one), ahead of the page's own script.
// With ?perf=1 it measures, through the page's own code paths and data:
//   paint      first paint and first contentful paint (the browser's own
//              paint timing), and when the session list's first card was drawn
//   scroll     frames while the list, a board and a thread are scrolled
//              down and back by a fixed path, one step a frame -- the same
//              path before and after, so the two are comparable
//   sheet      frames while the task panel opens (its animation, if any)
// and reports each to the harness (the stand-in bot, __BOT__) as JSON lines.
(function () {
  if (!/[?&]perf=/.test(location.search)) return;
  const BOT = "__BOT__";
  const log = e => fetch(BOT + "/probe/log", {method: "POST", mode: "no-cors", body: JSON.stringify(e)})
    .catch(() => {});
  const sleep = ms => new Promise(ok => setTimeout(ok, ms));
  let listAt = null;
  (function watch() {
    if (document.querySelector && document.querySelector("#list .card")) { listAt = performance.now(); return; }
    requestAnimationFrame(watch);
  })();
  function stats(dts, ms) {
    const s = dts.slice().sort((a, b) => a - b);
    const q = p => s.length ? s[Math.min(s.length - 1, Math.floor(p * s.length))] : null;
    return {frames: dts.length, fps: Math.round(10 * dts.length / (ms / 1000)) / 10,
            p50_ms: q(0.5) && +q(0.5).toFixed(1), p95_ms: q(0.95) && +q(0.95).toFixed(1),
            long: dts.filter(d => d > 25).length, worst_ms: s.length ? +s[s.length - 1].toFixed(1) : null};
  }
  // Frames counted for `ms` while `step(fraction)` runs once a frame.
  function frames(ms, step) {
    return new Promise(ok => {
      const dts = [];
      let t0 = null, last = null;
      function tick(t) {
        if (t0 === null) t0 = last = t; else { dts.push(t - last); last = t; }
        const f = (t - t0) / ms;
        if (f >= 1) return ok(stats(dts, t - t0));
        if (step) step(f);
        requestAnimationFrame(tick);
      }
      requestAnimationFrame(tick);
    });
  }
  // Down and back up along the same path: 3000px at most, each way.
  const scrollRun = (el, ms) => {
    if (!el) return Promise.resolve({missing: true});
    const span = Math.min(3000, Math.max(0, el.scrollHeight - el.clientHeight));
    return frames(ms, f => { el.scrollTop = Math.round(span * (f < 0.5 ? f * 2 : 2 - f * 2)); })
      .then(r => Object.assign(r, {span}));
  };
  async function run() {
    await sleep(4000);
    const paint = {};
    for (const p of (performance.getEntriesByType ? performance.getEntriesByType("paint") : []))
      paint[p.name] = +p.startTime.toFixed(1);
    const nav = performance.getEntriesByType ? performance.getEntriesByType("navigation")[0] : null;
    const out = {phase: "perf", width: innerWidth, paint, list_drawn_ms: listAt && +listAt.toFixed(1),
                 dom_content_loaded_ms: nav ? +nav.domContentLoadedEventEnd.toFixed(1) : null};
    out.scroll_list = await scrollRun(document.getElementById("list"), 3000);
    // The task panel opening, then scrolled.
    toggleTasks();
    out.sheet_open = await frames(500);
    await sleep(3000);
    out.scroll_tasks = await scrollRun(document.querySelector("#taskmodal .box"), 3000);
    toggleTasks();
    await sleep(800);
    // A project's board.
    openLink(new URLSearchParams("view=project&slug=silkworm"));
    await sleep(3500);
    out.scroll_board = await scrollRun(document.querySelector("#boardmodal .box"), 3000);
    closeBoard();
    await sleep(800);
    // The longest conversation, opened as a tap would.
    const conv = (lastSessions || []).filter(s => (s.kind || "thread") === "thread" && !s.hidden)
      .sort((a, b) => (b.turns || 0) - (a.turns || 0))[0];
    if (conv) {
      openSession(conv.key);
      out.thread_open = await frames(500);
      await sleep(4000);
      const m = document.querySelector("main");
      if (m) m.scrollTop = m.scrollHeight;
      out.scroll_thread = await scrollRun(m, 3000);
    }
    log(out);
    log({phase: "done"});
  }
  addEventListener("load", () => run().catch(e => log({phase: "error", error: String(e && e.stack || e)})));
})();

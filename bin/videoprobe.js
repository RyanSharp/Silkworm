// The dashboard's video probe: injected by `bin/dashshots --video-probe` into
// a copy of the dashboard (never the real one), ahead of the page's own
// script. With ?vprobe=1 it waits for the page to draw its <video> players,
// takes the first of each kind (.mp4, .mov, .m4v, .webm), and in the
// browser's own player: plays it (muted, as a script may without a tap),
// seeks to 60% of its length, and checks it plays on from there. Each
// answer goes to the harness (the stand-in bot, __BOT__) as a JSON line, and
// the lot is written over the page, for the screenshot.
(function () {
  if (!/[?&]vprobe=1/.test(location.search)) return;
  const BOT = "__BOT__";
  // Read now: the page takes its query off the address bar once it has it.
  const DRAWER = /[?&]drawer=1/.test(location.search);
  const log = e => fetch(BOT + "/probe/log", {method: "POST", mode: "no-cors", body: JSON.stringify(e)})
    .catch(() => {});
  const sleep = ms => new Promise(ok => setTimeout(ok, ms));
  const once = (el, ev, ms) => new Promise(ok => {
    const t = setTimeout(() => ok(false), ms);
    el.addEventListener(ev, () => { clearTimeout(t); ok(true); }, {once: true});
  });
  const r1 = x => Math.round(x * 10) / 10;
  function overlay(lines, good) {
    let d = document.getElementById("vprobe");
    if (!d) {
      d = document.createElement("pre");
      d.id = "vprobe";
      d.style.cssText = "position:fixed;left:8px;right:8px;top:8px;z-index:2147483647;margin:0;"
        + "white-space:pre-wrap;font:600 13px/1.35 ui-monospace,Menlo,monospace;color:#fff;"
        + "padding:8px 10px;border-radius:10px;pointer-events:none;opacity:.93";
      document.body.appendChild(d);
    }
    d.style.background = good === undefined ? "#444" : good ? "#1d6b35" : "#c0262d";
    d.textContent = lines.join("\n");
  }
  // Not "load": a page with players need not fire it soon, and the players
  // come later anyway.
  addEventListener("DOMContentLoaded", async () => {
    try {
      overlay(["video probe: waiting for the page's players…"]);
      let vids = [];
      for (let i = 0; i < 240 && !vids.length; i++) {
        await sleep(250);
        if (i % 8 === 0) overlay([`video probe: waiting for the page's players… ${i / 4}s`]);
        vids = Array.from(document.querySelectorAll("video"));
        // A thread whose transcript is gone is slow to say so, past the
        // deep link's wait for &drawer=1: its files are read, so open it here.
        if (!vids.length && i > 40 && DRAWER && typeof tview !== "undefined"
            && (tview.files || []).length && typeof openDrawer === "function" && !drawerIsOpen()) openDrawer();
      }
      const kinds = {};
      for (const v of vids) {
        const m = (v.getAttribute("src") || "").match(/\.(mp4|mov|m4v|webm)#/i);
        if (m && !kinds[m[1].toLowerCase()]) kinds[m[1].toLowerCase()] = v;
      }
      const lines = [`video probe: ${vids.length} players on the page`];
      if (!vids.length) {
        // What the page had got to (its globals: one lexical scope for all
        // classic scripts), so a probe that found nothing says why.
        const t = typeof tview !== "undefined" ? tview : {};
        lines.push("open: " + (typeof active !== "undefined" && active ? active.key : "none")
                   + ` · read: key ${t.key} cursor ${t.cursor} error ${t.error} busy ${t.busy}`
                   + ` files ${(t.files || []).length} · drawer ${typeof drawerIsOpen === "function" && drawerIsOpen()}`);
      }
      overlay(lines);
      const results = [];
      for (const [kind, v] of Object.entries(kinds)) {
        const name = decodeURIComponent((v.getAttribute("src").match(/path=([^#&]+)/) || ["", ""])[1]).split("/").pop();
        const r = {kind, name};
        v.muted = true;
        if (v.scrollIntoView) v.scrollIntoView({block: "center"});
        if (v.readyState < 1) r.metadata = await once(v, "loadedmetadata", 30000);
        r.duration = r1(v.duration);
        try { await v.play(); r.played = true; } catch (e) { r.played = false; r.play_error = String(e); }
        await sleep(1500);
        r.played_to = r1(v.currentTime);
        const target = v.duration * 0.6;
        const seeked = once(v, "seeked", 30000);
        v.currentTime = target;
        r.seeked = await seeked;
        r.seeked_to = r1(v.currentTime);
        await sleep(1500);
        r.after = r1(v.currentTime);
        v.pause();
        r.ready_state = v.readyState;
        r.error = v.error ? v.error.code : null;
        r.ok = !!(r.played && r.seeked && r.played_to > 0.1 && Math.abs(v.currentTime - target) < 3
                  && r.after > r.seeked_to && r.error === null);
        results.push(r);
        log({phase: "video", ...r});
        lines.push(`${r.ok ? "✓" : "✘"} .${kind} ${name}: ${r.duration}s long, played to ${r.played_to}s, `
                   + `seeked to ${r.seeked_to}s, on to ${r.after}s` + (r.error !== null ? `, error ${r.error}` : "")
                   + (r.play_error ? `, ${r.play_error}` : ""));
        overlay(lines);
      }
      const good = results.length > 0 && results.every(r => r.ok);
      overlay(lines, good);
      log({phase: "done", ok: good, results});
    } catch (e) {
      overlay(["video probe failed: " + e], false);
      log({phase: "error", error: String(e)});
    }
  });
})();

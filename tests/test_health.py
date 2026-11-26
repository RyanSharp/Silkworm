"""Project health and its probes.

Run by tests/run.py with every other tests/test_*.py; see tests/harness.py."""

from harness import *  # noqa: F401,F403


# --- project health (health.py) ------------------------------------------------

def _health_stores():
    import health as H
    import tasks as T
    d = Path(tempfile.mkdtemp())
    return H.HealthStore(d / "health.json"), T.TaskStore(d / "t.json"), d


def _health_items(ts, slug="cadence"):
    import health as H
    return [t for t in ts.all().values()
            if t.get("source") == H.SOURCE and t.get("project") == slug]


def _gh_run(rid, wf, sha, conclusion, at, event="push", branch="main"):
    return {"id": rid, "workflow_id": wf, "head_sha": sha * 40 if len(sha) == 1 else sha,
            "status": "completed", "conclusion": conclusion, "created_at": at,
            "event": event, "head_branch": branch,
            "html_url": f"https://github.com/o/r/actions/runs/{rid}"}


class _HealthGh:
    """gh, answered from fixtures: workflows, runs per workflow, logs per run."""

    def __init__(self, workflows, runs, logs=None, default_branch="main"):
        self.workflows, self.runs, self.logs = workflows, runs, logs or {}
        self.default_branch, self.calls, self._cache = default_branch, [], {}

    def __call__(self, *args, **kw):
        import health as H
        self.calls.append(args)
        if args[:1] == ("run",):
            rid = args[2]
            if rid not in self.logs:
                raise H.GhUnavailable(f"no log for {rid}")
            return self.logs[rid]
        path = args[-1]
        if path.endswith("/actions/workflows?per_page=100"):
            return json.dumps({"workflows": self.workflows})
        m = _re.search(r"/actions/workflows/(\d+)/runs\?branch=([^&]+)", path)
        if m:
            return json.dumps({"workflow_runs": [r for r in self.runs
                                                 if str(r["workflow_id"]) == m.group(1)]})
        return json.dumps({"default_branch": self.default_branch})

    def cached(self, name):
        return self._cache.get(name)

    def cache(self, name, value):
        self._cache[name] = value


def _xcode_log(*tests):
    body = "".join(f"test\tRun tests\t2026-10-19T16:43:51.9971800Z \t{t}()\n" for t in tests)
    return ("test\tRun tests\t2026-10-19T16:40:45.0975850Z note: building\n"
            "test\tRun tests\t2026-10-19T16:43:51.9968970Z Failing tests:\n" + body
            + "test\tRun tests\t2026-10-19T16:43:51.9973870Z \n"
            "test\tRun tests\t2026-10-19T16:43:51.9974250Z ** TEST FAILED **\n")


_CADENCE = {"slug": "cadence", "scope": {"cwd": "/w/cadence",
                                         "repo": "github.com/RyanSharp/Cadence"}}
_FLOWS = [{"id": 1, "name": "Tests", "path": ".github/workflows/tests.yml", "state": "active"},
          {"id": 2, "name": "Backend tests", "path": ".github/workflows/backend-tests.yml",
           "state": "active"}]


def test_health_state_transitions_page_once_and_clear():
    import health as H
    import tasks as T
    print("\nhealth: ok→fail is one item, fail→fail no second, fail→ok clears, unknown never pages")
    hs, ts, d = _health_stores()
    ok = H.result(H.OK, "2 workflows pass")
    bad = H.result(H.FAIL, "Tests failed at aaaa", ["Tests: failure"], "https://x/1",
                   fingerprint="1@aaaa")
    bad2 = H.result(H.FAIL, "Tests failed at bbbb", ["Tests: failure"], "https://x/2",
                    fingerprint="1@bbbb")
    unk = H.unknown("gh is not installed")

    def step(res, now):
        hs.record("cadence", "ci", res, now)
        return H.surface(hs, ts, "cadence", "ci", scope=_CADENCE["scope"])

    check("an unknown first result opens nothing", step(unk, 100) == {}
          and not _health_items(ts))
    check("nor does ok", step(ok, 200) == {} and not _health_items(ts))
    rec = hs.get("cadence", "ci")
    check("first_seen is the first check, last_changed when it became ok",
          rec["first_seen"] == 100 and rec["last_changed"] == 200, str(rec))
    moved = step(bad, 300)
    items = _health_items(ts)
    check("ok→fail opens exactly one needs-you item", "opened" in moved and len(items) == 1
          and items[0]["state"] == T.NEEDS_INPUT, str(moved))
    check("filed under the project, as the item's source", items[0]["project"] == "cadence"
          and items[0]["source"] == H.SOURCE and "Tests failed at aaaa" in items[0]["title"])
    item = items[0]["id"]
    check("fail→fail does not open a second", "updated" in step(bad, 400)
          and len(_health_items(ts)) == 1)
    check("nor does a different failure while the item is open -- it is reworded",
          "updated" in step(bad2, 500) and len(_health_items(ts)) == 1
          and "bbbb" in ts.get(item)["title"])
    check("a persistent failure keeps its first last_changed",
          hs.get("cadence", "ci")["last_changed"] == 300)
    check("fail→unknown neither closes it nor opens another",
          step(unk, 600) == {} and ts.get(item)["state"] == T.NEEDS_INPUT
          and len(_health_items(ts)) == 1)
    check("and unknown remembers the last known status",
          hs.get("cadence", "ci")["last_known"]["status"] == H.FAIL)
    check("unknown→fail again is still the one item", "updated" in step(bad2, 650)
          and len(_health_items(ts)) == 1)
    check("and still failing since it first failed, not since gh came back",
          hs.get("cadence", "ci")["last_changed"] == 300, str(hs.get("cadence", "ci")))
    moved = step(ok, 700)
    check("fail→ok closes the item", "closed" in moved and ts.get(item)["state"] == T.DONE,
          str(moved))
    check("and forgets it", not hs.get("cadence", "ci")["attention"])
    check("ok→ok says nothing", step(ok, 800) == {})
    # A person dismissing it silences that failure, and only that one.
    step(bad, 900)
    item2 = hs.get("cadence", "ci")["attention"]
    ts.transition(item2, T.CANCELLED, "dismissed")
    check("a dismissed failure is not re-opened while it persists",
          step(bad, 1000) == {} and len([t for t in _health_items(ts)
                                          if t["state"] not in T.ENDED]) == 0)
    check("a different failure opens a new item", "opened" in step(bad2, 1100)
          and len([t for t in _health_items(ts) if t["state"] not in T.ENDED]) == 1)
    hs.forget("cadence", "ci")              # a store that lost its records
    hs.record("cadence", "ci", bad2, 1150)
    check("a store that lost its pointer finds the open item, not a second",
          "updated" in H.surface(hs, ts, "cadence", "ci")
          and len([t for t in _health_items(ts) if t["state"] not in T.ENDED]) == 1)
    check("warn closes a failing item too -- it is not a page",
          "closed" in step(H.result(H.WARN, "flaky"), 1200)
          and not [t for t in _health_items(ts) if t["state"] not in T.ENDED])
    # The runner claims the item (a reply asked for a fix) between surface()
    # reading it and closing it: the close must refuse, not end a live turn.
    step(bad, 1300)
    live = hs.get("cadence", "ci")["attention"]
    ts.transition(live, T.QUEUED, "fix it")
    stale = dict(ts.get(live))
    ts.transition(live, T.RUNNING, "claimed")
    real_get = ts.get
    ts.get = lambda tid: dict(stale) if tid == live else real_get(tid)
    try:
        moved = step(ok, 1400)
    finally:
        ts.get = real_get
    check("a close racing the runner refuses, leaving the turn running",
          "closed" not in moved and ts.get(live)["state"] == T.RUNNING, str(moved))
    reread = H.HealthStore(d / "health.json", readonly=True)
    check("the store is on disk, atomically, with its backup",
          reread.get("cadence", "ci")["status"] == H.OK
          and reread.get("cadence", "ci")["checked_at"] == 1400
          and (d / "health.json.prev").exists())
    try:
        reread.record("cadence", "ci", ok)
        wrote = True
    except RuntimeError:
        wrote = False
    check("a read-only store refuses to save", not wrote)
    check("an unknown status is refused outright",
          _raises(lambda: H.result("red", "x"), ValueError))


def _raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    return False


def test_health_ci_reads_the_latest_run_of_each_workflow():
    import health as H
    print("\nhealth ci: the latest completed run per workflow, a path-filtered one included")
    # Tests ran a lot and passes; Backend tests (path-filtered) last ran days
    # ago and failed. The latest run overall is a pass.
    runs = [_gh_run(100 + i, 1, f"{i:040x}", "success", f"2026-10-1{i}T00:00:00Z")
            for i in range(1, 9)]
    runs += [_gh_run(50, 2, "e" * 40, "failure", "2026-10-02T00:00:00Z"),
             _gh_run(40, 2, "d" * 40, "success", "2026-10-01T00:00:00Z")]
    gh = _HealthGh(_FLOWS, runs, logs={"50": "FAILED tests/test_db.py::test_rls - boom\n"})
    res = H.ci_probe(_CADENCE, gh)
    check("a path-filtered workflow's old failing run still fails the check",
          res["status"] == H.FAIL and "Backend tests failed at eeeeeeee" in res["summary"],
          res["summary"])
    check("with the failing test pulled from its log",
          "tests/test_db.py::test_rls" in res["summary"] or
          any("test_rls" in e for e in res["evidence"]), str(res))
    check("its link is the failing run", res["link"].endswith("/runs/50"), res["link"])
    check("and its fingerprint names the workflow and commit", res["fingerprint"] == "2@" + "e" * 40)
    check("the default branch was asked for, the scope naming none",
          any(c[-1] == "repos/RyanSharp/Cadence" for c in gh.calls))
    # Newest first is not enough: cancelled runs and pull requests say nothing.
    runs2 = [_gh_run(9, 1, "c" * 40, "cancelled", "2026-10-19T03:00:00Z"),
             _gh_run(8, 1, "b" * 40, "failure", "2026-10-19T02:00:00Z", event="pull_request"),
             _gh_run(7, 1, "a" * 40, "success", "2026-10-19T01:00:00Z"),
             _gh_run(6, 2, "f" * 40, "success", "2026-10-18T01:00:00Z"),
             _gh_run(5, 2, "9" * 40, "failure", "2026-10-18T01:00:00Z", branch="feature")]
    hidden = [_gh_run(3, 1, "c" * 40, "cancelled", "2026-10-19T03:00:00Z"),
              _gh_run(2, 1, "b" * 40, "failure", "2026-10-19T02:00:00Z")]
    res = H.ci_probe(_CADENCE, _HealthGh(_FLOWS[:1], hidden))
    check("a cancelled latest run does not hide the failure before it",
          res["status"] == H.FAIL and "bbbbbbbb" in res["summary"], str(res))
    res = H.ci_probe(_CADENCE, _HealthGh(_FLOWS, runs2))
    check("a cancelled run, a pull request's and another branch's are looked past",
          res["status"] == H.OK and "2 workflows pass on main" in res["summary"], str(res))
    benji = {"slug": "benji", "scope": {"repo": "github.com/RyanSharp/BenjiChair",
                                        "branch": "v7"}}
    gh = _HealthGh(_FLOWS, [_gh_run(1, 1, "a" * 40, "success", "2026-10-19T01:00:00Z",
                                  branch="v7")])
    res = H.ci_probe(benji, gh)
    check("a project's own base branch is read, not the default",
          res["status"] == H.OK and "on v7" in res["summary"]
          and not any(c[-1].endswith("BenjiChair") for c in gh.calls), str(res))
    res = H.ci_probe(_CADENCE, _HealthGh([], []))
    check("no workflows is ok, saying so", res["status"] == H.OK
          and "no workflow" in res["summary"])
    disabled = [dict(_FLOWS[0], state="disabled_manually")]
    res = H.ci_probe(_CADENCE, _HealthGh(disabled, [_gh_run(1, 1, "a" * 40, "failure",
                                                          "2026-10-19T01:00:00Z")]))
    check("a disabled workflow is not read", res["status"] == H.OK, str(res))
    check("only projects with a GitHub repository get the ci check",
          [c.name for c in H.checks_for(_CADENCE)] == ["ci"]
          and H.checks_for({"slug": "k", "scope": {"cwd": "/w/k"}}) == []
          and H.github_repo({"scope": {"repo": "https://github.com/a/b.git"}}) == "a/b")


def test_health_ci_tells_flaky_from_broken():
    import health as H
    print("\nhealth ci: the same test failing on and off across commits is flaky, and said so")
    flake = "StoreServiceTests.testIsProFalseAfterSubscriptionExpires"
    seq = [  # oldest first: fail on A, pass on B, fail on C, pass on D (latest)
        (1, "a", "failure"), (2, "b", "success"), (3, "c", "failure"), (4, "d", "success")]
    runs = [_gh_run(r, 1, s, c, f"2026-10-1{r}T00:00:00Z") for r, s, c in seq]
    logs = {"1": _xcode_log(flake), "3": _xcode_log(flake)}
    gh = _HealthGh(_FLOWS[:1], runs, logs)
    res = H.ci_probe(_CADENCE, gh)
    check("passing on its latest run but flaky is warn, not fail", res["status"] == H.WARN,
          str(res))
    check("naming the test, read from the xcodebuild log block",
          flake in res["summary"] and any("flaky" in e and "aaaaaaaa" in e and "cccccccc" in e
                                         for e in res["evidence"]), str(res))
    fetched = sum(1 for c in gh.calls if c[:1] == ("run",))
    H.ci_probe(_CADENCE, gh)
    check("each run's log is read once and remembered",
          fetched == 2 and sum(1 for c in gh.calls if c[:1] == ("run",)) == 2, str(fetched))
    runs = runs + [_gh_run(5, 1, "e", "failure", "2026-10-15T00:00:00Z")]
    res = H.ci_probe(_CADENCE, _HealthGh(_FLOWS[:1], runs, {**logs, "5": _xcode_log(flake)}))
    check("failing on its latest run is fail, and still says flaky",
          res["status"] == H.FAIL and "flaky" in res["summary"] and flake[:20] in res["summary"],
          res["summary"])
    broke = [_gh_run(1, 1, "a", "success", "2026-10-11T00:00:00Z"),
             _gh_run(2, 1, "b", "failure", "2026-10-12T00:00:00Z"),
             _gh_run(3, 1, "c", "failure", "2026-10-13T00:00:00Z")]
    res = H.ci_probe(_CADENCE, _HealthGh(_FLOWS[:1], broke,
                                       {"2": _xcode_log("T.testX"), "3": _xcode_log("T.testX")}))
    check("broken and staying broken is fail, not flaky",
          res["status"] == H.FAIL and "flaky" not in res["summary"]
          and not any("flaky" in e for e in res["evidence"]), str(res))
    retried = [_gh_run(1, 1, "a", "failure", "2026-10-11T00:00:00Z"),
               _gh_run(2, 1, "a", "success", "2026-10-12T00:00:00Z"),
               _gh_run(3, 1, "a", "failure", "2026-10-13T00:00:00Z"),
               _gh_run(4, 1, "b", "success", "2026-10-14T00:00:00Z")]
    res = H.ci_probe(_CADENCE, _HealthGh(_FLOWS[:1], retried,
                                       {"1": _xcode_log("T.testX"), "3": _xcode_log("T.testX")}))
    check("failures on one commit only are not called flaky across commits",
          res["status"] == H.OK, str(res))
    check("log text loses its job/step columns and timestamps",
          H.log_failures(H.log_text(_xcode_log("A.b", "A.b", "C.d"))) == ["A.b", "C.d"])
    check("an unreadable log costs the names, not the check",
          H.ci_probe(_CADENCE, _HealthGh(_FLOWS[:1], runs[-1:]))["status"] == H.FAIL)
    expired = [_gh_run(i, 1, f"{i:x}" * 40, "failure" if i % 2 else "success",
                       f"2026-10-{10 + i}T00:00:00Z") for i in range(1, 10)]
    gh = _HealthGh(_FLOWS[:1], expired)
    H.ci_probe(_CADENCE, gh)
    tried = sum(1 for c in gh.calls if c[:1] == ("run",))
    H.ci_probe(_CADENCE, gh)
    check("a log that could not be read is not tried again every probe",
          0 < tried <= H.LOGS_PER_PROBE
          and sum(1 for c in gh.calls if c[:1] == ("run",)) > tried
          and sum(1 for c in gh.calls if c[:1] == ("run",)) <= 2 * tried
          and len({c[2] for c in gh.calls if c[:1] == ("run",)}) == len(
              [c for c in gh.calls if c[:1] == ("run",)]), str(tried))
    check("pull requests' and workflow_run's runs are not the branch's",
          "workflow_run" not in H.BRANCH_EVENTS and "pull_request" not in H.BRANCH_EVENTS)


def test_health_probe_that_cannot_run_is_unknown_and_never_pages():
    import health as H
    print("\nhealth: a probe that cannot run is unknown -- gh missing, a raise -- and pages nobody")
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = tempfile.mkdtemp()
    try:
        try:
            H.gh_cli("api", "repos/a/b")
            why = ""
        except H.GhUnavailable as e:
            why = str(e)
        check("gh missing raises GhUnavailable, saying so", why == "gh is not installed", why)
        res = H.ci_probe(_CADENCE, H.gh_cli)
    finally:
        os.environ["PATH"] = old
    check("and the ci check is unknown, not fail", res["status"] == H.UNKNOWN
          and "not installed" in res["summary"], str(res))

    def offline(*a, **k):
        raise H.GhUnavailable("gh api failed: HTTP 403: API rate limit exceeded")
    hs, ts, _ = _health_stores()
    hs.record("cadence", "ci", H.result(H.FAIL, "red", fingerprint="1@a"), 1)
    H.surface(hs, ts, "cadence", "ci")
    item = hs.get("cadence", "ci")["attention"]
    rows = H.run_all(hs, ts, [_CADENCE], offline)
    check("rate-limited is unknown", rows and rows[0]["status"] == H.UNKNOWN
          and "rate limit" in rows[0]["summary"], str(rows))
    check("and leaves the open item exactly as it was",
          ts.get(item)["state"] == "needs_input" and len(_health_items(ts)) == 1)
    reg = {"boom": H.Check("boom", lambda rec, gh: 1 / 0),
           "junk": H.Check("junk", lambda rec, gh: {"status": "purple"})}
    rows = H.run_all(hs, ts, [_CADENCE], offline, registry=reg)
    check("a probe that raises, or returns no result, is unknown -- the rest still run",
          sorted((r["check"], r["status"]) for r in rows)
          == [("boom", H.UNKNOWN), ("junk", H.UNKNOWN)], str(rows))
    by = {r["check"]: r["summary"] for r in rows}
    check("each saying which: the raise, or no result",
          "ZeroDivisionError" in by["boom"] and "returned no result" in by["junk"], str(by))
    check("and a check that no longer runs is retired, its item closed",
          hs.get("cadence", "ci") is None and ts.get(item)["state"] == "done")
    check("none of it opened an item", not [t for t in _health_items(ts)
                                            if t["state"] not in ("done", "cancelled")])
    hs.record("cadence", "ci", H.result(H.FAIL, "red", fingerprint="1@b"), 2)
    H.surface(hs, ts, "cadence", "ci")
    lost = hs.get("cadence", "ci")["attention"]
    hs.update("cadence", "ci", attention="")
    H.retire(hs, ts, set())
    check("retiring a check whose pointer was lost still closes its item",
          ts.get(lost)["state"] == "done" and hs.get("cadence", "ci") is None)
    hs.record("cadence", "ci", H.result(H.FAIL, "red", fingerprint="1@c"), 3)
    H.surface(hs, ts, "cadence", "ci")
    busy = hs.get("cadence", "ci")["attention"]
    ts.transition(busy, "queued", "fix it")
    ts.transition(busy, "running", "claimed")
    H.retire(hs, ts, set())
    check("one whose item is mid-turn is kept, to be retired after",
          hs.get("cadence", "ci") is not None and ts.get(busy)["state"] == "running")


def test_health_registry_takes_new_checks_without_changing_the_core():
    import health as H
    print("\nhealth: a new check plugs in through the registry")
    hs, ts, _ = _health_stores()
    opened, closed = [], []
    reg = {"disk": H.Check("disk", lambda rec, gh: H.result(H.FAIL, "disk 99%"),
                           applies=lambda rec: rec["slug"] in ("cadence", "old"))}
    projects_ = [_CADENCE, {"slug": "kitchen", "scope": {}}, {**_CADENCE, "slug": "old",
                                                             "archived": True}]
    H.run_all(hs, ts, projects_, None, registry=reg,
              on_open=lambda r, i: opened.append(i["id"]),
              on_close=lambda r, i: closed.append(i["id"]))
    check("it runs where it applies, never on archived projects",
          [(r["project"], r["check"]) for r in hs.all()] == [("cadence", "disk")])
    check("and its failure is announced once", len(opened) == 1)
    H.run_all(hs, ts, projects_, None, registry=reg, on_open=lambda r, i: opened.append(1))
    check("not again an hour later", len(opened) == 1)
    reg["disk"].probe = lambda rec, gh: H.result(H.OK, "disk 40%")
    H.run_all(hs, ts, projects_, None, registry=reg, on_close=lambda r, i: closed.append(1))
    check("and its recovery is announced", len(closed) == 1)
    check("a bad name is refused", _raises(lambda: H.register("Bad Name", lambda r, g: None),
                                           ValueError))
    check("HEALTH_LEAD: 15 minutes by default, off stops it, nonsense refused",
          H.parse_lead("") == 15 and H.parse_lead(None) == 15 and H.parse_lead("1h") == 60
          and H.parse_lead("0") == 0 and H.parse_lead("off") is None
          and H.parse_lead("OFF") is None
          and _raises(lambda: H.parse_lead("hourly"), ValueError)
          and _raises(lambda: H.parse_lead("5s"), ValueError))


def test_health_reaches_the_operator_digest_and_board():
    import board as BD
    import digest as D
    import health as H
    import operator_role as O
    import projects as P
    print("\nhealth: the operator's context, the digest and the board each get it once")
    hs, ts, d = _health_stores()
    now = time.time()
    hs.record("cadence", "ci", H.result(H.FAIL, "Tests failed at 29d8b76c",
                                        ["Tests: failure at 29d8b76c", "  failing: S.t"],
                                        "https://github.com/x/runs/1", "1@29d8"), now - 7200)
    H.surface(hs, ts, "cadence", "ci")
    hs.record("silkworm", "ci", H.result(H.OK, "1 workflow passes on main",
                                         ["SECRET-OK-EVIDENCE"]), now)
    hs.record("trader", "ci", H.unknown("gh is not installed"), now)
    rows = hs.all()
    sec = H.context_section(rows, now)
    check("the section details every non-ok check, worst first",
          sec.index("cadence") < sec.index("trader") and "failing: S.t" in sec
          and "https://github.com/x/runs/1" in sec and "unknown" in sec, sec)
    check("names the needs-you item", hs.get("cadence", "ci")["attention"] in sec)
    check("and counts the ok ones without listing them",
          "SECRET-OK-EVIDENCE" not in sec and "silkworm" not in sec
          and "1 of 3 checks ok across 3 projects" in sec, sec)
    check("says when nothing has run", "no health checks" in H.context_section([]))
    text = O.context([], lambda r: "", {"ok": True}, [], [], {}, {}, "", health_rows=rows)
    check("the operator's live state carries a Project health section",
          "## Project health" in text and "Tests failed at 29d8b76c" in text, text[-600:])
    text = O.context([], lambda r: "", {"ok": True}, [], [], {}, {}, "", health_rows=None)
    check("and a store it could not read is said, not taken for healthy",
          "(could not be read this turn)" in text.split("## Project health")[1][:120])
    recs = list(ts.all().values())
    dg = D.render(recs, now, health=rows)
    check("the digest has one health line per project that is not ok",
          dg.count("• health:") == 2 and "ci fail — Tests failed at 29d8b76c" in dg
          and "*silkworm*" not in dg, dg)
    check("and does not count the item as needs input as well", "needs input" not in dg, dg)
    check("without health rows, no health line", "health" not in D.render(recs, now))
    ps = P.ProjectStore(d / "p.json")
    ps.ensure("cadence", scope=_CADENCE["scope"])
    ps.ensure("silkworm", scope={"cwd": "/w/s"})
    ov = BD.overview(ps.all(), recs, health_rows=rows)
    card = {p["slug"]: p for p in ov["projects"]}
    check("the dashboard card carries one line while a check fails",
          (card["cadence"].get("health") or {}).get("status") == H.FAIL
          and "ci: Tests failed" in card["cadence"]["health"]["line"]
          and card["silkworm"].get("health") is None, str(card["cadence"].get("health")))
    check("and the item counts once among what needs you", card["cadence"]["needs"] == 1)
    viz = (BASE / "visualizer.py").read_text()
    check("the dashboard renders it", "p.health ? " in viz and "esc(p.health.line)" in viz)


def test_health_cli_prints_without_writing_and_operator_may_run_it():
    import health as H
    import roles as R
    print("\nsilkworm health: prints the store, writes nothing, and is the operator's")
    cli = _load_cli()
    hs, ts, d = _health_stores()
    path = d / "health.json"
    now = time.time()
    hs.record("cadence", "ci", H.result(H.WARN, "2 workflows pass on main, but flaky",
                                        ["  flaky: S.t failed on 52518530, 29d8b76c"],
                                        "https://github.com/x/runs/9"), now - 3600)
    hs.record("silkworm", "ci", H.result(H.OK, "1 workflow passes on main",
                                         ["OK-DETAIL"]), now)
    before = {p.name: p.stat().st_mtime_ns for p in d.iterdir()}

    def run(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.do_health(argv, path=path, now=now)
        return code, out.getvalue(), err.getvalue()

    code, out, _ = run([])
    check("prints every project's checks", code == 0 and "cadence" in out and "silkworm" in out
          and "warn" in out and "flaky: S.t" in out and "2 check(s), 1 ok" in out, out)
    check("an ok check gets its line, not its evidence", "OK-DETAIL" not in out, out)
    code, out, _ = run(["--project", "cadence"])
    check("--project narrows it", code == 0 and "  cadence" in out
          and "  silkworm" not in out, out)
    code, out, _ = run(["--json", "--project", "silkworm"])
    data = json.loads(out)
    check("--json is the records", code == 0 and data["ok"] and data["overall"] == "ok"
          and [r["project"] for r in data["checks"]] == ["silkworm"], out)
    for bad in (["--run"], ["--project"], ["--write", "x"], ["cadence"]):
        code, _, err = run(bad)
        check(f"{' '.join(bad)} is a usage error", code == 2 and "usage" in err, err)
    after = {p.name: p.stat().st_mtime_ns for p in d.iterdir()}
    check("and nothing was written", before == after, f"{before} {after}")
    code, out, _ = run([])
    missing = d / "nope.json"
    code2 = cli.do_health([], path=missing)
    check("no store yet is 'no checks have run', exit 0, and creates nothing",
          code2 == 0 and not missing.exists())
    path.write_text("{not json")
    (d / "health.json.prev").write_text("{not json")
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        code3 = cli.do_health([], path=path)
    check("an unreadable store exits 1, saying so", code3 == 1 and "cannot be read" in err.getvalue(),
          err.getvalue())
    check("and is left exactly as it was", path.read_text() == "{not json"
          and not (d / "health.json.corrupt").exists())
    tools = R.OPERATOR_TOOLS.format(bin="/s/bin/silkworm")
    check("the operator's allowlist has `silkworm health`",
          "Bash(/s/bin/silkworm health:*)" in tools)
    src = ast.get_source_segment((BASE / "bin" / "silkworm").read_text(),
                                 next(n for n in ast.parse((BASE / "bin" / "silkworm").read_text()).body
                                      if isinstance(n, ast.FunctionDef) and n.name == "do_health"))
    check("do_health opens the store read-only and runs no probe unless --probe asks",
          "readonly=True" in src and "gh_cli" not in src and "run_all" not in src
          and "bot_call" not in src and "_save" not in src
          and src.count("probe_now(") == 1 and "if probe:" in src)
    check("documented, and dispatched", "silkworm health [--project P] [--json]" in cli.__doc__
          and 'cmd == "health"' in (BASE / "bin" / "silkworm").read_text())
    check("health.json is gitignored", "health.json" in
          (BASE / ".gitignore").read_text().splitlines())


def test_health_scheduler_is_off_the_hot_path_and_swallows_failures():
    import health as H
    print("\nhealth scheduler: its own registered thread, daily, and a failing pass is retried")
    src = (BASE / "bot.py").read_text()
    check("registered with daemons, forever only when on",
          'daemons.start(_health_scheduler, "health", forever=health_enabled())' in src)

    class Stop(BaseException):
        pass

    class Store:
        def __init__(self):
            self.on = "2026-10-19"
        def pass_on(self):
            return self.on
        def mark_pass(self, now):
            self.on = now.strftime("%Y-%m-%d")

    def sched(at, clock, run, beats=4):
        sleeps, logged = [], []
        times = iter(clock)

        def sleep(sec):
            sleeps.append(sec)
            if len(sleeps) > beats:
                raise Stop()
        fake_log = types.SimpleNamespace(exception=lambda *a, **k: logged.append(a),
                                         info=lambda *a, **k: None, error=lambda *a, **k: None)
        store = Store()
        import threading
        ns = lift("_health_scheduler", HEALTH_AT=at, HEALTH_SCHEDULE="x",
                           HEALTH_BUSY=threading.Event(),
                           HEALTH=store, health=H, run_health=run,
                           datetime=types.SimpleNamespace(now=lambda: next(times)),
                           time=types.SimpleNamespace(sleep=sleep), log=fake_log)
        try:
            ns["_health_scheduler"]()
            return False, sleeps, logged, store
        except Stop:
            return True, sleeps, logged, store

    day = lambda h, m=0: datetime(2026, 10, 20, h, m)
    runs = []
    stopped, sleeps, _, store = sched("08:15", [day(7), day(8, 14), day(8, 15), day(9),
                                                day(23)], lambda: runs.append(1))
    check("one pass a day, at its time: not before, not twice",
          stopped and len(runs) == 1 and store.on == "2026-10-20", f"{runs} {store.on}")
    check("after a settling delay, then a five-minute beat", sleeps[0] == 120
          and set(sleeps[1:]) == {300}, str(sleeps))
    boom = []

    def raising():
        boom.append(1)
        raise RuntimeError("gh exploded")
    stopped, _, logged, store = sched("08:15", [day(10), day(10, 5), day(10, 10), day(10, 15),
                                                day(10, 20)], raising)
    check("a pass that raises is logged and tried again on the next beat, not marked done",
          stopped and len(boom) == 4 and len(logged) == 4 and store.on == "2026-10-19",
          f"{boom} {store.on}")
    stopped, sleeps, _, _ = sched(None, [], lambda: runs.append("off"))
    check("off (no time) returns at once, running nothing", not stopped and not sleeps
          and "off" not in runs)


def test_health_runs_daily_just_before_the_operator_sweep():
    import health as H
    import operator_role as O
    import projects as P
    print("\nhealth: once a day, shortly before the sweep, caught up after a restart")
    at, line = H.schedule("08:30", "15m", O.DEFAULT_SWEEP_AT, P.parse_at)
    check("15 minutes before the operator's sweep", at == "08:15"
          and "daily at 08:15" in line and "before the operator's sweep at 08:30" in line, line)
    at, _ = H.schedule("9am", "1h", O.DEFAULT_SWEEP_AT, P.parse_at)
    check("the lead is configurable, and follows the sweep's own time", at == "08:00", at)
    at, _ = H.schedule("08:30", "0", O.DEFAULT_SWEEP_AT, P.parse_at)
    check("0 is at the sweep's time, not off", at == "08:30", str(at))
    at, line = H.schedule("off", "15m", O.DEFAULT_SWEEP_AT, P.parse_at)
    check("with the sweep off the daily pass still runs, at that time",
          at == "08:15" and "sweep is off" in line, f"{at} {line}")
    at, _ = H.schedule("0", "15m", O.DEFAULT_SWEEP_AT, P.parse_at)
    check("OPERATOR_SWEEP_AT=0 is midnight, as the sweep reads it, not off", at == "00:00", str(at))
    at, line = H.schedule("nonsense", "15m", O.DEFAULT_SWEEP_AT, P.parse_at)
    check("an unreadable sweep time anchors to the default, saying so",
          at == "08:15" and "unreadable" in line, line)
    for lead in ("off", "none"):
        at, line = H.schedule("08:30", lead, O.DEFAULT_SWEEP_AT, P.parse_at)
        check(f"HEALTH_LEAD={lead} is off", at is None and "off" in line, f"{at} {line}")
    for bad in ("soon", "13h", "-5m", "90s"):
        at, line = H.schedule("08:30", bad, O.DEFAULT_SWEEP_AT, P.parse_at)
        check(f"HEALTH_LEAD={bad} is refused as off, with the reason",
              at is None and line.startswith("off: "), f"{at} {line}")
    check("a lead past midnight is held at 00:00, still today", H.pass_at("00:10", 30) == "00:00")
    d = lambda h, m=0: datetime(2026, 10, 20, h, m)
    check("not before its time", not H.pass_due(d(8, 14), "08:15", "2026-10-19"))
    check("due at its time", H.pass_due(d(8, 15), "08:15", "2026-10-19"))
    check("caught up after a restart that slept through it",
          H.pass_due(d(13), "08:15", "2026-10-18"))
    check("and never twice in a day", not H.pass_due(d(13), "08:15", "2026-10-20"))
    hs, _ts, dd = _health_stores()
    hs.record("cadence", "ci", H.result(H.OK, "fine"))
    hs.mark_pass(d(8, 16))
    again = H.HealthStore(dd / "health.json", readonly=True)
    check("the day a pass finished is durable, beside the results",
          again.pass_on() == "2026-10-20" and again.get("cadence", "ci"))

    # The sweep waits for that morning's pass as it waits for the digest.
    st = O.State(Path(tempfile.mkdtemp()) / "operator.json")
    st.set_thread("D1:1.0")
    today = "2026-10-20"
    check("health on and not yet run: the sweep waits",
          not st.sweep_due(d(8, 30), "08:30", today, True, health_last_on="2026-10-19",
                           health_on=True))
    check("but not for ever: due after the grace, as for the digest",
          st.sweep_due(d(11, 31), "08:30", today, True, health_last_on="2026-10-19",
                       health_on=True))
    check("health ran this morning: due at its time",
          st.sweep_due(d(8, 30), "08:30", today, True, health_last_on=today, health_on=True))
    check("health off: never waited for",
          st.sweep_due(d(8, 30), "08:30", today, True, health_last_on="", health_on=False))

    src = (BASE / "bot.py").read_text()
    tree = ast.parse(src)
    main = next(n for n in tree.body if isinstance(n, ast.If)
                and "__main__" in ast.unparse(n.test))
    logs = [ast.unparse(c) for c in ast.walk(main) if isinstance(c, ast.Call)
            and ast.unparse(c.func) == "log.info"]
    check("the resolved schedule is logged at startup",
          any("'health checks: %s'" in c and "HEALTH_SCHEDULE" in c for c in logs), str(logs)[-400:])
    check("and HEALTH_EVERY is no longer read, only warned about if left in .env",
          'os.environ.get("HEALTH_EVERY", ' not in src and "parse_every" not in src
          and "HEALTH_EVERY" in ast.unparse(main) and not hasattr(H, "parse_every"))
    sched = ast.unparse(next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                             and n.name == "_operator_scheduler"))
    check("the sweep scheduler passes today's health pass to sweep_due",
          "health_last_on=" in sched and "health_on=health_on" in sched, sched[-500:])
    check("and waits while a pass is under way",
          "HEALTH_BUSY.is_set()" in sched.split("sweep_due")[0], sched[-700:])
    readme = (BASE / "README.md").read_text()
    env = (BASE / ".env.example").read_text()
    check("documented: HEALTH_LEAD and HEALTH_WARN_DAYS in the README and .env.example",
          "`HEALTH_LEAD`" in readme and "`HEALTH_WARN_DAYS`" in readme
          and "#HEALTH_LEAD=15m" in env and "#HEALTH_WARN_DAYS=3" in env
          and "| `HEALTH_EVERY`" not in readme)


def _brief_registry():
    """The real registry's decisions and short forms -- the CI check's and
    the probes' -- without the probes registered globally."""
    import health as H
    P = _probes()
    reg = P.register_all(registry={})
    reg["ci"] = H.REGISTRY["ci"]
    return reg


def test_health_brief_is_one_line_per_project_with_what_is_done_or_decided():
    import health as H
    import operator_role as O
    print("\nhealth brief: one line per project, a decision or a proposal per problem, one message")
    reg = _brief_registry()
    hs, ts, _d = _health_stores()
    now = time.time()
    day = 86400
    hs.record("cadence", "ci", H.result(H.OK, "2 workflows pass on main (latest at 77e936ec)"),
              now - 5 * day)
    hs.record("cadence", "backend-drift", H.result(H.OK, "production matches origin/main"),
              now - 5 * day)
    hs.record("cadence", "app-store", H.result(H.OK, "1.1.0 live"), now - 5 * day)
    hs.record("silkworm", "ci", H.result(H.OK, "no workflow has run on main"), now - day)
    green = H.brief(hs.all(), ts, now, registry=reg)
    check("all green: one line per project, nothing under them",
          green.splitlines()[1:] == ["🟢 *cadence* — CI green, backend current, 1.1.0 live",
                                     "🟢 *silkworm* — no CI runs"]
          and "4 of 4 checks ok" in green and "•" not in green, green)
    # A failure that is new, a warning that is old, a decision, an unknown.
    hs.record("cadence", "ci", H.result(H.FAIL, "on main: Tests failed at 29d8b76c",
                                        ["Tests: failure at 29d8b76c"], "https://x/1", "1@29d8"),
              now - 3600)
    hs.record("cadence", "backend-drift", H.result(H.WARN, "2 change(s) on origin/main not in "
                                                   "production, oldest 5d"), now - 2 * day)
    hs.record("trader", "healthcheck", H.result(H.FAIL, "the trader's healthcheck fails: bars",
                                                ["bars: stale"], fingerprint="bars"),
              now - 6 * day)
    hs.record("trader", "dispatching", H.result(H.OK, "dispatched"), now - 6 * day)
    hs.record("saga", "backend-drift", H.unknown("no credentials for Supabase project jl (x.env "
                                                 "is missing)"), now - day)
    prop = ts.create("Fix the bars", state="proposed", role="implementor", project="trader",
                     source="ideation", source_ref=H.proposal_ref("trader", "healthcheck"))
    H.link_proposal(hs, "trader", "healthcheck", prop["id"], now - 2 * day)
    text = H.brief(hs.all(), ts, now, registry=reg)
    lines = text.splitlines()
    check("worst projects first, ok ones last",
          [l.split("*")[1] for l in lines[1:] if not l.startswith("   ")]
          == ["cadence", "trader", "saga", "silkworm"], text)
    check("a project with trouble still names its ok checks in its line",
          "🔴 *cadence* — 1.1.0 live" in text and "🔴 *trader* — dispatching" in text, text)
    check("a new failure says it is new since yesterday, and what it was",
          "ci failing, new since yesterday (was ok): on main: Tests failed at 29d8b76c" in text, text)
    check("and that a fix may be proposed", "→ a fix may be proposed (once)" in text, text)
    check("an old one says how long it has been unchanged",
          "healthcheck failing, unchanged 6d" in text, text)
    check("and names its open proposal and since when, by weekday",
          f"→ proposal {prop['id']} open since "
          "today" in text,
          text)
    check("a backend's drift is the owner's decision, stated exactly",
          "→ decide: release cadence backend (`!release cadence backend`), or tell me it waits"
          in text, text)
    check("an unknown says it cannot look, and the decision that would let it",
          "backend-drift can't look" in text and "→ decide: add the saga Supabase credential"
          in text, text)
    check("readable on a phone: no line over 200 chars",
          max(len(l) for l in lines) <= 200, str(max(len(l) for l in lines)))
    check("a store that could not be read is said, not taken for healthy",
          "could not be read" in H.brief(None, ts, now)
          and "no checks have run" in H.brief([], ts, now))
    # The next morning's pass, a little late: yesterday's news is not new.
    for slug, chk in (("cadence", "ci"), ("cadence", "backend-drift")):
        r = hs.get(slug, chk)
        hs.record(slug, chk, H.result(r["status"], r["summary"], r["evidence"], r["link"],
                                      r["fingerprint"]), now + 86400 + 1800)
    nxt = H.brief(hs.all(), ts, now + 86400 + 2400, registry=reg)
    check("a change is new since yesterday on one morning only, however late the pass",
          "new since yesterday" not in nxt.split("*cadence*")[1].split("*trader*")[0]
          and "ci failing, unchanged" in nxt, nxt)
    # A proposal filed on Tuesday reads "since Tuesday".
    ts.update(prop["id"], created=now - 3 * day)
    later = H.brief(hs.all(), ts, now, registry=reg)
    check("a proposal from days ago is dated by weekday",
          f"open since {datetime.fromtimestamp(now - 3 * day).strftime('%A')}" in later, later)

    # One Slack message: the brief, then the reply -- whatever their size.
    reply = ("Landed tsk_a.\nRetried tsk_b.\n" * 200) + "*Needs you:* answer Q-END"
    big_brief = "\n".join(f"🟢 *p{i}* — CI green" for i in range(400))
    for limit in (3800, 1500):
        body = O.compose_sweep(big_brief, reply, limit)
        check(f"brief and reply fit one {limit}-char message",
              len(body) <= limit and body.startswith("🟢 *p0*") and body.endswith("Q-END"),
              f"{len(body)}")
        check("the brief is cut by whole lines, saying where the rest is",
              "more line(s): `silkworm health`" in body
              and all(l.startswith("🟢 *p") or "more line" in l
                      for l in body.split("\n\n")[0].splitlines()), body[:300])
    small = O.compose_sweep(text, "Landed tsk_a.\n*Needs you:* nothing.", 3800)
    check("a brief that fits is whole, above the reply",
          small.startswith(text) and small.endswith("*Needs you:* nothing."), small[-200:])
    check("no brief is the reply alone", O.compose_sweep("", "hi", 3800) == "hi")
    sent = []

    class Client:
        def chat_update(self, **kw):
            sent.append(kw["text"])
    n = O.post_sweep(Client(), "D1", "1.0", "2.0", reply, "\n_footer_", 3800, brief=big_brief)
    check("post_sweep posts brief and reply as one message within the limit",
          n == 1 and len(sent) == 1 and len(sent[0]) <= 3800 and sent[0].startswith("🟢 *p0*")
          and sent[0].endswith("_footer_"), str(len(sent[0]) if sent else 0))
    bot = (BASE / "bot.py").read_text()
    check("the sweep posts Silkworm's brief, not the model's paraphrase",
          "brief = health_brief()" in bot and "brief=brief)" in bot)


def _health_filing(**hs_kw):
    """handle_file_task, lifted, with a real health store and the real
    proposal rules wired in beside the real caps and dedup."""
    import health as H
    fn, counts, begin, ts, made = _file_task_impl()
    hs = H.HealthStore(Path(tempfile.mkdtemp()) / "health.json")
    fn.__globals__.update(health=H, HEALTH=hs, HEALTH_WARN_DAYS=3.0)
    return fn, counts, begin, ts, hs


def test_health_fix_proposals_are_once_per_check_through_the_gate():
    import health as H
    import scoping as S
    print("\nhealth fix proposals: once per (project, check), proposed, capped, deduped")
    fn, counts, begin, ts, hs = _health_filing()
    H.REGISTRY.setdefault("ci", H.REGISTRY["ci"])
    now = time.time()
    hs.record("cadence", "ci", H.result(H.FAIL, "on main: Tests failed at 29d8b76c",
                                        ["Tests: failure at 29d8b76c", "  failing: StoreServiceTests"],
                                        "https://github.com/x/runs/1", "1@29d8"), now - 3600)
    H.surface(hs, ts, "cadence", "ci")
    item = hs.get("cadence", "ci")["attention"]
    key = "D9:9.0"

    def file(goal, health="cadence/ci", **kw):
        return fn({"key": key, "goal": goal, "health": health, "caller_role": "operator",
                   "role": "implementor", **kw})
    begin(key)
    goal = "Fix the failing StoreServiceTests on main so the Tests workflow is green again"
    r = file(goal)
    t = ts.get(r.get("id") or "") or {}
    check("a newly failing check gets one proposal", r.get("ok") and t.get("state") == "proposed",
          str(r))
    check("under the check's project, filed by the operator, linked from its ref",
          t.get("project") == "cadence" and t.get("filed_by") == "operator"
          and t.get("source_ref") == "health:cadence/ci", str(t)[:300])
    check("with the check's evidence in its goal",
          "failing: StoreServiceTests" in t.get("goal", "")
          and "https://github.com/x/runs/1" in t.get("goal", "") and t["goal"].startswith(goal))
    rec = hs.get("cadence", "ci")
    check("and the check records the link, for this red spell of this workflow",
          rec.get("proposal") == t.get("id")
          and rec.get("proposal_for") == f"fail@{int(rec['last_changed'])}:1", str(rec)[:300])
    check("not a duplicate of the check's own needs-you item",
          ts.get(item)["state"] == "needs_input")
    r2 = file("Fix the failing tests on main another way, rewriting StoreServiceTests entirely")
    check("refused while that proposal is open", not r2.get("ok")
          and "already has a fix proposal open" in r2.get("error", ""), str(r2))
    ts.transition(t["id"], "cancelled", "dismissed")
    r3 = file("Fix the failing tests on main another way, rewriting StoreServiceTests entirely")
    check("and for the same trouble once it was dismissed", not r3.get("ok")
          and "already proposed" in r3.get("error", ""), str(r3))
    hs.record("cadence", "ci", H.result(H.FAIL, "on main: Tests failed at 52518530",
                                        ["Tests: failure at 52518530", "  failing: StoreServiceTests"],
                                        fingerprint="1@5251"), now)
    r3b = file("Fix the failing tests on main another way, rewriting StoreServiceTests entirely")
    check("a push to a still-red main is the same trouble, not a new one to propose again",
          not r3b.get("ok") and "already proposed" in r3b.get("error", ""), str(r3b))
    hs.record("cadence", "ci", H.result(H.FAIL, "on main: Tests failed at 52518530",
                                        ["Tests: failure at 52518530", "  failing: SyncTests"],
                                        fingerprint="1@5251,2@5251"), now)
    r4 = file("Fix SyncTests failing on main after the sync refactor broke the merge order")
    check("a different failure may be proposed once the old proposal is closed",
          r4.get("ok"), str(r4))
    # Restored from the task store alone: a record that lost its link.
    hs.update("cadence", "ci", proposal="", proposal_for="")
    r5 = file("Fix SyncTests failing on main, a third attempt at the same failing test")
    check("an open proposal is found by its ref when the record lost the link",
          not r5.get("ok") and r4.get("id", "?") in r5.get("error", ""), str(r5))

    # Warnings: only once they have persisted.
    hs.record("trader", "healthcheck", H.result(H.WARN, "the healthcheck has not run since Fri"),
              now - 86400)
    r6 = file("Make the trader healthcheck run on its schedule again", health="trader/healthcheck")
    check("a young warning is refused, saying when it becomes eligible",
          not r6.get("ok") and "has warned for 1.0d" in r6.get("error", ""), str(r6))
    hs.record("silkworm", "ci", H.result(H.WARN, "1 workflow passes on main, but flaky"),
              now - 4 * 86400)
    begin(key)                   # a new turn: the per-turn cap is checked below
    r7 = file("Make the flaky silkworm test deterministic with a fake clock", health="silkworm/ci")
    check("one past HEALTH_WARN_DAYS may be proposed", r7.get("ok"), str(r7))

    # Owner decisions are never tasks.
    _brief_registry()            # imports the probes; register their decisions locally
    P = _probes()
    saved = dict(H.REGISTRY)
    try:
        P.register_all(registry=H.REGISTRY)
        hs.record("cadence", "backend-drift", H.result(H.FAIL, "3 change(s) not in production, "
                                                       "oldest 9d"), now - 9 * 86400)
        before = len(ts.all())
        r8 = file("Deploy the cadence backend functions that drifted from main",
                  health="cadence/backend-drift")
        check("an owner decision is refused as a task, with the decision it is",
              not r8.get("ok") and "owner's decision" in r8.get("error", "")
              and "!release cadence backend" in r8.get("error", ""), str(r8))
        hs.record("fathom", "app-store", H.result(H.WARN, "1.0 waiting for review for 11d"),
                  now - 11 * 86400)
        r9 = file("Chase the App Store review for fathom", health="fathom/app-store")
        check("so is a slow App Review", not r9.get("ok") and "owner's decision" in r9["error"])
        check("and neither filed anything", len(ts.all()) == before)
    finally:
        H.REGISTRY.clear()
        H.REGISTRY.update(saved)

    # The ordinary caps still apply: the per-turn proposal limit...
    fn, counts, begin, ts, hs = _health_filing()
    begin(key)
    for i, (slug, words) in enumerate((("a", "alpha widget parser"), ("b", "beta cache eviction"),
                                       ("c", "gamma socket retries"))):
        hs.record(slug, "ci", H.result(H.FAIL, f"{slug} failing", [f"{slug}: failure"],
                                       fingerprint=f"{slug}@1"), now)
    got = [fn({"key": key, "goal": f"Fix the {w} that broke CI on main for {s}",
               "health": f"{s}/ci", "caller_role": "operator"})
           for s, w in (("a", "alpha widget parser"), ("b", "beta cache eviction"),
                        ("c", "gamma socket retries"))]
    check("the per-turn proposal cap still holds", [g.get("ok") for g in got][:S.MAX_PROPOSALS]
          == [True] * S.MAX_PROPOSALS and not got[S.MAX_PROPOSALS].get("ok")
          and "limit" in got[S.MAX_PROPOSALS].get("error", ""), str(got))
    check("and a held filing is kept on the check, for the brief",
          "limit" in (hs.get("c", "ci") or {}).get("proposal_held", ""),
          str(hs.get("c", "ci"))[:300])
    # ...the backlog cap...
    fn, counts, begin, ts, hs = _health_filing()
    begin(key)
    for i in range(S.max_open_proposals()):
        ts.create(f"Unrelated proposal number {i} about the {i}th widget", state="proposed",
                  role="implementor", project="d", source="ideation")
    hs.record("d", "ci", H.result(H.FAIL, "d failing", ["d: failure"], fingerprint="d@1"), now)
    r = fn({"key": key, "goal": "Fix the zebra encoder that broke CI on main",
            "health": "d/ci", "caller_role": "operator"})
    check("the open-proposal backlog cap still holds", not r.get("ok")
          and "standing limit" in r.get("error", ""), str(r))
    # ...and dedup, against anything but the check's own item.
    fn, counts, begin, ts, hs = _health_filing()
    begin(key)
    ts.create("Fix the failing StoreServiceTests on main so the Tests workflow is green",
              state="queued", role="implementor", project="cadence", source="scoped")
    hs.record("cadence", "ci", H.result(H.FAIL, "on main: Tests failed",
                                        ["failing: StoreServiceTests"], fingerprint="z"), now)
    r = fn({"key": key, "goal": "Fix the failing StoreServiceTests on main so the Tests "
            "workflow is green again", "health": "cadence/ci", "caller_role": "operator"})
    check("an equivalent task already open is a duplicate", not r.get("ok")
          and r.get("duplicate"), str(r))
    fn, counts, begin, ts, hs = _health_filing()
    begin(key)
    hs.record("e", "ci", H.result(H.FAIL, "e failing", ["e: failure"], fingerprint="e@1"), now)
    r = fn({"key": key, "goal": "Fix the quokka renderer that broke CI on main", "health": "e/ci"})
    check("a health fix is a proposal even from a person who did not ask for one",
          r.get("ok") and r.get("state") == "proposed"
          and ts.get(r["id"])["state"] == "proposed", str(r))
    hs.record("h", "ci", H.result(H.FAIL, "h failing", ["h: failure"], fingerprint="h@1"), now)
    short_goal = fn({"key": key, "goal": "fix", "health": "h/ci"})
    check("the operator's own words are held to the length rules, evidence aside",
          not short_goal.get("ok") and "at least" in short_goal.get("error", ""), str(short_goal))
    # Dedup ignores the check's own alarms, closed ones included.
    fn, counts, begin, ts, hs = _health_filing()
    begin(key)
    hs.record("f", "ci", H.result(H.FAIL, "Tests failed at aaaa", ["failing: WidgetTests"],
                                  "https://x/9", "1@aaaa"), now - 3 * 86400)
    H.surface(hs, ts, "f", "ci")
    old_item = hs.get("f", "ci")["attention"]
    hs.record("f", "ci", H.result(H.OK, "fine"), now - 2 * 86400)
    H.surface(hs, ts, "f", "ci")
    hs.record("f", "ci", H.result(H.FAIL, "Tests failed at bbbb", ["failing: WidgetTests"],
                                  "https://x/9", "1@bbbb"), now)
    H.surface(hs, ts, "f", "ci")
    r = fn({"key": key, "goal": "Fix the failing WidgetTests on main so Tests is green again",
            "health": "f/ci"})
    check("a fix is not a duplicate of the check's earlier, closed alarm",
          ts.get(old_item)["state"] == "done" and r.get("ok"), str(r))
    # A proposal nobody accepted is dismissed when its check passes again.
    hs.record("f", "ci", H.result(H.OK, "fine"), now + 60)
    H.surface(hs, ts, "f", "ci")
    check("an unaccepted fix proposal is dismissed once the check passes",
          ts.get(r["id"])["state"] == "cancelled", str(ts.get(r["id"])["state"]))
    acc = ts.create("Fix g", state="queued", role="implementor", project="g",
                    source_ref=H.proposal_ref("g", "ci"))
    hs.record("g", "ci", H.result(H.FAIL, "red", fingerprint="1@g"), now)
    H.link_proposal(hs, "g", "ci", acc["id"], now)
    hs.record("g", "ci", H.result(H.OK, "fine"), now + 60)
    H.surface(hs, ts, "g", "ci")
    check("but one already accepted is work under way, and left alone",
          ts.get(acc["id"])["state"] == "queued")
    bad = fn({"key": key, "goal": "Fix something real on the main branch", "health": "cadence"})
    check("--health names project/check", not bad.get("ok") and "<project>/<check>" in bad["error"])
    check("and a check that never ran is refused",
          "no such health check" in fn({"key": key, "goal": "Fix something real on main",
                                         "health": "nope/ci"}).get("error", ""))

    do_file_task, sent = _cli_file_task_impl()
    os.environ["SILKWORM_THREAD"] = key
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            do_file_task(["--propose", "--health", "cadence/ci", "Fix", "the", "tests"])
    finally:
        os.environ.pop("SILKWORM_THREAD", None)
    check("`silkworm task --health` sends the check", sent and sent[-1][1].get("health")
          == "cadence/ci" and sent[-1][1].get("goal") == "Fix the tests", str(sent))


def test_health_action_says_what_is_done_or_decided():
    import health as H
    print("\nhealth actions: eligible, watching, held, proposal, decision")
    reg = _brief_registry()
    hs, ts, _d = _health_stores()
    now = time.time()
    hs.record("t", "ci", H.result(H.FAIL, "broken", fingerprint="f1"), now)
    check("a fresh failure is eligible", H.action(hs.get("t", "ci"), ts, now, registry=reg)[0]
          == "eligible")
    H.hold_proposal(hs, "t", "ci", "the backlog is full", now)
    check("a held one says why", H.action(hs.get("t", "ci"), ts, now, registry=reg)
          == ("held", "fix proposal held: the backlog is full"))
    hs.record("t", "ci", H.result(H.FAIL, "broken", fingerprint="f1"), now + 86400)
    check("and the hold lapses at the next pass, offering it again",
          H.action(hs.get("t", "ci"), ts, now + 86400, registry=reg)[0] == "eligible")
    hs.record("t", "healthcheck", H.result(H.WARN, "late"), now - 86400)
    k, w = H.action(hs.get("t", "healthcheck"), ts, now, registry=reg)
    check("a young warning is watched, with the days left", k == "watching" and "in 2d" in w, w)
    hs.record("m", "disk", H.result(H.WARN, "x: 10 GB free of 494 GB (2%)"), now)
    check("disk is the owner's", H.action(hs.get("m", "disk"), ts, now, registry=reg)[0]
          == "decision")
    hs.record("x", "ci", H.unknown("gh is not installed"), now)
    check("an unknown without a decision has nothing to act on",
          H.action(hs.get("x", "ci"), ts, now, registry=reg)[0] == "looking")
    ctx = H.context_section(hs.all(), now, actions=lambda r: H.action(r, ts, now,
                                                                      registry=reg)[1])
    check("the operator's context says it under each check",
          "→ a fix may be proposed (once)" in ctx and "→ decide: free" in ctx
          and "→ watching" in ctx, ctx)
    bot = (BASE / "bot.py").read_text()
    check("and the bot gives it the exact command where one may be filed",
          "task --propose --health " in bot and "health_actions=health_action" in bot)


def test_mini_is_a_registered_project_that_is_never_clobbered():
    import health as H
    import projects as P
    import workspaces as W
    print("\nthe mini: a real, repo-less project, registered once, never overwritten")
    d = Path(tempfile.mkdtemp())
    ps = P.ProjectStore(d / "p.json")
    made = ps.register_if_absent("mini", "Mac mini", {"cwd": "/w/Silkworm"})
    rec = ps.get("mini")
    check("registered with its title and Silkworm's checkout as its home",
          made and rec["title"] == "Mac mini" and rec["scope"] == {"cwd": "/w/Silkworm"}
          and not rec["scope"].get("repo"), str(rec))
    ps.update("mini", scope={"cwd": "/elsewhere"}, auto_merge=False)
    stamp = ps.get("mini")["updated"]
    again = ps.register_if_absent("mini", "Mac mini", {"cwd": "/w/Silkworm"})
    check("a second registration changes nothing, not even its updated stamp",
          again is None and ps.get("mini")["scope"] == {"cwd": "/elsewhere"}
          and ps.get("mini")["updated"] == stamp, str(ps.get("mini")))
    ps2 = P.ProjectStore(d / "p2.json")
    ps2.ensure("box", initial={"title": "Mac mini"})
    check("a project already titled Mac mini is not shadowed by a second",
          ps2.register_if_absent("mini", "Mac mini", {"cwd": "/w"}) is None
          and ps2.get("mini") is None)
    check("reloaded, it is still one record", len(P.ProjectStore(d / "p.json").all()) == 1)
    P_ = _probes()
    recs = P_.projects_with_mini(ps.all())
    check("the health pass uses the registered record, not a stand-in beside it",
          [r["slug"] for r in recs].count("mini") == 1
          and next(r for r in recs if r["slug"] == "mini")["scope"] == {"cwd": "/elsewhere"})
    check("and still has the stand-in before registration",
          [r["slug"] for r in P_.projects_with_mini([])] == ["mini"])
    # Two projects at Silkworm's checkout: the repository wins the tie.
    both = [{"slug": "mini", "title": "Mac mini", "scope": {"cwd": "/w/Silkworm"}},
            {"slug": "silkworm", "title": "Silkworm",
             "scope": {"cwd": "/w/Silkworm", "repo": "github.com/x/Silkworm"}}]
    for order in (both, both[::-1]):
        check("work in Silkworm's checkout is still Silkworm's, whichever sorts first",
              W.project_for("/w/Silkworm/bot.py", order) == "silkworm")
    check("and a deeper project still beats both",
          W.project_for("/w/Silkworm/x/y", both + [{"slug": "deep", "scope": {"cwd": "/w/Silkworm/x"}}])
          == "deep")
    hs, ts, _ = _health_stores()
    hs.record("mini", "disk", H.result(H.FAIL, "disk nearly full"))
    H.surface(hs, ts, "mini", "disk", scope=ps.scope_for("mini"))
    item = ts.get(hs.get("mini", "disk")["attention"])
    check("its needs-you items take its scope, so a reply runs somewhere real",
          item["project"] == "mini" and item["scope"] == {"cwd": "/elsewhere"}, str(item)[:200])
    bot = (BASE / "bot.py").read_text()
    tree = ast.parse(bot)
    main = next(n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test))
    check("registered at startup", "register_mini()" in ast.unparse(main))
    reg = ast.unparse(next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                           and n.name == "register_mini"))
    check("through register_if_absent only, with Silkworm's checkout",
          "register_if_absent" in reg and "ensure(" not in reg and "BASE_DIR" in reg)


def test_operator_knows_it_is_the_chief_of_staff():
    import operator_role as O
    import roles as R
    print("\noperator prompt: chief of staff, decisions over work, never refile")
    text = R.OPERATOR_SYSTEM
    check("it knows its job", "chief of staff" in text)
    check("health every turn", "Check Project health on every turn" in text)
    check("decisions stated, not filed", "Prefer stating a decision over filing work" in text
          and "never file it as a task" in text)
    check("one proposal per check, never refiled",
          "--health <project>/<check>" in text and "never refile" in text)
    check("its refusals unchanged", "Not yours on your own judgement, and refused in code" in text
          and "releases, publishing where publish" in text)
    check("the sweep goal and note carry it", "--health" in O.SWEEP_GOAL
          and "do not repeat it" in O.SWEEP_NOTE)
    # The sweep's own "Not yours" list must not be shorter than the main
    # prompt's: every decision the accept guard refuses is named in both.
    for verb in ("accepting", "approving", "dismissing", "closing", "digging",
                 "cancelling", "not your own"):
        check(f"both 'Not yours' lists name {verb!r}",
              verb in text and verb in O.SWEEP_GOAL.split("Not yours", 1)[-1])


# --- project health probes (health_probes.py) -------------------------------------

_PROBE_REC = None


def _recorded():
    global _PROBE_REC
    if _PROBE_REC is None:
        _PROBE_REC = json.loads((BASE / "tests" / "fixtures" / "health" / "recorded.json")
                                .read_text())
    return _PROBE_REC


#: 2026-10-20 22:00 UTC, just after the fixtures were recorded.
_PROBE_NOW = datetime(2026, 10, 20, 22, 0, tzinfo=timezone.utc).timestamp()


def _probe_sys(**kw):
    """A System answering from recordings: `node` (App Store Connect), the
    trader's interpreter, lsof and netstat by responder; HTTP by URL suffix;
    git for real, over a scratch repository; files on a scratch home."""
    P = _probes()

    class Sys(P.System):
        def __init__(self, home, now=_PROBE_NOW, asc=None, trader=None, lsof=None,
                     netstat=None, http=None, env=None, disk=None, tick=0.0,
                     tailscale=None):
            self._home, self._now = Path(home), now
            self.asc, self.trader, self.lsof, self.netstat = asc or {}, trader, lsof, netstat
            self.http, self.envs, self.disk, self.tick = http or {}, env or {}, disk, tick
            self.calls, self._clock = [], 0.0
            self.tailscale = tailscale

        def home(self):
            return self._home

        def now(self):
            return self._now

        def clock(self):
            return self._clock

        def env(self, name):
            return self.envs.get(name)

        def _answer(self, ans, argv):
            if isinstance(ans, Exception):
                raise ans
            return ans(argv) if callable(ans) else ans

        def run(self, argv, timeout, cwd=None):
            argv = [str(a) for a in argv]
            self.calls.append((argv, timeout))
            self._clock += self.tick
            name = Path(argv[0]).name
            if name == "git":
                return super().run(argv, timeout, cwd)
            if name == "node":
                path = argv[3]
                for k, v in self.asc.items():
                    if path == k or path.endswith(k):
                        return self._answer(v, argv)
                return 0, "HTTP 404 Not Found\n{}", ""
            if name.startswith("python"):
                return self._answer(self.trader, argv)
            if name == "lsof":
                return self._answer(self.lsof, argv)
            if name == "netstat":
                return self._answer(self.netstat, argv)
            if name == "tailscale" and self.tailscale is not None:
                return self._answer(self.tailscale, argv)
            raise P.Unavailable(f"{name} is not installed here")

        def http_get(self, url, headers, timeout):
            self.calls.append(([url], timeout))
            self._clock += self.tick
            for k, v in self.http.items():
                if url.endswith(k):
                    if isinstance(v, Exception):
                        raise v
                    return v
            return 404, '{"message":"not found"}'

        def disk_usage(self, path):
            if isinstance(self.disk, Exception):
                raise self.disk
            return self.disk

    return Sys(**kw)


def _asc_ok(text):
    """asc.mjs's output for a recorded body: status line, then the JSON."""
    return (0, text, "")


def _asc_mut(text, fn):
    head, _, body = text.partition("\n")
    data = json.loads(body)
    fn(data)
    return (0, head + "\n" + json.dumps(data), "")


_ASC_401 = (0, "HTTP 401 Unauthorized\n" + json.dumps({"errors": [{
    "status": "401", "code": "NOT_AUTHORIZED",
    "title": "Authentication credentials are missing or invalid.",
    "detail": "Provide a properly configured and signed bearer token, and make sure that it "
              "has not expired."}]}), "")


def _probe_home():
    d = Path(tempfile.mkdtemp())
    (d / "home").mkdir()
    return d


def _run_probe(probe, rec):
    import health as H
    return H.run_check(H.Check("t", probe), rec, None)


def test_probe_app_store_reads_versions_and_reviews():
    P = _probes()
    import health as H
    print("\napp-store: version state and open review, from recorded App Store Connect answers")
    R = _recorded()
    cfg = {"apps": {"fathom": "6818993851", "mondays": "6806709316"}}
    fathom, mondays = {"slug": "fathom", "scope": {}}, {"slug": "mondays", "scope": {}}
    client = Path(tempfile.mkdtemp()) / "asc.mjs"
    client.write_text("// stand-in")
    cfg["asc"] = {"client": str(client)}

    def run(rec, asc, now=_PROBE_NOW, **kw):
        sys_ = _probe_sys(home=_probe_home(), now=now, asc=asc, **kw)
        return _run_probe(P.make_app_store(sys_, cfg), rec), sys_

    fa = {"appStoreVersions": _asc_ok(R["fathom_versions"]),
          "/v1/reviewSubmissions": _asc_ok(R["fathom_submissions"])}
    r, _ = run(fathom, fa)
    check("waiting for review 11 days is warn, saying how long and since when",
          r["status"] == H.WARN and "waiting for review for 11d" in r["summary"]
          and "2026-10-09" in r["summary"] and "longer than 3d" in r["summary"], r["summary"])
    r, _ = run(fathom, fa, now=datetime(2026, 10, 10, 12, tzinfo=timezone.utc).timestamp())
    check("a day in review is ok", r["status"] == H.OK and "waiting for review" in r["summary"],
          r["summary"])
    rej = dict(fa, appStoreVersions=_asc_mut(R["fathom_versions"], lambda d: d["data"][0]
                                             ["attributes"].update(appStoreState="REJECTED")))
    r, _ = run(fathom, rej)
    check("REJECTED fails, naming the version", r["status"] == H.FAIL and "1.0 rejected"
          in r["summary"] and r["fingerprint"], r["summary"])
    dan = dict(fa, appStoreVersions=_asc_mut(
        R["fathom_versions"], lambda d: d["data"][0]["attributes"].update(
            appStoreState="DEVELOPER_ACTION_NEEDED")))
    check("DEVELOPER_ACTION_NEEDED fails", run(fathom, dan)[0]["status"] == H.FAIL)
    unres = dict(fa, **{"/v1/reviewSubmissions": _asc_mut(
        R["fathom_submissions"], lambda d: d["data"][0]["attributes"].update(
            state="UNRESOLVED_ISSUES"))})
    r, _ = run(fathom, unres)
    check("a review submission with unresolved issues fails", r["status"] == H.FAIL
          and "unresolved issues" in r["summary"], r["summary"])
    r, _ = run(mondays, {"appStoreVersions": _asc_ok(R["mondays_versions"]),
                         "/v1/reviewSubmissions": _asc_ok(R["mondays_submissions"])})
    check("a version in preparation beside a live one is ok, and says both",
          r["status"] == H.OK and "1.1 prepare for submission" in r["summary"]
          and "1.0 live" in r["summary"], r["summary"])
    r, _ = run(fathom, {"appStoreVersions": _ASC_401, "/v1/reviewSubmissions": _ASC_401})
    check("a refused key is unknown, never fail", r["status"] == H.UNKNOWN
          and "refused the API key" in r["summary"], r["summary"])
    r, s = run(fathom, {"appStoreVersions": P.Unavailable("node timed out after 30s")})
    check("a timeout is unknown", r["status"] == H.UNKNOWN and "timed out" in r["summary"])
    check("every App Store Connect call is bounded",
          all(t <= 30 for argv, t in s.calls if Path(argv[0]).name == "node"))
    r, _ = run(fathom, {"appStoreVersions": (1, "", "Error: ENOENT AuthKey_X.p8")})
    check("the client failing is unknown", r["status"] == H.UNKNOWN, r["summary"])
    r, _ = run(fathom, {"appStoreVersions": (0, "HTTP 500 Internal Server Error\n{}", "")})
    check("Apple's own error is unknown", r["status"] == H.UNKNOWN, r["summary"])
    html = (0, "HTTP 200 OK\n<html>maintenance</html>", "")
    r, s = run(fathom, {"appStoreVersions": html, "/v1/reviewSubmissions": html})
    check("a 200 that is not JSON is unknown, not 'no version yet'", r["status"] == H.UNKNOWN,
          r["summary"])
    r, s = run(fathom, fa)
    q = next(a[4] for a, _ in s.calls if a[3] == "/v1/reviewSubmissions")
    check("it asks for the open submissions, not the newest ten",
          "filter[state]=" in q and "UNRESOLVED_ISSUES" in q, q)
    r = _run_probe(P.make_app_store(_probe_sys(home=_probe_home()), {"apps": {}}), fathom)
    check("no app id in the table is unknown, not a guess", r["status"] == H.UNKNOWN
          and "health.toml" in r["summary"])
    slow = _probe_sys(home=_probe_home(), asc=fa, tick=P.PROBE_BUDGET_S - 0.5)
    r = _run_probe(P.make_app_store(slow, cfg), fathom)
    check("a probe out of its budget stops and is unknown", r["status"] == H.UNKNOWN
          and "budget" in r["summary"], r["summary"])


def test_probe_testflight_reads_the_latest_build():
    P = _probes()
    import health as H
    print("\ntestflight: the latest build's processing state")
    R = _recorded()
    client = Path(tempfile.mkdtemp()) / "asc.mjs"
    client.write_text("// stand-in")
    cfg = {"apps": {"mondays": "6806709316"}, "asc": {"client": str(client)}}
    rec = {"slug": "mondays", "scope": {}}

    def run(ans, now=_PROBE_NOW):
        return _run_probe(P.make_testflight(_probe_sys(home=_probe_home(), now=now,
                                                       asc={"/v1/builds": ans}), cfg), rec)

    r = run(_asc_ok(R["mondays_builds"]))
    check("VALID is ok, naming build and version", r["status"] == H.OK
          and "build 41 (1.1) valid" in r["summary"], r["summary"])

    def state(s):
        return _asc_mut(R["mondays_builds"], lambda d: d["data"][0]["attributes"]
                        .update(processingState=s))
    for bad in ("INVALID", "FAILED"):
        r = run(state(bad))
        check(f"{bad} fails", r["status"] == H.FAIL and r["fingerprint"], r["summary"])
    # The recorded build was uploaded 2026-10-20 21:47 UTC.
    r = run(state("PROCESSING"), now=_PROBE_NOW + 3 * 3600)
    check("processing for over 2h warns", r["status"] == H.WARN and "still processing"
          in r["summary"], r["summary"])
    r = run(state("PROCESSING"))
    check("processing just after upload is ok", r["status"] == H.OK, r["summary"])
    r = run(_ASC_401)
    check("a refused key is unknown", r["status"] == H.UNKNOWN)
    r = run(P.Unavailable("node timed out after 30s"))
    check("a timeout is unknown", r["status"] == H.UNKNOWN)
    r = run(_asc_mut(R["mondays_builds"], lambda d: d.update(data=[], included=[])))
    check("no builds is ok", r["status"] == H.OK and "no builds" in r["summary"])


def _trader_tree(snippet_stdout=None):
    d = _probe_home()
    trader = d / "trader"
    (trader / ".venv" / "bin").mkdir(parents=True)
    (trader / ".venv" / "bin" / "python3").write_text("")
    return d, {"slug": "trader", "scope": {"cwd": str(trader)}}


def _trader_out(**changes):
    R = _recorded()
    line = next(ln for ln in R["trader_snippet_stdout"].splitlines()
                if ln.startswith("HEALTH_JSON "))
    data = json.loads(line[len("HEALTH_JSON "):])
    data.update(changes)
    return (0, "HEALTH_JSON " + json.dumps(data) + "\n", "")


def test_probe_trader_dispatching_reuses_the_heartbeat_verdict():
    P = _probes()
    import health as H
    print("\ndispatching: the trader's own heartbeat verdict, under its own interpreter")
    d, rec = _trader_tree()
    cfg = {"trader": {"project": "trader", "dispatch_grace_minutes": 60}}

    def run(trader):
        s = _probe_sys(home=d / "home", trader=trader)
        return _run_probe(P.make_dispatching(s, cfg), rec), s

    r, s = run(_trader_out())
    check("a dispatch for the latest slot is ok", r["status"] == H.OK
          and "62/62 live quotes" in r["summary"], r["summary"])
    argv = next(a for a, _ in s.calls if Path(a[0]).name.startswith("python"))
    check("it runs the trader's interpreter with the trader's verdict, grace 60 min",
          argv[0].endswith(".venv/bin/python3") and "health_verdict" in argv[2]
          and argv[-1] == "60")
    check("and the snippet runs nothing that pages", "page_failures" not in P.TRADER_SNIPPET
          and "main()" not in P.TRADER_SNIPPET and "notify" not in P.TRADER_SNIPPET)
    frozen = _trader_out(verdict=[False, "NOT DISPATCHING: newest dispatch 2026-10-14 20:00 UTC "
                                         "close, 62/62 live quotes — the daemon is alive but "
                                         "has missed its scheduled dispatch(es)"],
                         newest={"at": "2026-10-14T20:00:41+00:00", "timing": "close",
                                 "live": 62, "total": 62,
                                 "describe": "2026-10-14 20:00 UTC close, 62/62 live quotes"})
    r, _ = run(frozen)
    check("the 10-15/16 freeze shape fails, in the verdict's words", r["status"] == H.FAIL
          and "NOT DISPATCHING" in r["summary"]
          and r["fingerprint"] == "2026-10-14T20:00:41+00:00", r["summary"])
    r, _ = run((1, "", "ModuleNotFoundError: No module named 'pandas'"))
    check("the trader's code failing to run is unknown", r["status"] == H.UNKNOWN
          and "could not run" in r["summary"], r["summary"])
    r, _ = run(P.Unavailable("python3 timed out after 60s"))
    check("a timeout is unknown", r["status"] == H.UNKNOWN and "timed out" in r["summary"])
    r, _ = run(_trader_out(log_error="FileNotFoundError: data/paper_trade.log"))
    check("a missing daemon log is unknown", r["status"] == H.UNKNOWN)
    (Path(rec["scope"]["cwd"]) / ".venv" / "bin" / "python3").unlink()
    r, _ = run(_trader_out())
    check("no trader interpreter is unknown", r["status"] == H.UNKNOWN
          and "interpreter" in r["summary"], r["summary"])


def test_probe_trader_gateway_listening_and_connected():
    P = _probes()
    import health as H
    print("\ngateway: the API port listening, and the daemon connected to it")
    R = _recorded()
    d, rec = _trader_tree()
    cfg = {"trader": {"project": "trader"}}

    def run(trader=None, lsof=(0, R["lsof"], ""), netstat=(0, R["netstat"], "")):
        s = _probe_sys(home=d / "home", trader=trader or _trader_out(), lsof=lsof,
                       netstat=netstat)
        return _run_probe(P.make_gateway(s, cfg), rec)

    r = run()
    check("listening and connected (recorded lsof) is ok", r["status"] == H.OK
          and "connected" in r["summary"], r["summary"])
    r = run(_trader_out(gateway_check=[False, "Gateway 127.0.0.1:4002 unreachable"]))
    check("the port not listening fails", r["status"] == H.FAIL
          and "not listening" in r["summary"])
    r = run(_trader_out(daemon=[False, "daemon not loaded"]))
    check("the daemon not running fails", r["status"] == H.FAIL and "not running" in r["summary"])
    no_client = "\n".join(ln for ln in R["netstat"].splitlines() if "ESTABLISHED" not in ln)
    head = R["lsof"].splitlines()[0]
    r = run(lsof=(1, head + "\n", ""), netstat=(0, no_client, ""))
    check("disconnected while the newest dispatch covers the latest slot warns: the "
          "daemon reconnects at its next dispatch (Gateway's nightly re-auth)",
          r["status"] == H.WARN and "next dispatch" in r["summary"], r["summary"])
    missed = _trader_out(newest={"at": "2026-10-19T20:00:51+00:00", "timing": "close",
                                 "live": 62, "total": 62, "describe": "x"})
    r = run(missed, lsof=(1, head + "\n", ""), netstat=(0, no_client, ""))
    check("disconnected with a slot due since the newest dispatch fails", r["status"] == H.FAIL
          and "not connected" in r["summary"], r["summary"])
    r = run(_trader_out(daemon=[False, "launchctl failed: timed out"]))
    check("launchctl failing to answer is unknown, not 'daemon down'",
          r["status"] == H.UNKNOWN, r["summary"])
    r = run(lsof=(1, "", ""))
    check("a client lsof does not attribute to the daemon warns", r["status"] == H.WARN,
          r["summary"])
    r = run(lsof=(2, "", "lsof: bad"), netstat=(1, "", ""))
    check("neither tool answering is unknown", r["status"] == H.UNKNOWN, r["summary"])
    r = run(lsof=P.Unavailable("lsof timed out after 15s"))
    check("a timeout is unknown", r["status"] == H.UNKNOWN and "timed out" in r["summary"])


def _healthcheck_home(log_text):
    R = _recorded()
    d, rec = _trader_tree()
    log = d / "paper_health.log"
    log.write_text(log_text)
    agents = d / "home" / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.traderjoe.paperhealth.plist").write_text(
        R["paperhealth_plist"].replace("/Users/owner/workspace/trader/data/paper_health.log",
                                       str(log)))
    return d, rec


def test_probe_trader_healthcheck_reads_its_log_never_runs_it():
    P = _probes()
    import health as H
    print("\nhealthcheck: the latest run of paper_healthcheck.py, from its log")
    R = _recorded()
    cfg = {"trader": {"project": "trader", "healthcheck_label": "com.traderjoe.paperhealth"}}
    # The recorded run: Tuesday 2026-10-20 13:15 local; next slot Wednesday 06:15.
    tue = datetime(2026, 10, 20, 14, 0).timestamp()

    def run(text, now=tue):
        d, rec = _healthcheck_home(text)
        s = _probe_sys(home=d / "home", now=now)
        return _run_probe(P.make_healthcheck(s, cfg), rec), s

    r, s = run(R["paper_health_log"])
    check("the recorded run fails, naming its failing checks", r["status"] == H.FAIL
          and "universe, bars" in r["summary"], r["summary"])
    check("its evidence is the healthcheck's own messages",
          any(e.startswith("FAIL universe: STALE") for e in r["evidence"]), r["evidence"])
    check("it ran nothing", not [a for a, _ in s.calls if Path(a[0]).name != "git"], s.calls)
    r2, _ = run(R["paper_health_log"].replace("(140d ago)", "(141d ago)"))
    check("the same failure a day older is the same fingerprint",
          r2["fingerprint"] == r["fingerprint"] and r["fingerprint"].startswith("bars,universe:"),
          f'{r["fingerprint"]} {r2["fingerprint"]}')
    r3, _ = run(R["paper_health_log"].replace("STALE: static universe", "MISSING: static universe"))
    check("a different failure of the same check is a different fingerprint",
          r3["status"] == H.FAIL and r3["fingerprint"] != r["fingerprint"])
    healthy = R["paper_health_log"].replace("[FAIL]", "[OK]")
    r, _ = run(healthy)
    check("a run with every check OK is ok", r["status"] == H.OK and "OK" in r["summary"],
          r["summary"])
    thu = datetime(2026, 10, 22, 9, 0).timestamp()
    r, _ = run(healthy, now=thu)
    check("a healthcheck that stopped running (missed Wed and Thu) warns",
          r["status"] == H.WARN and "has not run since" in r["summary"], r["summary"])
    sat = datetime(2026, 10, 24, 12, 0).timestamp()
    r, _ = run(healthy.replace("2026-10-20T13:15:05", "2026-10-23T13:15:05"), now=sat)
    check("a weekend with Friday's run is not stale", r["status"] == H.OK, r["summary"])
    r, _ = run("Traceback (most recent call last):\n  ...\n")
    check("a log with no completed run is unknown", r["status"] == H.UNKNOWN)
    d, rec = _trader_tree()
    r = _run_probe(P.make_healthcheck(_probe_sys(home=d / "home"), cfg), rec)
    check("no plist anywhere is unknown", r["status"] == H.UNKNOWN, r["summary"])


def _drift_repo(commits):
    """A scratch repo: commits = [(epoch, {path: text})], on main."""
    d = Path(tempfile.mkdtemp()) / "repo"
    d.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True, env=env)
    for at, files in commits:
        for path, text in files.items():
            (d / path).parent.mkdir(parents=True, exist_ok=True)
            (d / path).write_text(text)
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True, env=env)
        stamp = f"@{int(at)} +0000"
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "c"], check=True,
                       env={**env, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})
    return d


def _supabase_home(token="sbp_" + "a1b2c3d4" * 5, account="personal", ref="refabc"):
    d = _probe_home()
    sb = d / "home" / ".supabase"
    (sb / "projects").mkdir(parents=True)
    (sb / "accounts").mkdir()
    (sb / "projects" / f"{ref}.env").write_text(
        (f"SUPABASE_ACCOUNT={account}\n" if account else "")
        + "SUPABASE_DB_PASSWORD=dbpassSECRET123\n")
    if token is not None:
        (sb / "accounts" / f"{account or 'personal'}.env").write_text(
            f"SUPABASE_ACCESS_TOKEN={token}\n")
    return d


def _deployed(slug, version, at):
    R = _recorded()
    f = dict(R["cadence_functions"][0])
    f.update(slug=slug, name=slug, version=version, updated_at=int(at * 1000))
    return f


def test_probe_backend_drift_compares_production_with_the_base_branch():
    P = _probes()
    import health as H
    print("\nbackend-drift: deployed functions and applied migrations against main")
    R = _recorded()
    day = 86400
    now = _PROBE_NOW
    cfg = {"supabase": {"cadence": {"ref": "refabc", "dir": "supabase"}}}
    base_files = {"supabase/functions/alpha/index.ts": "a1",
                  "supabase/functions/_shared/util.ts": "s1",
                  "supabase/functions/import_map.json": "{}",
                  "supabase/migrations/20261001000001_first.sql": "select 1;"}
    applied = [{"version": "20261001000001", "name": "first"}]

    def run(repo, http, home=None, now=now):
        rec = {"slug": "cadence", "scope": {"cwd": str(repo)}}
        s = _probe_sys(home=(home or _supabase_home()) / "home", now=now, http=http)
        return _run_probe(P.make_backend_drift(s, cfg), rec), s

    def api(funcs, migs=applied):
        return {"/functions": (200, json.dumps(funcs)),
                "/database/migrations": (200, json.dumps(migs))}

    repo = _drift_repo([(now - 20 * day, base_files)])
    r, s = run(repo, api([_deployed("alpha", 3, now - 19 * day)]))
    check("deployed after main's last change, every migration applied: ok",
          r["status"] == H.OK and "1 function(s) and 1 migration(s) match main" in r["summary"],
          r["summary"])
    check("read-only: GETs only, against the project's own ref",
          all(a[0].startswith("https://api.supabase.com/v1/projects/refabc/")
              for a, _ in s.calls if a[0].startswith("http")))
    check("each call bounded", all(t <= 30 for _, t in s.calls))
    check("the real response shape parsed (recorded cadence functions)",
          isinstance(R["cadence_functions"], list) and "updated_at" in R["cadence_functions"][0])

    repo = _drift_repo([(now - 20 * day, base_files),
                        (now - 2 * day, {"supabase/functions/_shared/util.ts": "s2"})])
    r, _ = run(repo, api([_deployed("alpha", 3, now - 19 * day)]))
    check("shared code changed after the deploy: that function drifted, warn while young",
          r["status"] == H.WARN and "alpha" in r["summary"] and "oldest 2d" in r["summary"],
          r["summary"])
    repo = _drift_repo([(now - 20 * day, base_files),
                        (now - 8 * day, {"supabase/functions/alpha/index.ts": "a2",
                                         "supabase/migrations/20261012000001_second.sql": "x"})])
    r, _ = run(repo, api([_deployed("alpha", 3, now - 19 * day)]))
    check(f"drift older than {P.DRIFT_FAIL_D}d fails, naming function and migration",
          r["status"] == H.FAIL and "alpha" in r["summary"] and "20261012000001" in r["summary"]
          and "stale:alpha:3" in r["fingerprint"] and "mig:20261012000001" in r["fingerprint"],
          r["summary"])
    repo = _drift_repo([(now - 20 * day, base_files),
                        (now - day, {"supabase/functions/beta/index.ts": "b"})])
    r, _ = run(repo, api([_deployed("alpha", 3, now - 19 * day)]))
    check("a function on main never deployed is drift", r["status"] == H.WARN
          and any("beta" in e and "never deployed" in e for e in r["evidence"]), r["evidence"])
    repo = _drift_repo([(now - 20 * day, base_files)])
    r, _ = run(repo, api([_deployed("alpha", 3, now - 19 * day), _deployed("gamma", 1, now)],
                         applied + [{"version": "20261019000001", "name": "elsewhere"}]))
    check("production running things main lacks warns", r["status"] == H.WARN
          and any("gamma" in e for e in r["evidence"])
          and any("20261019000001" in e for e in r["evidence"]), r["evidence"])
    r, _ = run(repo, api([_deployed("alpha", 3, now - 19 * day)], []))
    check("no migration history in production is unknown, not 'nothing applied'",
          r["status"] == H.UNKNOWN and "no migration history" in r["summary"], r["summary"])

    tok = "sbp_" + "a1b2c3d4" * 5
    refused = {"/functions": (401, json.dumps({"message": "JWT could not be decoded"})),
               "/database/migrations": (401, "{}")}
    r, _ = run(repo, refused)
    check("a refused management token is unknown, with the reason (the Mondays case)",
          r["status"] == H.UNKNOWN and "refused account personal's management token" in r["summary"]
          and "JWT could not be decoded" in r["summary"], r["summary"])
    forbidden = {"/functions": (403, '{"message":"Your account does not have the necessary '
                                     'privileges to access this endpoint."}')}
    r, _ = run(repo, forbidden)
    check("a token for another account (403, recorded for Saga) is unknown",
          r["status"] == H.UNKNOWN and "HTTP 403" in r["summary"])
    r, _ = run(repo, {"/functions": P.Unavailable("api.supabase.com unreachable: timed out")})
    check("a timeout is unknown", r["status"] == H.UNKNOWN and "timed out" in r["summary"])
    r, _ = run(repo, api([]), home=_supabase_home(account=""))
    check("a project file naming no account is unknown, saying which file",
          r["status"] == H.UNKNOWN and "SUPABASE_ACCOUNT" in r["summary"]
          and "refabc.env" in r["summary"], r["summary"])
    r, _ = run(repo, api([]), home=_supabase_home(token="sb_secret_" + "z" * 30))
    check("a project secret key where the management token goes is unknown",
          r["status"] == H.UNKNOWN and "not a management token" in r["summary"], r["summary"])
    r, _ = run(repo, api([]), home=_supabase_home(ref="other"))
    check("no credentials for the project is unknown", r["status"] == H.UNKNOWN
          and "no credentials" in r["summary"], r["summary"])
    for res in (r,):
        check("and no token value appears", tok not in json.dumps(res))

    # Landing rebases: the committer time becomes the landing, after a deploy
    # made from the task's worktree. Author time is when the change was made.
    repo = _drift_repo([(now - 20 * day, base_files)])
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_AUTHOR_DATE": f"@{int(now - 3 * day)} +0000",
           "GIT_COMMITTER_DATE": f"@{int(now - day)} +0000"}
    (repo / "supabase/functions/alpha/index.ts").write_text("a2")
    (repo / "supabase/functions/README.md").write_text("notes")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "x"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "readme"], check=True,
                   env={**env, "GIT_AUTHOR_DATE": f"@{int(now - 3600)} +0000"})
    r, _ = run(repo, api([_deployed("alpha", 4, now - 2 * day)]))
    check("deployed from the branch before it landed is current (author time, not landing)",
          r["status"] == H.OK, r["summary"])
    check("and a README beside the functions is not shared code", r["status"] == H.OK)
    # origin ahead of local main (merged on the forge): origin is the base.
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", "HEAD"],
                   check=True)
    (repo / "supabase/functions/alpha/index.ts").write_text("a3")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "forge"], check=True,
                   env={**env, "GIT_AUTHOR_DATE": f"@{int(now - 3600)} +0000"})
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", "HEAD"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "reset", "-q", "--hard", "HEAD~1"], check=True)
    r, _ = run(repo, api([_deployed("alpha", 4, now - 2 * day)]))
    check("a base whose origin copy is ahead is read from origin", r["status"] == H.WARN
          and "alpha" in r["summary"], r["summary"])


def test_probe_backend_drift_measures_a_release_by_ancestry():
    P = _probes()
    import health as H
    print("\nbackend-drift: a deploy a release explains is measured by what landed since its tag")
    day = 86400
    now = _PROBE_NOW
    cfg = {"supabase": {"cadence": {"ref": "refabc", "dir": "supabase"}}}
    base_files = {"supabase/functions/alpha/index.ts": "a1",
                  "supabase/functions/_shared/util.ts": "s1",
                  "supabase/migrations/20261001000001_first.sql": "select 1;",
                  ".silkworm/release.toml": '[targets.backend]\npaths = ["supabase/"]\n'
                                            'ship = "command"\ncommands = ["true"]\n\n'
                                            '[targets.ios]\npaths = ["ios/"]\nship = "tag"\n'}
    applied = [{"version": "20261001000001", "name": "first"}]
    api = lambda funcs: {"/functions": (200, json.dumps(funcs)),       # noqa: E731
                         "/database/migrations": (200, json.dumps(applied))}
    ident = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(repo, *args, authored=None, committed=None):
        env = {**os.environ, **ident}
        if authored is not None:
            env["GIT_AUTHOR_DATE"] = f"@{int(authored)} +0000"
        if committed is not None:
            env["GIT_COMMITTER_DATE"] = f"@{int(committed)} +0000"
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env)

    def land(repo, path, text, authored, landed):
        # A rebase-landing: authored when written, committed when it landed.
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "land", authored=authored, committed=landed)

    def release(repo, tag, at):
        git(repo, "tag", "-a", tag, "-m", tag, committed=at)       # tagger date = at

    def run(repo, funcs):
        rec = {"slug": "cadence", "scope": {"cwd": str(repo)}}
        s = _probe_sys(home=_supabase_home() / "home", now=now, http=api(funcs))
        return _run_probe(P.make_backend_drift(s, cfg), rec)

    # (a) Authored day -5, waiting in review; a sibling's !release redeploys
    # alpha on day -4 from a tree without it; it lands day -3. Author time is
    # older than the deploy, so author time alone reads this as current.
    repo = _drift_repo([(now - 20 * day, base_files)])
    release(repo, "backend/v1.0.0", now - 20 * day)
    land(repo, "supabase/functions/beta/index.ts", "b", now - 4 * day - 600, now - 4 * day - 600)
    release(repo, "backend/v1.0.1", now - 4 * day + 60)
    land(repo, "supabase/functions/alpha/index.ts", "a2", now - 5 * day, now - 3 * day)
    r = run(repo, [_deployed("alpha", 5, now - 4 * day), _deployed("beta", 1, now - 4 * day)])
    check("authored before a release's redeploy, landed after it: drift",
          r["status"] == H.WARN and "alpha" in r["summary"] and "beta" not in r["summary"]
          and "stale:alpha:5" == r["fingerprint"], r["summary"])
    check("the line names the release and when the change landed",
          any("alpha" in e and "backend/v1.0.1" in e and "1 commit(s)" in e
              and P._iso(now - 3 * day) in e for e in r["evidence"]), r["evidence"])
    check("aged from the landing, not the authoring", "oldest 3d" in r["summary"], r["summary"])
    land(repo, "supabase/functions/_shared/util.ts", "s2", now - 6 * day, now - 2 * day)
    r = run(repo, [_deployed("alpha", 5, now - 4 * day), _deployed("beta", 1, now - 4 * day)])
    check("shared code landed after the release drifts every function built with it",
          r["status"] == H.WARN and "alpha" in r["fingerprint"] and "beta" in r["fingerprint"],
          r["summary"])

    repo = _drift_repo([(now - 20 * day, base_files)])
    land(repo, "supabase/functions/alpha/index.ts", "a2", now - 5 * day, now - 4 * day - 900)
    release(repo, "backend/v1.0.0", now - 4 * day + 60)
    land(repo, "ios/App.swift", "x", now - day, now - day)
    r = run(repo, [_deployed("alpha", 5, now - 4 * day)])
    check("nothing of the function's landed since its release: current, naming the tag",
          r["status"] == H.OK and any("as backend/v1.0.0" in e for e in r["evidence"]),
          r["evidence"])
    release(repo, "ios/v1.2.0", now - 3 * day)
    r = run(repo, [_deployed("alpha", 5, now - 3 * day)])
    check("another target's tag does not explain a backend deploy", r["status"] == H.OK
          and not any("ios/v1.2.0" in e for e in r["evidence"]), r["evidence"])

    # (b) Deployed by hand from the task's worktree on day -2, landed day -1:
    # no release was cut with that deploy, so author time still decides. The
    # older release tag says nothing about what this deploy contained.
    repo = _drift_repo([(now - 20 * day, base_files)])
    release(repo, "backend/v1.0.0", now - 10 * day)
    land(repo, "supabase/functions/alpha/index.ts", "a2", now - 3 * day, now - day)
    r = run(repo, [_deployed("alpha", 6, now - 2 * day)])
    check("deployed from the worktree before landing, an older release tag present: current",
          r["status"] == H.OK and any("current with main" in e and "as " not in e
                                      for e in r["evidence"]), r["evidence"])
    land(repo, "supabase/functions/alpha/index.ts", "a3", now - 3600, now - 3600)
    r = run(repo, [_deployed("alpha", 6, now - 2 * day)])
    check("and a change authored after that deploy still drifts by author time",
          r["status"] == H.WARN and "alpha" in r["summary"], r["summary"])
    # A release at 12:00, then a hand deploy from a worktree at 12:05: the tag
    # came before the deploy, so it did not make it.
    repo = _drift_repo([(now - 20 * day, base_files)])
    release(repo, "backend/v1.0.0", now - 2 * day - 300)
    land(repo, "supabase/functions/alpha/index.ts", "a2", now - 3 * day, now - day)
    r = run(repo, [_deployed("alpha", 6, now - 2 * day)])
    check("a release tag from just before a worktree deploy does not explain it: current",
          r["status"] == H.OK and not any("as backend" in e for e in r["evidence"]),
          r["evidence"])

    # A release whose later commands ran 20 minutes after this function
    # deployed: the tag is still that release's.
    repo = _drift_repo([(now - 20 * day, base_files)])
    release(repo, "backend/v1.0.0", now - 4 * day + 1200)
    land(repo, "supabase/functions/alpha/index.ts", "a2", now - 5 * day, now - 3 * day)
    r = run(repo, [_deployed("alpha", 5, now - 4 * day)])
    check("a release running past DRIFT_SLACK_S still explains its deploys: drift",
          r["status"] == H.WARN and any("as backend/v1.0.0" in e for e in r["evidence"]),
          r["summary"])

    # Merged on the forge with a merge commit: the branch's own commits keep
    # their committer time, from before the deploy; it landed at the merge.
    repo = _drift_repo([(now - 20 * day, base_files)])
    git(repo, "checkout", "-qb", "feature")
    land(repo, "supabase/functions/alpha/index.ts", "a2", now - 5 * day, now - 5 * day)
    git(repo, "checkout", "-q", "main")
    release(repo, "backend/v1.0.0", now - 4 * day + 60)
    git(repo, "merge", "-q", "--no-ff", "-m", "merge", "feature", committed=now - 3 * day)
    r = run(repo, [_deployed("alpha", 5, now - 4 * day)])
    check("a merge-commit landing is drift, aged from the merge",
          r["status"] == H.WARN and "oldest 3d" in r["summary"], r["summary"])

    broken = dict(base_files, **{".silkworm/release.toml": "[targets.backend\npaths = 1"})
    repo = _drift_repo([(now - 20 * day, broken)])
    release(repo, "backend/v1.0.0", now - 19 * day)
    r = run(repo, [_deployed("alpha", 3, now - 19 * day)])
    check("a release.toml that does not parse falls back to author time, not unknown",
          r["status"] == H.OK, r["summary"])
    odd = dict(base_files, **{".silkworm/release.toml": 'targets = "x"\n'})
    repo = _drift_repo([(now - 20 * day, odd)])
    r = run(repo, [_deployed("alpha", 3, now - 19 * day)])
    check("nor does one whose targets are not tables", r["status"] == H.OK, r["summary"])


def test_probe_backend_drift_finds_production_schema_no_migration_creates():
    P = _probes()
    import health as H
    print("\nbackend-drift: production schema no base-branch migration creates (the Mondays case)")
    R = _recorded()
    day = 86400
    now = _PROBE_NOW
    cfg = {"supabase": {"mondays": {"ref": "refabc", "dir": "backend/supabase"}}}
    mig = "backend/supabase/migrations/"
    # Mondays on 2026-11-17: main's numbering jumps 0023 -> 0027; feat/premium's
    # 0024-0026 were applied by hand and the history baselined from main.
    files = {f"{mig}{n:04d}_m{n}.sql": "select 1;" for n in range(1, 23)}
    files.update({
        f"{mig}0001_m1.sql": "create table profiles (\n  id uuid primary key,\n"
                             "  display_name text not null -- , subscription_status text\n);\n"
                             "create table if not exists public.groups (id uuid, title text);\n"
                             "create or replace view public.member_view as select 1;\n",
        f"{mig}0002_m2.sql": "alter table profiles add column if not exists pro_until "
                             "timestamptz;\ncreate or replace function public.add_entry("
                             "p uuid) returns uuid language sql as $$ select p $$;\n"
                             "/* create function my_entitlement() later */\n",
        f"{mig}0023_add_entry.sql": "alter table groups rename column title to name;\n",
        f"{mig}0027_account_deletion.sql": "create function delete_account() returns void "
                                           "language sql as $$ select $$;\n",
        "backend/supabase/functions/alpha/index.ts": "a1",
    })
    applied = [{"version": f"{n:04d}"} for n in list(range(1, 24)) + [27]]
    premium = _types(
        tables={"profiles": ["id", "display_name", "pro_until", "subscription_status",
                             "subscription_expires_at"],
                "groups": ["id", "name"]},
        views={"member_view": ["x"]},
        functions=["add_entry", "apply_subscription", "delete_account", "my_entitlement",
                   "premium_members"])

    def run(repo, types, migs=applied):
        rec = {"slug": "mondays", "scope": {"cwd": str(repo)}}
        http = {"/functions": (200, json.dumps([_deployed("alpha", 3, now - 19 * day)])),
                "/database/migrations": (200, json.dumps(migs))}
        if types is not None:
            http["/types/typescript?included_schemas=public"] = types
        s = _probe_sys(home=_supabase_home() / "home", now=now, http=http)
        return _run_probe(P.make_backend_drift(s, cfg), rec), s

    repo = _drift_repo([(now - 20 * day, files)])
    r, s = run(repo, (200, premium))
    ev = r["evidence"]
    check("every migration applied, yet production has objects main never creates: warn",
          r["status"] == H.WARN and "not on main" in r["summary"], r["summary"])
    for fn in ("apply_subscription", "my_entitlement", "premium_members"):
        check(f"names function {fn}", any(e.startswith(f"function {fn}: in production, no "
                                                       "migration on main creates it")
                                          for e in ev), ev)
    check("names the unrecorded columns of profiles, in one line",
          [e for e in ev if e.startswith("columns profiles.subscription_status, "
                                         "subscription_expires_at:")] != [], ev)
    check("names the numbering gap 0024–0026, which production's history also skips",
          any(e.startswith("migration 0024–0026: main skips these numbers; production's "
                           "history skips them too") for e in ev), ev)
    check("what a migration creates is not reported: create [or replace] [public.] "
          "function, if not exists, add column, rename column, views",
          not any(w in e for e in ev if "creates" in e
                  for w in ("add_entry", "delete_account", "groups", "pro_until",
                            "display_name", "member_view", "alpha"))
          and len([e for e in ev if "creates" in e]) == 4, ev)
    check("a comment is not a migration (subscription_status, my_entitlement)",
          any("subscription_status" in e for e in ev)
          and any(e.startswith("function my_entitlement") for e in ev))
    check("still read-only: GETs only, the schema through the types endpoint",
          [a[0] for a, _ in s.calls if a[0].startswith("http")][-1].endswith(
              "/v1/projects/refabc/types/typescript?included_schemas=public")
          and all(a[0].startswith("https://api.supabase.com/v1/projects/refabc/")
                  for a, _ in s.calls if a[0].startswith("http")))
    check("each call bounded", all(t <= 30 for _, t in s.calls))
    check("and no token value appears", "sbp_" not in json.dumps(r))

    # The premium backend lands on main: nothing unrecorded, gap filled.
    landed = dict(files, **{
        f"{mig}0024_subscriptions.sql": "alter table profiles\n  add column subscription_status "
                                        "text,\n  add column subscription_expires_at timestamptz;\n",
        f"{mig}0025_subscription_apply.sql": "create or replace function apply_subscription("
                                             ") returns void as $$ $$ language sql;\n"
                                             "create or replace function my_entitlement() "
                                             "returns void as $$ $$ language sql;\n",
        f"{mig}0026_subscription_order.sql": "create function premium_members(p uuid[]) "
                                             "returns void as $$ $$ language sql;\n"})
    repo = _drift_repo([(now - 20 * day, landed)])
    full = [{"version": f"{n:04d}"} for n in range(1, 28)]
    r, _ = run(repo, (200, premium), full)
    check("once the premium backend is on main and recorded: ok, saying the schema matched",
          r["status"] == H.OK and any("public schema (2 table(s), 1 view(s), 5 function(s)) is "
                                      "all created by main's migrations" in e
                                      for e in r["evidence"]), (r["summary"], r["evidence"]))

    # A gap production's history fills (recorded but not on main) says so.
    repo = _drift_repo([(now - 20 * day, files)])
    r, _ = run(repo, (200, premium), applied + [{"version": "0025"}])
    check("a gap production's history partly fills says so",
          any(e.startswith("migration 0024–0026: main skips these numbers; production's "
                           "history has some of them") for e in r["evidence"])
          and any(e.startswith("migration 0025: applied in production") for e in r["evidence"]),
          r["evidence"])

    # The schema cannot be read: the rest of the check stands, saying why.
    for label, ans in (("refused", (403, '{"message":"no"}')), ("missing", None),
                       ("not types", (200, '{"x": 1}')),
                       ("no public schema", (200, json.dumps({"types": "export {}\n"}))),
                       ("unreachable", P.Unavailable("api.supabase.com unreachable: timed out"))):
        r, _ = run(repo, ans)
        check(f"schema {label}: still warns on the gap, evidence says the schema was not read",
              r["status"] == H.WARN and any("0024–0026" in e for e in r["evidence"])
              and any(e.startswith("production schema not compared with main's migrations")
                      for e in r["evidence"])
              and not any("no migration on main creates" in e for e in r["evidence"]),
              (r["summary"], r["evidence"]))

    # Timestamped versions have no numbering to skip.
    check("timestamped versions have no gaps",
          P.numbering_gaps(["20261001000001", "20261019000001"]) == [])
    check("sequential gaps, zero-padded, each run once",
          P.numbering_gaps(["0001", "0002", "0005", "0007"]) == [("0003", "0004"),
                                                                 ("0006", "0006")])
    check("a single gap reads as one number",
          any(e.startswith("migration 0024: main skips this number")
              for e in run(_drift_repo([(now - 20 * day, {
                  k: v for k, v in landed.items() if "0024_" not in k})]),
                  (200, _types(functions=["add_entry"])), full)[0]["evidence"]))

    # Forms a migration creates things in, which must not read as unrecorded.
    U, C = P.unrecorded_objects, P._sql_code
    some = {"tables": {"profiles": ["id", "tier"]}, "views": {},
            "functions": ["apply_subscription"]}
    check("supabase db diff's quoted \"public\".\"x\" counts as created",
          U(some, C('create table "public"."profiles" ("id" uuid, "tier" text);\n'
                    'create or replace function "public"."apply_subscription"() returns void '
                    "as $$ $$ language sql;")) == [])
    check("alter table … add <col> without COLUMN counts",
          U(some, C("create table profiles (id uuid);\nalter table profiles add tier text;\n"
                    "create function apply_subscription() returns void as $$ $$;")) == [])
    check("a /* or -- inside a string or a $$ body is not a comment",
          U(some, C("select '/*';\ncreate table profiles (id uuid, tier text);\n"
                    "create function apply_subscription() returns void as $$ select 1 -- x\n"
                    "$$;\nselect '*/';")) == [])
    check("and a real comment still is",
          U(some, C("create table profiles (id uuid);\n-- alter table profiles add tier\n"
                    "/* create function apply_subscription() */")) ==
          ["function apply_subscription", "column profiles.tier"])
    check("an apostrophe in a $$ body does not hide the comment after it",
          U(some, C("create table profiles (id uuid, tier text);\n"
                    "create function g() returns text as $body$ select 'it''s' || $$don't$$ "
                    "$body$;\n-- create function apply_subscription()\nselect 'x';")) ==
          ["function apply_subscription"])
    odd = ("  public: {\n    Tables: {\n      profiles: {\n        Row: {\n          id: string\n"
           "        }\n      }\n      \"2fa-codes\": {\n        Row: {\n          code: string\n"
           "        }\n      }\n    }\n    Functions: {\n      grant_pro:\n"
           "        | { Args: never; Returns: undefined }\n"
           "        | { Args: { p: string }; Returns: undefined }\n    }\n  }\n")
    got = P.schema_objects(odd)
    check("an overloaded function (a union on the next lines) is read",
          got["functions"] == ["grant_pro"], got)
    check("a table name this cannot read does not lend its columns to the one before",
          got["tables"] == {"profiles": ["id"]}, got)
    two = dict(files, **{f"{mig}0027_b.sql": "create function premium_members() returns void "
                                             "as $$ $$;\n"})
    r, _ = run(_drift_repo([(now - 20 * day, two)]), (200, premium))
    check("two files sharing a version number are both read",
          not any(e.startswith("function premium_members") for e in r["evidence"])
          and any(e.startswith("function my_entitlement") for e in r["evidence"]),
          r["evidence"])

    # The real answer, recorded from Mondays on 2026-11-17.
    real = P.schema_objects(R["mondays_types"]["types"])
    check("the recorded Mondays schema parses: tables, their Row columns, functions",
          "profiles" in real["tables"] and "subscription_status" in real["tables"]["profiles"]
          and {"apply_subscription", "my_entitlement", "premium_members", "add_entry"}
          <= set(real["functions"]) and "issue_state" not in real["functions"]
          and not real["views"], real["functions"])
    check("a column is read from Row only, not Insert or Update",
          all(len(c) == len(set(c)) for c in real["tables"].values()))


def test_probe_mini_credentials_and_disk():
    P = _probes()
    import health as H
    print("\ncredentials and disk: the mini's own checks")
    R = _recorded()
    client = Path(tempfile.mkdtemp()) / "asc.mjs"
    client.write_text("// stand-in")
    cfg = {"asc": {"client": str(client)}, "mini": {"disk": "/x"}}
    asc = {"/v1/certificates": _asc_ok(R["certificates"]), "/v1/profiles": _asc_ok(R["profiles"])}
    token = lambda has: {"mode": "token", "stores": []}            # noqa: E731
    soonest = min(P._parse_time(c["attributes"]["expirationDate"])
                  for c in json.loads(R["certificates"].partition("\n")[2])["data"])

    def run(state=token, asc=asc, now=_PROBE_NOW):
        s = _probe_sys(home=_probe_home(), now=now, asc=asc)
        return _run_probe(P.make_credentials(s, cfg, claude_state=state), MINI)

    MINI = P.MINI
    r = run()
    check("a long-lived claude token and certificates good for months is ok",
          r["status"] == H.OK, r["summary"])
    r = run(now=soonest - 10 * 86400)
    check("a certificate expiring in 10 days warns, naming it", r["status"] == H.WARN
          and "Apple Development: Ryan Sharp" in r["summary"], r["summary"])
    r = run(now=soonest - 2 * 86400)
    check("inside 3 days fails", r["status"] == H.FAIL and r["fingerprint"], r["summary"])
    invalid = dict(asc, **{"/v1/profiles": _asc_mut(R["profiles"], lambda d: d["data"][0]
                                                    ["attributes"].update(profileState="INVALID"))})
    r = run(asc=invalid)
    check("an invalidated profile warns", r["status"] == H.WARN and "INVALID" in r["summary"])
    oauth = lambda h: lambda has: {"mode": "oauth", "stores": ["file"], "source": "file",  # noqa: E731
                                   "expires_at": _PROBE_NOW + h * 3600, "hours_left": h}
    check("claude's refresh token: 8 days left warns, 2 fails, a month is ok",
          run(state=oauth(8 * 24))["status"] == H.WARN
          and run(state=oauth(48))["status"] == H.FAIL
          and run(state=oauth(30 * 24))["status"] == H.OK)
    r = run(state=lambda has: {"mode": "missing", "stores": []})
    check("claude credentials missing fails", r["status"] == H.FAIL)
    r = run(state=lambda has: {"mode": "unreadable", "stores": ["keychain"],
                               "source": "keychain"})
    check("a Keychain copy this process cannot read is unknown (security timing out, rc 36)",
          r["status"] == H.UNKNOWN, r["summary"])
    r = run(state=lambda has: {"mode": "unreadable", "stores": ["file"], "source": "file"})
    check("a credentials file that will not parse fails", r["status"] == H.FAIL)
    exp = min(P._parse_time(c["attributes"]["expirationDate"])
              for c in json.loads(R["profiles"].partition("\n")[2])["data"])
    certs_far = _asc_mut(R["certificates"], lambda d: [c["attributes"].update(
        expirationDate="2030-01-01T00:00:00.000+00:00") for c in d["data"]])
    r = run(asc=dict(asc, **{"/v1/certificates": certs_far}), now=exp - 86400)
    check("a profile a day from expiry warns, never fails (Xcode renews it)",
          r["status"] == H.WARN, r["summary"])
    r = run(asc={"/v1/certificates": _ASC_401})
    check("Apple unreadable (key refused) is unknown, never fail", r["status"] == H.UNKNOWN,
          r["summary"])
    r = run(asc={"/v1/certificates": P.Unavailable("node timed out after 30s")})
    check("an Apple timeout is unknown", r["status"] == H.UNKNOWN)
    check("credentials.state is what it reuses", "credentials.state(" in
          (BASE / "health_probes.py").read_text())

    Usage = types.SimpleNamespace

    def disk(free, total=1000):
        s = _probe_sys(home=_probe_home(), disk=Usage(free=free, total=total, used=total - free))
        return _run_probe(P.make_disk(s, cfg), MINI)
    check("disk: 42% free ok, 10% warn, 4% fail",
          disk(420)["status"] == H.OK and disk(100)["status"] == H.WARN
          and disk(40)["status"] == H.FAIL)
    check("disk unreadable is unknown",
          _run_probe(P.make_disk(_probe_sys(home=_probe_home(), disk=OSError("x")), cfg),
                     MINI)["status"] == H.UNKNOWN)


def test_probe_results_never_carry_a_secret():
    P = _probes()
    import health as H
    print("\nhealth probes: no secret in any summary, evidence line, link, fingerprint or log")
    R = _recorded()
    secrets = {"sbp": "sbp_" + "f00dfeed" * 5, "jwt": ".".join(("eyJ" + "hbGciOiJFUzI1NiJ9", "eyJ" + "pc3MiOiJ4In0", "c2lnbmF0dXJlLXNpZw")),
               "anth": "sk-ant-oat01-" + "Q" * 30, "dbpass": "dbpassSECRET123",
               "sbsecret": "sb_secret_" + "k" * 24}
    home = _supabase_home(token=secrets["sbp"])
    repo = _drift_repo([(_PROBE_NOW - 10 * 86400,
                         {"supabase/functions/alpha/index.ts": "a",
                          "supabase/migrations/20261001000001_first.sql": "x"})])
    echo = (f'Invalid token {secrets["sbp"]} (Bearer {secrets["jwt"]}) '
            f'key {secrets["sbsecret"]} {secrets["anth"]}')
    client = Path(tempfile.mkdtemp()) / "asc.mjs"
    client.write_text("//")
    agents = home / "home" / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    log = home / "paper_health.log"
    log.write_text("paper healthcheck 2026-10-20T13:15:05\n"
                   f"  [FAIL] webhook: ALERT_WEBHOOK_URL rejected {secrets['anth']}\n")
    (agents / "com.traderjoe.paperhealth.plist").write_text(
        R["paperhealth_plist"].replace("/Users/owner/workspace/trader/data/paper_health.log",
                                       str(log)))
    trader = home / "trader"
    (trader / ".venv" / "bin").mkdir(parents=True)
    (trader / ".venv" / "bin" / "python3").write_text("")
    cfg = {"trader": {"project": "trader"}, "apps": {"fathom": "1"},
           "supabase": {"cadence": {"ref": "refabc", "dir": "supabase"}},
           "asc": {"client": str(client)}, "mini": {"disk": "/x"}}
    leaky_asc = (0, "HTTP 401 Unauthorized\n" + json.dumps(
        {"errors": [{"detail": echo}]}), "")
    scenarios = [
        dict(http={"/functions": (401, json.dumps({"message": echo}))}),
        dict(http={"/functions": (500, json.dumps({"message": echo}))}),
        dict(http={"/functions": P.Unavailable(echo)}),
        dict(asc={"appStoreVersions": leaky_asc, "/v1/builds": leaky_asc,
                  "/v1/certificates": leaky_asc}),
        dict(asc={"appStoreVersions": (1, "", echo), "/v1/builds": (1, echo, ""),
                  "/v1/certificates": (1, "", echo)}),
        dict(trader=(1, "", f"KeyError: {echo}"), lsof=(1, "", echo)),
        dict(trader=_trader_out(verdict=[False, echo], daemon=[True, f"PID=1 {echo}"]),
             lsof=(1, "", ""), netstat=(1, "", echo)),
    ]
    records, logs = [], []
    handler = logging.Handler()
    handler.emit = lambda rec: logs.append(rec.getMessage())
    logging.getLogger("silkworm.health").addHandler(handler)
    try:
        for kw in scenarios:
            s = _probe_sys(home=home / "home", disk=types.SimpleNamespace(free=1, total=100),
                           **kw)
            reg = P.register_all(s, cfg, registry={})
            for rec in ({"slug": "trader", "scope": {"cwd": str(trader)}},
                        {"slug": "fathom", "scope": {}},
                        {"slug": "cadence", "scope": {"cwd": str(repo)}}, dict(P.MINI)):
                for check_ in H.checks_for(rec, reg):
                    records.append(H.run_check(check_, rec, None))
    finally:
        logging.getLogger("silkworm.health").removeHandler(handler)
    blob = json.dumps(records) + "\n".join(logs)
    leaked = [k for k, v in secrets.items() if v in blob]
    check(f"{len(records)} results across every probe, none carrying a secret", not leaked
          and len(records) >= 40, f"leaked: {leaked}")
    check("and the scrubbing is on every registered probe",
          all(getattr(c.probe, "__wrapped__", None) for c in P.register_all(
              registry={}).values()))
    boom = P.scrubbed(lambda rec, gh: (_ for _ in ()).throw(
        RuntimeError(f"failed with {secrets['sbp']}")))
    out = H.run_check(H.Check("boom", boom), {"slug": "x"}, None)
    check("a probe that raises is unknown, and its message scrubbed too",
          out["status"] == H.UNKNOWN and secrets["sbp"] not in json.dumps(out), out["summary"])
    check("scrub keeps ordinary text", P.scrub("1.0 waiting for review for 11d") ==
          "1.0 waiting for review for 11d")
    check("and removes a credential it was handed, whatever its shape",
          P.scrub("token plainSECRETvalue42 refused", known=["plainSECRETvalue42"])
          == "token [redacted] refused")


def test_probe_mini_is_checked_and_the_cli_can_probe_now():
    P = _probes()
    import health as H
    print("\nhealth probes: the bot's pass includes the mini; `silkworm health --probe`")
    calls = {}
    fake_h = types.SimpleNamespace(
        run_all=lambda store, ts, recs, gh, **kw: calls.setdefault("recs", list(recs)) and [],
        gh_cli=None, OK="ok")
    ns = lift("run_health", HEALTH=object(), task_store=None, health=fake_h,
                       health_probes=P, project_store=types.SimpleNamespace(
                           all=lambda include_archived=False: [{"slug": "cadence"}],
                           scope_for=lambda s: {}),
                       announce_health=lambda *a: None)
    ns["run_health"]()
    check("the bot's pass runs over every project and the mini",
          [r["slug"] for r in calls.get("recs") or ()] == ["cadence", "mini"], calls)
    check("bot.py imports the probes, so they are registered",
          "import health_probes" in (BASE / "bot.py").read_text())
    check("projects_with_mini never doubles it",
          [r["slug"] for r in P.projects_with_mini([{"slug": "mini"}, {"slug": "a"}])]
          == ["mini", "a"]
          and [r["slug"] for r in P.projects_with_mini([{"slug": "a"}])] == ["a", "mini"])
    reg = {"disk": H.Check("disk", lambda rec, gh: H.result(H.WARN, "disk 12% free"),
                           applies=lambda rec: rec["slug"] == "mini"),
           "x": H.Check("x", lambda rec, gh: H.result(H.OK, "fine"),
                        applies=lambda rec: rec["slug"] == "a")}
    cli = _load_cli()
    old = os.environ.get("SILKWORM_TASK_ROLE")
    try:
        os.environ["SILKWORM_TASK_ROLE"] = "operator"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            refused = cli.probe_now("", recs=[{"slug": "a"}], registry=reg)
        check("--probe is refused to the operator (restricted, no network)",
              refused is None and "refused" in err.getvalue(), err.getvalue())
        os.environ.pop("SILKWORM_TASK_ROLE", None)
        old_role = os.environ.get("SILKWORM_ROLE")
        os.environ["SILKWORM_ROLE"] = "operator"
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                refused = cli.probe_now("", recs=[{"slug": "a"}], registry=reg)
        finally:
            if old_role is None:
                os.environ.pop("SILKWORM_ROLE", None)
            else:
                os.environ["SILKWORM_ROLE"] = old_role
        check("and to an operator turn known only by SILKWORM_ROLE", refused is None)
        os.environ["SILKWORM_TASK_ROLE"] = "implementor"
        rows = cli.probe_now("", recs=[{"slug": "a"}, {"slug": "z", "archived": True}],
                             registry=reg)
        check("anyone else gets every applicable check, the mini included, archived skipped",
              [(r["project"], r["check"], r["status"]) for r in rows]
              == [("a", "x", "ok"), ("mini", "disk", "warn")], rows)
        rows = cli.probe_now("mini", recs=[{"slug": "a"}], registry=reg)
        check("--project narrows it", [r["project"] for r in rows] == ["mini"])
    finally:
        if old is None:
            os.environ.pop("SILKWORM_TASK_ROLE", None)
        else:
            os.environ["SILKWORM_TASK_ROLE"] = old
    src = (BASE / "bin" / "silkworm").read_text()
    seg = ast.get_source_segment(src, next(n for n in ast.parse(src).body
                                           if isinstance(n, ast.FunctionDef)
                                           and n.name == "probe_now"))
    check("probe_now records nothing and opens no item",
          "record(" not in seg and "surface(" not in seg and "run_all" not in seg
          and "_save" not in seg and "repair=False" in seg)


def _ts_fixture():
    return json.loads((BASE / "tests" / "fixtures" / "health" / "tailscale.json").read_text())


def _ts_world(status, serve):
    """A `tailscale` responder: `status` and `serve` are each a dict (printed
    as JSON, exit 0), an (rc, out, err) tuple, or an exception to raise."""
    def answer(part):
        if isinstance(part, Exception):
            raise part
        return part if isinstance(part, tuple) else (0, json.dumps(part), "")

    def respond(argv):
        return answer(serve if argv[1:3] == ["serve", "status"] else status)
    return respond


def test_probe_tailscale_reads_the_phone_dashboards_path():
    P = _probes()
    import health as H
    print("\ntailscale: the phone's way to the dashboard, from statuses recorded on the mini")
    F = _ts_fixture()
    MINI = P.MINI
    running, live = F["status_running"], F["serve_live"]
    dns = running["Self"]["DNSName"].rstrip(".")
    stale = next(iter(live["Web"])).rsplit(":", 1)[0]
    current = {"TCP": live["TCP"], "Web": {f"{dns}:443": next(iter(live["Web"].values()))}}

    def run(status, serve=current, find=lambda: "/opt/homebrew/bin/tailscale", env=None):
        s = _probe_sys(home=_probe_home(), tailscale=_ts_world(status, serve), env=env)
        # Scrubbed, as register_all registers every probe.
        res = _run_probe(P.scrubbed(P.make_tailscale(s, {}, find_bin=find)), MINI)
        return res, P._decide_tailscale(res), s

    check("the recording is the live one: the node is renamed, serve still has the old name",
          stale != dns and stale.startswith("old-mini.") and dns.startswith("mac-mini."),
          (stale, dns))
    r, d, s = run(running)
    check("Running, logged in, MagicDNS and HTTPS on, serve forwarding this name to :8790, "
          "Funnel off: ok", r["status"] == H.OK and f"https://{dns}/" in r["summary"], r)
    check("ok has no decision", d == "")
    check("it asks `status --json` and `serve status --json`, bounded",
          [c[0][1:] for c in s.calls] == [["status", "--json"], ["serve", "status", "--json"]]
          and all(0 < c[1] <= 15 for c in s.calls), s.calls)
    r, d, _ = run(running, env={"SILKWORM_VIZ_PORT": "9000"})
    check("the dashboard's port is the visualizer's own (SILKWORM_VIZ_PORT)",
          r["status"] == H.FAIL and "does not forward to the dashboard (:9000)" in r["summary"]
          and "also exposes http://127.0.0.1:8790" in r["summary"], r["summary"])

    r, d, _ = run(running, live)
    check("the mini as it was on 2026-10-25: serve under a name it no longer has fails",
          r["status"] == H.FAIL and stale in r["summary"] and f"now {dns}" in r["summary"],
          r["summary"])
    check("decide: run `silkworm tailscale on` on the mini, or rename it back",
          "silkworm tailscale on" in d and "rename" in d, d)

    r, d, _ = run(F["status_stopped"])
    check("stopped (logged in, disconnected) fails, saying so",
          r["status"] == H.FAIL and "stopped" in r["summary"] and r["fingerprint"] == "Stopped",
          r)
    check("decide: open the Tailscale app on the mini and connect",
          "open the Tailscale app and connect" in d and "on the mini" in d, d)

    r, d, _ = run(F["status_needs_login"], F["serve_empty"])
    check("logged out (NeedsLogin, recorded from a fresh tailscaled) fails",
          r["status"] == H.FAIL and "logged out (NeedsLogin)" in r["summary"], r["summary"])
    check("and its serve config, empty, is named too",
          "does not forward to the dashboard (:8790)" in r["summary"], r["summary"])
    check("decide: sign in, then `silkworm tailscale on`",
          "sign in" in d and "silkworm tailscale on" in d, d)

    r, d, _ = run(running, F["serve_empty"])
    check("serve config missing fails", r["status"] == H.FAIL
          and r["summary"] == "serve does not forward to the dashboard (:8790)", r["summary"])
    check("decide: `silkworm tailscale on`", d == "on the mini, run `silkworm tailscale on`", d)

    funnel = dict(current, AllowFunnel={f"{dns}:443": True})
    r, d, _ = run(running, funnel)
    check("Funnel on fails, even with serve otherwise right",
          r["status"] == H.FAIL and "Funnel is on" in r["summary"], r["summary"])
    check("decide: `tailscale funnel reset`", "tailscale funnel reset" in d, d)
    r, _, _ = run(running, F["serve_funnel"])
    check("the recorded serve config with Funnel turned on fails on both counts",
          "Funnel is on" in r["summary"] and "now " in r["summary"], r["summary"])

    no_dns = json.loads(json.dumps(running))
    no_dns["CurrentTailnet"]["MagicDNSEnabled"] = False
    no_https = dict(running, CertDomains=None)
    for label, st in (("MagicDNS off", no_dns), ("HTTPS certificates off", no_https)):
        r, d, _ = run(st)
        check(f"{label} fails; decide names the admin console",
              r["status"] == H.FAIL and "MagicDNS or HTTPS" in r["summary"]
              and "login.tailscale.com/admin/dns" in d, (r["summary"], d))

    r, d, _ = run((1, "", F["daemon_down_stderr"]))
    check("no tailscaled answering (recorded with its daemon killed) fails, not unknown",
          r["status"] == H.FAIL and "tailscaled is not running" in r["summary"]
          and r["fingerprint"] == "down", r)
    check("decide: start Tailscale on the mini", "open the Tailscale app" in d, d)

    r, _, s = run(running, find=lambda: "")
    check("no tailscale CLI: unknown, and nothing run",
          r["status"] == H.UNKNOWN and "not installed" in r["summary"] and not s.calls, r)
    r, d, _ = run(P.Unavailable("tailscale timed out after 15s"))
    check("a timeout is unknown, never fail", r["status"] == H.UNKNOWN
          and "timed out" in r["summary"] and d == "", r)
    r, _, _ = run(running, P.Unavailable("tailscale timed out after 15s"))
    check("a serve status timeout is unknown too", r["status"] == H.UNKNOWN, r)
    r, _, _ = run((1, "", "some new error"))
    check("an error that is not 'no daemon' is unknown: could not look",
          r["status"] == H.UNKNOWN, r)
    r, _, _ = run((0, "", ""))
    check("a status that prints nothing is unknown, not a state", r["status"] == H.UNKNOWN, r)
    r, d, _ = run(dict(running, BackendState="Stopped"),
                  dict(current, AllowFunnel={f"{dns}:443": True},
                       TCP={str(p): {"TCPForward": f"127.0.0.1:{p}"} for p in range(8000, 8012)},
                       Web={f"old-{i}.example.ts.net:443": {"Handlers": {
                           "/": {"Proxy": "http://127.0.0.1:8790"}}} for i in range(4)}))
    check("with many troubles at once, Funnel still leads the summary and the decision",
          r["summary"].startswith("Funnel is on") and "tailscale funnel reset" in d, r["summary"])
    r, _, _ = run(dict(running, BackendState="Starting"))
    check("Starting is unknown: between states, not wrong", r["status"] == H.UNKNOWN, r)

    # Keys: whatever tailscale prints, the result never carries one.
    leaky = json.loads(json.dumps(running))
    leaky["Self"]["PublicKey"] = "nodekey:" + "9c" * 32
    leaky["AuthURL"] = "https://login.tailscale.com/a/1a2b3c4d5e6f"
    secrets = ["9c" * 32, "1a2b3c4d5e6f", "tskey-" + "auth-kAbCdEf123CNTRL-" + "z" * 12]
    outs = [run(leaky)[0],
            run((1, "", f"{secrets[2]} To authenticate, visit: {leaky['AuthURL']} "
                        f"nodekey:{'9c' * 32}"))[0],
            run((1, "", f"failed to connect to local tailscaled: {leaky['AuthURL']} "
                        f"{secrets[2]}"))[0]]
    blob = json.dumps(outs)
    check("no node key, auth key or login link in any result",
          not [x for x in secrets if x in blob], blob)
    check("and the recorded fixtures carry none either",
          not _re.search(r"nodekey:[0-9a-f]{8}|tskey-|login\.tailscale\.com/a/",
                         json.dumps(F)))

    src = (BASE / "health_probes.py").read_text()
    cli_src = (BASE / "bin" / "silkworm").read_text()
    defs = lambda text: {n.name for n in ast.walk(ast.parse(text)) if isinstance(n, ast.FunctionDef)}
    check("one implementation: tailnet.py's helpers, used by both, defined by neither",
          not {"tailscale_bin", "tailscale_json", "tailscale_facts", "serve_entries",
               "serves_viz"} & (defs(src) | defs(cli_src))
          and "tailnet.tailscale_call(" in src and "tailnet.tailscale_bin" in src
          and "tailnet.tailscale_facts(" in cli_src and "tailnet.tailscale_bin()" in cli_src)
    check("tailscale_facts and the probe boil down through the same facts_from",
          "facts_from(tailscale_json(" in (BASE / "tailnet.py").read_text()
          and "tailnet.facts_from(" in src)

    # It is the mini's, and `silkworm health --project mini` shows it.
    reg = P.register_all(_probe_sys(home=_probe_home(), tailscale=_ts_world(running, live),
                                    disk=types.SimpleNamespace(free=500, total=1000)),
                         {"mini": {"disk": "/x"}}, registry={})
    check("registered for the mini only",
          [s for s in ("mini", "cadence", "trader") if reg["tailscale"].applies({"slug": s})]
          == ["mini"])
    cli = _load_cli()
    old = os.environ.pop("SILKWORM_TASK_ROLE", None)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            rows = cli.probe_now("mini", recs=[{"slug": "cadence"}],
                                 registry={"tailscale": reg["tailscale"]})
    finally:
        if old is not None:
            os.environ["SILKWORM_TASK_ROLE"] = old
    check("`silkworm health --probe --project mini` runs it",
          [(r["project"], r["check"], r["status"]) for r in rows]
          == [("mini", "tailscale", "fail")], rows)
    path = Path(tempfile.mkdtemp()) / "health.json"
    hs = H.HealthStore(path)
    hs.record("mini", "tailscale", rows[0]["result"] if "result" in rows[0] else
              {k: rows[0][k] for k in ("status", "summary", "evidence", "link", "fingerprint")})
    hs.record("cadence", "ci", H.result(H.OK, "green"))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cli.do_health(["--project", "mini"], path=path)
    text = out.getvalue()
    check("`silkworm health --project mini` lists it with its evidence, and only the mini",
          rc == 0 and "tailscale" in text and stale in text and "Funnel off" in text
          and "cadence" not in text, text)


def test_probe_config_maps_projects_explicitly():
    P = _probes()
    print("\nhealth.toml: an explicit table, read by every probe that needs one")
    cfg = P.load_config()
    check("the five apps, by App Store Connect id",
          sorted(cfg.get("apps") or {}) == ["cadence", "fathom", "mondays", "saga", "silkswing"]
          and all(str(v).isdigit() for v in cfg["apps"].values()))
    check("the Supabase backends, each with its ref and directory -- not Saga's, dropped "
          "2026-11-19 until it heads to review",
          sorted(cfg.get("supabase") or {}) == ["cadence", "mondays"]
          and all(v.get("ref") and v.get("dir") for v in cfg["supabase"].values()))
    text = (BASE / "health.toml").read_text()
    check("and no credential in it", not _re.search(r"sbp_|sb_secret|sk-ant|BEGIN|PASSWORD=",
                                                     text))
    reg = P.register_all(registry={})
    check("every probe registered under its name",
          sorted(reg) == ["app-store", "backend-drift", "credentials", "disk", "dispatching",
                          "gateway", "healthcheck", "revision", "tailscale", "testflight"])
    applies = {n: [s for s in ("trader", "cadence", "saga", "mondays", "fathom", "silkswing",
                               "silkworm", "mini") if reg[n].applies({"slug": s})]
               for n in reg}
    check("each applies only where the table says",
          applies == {"app-store": ["cadence", "saga", "mondays", "fathom", "silkswing"],
                      "testflight": ["cadence", "saga", "mondays", "fathom", "silkswing"],
                      "backend-drift": ["cadence", "mondays"],
                      "dispatching": ["trader"], "gateway": ["trader"],
                      "healthcheck": ["trader"], "credentials": ["mini"], "disk": ["mini"],
                      "tailscale": ["mini"], "revision": ["mini"]},
          applies)
    import health as H
    saga = {"slug": "saga"}
    check("a project with no Supabase table has no backend-drift row at all, not an "
          "unknown or a config error -- its other checks still run",
          [c.name for c in H.checks_for(saga, reg)] == ["app-store", "testflight"]
          and reg["backend-drift"].applies(saga) is False,
          [c.name for c in H.checks_for(saga, reg)])
    hs, ts, _ = _health_stores()
    hs.record("saga", "backend-drift", H.unknown("no credentials for Supabase project"), 1)
    hs.record("saga", "testflight", H.result(H.OK, "build 43 valid"), 1)
    H.retire(hs, ts, {H.key("saga", c.name) for c in H.checks_for(saga, reg)})
    check("and the old unknown row is retired on the next pass; the rest kept",
          hs.get("saga", "backend-drift") is None and hs.get("saga", "testflight") is not None)

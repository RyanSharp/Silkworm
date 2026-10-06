"""What one turn cost, when the CLI reports what the whole session has.

Since Claude Code 2.1.277 the `total_cost_usd` in a `claude -p --resume`
result event is the session's running total, restored across resumes -- not
the cost of the turn that just ran. Measured 2026-10-06 on haiku: a fresh
session reported $0.016031; resumed, $0.018464 (0.016031 + that turn's own
0.002433); again, $0.020879. Every resumed turn was recorded at that figure
and added to the thread and the task, so each one re-counted every turn before
it and a long conversation's recorded cost grew quadratically: the seven days
to 2026-10-06 recorded $12,234 against roughly $912 actually spent.

So a turn's cost is the increase in its session's reported total since the
last turn of the same session. A session seen for the first time, or one whose
total went down, has nothing to difference against: its turn costs what was
reported. The last total of every session is kept on its thread's record
(`session_totals` in sessions.json) so the difference survives restarts.

Before this machine moved to 2.1.283 at 2026-09-27 04:20 local, a resumed turn
reported its own cost. Those records were right and must never be
"corrected" by differencing; `plan()` touches only turns that ended at or
after CUTOFF.

These are API-list-price equivalents: the bot runs on a subscription token,
so nothing here is a billed amount.
"""

import logging
from datetime import datetime

log = logging.getLogger("silkworm.turncost")

#: When this machine's CLI started reporting running totals: 2.1.283 was
#: installed at 04:20 local on 2026-09-27. Local, because that is the clock
#: the bot that recorded the turns ran on.
CUTOFF = datetime(2026, 9, 27, 4, 20).timestamp()

#: How many sessions a thread remembers the last total of. A thread mostly
#: has one; reviewers and lost sessions add more. Old ones are never resumed.
KEEP_SESSIONS = 50

#: Said wherever a cost is shown.
LABEL = "API list-price equivalent, not a billed amount"


def per_turn(reported, previous) -> float:
    """This turn's cost, given the session's reported total now and at its
    previous turn (None for a session not seen before)."""
    reported = float(reported or 0.0)
    if previous is None or reported < float(previous):
        return reported
    return reported - float(previous)


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _ended(rec: dict):
    """When a record's single run ended. `costs._ended` reads its events;
    a compacted record has none, and falls back to `updated`."""
    import costs
    return costs._ended(rec) or rec.get("updated") or rec.get("created")


def _runs(rec: dict):
    """(runs, extra) for a record: `runs` is [(ended_at, raw reported
    figure)] for each run recorded before per-turn costs were, and `extra` the
    [at, usd] runs recorded per turn since (a corrected task sent back again).
    None if the record has nothing to plan: no cost, or recorded per turn
    from the start and so already right.
    """
    result = rec.get("result")
    if not isinstance(result, dict) or not _num(result.get("cost")):
        return None
    corrected = "cost_uncorrected" in result
    if "cost_reported_total" in result and not corrected:
        return None                     # recorded per turn already
    current = [list(r) for r in (result.get("cost_runs") or [])
               if isinstance(r, (list, tuple)) and len(r) == 2 and _num(r[1])]
    original = result.get("cost_runs_uncorrected")
    if original is None and not corrected:
        original = current
    original = [r for r in (original or []) if isinstance(r, (list, tuple))
                and len(r) == 2 and _num(r[1])]
    if original:
        runs = [(r[0] if _num(r[0]) else None, float(r[1])) for r in original]
    else:
        raw = result.get("cost_uncorrected", result["cost"])
        at = (current[0][0] if corrected and current and _num(current[0][0])
              else _ended(rec))
        runs = [(at, float(raw))]
    extra = current[len(runs):] if corrected else []
    return runs, extra


def plan(records: dict, cutoff: float = CUTOFF, fresh=None) -> dict:
    """The correction of `records` ({id: task record}), without applying it.

    Returns {"tasks": {id: result fields to merge}, "threads": {key:
    {"targets", "pairs", "seeds"}}}. Pure, and deterministic in the records:
    it reads each run's original figure (`cost_uncorrected` once corrected),
    so running it again -- straight away, after a failure part-way, or after
    corrected tasks have been reworked -- plans the same correction.

    `fresh(role)` says whether a role starts a new session every run. A record
    holds only its latest session id, so the earlier runs of such a role were
    other sessions and are never differenced against each other.
    """
    if fresh is None:
        import roles
        fresh = roles.is_fresh
    by_session: dict = {}               # session key -> [(ended, tid, i, raw)]
    planned: dict = {}
    for tid, rec in records.items():
        sid = rec.get("session_id")
        got = _runs(rec)
        if not sid or not got:
            continue
        runs, extra = got
        planned[tid] = (runs, extra)
        last = len(runs) - 1 + len(extra)
        for i, (at, raw) in enumerate(runs):
            if at is None or at < cutoff:
                continue                # its own cost; leave it as it is
            key = sid if (i == last or not fresh(rec.get("role") or "")) else (tid, i)
            by_session.setdefault(key, []).append((at, tid, i, raw))

    new_runs = {tid: [raw for _, raw in runs] for tid, (runs, _) in planned.items()}
    last_total: dict = {}               # session id -> (ended, tid, i, raw)
    for key, turns in by_session.items():
        turns.sort(key=lambda t: (t[0], t[1], t[2]))
        prev = None
        for at, tid, i, raw in turns:
            new_runs[tid][i] = round(per_turn(raw, prev), 6)
            prev = raw
        if isinstance(key, str):
            last_total[key] = turns[-1]

    out_tasks: dict = {}
    threads: dict = {}

    def thread(key):
        return threads.setdefault(key, {"targets": {}, "pairs": [], "seeds": {}})

    for tid, (runs, extra) in planned.items():
        rec = records[tid]
        result = rec["result"]
        corrected = new_runs[tid]
        if not extra and "cost_uncorrected" not in result \
                and all(abs(n - raw) < 1e-9 for n, (_, raw) in zip(corrected, runs)):
            continue                    # nothing post-cutoff was differenced
        original_cost = result.get("cost_uncorrected", result["cost"])
        fields = {"cost_uncorrected": original_cost}
        had_runs = "cost_runs_uncorrected" in result or bool(
            result.get("cost_runs")) and "cost_uncorrected" not in result
        if had_runs or extra:
            if had_runs:
                fields["cost_runs_uncorrected"] = result.get(
                    "cost_runs_uncorrected", result.get("cost_runs"))
            # Dated as already written where they were: a rerun's add_run
            # stamps the earlier run itself, and redating it is not a fix.
            dated = (result.get("cost_runs") or []) if "cost_uncorrected" in result else []
            fields["cost_runs"] = ([[dated[i][0] if i < len(dated) else at, usd]
                                    for i, ((at, _), usd) in enumerate(zip(runs, corrected))]
                                   + [list(r) for r in extra])
            fields["cost"] = round(sum(r[1] for r in fields["cost_runs"]), 6)
        else:
            fields["cost"] = corrected[0]
        out_tasks[tid] = fields
        key = rec.get("thread")
        if not key:
            continue
        th = thread(key)
        th["targets"][tid] = round(sum(corrected) - sum(raw for _, raw in runs), 6)
        current = result.get("cost_runs") or [[None, result["cost"]]]
        for (at, raw), new, cur in zip(runs, corrected, current):
            cur = float(cur[1]) if _num(cur[1]) else raw
            if abs(cur - new) > 1e-9:
                th["pairs"].append((at or 0, cur, new))
    for sid, (_, tid, _, raw) in last_total.items():
        key = records[tid].get("thread")
        if key:
            thread(key)["seeds"][sid] = raw
    for th in threads.values():
        th["pairs"] = [(cur, new) for _, cur, new in sorted(th["pairs"])]
    return {"tasks": out_tasks, "threads": threads}


def correct(task_store, session_store, cutoff: float = CUTOFF, fresh=None) -> dict:
    """Plan and apply the one-off correction through the stores.

    Idempotent: a second run plans the same figures, finds them already in
    place, and changes nothing. Returns before/after totals for the log.
    """
    records = task_store.all()
    before_tasks = _sum_costs(records)
    before_threads = _sum_threads(session_store.all())
    p = plan(records, cutoff, fresh)
    changed = task_store.correct_costs(p["tasks"])
    for key, th in p["threads"].items():
        session_store.correct_costs(key, th["targets"], th["pairs"], th["seeds"])
    summary = {"tasks_changed": changed,
               "tasks_before": round(before_tasks, 2),
               "tasks_after": round(_sum_costs(task_store.all()), 2),
               "threads_before": round(before_threads, 2),
               "threads_after": round(_sum_threads(session_store.all()), 2)}
    return summary


def _sum_costs(records: dict) -> float:
    return sum(float((r.get("result") or {}).get("cost") or 0.0)
               for r in records.values() if isinstance(r.get("result"), dict)
               and _num((r.get("result") or {}).get("cost")))


def _sum_threads(entries: dict) -> float:
    return sum(float(e.get("cost") or 0.0) for e in entries.values())

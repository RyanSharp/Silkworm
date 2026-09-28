"""Is each background thread still there, or did one of them quietly stop?

The bot runs about fourteen daemon threads: sweepers, schedulers, watchers,
task workers. Each one is a bare `while True:` loop, and an exception that
escapes its body ends that thread for the lifetime of the process. Nothing
restarts it, nothing notices, and -- because a dead thread logs nothing further
-- the only symptom is the absence of work that used to happen. That is how the
Slack link went down for seventeen hours: the process was up, the HTTP server
answered, and no one could see the one part that had stopped.

slack_health.py makes this argument for the socket. This is the same argument
for everything else, and the cheapest possible version of it: remember each
thread as it is started, and let `/status` say which of them are gone.

Three outcomes are distinguished, because reporting them alike would either cry
wolf or stay silent:

  * still alive -- the normal case, and nothing to say.
  * stopped by an exception -- always reported, whatever the thread was for.
    The traceback is already in the log; this says which loop it ended.
  * returned normally -- expected of a startup pass (`forever=False`), and a
    failure of a loop that was meant to outlive the process (`forever=True`).
    A feature switched off in .env returns immediately on purpose, so its
    thread is started with `forever` set to whether it is switched on at all.

Kept separate from bot.py so it can be tested without a Slack token.
"""

import threading
import time

#: name -> {"thread", "forever", "started", "stopped", "error"}
_registry: dict[str, dict] = {}
_lock = threading.Lock()


def start(target, name: str, *, forever: bool = True, args: tuple = ()) -> threading.Thread:
    """Start a daemon thread and remember it, so its absence is noticeable.

    Registered before it is started rather than after: a thread that raises on
    its first line would otherwise be running -- and then gone -- before the
    registry had heard of it, which is exactly the case worth catching.
    """
    entry = {"thread": None, "forever": bool(forever), "started": time.time(),
             "stopped": None, "error": None}

    def run() -> None:
        try:
            target(*args)
        except BaseException as e:            # noqa: BLE001 - recorded, then re-raised
            entry["error"] = f"{type(e).__name__}: {e}".strip().splitlines()[0][:200]
            raise

    thread = threading.Thread(target=run, daemon=True, name=name)
    entry["thread"] = thread
    with _lock:
        _registry[name] = entry
    thread.start()
    return thread


def _observe(entry: dict, now: float) -> bool:
    """Whether this thread is alive, stamping when it was first seen not to be.

    A thread's death has no timestamp of its own, so "dead for" is measured
    from the first check that noticed -- up to one poll late, and honest about
    which end it errs on.
    """
    thread = entry["thread"]
    if thread is not None and thread.is_alive():
        entry["stopped"] = None
        return True
    if entry["stopped"] is None:
        entry["stopped"] = now
    return False


def dead(now: float | None = None) -> list[dict]:
    """Registered threads that ought to be running and are not.

    A startup pass that finished its job is not dead, so `forever=False`
    threads are only reported if they ended by raising.
    """
    now = time.time() if now is None else now
    out = []
    with _lock:
        entries = list(_registry.items())
    for name, entry in entries:
        if _observe(entry, now):
            continue
        if entry["error"] is None and not entry["forever"]:
            continue
        out.append({"name": name,
                    "reason": entry["error"] or "returned without raising",
                    "dead_for": round(now - (entry["stopped"] or now), 1)})
    return sorted(out, key=lambda d: d["name"])


def status(now: float | None = None) -> dict:
    """What `/status` reports: how many we started, how many run, which are gone.

    `alive` is counted, not derived as count minus dead: a startup pass that
    finished is neither, and subtracting would report it as running.
    """
    now = time.time() if now is None else now
    gone = dead(now)
    with _lock:
        entries = list(_registry.values())
    alive = sum(1 for e in entries if e["thread"] is not None and e["thread"].is_alive())
    return {"count": len(entries), "alive": alive, "dead": gone}


def names() -> list[str]:
    with _lock:
        return sorted(_registry)


def reset() -> None:
    """Forget everything. For tests; the bot registers once and never clears."""
    with _lock:
        _registry.clear()

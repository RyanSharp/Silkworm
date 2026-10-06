"""Retention for artifacts/ -- the local copies of files turns sent to Slack.

upload_outbox() moves every file a turn produces into
artifacts/<thread-key>/ so the visualizer can show it back. Until this module
nothing ever left: by 2026-09-29 it was 301MB, almost all of it screen-capture
video, with 135MB of that already pointed at by nothing.

The rule, and why it is this one:

  * An uploaded file older than ARTIFACT_MAX_AGE_DAYS loses its bytes. Its
    record stays in the thread's `files` list, stamped `pruned`, so the
    visualizer still says the file existed and when it went. Age rather than a
    byte budget because an artifact is a *mirror*: the file was uploaded to the
    Slack thread, and Slack keeps that copy. What the local one buys is quick
    access from the dashboard while a thread is live, and that value decays
    with time, not with size. A byte budget would have kept a dead thread's
    video forever if it happened to fit, and evicted a live thread's newest
    screenshot if it did not.
  * A file whose record says it never reached Slack (`uploaded` not True) is
    kept regardless of age. There it is the only copy, not a mirror.
  * A file no record names at all is removed once it is a day old -- unless
    its name carries UNSENT, which upload_outbox gives a file whose upload
    failed, so the only-copy clause survives losing the record. Nothing can
    show it -- the visualizer serves only recorded paths -- so it is weight
    with no fact attached to lose. This is how most of the space goes today:
    `!reset` drops a thread's record, files list and all, and SessionStore's
    200-entry cap on `files` drops the oldest record while the file stays. The
    day of grace covers upload_outbox moving a file in a moment before it
    records it.
  * Nothing in the directory of a thread with a pending turn is touched.
  * Only directories upload_outbox makes -- `<channel>__<ts>`, a thread key
    with its colon swapped -- are this rule's to sweep. Anything else under
    artifacts/ was put there by hand or by a turn (a multi-step plan keeps its
    progress tracker in artifacts/plan-<date>/, for one) and no record will
    ever name it, so the unreferenced clause would take it after a day.

plan() decides and touches nothing; apply() carries a plan out. The sweeper
only calls apply() when ARTIFACT_PRUNE is on, and otherwise logs what it would
have removed -- see bot._sweep_pass.
"""

import os
import re
import time
from pathlib import Path

#: Put in an archived file's name by upload_outbox when the upload failed. A
#: record saying `uploaded: False` is lost along with the record (the cap,
#: !reset, a failed save), and a file with no record would otherwise count as
#: abandoned; the name is what still says it is the only copy.
UNSENT = "-unsent-"

#: How old an unreferenced file must be before it counts as abandoned rather
#: than mid-archive.
UNREFERENCED_GRACE_S = 86400

#: The name upload_outbox gives a thread's directory: key.replace(":", "__"),
#: and every key is f"{channel}:{ts}".
THREAD_DIR = re.compile(r"^[A-Za-z0-9]+__[0-9]+(\.[0-9]+)?$")


def _real(p) -> str:
    return os.path.realpath(str(p))


def plan(root: Path, sessions: dict, days: float, now: float | None = None) -> list[dict]:
    """Every file under `root` the rule would remove, with why and how big.

    `days` <= 0 switches the age rule off; unreferenced files still count.
    """
    now = time.time() if now is None else now
    recorded: dict[str, tuple[str, dict]] = {}
    held: set[str] = set()
    for key, entry in sessions.items():
        if entry.get("pending"):
            held.add(key.replace(":", "__"))
        # list() is one C-level copy under the GIL: store.all() copies each
        # entry but not this list, which add_file trims in place.
        for rec in list(entry.get("files") or ()):
            if rec.get("path"):
                recorded[_real(rec["path"])] = (key, rec)
    out = []
    try:
        dirs = sorted(d for d in Path(root).iterdir()
                      if d.is_dir() and THREAD_DIR.match(d.name))
    except FileNotFoundError:
        return out
    for d in dirs:
        if d.name in held:
            continue
        for f in sorted(d.rglob("*")):
            try:
                if not f.is_file():
                    continue
                st = f.stat()
            except OSError:
                continue          # removed under us; nothing to plan
            hit = recorded.get(_real(f))
            if hit:
                key, rec = hit
                if key.replace(":", "__") in held:
                    continue
                if rec.get("pruned") or rec.get("uploaded") is not True:
                    continue
                if days > 0 and now - float(rec.get("ts") or st.st_mtime) > days * 86400:
                    # `record` is the path as the record spells it, which is
                    # what mark_pruned matches; `path` is the file on disk.
                    out.append({"path": str(f), "size": st.st_size, "key": key,
                                "dir": d.name, "record": rec["path"], "why": "expired"})
            elif UNSENT in f.name:
                continue
            elif now - max(st.st_mtime, st.st_ctime) > UNREFERENCED_GRACE_S:
                out.append({"path": str(f), "size": st.st_size, "key": None,
                            "dir": d.name, "why": "unreferenced"})
    return out


def apply(doomed: list[dict], store, now: float | None = None) -> tuple[int, int]:
    """Remove what plan() chose; stamp the records of expired files. -> (files, bytes)

    The file goes first and the stamp second, so a failed save leaves a record
    that still says the file exists -- which the visualizer checks on disk and
    shows as gone anyway -- rather than one claiming a removal that did not
    happen.
    """
    now = time.time() if now is None else now
    removed, freed = 0, 0
    by_key: dict[str, list[str]] = {}
    # plan() read `pending` once; a turn may have started on one of these
    # threads since. Ask again, so the exemption holds at the moment of removal.
    held = {k.replace(":", "__") for k, e in store.all().items() if e.get("pending")}
    for item in doomed:
        if item.get("dir") in held:
            continue
        try:
            os.unlink(item["path"])
        except FileNotFoundError:
            pass
        except OSError:
            continue
        removed += 1
        freed += item["size"]
        if item["key"]:
            by_key.setdefault(item["key"], []).append(item.get("record", item["path"]))
    for key, paths in by_key.items():
        store.mark_pruned(key, paths, now)
    return removed, freed


def summary(doomed: list[dict]) -> str:
    n = {w: [i for i in doomed if i["why"] == w] for w in ("expired", "unreferenced")}
    mb = lambda items: sum(i["size"] for i in items) / 1e6   # noqa: E731
    return (f"{len(doomed)} file(s), {mb(doomed):.1f}MB -- "
            f"{len(n['expired'])} expired ({mb(n['expired']):.1f}MB), "
            f"{len(n['unreferenced'])} unreferenced ({mb(n['unreferenced']):.1f}MB)")

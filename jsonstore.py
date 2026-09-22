"""Crash-safe reading and writing of the JSON files Silkworm keeps its state in.

Every store here used to save with `path.write_text(json.dumps(...))`, which
truncates the target and *then* writes into it. A kill landing in that window --
a restart, a crash, a reboot -- leaves a half-written file. tasks.json is over a
megabyte and is rewritten on every create, update, transition and claim, from
several threads at once, so that window is open a lot.

`save()` closes it: the new contents go to a temp file in the same directory,
get flushed, and are moved over the target with os.replace, which is atomic. A
reader sees either the whole old file or the whole new one, never a prefix.

Each completed save is then written a second time to `<name>.prev`, and `load()`
falls back to that copy when the primary will not parse, saying loudly in the
log which one it used. It is written rather than hard-linked on purpose: a link
would share an inode with the primary, so anything that truncated one -- an
older build, an editor saving in place, a half-flushed block after a power cut
-- would take the backup with it, which is precisely the case it exists for.

What `load()` will not do is start empty. An empty store is indistinguishable
from a fresh install, which is exactly how losing 400 task records becomes
invisible; when there is nothing left to read, it raises and you find out.

Nor, for whoever owns the file, will it fall back on one it simply could not
open. A permission or a momentarily exhausted fd table says nothing about the
contents, and the backup is always an older save -- so substituting it there
would hand the bot stale records, which it would then write back over a file
that was fine all along. Only content we can read and cannot parse is treated
as corruption. A `repair=False` reader writes nothing and so cannot rewind
anything; it still gets the fallback, and still touches neither file.
"""

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("silkworm.jsonstore")

#: The last contents known to parse. Written on every save, read only when the
#: primary is unreadable.
BACKUP_SUFFIX = ".prev"

#: Where an unreadable primary is moved so recovery doesn't destroy the
#: evidence. One slot: the interesting corruption is the one you just hit.
CORRUPT_SUFFIX = ".corrupt"

#: Content that is not readable JSON: the file is the problem, and recovering
#: means replacing it.
_BAD_CONTENT = (json.JSONDecodeError, UnicodeDecodeError, ValueError)
#: ...as opposed to not being able to read it at all (permissions, a full fd
#: table, a flaky disk), where the file may well be perfectly good.
_READ_ERRORS = _BAD_CONTENT + (OSError,)


class WrongShape(ValueError):
    """Readable JSON, but not the kind of thing this store holds."""


class CorruptStore(RuntimeError):
    """A store file and its backup were both unreadable."""


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + BACKUP_SUFFIX)


def corrupt_path(path: Path) -> Path:
    return path.with_name(path.name + CORRUPT_SUFFIX)


def _parse(text: str, default):
    """json.loads, plus the one thing json.loads will not tell you.

    A store that holds a dict and reads back `null`, `0` or `[]` is not a store
    that happens to be empty: it is the wrong file, an older format, or a write
    that went somewhere it should not have. The callers coerce with `or {}`, so
    without this it loads as empty and the next save writes that emptiness over
    both copies -- the exact silent loss the rest of this module refuses. It is
    content we can read and cannot use, which is corruption, so it recovers the
    same way.
    """
    data = json.loads(text)
    if default is not None and not isinstance(data, type(default)):
        raise WrongShape(f"expected {type(default).__name__}, "
                         f"found {type(data).__name__}")
    return data


def _scratch(path: Path) -> Path:
    # Same directory, so the final os.replace is a rename within one filesystem.
    # Pid and thread id keep two concurrent savers off each other's temp file.
    return path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")


def _write(path: Path, text: str) -> None:
    """Put `text` at `path` without the target ever holding a prefix of it."""
    tmp = _scratch(path)
    try:
        # Created at 0600 rather than at whatever the umask says: between here
        # and the chmod below, this file holds the entire contents of a store
        # someone may have deliberately locked down.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with open(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())           # on disk, not just in the page cache
        # write_text() wrote through an existing file and so kept its mode;
        # replacing it with a fresh temp file would hand every store whatever
        # the umask says instead. Carry the mode across rather than quietly
        # widening -- or narrowing -- a file that already had one.
        try:
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        except OSError:
            pass                            # no target yet, or a mode we can't set
        os.replace(tmp, path)               # atomic: readers see old or new
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def save(path: Path, data, *, indent: int = 2, keep_backup: bool = True) -> None:
    """Write `data` to `path` so that a kill mid-save cannot truncate it."""
    text = json.dumps(data, indent=indent)      # before any file is touched
    _write(path, text)
    if keep_backup:
        # After the primary, not before, so the backup holds the last save that
        # actually finished: falling back to it costs at most the one write
        # that was interrupted. A store that can't be backed up is still worth
        # having saved, so a failure here is logged rather than raised.
        try:
            _write(backup_path(path), text)
        except OSError as exc:
            log.warning("could not refresh %s: %s", backup_path(path).name, exc)


def load(path: Path, *, default=None, strict: bool = True, repair: bool = True):
    """Read `path`, falling back to `<name>.prev` if the primary won't parse.

    Returns `default` when there is genuinely nothing on disk yet. When there
    *was* something and none of it can be read, `strict` decides between raising
    CorruptStore (right for anything holding records) and returning `default`
    (right for a watermark, where the cost of starting over is a rescan).

    The fallback is for content we can prove is bad, and only that. A file we
    merely could not *open* gets none of this: see below.

    `repair` is for readers that do not own the file. Recovering normally means
    setting the wreckage aside and putting the recovered contents back, which is
    right for the process that is about to keep writing there and wrong for
    anyone else: the dashboard is a second process reading the bot's live
    files, and it renaming one out from under a running bot -- which is holding
    the real records in memory and will save them over the top -- would turn a
    readable situation into a lost one. With `repair=False` the fallback still
    happens, silently and in memory, and nothing on disk is touched.
    """
    if repair:
        _sweep_scratch(path)                # orphans from a save that was killed
    primary_error = None
    if path.exists():
        try:
            text = path.read_text()
            data = _parse(text, default)
        except _READ_ERRORS as exc:
            primary_error = exc
        else:
            # A store that has been read but not yet written has no backup, so
            # the first boot after this shipped would have had no second copy
            # of a file it had just proved good. Seed it from the text we read,
            # which is by definition the last save that finished. Once per
            # file: after this, save() keeps it current.
            if repair and not backup_path(path).exists():
                try:
                    _write(backup_path(path), text)
                except OSError as exc:
                    log.warning("could not seed %s: %s", backup_path(path).name, exc)
            return data

    backup = backup_path(path)

    # Being unable to read a file is not the same as knowing it is bad, and the
    # difference decides who wins. The backup is by definition an *older* save;
    # substituting it for a primary that may be perfectly good hands the caller
    # stale records, and the caller is the bot, which holds them in memory and
    # writes them straight back over the good file on its next save. A momentary
    # EACCES or EMFILE would spend the board that way, silently, one save later.
    # So a primary we could not open is not recovered from and not touched: it
    # is reported. Refusing to start is recoverable; a silent rewind is not.
    #
    # That binds the owner, not everyone. The rewind happens because the caller
    # writes what it was given back; a `repair=False` reader never writes, so
    # the worst it can do with the backup is draw a slightly old page, and
    # failing the dashboard over a passing EACCES would be the worse trade.
    if repair and primary_error is not None and not isinstance(primary_error, _BAD_CONTENT):
        message = (f"{path} could not be read ({primary_error}). Leaving it "
                   f"exactly where it is -- a file that will not open may be "
                   f"perfectly good, and falling back to {backup.name} would "
                   f"quietly rewind it to an older save.")
        if strict:
            raise CorruptStore(message)
        log.error("%s Continuing with an empty %s.", message, path.name)
        return default

    backup_error = None
    if backup.exists():
        try:
            data = _parse(backup.read_text(), default)
        except _READ_ERRORS as exc:
            backup_error = exc
        else:
            # For an owner, only reachable with a primary that is absent or
            # provably bad -- an unreadable one refused above. A read-only
            # reader can also arrive here on one it merely could not open, and
            # changes nothing on the way through.
            set_aside = repair and _set_aside(path, primary_error)
            if primary_error is None:
                log.error("%s is missing — recovered from %s", path.name, backup.name)
            else:
                log.error("%s %s (%s) — recovered from %s%s", path.name,
                          _why(primary_error), primary_error, backup.name,
                          f"; the unreadable copy is kept at {corrupt_path(path).name}"
                          if set_aside else "")
            # Put the recovered contents back where they belong, so the next
            # reader doesn't have to repeat this -- but only into a slot that is
            # now empty. If setting the wreckage aside failed, writing over it
            # here would destroy the one copy of the corruption we promised to
            # keep, so leave both alone and recover again next time.
            if repair and (primary_error is None or set_aside):
                # No backup pass: it already holds exactly this. Tidying up is
                # not worth losing over -- we are holding the records, and
                # refusing to hand them back because the disk is full would be
                # the loss this whole module exists to prevent.
                try:
                    save(path, data, keep_backup=False)
                except OSError as exc:
                    log.warning("recovered %s but could not write it back: %s",
                                path.name, exc)
            return data

    if primary_error is None and backup_error is None:
        return default                          # nothing there at all: fresh install

    # Nothing readable, and something was there. Both files stay exactly where
    # they are -- the wreckage is only ever set aside to make room for contents
    # to be written back, and there are none.
    #
    # Renaming it here is what made the refusal last exactly one boot. The
    # primary would be *missing* the next time round, which reads as a fresh
    # install and returns empty without so much as a log line; and the bot's
    # launchd job has KeepAlive set, so nobody has to decide to try again. The
    # guarantee is worth having only if it holds every time it is asked.
    trouble = (f"{path} is missing" if primary_error is None
               else f"{path} {_why(primary_error)} ({primary_error})")
    fallback = (f"its backup {backup.name} {_why(backup_error)} too ({backup_error})"
                if backup_error else f"there is no {backup.name} to fall back on")
    message = (f"{trouble} and {fallback}. Both are left exactly where they are."
               " Refusing to start empty — an empty store looks exactly like a "
               "fresh install.")
    if strict:
        raise CorruptStore(message)
    log.error("%s — continuing with an empty %s", message, path.name)
    return default


def _why(error: Exception) -> str:
    if isinstance(error, WrongShape):
        return "is not a store of this kind"
    return "did not parse" if isinstance(error, _BAD_CONTENT) else "could not be read"


def _sweep_scratch(path: Path) -> list:
    """Delete temp files left behind by a writer that is no longer running.

    `_write`'s own cleanup is a `finally`, which a signal does not run -- and a
    signal is the case this module is about. Every `silkworm restart` that
    lands mid-save leaves a full copy of the store behind, 3 MB for the board,
    gitignored and therefore invisible, and nothing else ever removes it.

    Done at load rather than at save: a restart is exactly when the orphans
    appear and exactly when nothing else is writing, and it keeps the cost off
    the path that runs on every create, update, transition and claim.

    A temp file carries the pid of the writer that made it, so one still being
    written is recognisable and left alone. A pid that has been reused by some
    unrelated process reads as alive, which leaves litter rather than deleting
    a live write -- the right way round.
    """
    swept = []
    for f in path.parent.glob(f"{path.name}*.tmp.*"):
        pid = f.name.rsplit(".tmp.", 1)[-1].split(".")[0]
        if not pid.isdigit() or int(pid) == os.getpid():
            continue                        # ours, and possibly in flight
        try:
            os.kill(int(pid), 0)
            continue                        # still running: leave it be
        except ProcessLookupError:
            pass
        except OSError:
            continue                        # alive, or not ours to ask about
        try:
            f.unlink()
            swept.append(f.name)
        except OSError as exc:
            log.warning("could not remove %s: %s", f.name, exc)
    if swept:
        log.info("removed %d temp file(s) left by a killed save: %s",
                 len(swept), ", ".join(swept))
    return swept


def _set_aside(path: Path, error: Exception | None) -> bool:
    """Move an unparseable file out of the way, keeping it for inspection.

    Only when its *contents* are the problem. A file we merely couldn't read --
    a permission, a momentarily exhausted fd table -- is probably fine, and
    renaming it would turn a transient failure into a real one.

    Reports whether the file was actually moved, so a caller can tell the
    difference between a slot it has emptied and one still holding the only
    copy of the wreckage.
    """
    if not isinstance(error, _BAD_CONTENT):
        return False
    try:
        os.replace(path, corrupt_path(path))
    except OSError as exc:
        log.warning("could not set %s aside: %s", path.name, exc)
        return False
    return True

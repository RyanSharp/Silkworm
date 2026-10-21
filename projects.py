"""Projects: what a task belongs to, as distinct from where it may act.

`scope` answers "which directory and repo may this touch" -- an isolation
question. `project` answers "what body of work is this part of" -- an
organisational one. They are deliberately separate: two projects can share a
repo, one project can span several, and plenty of projects ("plan the Asia
trip") have no repo at all. Grouping tasks by cwd would quietly work for the
first case and fall apart on the last.

A project without a repo gets a directory of its own, holding both its files
and a CLAUDE.md that Claude Code loads by itself -- so the brief needs no
injection machinery at all. A repo-backed project already has a home and a
CLAUDE.md that belongs to you; we never write there, and learnings cover it.

Projects are created on first use rather than set up in advance. A task system
that requires you to define a project before you can file anything is a task
system you stop using. A project may carry a default scope, which tasks filed
under it inherit when they don't specify their own -- so "work on the trader"
lands in the right directory without repeating it every time.
"""

import logging
import re
import threading
import time
from pathlib import Path

import jsonstore

log = logging.getLogger("silkworm.projects")

VERSION = 1

#: A brief is context on every prompt, so it has to stay small. Past this it
#: costs more than it saves.
BRIEF_CHARS = 1500

#: Where a project with no repo of its own lives. It needs somewhere for its
#: files regardless -- itineraries, notes, screenshots -- and that directory is
#: also where its CLAUDE.md goes.
PROJECT_ROOT = Path.home() / "workspace" / "projects"

#: Where facts drawn from mail accumulate. Deliberately *not* CLAUDE.md: that
#: file is capped and rewritten wholesale after every task, so an itinerary put
#: there would be summarised away within a turn or two. This one only ever
#: grows, and CLAUDE.md points at it.
LOGISTICS = "logistics.md"

#: Appended to CLAUDE.md structurally rather than written by the model, so a
#: brief rewrite cannot drop the pointer and strand the file.
POINTER = f"Confirmations, bookings and dates are in `./{LOGISTICS}` -- read it "\
          "before answering anything about schedule, travel or reservations."

FIELDS: dict[str, tuple] = {
    "slug":     (None,  "stable identifier, e.g. asia-trip"),
    "v":        (0,     "schema version of this record"),
    "title":    ("",    "human name, e.g. Asia Trip"),
    "scope":    (dict,  "default {cwd, repo} tasks inherit when unset"),
    # What has been decided about this project, injected into every task filed
    # under it. Rewritten rather than appended, so it stays a short living
    # brief instead of an ever-growing log that taxes every prompt.
    # The brief itself lives in the project's CLAUDE.md, which Claude Code
    # loads on its own; only the timestamp is tracked here.
    "brief_at": (0.0,   "unix time the brief was last rewritten"),
    # The Gmail label whose mail belongs to this project. Empty means "use the
    # title", so a label you already keep needs no configuration at all.
    "mail_label": ("",  "gmail label to draw facts from; blank = the title"),
    # How this project proves its own work. Unset means unverified, which is
    # different from failing: work is never auto-merged without one, because
    # silence is not confidence.
    "test_cmd": ("",    "command that verifies this project, e.g. ./bin/silkworm test"),
    # Off by default, and refused outright without a test command: landing work
    # unattended on a project that cannot prove itself is merging on a guess.
    "auto_merge": (False, "land reviewed, verified work on the base branch"),
    # Whether a landing is published. Landing fast-forwards the *local* base;
    # origin only hears about it if this is on. Off by default because pushing
    # to a shared remote is an outward act on someone else's repository, and
    # because it is the one step here that cannot be undone quietly. On, it
    # keeps origin and the local base in step -- which is what lets the next
    # task's worktree, cut from origin, see work that has already landed.
    "publish": (False, "push the base to origin after landing, so the next task sees it"),
    # Out-of-hours ideation. "HH:MM" local time, or "" for off. `ideate_on` is
    # the last date the night was *decided* -- run, or deliberately skipped
    # because the project's proposal backlog was already at its standing
    # limit. Deliberately not "last ran": reading it that way would report a
    # session that never happened. It exists so a restart cannot make the pass
    # run twice in a night, and a missed night is skipped rather than fired
    # late.
    "ideate_at": ("",   "local HH:MM to look for improvements, or off"),
    "ideate_on": ("",   "YYYY-MM-DD the night was decided: run or skipped"),
    # Files the project's own build rewrites, which therefore never count as
    # uncommitted work: a task checkout holding only these is released (and
    # they are put back as committed first), and the base checkout is not
    # "dirty" over them. Cadence's xcodegen build injects keys into both
    # Info.plists; counting those held fourteen checkouts and refused every
    # landing. Relative to the repo root; a trailing "/" means a directory.
    "generated": (list, "paths a build regenerates, never counted as uncommitted work"),
    # Whether the base fails its own suite, with nobody's change on it. Set by
    # basered.py when a task's tests fail and a clean checkout of the base
    # fails them too; cleared when a later base commit passes. While set, the
    # project's implementor work is held rather than bounced off it one task
    # at a time. {sha, base, tests, since, attention} or None.
    "main_failing": (None, "the base fails its own tests: {sha, base, tests, since, attention}"),
    # What the suite said about recent base commits, so each is run at most
    # once: {sha: {ok, failed, at}}. A handful, newest kept.
    "base_checks": (dict, "suite results on recent base commits, by sha"),
    "archived": (False, "hidden from pickers; existing tasks keep their label"),
    "created":  (0.0,   "unix time"),
    "updated":  (0.0,   "unix time"),
}


#: Generated paths already known when the field was added, for records that
#: predate it. Never re-applied once a record carries the field.
SEED_GENERATED = {
    "cadence": ["ios/Cadence/Info.plist", "ios/CadenceWidget/Info.plist"],
}


def parse_paths(text) -> list[str]:
    """A list of repo-relative paths from a form or a command: commas,
    spaces or newlines between them; "off"/"none"/"clear" for none."""
    if isinstance(text, (list, tuple)):
        items = [str(t) for t in text]
    else:
        text = (text or "").strip()
        if text.lower() in ("off", "none", "clear"):
            return []
        items = re.split(r"[,\s]+", text)
    out = []
    for item in items:
        item = item.strip()
        while item.startswith("./"):
            item = item[2:]
        if not item:
            continue
        if item.startswith("/") or ".." in Path(item).parts:
            raise ValueError(f"{item!r} is not a path inside the repository")
        if item not in out:
            out.append(item)
    return out


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s[:40] or "untitled"


def default(name: str):
    if name not in FIELDS:
        return None
    d = FIELDS[name][0]
    return d() if callable(d) else d


class ProjectStore:
    """projects.json: slug -> record."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        for slug, rec in (jsonstore.load(path, default={}) or {}).items():
            # Seeded only where the field has never existed, so a list the
            # user has since edited -- or cleared -- is never put back.
            if "generated" not in rec and slug in SEED_GENERATED:
                rec["generated"] = list(SEED_GENERATED[slug])
            for f in FIELDS:
                rec.setdefault(f, default(f))
            self._data[slug] = rec

    def _save(self) -> None:
        jsonstore.save(self._path, self._data)

    def get(self, slug: str) -> dict | None:
        with self._lock:
            rec = self._data.get(slug)
            return dict(rec) if rec else None

    def ensure(self, name: str, initial: dict | None = None, **fields) -> dict:
        """Look up a project by name or slug, creating it if it's new.

        `fields` are written whether the project is new or not; `initial` only
        when this call creates it. Filing a thread under a project passes the
        thread's directory as `initial` -- passed as a field, `!project
        Silkworm` from a fresh thread replaced Silkworm's whole scope (cwd,
        repo, branch) with nothing, and auto-merge would have run in an empty
        folder.
        """
        with self._lock:
            slug = self._resolve(name)
            rec = self._data.get(slug)
            if rec is None:
                rec = {f: default(f) for f in FIELDS}
                rec.update(slug=slug, v=VERSION, title=(name or slug).strip(),
                           created=time.time(), updated=time.time())
                rec.update({k: v for k, v in (initial or {}).items()
                            if k in FIELDS and k not in ("slug", "created")})
                self._data[slug] = rec
                log.info("project %s created (%s)", slug, rec["title"])
            rec.update({k: v for k, v in fields.items()
                        if k in FIELDS and k not in ("slug", "created")})
            rec["updated"] = time.time()
            self._save()
            return dict(rec)

    def register_if_absent(self, slug: str, title: str, scope: dict) -> dict | None:
        """Create the project `slug` with this title and scope if -- and only
        if -- nothing answers to it yet: not the slug, and not a title that
        resolves to another project. Returns the new record, or None when one
        was already there, which is left exactly as it is (not even its
        `updated` stamp moves). For records Silkworm itself needs to exist,
        made at every startup: a scope you have since changed is yours."""
        with self._lock:
            if slug in self._data or self._resolve(title) in self._data:
                return None
            rec = {f: default(f) for f in FIELDS}
            rec.update(slug=slug, v=VERSION, title=title.strip(), scope=dict(scope or {}),
                       created=time.time(), updated=time.time())
            self._data[slug] = rec
            self._save()
            log.info("project %s registered (%s)", slug, title)
            return dict(rec)

    def _resolve(self, name: str) -> str:
        """The slug `name` means: a slug that exists, else an existing
        project's title, else the slug a new project would get.

        A rename changes the title and keeps the slug, so slugifying the new
        title alone names a project that does not exist -- `!project Japan
        2027` after renaming Asia Trip made a second, empty one. Called with
        the lock held.
        """
        slug = slugify(name)
        if slug in self._data:
            return slug
        want = " ".join((name or "").split()).casefold()
        for s, rec in self._data.items():
            if want and " ".join((rec.get("title") or "").split()).casefold() == want:
                return s
        return slug

    def slug_for(self, name: str) -> str:
        """`ensure`'s lookup without writing anything."""
        with self._lock:
            return self._resolve(name)

    def update(self, slug: str, **fields) -> dict | None:
        """Write fields on a project that exists; None if it does not.

        `ensure` creates what it cannot find, which is right for a person
        naming a project and wrong for bookkeeping about one: a record written
        for a project since removed would bring it back, empty.
        """
        with self._lock:
            rec = self._data.get(slug)
            if rec is None:
                return None
            rec.update({k: v for k, v in fields.items()
                        if k in FIELDS and k not in ("slug", "created")})
            rec["updated"] = time.time()
            self._save()
            return dict(rec)

    def set_archived(self, slug: str, archived: bool) -> bool:
        with self._lock:
            rec = self._data.get(slug)
            if not rec:
                return False
            rec["archived"] = bool(archived)
            rec["updated"] = time.time()
            self._save()
            return True

    def all(self, include_archived: bool = True) -> list[dict]:
        with self._lock:
            out = [dict(r) for r in self._data.values()
                   if include_archived or not r.get("archived")]
        return sorted(out, key=lambda r: r.get("title", "").lower())

    def home(self, slug: str, create: bool = False) -> Path | None:
        """The project's own directory, for projects that have no repo.

        A repo-backed project already has a home and a CLAUDE.md of its own,
        which belongs to you -- we never write there.
        """
        rec = self.get(slug or "")
        if not rec:
            return None
        if (rec.get("scope") or {}).get("repo"):
            return None
        path = Path((rec.get("scope") or {}).get("cwd") or (PROJECT_ROOT / slug))
        # Ask the filesystem, not the record. `!project` sets cwd from the
        # thread and leaves `repo` empty, so a project pointed at a real
        # checkout looked repo-less -- and set_brief would have replaced a
        # hand-written CLAUDE.md with a 150-word generated one. A directory
        # that is itself a repo is yours, however the record was filled in.
        if (path / ".git").exists():
            return None
        if create:
            path.mkdir(parents=True, exist_ok=True)
            # By slug, re-read under the lock. Through ensure(title) a
            # renamed project's new title slugified to a project that did not
            # exist, which was created holding the cwd -- and this one, still
            # without one, made the same miss on every later call.
            with self._lock:
                live = self._data.get(slug)
                if live is not None and not (live.get("scope") or {}).get("cwd"):
                    live["scope"] = {**(live.get("scope") or {}), "cwd": str(path)}
                    live["updated"] = time.time()
                    self._save()
        return path

    def brief_path(self, slug: str, create: bool = False) -> Path | None:
        home = self.home(slug, create=create)
        return (home / "CLAUDE.md") if home else None

    def set_brief(self, slug: str, text: str) -> dict | None:
        """Write the brief to the project's CLAUDE.md.

        Claude Code loads CLAUDE.md from the working directory by itself, so
        the file *is* the injection -- nothing needs to put it in a prompt.
        """
        rec = self.get(slug or "")
        if not rec:
            return None
        path = self.brief_path(slug, create=True)
        if not path:
            log.info("project %s is repo-backed; its CLAUDE.md is yours, not ours", slug)
            return rec
        body = (text or "").strip()
        # The pointer is structural, not part of the brief the model rewrites,
        # so it survives every rewrite -- including one that returns nothing.
        tail = f"\n{POINTER}\n" if (path.parent / LOGISTICS).exists() else ""
        if body or tail:
            head = f"# {rec['title']}\n\n"
            path.write_text(f"{head}{body}\n{tail}" if body else f"{head}{tail.lstrip()}")
        elif path.exists():
            path.unlink()
        with self._lock:
            r = self._data.get(slug)
            r["brief_at"] = time.time()
            r["updated"] = time.time()
            self._save()
            return dict(r)

    def brief_for(self, slug: str) -> str:
        path = self.brief_path(slug)
        if not path or not path.exists():
            return ""
        text = path.read_text()
        # strip the title heading and the structural pointer we write, so
        # reading it back round-trips and a rewrite never sees its own scaffold
        text = re.sub(r"\A#\s+.*\n+", "", text)
        return text.replace(POINTER, "").strip()

    def logistics_path(self, slug: str, create: bool = False) -> Path | None:
        home = self.home(slug, create=create)
        return (home / LOGISTICS) if home else None

    def add_fact(self, slug: str, heading: str, body: str, source: str = "") -> Path | None:
        """Append a dated entry to the project's logistics file.

        Append-only on purpose. Unlike the brief, these are facts with no
        shelf life -- a flight number does not get summarised, and a past trip
        is history rather than clutter.
        """
        path = self.logistics_path(slug, create=True)
        if not path:
            return None
        new = not path.exists()
        with self._lock:
            with path.open("a") as fh:
                if new:
                    rec = self._data.get(slug) or {}
                    fh.write(f"# {rec.get('title', slug)} -- logistics\n\n"
                             "Facts drawn from mail. Append-only.\n")
                fh.write(f"\n## {heading}\n")
                if source:
                    fh.write(f"*{source}*\n")
                fh.write(f"\n{body.strip()}\n")
        if new:
            # first entry: make sure CLAUDE.md starts pointing here
            self.set_brief(slug, self.brief_for(slug))
        return path

    def label_for(self, slug: str) -> str:
        """The Gmail label this project draws facts from.

        Defaults to the project's title, because a label named after the
        project is what you would already have. Set `mail_label` to point at a
        differently-named one.
        """
        rec = self.get(slug or "")
        if not rec or rec.get("archived"):
            return ""
        return (rec.get("mail_label") or rec.get("title") or "").strip()

    def mail_targets(self) -> list[tuple[str, str, Path]]:
        """(slug, label, directory) for every project mail can be filed into.

        Only projects with a directory of our own qualify: a repo-backed
        project's files belong to you, and we do not write there.
        """
        out = []
        for rec in self.all(include_archived=False):
            label = self.label_for(rec["slug"])
            home = self.home(rec["slug"])
            if label and home:
                out.append((rec["slug"], label, home))
        return out

    def scope_for(self, slug: str) -> dict:
        """The default scope a task filed under this project inherits."""
        rec = self.get(slug or "")
        return dict((rec or {}).get("scope") or {})


def parse_at(text: str) -> str:
    """Normalise "2am", "02:00", "14:30" to "HH:MM". Raises ValueError."""
    t = (text or "").strip().lower().replace(".", ":")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if not m:
        raise ValueError(f"could not read a time from {text!r} (try 02:00 or 2am)")
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm:
        if not 1 <= hour <= 12:
            raise ValueError("a 12-hour time needs an hour between 1 and 12")
        hour = (hour % 12) + (12 if ampm == "pm" else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"{text!r} is not a time of day")
    return f"{hour:02d}:{minute:02d}"


def unready(rec) -> str:
    """Why this project may not take unsupervised work, or "" if it may.

    Unsupervised means nobody is in the loop between a task starting and its
    result reaching the base: nightly ideation, and implementor tasks the
    queue runs on its own. That is only safe where the project can prove a
    change (a test command) and decides for itself what happens to proven
    work (auto-merge). Without those, an unattended task ends as a branch
    nothing tests and nothing merges -- which is how sixty of them piled up.
    """
    if not rec:
        return "is not a registered project"
    missing = [what for what, have in (("a test command", (rec.get("test_cmd") or "").strip()),
                                       ("auto-merge", rec.get("auto_merge")))
               if not have]
    return ("needs " + " and ".join(missing) + " before it can take unsupervised work"
            if missing else "")


def due_for_ideation(records, now) -> list:
    """Slugs whose scheduled time has passed today and that have not run.

    Compares against the wall clock rather than tracking a timer, so a bot
    that was asleep or restarted at 02:00 still runs the pass when it comes
    back -- and one that already ran today does not run again.
    """
    today = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    due = []
    for rec in records:
        at = (rec.get("ideate_at") or "").strip()
        if not at or rec.get("archived") or rec.get("ideate_on") == today:
            continue
        if unready(rec):                 # switched on before the rule existed
            continue
        if hhmm >= at:
            due.append(rec["slug"])
    return due

BRIEF_PROMPT = """Keep a short standing brief for a project, for whoever picks \
up the next piece of work on it.

Current brief (rewrite it; do not append):
{brief}

What just happened:
{event}

Return the updated brief and nothing else. Keep only durable things: decisions \
made, constraints, preferences, names and facts that will still matter next \
week. Drop anything transient, anything already obvious from the task itself, \
and anything superseded by what just happened. Aim for under 150 words. If \
nothing durable changed, return the current brief unchanged."""


def summarise(projects: list[dict], tasks: list[dict],
              needs_attention: tuple, hidden=()) -> list[dict]:
    """Projects with their task counts, so a picker shows where the work is.

    `hidden` is slugs left out on purpose -- archived ones, when the caller
    listed only the live projects. Without it their tasks brought them back
    as if they had never been registered, and archiving hid nothing.
    """
    by_slug: dict[str, dict] = {}
    for t in tasks:
        slug = t.get("project") or ""
        if not slug:
            continue
        row = by_slug.setdefault(slug, {"open": 0, "needs": 0, "total": 0,
                                        "proposed": 0})
        row["total"] += 1
        if t.get("state") in needs_attention:
            row["needs"] += 1
        # Counted on its own as well as inside `needs`: it is the one pile a
        # nightly pass can add to by itself, so a project stops being looked
        # at while it is deep, and the panel should be able to say so.
        if t.get("state") == "proposed":
            row["proposed"] += 1
        if t.get("state") in ("proposed", "queued", "running", "blocked",
                              "awaiting_approval", "needs_input"):
            row["open"] += 1
    out = []
    for p in projects:
        counts = by_slug.get(p["slug"],
                             {"open": 0, "needs": 0, "total": 0, "proposed": 0})
        out.append({**p, **counts})
    # Anything filed under a project that was never registered still shows up,
    # rather than vanishing because its record is missing.
    known = {p["slug"] for p in projects} | set(hidden)
    for slug, counts in by_slug.items():
        if slug not in known:
            out.append({"slug": slug, "title": slug, "scope": {},
                        "archived": False, **counts})
    return sorted(out, key=lambda r: (-r["needs"], -r["open"], r["title"].lower()))

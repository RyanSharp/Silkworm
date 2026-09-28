"""The Slack Home tab: the task board, reachable wherever Slack is.

The dashboard binds loopback, so away from the house the board was out of
reach -- and the board is where decisions wait. This puts the "what needs me"
view in the app's Home tab instead: no server to expose, no second login, and
the bot already holds the socket that publishes it.

Deliberately the attention view, not the backlog. It shows what is waiting on
you, what is running, and what is scheduled to wake, and nothing else; the
dashboard remains the place to dig.

Every button routes through the same `handle_tasks` the dashboard calls, so
the lifecycle rules, the landing that approval starts, and the stop that
cancelling a running task performs are one code path rather than two that can
drift. This module only renders and relays.

Split in two so the half with the rules is testable without Slack: `render`
is pure and returns a view; `register` wires it to Bolt.
"""

import json
import logging
import threading
import time

import slacklinks
import tasks

log = logging.getLogger("silkworm.home")

#: Slack refuses a Home view over 100 blocks. Whatever does not fit is counted
#: and pointed at, never silently dropped.
MAX_BLOCKS = 100
#: Per task on the board: a section and its buttons, plus a context line.
MAX_ATTENTION = 25
MAX_LISTED = 12          # running / watching, one line each

#: Most urgent first: approval can land work, input is something asked of you.
ORDER = (tasks.AWAITING_APPROVAL, tasks.NEEDS_INPUT, tasks.FAILED, tasks.PROPOSED)
HEADINGS = {
    tasks.AWAITING_APPROVAL: ":eyes: Awaiting approval",
    tasks.NEEDS_INPUT: ":speech_balloon: Needs your input",
    tasks.FAILED: ":x: Failed",
    tasks.PROPOSED: ":bulb: Proposed",
}

#: The same buttons the dashboard's taskButtons() offers, state for state.
#: (label, action, style, confirm-text or None). "rework"/"answer" open a modal.
BUTTONS = {
    tasks.PROPOSED: [("Accept", "accept", "primary", None),
                     ("Dismiss", "dismiss", None, "Dismiss this proposal?")],
    tasks.AWAITING_APPROVAL: [
        ("Approve", "approve", "primary",
         "Approve and land this branch on the base?"),
        ("Send back…", "rework", None, None),
        ("Dismiss", "dismiss", None, "Dismiss this task? Its branch is left as it is.")],
    tasks.NEEDS_INPUT: [("Answer…", "answer", "primary", None),
                        ("Dismiss", "dismiss", None, "Dismiss this task?")],
    tasks.FAILED: [("Retry", "retry", "primary", None),
                   ("Dismiss", "dismiss", None, "Dismiss this failure?")],
    tasks.QUEUED: [("Cancel", "cancel", None, "Cancel this task?")],
    tasks.BLOCKED: [("Cancel", "cancel", None, "Cancel this task?")],
}

#: Actions that are a plain call to handle_tasks; the others open a modal.
DIRECT = ("accept", "dismiss", "approve", "retry", "cancel")
MODAL_CALLBACK = "home_rework"


# --- rendering (pure) ---------------------------------------------------------

def esc(text) -> str:
    """Escape for Slack mrkdwn. A goal is arbitrary text; `<` in one would
    otherwise be read as the start of a link or a mention."""
    return (str(text or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def clip(text, width: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[:width - 1].rstrip() + "…"


def label(task: dict) -> str:
    return (task.get("title") or "").strip() or \
        (task.get("goal") or "").strip().split("\n", 1)[0] or task.get("id", "?")


def ago(ts, now: float) -> str:
    """Age of a past timestamp. A missing one is said to be unknown rather
    than rendered as fifty-odd years."""
    if not ts:
        return "?"
    s = max(0, now - ts)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{int(s // size)}{unit} ago"
    return "just now"


def until(seconds) -> str:
    """Time to a wake-up. Overdue says so, rather than clamping to a small
    positive number that reads as imminent."""
    s = int(seconds or 0)
    if s <= 0:
        return "due now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"in {s // size}{unit}"
    return f"in {s}s"


def thread_url(thread: str, base: str) -> str:
    """A link to a task's Slack thread, from its `channel:ts` key."""
    if not base:
        return ""
    return slacklinks.for_key(thread or "", base)


def _text(t: str) -> dict:
    """A mrkdwn object within Slack's 3000 characters. Cut at a line break
    where there is one: escaping can grow text fivefold, and a blind cut can
    land inside an `&amp;` or a `<url|name>` link and render as debris."""
    if len(t) > 3000:
        cut = t.rfind("\n", 0, 2990)
        t = (t[:cut] if cut > 0 else t[:2990]) + "\n…"
    return {"type": "mrkdwn", "text": t}


def _button(text, action, tid, style=None, confirm=None, url=None) -> dict:
    b = {"type": "button", "text": {"type": "plain_text", "text": text[:75]},
         "action_id": f"home_{action}", "value": tid}
    if style:
        b["style"] = style
    if url:
        b["url"] = url
    if confirm:
        b["confirm"] = {"title": {"type": "plain_text", "text": text.rstrip("…")[:100]},
                        "text": _text(confirm),
                        "confirm": {"type": "plain_text", "text": "Yes"},
                        "deny": {"type": "plain_text", "text": "No"}}
    return b


def _detail(task: dict) -> str:
    """The one thing worth reading before you decide: the review, or why it
    stopped."""
    state = task.get("state")
    review = (task.get("result") or {}).get("review") or {}
    if state == tasks.AWAITING_APPROVAL and review:
        lines = []
        if review.get("summary"):
            lines.append(f"_Review:_ {esc(clip(review['summary'], 280))}")
        findings = review.get("findings") or []
        for f in findings[:3]:
            lines.append(f"• {esc(clip(f, 240))}")
        if len(findings) > 3:
            lines.append(f"_…and {len(findings) - 3} more finding"
                         f"{'s' if len(findings) - 3 != 1 else ''}_")
        if task.get("verified") is True:
            lines.append(":test_tube: tests pass")
        elif task.get("verified") is False:
            lines.append(":x: tests failed")
        return "\n".join(lines)
    events = task.get("events") or []
    if state in (tasks.FAILED, tasks.NEEDS_INPUT) and events:
        detail = (events[-1] or {}).get("detail") or ""
        return f"_{esc(clip(detail, 300))}_" if detail else ""
    if state == tasks.PROPOSED:
        goal = (task.get("goal") or "").strip()
        first, _, rest = goal.partition("\n")
        return esc(clip(rest or "", 400)) if rest.strip() else ""
    return ""


def _task_blocks(task: dict, now: float, base_url: str) -> list[dict]:
    tid = task.get("id", "")
    meta = [f"`{tid}`"]
    if task.get("project"):
        meta.append(esc(task["project"]))
    if task.get("role") and task.get("role") != "assistant":
        meta.append(esc(task["role"]))
    meta.append(ago(task.get("updated") or task.get("created"), now))
    body = f"*{esc(clip(label(task), 150))}*"
    detail = _detail(task)
    if detail:
        body += "\n" + detail
    blocks = [{"type": "section", "block_id": f"t:{tid}", "text": _text(body)},
              {"type": "context", "elements": [_text(" · ".join(meta))]}]
    # The value carries the state the button was drawn for; see seen_state().
    stamp = f"{tid}|{task.get('state', '')}"
    buttons = [_button(*b[:2], stamp, style=b[2], confirm=b[3])
               for b in BUTTONS.get(task.get("state"), [])]
    url = thread_url(task.get("thread", ""), base_url)
    if url:
        buttons.append(_button("Thread", "thread", tid, url=url))
    if buttons:
        blocks.append({"type": "actions", "block_id": f"a:{tid}",
                       "elements": buttons[:25]})
    return blocks


def render(all_tasks, *, now: float, watching=(), unmerged: str = "",
           base_url: str = "", notice: str = "", allowed: bool = True) -> dict:
    """The Home view for one user.

    `allowed=False` renders nothing of the board: task goals and reviewer
    findings are private to whoever runs the bot, and the Home tab is visible
    to anyone in the workspace who opens the app.
    """
    if not allowed:
        return {"type": "home", "blocks": [{"type": "section", "text": _text(
            "This bot's task board is only visible to the people on its allowlist.")}]}

    items = list(all_tasks)
    attention = sorted(
        (t for t in items if t.get("state") in tasks.NEEDS_ATTENTION),
        key=lambda t: (ORDER.index(t["state"]) if t["state"] in ORDER else 9,
                       -(t.get("updated") or t.get("created") or 0)))
    running = sorted((t for t in items if t.get("state") == tasks.RUNNING),
                     key=lambda t: -(t.get("updated") or 0))
    queued = [t for t in items if t.get("state") == tasks.QUEUED]

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Silkworm"}},
        {"type": "actions", "block_id": "home:top", "elements": [
            _button("Refresh", "refresh", "refresh")]},
        {"type": "context", "elements": [_text(
            f"*{len(attention)}* need you · *{len(running)}* running · "
            f"*{len(queued)}* queued · updated {time.strftime('%H:%M', time.localtime(now))}")]},
    ]
    if notice:
        blocks.insert(1, {"type": "section", "text": _text(notice)})

    # Needs you, grouped by state. Built into a separate list first so the
    # overflow line can say exactly how many did not fit.
    board, shown = [], 0
    current = None
    for t in attention:
        chunk = []
        if t["state"] != current:
            current = t["state"]
            count = sum(1 for x in attention if x["state"] == current)
            chunk += [{"type": "divider"},
                      {"type": "section", "text": _text(
                          f"*{HEADINGS.get(current, esc(current))}* ({count})")}]
        chunk += _task_blocks(t, now, base_url)
        # Room kept for the tail sections (running, watching, unmerged).
        if shown >= MAX_ATTENTION or len(blocks) + len(board) + len(chunk) > MAX_BLOCKS - 12:
            break
        board += chunk
        shown += 1
    if not attention:
        board = [{"type": "divider"}, {"type": "section", "text": _text(
            ":sparkles: Nothing needs you.")}]
    blocks += board
    if shown < len(attention):
        blocks.append({"type": "context", "elements": [_text(
            f"…and {len(attention) - shown} more waiting — "
            "the rest are on the dashboard.")]})

    def listing(title, rows):
        if not rows:
            return
        more = len(rows) - MAX_LISTED
        body = "\n".join(rows[:MAX_LISTED])
        if more > 0:
            body += f"\n_…and {more} more_"
        blocks.extend([{"type": "divider"},
                       {"type": "section", "text": _text(f"*{title}*\n{body}")}])

    def line(t, suffix):
        url = thread_url(t.get("thread", ""), base_url)
        name = esc(clip(label(t), 90))
        return f"• {f'<{url}|{name}>' if url else name} — {suffix}"

    listing(f":gear: Running ({len(running)})",
            [line(t, f"started {ago(t.get('updated'), now)}") for t in running])
    listing(f":alarm_clock: Watching ({len(watching)})",
            [line({"title": clip(w.get("goal"), 90), "thread": w.get("thread")},
                  until(w.get("in_s"))) for w in watching])
    if unmerged:
        blocks += [{"type": "divider"}, {"type": "context", "elements": [
            _text(f":warning: {esc(unmerged)} — land or drop them from the dashboard")]}]
    return {"type": "home", "blocks": blocks[:MAX_BLOCKS]}


def seen_state(value: str) -> tuple[str, str]:
    """(task id, the state it was in when its button was drawn).

    A published view stays as it was until it is published again, and the
    lifecycle lets a running task go back to queued or on to done. Clicked
    later, a button drawn for `failed` would requeue a task that is by then
    running -- a second agent in the same checkout -- and one drawn for
    `awaiting_approval` would close a rerun. So a click is only acted on if
    the task is still where you saw it.
    """
    tid, _, state = (value or "").partition("|")
    return tid, state


def rework_modal(task: dict, answer: bool) -> dict:
    """The notes prompt for Send back / Answer.

    An answer is required -- a blank one resumes the task with nothing to go
    on. Send-back notes are optional: the reviewer's findings are attached
    either way. Closing the modal abandons the action; "never mind" is not
    the same as "no notes".
    """
    tid = task.get("id", "")
    prompt = ("Your answer — the task resumes with it." if answer else
              "Anything to add? The reviewer's findings are included "
              "automatically; leave blank to send back with just those.")
    return {
        "type": "modal", "callback_id": MODAL_CALLBACK,
        "private_metadata": json.dumps({"id": tid, "answer": answer,
                                        "seen": task.get("state", "")}),
        "title": {"type": "plain_text", "text": "Answer" if answer else "Send back"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "section", "text": _text(f"*{esc(clip(label(task), 200))}*\n`{tid}`")},
            {"type": "input", "block_id": "notes", "optional": not answer,
             "label": {"type": "plain_text", "text": "Answer" if answer else "Notes"},
             "element": {"type": "plain_text_input", "action_id": "notes",
                         "multiline": True},
             "hint": {"type": "plain_text", "text": prompt[:150]}},
        ],
    }


# --- wiring -------------------------------------------------------------------

class Home:
    """Publishes the view and relays clicks. `call` is bot.handle_tasks."""

    NOTICE_S = 90      # how long a result line stays at the top of the tab
    UNMERGED_S = 300   # the survey asks git about every branch; cache it

    def __init__(self, *, store, call, allowed_users, base_url="",
                 watching=None, unmerged=None):
        self.store = store
        self.call = call
        self.allowed_users = set(allowed_users or ())
        self.base_url = base_url
        self._watching = watching or (lambda: [])
        self._unmerged = unmerged or (lambda: "")
        self._notices: dict[str, tuple[str, float]] = {}
        self._cache = ("", 0.0)
        self._lock = threading.Lock()

    def allowed(self, user: str) -> bool:
        return not self.allowed_users or user in self.allowed_users

    def unmerged(self) -> str:
        with self._lock:
            line, at = self._cache
        if time.time() - at < self.UNMERGED_S:
            return line
        try:
            line = self._unmerged() or ""
        except Exception:
            log.exception("unmerged survey failed")
            line = ""
        with self._lock:
            self._cache = (line, time.time())
        return line

    def note(self, user: str, text: str) -> None:
        with self._lock:
            self._notices[user] = (text, time.time())

    def view_for(self, user: str) -> dict:
        now = time.time()
        if not self.allowed(user):
            return render((), now=now, allowed=False)
        with self._lock:
            text, at = self._notices.get(user, ("", 0))
            if now - at > self.NOTICE_S:
                self._notices.pop(user, None)
                text = ""
        try:
            watching = self._watching() or []
        except Exception:
            log.exception("watching list failed")
            watching = []
        items = [dict(r, id=tid) for tid, r in self.store.all().items()]
        return render(items, now=now, watching=watching, unmerged=self.unmerged(),
                      base_url=self.base_url, notice=text)

    def publish(self, client, user: str) -> None:
        try:
            client.views_publish(user_id=user, view=self.view_for(user))
        except Exception:
            log.exception("could not publish the Home tab for %s", user)

    # -- handlers --

    def on_opened(self, event, client):
        if event.get("tab") == "home":
            self.publish(client, event["user"])

    def on_refresh(self, ack, body, client):
        ack()
        self.publish(client, body["user"]["id"])

    def on_link(self, ack):
        ack()        # a URL button still sends an action; it needs no reply

    def on_direct(self, ack, body, client):
        ack()
        user = body["user"]["id"]
        act = body["actions"][0]
        action = act["action_id"].removeprefix("home_")
        tid, seen = seen_state(act.get("value", ""))
        if not self.allowed(user):
            return self.publish(client, user)
        if action not in DIRECT:
            return
        if self.moved_on(user, tid, seen):
            return self.publish(client, user)
        try:
            r = self.call({"action": action, "id": tid, "by": f"slack:{user}"})
        except Exception as e:
            log.exception("home action %s on %s failed", action, tid)
            r = {"ok": False, "error": str(e)}
        self.note(user, self.outcome(action, tid, r))
        self.publish(client, user)

    def on_modal(self, ack, body, client):
        ack()
        user = body["user"]["id"]
        act = body["actions"][0]
        if not self.allowed(user):
            return self.publish(client, user)
        tid, seen = seen_state(act.get("value", ""))
        task = self.store.get(tid)
        if not task:
            self.note(user, ":warning: That task no longer exists.")
            return self.publish(client, user)
        if self.moved_on(user, tid, seen):
            return self.publish(client, user)
        task = dict(task, id=tid)
        try:
            client.views_open(trigger_id=body["trigger_id"],
                              view=rework_modal(task, act["action_id"] == "home_answer"))
        except Exception:
            log.exception("could not open the send-back modal")

    def on_submit(self, ack, body, client, view):
        user = body["user"]["id"]
        if not self.allowed(user):
            return ack()
        meta = json.loads(view.get("private_metadata") or "{}")
        notes = (((view.get("state") or {}).get("values") or {})
                 .get("notes", {}).get("notes", {}).get("value") or "").strip()
        if meta.get("answer") and not notes:
            return ack(response_action="errors",
                       errors={"notes": "An answer is needed to resume the task."})
        ack()
        tid = meta.get("id", "")
        # The form can sit open for minutes; check again on the way out.
        if self.moved_on(user, tid, meta.get("seen", "")):
            return self.publish(client, user)
        try:
            r = self.call({"action": "rework", "id": tid, "notes": notes,
                           "by": f"slack:{user}"})
        except Exception as e:
            log.exception("home rework on %s failed", tid)
            r = {"ok": False, "error": str(e)}
        self.note(user, self.outcome("answer" if meta.get("answer") else "rework", tid, r))
        self.publish(client, user)

    def moved_on(self, user: str, tid: str, seen: str) -> bool:
        """True, with a note to the user, if the task left `seen`."""
        now = (self.store.get(tid) or {}).get("state")
        if not seen or now == seen:
            return False
        self.note(user, f":arrows_counterclockwise: `{esc(tid)}` has moved on — it "
                        f"is {esc(now or 'gone')} now, not {esc(seen)}. Nothing was done.")
        return True

    @staticmethod
    def outcome(action: str, tid: str, r: dict) -> str:
        if not r.get("ok"):
            return f":warning: Couldn't {action} `{tid}`: {esc(r.get('error') or 'refused')}"
        done = {"accept": "Accepted", "dismiss": "Dismissed", "approve": "Approved",
                "retry": "Requeued", "cancel": "Cancelled", "rework": "Sent back",
                "answer": "Answered"}.get(action, action)
        note = f" — {esc(r['note'])}" if r.get("note") else ""
        return f":white_check_mark: {done} `{tid}`{note}"


def register(app, home: Home) -> None:
    app.event("app_home_opened")(home.on_opened)
    app.action("home_refresh")(home.on_refresh)
    app.action("home_thread")(home.on_link)
    for action in DIRECT:
        app.action(f"home_{action}")(home.on_direct)
    app.action("home_rework")(home.on_modal)
    app.action("home_answer")(home.on_modal)
    app.view(MODAL_CALLBACK)(home.on_submit)

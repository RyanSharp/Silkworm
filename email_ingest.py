"""Watch a Gmail inbox and propose tasks for mail that wants a person.

Read-only by construction. The mailbox is opened readonly and bodies are
fetched with BODY.PEEK, so nothing is ever marked as read, moved or deleted --
watching your inbox must not change what you see when you next open it.

Everything it finds lands as a `proposed` task, never `queued`. A triage model
is a guess, and the accept/dismiss gate is what stops a wrong guess from
becoming work or from burying the list that is supposed to reach empty.

Only sender, subject, date and a short snippet are sent to the model. Full
bodies stay on the machine: enough to judge whether something is actionable,
without shipping the contents of your mail anywhere.
"""

import email
import email.utils
import imaplib
import json
import logging
import re
import subprocess
from email.header import decode_header, make_header

log = logging.getLogger("silkworm.email")

SNIPPET_CHARS = 400
DEFAULT_MAILBOX = "INBOX"

TRIAGE_PROMPT = """You are triaging a person's inbox. Below are unread messages.

Flag only mail that genuinely needs *them* specifically to do or decide \
something: a direct question, a deadline, a bill or payment problem, an \
account or security issue, a reply someone is waiting on, a booking or \
appointment to confirm.

Do not flag newsletters, marketing, notifications, receipts for things that \
already worked, social updates, or anything a person could ignore for a month \
with no consequence. When unsure, do not flag it.

Messages:
{items}

Reply with only a json array, one object per message you are flagging:
[{{"id": "<the id shown>", "title": "<short imperative action>", \
"why": "<one clause>"}}]
Return [] if none qualify."""


def _decode(value: str) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def _snippet(msg) -> str:
    """First readable text of a message, trimmed."""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_payload(decode=True) or b""
                    break
            else:
                body = b""
        else:
            body = msg.get_payload(decode=True) or b""
        text = body.decode(msg.get_content_charset() or "utf-8", "replace")
    except Exception:
        text = ""
    text = re.sub(r"https?://\S+", "[link]", text)      # links add noise, not signal
    text = re.sub(r"\s+", " ", text).strip()
    return text[:SNIPPET_CHARS]


def imap_fetch(host: str, user: str, password: str, mailbox: str,
               since_uid: int, limit: int) -> tuple[list[dict], int]:
    """Unread messages newer than `since_uid`, without touching their state."""
    items: list[dict] = []
    high = since_uid
    conn = imaplib.IMAP4_SSL(host)
    try:
        conn.login(user, password)
        # readonly: opening a mailbox read/write would clear \Recent flags.
        conn.select(mailbox, readonly=True)
        criteria = f"(UNSEEN UID {since_uid + 1}:*)" if since_uid else "(UNSEEN)"
        typ, data = conn.uid("SEARCH", None, criteria)
        if typ != "OK":
            return [], since_uid
        uids = [int(u) for u in (data[0] or b"").split()]
        uids = [u for u in uids if u > since_uid]
        for uid in sorted(uids)[-limit:]:
            # PEEK so the message is not marked read behind the user's back.
            typ, raw = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if typ != "OK" or not raw or not isinstance(raw[0], tuple):
                continue
            msg = email.message_from_bytes(raw[0][1])
            items.append({
                "uid": uid,
                "id": msg.get("Message-ID", f"uid-{uid}").strip("<>"),
                "from": _decode(msg.get("From", "")),
                "subject": _decode(msg.get("Subject", "(no subject)")),
                "date": msg.get("Date", ""),
                "snippet": _snippet(msg),
            })
            high = max(high, uid)
        for u in uids:
            high = max(high, u)      # advance past mail we skipped, too
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return items, high


def _render(items: list[dict]) -> str:
    out = []
    for it in items:
        out.append(f"--- id: {it['id']}\nfrom: {it['from']}\n"
                   f"subject: {it['subject']}\ndate: {it['date']}\n"
                   f"{it['snippet']}")
    return "\n\n".join(out)


def triage(items: list[dict], *, binary: str, model: str, env: dict,
           cwd: str = "") -> list[dict]:
    """Ask a cheap model which of these want a person. Fails closed: on any
    trouble reading the answer, nothing is flagged rather than everything."""
    if not items:
        return []
    try:
        proc = subprocess.run(
            [binary, "-p", "--model", model, "--output-format", "text"],
            input=TRIAGE_PROMPT.format(items=_render(items)),
            capture_output=True, text=True, timeout=180, cwd=cwd or None, env=env)
        text = proc.stdout
    except Exception:
        log.exception("email triage failed")
        return []
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        log.warning("email triage returned no json array; flagging nothing")
        return []
    try:
        flagged = json.loads(m.group(0))
    except json.JSONDecodeError:
        log.warning("email triage json did not parse; flagging nothing")
        return []
    by_id = {it["id"]: it for it in items}
    out = []
    for f in flagged if isinstance(flagged, list) else []:
        if not isinstance(f, dict):
            continue
        src = by_id.get(str(f.get("id", "")))
        if not src:                       # a hallucinated id is not a message
            log.warning("triage flagged an unknown message id %r", f.get("id"))
            continue
        out.append({**src, "title": str(f.get("title") or src["subject"])[:70],
                    "why": str(f.get("why", ""))[:200]})
    return out


def ingest(task_store, state, *, host, user, password, mailbox=DEFAULT_MAILBOX,
           limit=25, binary="claude", model="haiku", env=None, cwd="",
           fetch=imap_fetch) -> dict:
    """One pass: read new unread mail, propose tasks for what needs a person.

    `state` is a dict persisted by the caller; the uid watermark lives in it so
    a message is only ever considered once.
    """
    since = int(state.get("uid", 0) or 0)
    seen = set(state.get("seen") or [])
    items, high = fetch(host, user, password, mailbox, since, limit)
    fresh = [it for it in items if it["id"] not in seen]
    flagged = triage(fresh, binary=binary, model=model, env=env or {}, cwd=cwd)

    created = []
    for f in flagged:
        task = task_store.create(
            f"Email from {f['from']}: {f['subject']}\n\n{f['snippet']}",
            title=f["title"],
            state="proposed",          # never straight to work; triage is a guess
            driver="queue",
            source="email",
            source_ref=f["id"],
        )
        created.append(task["id"])
    state["uid"] = max(since, high)
    # Remember what was examined, so a message is not re-proposed if the uid
    # watermark ever moves backwards (a rebuilt mailbox re-numbers uids).
    state["seen"] = (list(seen) + [it["id"] for it in fresh])[-500:]
    log.info("email: %d new, %d proposed", len(fresh), len(created))
    return {"scanned": len(fresh), "proposed": len(created), "tasks": created,
            "uid": state["uid"]}

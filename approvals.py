"""Slack-button tool approvals.

When the bot runs Claude in CLAUDE_APPROVAL_MODE=slack, a PreToolUse hook
(approval_hook.py) POSTs each tool request to the local HTTP server here.
We post an Approve/Deny message in the originating Slack thread and block the
hook's request until someone clicks or the timeout passes (deny by default).
"""

import json
import logging
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("silkworm.approvals")


def describe_tool(name: str, tool_input: dict) -> str:
    if name == "Bash":
        detail = tool_input.get("command", "")
    elif name in ("Read", "Write", "Edit", "NotebookEdit"):
        detail = tool_input.get("file_path", "")
    elif name in ("Glob", "Grep"):
        detail = tool_input.get("pattern", "")
    elif name in ("WebFetch",):
        detail = tool_input.get("url", "")
    elif name in ("WebSearch",):
        detail = tool_input.get("query", "")
    elif name == "Task":
        detail = tool_input.get("description", "")
    else:
        detail = json.dumps(tool_input)
    return detail[:400]


class ApprovalManager:
    def __init__(self, client, *, port: int, timeout: float,
                 auto_allow: set[str], allowed_users: set[str],
                 resolve_thread):
        """resolve_thread(session_id) -> (channel, thread_ts) | None"""
        self.client = client
        self.port = port
        self.timeout = timeout
        self.auto_allow = auto_allow
        self.allowed_users = allowed_users
        self.resolve_thread = resolve_thread
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    # -- HTTP side (called by approval_hook.py) -----------------------------

    def start(self) -> None:
        manager = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def do_POST(self):
                if self.path != "/approve":
                    self.send_response(404)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", 0))
                try:
                    payload = json.loads(self.rfile.read(length))
                except Exception:
                    payload = {}
                answer = manager.handle_request(payload)
                body = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True,
                         name="approval-http").start()
        log.info("approval server listening on 127.0.0.1:%d", self.port)

    def handle_request(self, payload: dict) -> dict:
        tool_name = payload.get("tool_name", "unknown")
        tool_input = payload.get("tool_input") or {}
        session_id = payload.get("session_id")

        if tool_name in self.auto_allow:
            return {"decision": "allow", "reason": f"{tool_name} is auto-allowed"}

        thread = self.resolve_thread(session_id) if session_id else None
        if not thread:
            log.warning("approval request for unknown session %s -> deny", session_id)
            return {"decision": "deny", "reason": "no Slack thread mapped to this session"}
        channel, thread_ts = thread

        approval_id = uuid.uuid4().hex[:12]
        entry = {"event": threading.Event(), "decision": None, "user": None}
        with self._lock:
            self._pending[approval_id] = entry

        detail = describe_tool(tool_name, tool_input)
        try:
            msg = self.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"Claude wants to run {tool_name}: {detail}",
                blocks=[
                    {"type": "section",
                     "text": {"type": "mrkdwn",
                              "text": f":lock: Claude wants to run *{tool_name}*\n```{detail}```"}},
                    {"type": "actions", "block_id": f"approval:{approval_id}",
                     "elements": [
                         {"type": "button", "action_id": "approval_approve",
                          "style": "primary", "value": approval_id,
                          "text": {"type": "plain_text", "text": "Approve"}},
                         {"type": "button", "action_id": "approval_deny",
                          "style": "danger", "value": approval_id,
                          "text": {"type": "plain_text", "text": "Deny"}},
                     ]},
                ],
            )
            entry["channel"] = channel
            entry["msg_ts"] = msg["ts"]
        except Exception:
            log.exception("failed to post approval message")
            with self._lock:
                self._pending.pop(approval_id, None)
            return {"decision": "deny", "reason": "could not post approval message"}

        entry["event"].wait(self.timeout)
        with self._lock:
            self._pending.pop(approval_id, None)

        if entry["decision"] == "allow":
            return {"decision": "allow", "reason": f"approved in Slack by <@{entry['user']}>"}
        if entry["decision"] == "deny":
            return {"decision": "deny", "reason": f"denied in Slack by <@{entry['user']}>"}

        # timeout — clean up the buttons so they can't be clicked later
        self._finalize_message(entry, f":hourglass: *{tool_name}* request expired — denied.")
        return {"decision": "deny", "reason": f"no decision within {int(self.timeout)}s"}

    # -- Slack side (button clicks) ------------------------------------------

    def register(self, app) -> None:
        app.action("approval_approve")(self._make_action_handler("allow"))
        app.action("approval_deny")(self._make_action_handler("deny"))

    def _make_action_handler(self, decision: str):
        def handler(ack, body, client):
            ack()
            user = body["user"]["id"]
            approval_id = body["actions"][0]["value"]
            with self._lock:
                entry = self._pending.get(approval_id)
            if entry is None:
                return
            if self.allowed_users and user not in self.allowed_users:
                client.chat_postEphemeral(
                    channel=entry["channel"], user=user,
                    thread_ts=body.get("message", {}).get("thread_ts"),
                    text="You're not authorized to approve tool calls.")
                return
            entry["decision"] = decision
            entry["user"] = user
            verdict = ":white_check_mark: Approved" if decision == "allow" else ":no_entry: Denied"
            self._finalize_message(entry, f"{verdict} by <@{user}>")
            entry["event"].set()
        return handler

    def _finalize_message(self, entry: dict, text: str) -> None:
        try:
            self.client.chat_update(
                channel=entry["channel"], ts=entry["msg_ts"], text=text,
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
            )
        except Exception:
            pass

"""Role templates: how a task is run, and whether its output gets checked.

A role is a reusable set of run settings a task references by name. Three
exist. `assistant` is today's Slack behaviour and is deliberately unchanged --
adding roles must not alter how a DM behaves. `implementor` does work and is
gated. `reviewer` checks that work with fresh context.

Fresh context is the entire point of the gate. A reviewer never resumes the
implementor's session: an independent reader is not invested in the reasoning
that produced the bug, which is why the pattern catches things self-review
misses. It is also read-only, and not merely asked to be -- it runs without
--dangerously-skip-permissions and with an explicit allowlist, so in headless
mode anything else is refused rather than prompted for.
"""

import json
import logging
import re

log = logging.getLogger("silkworm.roles")

# Tools a reviewer may use: read the tree, read history, run nothing else.
REVIEWER_TOOLS = "Read Grep Glob Bash(git diff:*) Bash(git log:*) Bash(git status:*)"

REVIEWER_SYSTEM = (
    "You are reviewing work another agent just completed. You did not do it and "
    "have no stake in it. Read the actual state of the repository rather than "
    "trusting the summary you are given.\n\n"
    "Judge only: does this achieve the stated goal, and is anything wrong, "
    "missing, or unsafe? Ignore style preferences.\n\n"
    "Finish your reply with a fenced json block, and nothing after it:\n"
    "```json\n"
    '{"ok": true|false, "summary": "one line", '
    '"findings": ["specific problem", "..."]}\n'
    "```\n"
    "ok=false only for something you would want a person to look at. An empty "
    "findings list with ok=true means it is genuinely fine."
)

IMPLEMENTOR_SYSTEM = (
    "Complete the task. When you are done, state concretely what you changed "
    "(files, commands run, results) so it can be verified independently."
)

ROLES: dict[str, dict] = {
    "assistant": {
        "system": "",
        "review": False,
        "restricted": False,
        "model": None,
    },
    "implementor": {
        "system": IMPLEMENTOR_SYSTEM,
        "review": True,          # its output goes to a reviewer before completing
        "restricted": False,
        "model": None,
    },
    "reviewer": {
        "system": REVIEWER_SYSTEM,
        "review": False,         # reviewing a review would never terminate
        "restricted": True,      # read-only, enforced by the permission args
        # Never resume an existing session: reviewing inside the conversation
        # that produced the work would inherit exactly the assumptions the
        # review exists to question.
        "fresh": True,
        "model": None,
    },
}


def get(name: str) -> dict:
    return ROLES.get(name or "assistant", ROLES["assistant"])


def needs_review(name: str) -> bool:
    return bool(get(name).get("review"))


def is_fresh(name: str) -> bool:
    """True if this role must start a new session rather than resume one."""
    return bool(get(name).get("fresh"))


def permission_args(name: str, default_args: list[str]) -> list[str]:
    """Run args for this role. A restricted role never gets full autonomy."""
    if get(name).get("restricted"):
        return ["--allowedTools", REVIEWER_TOOLS]
    return list(default_args)


def system_prompt(name: str) -> str:
    return get(name).get("system", "")


def review_goal(task: dict, result_text: str) -> str:
    """The prompt handed to a reviewer, with fresh context."""
    scope = task.get("scope") or {}
    return (
        f"Goal that was given:\n{task.get('goal', '')}\n\n"
        f"Working directory: {scope.get('cwd', '?')}\n\n"
        f"What the implementor reported:\n{result_text[:4000]}\n\n"
        "Verify it against the repository itself."
    )


def parse_verdict(text: str) -> dict:
    """Pull the verdict out of a reviewer's reply.

    Fails closed: if no verdict can be read, the work is treated as needing a
    person, never as approved. A reviewer that rambles must not silently pass
    something through.
    """
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text or "", re.S)
    if not blocks:                       # tolerate a bare object
        m = re.search(r"\{[^{}]*\"ok\"\s*:.*?\}", text or "", re.S)
        blocks = [m.group(0)] if m else []
    for raw in reversed(blocks):
        try:
            v = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(v, dict) and "ok" in v:
            return {
                "ok": bool(v.get("ok")),
                "summary": str(v.get("summary", ""))[:300],
                "findings": [str(f)[:300] for f in (v.get("findings") or [])][:20],
                "parsed": True,
            }
    log.warning("no verdict found in reviewer output; treating as needing review")
    return {"ok": False, "summary": "reviewer returned no readable verdict",
            "findings": [], "parsed": False}

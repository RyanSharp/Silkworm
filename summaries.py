"""Per-session context summaries.

A one-to-three sentence "what is this conversation about" blurb for every
tracked thread, shown in the visualizer so you can see a session's context
without opening its transcript. Generated with a cheap model from the session's
Claude Code transcript; refreshed in the background after each turn and
backfillable on demand.

Summaries live on the session record in sessions.json (`summary`, plus a
`summary_ts` watermark = the transcript's last-seen entry timestamp, so a
refresh is skipped when nothing new has happened).
"""

import logging
import subprocess

from harvester import find_transcript, _new_turns

log = logging.getLogger("silkworm.summaries")

# The instruction comes *after* the transcript on purpose. With a long
# conversation in front of it, a model told what to do only up top tends to
# follow the conversation's own pattern and continue it instead of describing
# it — which produced summaries that read as replies.
PROMPT = (
    "Below, between <transcript> tags, is a recorded conversation between a "
    "user and an AI assistant. You are describing it to someone else, not "
    "taking part in it.\n\n"
    "<transcript>\n{convo}\n</transcript>\n\n"
    "Summarize what that conversation is about in 1-3 sentences, so someone "
    "scanning a list of sessions understands its context and current focus. "
    "Describe the goal and what's been done — not pleasantries.\n\n"
    "Write ABOUT the conversation in the third person (\"The user is…\", "
    "\"They are building…\"). Never copy, quote, or continue the assistant's "
    "wording, never address the reader, and never answer anything asked in "
    "the transcript. Plain prose only: no preamble, no markdown, no quotes."
)


def _run_model(binary: str, model: str, env: dict, cwd: str, prompt: str,
               timeout: int = 90) -> str:
    proc = subprocess.run(
        [binary, "-p", "--model", model, "--output-format", "text"],
        input=prompt, capture_output=True, text=True, timeout=timeout,
        cwd=cwd or None, env=env)
    return proc.stdout.strip()


def summarize_one(store, key: str, *, binary: str, model: str, env: dict,
                  force: bool = False) -> bool:
    """(Re)generate the summary for one thread. Returns True if it wrote one."""
    entry = store.get(key)
    if not entry:
        return False
    sid = entry.get("session_id")
    path = find_transcript(sid) if sid else None
    if not path:
        return False

    convo, max_ts = _new_turns(path, "")  # whole transcript, capped to recent
    if not convo.strip():
        return False
    if not force and max_ts and entry.get("summary") and entry.get("summary_ts") == max_ts:
        return False  # nothing new since last summary

    try:
        text = _run_model(binary, model, env, entry.get("cwd", ""),
                          PROMPT.format(convo=convo))
    except Exception:
        log.exception("summary generation failed for %s", key)
        return False
    text = " ".join(text.split())
    if not text:
        return False
    if len(text) > 600:
        text = text[:597].rstrip() + "…"

    store.update(key, summary=text, summary_ts=max_ts)
    log.info("summarized %s (%d chars)", key, len(text))
    return True


def backfill(store, *, binary: str, model: str, env: dict, force: bool = False) -> dict:
    """Summarize every tracked thread (missing ones only, unless force)."""
    done = skipped = failed = 0
    for key, entry in store.all().items():
        if not force and entry.get("summary"):
            skipped += 1
            continue
        try:
            if summarize_one(store, key, binary=binary, model=model, env=env, force=force):
                done += 1
            else:
                skipped += 1
        except Exception:
            log.exception("backfill failed for %s", key)
            failed += 1
    return {"summarized": done, "skipped": skipped, "failed": failed}

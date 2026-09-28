"""Links into Slack threads, built one way.

A bare archive link, `/archives/<channel>/p<ts>`, names a message but not the
conversation to open it in. For a DM with an app, Slack resolves that to the
app -- and once the app has a Home tab, the app opens on Home, so every "open
thread" link went there instead of to the thread. The permalink Slack itself
generates (chat.getPermalink) carries `thread_ts` and `cid`, which is what
opens the conversation at the thread; this builds that shape without an API
call per link.

Every link to a thread comes from here, so they cannot drift apart again.
"""

DEFAULT_BASE = "https://slack.com"


def thread_link(channel: str, ts: str, base: str = DEFAULT_BASE) -> str:
    """A link that opens `channel` at the thread rooted at `ts`."""
    base = (base or DEFAULT_BASE).rstrip("/")
    return (f"{base}/archives/{channel}/p{ts.replace('.', '')}"
            f"?thread_ts={ts}&cid={channel}")


def for_key(key: str, base: str = DEFAULT_BASE) -> str:
    """The same, from a `channel:ts` thread key; empty if it is not one."""
    if not key or ":" not in key:
        return ""
    channel, ts = key.split(":", 1)
    return thread_link(channel, ts, base) if channel and ts else ""

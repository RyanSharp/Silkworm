"""Text with anything shaped like a credential replaced.

What the health checks put in their results, the ops runner in its log and
the dashboard's search in its index all pass through scrub(). Its own module,
standard library only and with no side effects on import: health_probes
registers its checks when imported, which nothing wanting a scrubber should
set off (the visualizer, smoke and watchdog run on whatever python3 is).
"""

import re

SECRETS = [
    re.compile(r"sbp_[A-Za-z0-9]{8,}"),                 # Supabase management token
    re.compile(r"sb_(?:secret|publishable)_[A-Za-z0-9_-]{8,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),            # Anthropic / claude token
    re.compile(r"xox[abposr]-[A-Za-z0-9-]{8,}"),         # Slack
    re.compile(r"xapp-[A-Za-z0-9-]{8,}"),                # Slack app-level token
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}"),  # GitHub token
    re.compile(r"github_pat_[A-Za-z0-9_]{16,}"),         # GitHub fine-grained token
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # a JWT
    re.compile(r"(?i)(bearer\s+)[^\s\"']+"),
    re.compile(r"\b(?:nodekey|mkey|discokey|nlpub|privkey):[0-9a-f]{8,}"),  # Tailscale keys
    re.compile(r"tskey-[A-Za-z0-9-]{8,}"),               # a Tailscale auth key
    re.compile(r"(login\.tailscale\.com/a/)[A-Za-z0-9]+"),  # a login link
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
]


def scrub(text, known=()) -> str:
    """`text` with anything shaped like a credential -- and any value in
    `known`, the credentials a caller actually read -- replaced."""
    out = str(text or "")
    for value in known:
        if value and len(value) >= 6:
            out = out.replace(value, "[redacted]")
    for pat in SECRETS:
        out = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "[redacted]", out)
    return out

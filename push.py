"""Web push: notifications on the phone the dashboard is installed on.

The dashboard installs as a home-screen app (visualizer.py); once its owner
taps "Enable notifications" there, the browser hands the page a
PushSubscription -- an endpoint at Apple's (or Google's, or Mozilla's) push
service plus two keys -- which is sent here and kept in push.json. The bot
then tells that endpoint when something needs the owner: a task waiting on
them, or a reply in a thread. It replaces Slack's notification; Slack's own
behaviour is untouched.

The wire format is two RFCs, implemented here over `cryptography` (the
stdlib has neither P-256 nor AES-GCM):

  * RFC 8291 -- the payload is encrypted to the subscription's keys
    (ECDH + HKDF + AES-128-GCM, `aes128gcm` content coding). Checked against
    the RFC's own worked example in the tests.
  * RFC 8292 -- VAPID: each request carries a JWT signed (ES256) with this
    install's own keypair, so the push service knows who is sending. The
    keypair is made once and kept outside any repository (key_dir_default(), mode 600);
    the private key is never printed or logged.

Sending never blocks or breaks what triggered it: Notifier queues and returns,
and a daemon thread delivers, logging and swallowing every error. A
subscription the push service says is gone (404/410) is dropped.

What sends is decided here, as pure functions of a task record:
needs_you() for a task entering awaiting_approval, needs_input or failed --
once per task and state, so a loop cannot spam -- and reply_message() for a
finished reply in a thread, never for a scheduled check that found nothing.
A transient failure (quota, overload) parks a task in `blocked` with a retry
time (bot.fail_or_retry), which is not a state that notifies.

What the owner hears is theirs to choose (the dashboard's notification
settings, kept in push.json beside the subscriptions): each kind -- needs
you, thread replies, health failures, deploy and landing results -- and each
project can be switched off, and quiet hours in their own time zone hold
everything for one summary once they end. A few needs-you items cannot wait
for morning (URGENT: a trader halt, the IB Gateway down or logged out, a
missed dispatch); those bypass quiet hours, each one a setting. All of it is
decided in Notifier.deliver, the one send path, after a task's move has
settled and before anything is sent or claimed as sent. The bot-is-down
alert (alert_main) is the one exception: it goes out whatever is set,
because it is sent by the watchdog when nothing else can be.

Python 3.9-compatible: bin/silkworm imports this under the system python3.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import queue
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import jsonstore
import tasks

log = logging.getLogger("silkworm.push")

#: Where the VAPID keypair lives: outside every repository and worktree.
DEFAULT_KEY_DIR = Path.home() / ".silkworm" / "vapid"


def key_dir_default() -> Path:
    """SILKWORM_VAPID_DIR, else DEFAULT_KEY_DIR -- read when asked, not at
    import: the bot loads .env after its imports."""
    return Path(os.environ.get("SILKWORM_VAPID_DIR", "").strip() or DEFAULT_KEY_DIR).expanduser()
PRIVATE_FILE = "private.pem"
PUBLIC_FILE = "public.txt"

#: The VAPID `sub` claim: how a push service can reach whoever runs this.
#: RFC 8292 allows a mailto: or an https: URL; Apple refuses a JWT without one.
DEFAULT_CONTACT = "https://github.com/RyanSharp/Silkworm"

#: Push services a subscription may point at. The bot POSTs to the endpoint,
#: so an endpoint is a URL it will fetch: only the real services are taken.
PUSH_HOSTS = ("push.apple.com", "fcm.googleapis.com", "push.services.mozilla.com",
              "notify.windows.com")

#: The states a task notifies on entering: exactly the ones that need you
#: and that the bot's own work leads to (proposals are filed, not reached).
NEEDS_YOU = {
    tasks.AWAITING_APPROVAL: "Ready for your approval",
    tasks.NEEDS_INPUT: "Needs your input",
    tasks.FAILED: "Failed",
}

#: Kept from growing without end: subscriptions (one per installed device),
#: and the record of what was already sent.
MAX_SUBSCRIPTIONS = 20
MAX_SENT = 5000
#: How long a push service should keep trying to deliver: a day.
TTL_S = 24 * 3600
#: A task's move is read again this long after it happened, and sent only if
#: the task is still there -- a restart's "failed" that is requeued in the
#: same breath is not news.
SETTLE_S = 2.0
SEND_TIMEOUT_S = 10.0
QUEUE_MAX = 200

TITLE_MAX = 60
BODY_MAX = 120

# --- what the owner chose to hear ------------------------------------------------

#: The kinds a notification can be, as the settings screen names them.
KINDS = {
    "needs_you": "Needs you: approvals, questions, failures",
    "reply": "Thread replies",
    "health": "Health failures",
    "results": "Deploy and landing results",
}
#: Needs-you items that may bypass quiet hours, each its own setting: id ->
#: (what the screen says, the health signals it covers). A signal is
#: "project/check", or "project/check:name" for one named check inside the
#: trader's own healthcheck (paper_healthcheck.py prints "FAIL halt: ...").
URGENT = {
    "trader-halt": ("Trader halted or paused (a drawdown halt tripped)",
                    ("trader/healthcheck:halt", "trader/healthcheck:paused")),
    "gateway": ("IB Gateway down or logged out",
                ("trader/gateway", "trader/healthcheck:gateway")),
    "dispatch": ("Trader missed a dispatch, or its daemon is down",
                 ("trader/dispatching", "trader/healthcheck:daemon",
                  "trader/healthcheck:dispatch")),
    "credentials": ("The mini's Claude or Apple credentials failing",
                    ("mini/credentials",)),
}
#: Bypassing by default: the ones where a night's wait costs money.
URGENT_DEFAULT = ("trader-halt", "gateway", "dispatch")
DEFAULT_QUIET = {"on": False, "start": "22:00", "end": "07:00", "tz": ""}
#: Held during quiet hours, at most this many (the oldest go first).
MAX_HELD = 200
#: How often the sender wakes with nothing to send, to see if quiet hours ended.
FLUSH_POLL_S = 60.0
#: The summary names this many of what it held; the rest are a count.
SUMMARY_NAMES = 3


def default_settings() -> dict:
    return {"kinds": {k: True for k in KINDS}, "projects": {},
            "quiet": dict(DEFAULT_QUIET),
            "urgent": {"bypass": True, "checks": list(URGENT_DEFAULT)}}


def _hhmm(text) -> int | None:
    """Minutes past midnight of "HH:MM", or None."""
    t = str(text or "").strip()
    if len(t) != 5 or t[2] != ":" or not (t[:2] + t[3:]).isdigit():
        return None
    h, m = int(t[:2]), int(t[3:])
    return h * 60 + m if h < 24 and m < 60 else None


def _zone(name: str):
    """The tzinfo of an IANA zone name, or None for "" (the mini's own)."""
    if not name:
        return None
    from zoneinfo import ZoneInfo
    return ZoneInfo(name)


def check_settings(raw) -> dict:
    """Settings as asked for, checked: ValueError with a sentence if not.

    Anything not given keeps its default; kinds and urgent ids not known
    here are refused rather than kept, so a typo cannot read as a setting.
    """
    if not isinstance(raw, dict):
        raise ValueError("settings must be an object")
    out = default_settings()
    kinds = raw.get("kinds") or {}
    if not isinstance(kinds, dict) or set(kinds) - set(KINDS):
        raise ValueError(f"kinds are {', '.join(KINDS)}")
    for k, v in kinds.items():
        out["kinds"][k] = bool(v)
    projects = raw.get("projects") or {}
    if not isinstance(projects, dict):
        raise ValueError("projects must map a project to on or off")
    for slug, v in projects.items():
        slug = str(slug).strip()
        if not slug or len(slug) > 80:
            raise ValueError("a project name is empty or too long")
        if not v:
            out["projects"][slug] = False      # only what is off is kept
    quiet = raw.get("quiet") or {}
    if not isinstance(quiet, dict):
        raise ValueError("quiet must be an object")
    q = dict(DEFAULT_QUIET, **{k: quiet[k] for k in DEFAULT_QUIET if k in quiet})
    q["on"] = bool(q["on"])
    for end in ("start", "end"):
        if _hhmm(q[end]) is None:
            raise ValueError(f"quiet hours' {end} must be HH:MM, 24-hour")
    if q["on"] and q["start"] == q["end"]:
        raise ValueError("quiet hours start and end at the same time")
    q["tz"] = str(q["tz"] or "").strip()
    try:
        _zone(q["tz"])
    except Exception:
        raise ValueError(f"{q['tz']!r} is not a time zone this machine knows")
    out["quiet"] = q
    urgent = raw.get("urgent") or {}
    if not isinstance(urgent, dict):
        raise ValueError("urgent must be an object")
    if "bypass" in urgent:
        out["urgent"]["bypass"] = bool(urgent["bypass"])
    if "checks" in urgent:
        checks = urgent["checks"]
        if not isinstance(checks, list) or set(checks) - set(URGENT):
            raise ValueError(f"urgent items are {', '.join(URGENT)}")
        out["urgent"]["checks"] = [c for c in URGENT if c in checks]
    return out


def in_quiet(settings: dict, now: float) -> bool:
    """Whether `now` falls in the quiet hours, in their own time zone.
    The window is [start, end), and may run past midnight."""
    q = (settings or {}).get("quiet") or {}
    start, end = _hhmm(q.get("start")), _hhmm(q.get("end"))
    if not q.get("on") or start is None or end is None or start == end:
        return False
    try:
        local = datetime.fromtimestamp(now, _zone(q.get("tz") or ""))
    except Exception:
        local = datetime.fromtimestamp(now)       # a zone gone from the machine
    m = local.hour * 60 + local.minute
    return start <= m < end if start < end else (m >= start or m < end)


def suppressed(msg: dict, settings: dict) -> str:
    """Why `msg` is switched off ("" if it is not)."""
    kind = msg.get("kind") or ""
    if kind and not ((settings or {}).get("kinds") or {}).get(kind, True):
        return f"{kind} is switched off"
    project = msg.get("project") or ""
    if project and ((settings or {}).get("projects") or {}).get(project, True) is False:
        return f"{project} is switched off"
    return ""


def bypasses(msg: dict, settings: dict) -> bool:
    """Whether `msg` is urgent enough, by the owner's settings, to be sent
    during quiet hours."""
    urgent = (settings or {}).get("urgent") or {}
    if not urgent.get("bypass"):
        return False
    signals = set(msg.get("signals") or ())
    return any(signals & set(URGENT[c][1]) for c in urgent.get("checks") or () if c in URGENT)


class Unavailable(RuntimeError):
    """Push cannot work here: `cryptography` is missing, or keys cannot be made."""


class BadSubscription(ValueError):
    """A subscription that cannot be used, and why, in a sentence."""


# --- encoding -----------------------------------------------------------------

def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    text = "".join(str(text).split())
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _crypto():
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise Unavailable("the `cryptography` package is not installed "
                          "(pip install -r requirements.txt)") from None
    return hashes, serialization, ec, decode_dss_signature, AESGCM


def available() -> bool:
    try:
        _crypto()
        return True
    except Unavailable:
        return False


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _public_bytes(key) -> bytes:
    _, serialization, _, _, _ = _crypto()
    return key.public_key().public_bytes(serialization.Encoding.X962,
                                         serialization.PublicFormat.UncompressedPoint)


# --- the VAPID keypair ----------------------------------------------------------

_KEY_LOCK = threading.Lock()


def keys_present(key_dir: Path | None = None) -> bool:
    d = Path(key_dir or key_dir_default())
    return (d / PRIVATE_FILE).is_file() and (d / PUBLIC_FILE).is_file()


def public_key(key_dir: Path | None = None) -> str:
    """The application server key the page subscribes with, or "" if none yet."""
    try:
        return (Path(key_dir or key_dir_default()) / PUBLIC_FILE).read_text().strip()
    except OSError:
        return ""


def _load_private(key_dir: Path):
    _, serialization, _, _, _ = _crypto()
    data = (key_dir / PRIVATE_FILE).read_bytes()
    return serialization.load_pem_private_key(data, password=None)


def ensure_keys(key_dir: Path | None = None) -> str:
    """Make the keypair if there is none, and return the public key.

    Made once: a new keypair would orphan every subscription made with the
    old one. The private key is written mode 600 in a mode 700 directory,
    created exclusively so two callers cannot each write their own.
    """
    d = Path(key_dir or key_dir_default())
    _, serialization, ec, _, _ = _crypto()
    with _KEY_LOCK:
        priv_path = d / PRIVATE_FILE
        if not priv_path.exists():
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
            try:
                fd = os.open(priv_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass                      # another process won; use its key
            else:
                with os.fdopen(fd, "wb") as f:
                    f.write(pem)
                log.info("made a VAPID keypair in %s", d)
        try:
            key = _load_private(d)
        except Exception as e:
            raise Unavailable(f"the push key in {d} cannot be read: {type(e).__name__}") from None
        pub = b64u(_public_bytes(key))
        if public_key(d) != pub:
            (d / PUBLIC_FILE).write_text(pub + "\n")
        return pub


# --- RFC 8291: the payload ---------------------------------------------------------

def encrypt(plaintext: bytes, p256dh: str, auth: str, *, as_private=None,
            salt: bytes | None = None, record_size: int = 4096) -> bytes:
    """`plaintext` encrypted to a subscription's keys, as an aes128gcm body.

    `as_private` and `salt` are for the RFC's worked example; every real
    message gets a fresh ephemeral key and salt.
    """
    _, _, ec, _, AESGCM = _crypto()
    ua_public = unb64u(p256dh)
    auth_secret = unb64u(auth)
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    as_public = _public_bytes(as_private)
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh_secret = as_private.exchange(ec.ECDH(), peer)
    # HKDF(auth_secret, ecdh_secret, "WebPush: info" || 0 || ua || as, 32)
    prk_key = _hmac(auth_secret, ecdh_secret)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    salt = salt if salt is not None else os.urandom(16)
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    if len(plaintext) + 17 > record_size:
        raise ValueError("the message does not fit in one record")
    # One record, so it is the last: padding delimiter 0x02, no padding.
    sealed = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + struct.pack("!I", record_size) + bytes([len(as_public)]) + as_public
    return header + sealed


# --- RFC 8292: who is sending ---------------------------------------------------------

def vapid_header(endpoint: str, key_dir: Path | None = None, contact: str = "",
                 now: float | None = None) -> str:
    """The Authorization header for one request to `endpoint`'s push service."""
    hashes, _, ec, decode_dss_signature, _ = _crypto()
    d = Path(key_dir or key_dir_default())
    key = _load_private(d)
    url = urlparse(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({
        "aud": f"{url.scheme}://{url.netloc}",
        "exp": int((now if now is not None else time.time()) + 12 * 3600),
        "sub": contact or os.environ.get("PUSH_CONTACT", "").strip() or DEFAULT_CONTACT,
    }, separators=(",", ":")).encode())
    signing_input = f"{head}.{claims}".encode()
    r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    # JWS wants the raw 64-byte r||s, not the DER cryptography returns.
    jwt = f"{head}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={jwt}, k={b64u(_public_bytes(key))}"


# --- subscriptions --------------------------------------------------------------------

def host_of(endpoint: str) -> str:
    return (urlparse(str(endpoint)).hostname or "").lower()


def check_subscription(sub) -> dict:
    """The parts of a PushSubscription (its toJSON()) that are kept, or
    BadSubscription."""
    if not isinstance(sub, dict):
        raise BadSubscription("no subscription")
    endpoint = str(sub.get("endpoint") or "")
    url = urlparse(endpoint)
    host = host_of(endpoint)
    if url.scheme != "https" or not host:
        raise BadSubscription("the endpoint must be an https URL")
    if not any(host == h or host.endswith("." + h) for h in PUSH_HOSTS):
        raise BadSubscription(f"{host} is not a push service")
    keys = sub.get("keys") or {}
    try:
        p256dh, auth = unb64u(keys.get("p256dh") or ""), unb64u(keys.get("auth") or "")
    except (ValueError, TypeError):
        raise BadSubscription("its keys are not base64url") from None
    if len(p256dh) != 65 or p256dh[0] != 4:
        raise BadSubscription("its p256dh is not a P-256 public key")
    if len(auth) != 16:
        raise BadSubscription("its auth secret is not 16 bytes")
    return {"endpoint": endpoint, "keys": {"p256dh": b64u(p256dh), "auth": b64u(auth)}}


class PushStore:
    """push.json: the subscriptions, and which notifications were sent.

    One writer, the bot (the dashboard relays to it); saved with jsonstore,
    atomically and with a backup, as every other store is.
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        data = jsonstore.load(self._path, default={}) or {}
        self._subs: dict = dict(data.get("subscriptions") or {})
        self._sent: dict = dict(data.get("sent") or {})
        try:
            self._settings = check_settings(data.get("settings") or {})
        except ValueError:
            log.warning("push.json's notification settings are unreadable; using the defaults")
            self._settings = default_settings()
        self._held: list = [m for m in (data.get("held") or []) if isinstance(m, dict)]

    def _save(self) -> None:
        jsonstore.save(self._path, {"version": 1, "subscriptions": self._subs,
                                    "sent": self._sent, "settings": self._settings,
                                    "held": self._held})

    # --- the owner's notification settings, and what quiet hours hold ---

    def settings(self) -> dict:
        # No lock, for the same reason as count(): it is read on the send
        # path, and set_settings replaces the dict rather than editing it.
        return json.loads(json.dumps(self._settings))

    def set_settings(self, raw) -> dict:
        clean = check_settings(raw)
        with self._lock:
            self._settings = clean
            self._save()
        return self.settings()

    def hold(self, msg: dict) -> int:
        with self._lock:
            self._held.append(dict(msg))
            del self._held[:-MAX_HELD]
            self._save()
            return len(self._held)

    def held(self) -> list[dict]:
        with self._lock:
            return [dict(m) for m in self._held]

    def take_held(self) -> list[dict]:
        """Everything held, and nothing held after."""
        with self._lock:
            out, self._held = self._held, []
            if out:
                self._save()
            return out

    def add(self, sub, agent: str = "") -> dict:
        clean = check_subscription(sub)
        with self._lock:
            prior = self._subs.get(clean["endpoint"]) or {}
            self._subs[clean["endpoint"]] = dict(clean, added=prior.get("added") or time.time(),
                                                 seen=time.time(), agent=str(agent)[:120])
            for old in sorted(self._subs, key=lambda e: self._subs[e].get("seen") or 0)[
                    :max(0, len(self._subs) - MAX_SUBSCRIPTIONS)]:
                del self._subs[old]
            self._save()
            return dict(self._subs[clean["endpoint"]])

    def remove(self, endpoint: str) -> bool:
        with self._lock:
            if self._subs.pop(str(endpoint), None) is None:
                return False
            self._save()
            return True

    def clear(self) -> int:
        with self._lock:
            n = len(self._subs)
            self._subs = {}
            self._save()
            return n

    def subscriptions(self) -> list[dict]:
        with self._lock:
            return [dict(s) for s in self._subs.values()]

    def count(self) -> int:
        # No lock: it is asked on a task's move (Notifier._enqueue), and the
        # lock is held across a save. Reading a dict's length is atomic.
        return len(self._subs)

    def claim(self, key: str) -> bool:
        """True the first time `key` is claimed, False ever after."""
        with self._lock:
            if key in self._sent:
                return False
            self._sent[key] = time.time()
            if len(self._sent) > MAX_SENT:
                for old in sorted(self._sent, key=self._sent.get)[:len(self._sent) - MAX_SENT]:
                    del self._sent[old]
            self._save()
            return True


# --- what sends ---------------------------------------------------------------------

def _line(text, limit: int) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[:limit - 1].rstrip() + "…"


def needs_you(rec: dict | None) -> dict | None:
    """The notification a task's move sends, or None.

    Only the states that need you. The body is the task's title -- never its
    goal, result or error text, which can hold anything a turn printed.
    """
    rec = rec or {}
    state = rec.get("state")
    if state not in NEEDS_YOU or not rec.get("id"):
        return None
    label = NEEDS_YOU[state]
    project = rec.get("project") or ""
    health = rec.get("source") == "health"
    return {"title": _line(f"{label} · {project}" if project else label, TITLE_MAX),
            "body": _line(rec.get("title") or rec.get("id"), BODY_MAX),
            "url": f"/?task={rec['id']}",
            "tag": f"task-{rec['id']}",
            "dedupe": f"task:{rec['id']}:{state}",
            "task": rec["id"], "state": state,
            "kind": "health" if health else "needs_you", "project": project,
            "signals": _ref_signals(rec.get("source_ref")) if health else []}


def _ref_signals(ref) -> list[str]:
    """A health item's signals from its source_ref ("slug/check@fp"): its
    "project/check", and for the trader's healthcheck, whose fingerprint
    starts with the failing names ("halt,universe:<hash>"), each of those."""
    head, _, fp = str(ref or "").partition("@")
    if "/" not in head:
        return []
    out = [head]
    if head.endswith("/healthcheck") and ":" in fp:
        out += [f"{head}:{n.strip()}" for n in fp.split(":", 1)[0].split(",") if n.strip()]
    return out


def health_signals(rec: dict) -> list[str]:
    """What a failing health record is, for URGENT: "project/check", and for
    a check that runs named checks of its own (the trader's healthcheck, whose
    evidence reads "FAIL halt: ..."), "project/check:name" for each failing."""
    base = f"{rec.get('project') or ''}/{rec.get('check') or ''}"
    out = [base]
    for line in rec.get("evidence") or ():
        line = str(line)
        if line.startswith("FAIL ") and ":" in line:
            out.append(f"{base}:{line[5:].split(':', 1)[0].strip()}")
    return out


def health_message(rec: dict, item: dict | None) -> dict | None:
    """The notification a health check's first failure sends (health.surface
    said "opened"), or None. Its line is the check's own one-line summary,
    which the probes write to be read, never raw evidence."""
    rec = rec or {}
    project, check = rec.get("project") or "", rec.get("check") or ""
    if not project or not check:
        return None
    tid = (item or {}).get("id") or ""
    # Its item, when it has one, as a task's move would carry it: so a check
    # that passes again before the message is sent -- or before quiet hours
    # end -- closes the item and the message goes unsent.
    task = {"task": tid, "state": tasks.NEEDS_INPUT} if tid else {}
    return {**task, "title": _line(f"Health failing · {project} {check}", TITLE_MAX),
            "body": _line(rec.get("summary") or "failing", BODY_MAX),
            "url": f"/?task={tid}" if tid else f"/?view=project&slug={project}&sec=health",
            "tag": f"health-{project}-{check}",
            "dedupe": f"health:{tid or project + '/' + check}:{rec.get('fingerprint') or ''}",
            "kind": "health", "project": project, "signals": health_signals(rec)}


def landing_message(task: dict, outcome: dict) -> dict | None:
    """The notification a finished landing sends: landed, or refused and why.
    Not for one still under way, nor a project that does not land."""
    task, outcome = task or {}, outcome or {}
    stage = outcome.get("stage") or ""
    if not task.get("id") or stage in ("not-enabled", "in-progress"):
        return None
    landed = bool(outcome.get("landed"))
    project = task.get("project") or ""
    head = "Landed" if landed else f"Landing refused ({stage or 'unknown'})"
    return {"title": _line(f"{head} · {project}" if project else head, TITLE_MAX),
            "body": _line(task.get("title") or task["id"], BODY_MAX),
            "url": f"/?task={task['id']}",
            "tag": f"land-{task['id']}",
            # The same refusal again (a retry refused alike) is not news.
            "dedupe": f"land:{task['id']}:{outcome.get('head') or ''}:{stage}:{landed}",
            "kind": "results", "project": project}


def deploy_message(text: str, project: str = "", key: str = "") -> dict | None:
    """The notification a deploy's report sends (`silkworm deploy`, via the
    bot's /tell). Its first line only: the rest is in the thread."""
    first = next((ln for ln in str(text or "").splitlines() if ln.strip()), "")
    if not first:
        return None
    return {"title": "Deploy", "body": _line(first.strip(" *_`"), BODY_MAX),
            "url": f"/?thread={key}" if key else "/", "tag": "deploy",
            "kind": "results", "project": project or ""}


def summary_message(held: list[dict]) -> dict | None:
    """What quiet hours held, as one notification: the thing itself if it
    was only one, else a count, the first few titles, and the inbox."""
    held = [m for m in held or () if m.get("title")]
    if not held:
        return None
    if len(held) == 1:
        one = dict(held[0])
        one.pop("dedupe", None)
        return one
    names = "; ".join(m["title"] for m in held[:SUMMARY_NAMES])
    more = len(held) - SUMMARY_NAMES
    return {"title": f"{len(held)} notifications from quiet hours",
            "body": _line(names + (f"; and {more} more" if more > 0 else ""), BODY_MAX),
            "url": "/?view=inbox", "tag": "quiet-summary",
            "urgent_send": any(m.get("task") for m in held)}


def reply_message(key: str, task_id: str, title: str = "", text: str = "",
                  source: str = "", project: str = "") -> dict | None:
    """The notification a finished reply in thread `key` sends, or None.

    Never for a scheduled check that found nothing (defer.QUIET): a watch
    that pings on every poll is the noise this is meant to replace. The body
    names the thread, not what was said.
    """
    import defer
    if not key or not task_id:
        return None
    if source == tasks.WAKE_SOURCE and (text or "").strip() == defer.QUIET:
        return None
    return {"title": "Silkworm replied",
            "body": _line(title or "in your conversation", BODY_MAX),
            "url": f"/?thread={key}",
            "tag": f"thread-{key}",
            "dedupe": f"reply:{task_id}",
            "kind": "reply", "project": project or ""}


def wire(msg: dict) -> bytes:
    """What the service worker receives: only what it shows and opens."""
    return json.dumps({k: msg[k] for k in ("title", "body", "url", "tag") if k in msg},
                      separators=(",", ":")).encode()


# --- sending -----------------------------------------------------------------------

def post(endpoint: str, body: bytes, authorization: str, urgency: str = "normal",
         timeout: float = SEND_TIMEOUT_S) -> int:
    """One request to a push service; its HTTP status (0 if none came back)."""
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
        "TTL": str(TTL_S), "Urgency": urgency, "Authorization": authorization})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


class Notifier:
    """Sends notifications off the caller's thread. Every entry point
    returns at once and never raises."""

    def __init__(self, store: PushStore | None, key_dir: Path | None = None, *,
                 current=None, sender=None, settle: float = SETTLE_S, clock=None,
                 poll: float = FLUSH_POLL_S):
        self.store = store
        self.key_dir = Path(key_dir or key_dir_default())
        self.current = current            # tid -> record, to re-read after settling
        self.sender = sender or post
        self.settle = settle
        self.clock = clock or time.time   # quiet hours read this; tests set it
        self.poll = poll
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self._worker = None
        self._start_lock = threading.Lock()

    # --- entry points (hot path: cheap, and never raise) ---

    def task_moved(self, rec: dict, previous: str = "", settled: bool = True) -> None:
        """TaskStore.on_move. `settled` is False for a move that is not a real
        ending (requeue_interrupted's failed-then-queued)."""
        try:
            if not settled or previous == (rec or {}).get("state"):
                return
            msg = needs_you(rec)
            if msg:
                self._enqueue(dict(msg, at=time.time()))
        except Exception:
            log.exception("push: could not queue a notification for a task move")

    def reply(self, key: str, task_id: str, title: str = "", text: str = "",
              source: str = "", project: str = "") -> None:
        try:
            msg = reply_message(key, task_id, title, text, source, project)
            if msg:
                self._enqueue(msg)
        except Exception:
            log.exception("push: could not queue a reply notification")

    def event(self, msg: dict | None) -> None:
        """Queue a message made by health_message, landing_message or
        deploy_message. Never raises."""
        try:
            if msg:
                self._enqueue(dict(msg))
        except Exception:
            log.exception("push: could not queue a notification")

    def resume(self) -> None:
        """At startup: if a restart came while quiet hours held anything,
        start the sender so the summary still goes out when they end."""
        try:
            if self.store is not None and self.store.held():
                self._ensure_worker()
        except Exception:
            log.exception("push: could not resume held notifications")

    def _enqueue(self, msg: dict) -> None:
        if self.store is None or not self.store.count():
            return                        # nobody to tell; nothing to remember
        self._ensure_worker()
        try:
            self._q.put_nowait(msg)
        except queue.Full:
            log.warning("push: queue full; dropped a notification")

    def _ensure_worker(self) -> None:
        with self._start_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, daemon=True,
                                                name="push-sender")
                self._worker.start()

    def _work(self) -> None:
        while True:
            try:
                msg = self._q.get(timeout=self.poll)
            except queue.Empty:
                msg = None
            if msg is not None:
                try:
                    self.deliver(msg)
                except Exception:
                    log.exception("push: delivery failed")
            try:
                self.flush()
            except Exception:
                log.exception("push: sending what quiet hours held failed")

    # --- delivery (the worker's thread) ---

    def deliver(self, msg: dict) -> dict:
        """Send one notification to every subscription. Returns counts."""
        out = {"sent": 0, "pruned": 0, "failed": 0, "skipped": ""}
        if msg.get("task") and self.current is not None:
            wait = (msg.get("at") or 0) + self.settle - time.time()
            if wait > 0:
                time.sleep(wait)
            now = self.current(msg["task"]) or {}
            if now.get("state") != msg.get("state"):
                out["skipped"] = "moved on"
                return out
        settings = self.store.settings()
        why = suppressed(msg, settings)
        if why:
            # Not claimed: switched back on, the next such move is sent.
            out["skipped"] = why
            return out
        if msg.get("dedupe") and not self.store.claim(msg["dedupe"]):
            out["skipped"] = "already sent"
            return out
        if bypasses(msg, settings):
            # Urgent enough to wake the owner, so urgent to the push service.
            msg = dict(msg, urgent_send=True)
        elif in_quiet(settings, self.clock()):
            self.store.hold(dict(msg, held_at=self.clock()))
            out["skipped"] = "held for quiet hours"
            return out
        return self.send_all(msg, out)

    def flush(self, force: bool = False) -> dict | None:
        """Once quiet hours are over, send what they held as one summary.

        Held messages are read again first: a task that has moved on since is
        not news, and a kind or project switched off meanwhile stays off.
        """
        if self.store is None:
            return None
        settings = self.store.settings()
        if not force and in_quiet(settings, self.clock()):
            return None
        held = self.store.take_held()
        if not held:
            return None
        kept = []
        for m in held:
            if suppressed(m, settings):
                continue
            if m.get("task") and self.current is not None:
                if (self.current(m["task"]) or {}).get("state") != m.get("state"):
                    continue
            kept.append(m)
        msg = summary_message(kept)
        if not msg:
            return {"sent": 0, "pruned": 0, "failed": 0, "skipped": "nothing left to say",
                    "held": len(held)}
        out = self.send_all(msg)
        if not out["sent"] and out["failed"]:
            # Nobody took it (the network down at the end of the night):
            # kept, and tried again at the sender's next wake.
            for m in kept:
                self.store.hold(m)
            log.warning("push: the quiet-hours summary could not be sent; kept %d", len(kept))
            out["skipped"] = "kept: not sent"
        return dict(out, held=len(held), summarised=len(kept))

    def send_all(self, msg: dict, out: dict | None = None) -> dict:
        out = out if out is not None else {"sent": 0, "pruned": 0, "failed": 0, "skipped": ""}
        body = wire(msg)
        for sub in self.store.subscriptions():
            host = host_of(sub.get("endpoint"))
            try:
                payload = encrypt(body, sub["keys"]["p256dh"], sub["keys"]["auth"])
                status = self.sender(sub["endpoint"], payload,
                                     vapid_header(sub["endpoint"], self.key_dir),
                                     "high" if msg.get("task") or msg.get("urgent_send")
                                     else "normal")
            except Exception as e:
                out["failed"] += 1
                # The host only: an endpoint is a capability URL.
                log.warning("push: could not send to %s: %s", host, type(e).__name__)
                continue
            if status in (404, 410):
                self.store.remove(sub["endpoint"])
                out["pruned"] += 1
                log.info("push: %s says a subscription is gone (%s); removed it", host, status)
            elif 200 <= status < 300:
                out["sent"] += 1
            else:
                out["failed"] += 1
                log.warning("push: %s answered %s", host, status)
        return out

    def test(self) -> dict:
        """Send a test notification now, on this thread."""
        return self.send_all({"title": "Silkworm", "body": "Notifications work.",
                              "url": "/", "tag": "test"})


# --- the bot's /push route -------------------------------------------------------

ACTIONS = ("key", "subscribe", "unsubscribe", "status", "test", "forget-all",
           "settings", "save-settings")


def settings_answer(store: PushStore, notifier: Notifier | None = None) -> dict:
    """What the settings screen draws: the settings, and the words for them."""
    st = store.settings()
    clock = notifier.clock if notifier is not None else time.time
    return {"ok": True, "settings": st,
            "kinds": [{"id": k, "label": v} for k, v in KINDS.items()],
            "urgent": [{"id": k, "label": v[0], "signals": list(v[1])}
                       for k, v in URGENT.items()],
            "held": len(store.held()), "quiet_now": in_quiet(st, clock())}


def handle(payload: dict, store: PushStore | None, notifier: Notifier,
           key_dir: Path | None = None) -> dict:
    """/push: what the dashboard and `silkworm push` ask of push.

    key          the public key to subscribe with (made on first ask)
    subscribe    {subscription: PushSubscription.toJSON(), agent?}
    unsubscribe  {endpoint}
    status       keys, subscriptions, whether cryptography is installed
    test         send a test notification to every subscription, now
    forget-all   drop every subscription
    settings     the notification settings (settings_answer)
    save-settings {settings}: replace them, checked (check_settings)
    """
    action = str((payload or {}).get("action") or "")
    d = Path(key_dir or key_dir_default())
    if action not in ACTIONS:
        return {"ok": False, "error": f"unknown action {action!r}; one of {', '.join(ACTIONS)}"}
    if action == "status":
        subs = store.subscriptions() if store is not None else []
        return {"ok": True, "available": available(), "keys": keys_present(d),
                "key_dir": str(d), "subscriptions": len(subs),
                "services": sorted({host_of(s.get("endpoint")) for s in subs}),
                "store": store is not None}
    if store is None:
        return {"ok": False, "error": "push.json could not be read; see the bot log"}
    try:
        if action == "key":
            return {"ok": True, "key": ensure_keys(d)}
        if action == "subscribe":
            ensure_keys(d)
            store.add(payload.get("subscription"), payload.get("agent") or "")
            return {"ok": True, "subscriptions": store.count()}
        if action == "unsubscribe":
            return {"ok": True, "removed": store.remove(str(payload.get("endpoint") or ""))}
        if action == "forget-all":
            return {"ok": True, "forgot": store.clear()}
        if action == "settings":
            return settings_answer(store, notifier)
        if action == "save-settings":
            try:
                store.set_settings(payload.get("settings"))
            except ValueError as e:
                return {"ok": False, "error": str(e)}
            # Quiet hours switched off, or moved to end before now: what
            # they held goes at the sender's next wake, within a minute.
            if store.held():
                notifier._ensure_worker()
            return settings_answer(store, notifier)
        if not store.count():
            return {"ok": False, "error": "no subscriptions: tap Enable notifications "
                                          "in the dashboard on the phone first"}
        out = notifier.test()
        return dict(out, ok=out["sent"] > 0, subscriptions=store.count())
    except (Unavailable, BadSubscription) as e:
        return {"ok": False, "error": str(e)}


# --- a direct send, for when the bot is the thing that is down -------------------

def alert_main(argv: list, sender=None) -> int:
    """`python push.py alert --home DIR --title T --body B`: send one
    notification to every subscribed device without the bot.

    The watchdog (via alerts.push_send) runs this when the bot is not
    answering, so it is the bot's own store and keys read directly. Exit 0
    when at least one device took it.
    """
    opts = {"--home": "", "--title": "Silkworm", "--body": ""}
    i = 0
    while i + 1 < len(argv) and argv[i] in opts:
        opts[argv[i]] = argv[i + 1]
        i += 2
    if i != len(argv) or not opts["--home"] or not opts["--body"]:
        print("usage: push.py alert --home DIR --title T --body B", file=sys.stderr)
        return 2
    store = PushStore(Path(opts["--home"]) / "push.json")
    if not store.count():
        print("no subscribed devices")
        return 1
    out = Notifier(store, sender=sender).send_all(
        {"title": _line(opts["--title"], 60), "body": _line(opts["--body"], BODY_MAX),
         "url": "/", "tag": "silkworm-down"})
    print(f"sent {out['sent']}, failed {out['failed']}, pruned {out['pruned']}")
    return 0 if out["sent"] else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["alert"]:
        sys.exit(alert_main(sys.argv[2:]))
    print("usage: push.py alert --home DIR --title T --body B", file=sys.stderr)
    sys.exit(2)

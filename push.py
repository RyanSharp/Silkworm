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
import threading
import time
import urllib.error
import urllib.request
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

    def _save(self) -> None:
        jsonstore.save(self._path, {"version": 1, "subscriptions": self._subs,
                                    "sent": self._sent})

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
    return {"title": _line(f"{label} · {project}" if project else label, TITLE_MAX),
            "body": _line(rec.get("title") or rec.get("id"), BODY_MAX),
            "url": f"/?task={rec['id']}",
            "tag": f"task-{rec['id']}",
            "dedupe": f"task:{rec['id']}:{state}",
            "task": rec["id"], "state": state}


def reply_message(key: str, task_id: str, title: str = "", text: str = "",
                  source: str = "") -> dict | None:
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
            "dedupe": f"reply:{task_id}"}


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
                 current=None, sender=None, settle: float = SETTLE_S):
        self.store = store
        self.key_dir = Path(key_dir or key_dir_default())
        self.current = current            # tid -> record, to re-read after settling
        self.sender = sender or post
        self.settle = settle
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
              source: str = "") -> None:
        try:
            msg = reply_message(key, task_id, title, text, source)
            if msg:
                self._enqueue(msg)
        except Exception:
            log.exception("push: could not queue a reply notification")

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
            msg = self._q.get()
            try:
                self.deliver(msg)
            except Exception:
                log.exception("push: delivery failed")

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
        if msg.get("dedupe") and not self.store.claim(msg["dedupe"]):
            out["skipped"] = "already sent"
            return out
        return self.send_all(msg, out)

    def send_all(self, msg: dict, out: dict | None = None) -> dict:
        out = out if out is not None else {"sent": 0, "pruned": 0, "failed": 0, "skipped": ""}
        body = wire(msg)
        for sub in self.store.subscriptions():
            host = host_of(sub.get("endpoint"))
            try:
                payload = encrypt(body, sub["keys"]["p256dh"], sub["keys"]["auth"])
                status = self.sender(sub["endpoint"], payload,
                                     vapid_header(sub["endpoint"], self.key_dir),
                                     "high" if msg.get("task") else "normal")
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

ACTIONS = ("key", "subscribe", "unsubscribe", "status", "test", "forget-all")


def handle(payload: dict, store: PushStore | None, notifier: Notifier,
           key_dir: Path | None = None) -> dict:
    """/push: what the dashboard and `silkworm push` ask of push.

    key          the public key to subscribe with (made on first ask)
    subscribe    {subscription: PushSubscription.toJSON(), agent?}
    unsubscribe  {endpoint}
    status       keys, subscriptions, whether cryptography is installed
    test         send a test notification to every subscription, now
    forget-all   drop every subscription
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
        if not store.count():
            return {"ok": False, "error": "no subscriptions: tap Enable notifications "
                                          "in the dashboard on the phone first"}
        out = notifier.test()
        return dict(out, ok=out["sent"] > 0, subscriptions=store.count())
    except (Unavailable, BadSubscription) as e:
        return {"ok": False, "error": str(e)}

"""Outside senders posting notifications through the Cloudflare tunnel.

The only part of Taco reachable from the internet is /hook/ (see
deploy/cloudflared/config.yml), and it needs a sender's shared secret:

    [[webhooks.senders]]
    name  = "alexa"
    token = "..."     # openssl rand -hex 24; keep it out of the repo

Wrong or missing secrets get the same 404 as a path that doesn't exist, an
IP that keeps guessing is locked out, and each sender is rate limited so a
leaked secret can't flood the screen.

Accepted notifications can also be forwarded to another service on the LAN
(Chilli, which passes them on to Telegram):

    [notifications.forward]
    url   = "http://192.168.4.105:8710/notify"
    token = "..."     # the secret Chilli knows Taco by (X-Chilli-Token)
"""

from __future__ import annotations

import hmac
import logging
import re
import time
from collections import defaultdict, deque
from typing import Any

import httpx

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 4096
MAX_TITLE = 80
MAX_TEXT = 300
MAX_KEY = 60
BAD_TRIES = 5            # wrong secrets from one IP within BAD_WINDOW_S...
BAD_WINDOW_S = 600
LOCKOUT_S = 3600         # ...lock that IP out this long
PER_SENDER_PER_MIN = 20
KINDS = {"info", "warning", "camera"}

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


class Rejected(Exception):
    """Answer with this status. 404 for anything an outsider shouldn't learn from."""

    def __init__(self, status: int):
        super().__init__(status)
        self.status = status


def clean(value: Any, limit: int) -> str:
    text = _CONTROL.sub(" ", str(value or "")).strip()
    return text[:limit].rstrip()


def _flag(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off")


def parse_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Validate and trim a notification from an outside sender."""
    title = clean(data.get("title"), MAX_TITLE)
    if not title:
        raise Rejected(400)
    kind = str(data.get("kind") or "info").lower()
    return {
        "title": title,
        "body": clean(data.get("body"), MAX_TEXT),
        "kind": kind if kind in KINDS else "info",
        "key": clean(data.get("key"), MAX_KEY) or None,
        "wake": _flag(data.get("wake"), True),
        "chime": _flag(data.get("chime"), True),
    }


async def forward(http: httpx.AsyncClient, target: dict[str, Any], n: dict[str, Any], sender: str) -> bool:
    """Pass an outside sender's notification on to target["url"]. Best effort:
    a failure is logged and never raised, so the webhook still answers."""
    url = target.get("url") or ""
    if not url:
        return False
    payload = {"title": n["title"], "body": n["body"], "kind": n["kind"], "key": n["key"] or "",
               "source": sender}
    try:
        r = await http.post(url, json=payload, headers={"X-Chilli-Token": str(target.get("token") or "")})
        r.raise_for_status()
        return True
    except httpx.HTTPError as e:
        log.warning("forwarding %r to %s failed: %s", n["title"], url, e)
        return False


class WebhookGuard:
    def __init__(self, senders: list[dict[str, Any]], clock=time.monotonic):
        # Skip entries without a usable secret rather than accept an empty one.
        self._senders = [(str(s["name"]), str(s["token"]).encode())
                         for s in senders if s.get("name") and len(str(s.get("token") or "")) >= 16]
        if len(self._senders) != len(senders):
            log.warning("webhook: ignoring senders without a name or a token of 16+ characters")
        self._clock = clock
        self._bad: dict[str, deque[float]] = defaultdict(deque)
        self._locked: dict[str, float] = {}
        self._sent: dict[str, deque[float]] = defaultdict(deque)

    @property
    def enabled(self) -> bool:
        return bool(self._senders)

    def check(self, ip: str, token: str | None) -> str:
        """Return the sender's name, or raise Rejected."""
        now = self._clock()
        if not self._senders or self._locked.get(ip, 0) > now:
            raise Rejected(404)
        given = (token or "").encode()
        sender = None
        for name, secret in self._senders:   # compare against every one: no early exit
            if hmac.compare_digest(given, secret):
                sender = name
        if sender is None:
            self._strike(ip, now)
            raise Rejected(404)
        sent = self._sent[sender]
        while sent and now - sent[0] > 60:
            sent.popleft()
        if len(sent) >= PER_SENDER_PER_MIN:
            log.warning("webhook: %s is over %d notifications a minute", sender, PER_SENDER_PER_MIN)
            raise Rejected(429)
        sent.append(now)
        return sender

    def _strike(self, ip: str, now: float) -> None:
        if len(self._bad) > 10_000:   # internet scanners rotate IPs; keep memory bounded
            self._bad = defaultdict(deque, {k: v for k, v in self._bad.items() if v and now - v[-1] <= BAD_WINDOW_S})
            self._locked = {k: t for k, t in self._locked.items() if t > now}
        bad = self._bad[ip]
        while bad and now - bad[0] > BAD_WINDOW_S:
            bad.popleft()
        bad.append(now)
        log.warning("webhook: bad secret from %s (%d in %d min)", ip, len(bad), BAD_WINDOW_S // 60)
        if len(bad) >= BAD_TRIES:
            self._locked[ip] = now + LOCKOUT_S
            bad.clear()
            log.warning("webhook: locked out %s for %d min", ip, LOCKOUT_S // 60)

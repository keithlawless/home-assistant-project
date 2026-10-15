import pytest

from taco.core import webhook as W
from taco.core.webhook import Rejected, WebhookGuard, parse_payload

SECRET = "s3cret-token-of-enough-length"
SENDERS = [{"name": "alexa", "token": SECRET}, {"name": "script", "token": "another-long-secret-123"}]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def status(fn):
    with pytest.raises(Rejected) as e:
        fn()
    return e.value.status


def test_valid_secret_names_sender():
    g = WebhookGuard(SENDERS)
    assert g.check("1.2.3.4", SECRET) == "alexa"
    assert g.check("1.2.3.4", "another-long-secret-123") == "script"


def test_wrong_or_missing_secret_is_404():
    g = WebhookGuard(SENDERS)
    assert status(lambda: g.check("1.2.3.4", "nope")) == 404
    assert status(lambda: g.check("1.2.3.4", None)) == 404
    assert status(lambda: g.check("1.2.3.4", "")) == 404


def test_no_senders_means_disabled():
    g = WebhookGuard([])
    assert not g.enabled
    assert status(lambda: g.check("1.2.3.4", SECRET)) == 404


def test_short_or_empty_tokens_ignored():
    g = WebhookGuard([{"name": "weak", "token": "short"}, {"name": "blank", "token": ""}])
    assert not g.enabled
    assert status(lambda: g.check("1.2.3.4", "")) == 404


def test_lockout_after_bad_tries_then_expires():
    clock = Clock()
    g = WebhookGuard(SENDERS, clock=clock)
    for _ in range(W.BAD_TRIES):
        status(lambda: g.check("6.6.6.6", "guess"))
    assert status(lambda: g.check("6.6.6.6", SECRET)) == 404      # even the right secret
    assert g.check("1.2.3.4", SECRET) == "alexa"                  # other IPs unaffected
    clock.t += W.LOCKOUT_S + 1
    assert g.check("6.6.6.6", SECRET) == "alexa"


def test_bad_tries_spread_out_dont_lock():
    clock = Clock()
    g = WebhookGuard(SENDERS, clock=clock)
    for _ in range(W.BAD_TRIES * 2):
        status(lambda: g.check("6.6.6.6", "guess"))
        clock.t += W.BAD_WINDOW_S / (W.BAD_TRIES - 1) + 1
    assert g.check("6.6.6.6", SECRET) == "alexa"


def test_rate_limit_per_sender():
    clock = Clock()
    g = WebhookGuard(SENDERS, clock=clock)
    for _ in range(W.PER_SENDER_PER_MIN):
        g.check("1.2.3.4", SECRET)
    assert status(lambda: g.check("1.2.3.4", SECRET)) == 429
    assert g.check("1.2.3.4", "another-long-secret-123") == "script"
    clock.t += 61
    assert g.check("1.2.3.4", SECRET) == "alexa"


def test_payload_cleaned_and_trimmed():
    n = parse_payload({"title": "Motion\x00 at\n" + "x" * 200, "body": "b" * 1000, "kind": "CAMERA",
                       "key": "front door", "wake": "false", "chime": "0"})
    assert "\x00" not in n["title"] and "\n" not in n["title"] and len(n["title"]) <= W.MAX_TITLE
    assert len(n["body"]) == W.MAX_TEXT
    assert n["kind"] == "camera" and n["key"] == "front door"
    assert n["wake"] is False and n["chime"] is False


def test_payload_defaults_and_bad_kind():
    n = parse_payload({"title": "Hi", "kind": "health"})
    assert n == {"title": "Hi", "body": "", "kind": "info", "key": None, "wake": True, "chime": True}


def test_payload_needs_title():
    assert status(lambda: parse_payload({"title": "  \x07 "})) == 400

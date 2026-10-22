import asyncio
import json
import logging
import stat
import time

import httpx
import pytest

from taco.core.oauth import OAuthError, OAuthToken

TOKEN_URL = "https://auth.example/token"
API_URL = "https://api.example/data"


class FakeService:
    """Token endpoint that rotates refresh tokens, plus one protected API."""

    def __init__(self, valid_refresh: set[str], expires_in: int = 10800):
        self.valid_refresh = set(valid_refresh)
        self.expires_in = expires_in
        self.refreshes = 0
        self.valid_access: set[str] = set()
        self.api_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            assert form["grant_type"] == "refresh_token" and form["client_id"] == "cid"
            if form["refresh_token"] not in self.valid_refresh:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.valid_refresh.discard(form["refresh_token"])   # rotation retires the old one
            self.refreshes += 1
            access, refresh = f"access-{self.refreshes}", f"refresh-{self.refreshes}"
            self.valid_access = {access}
            self.valid_refresh.add(refresh)
            return httpx.Response(200, json={"access_token": access, "refresh_token": refresh,
                                             "expires_in": self.expires_in})
        self.api_calls += 1
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in self.valid_access:
            return httpx.Response(403, json={"error": {"code": 3, "message": "Access token expired"}})
        return httpx.Response(200, json={"ok": True})


def make(tmp_path, seed="seed-token"):
    return OAuthToken("svc", TOKEN_URL, "cid", "secret", seed, tmp_path)


def run(service, coro_fn):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(service.handler)) as http:
            return await coro_fn(http)
    return asyncio.run(go())


def test_seed_then_rotated_token_persisted_privately(tmp_path):
    svc = FakeService({"seed-token"})
    tok = make(tmp_path)
    assert run(svc, tok.access_token) == "access-1"
    path = tmp_path / "svc.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["refresh_token"] == "refresh-1"


def test_no_refresh_until_near_expiry(tmp_path):
    svc = FakeService({"seed-token"})
    tok = make(tmp_path)
    run(svc, tok.access_token)
    run(svc, tok.access_token)
    assert svc.refreshes == 1
    tok._tok["expires_at"] = time.time() + 60   # inside the early-refresh window
    assert run(svc, tok.access_token) == "access-2"


def test_restart_uses_stored_token_not_retired_seed(tmp_path):
    svc = FakeService({"seed-token"})
    run(svc, make(tmp_path).access_token)
    again = make(tmp_path)                       # a taco-core restart
    again._tok["expires_at"] = 0
    assert run(svc, again.access_token) == "access-2"


def test_new_seed_in_config_replaces_store(tmp_path):
    svc = FakeService({"seed-token"})
    run(svc, make(tmp_path).access_token)
    svc.valid_refresh = {"fresh-seed"}           # re-authorized by hand
    assert run(svc, make(tmp_path, seed="fresh-seed").access_token) == "access-2"


def test_rejected_stored_token_falls_back_to_seed(tmp_path):
    svc = FakeService({"seed-token"})
    tok = make(tmp_path)
    run(svc, tok.access_token)
    svc.valid_refresh = {"seed-token"}           # stored token revoked, seed still good
    assert run(svc, lambda http: tok.access_token(http, force=True)) == "access-2"


def test_all_rejected_raises(tmp_path):
    with pytest.raises(OAuthError):
        run(FakeService(set()), make(tmp_path).access_token)


def test_request_refreshes_once_on_expired_access(tmp_path):
    svc = FakeService({"seed-token"})
    tok = make(tmp_path)
    run(svc, tok.access_token)
    svc.valid_access = set()                     # server expired it early
    r = run(svc, lambda http: tok.request(http, "GET", API_URL))
    assert r.status_code == 200 and svc.refreshes == 2 and svc.api_calls == 2


def test_concurrent_callers_share_one_refresh(tmp_path):
    svc = FakeService({"seed-token"})
    tok = make(tmp_path)

    async def both(http):
        return await asyncio.gather(tok.access_token(http), tok.access_token(http))
    assert run(svc, both) == ["access-1", "access-1"]
    assert svc.refreshes == 1


def test_tokens_never_logged(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="taco.core.oauth")
    svc = FakeService({"seed-token"})
    tok = make(tmp_path)
    run(svc, tok.access_token)
    svc.valid_refresh = set()
    with pytest.raises(OAuthError):
        run(svc, lambda http: tok.access_token(http, force=True))
    for secret in ("seed-token", "refresh-1", "access-1", "secret"):
        assert secret not in caplog.text


def test_missing_credentials_rejected(tmp_path):
    with pytest.raises(ValueError):
        OAuthToken("svc", TOKEN_URL, "", "secret", "seed", tmp_path)

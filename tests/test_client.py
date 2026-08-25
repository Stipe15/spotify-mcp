from __future__ import annotations

import asyncio

import httpx2
import pytest

from spotify_mcp.api.client import SpotifyClient
from spotify_mcp.config import Settings
from spotify_mcp.errors import RateLimited, SpotifyAPIError


class FakeTokenManager:
    def __init__(self, token: str = "AT"):
        self.token = token
        self.refresh_calls = 0

    async def bearer_token(self) -> str:
        return self.token

    async def force_refresh(self) -> str:
        self.refresh_calls += 1
        self.token = f"{self.token}-refreshed"
        return self.token


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch: pytest.MonkeyPatch):
    async def noop(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", noop)


def _client(settings: Settings, handler) -> SpotifyClient:
    transport = httpx2.MockTransport(handler)
    return SpotifyClient(settings, FakeTokenManager(), transport=transport)


@pytest.mark.asyncio
async def test_get_returns_json_body(settings: Settings):
    client = _client(settings, lambda r: httpx2.Response(200, json={"id": "me"}))
    result = await client.get("/me")
    assert result == {"id": "me"}
    await client.aclose()


@pytest.mark.asyncio
async def test_204_returns_empty_dict(settings: Settings):
    client = _client(settings, lambda r: httpx2.Response(204))
    result = await client.put("/playlists/x/items", json={"uris": []})
    assert result == {}
    await client.aclose()


@pytest.mark.asyncio
async def test_dry_run_skips_writes_but_not_reads(settings: Settings):
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return httpx2.Response(200, json={"ok": True})

    dry_settings = Settings(**{**settings.__dict__, "dry_run": True})
    client = _client(dry_settings, handler)

    write_result = await client.post("/playlists", json={"name": "x"})
    assert write_result["dry_run"] is True
    assert calls["n"] == 0

    read_result = await client.get("/me")
    assert read_result == {"ok": True}
    assert calls["n"] == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_429_retries_after_retry_after_header_then_succeeds(settings: Settings):
    attempts = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx2.Response(429, headers={"Retry-After": "1"})
        return httpx2.Response(200, json={"ok": True})

    client = _client(settings, handler)
    result = await client.get("/search")
    assert result == {"ok": True}
    assert attempts["n"] == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_429_exhausting_retries_raises_rate_limited(settings: Settings):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, headers={"Retry-After": "2"})

    client = _client(settings, handler)
    with pytest.raises(RateLimited) as exc_info:
        await client.get("/search")
    assert exc_info.value.retry_after_s == 2.0
    await client.aclose()


@pytest.mark.asyncio
async def test_5xx_retries_then_raises_after_max_retries(settings: Settings):
    attempts = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts["n"] += 1
        return httpx2.Response(503)

    client = _client(settings, handler)
    with pytest.raises(SpotifyAPIError) as exc_info:
        await client.get("/me")
    assert exc_info.value.status_code == 503
    # max_retries=2 (from the settings fixture) -> initial + 2 retries = 3 attempts
    assert attempts["n"] == 3
    await client.aclose()


@pytest.mark.asyncio
async def test_401_forces_refresh_and_retries_once(settings: Settings):
    attempts = {"n": 0}
    seen_tokens = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts["n"] += 1
        seen_tokens.append(request.headers["authorization"])
        if attempts["n"] == 1:
            return httpx2.Response(401)
        return httpx2.Response(200, json={"ok": True})

    transport = httpx2.MockTransport(handler)
    tokens = FakeTokenManager("AT")
    client = SpotifyClient(settings, tokens, transport=transport)

    result = await client.get("/me")
    assert result == {"ok": True}
    assert tokens.refresh_calls == 1
    assert seen_tokens == ["Bearer AT", "Bearer AT-refreshed"]
    await client.aclose()


@pytest.mark.asyncio
async def test_repeated_401_after_refresh_raises_instead_of_looping(settings: Settings):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401)

    client = _client(settings, handler)
    with pytest.raises(SpotifyAPIError) as exc_info:
        await client.get("/me")
    assert exc_info.value.status_code == 401
    await client.aclose()


@pytest.mark.asyncio
async def test_error_body_message_and_reason_are_surfaced(settings: Settings):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            403, json={"error": {"status": 403, "message": "restricted", "reason": "FORBIDDEN"}}
        )

    client = _client(settings, handler)
    with pytest.raises(SpotifyAPIError) as exc_info:
        await client.get("/playlists/other-users-playlist/items")
    assert exc_info.value.status_code == 403
    assert exc_info.value.reason == "FORBIDDEN"
    assert "restricted" in str(exc_info.value)
    await client.aclose()

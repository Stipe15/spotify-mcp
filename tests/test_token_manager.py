from __future__ import annotations

import asyncio

import httpx2
import pytest

from spotify_mcp.auth.manager import TokenManager
from spotify_mcp.auth.store import StoredToken, read_token, write_token
from spotify_mcp.config import Settings
from spotify_mcp.errors import AuthError, TokenRefreshError


def _client_counting(handler) -> tuple[httpx2.AsyncClient, list[int]]:
    calls = {"n": 0}

    def wrapped(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return handler(request)

    transport = httpx2.MockTransport(wrapped)
    return httpx2.AsyncClient(transport=transport), calls


@pytest.mark.asyncio
async def test_bearer_token_raises_when_never_authorized(settings: Settings):
    http, _ = _client_counting(lambda r: httpx2.Response(200))
    manager = TokenManager(settings, http_client=http)
    with pytest.raises(AuthError):
        await manager.bearer_token()
    await manager.aclose()


@pytest.mark.asyncio
async def test_exchange_code_stores_token_and_returns_it(settings: Settings):
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert b"grant_type=authorization_code" in request.content
        return httpx2.Response(
            200,
            json={
                "access_token": "AT1",
                "refresh_token": "RT1",
                "expires_in": 3600,
                "scope": "a b",
            },
        )

    http, calls = _client_counting(handler)
    manager = TokenManager(settings, http_client=http)
    await manager.exchange_code(code="c", code_verifier="v", redirect_uri=settings.redirect_uri)

    assert calls["n"] == 1
    assert await manager.bearer_token() == "AT1"
    assert manager.is_authorized
    assert manager.granted_scopes == ["a", "b"]
    await manager.aclose()


@pytest.mark.asyncio
async def test_bearer_token_does_not_refresh_when_far_from_expiry(settings: Settings, fixed_time):
    write_token(
        settings.token_path,
        StoredToken(
            access_token="AT_VALID",
            refresh_token="RT",
            expires_at=fixed_time["now"] + 3000,
            scope="a",
            obtained_at=fixed_time["now"],
        ),
    )
    http, calls = _client_counting(lambda r: httpx2.Response(500))  # would fail loudly if hit
    manager = TokenManager(settings, http_client=http)
    token = await manager.bearer_token()
    assert token == "AT_VALID"
    assert calls["n"] == 0
    await manager.aclose()


@pytest.mark.asyncio
async def test_bearer_token_refreshes_when_within_skew(settings: Settings, fixed_time):
    write_token(
        settings.token_path,
        StoredToken(
            access_token="AT_OLD",
            refresh_token="RT",
            expires_at=fixed_time["now"] + 10,
            scope="a",
            obtained_at=fixed_time["now"],
        ),
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert b"grant_type=refresh_token" in request.content
        assert b"RT" in request.content
        return httpx2.Response(
            200, json={"access_token": "AT_NEW", "expires_in": 3600, "scope": "a"}
        )

    http, calls = _client_counting(handler)
    manager = TokenManager(settings, http_client=http)
    token = await manager.bearer_token()
    assert token == "AT_NEW"
    assert calls["n"] == 1
    await manager.aclose()


@pytest.mark.asyncio
async def test_refresh_preserves_refresh_token_when_omitted(settings: Settings, fixed_time):
    write_token(
        settings.token_path,
        StoredToken(
            access_token="AT_OLD",
            refresh_token="RT_ORIGINAL",
            expires_at=fixed_time["now"] + 10,
            scope="a",
            obtained_at=fixed_time["now"],
        ),
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, json={"access_token": "AT_NEW", "expires_in": 3600, "scope": "a"}
        )

    http, _ = _client_counting(handler)
    manager = TokenManager(settings, http_client=http)
    await manager.bearer_token()

    stored = read_token(settings.token_path)
    assert stored.refresh_token == "RT_ORIGINAL"
    await manager.aclose()


@pytest.mark.asyncio
async def test_invalid_grant_clears_token_and_raises(settings: Settings, fixed_time):
    write_token(
        settings.token_path,
        StoredToken(
            access_token="AT_OLD",
            refresh_token="RT_BAD",
            expires_at=fixed_time["now"] + 10,
            scope="a",
            obtained_at=fixed_time["now"],
        ),
    )
    http, _ = _client_counting(lambda r: httpx2.Response(400, json={"error": "invalid_grant"}))
    manager = TokenManager(settings, http_client=http)

    with pytest.raises(TokenRefreshError):
        await manager.bearer_token()
    assert not manager.is_authorized
    assert not settings.token_path.exists()
    await manager.aclose()


@pytest.mark.asyncio
async def test_concurrent_bearer_token_calls_produce_single_refresh(settings: Settings, fixed_time):
    write_token(
        settings.token_path,
        StoredToken(
            access_token="AT_OLD",
            refresh_token="RT",
            expires_at=fixed_time["now"] + 10,
            scope="a",
            obtained_at=fixed_time["now"],
        ),
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, json={"access_token": "AT_NEW", "expires_in": 3600, "scope": "a"}
        )

    http, calls = _client_counting(handler)
    manager = TokenManager(settings, http_client=http)

    results = await asyncio.gather(*[manager.bearer_token() for _ in range(10)])

    assert calls["n"] == 1
    assert all(r == "AT_NEW" for r in results)
    await manager.aclose()

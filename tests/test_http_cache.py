"""ETag conditional-GET caching (PLAN.md §6). Verified against the real API
that Spotify sends `ETag` + `Cache-Control: max-age=0`, so this never skips
the network — it only skips re-parsing the body on a 304.
"""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest

from spotify_mcp.api.cache import ETagCache, cache_key
from spotify_mcp.api.client import SpotifyClient
from spotify_mcp.config import Settings


class FakeTokenManager:
    async def bearer_token(self) -> str:
        return "AT"

    async def force_refresh(self) -> str:
        return "AT"


def test_cache_key_is_order_independent():
    a = cache_key("/search", {"q": "x", "limit": 5})
    b = cache_key("/search", {"limit": 5, "q": "x"})
    assert a == b


def test_cache_key_differs_by_path_or_params():
    assert cache_key("/tracks/1", None) != cache_key("/tracks/2", None)
    assert cache_key("/search", {"q": "a"}) != cache_key("/search", {"q": "b"})


def test_cache_put_and_get_roundtrip(tmp_path: Path):
    cache = ETagCache(tmp_path / "etag.sqlite3")
    try:
        assert cache.get("k1") is None
        cache.put("k1", "W/abc", {"id": "me"})
        etag, body = cache.get("k1")
        assert etag == "W/abc"
        assert body == {"id": "me"}
    finally:
        cache.close()


def test_cache_put_overwrites_existing_entry(tmp_path: Path):
    cache = ETagCache(tmp_path / "etag.sqlite3")
    try:
        cache.put("k1", "W/v1", {"id": "old"})
        cache.put("k1", "W/v2", {"id": "new"})
        etag, body = cache.get("k1")
        assert etag == "W/v2"
        assert body == {"id": "new"}
    finally:
        cache.close()


def test_stats_reports_entry_count(tmp_path: Path):
    cache = ETagCache(tmp_path / "etag.sqlite3")
    try:
        assert cache.stats()["entries"] == 0
        cache.put("k1", "e1", {"a": 1})
        cache.put("k2", "e2", {"b": 2})
        assert cache.stats()["entries"] == 2
    finally:
        cache.close()


def _client_with_cache(
    settings: Settings, handler, tmp_path: Path
) -> tuple[SpotifyClient, ETagCache]:
    cache = ETagCache(tmp_path / "etag.sqlite3")
    transport = httpx2.MockTransport(handler)
    client = SpotifyClient(settings, FakeTokenManager(), transport=transport, etag_cache=cache)
    return client, cache


@pytest.mark.asyncio
async def test_second_get_sends_if_none_match_and_returns_cached_body_on_304(
    settings: Settings, tmp_path: Path
):
    calls = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        inm = request.headers.get("if-none-match")
        calls.append(inm)
        if inm == '"v1"':
            return httpx2.Response(304)  # no body, as a real 304 has none
        return httpx2.Response(200, json={"id": "me", "v": 1}, headers={"ETag": '"v1"'})

    client, cache = _client_with_cache(settings, handler, tmp_path)
    try:
        first = await client.get("/me")
        second = await client.get("/me")
    finally:
        await client.aclose()
        cache.close()

    assert calls == [None, '"v1"']  # first request has no cached etag yet
    assert first == {"id": "me", "v": 1}
    assert second == {"id": "me", "v": 1}  # served from cache on the 304


@pytest.mark.asyncio
async def test_changed_response_updates_the_cached_body(settings: Settings, tmp_path: Path):
    responses = [
        httpx2.Response(200, json={"v": 1}, headers={"ETag": '"v1"'}),
        httpx2.Response(200, json={"v": 2}, headers={"ETag": '"v2"'}),
    ]

    def handler(request: httpx2.Request) -> httpx2.Response:
        return responses.pop(0)

    client, cache = _client_with_cache(settings, handler, tmp_path)
    try:
        first = await client.get("/me")
        second = await client.get("/me")

        assert first == {"v": 1}
        assert second == {"v": 2}  # server sent fresh data; cache reflects it
        key = cache_key("/me", None)
        etag, body = cache.get(key)
        assert etag == '"v2"'
        assert body == {"v": 2}
    finally:
        await client.aclose()
        cache.close()


@pytest.mark.asyncio
async def test_no_cache_means_no_conditional_header_and_no_caching(settings: Settings):
    seen_headers = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_headers.append(request.headers.get("if-none-match"))
        return httpx2.Response(200, json={"id": "me"}, headers={"ETag": '"v1"'})

    transport = httpx2.MockTransport(handler)
    client = SpotifyClient(settings, FakeTokenManager(), transport=transport)  # no etag_cache
    try:
        await client.get("/me")
        await client.get("/me")
    finally:
        await client.aclose()

    assert seen_headers == [None, None]
    assert client.cache_stats() is None


@pytest.mark.asyncio
async def test_non_get_requests_are_never_cached(settings: Settings, tmp_path: Path):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(201, json={"id": "new"}, headers={"ETag": '"v1"'})

    client, cache = _client_with_cache(settings, handler, tmp_path)
    try:
        await client.post("/me/playlists", json={"name": "x"})
        assert cache.stats()["entries"] == 0
    finally:
        await client.aclose()
        cache.close()

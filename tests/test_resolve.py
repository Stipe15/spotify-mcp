"""Resolver orchestration: cache hits, retry/paging/relaxed-search sequence,
negative caching, and strict-mode gating — with a mocked Spotify transport,
no real network.
"""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest

from spotify_mcp.api.client import SpotifyClient
from spotify_mcp.resolver.cache import ResolutionCache
from spotify_mcp.resolver.resolve import Candidate, Resolved, Unresolved, resolve_candidates


class FixedTokenManager:
    async def bearer_token(self) -> str:
        return "AT"

    async def force_refresh(self) -> str:
        return "AT"


def _track_json(name: str, artist_name: str, artist_id: str = "a1", track_id: str = "t1") -> dict:
    return {
        "id": track_id,
        "name": name,
        "uri": f"spotify:track:{track_id}",
        "duration_ms": 200_000,
        "artists": [{"id": artist_id, "name": artist_name, "uri": f"spotify:artist:{artist_id}"}],
        "album": {
            "id": "al1",
            "name": "Album",
            "uri": "spotify:album:al1",
            "release_date": "2020-01-01",
        },
        "external_ids": {"isrc": "US1234567890"},
    }


def _search_response(tracks: list[dict]) -> httpx2.Response:
    return httpx2.Response(200, json={"tracks": {"items": tracks}})


def make_client(settings, handler) -> SpotifyClient:
    transport = httpx2.MockTransport(handler)
    return SpotifyClient(settings, FixedTokenManager(), transport=transport)


@pytest.mark.asyncio
async def test_exact_match_resolves_on_first_search(settings, tmp_path: Path):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return _search_response([_track_json("Blinding Lights", "The Weeknd")])

    client = make_client(settings, handler)
    cache = ResolutionCache(tmp_path / "cache.duckdb")
    try:
        outcome = await resolve_candidates(
            client, cache, [Candidate(artist="The Weeknd", title="Blinding Lights")]
        )
    finally:
        cache.close()
        await client.aclose()

    assert len(outcome.resolved) == 1
    assert outcome.resolved[0].confidence == "exact"
    assert outcome.resolved[0].method == "search"


@pytest.mark.asyncio
async def test_cache_hit_makes_zero_http_calls(settings, tmp_path: Path):
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return _search_response([_track_json("Blinding Lights", "The Weeknd")])

    cache_path = tmp_path / "cache.duckdb"
    candidate = Candidate(artist="The Weeknd", title="Blinding Lights")

    client1 = make_client(settings, handler)
    cache1 = ResolutionCache(cache_path)
    try:
        await resolve_candidates(client1, cache1, [candidate])
    finally:
        cache1.close()
        await client1.aclose()
    assert calls["n"] == 1

    client2 = make_client(settings, handler)
    cache2 = ResolutionCache(cache_path)
    try:
        outcome2 = await resolve_candidates(client2, cache2, [candidate])
    finally:
        cache2.close()
        await client2.aclose()

    assert calls["n"] == 1  # no new HTTP call on the second, cached resolution
    assert outcome2.resolved[0].method == "cache"
    assert outcome2.resolved[0].uri == "spotify:track:t1"


@pytest.mark.asyncio
async def test_pages_once_when_first_page_has_no_acceptable_match(settings, tmp_path: Path):
    offsets_seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        offset = int(request.url.params.get("offset", "0"))
        offsets_seen.append(offset)
        if offset == 0:
            return _search_response([_track_json("Blinding Lights", "Wrong Artist")])
        return _search_response([_track_json("Blinding Lights", "The Weeknd")])

    client = make_client(settings, handler)
    cache = ResolutionCache(tmp_path / "cache.duckdb")
    try:
        outcome = await resolve_candidates(
            client, cache, [Candidate(artist="The Weeknd", title="Blinding Lights")]
        )
    finally:
        cache.close()
        await client.aclose()

    assert offsets_seen == [0, 10]
    assert len(outcome.resolved) == 1
    assert outcome.resolved[0].method == "search_offset"


@pytest.mark.asyncio
async def test_falls_back_to_relaxed_search_when_paging_also_fails(settings, tmp_path: Path):
    query_types = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        q = request.url.params.get("q", "")
        query_types.append("filtered" if 'track:"' in q else "relaxed")
        if 'track:"' in q:
            return _search_response([])  # nothing on either filtered page
        return _search_response([_track_json("Blinding Lights", "The Weeknd")])

    client = make_client(settings, handler)
    cache = ResolutionCache(tmp_path / "cache.duckdb")
    try:
        outcome = await resolve_candidates(
            client, cache, [Candidate(artist="The Weeknd", title="Blinding Lights")]
        )
    finally:
        cache.close()
        await client.aclose()

    assert query_types == ["filtered", "filtered", "relaxed"]
    assert len(outcome.resolved) == 1
    assert outcome.resolved[0].method == "search_relaxed"


@pytest.mark.asyncio
async def test_every_input_ends_up_in_resolved_or_unresolved_never_dropped(
    settings, tmp_path: Path
):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return _search_response([])  # nothing ever matches

    client = make_client(settings, handler)
    cache = ResolutionCache(tmp_path / "cache.duckdb")
    candidates = [
        Candidate(artist="Artist A", title="Song A"),
        Candidate(artist="Artist B", title="Song B"),
    ]
    try:
        outcome = await resolve_candidates(client, cache, candidates)
    finally:
        cache.close()
        await client.aclose()

    assert len(outcome.resolved) == 0
    assert len(outcome.unresolved) == 2
    assert {u.input.artist for u in outcome.unresolved} == {"Artist A", "Artist B"}
    for u in outcome.unresolved:
        assert isinstance(u, Unresolved)
        assert u.reason


@pytest.mark.asyncio
async def test_negative_result_is_cached_and_not_researched(settings, tmp_path: Path):
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return _search_response([])

    cache_path = tmp_path / "cache.duckdb"
    candidate = Candidate(artist="Nobody", title="Nothing")

    client1 = make_client(settings, handler)
    cache1 = ResolutionCache(cache_path)
    try:
        await resolve_candidates(client1, cache1, [candidate])
    finally:
        cache1.close()
        await client1.aclose()
    calls_after_first = calls["n"]
    assert calls_after_first == 3  # filtered + offset + relaxed, all empty

    client2 = make_client(settings, handler)
    cache2 = ResolutionCache(cache_path)
    try:
        outcome2 = await resolve_candidates(client2, cache2, [candidate])
    finally:
        cache2.close()
        await client2.aclose()

    assert calls["n"] == calls_after_first  # no new calls; served from negative cache
    assert len(outcome2.unresolved) == 1
    assert "cached" in outcome2.unresolved[0].reason


@pytest.mark.asyncio
async def test_strict_mode_rejects_weak_matches_into_unresolved(settings, tmp_path: Path):
    def handler(request: httpx2.Request) -> httpx2.Response:
        # A fuzzy-but-not-exact title, same artist.
        return _search_response([_track_json("Blinding Light", "The Weeknd")])

    client = make_client(settings, handler)
    cache = ResolutionCache(tmp_path / "cache.duckdb")
    try:
        strict_outcome = await resolve_candidates(
            client, cache, [Candidate(artist="The Weeknd", title="Blinding Lights")], strict=True
        )
    finally:
        cache.close()
        await client.aclose()

    assert len(strict_outcome.resolved) == 0
    assert len(strict_outcome.unresolved) == 1
    assert "weak" in strict_outcome.unresolved[0].reason


@pytest.mark.asyncio
async def test_non_strict_mode_accepts_weak_matches_flagged_as_such(settings, tmp_path: Path):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return _search_response([_track_json("Blinding Light", "The Weeknd")])

    client = make_client(settings, handler)
    cache = ResolutionCache(tmp_path / "cache.duckdb")
    try:
        outcome = await resolve_candidates(
            client, cache, [Candidate(artist="The Weeknd", title="Blinding Lights")], strict=False
        )
    finally:
        cache.close()
        await client.aclose()

    assert len(outcome.resolved) == 1
    assert outcome.resolved[0].confidence == "weak"


@pytest.mark.asyncio
async def test_second_track_by_same_artist_reuses_verified_artist_id(settings, tmp_path: Path):
    """Once an artist id is confirmed for one candidate, a second candidate
    by the same input artist name should match by id even against a result
    whose artist name string differs slightly."""
    search_queries = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        q = request.url.params.get("q", "")
        search_queries.append(q)
        if "Song One" in q:
            return _search_response([_track_json("Song One", "The Weeknd", track_id="t1")])
        # Slightly different artist credit string on this result.
        return _search_response(
            [_track_json("Song Two", "The Weeknd ", artist_id="a1", track_id="t2")]
        )

    client = make_client(settings, handler)
    cache = ResolutionCache(tmp_path / "cache.duckdb")
    try:
        outcome = await resolve_candidates(
            client,
            cache,
            [
                Candidate(artist="The Weeknd", title="Song One"),
                Candidate(artist="The Weeknd", title="Song Two"),
            ],
        )
    finally:
        cache.close()
        await client.aclose()

    assert len(outcome.resolved) == 2


@pytest.mark.asyncio
async def test_rejected_candidates_are_surfaced_on_unresolved(settings, tmp_path: Path):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return _search_response([_track_json("Song - Karaoke Version", "The Artist")])

    client = make_client(settings, handler)
    cache = ResolutionCache(tmp_path / "cache.duckdb")
    try:
        outcome = await resolve_candidates(
            client, cache, [Candidate(artist="The Artist", title="Song")]
        )
    finally:
        cache.close()
        await client.aclose()

    assert len(outcome.unresolved) == 1
    assert outcome.unresolved[0].rejected_candidates  # not empty — shows what was rejected and why


def test_resolution_rate_property():
    from spotify_mcp.resolver.resolve import ResolveOutcome

    assert ResolveOutcome(resolved=[Resolved] * 3, unresolved=[]).resolution_rate == 1.0
    assert ResolveOutcome(resolved=[], unresolved=[Unresolved] * 3).resolution_rate == 0.0
    assert ResolveOutcome(resolved=[1], unresolved=[1]).resolution_rate == 0.5
    assert ResolveOutcome(resolved=[], unresolved=[]).resolution_rate == 1.0

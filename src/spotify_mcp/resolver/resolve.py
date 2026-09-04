"""Resolver orchestration (PLAN.md §7): cache lookup -> filtered search ->
score -> page once -> relaxed retry -> give up. Every input candidate ends
up in exactly one of `resolved` or `unresolved` — never silently dropped,
never silently substituted.

Bounded concurrency reuses SpotifyClient's own semaphore/rate-limiter for
the actual HTTP traffic; the modest cap here just keeps a huge candidate
list from firing hundreds of tasks into that queue at once.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from spotify_mcp.api.client import SpotifyClient
from spotify_mcp.api.models import Track
from spotify_mcp.resolver.cache import ResolutionCache
from spotify_mcp.resolver.match import pick_best
from spotify_mcp.resolver.normalize import normalize_key, normalize_text

_MAX_CONCURRENT_RESOLUTIONS = 8
_SEARCH_PAGE_SIZE = 10


@dataclass
class Candidate:
    artist: str
    title: str
    album: str | None = None
    year: str | None = None


@dataclass
class Resolved:
    input: Candidate
    uri: str
    track_id: str
    matched_artist: str
    matched_title: str
    isrc: str | None
    confidence: str
    method: str


@dataclass
class Unresolved:
    input: Candidate
    reason: str
    rejected_candidates: list[str] = field(default_factory=list)


@dataclass
class ResolveOutcome:
    resolved: list[Resolved]
    unresolved: list[Unresolved]

    @property
    def resolution_rate(self) -> float:
        total = len(self.resolved) + len(self.unresolved)
        return (len(self.resolved) / total) if total else 1.0


async def resolve_candidates(
    spotify: SpotifyClient,
    cache: ResolutionCache,
    candidates: list[Candidate],
    *,
    strict: bool = True,
) -> ResolveOutcome:
    if not candidates:
        return ResolveOutcome(resolved=[], unresolved=[])

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_RESOLUTIONS)
    lock = asyncio.Lock()
    verified_artist_ids: dict[
        str, str
    ] = {}  # normalized input artist -> verified Spotify artist id

    async def bounded(candidate: Candidate) -> Resolved | Unresolved:
        async with semaphore:
            return await _resolve_one(spotify, cache, candidate, verified_artist_ids, lock, strict)

    results = await asyncio.gather(*(bounded(c) for c in candidates))
    resolved = [r for r in results if isinstance(r, Resolved)]
    unresolved = [r for r in results if isinstance(r, Unresolved)]
    return ResolveOutcome(resolved=resolved, unresolved=unresolved)


async def _resolve_one(
    spotify: SpotifyClient,
    cache: ResolutionCache,
    candidate: Candidate,
    verified_artist_ids: dict[str, str],
    lock: asyncio.Lock,
    strict: bool,
) -> Resolved | Unresolved:
    norm_key = normalize_key(candidate.artist, candidate.title)
    norm_artist = normalize_text(candidate.artist)

    cached = cache.get(norm_key)
    if cached is not None:
        if cached.is_negative:
            return Unresolved(input=candidate, reason="previously unresolved (cached)")
        if strict and cached.confidence == "weak":
            return Unresolved(
                input=candidate,
                reason=(
                    "only a weak/fuzzy match was found (cached); strict mode requires exact/strong"
                ),
                rejected_candidates=[cached.track_uri] if cached.track_uri else [],
            )
        async with lock:
            verified_artist_ids.setdefault(norm_artist, cached.artist_id)
        return Resolved(
            input=candidate,
            uri=cached.track_uri,
            track_id=cached.track_id,
            matched_artist=cached.matched_artist_name or candidate.artist,
            matched_title=cached.matched_title or candidate.title,
            isrc=cached.isrc,
            confidence=cached.confidence,
            method="cache",
        )

    async with lock:
        verified_id = verified_artist_ids.get(norm_artist)

    search_calls = 0
    method = "search"

    tracks, search_calls = await _search_filtered(spotify, candidate, offset=0, calls=search_calls)
    best = pick_best(tracks, candidate.artist, candidate.title, candidate.album, verified_id)

    if best is None:
        tracks, search_calls = await _search_filtered(
            spotify, candidate, offset=_SEARCH_PAGE_SIZE, calls=search_calls
        )
        best = pick_best(tracks, candidate.artist, candidate.title, candidate.album, verified_id)
        method = "search_offset"

    if best is None:
        tracks, search_calls = await _search_relaxed(spotify, candidate, calls=search_calls)
        best = pick_best(tracks, candidate.artist, candidate.title, candidate.album, verified_id)
        method = "search_relaxed"

    if best is None:
        cache.put_negative(norm_key, method=method, search_calls=search_calls)
        return Unresolved(
            input=candidate,
            reason="no acceptable match found after filtered, paged, and relaxed search",
            rejected_candidates=[t.uri for t in tracks[:5]],
        )

    isrc = best.track.external_ids.isrc if best.track.external_ids else None
    cache.put_positive(
        norm_key,
        track_uri=best.track.uri,
        track_id=best.track.id,
        artist_id=best.matched_artist_id,
        matched_artist_name=best.matched_artist_name,
        matched_title=best.matched_title,
        isrc=isrc,
        confidence=best.confidence,
        method=method,
        search_calls=search_calls,
    )
    async with lock:
        verified_artist_ids.setdefault(norm_artist, best.matched_artist_id)

    if strict and best.confidence == "weak":
        return Unresolved(
            input=candidate,
            reason="only a weak/fuzzy match was found; strict mode requires exact/strong",
            rejected_candidates=[best.track.uri],
        )

    return Resolved(
        input=candidate,
        uri=best.track.uri,
        track_id=best.track.id,
        matched_artist=best.matched_artist_name,
        matched_title=best.matched_title,
        isrc=isrc,
        confidence=best.confidence,
        method=method,
    )


async def _search_filtered(
    spotify: SpotifyClient, candidate: Candidate, *, offset: int, calls: int
) -> tuple[list[Track], int]:
    q_parts = [f'track:"{candidate.title}"', f'artist:"{candidate.artist}"']
    if candidate.year:
        q_parts.append(f"year:{candidate.year}")
    tracks = await _run_search(spotify, " ".join(q_parts), offset)
    return tracks, calls + 1


async def _search_relaxed(
    spotify: SpotifyClient, candidate: Candidate, *, calls: int
) -> tuple[list[Track], int]:
    """Drop the track: filter, keep artist: — free-text title matching is
    looser on Spotify's side, but match.py still verifies the artist and
    title strictly on the results, so this never lowers acceptance criteria,
    only widens what gets a chance to be checked against them."""
    q = f'{candidate.title} artist:"{candidate.artist}"'
    tracks = await _run_search(spotify, q, offset=0)
    return tracks, calls + 1


async def _run_search(spotify: SpotifyClient, query: str, offset: int) -> list[Track]:
    data = await spotify.get(
        "/search",
        params={"q": query, "type": "track", "limit": _SEARCH_PAGE_SIZE, "offset": offset},
    )
    items = ((data.get("tracks") or {}).get("items")) or []
    tracks = []
    for item in items:
        try:
            tracks.append(Track.model_validate(item))
        except Exception:  # noqa: BLE001 - a malformed result is skipped, not fatal
            continue
    return tracks

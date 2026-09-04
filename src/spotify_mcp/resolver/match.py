"""Candidate scoring and artist-ID verification. Pure functions, no I/O —
the other half of PLAN.md §7's "this is where it fails silently if you're
careless."

The core rule: a result is never accepted on title text alone. The artist
must match too (by normalized name, or by a previously-verified Spotify
artist id for the same input artist), and the candidate must not trip a
variant-recording marker the input didn't ask for.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

from spotify_mcp.api.models import SimplifiedArtist, Track
from spotify_mcp.resolver.normalize import (
    has_uncalled_for_variant_marker,
    normalize_text,
    strip_bracketed_suffixes,
)

Confidence = Literal["exact", "strong", "weak"]

# Below this fuzzy-match ratio (on normalized, suffix-stripped titles), a
# title is not considered a match at all, weak or otherwise.
_WEAK_MATCH_THRESHOLD = 0.85


@dataclass
class MatchResult:
    track: Track
    confidence: Confidence
    matched_artist_id: str
    matched_artist_name: str
    matched_title: str


def find_matching_artist(
    input_artist: str, candidate_artists: list[SimplifiedArtist], verified_artist_id: str | None
) -> SimplifiedArtist | None:
    """A candidate artist matches if its id was already verified for this
    input artist earlier in the batch, or if its normalized name matches
    the input. Verifying by id first means a second, third, ... track by an
    artist we've already confirmed doesn't re-litigate a name-string match
    that could drift (e.g. "Bad Bunny" vs "Bad Bunny " with a trailing
    space some catalog entries carry)."""
    if verified_artist_id:
        for artist in candidate_artists:
            if artist.id == verified_artist_id:
                return artist
    target = normalize_text(input_artist)
    for artist in candidate_artists:
        if normalize_text(artist.name) == target:
            return artist
    return None


def _title_confidence(input_title: str, candidate_title: str) -> Confidence | None:
    input_norm = normalize_text(input_title)
    candidate_norm = normalize_text(candidate_title)
    if input_norm == candidate_norm:
        return "exact"

    candidate_stripped = normalize_text(strip_bracketed_suffixes(candidate_title))
    if input_norm == candidate_stripped:
        return "strong"

    ratio = SequenceMatcher(None, input_norm, candidate_stripped).ratio()
    if ratio >= _WEAK_MATCH_THRESHOLD:
        return "weak"
    return None


def score_candidate(
    track: Track,
    input_artist: str,
    input_title: str,
    input_album: str | None,
    verified_artist_id: str | None,
) -> MatchResult | None:
    """Returns a MatchResult if `track` is an acceptable match for the
    input, else None. Never returns a confidence for a track that trips an
    uncalled-for variant marker, no matter how well the title matches."""
    artist = find_matching_artist(input_artist, track.artists, verified_artist_id)
    if artist is None:
        return None

    marker = has_uncalled_for_variant_marker(track.name, input_title)
    if marker is None and track.album is not None:
        marker = has_uncalled_for_variant_marker(track.album.name, input_album or "")
    if marker is not None:
        return None

    confidence = _title_confidence(input_title, track.name)
    if confidence is None:
        return None

    return MatchResult(
        track=track,
        confidence=confidence,
        matched_artist_id=artist.id,
        matched_artist_name=artist.name,
        matched_title=track.name,
    )


_CONFIDENCE_RANK: dict[Confidence, int] = {"exact": 3, "strong": 2, "weak": 1}


def pick_best(
    tracks: list[Track],
    input_artist: str,
    input_title: str,
    input_album: str | None,
    verified_artist_id: str | None,
) -> MatchResult | None:
    """Scores every candidate and returns the best acceptable match, if any.
    Ties on confidence break toward the earlier release (the original
    recording is usually the earliest one), then toward a non-explicit
    result if the input's explicitness is unknown."""
    scored = [
        score_candidate(t, input_artist, input_title, input_album, verified_artist_id)
        for t in tracks
    ]
    acceptable = [m for m in scored if m is not None]
    if not acceptable:
        return None

    def sort_key(m: MatchResult) -> tuple[int, str]:
        release_date = m.track.album.release_date if m.track.album else "9999-99-99"
        return (-_CONFIDENCE_RANK[m.confidence], release_date or "9999-99-99")

    acceptable.sort(key=sort_key)
    return acceptable[0]

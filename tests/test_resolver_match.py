"""Resolver matching logic — the primary correctness target for Phase 5
(PLAN.md): "This is where it fails silently if you're careless." Pure unit
tests over normalize.py and match.py, no network.
"""

from __future__ import annotations

from spotify_mcp.api.models import ExternalIds, SimplifiedAlbum, SimplifiedArtist, Track
from spotify_mcp.resolver.match import find_matching_artist, pick_best, score_candidate
from spotify_mcp.resolver.normalize import (
    extract_featured_artists,
    has_uncalled_for_variant_marker,
    normalize_key,
    normalize_text,
    strip_bracketed_suffixes,
)


def _track(
    name: str,
    artist_name: str,
    *,
    artist_id: str = "a1",
    album_name: str = "Album",
    release_date: str = "2020-01-01",
    isrc: str | None = None,
    track_id: str = "t1",
) -> Track:
    return Track(
        id=track_id,
        name=name,
        uri=f"spotify:track:{track_id}",
        duration_ms=200_000,
        artists=[
            SimplifiedArtist(id=artist_id, name=artist_name, uri=f"spotify:artist:{artist_id}")
        ],
        album=SimplifiedAlbum(
            id="al1", name=album_name, uri="spotify:album:al1", release_date=release_date
        ),
        external_ids=ExternalIds(isrc=isrc) if isrc else ExternalIds(),
    )


# ---------------------------------------------------------------------------
# normalize.py
# ---------------------------------------------------------------------------


def test_normalize_text_strips_diacritics_and_leading_the():
    assert normalize_text("The Beatles") == "beatles"
    assert normalize_text("Café del Mar") == "cafe del mar"


def test_normalize_text_collapses_whitespace_and_unifies_punctuation():
    assert normalize_text("  Hello   World  ") == "hello world"
    assert normalize_text("Rock ’n’ Roll") == normalize_text("Rock 'n' Roll")


def test_normalize_text_is_none_safe():
    assert normalize_text(None) == ""
    assert normalize_text("") == ""


def test_normalize_key_is_stable_across_case_and_the():
    assert normalize_key("The Weeknd", "Blinding Lights") == normalize_key(
        "the weeknd", "BLINDING LIGHTS"
    )


def test_strip_bracketed_suffixes_removes_single_and_stacked_suffixes():
    assert strip_bracketed_suffixes("Blinding Lights - Single Version") == "Blinding Lights"
    assert strip_bracketed_suffixes("Song (Remastered 2011)") == "Song"
    assert strip_bracketed_suffixes("Song (Live) - Remastered") == "Song"


def test_strip_bracketed_suffixes_is_a_noop_on_plain_titles():
    assert strip_bracketed_suffixes("Blinding Lights") == "Blinding Lights"


def test_extract_featured_artists():
    assert extract_featured_artists("Song (feat. Drake)") == ("Song", ["Drake"])
    assert extract_featured_artists("Song featuring Drake and Future") == (
        "Song",
        ["Drake", "Future"],
    )
    assert extract_featured_artists("Plain Song") == ("Plain Song", [])


def test_variant_marker_rejects_karaoke_cover_tribute_sped_up_remix():
    for candidate, expected_marker in [
        ("Song - Karaoke Version", "karaoke"),
        ("Song (Tribute Band Cover)", "tribute"),
        ("Song (Sped Up)", "sped up"),
        ("Song (Slowed + Reverb)", "slowed"),
        ("Song - Remix", "remix"),
        ("Song (Nightcore)", "nightcore"),
        ("Song - Instrumental", "instrumental"),
    ]:
        assert has_uncalled_for_variant_marker(candidate, "Song") == expected_marker


def test_variant_marker_allows_explicit_live_request():
    """If the user's own input already says (Live), a candidate's 'live at'/
    'live from' phrasing is not flagged — several live-ish phrasings share
    the root keyword 'live'."""
    assert has_uncalled_for_variant_marker("Song - Live at Wembley", "Song (Live)") is None
    assert has_uncalled_for_variant_marker("Song (Live from Paris)", "Song Live") is None


def test_variant_marker_returns_none_for_a_clean_match():
    assert has_uncalled_for_variant_marker("Blinding Lights", "Blinding Lights") is None


# ---------------------------------------------------------------------------
# match.py
# ---------------------------------------------------------------------------


def test_find_matching_artist_by_normalized_name():
    artists = [SimplifiedArtist(id="a1", name="The Weeknd", uri="spotify:artist:a1")]
    found = find_matching_artist("the weeknd", artists, verified_artist_id=None)
    assert found is not None
    assert found.id == "a1"


def test_find_matching_artist_prefers_verified_id_over_name():
    artists = [
        SimplifiedArtist(id="wrong", name="The Weeknd", uri="spotify:artist:wrong"),
        SimplifiedArtist(id="right", name="Some Other Name", uri="spotify:artist:right"),
    ]
    found = find_matching_artist("The Weeknd", artists, verified_artist_id="right")
    assert found.id == "right"


def test_find_matching_artist_returns_none_when_no_match():
    artists = [SimplifiedArtist(id="a1", name="Someone Else", uri="spotify:artist:a1")]
    assert find_matching_artist("The Weeknd", artists, verified_artist_id=None) is None


def test_score_candidate_exact_match():
    track = _track("Blinding Lights", "The Weeknd")
    result = score_candidate(track, "The Weeknd", "Blinding Lights", None, None)
    assert result is not None
    assert result.confidence == "exact"
    assert result.matched_artist_id == "a1"


def test_score_candidate_rejects_wrong_artist_even_with_identical_title():
    """The single most important guarantee: title match alone is never enough."""
    track = _track("Blinding Lights", "Some Cover Band")
    result = score_candidate(track, "The Weeknd", "Blinding Lights", None, None)
    assert result is None


def test_score_candidate_rejects_karaoke_even_with_matching_artist_credit():
    track = _track("Blinding Lights - Karaoke Version", "The Weeknd")
    result = score_candidate(track, "The Weeknd", "Blinding Lights", None, None)
    assert result is None


def test_score_candidate_rejects_sped_up_edit():
    track = _track("Blinding Lights (Sped Up)", "The Weeknd")
    assert score_candidate(track, "The Weeknd", "Blinding Lights", None, None) is None


def test_score_candidate_rejects_live_version_when_not_requested():
    track = _track("Blinding Lights - Live at Wembley", "The Weeknd")
    assert score_candidate(track, "The Weeknd", "Blinding Lights", None, None) is None


def test_score_candidate_rejects_tribute_band():
    track = _track("Blinding Lights (In the Style of The Weeknd)", "Tribute Artists")
    assert score_candidate(track, "The Weeknd", "Blinding Lights", None, None) is None


def test_score_candidate_accepts_strong_match_via_suffix_stripping():
    track = _track("Blinding Lights - Single Version", "The Weeknd")
    result = score_candidate(track, "The Weeknd", "Blinding Lights", None, None)
    assert result is not None
    assert result.confidence == "strong"


def test_score_candidate_rejects_below_weak_threshold():
    track = _track("A Completely Different Song Title", "The Weeknd")
    assert score_candidate(track, "The Weeknd", "Blinding Lights", None, None) is None


def test_score_candidate_verifies_by_id_for_known_artist():
    """Once an artist id is verified for this input artist earlier in a
    batch, a track credited to that id matches even if the artist name on
    this particular result differs slightly (e.g. trailing whitespace,
    a regional credit variant)."""
    track = _track("Blinding Lights", "The Weeknd ", artist_id="a1")
    result = score_candidate(track, "The Weeknd", "Blinding Lights", None, verified_artist_id="a1")
    assert result is not None


def test_pick_best_prefers_exact_over_strong():
    strong = _track("Blinding Lights - Single Version", "The Weeknd", track_id="strong")
    exact = _track("Blinding Lights", "The Weeknd", track_id="exact")
    best = pick_best([strong, exact], "The Weeknd", "Blinding Lights", None, None)
    assert best.track.id == "exact"


def test_pick_best_prefers_earlier_release_among_same_confidence_ties():
    later = _track("Blinding Lights", "The Weeknd", track_id="later", release_date="2022-01-01")
    earlier = _track("Blinding Lights", "The Weeknd", track_id="earlier", release_date="2020-01-01")
    best = pick_best([later, earlier], "The Weeknd", "Blinding Lights", None, None)
    assert best.track.id == "earlier"


def test_pick_best_returns_none_when_nothing_acceptable():
    wrong_artist = _track("Blinding Lights", "Someone Else")
    karaoke = _track("Blinding Lights - Karaoke", "The Weeknd")
    assert pick_best([wrong_artist, karaoke], "The Weeknd", "Blinding Lights", None, None) is None


def test_pick_best_returns_none_for_empty_candidate_list():
    assert pick_best([], "The Weeknd", "Blinding Lights", None, None) is None


def test_never_a_near_match_substitution_across_a_tricky_candidate_set():
    """The scenario from PLAN.md's own Phase 5 verification step: a mixed
    batch of a real track alongside a karaoke version, a sped-up edit, a
    live album cut, and a tribute-band recording — only the real one may
    ever be accepted."""
    tricky_candidates = [
        _track("Bohemian Rhapsody - Karaoke Version", "Queen", track_id="karaoke"),
        _track("Bohemian Rhapsody (Sped Up)", "Queen", track_id="sped_up"),
        _track("Bohemian Rhapsody - Live at Wembley", "Queen", track_id="live"),
        _track(
            "Bohemian Rhapsody", "Queen Tribute Band", artist_id="tribute_id", track_id="tribute"
        ),
        _track("Bohemian Rhapsody", "Queen", track_id="real"),
    ]
    best = pick_best(tricky_candidates, "Queen", "Bohemian Rhapsody", None, None)
    assert best is not None
    assert best.track.id == "real"
    assert best.confidence == "exact"

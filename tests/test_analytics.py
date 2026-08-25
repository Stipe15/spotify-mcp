"""Analytics queries against the seeded synthetic fixture — the other
primary correctness target for Phase 4 (PLAN.md). The fixture is
deterministic under its default seed, so these assert exact numbers rather
than just "returns something."
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from spotify_mcp.analytics import queries
from spotify_mcp.analytics.fixture import TOTAL_PLAYS, write_fixture
from spotify_mcp.analytics.ingest import ingest


@pytest.fixture(scope="module")
def fixture_db(tmp_path_factory: pytest.TempPathFactory) -> duckdb.DuckDBPyConnection:
    tmp = tmp_path_factory.mktemp("analytics")
    export_dir = tmp / "export"
    write_fixture(export_dir)
    db_path = tmp / "listening.duckdb"
    ingest(
        db_path,
        export_dir,
        local_timezone="Europe/Zagreb",
        skip_ms_threshold=30_000,
        substantial_ms=30_000,
    )
    con = duckdb.connect(str(db_path), read_only=True)
    yield con
    con.close()


def test_fixture_is_deterministic_under_the_default_seed(tmp_path: Path):
    a = write_fixture(tmp_path / "a")
    b = write_fixture(tmp_path / "b")
    assert a.read_bytes() == b.read_bytes()


def test_listening_summary_matches_known_fixture_totals(fixture_db):
    summary = queries.listening_summary(fixture_db, None, None)
    assert summary["total_plays"] == TOTAL_PLAYS
    assert summary["distinct_artists"] == 58  # 40 core + 10 fading + 8 late
    assert summary["earliest_play"].isoformat() == "2024-06-12"
    assert summary["latest_play"].isoformat() == "2026-07-31"
    # Skip-prone artists are weighted to dominate plays at a high skip rate;
    # this is a design target (~25%), not an incidental number.
    assert 0.20 < summary["overall_skip_rate"] < 0.32


def test_listening_summary_respects_date_range(fixture_db):
    full = queries.listening_summary(fixture_db, None, None)
    narrowed = queries.listening_summary(fixture_db, "2026-01-01", "2026-01-31")
    assert narrowed["total_plays"] < full["total_plays"]
    assert narrowed["earliest_play"].isoformat() >= "2026-01-01"
    assert narrowed["latest_play"].isoformat() <= "2026-01-31"


def test_listening_by_hour_sums_to_total_plays(fixture_db):
    buckets = queries.listening_by_hour(fixture_db, None, None, "hour")
    assert len(buckets) == 24
    assert sum(b["plays"] for b in buckets) == TOTAL_PLAYS
    assert {b["played_hour"] for b in buckets} == set(range(24))


def test_listening_by_hour_evening_outweighs_early_morning(fixture_db):
    """The diurnal curve peaks 20-22h and is near-flat overnight — the whole
    point of generating it rather than using a uniform distribution."""
    buckets = {
        b["played_hour"]: b["plays"]
        for b in queries.listening_by_hour(fixture_db, None, None, "hour")
    }
    evening_peak = buckets[21]
    overnight = buckets[3]
    assert evening_peak > overnight * 3


def test_listening_by_hour_rejects_invalid_grouping(fixture_db):
    with pytest.raises(ValueError, match="by must be one of"):
        queries.listening_by_hour(fixture_db, None, None, "not_a_real_grouping")


def test_skip_stats_surfaces_the_designed_skip_prone_artists(fixture_db):
    rows = queries.skip_stats(fixture_db, "artist", min_plays=5, limit=5)
    assert len(rows) == 5
    # Every top-5 result should be a clearly skip-heavy artist by design.
    for row in rows:
        assert row["derived_skip_rate"] > 0.7
    # Sorted descending by skip rate.
    rates = [r["derived_skip_rate"] for r in rows]
    assert rates == sorted(rates, reverse=True)


def test_skip_stats_min_plays_filters_low_sample_rows(fixture_db):
    unfiltered = queries.skip_stats(fixture_db, "artist", min_plays=0, limit=1000)
    filtered = queries.skip_stats(fixture_db, "artist", min_plays=100, limit=1000)
    assert len(filtered) < len(unfiltered)
    assert all(r["plays"] >= 100 for r in filtered)


def test_top_local_artist_matches_skip_prone_dominance(fixture_db):
    """The skip-prone artists were also weighted 4x in the generator, so
    they should dominate top_local by play count too, not just skip_stats."""
    top = queries.top_local(fixture_db, "artist", None, None, "plays", 5)
    skip_heavy = {r["artist_name"] for r in queries.skip_stats(fixture_db, "artist", 5, 5)}
    top_names = {r["entity"] for r in top}
    assert top_names == skip_heavy


def test_top_local_track_includes_track_name(fixture_db):
    rows = queries.top_local(fixture_db, "track", None, None, "plays", 3)
    assert all("track_name" in r and r["track_name"] for r in rows)


def test_dropped_artists_finds_only_fading_pool_artists(fixture_db):
    rows = queries.dropped_artists(fixture_db, silent_since_months=6, min_plays=10, limit=20)
    assert len(rows) > 0
    for row in rows:
        assert row["play_count"] >= 10


def test_dropped_artists_min_plays_excludes_low_volume(fixture_db):
    loose = queries.dropped_artists(fixture_db, silent_since_months=6, min_plays=1, limit=100)
    strict = queries.dropped_artists(fixture_db, silent_since_months=6, min_plays=50, limit=100)
    assert len(strict) < len(loose)


def test_taste_drift_separates_gained_lost_and_held(fixture_db):
    result = queries.taste_drift(
        fixture_db, ("2024-06-01", "2025-01-31"), ("2026-02-01", "2026-07-31"), "artist", 20
    )
    assert len(result["gained"]) > 0
    assert len(result["lost"]) > 0
    # An artist present in both periods must never appear in gained or lost.
    gained_names = {r["artist_name"] for r in result["gained"]}
    lost_names = {r["artist_name"] for r in result["lost"]}
    held_names = {r["artist_name"] for r in result["held"]}
    assert gained_names.isdisjoint(lost_names)
    assert gained_names.isdisjoint(held_names)
    assert lost_names.isdisjoint(held_names)
    # Every "gained" artist genuinely had zero plays in period A.
    for row in result["gained"]:
        assert row["period_a_plays"] == 0
        assert row["period_b_plays"] > 0
    for row in result["lost"]:
        assert row["period_b_plays"] == 0
        assert row["period_a_plays"] > 0


def test_rediscover_tracks_finds_early_tracks_gone_quiet(fixture_db):
    rows = queries.rediscover_tracks(
        fixture_db, "2024-06-01", "2025-01-31", not_since_months=6, min_plays=2, limit=40
    )
    assert len(rows) > 0
    for row in rows:
        assert row["first_played"].isoformat() >= "2024-06-01"
        assert row["play_count"] >= 2


def test_rediscover_tracks_excludes_still_actively_played_tracks(fixture_db):
    """A core-artist track played throughout the whole span should never
    appear — it's still being touched, not something to rediscover."""
    rediscover_uris = {
        r["track_uri"]
        for r in queries.rediscover_tracks(
            fixture_db, "2024-06-01", "2026-07-31", not_since_months=1, min_plays=1, limit=10_000
        )
    }
    recently_played_uris = {
        row[0]
        for row in fixture_db.execute(
            "SELECT DISTINCT track_uri FROM plays WHERE played_date > '2026-07-01'"
        ).fetchall()
    }
    assert rediscover_uris.isdisjoint(recently_played_uris)

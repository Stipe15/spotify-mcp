"""Ingest: schema tolerance (unknown fields preserved and reported, never
fatal) and loud failure (missing required fields stop the whole run) —
PLAN.md R4. Also covers the idempotent re-run guarantee.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from spotify_mcp.analytics.ingest import IngestError, ingest

_DEFAULTS = {"local_timezone": "UTC", "skip_ms_threshold": 30_000, "substantial_ms": 30_000}


def _write_export(export_dir: Path, filename: str, records: list[dict]) -> None:
    export_dir.mkdir(parents=True, exist_ok=True)
    (export_dir / filename).write_text(json.dumps(records), encoding="utf-8")


def _record(**overrides) -> dict:
    base = {
        "ts": "2026-01-15T10:00:00Z",
        "ms_played": 200_000,
        "master_metadata_track_name": "Song",
        "master_metadata_album_artist_name": "Artist",
        "master_metadata_album_album_name": "Album",
        "spotify_track_uri": "spotify:track:abc123",
        "reason_start": "trackdone",
        "reason_end": "trackdone",
        "shuffle": False,
        "skipped": False,
        "offline": False,
        "incognito_mode": False,
        "platform": "ios",
        "conn_country": "HR",
    }
    base.update(overrides)
    return base


def test_ingest_requires_at_least_one_json_file(tmp_path: Path):
    empty_dir = tmp_path / "export"
    empty_dir.mkdir()
    with pytest.raises(IngestError, match="No .json files"):
        ingest(tmp_path / "db.duckdb", empty_dir, **_DEFAULTS)


def test_missing_required_field_fails_the_whole_run_loudly(tmp_path: Path):
    export_dir = tmp_path / "export"
    bad_record = _record()
    del bad_record["ms_played"]
    _write_export(export_dir, "endsong_0.json", [_record(), bad_record])

    db_path = tmp_path / "db.duckdb"
    with pytest.raises(IngestError, match=r"missing required field.*ms_played"):
        ingest(db_path, export_dir, **_DEFAULTS)

    # Nothing was committed: the fact table stays empty after a failed run.
    con = duckdb.connect(str(db_path))
    assert con.execute("SELECT COUNT(*) FROM plays").fetchone()[0] == 0


def test_unknown_fields_are_preserved_and_reported_not_fatal(tmp_path: Path):
    export_dir = tmp_path / "export"
    _write_export(
        export_dir, "endsong_0.json", [_record(some_field_spotify_added_later="surprise")]
    )
    report = ingest(tmp_path / "db.duckdb", export_dir, **_DEFAULTS)

    assert report.rows_inserted == 1
    assert report.unknown_fields == ["some_field_spotify_added_later"]

    con = duckdb.connect(str(tmp_path / "db.duckdb"))
    payload = con.execute("SELECT payload FROM raw_plays").fetchone()[0]
    assert json.loads(payload)["some_field_spotify_added_later"] == "surprise"


def test_non_array_json_file_fails_loudly(tmp_path: Path):
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "endsong_0.json").write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with pytest.raises(IngestError, match="expected a JSON array"):
        ingest(tmp_path / "db.duckdb", export_dir, **_DEFAULTS)


def test_non_object_record_fails_loudly(tmp_path: Path):
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "endsong_0.json").write_text(
        json.dumps([_record(), "not an object"]), encoding="utf-8"
    )
    with pytest.raises(IngestError, match="expected an object"):
        ingest(tmp_path / "db.duckdb", export_dir, **_DEFAULTS)


def test_invalid_json_fails_loudly(tmp_path: Path):
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "endsong_0.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(IngestError, match="not valid JSON"):
        ingest(tmp_path / "db.duckdb", export_dir, **_DEFAULTS)


def test_invalid_timezone_fails_loudly(tmp_path: Path):
    export_dir = tmp_path / "export"
    _write_export(export_dir, "endsong_0.json", [_record()])
    with pytest.raises(IngestError, match="not a recognized IANA timezone"):
        ingest(
            tmp_path / "db.duckdb",
            export_dir,
            local_timezone="Not/A_Real_Zone",
            skip_ms_threshold=30_000,
            substantial_ms=30_000,
        )


def test_rerunning_ingest_on_unchanged_export_is_a_no_op(tmp_path: Path):
    export_dir = tmp_path / "export"
    _write_export(
        export_dir, "endsong_0.json", [_record(), _record(spotify_track_uri="spotify:track:def456")]
    )
    db_path = tmp_path / "db.duckdb"

    first = ingest(db_path, export_dir, **_DEFAULTS)
    assert first.files_ingested == 1
    assert first.rows_inserted == 2

    second = ingest(db_path, export_dir, **_DEFAULTS)
    assert second.files_ingested == 0
    assert second.files_skipped_already_ingested == 1
    assert second.rows_inserted == 0
    assert second.total_plays_after_rebuild == first.total_plays_after_rebuild


def test_adding_a_new_file_ingests_only_the_new_one(tmp_path: Path):
    export_dir = tmp_path / "export"
    _write_export(export_dir, "endsong_0.json", [_record()])
    db_path = tmp_path / "db.duckdb"
    ingest(db_path, export_dir, **_DEFAULTS)

    _write_export(export_dir, "endsong_1.json", [_record(spotify_track_uri="spotify:track:new")])
    second = ingest(db_path, export_dir, **_DEFAULTS)

    assert second.files_ingested == 1
    assert second.files_skipped_already_ingested == 1
    assert second.total_plays_after_rebuild == 2


def test_byte_identical_files_in_one_run_are_caught_by_the_hash_skip_list(tmp_path: Path):
    """Two files with identical content (a duplicate export window) hash the
    same, so the second is skipped by the ingested_files check — even though
    both are seen within the same run, not just across separate runs."""
    export_dir = tmp_path / "export"
    same_play = _record()
    _write_export(export_dir, "endsong_0.json", [same_play])
    _write_export(export_dir, "endsong_1.json", [same_play])
    db_path = tmp_path / "db.duckdb"

    report = ingest(db_path, export_dir, **_DEFAULTS)
    assert report.files_ingested == 1
    assert report.files_skipped_already_ingested == 1
    assert report.rows_inserted == 1
    assert report.total_plays_after_rebuild == 1


def test_duplicate_play_across_two_distinct_files_is_deduplicated(tmp_path: Path):
    """The same (track_uri, ts, ms_played) appearing in two files that are
    NOT byte-identical (different filenames/content elsewhere in the file)
    still collapses to one play via the row-level dedup in _rebuild_plays,
    since the hash skip-list can't catch this case."""
    export_dir = tmp_path / "export"
    same_play = _record()
    _write_export(export_dir, "endsong_0.json", [same_play])
    # A second file with a padding record so its content (and hash) differs,
    # while still containing the same play.
    _write_export(
        export_dir,
        "endsong_1.json",
        [same_play, _record(spotify_track_uri="spotify:track:padding")],
    )
    db_path = tmp_path / "db.duckdb"

    report = ingest(db_path, export_dir, **_DEFAULTS)
    assert report.files_ingested == 2
    assert report.rows_inserted == 3  # all three raw rows kept
    assert report.total_plays_after_rebuild == 2  # the true duplicate play collapses


def test_music_and_podcast_rows_get_correct_content_type(tmp_path: Path):
    export_dir = tmp_path / "export"
    podcast = _record(
        master_metadata_track_name=None,
        master_metadata_album_artist_name=None,
        master_metadata_album_album_name=None,
        spotify_track_uri=None,
        episode_name="Ep 1",
        episode_show_name="Show",
        spotify_episode_uri="spotify:episode:xyz",
    )
    _write_export(export_dir, "endsong_0.json", [_record(), podcast])
    db_path = tmp_path / "db.duckdb"
    ingest(db_path, export_dir, **_DEFAULTS)

    con = duckdb.connect(str(db_path))
    rows = con.execute("SELECT content_type FROM plays ORDER BY content_type").fetchall()
    assert rows == [("music",), ("podcast",)]


def test_is_skip_derivation_matches_reason_end_and_threshold(tmp_path: Path):
    export_dir = tmp_path / "export"
    skip = _record(spotify_track_uri="spotify:track:skip", ms_played=5_000, reason_end="fwdbtn")
    not_skip_short_but_wrong_reason = _record(
        spotify_track_uri="spotify:track:notskip1", ms_played=5_000, reason_end="trackdone"
    )
    not_skip_long_fwdbtn = _record(
        spotify_track_uri="spotify:track:notskip2", ms_played=200_000, reason_end="fwdbtn"
    )
    _write_export(
        export_dir, "endsong_0.json", [skip, not_skip_short_but_wrong_reason, not_skip_long_fwdbtn]
    )
    db_path = tmp_path / "db.duckdb"
    ingest(db_path, export_dir, **_DEFAULTS)

    con = duckdb.connect(str(db_path))
    rows = dict(con.execute("SELECT track_uri, is_skip FROM plays").fetchall())
    assert rows["spotify:track:skip"] is True
    assert rows["spotify:track:notskip1"] is False
    assert rows["spotify:track:notskip2"] is False

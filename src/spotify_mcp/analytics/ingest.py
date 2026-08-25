"""Export JSON -> raw_plays -> plays. Re-runnable and idempotent: files are
tracked by content hash, so ingesting an unchanged export directory a second
time skips every file rather than re-inserting duplicates. `plays` (and
therefore every view built on it) is always fully rebuilt from the complete
accumulated `raw_plays` afterward, so a code change to the derivation logic
takes effect on the next ingest without needing a fresh export.

Schema-tolerant per PLAN.md R4 (the real export's exact field names are
unverified until the user's data request arrives): unknown fields are kept
verbatim in `raw_plays.payload` and reported, never dropped. Missing REQUIRED
fields fail the whole run loudly, naming the offending file and record,
rather than silently skipping or guessing.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import duckdb

from spotify_mcp.analytics.db import connect_rw
from spotify_mcp.errors import SpotifyMCPError
from spotify_mcp.logging import get_logger

logger = get_logger("analytics.ingest")


class IngestError(SpotifyMCPError):
    pass


# Without these two, a play record means nothing. Everything else may
# legitimately be absent on some rows (podcast plays lack track fields, and
# vice versa) so nothing else is required.
REQUIRED_FIELDS = {"ts", "ms_played"}

# Every field the loader maps into `plays`. Present-but-unmapped fields are
# preserved in raw_plays.payload regardless and reported via IngestReport —
# this set only controls what counts as "unknown" for that report.
KNOWN_FIELDS = {
    "ts",
    "username",
    "platform",
    "ms_played",
    "conn_country",
    "ip_addr",
    "user_agent",
    "master_metadata_track_name",
    "master_metadata_album_artist_name",
    "master_metadata_album_album_name",
    "spotify_track_uri",
    "episode_name",
    "episode_show_name",
    "spotify_episode_uri",
    "audiobook_title",
    "audiobook_uri",
    "audiobook_chapter_uri",
    "audiobook_chapter_title",
    "reason_start",
    "reason_end",
    "shuffle",
    "skipped",
    "offline",
    "offline_timestamp",
    "incognito_mode",
}


@dataclass
class IngestReport:
    run_id: int
    files_ingested: int
    files_skipped_already_ingested: int
    rows_inserted: int
    unknown_fields: list[str]
    total_plays_after_rebuild: int


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _next_run_id(con: duckdb.DuckDBPyConnection) -> int:
    row = con.execute("SELECT COALESCE(MAX(run_id), 0) + 1 FROM ingest_runs").fetchone()
    return int(row[0])


def _validate_timezone(tz_name: str) -> None:
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise IngestError(
            f"{tz_name!r} is not a recognized IANA timezone name (e.g. 'Europe/Zagreb', "
            "'America/New_York'). Set SPOTIFY_MCP_TIMEZONE in .env to a valid one."
        ) from exc


def _load_records(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IngestError(f"{path.name}: not valid JSON ({exc}).") from exc
    if not isinstance(data, list):
        raise IngestError(
            f"{path.name}: expected a JSON array of play objects, got {type(data).__name__}."
        )
    for idx, record in enumerate(data):
        if not isinstance(record, dict):
            raise IngestError(
                f"{path.name}[{idx}]: expected an object, got {type(record).__name__}."
            )
    return data


def ingest(
    db_path: Path,
    export_dir: Path,
    *,
    local_timezone: str,
    skip_ms_threshold: int,
    substantial_ms: int,
) -> IngestReport:
    if not export_dir.is_dir():
        raise IngestError(f"{export_dir} is not a directory.")
    files = sorted(export_dir.glob("*.json"))
    if not files:
        raise IngestError(f"No .json files found in {export_dir}.")
    _validate_timezone(local_timezone)

    con = connect_rw(db_path)
    try:
        run_id = _next_run_id(con)
        started_at = time.time()
        already = {r[0] for r in con.execute("SELECT file_hash FROM ingested_files").fetchall()}

        files_ingested = 0
        files_skipped = 0
        rows_inserted = 0
        unknown_fields: set[str] = set()

        con.execute("BEGIN TRANSACTION")
        try:
            for path in files:
                digest = _file_hash(path)
                if digest in already:
                    files_skipped += 1
                    continue

                records = _load_records(path)
                for idx, record in enumerate(records):
                    missing = REQUIRED_FIELDS - record.keys()
                    if missing:
                        raise IngestError(
                            f"{path.name}[{idx}]: missing required field(s) {sorted(missing)}. "
                            f"Record: {record!r}"
                        )
                    unknown_fields |= record.keys() - KNOWN_FIELDS
                    con.execute(
                        "INSERT INTO raw_plays VALUES (?, ?, ?, ?)",
                        [run_id, path.name, idx, json.dumps(record)],
                    )
                    rows_inserted += 1

                con.execute(
                    "INSERT INTO ingested_files VALUES (?, ?, ?, ?)",
                    [digest, path.name, run_id, _now()],
                )
                # Two files ingested in the same run can be byte-identical
                # (e.g. a re-exported duplicate window) — without this, the
                # second one would collide on ingested_files' file_hash PK
                # instead of being skipped, since `already` was only
                # computed once at the top of the run.
                already.add(digest)
                files_ingested += 1

            _rebuild_plays(con, local_timezone, skip_ms_threshold, substantial_ms)
            total = con.execute("SELECT COUNT(*) FROM plays").fetchone()[0]

            con.execute(
                "INSERT INTO ingest_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    run_id,
                    _ts(started_at),
                    _now(),
                    files_ingested,
                    files_skipped,
                    rows_inserted,
                    sorted(unknown_fields),
                    "ok" if not unknown_fields else "ok_with_warnings",
                    None,
                ],
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        if unknown_fields:
            logger.warning("ingest: unrecognized fields observed: %s", sorted(unknown_fields))

        return IngestReport(
            run_id=run_id,
            files_ingested=files_ingested,
            files_skipped_already_ingested=files_skipped,
            rows_inserted=rows_inserted,
            unknown_fields=sorted(unknown_fields),
            total_plays_after_rebuild=total,
        )
    finally:
        con.close()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ts(epoch_s: float) -> datetime:
    return datetime.fromtimestamp(epoch_s, timezone.utc).replace(tzinfo=None)


def _rebuild_plays(
    con: duckdb.DuckDBPyConnection,
    local_timezone: str,
    skip_ms_threshold: int,
    substantial_ms: int,
) -> None:
    """Fully rebuild `plays` from the complete `raw_plays` table.

    Field extraction from the JSON payload assumes the community-documented
    Extended Streaming History field names (PLAN.md's own field list, cross-
    checked against public parsers). This is the one place the assumption is
    unverified until the real export arrives — see PLAN.md R4. The synthetic
    fixture matches this exact shape, so ingest is exercised end-to-end
    either way; if the real export's field names differ, this function is
    the only thing that needs to change, not the schema or the tools built
    on top of it.
    """
    con.execute("DELETE FROM plays")
    # local_timezone is validated as a real IANA zone above (not user-remote
    # input — it's the operator's own .env), and thresholds are ints from
    # Settings, so straight interpolation here is safe.
    con.execute(f"""
        WITH extracted AS (
            SELECT
                source_file, source_index,
                (payload->>'ts')::TIMESTAMP AS ts_utc,
                TRY_CAST(payload->>'ms_played' AS BIGINT) AS ms_played,
                NULLIF(payload->>'master_metadata_track_name', '') AS track_name,
                NULLIF(payload->>'master_metadata_album_artist_name', '') AS artist_name,
                NULLIF(payload->>'master_metadata_album_album_name', '') AS album_name,
                NULLIF(payload->>'spotify_track_uri', '') AS track_uri,
                NULLIF(payload->>'episode_name', '') AS episode_name,
                NULLIF(payload->>'episode_show_name', '') AS episode_show,
                NULLIF(payload->>'spotify_episode_uri', '') AS episode_uri,
                payload->>'reason_start' AS reason_start,
                payload->>'reason_end' AS reason_end,
                TRY_CAST(payload->>'shuffle' AS BOOLEAN) AS shuffle,
                TRY_CAST(payload->>'skipped' AS BOOLEAN) AS skipped_raw,
                TRY_CAST(payload->>'offline' AS BOOLEAN) AS offline,
                TRY_CAST(payload->>'incognito_mode' AS BOOLEAN) AS incognito,
                payload->>'platform' AS platform,
                payload->>'conn_country' AS conn_country
            FROM raw_plays
            WHERE (payload->>'ts') IS NOT NULL
        ),
        deduped AS (
            SELECT *,
                CASE WHEN COALESCE(track_uri, episode_uri) IS NOT NULL THEN
                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(track_uri, episode_uri), ts_utc, ms_played
                        ORDER BY source_file, source_index
                    )
                ELSE 1 END AS rn
            FROM extracted
        ),
        final AS (
            SELECT
                ts_utc,
                ts_utc AT TIME ZONE 'UTC' AT TIME ZONE '{local_timezone}' AS ts_local,
                ms_played, track_name, artist_name, album_name, track_uri,
                CASE WHEN track_uri IS NOT NULL THEN split_part(track_uri, ':', 3) END AS track_id,
                episode_name, episode_show, episode_uri,
                CASE WHEN track_uri IS NOT NULL THEN 'music'
                     WHEN episode_uri IS NOT NULL THEN 'podcast'
                     ELSE 'unknown' END AS content_type,
                reason_start, reason_end, shuffle, skipped_raw, offline, incognito,
                platform, conn_country
            FROM deduped
            WHERE rn = 1
        )
        INSERT INTO plays
        SELECT
            ROW_NUMBER() OVER (ORDER BY ts_utc, track_uri) AS play_id,
            ts_utc, ts_local, ms_played, track_name, artist_name, album_name,
            track_uri, track_id, episode_name, episode_show, episode_uri, content_type,
            reason_start, reason_end, shuffle, skipped_raw, offline, incognito,
            platform, conn_country,
            CAST(ts_local AS DATE) AS played_date,
            EXTRACT(YEAR FROM ts_local)::SMALLINT AS played_year,
            date_trunc('month', ts_local)::DATE AS played_month,
            EXTRACT(HOUR FROM ts_local)::TINYINT AS played_hour,
            isodow(ts_local)::TINYINT AS played_dow,
            (reason_end = 'fwdbtn' AND ms_played < {skip_ms_threshold}) AS is_skip,
            (ms_played >= {substantial_ms}) AS is_substantial
        FROM final
    """)

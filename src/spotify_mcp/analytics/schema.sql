-- Local analytics store schema (PLAN.md §4). Three layers:
--   1. raw_plays / ingest_runs / ingested_files — the export, verbatim, never mutated
--   2. plays — the normalized fact table, fully rebuilt from raw_plays on every ingest
--   3. views below — derived, always consistent with `plays` by construction

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id BIGINT PRIMARY KEY,
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    files_ingested INTEGER,
    files_skipped_already_ingested INTEGER,
    row_count BIGINT,
    unknown_fields VARCHAR[],
    status VARCHAR,
    notes VARCHAR
);

-- Tracks which export files (by content hash) have already been loaded, so
-- re-running ingest on an unchanged export directory is a genuine no-op
-- rather than re-inserting duplicate raw rows under a new run_id.
CREATE TABLE IF NOT EXISTS ingested_files (
    file_hash VARCHAR PRIMARY KEY,
    source_file VARCHAR NOT NULL,
    ingest_run_id BIGINT NOT NULL,
    ingested_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_plays (
    ingest_run_id BIGINT NOT NULL,
    source_file VARCHAR NOT NULL,
    source_index BIGINT NOT NULL,
    payload JSON NOT NULL
);

CREATE TABLE IF NOT EXISTS plays (
    play_id BIGINT,
    ts_utc TIMESTAMP,
    ts_local TIMESTAMP,
    ms_played BIGINT,

    track_name VARCHAR,
    artist_name VARCHAR,
    album_name VARCHAR,
    track_uri VARCHAR,
    track_id VARCHAR,

    episode_name VARCHAR,
    episode_show VARCHAR,
    episode_uri VARCHAR,
    content_type VARCHAR, -- 'music' | 'podcast' | 'unknown'

    reason_start VARCHAR,
    reason_end VARCHAR,
    shuffle BOOLEAN,
    skipped_raw BOOLEAN,
    offline BOOLEAN,
    incognito BOOLEAN,
    platform VARCHAR,
    conn_country VARCHAR,

    played_date DATE,
    played_year SMALLINT,
    played_month DATE,   -- first-of-month bucket
    played_hour TINYINT, -- local hour, 0-23
    played_dow TINYINT,  -- local ISO weekday, 1 (Mon) - 7 (Sun)

    is_skip BOOLEAN,        -- reason_end = 'fwdbtn' AND ms_played < skip_ms_threshold
    is_substantial BOOLEAN  -- ms_played >= substantial_ms
);

-- Per-track rollup. No `duration_ms` — the export doesn't carry it, and the
-- API can only supply it one call per distinct track (batch lookups are
-- gone); deferred past Phase 6, see PLAN.md R6.
CREATE OR REPLACE VIEW dim_track AS
SELECT
    track_uri,
    any_value(track_name) AS track_name,
    any_value(artist_name) AS artist_name,
    any_value(album_name) AS album_name,
    MIN(ts_utc) AS first_played,
    MAX(ts_utc) AS last_played,
    COUNT(*) AS play_count,
    SUM(ms_played) AS total_ms
FROM plays
WHERE track_uri IS NOT NULL
GROUP BY track_uri;

CREATE OR REPLACE VIEW dim_artist AS
SELECT
    artist_name,
    MIN(ts_utc) AS first_played,
    MAX(ts_utc) AS last_played,
    COUNT(*) AS play_count,
    SUM(ms_played) AS total_ms,
    COUNT(DISTINCT track_uri) AS distinct_tracks
FROM plays
WHERE artist_name IS NOT NULL
GROUP BY artist_name;

-- Artist x month, with each artist's share of that month's plays — the base
-- for taste_drift (compare an artist's share between two periods).
CREATE OR REPLACE VIEW v_monthly_artist AS
SELECT
    played_month,
    artist_name,
    COUNT(*) AS plays,
    SUM(ms_played) AS ms,
    COUNT(*) * 1.0 / SUM(COUNT(*)) OVER (PARTITION BY played_month) AS share_of_month
FROM plays
WHERE artist_name IS NOT NULL
GROUP BY played_month, artist_name;

CREATE OR REPLACE VIEW v_hourly AS
SELECT
    played_hour,
    played_dow,
    COUNT(*) AS plays,
    SUM(ms_played) AS ms
FROM plays
GROUP BY played_hour, played_dow;

-- Per (artist, track) skip aggregates. Both the derived `is_skip` rate and
-- the export's own raw `skipped` field are surfaced side by side — the
-- derived definition is a heuristic (see plays.is_skip comment above and
-- docs/analytics_schema.md), never presented as ground truth on its own.
CREATE OR REPLACE VIEW v_skip AS
SELECT
    artist_name,
    track_uri,
    any_value(track_name) AS track_name,
    COUNT(*) AS plays,
    SUM(CASE WHEN is_skip THEN 1 ELSE 0 END) AS derived_skips,
    AVG(CASE WHEN is_skip THEN 1.0 ELSE 0.0 END) AS derived_skip_rate,
    SUM(CASE WHEN skipped_raw THEN 1 ELSE 0 END) AS raw_skipped_count,
    AVG(ms_played) AS avg_ms_played
FROM plays
WHERE artist_name IS NOT NULL
GROUP BY artist_name, track_uri;

# Local listening-history schema

This is the schema `query_listening_history` runs guarded SQL against, and
what the nine named analytics tools are built on top of. Read-only: the
connection cannot write, and `query_listening_history` additionally rejects
anything that isn't a single `SELECT`/`WITH` statement (see
`analytics/sqlguard.py`).

## `plays` — one row per play, the only table you should query directly

| Column | Type | Notes |
| --- | --- | --- |
| `play_id` | BIGINT | Surrogate id, stable within one ingest, not across re-ingests |
| `ts_utc` | TIMESTAMP | When the play ended, UTC |
| `ts_local` | TIMESTAMP | `ts_utc` converted to the configured local timezone (`SPOTIFY_MCP_TIMEZONE`, default UTC) |
| `ms_played` | BIGINT | Milliseconds actually played |
| `track_name`, `artist_name`, `album_name` | VARCHAR | NULL for podcast/episode rows |
| `track_uri` | VARCHAR | `spotify:track:...`, NULL for episodes |
| `track_id` | VARCHAR | The id portion of `track_uri`, NULL for episodes |
| `episode_name`, `episode_show`, `episode_uri` | VARCHAR | NULL for music rows |
| `content_type` | VARCHAR | `'music'`, `'podcast'`, or `'unknown'` |
| `reason_start`, `reason_end` | VARCHAR | Spotify's own play-transition codes, e.g. `trackdone`, `fwdbtn`, `backbtn`, `clickrow` |
| `shuffle`, `offline`, `incognito` | BOOLEAN | |
| `skipped_raw` | BOOLEAN | The export's own `skipped` field — often unreliable/null, see below |
| `platform`, `conn_country` | VARCHAR | |
| `played_date` | DATE | `ts_local` truncated to the day |
| `played_year` | SMALLINT | |
| `played_month` | DATE | First-of-month bucket of `ts_local` |
| `played_hour` | TINYINT | Local hour, 0-23 |
| `played_dow` | TINYINT | Local ISO weekday, 1 (Monday) - 7 (Sunday) |
| `is_skip` | BOOLEAN | Derived: `reason_end = 'fwdbtn' AND ms_played < skip_ms_threshold` (default 30000ms) |
| `is_substantial` | BOOLEAN | Derived: `ms_played >= substantial_ms` (default 30000ms) |

**On `is_skip`:** the export's own `skipped` field is frequently null or
unreliable, and the export carries no track `duration_ms` at all (so a true
"skipped 40% through" completion ratio isn't computable locally — see
PLAN.md R6). `is_skip` is a heuristic, not ground truth. Every tool that
reports a skip rate surfaces both the derived rate and the raw field's own
rate side by side, with this definition attached, rather than presenting
either as authoritative on its own.

## Views (read-only, always consistent with `plays`)

- **`dim_track`** — one row per `track_uri`: `track_name`, `artist_name`,
  `album_name`, `first_played`, `last_played`, `play_count`, `total_ms`. No
  `duration_ms` — the export doesn't carry it and the API can only supply it
  one call per distinct track now that batch lookups are gone; deferred.
- **`dim_artist`** — one row per `artist_name`: `first_played`,
  `last_played`, `play_count`, `total_ms`, `distinct_tracks`.
- **`v_monthly_artist`** — `played_month` x `artist_name`: `plays`, `ms`,
  `share_of_month` (that artist's fraction of all plays that month). The
  base for taste-drift comparisons.
- **`v_hourly`** — `played_hour` x `played_dow`: `plays`, `ms`.
- **`v_skip`** — `artist_name` x `track_uri`: `plays`, `derived_skips`,
  `derived_skip_rate`, `raw_skipped_count`, `avg_ms_played`.

## Provenance tables (rarely queried directly)

- **`ingest_runs`** — one row per ingest, with file/row counts and any
  unrecognized fields observed in that run.
- **`ingested_files`** — content hash per already-ingested export file, so
  re-running ingest on an unchanged export is a no-op.
- **`raw_plays`** — the verbatim JSON payload of every ingested record,
  including fields not mapped into `plays`. If you need something `plays`
  doesn't expose, it's in here as `payload->>'field_name'`.

## Example queries

```sql
-- Plays by day of week, most recent 90 days
SELECT played_dow, COUNT(*) AS plays
FROM plays
WHERE played_date >= current_date - 90
GROUP BY played_dow ORDER BY played_dow;

-- Artists you played a lot in a given month but haven't touched in 6+ months
SELECT artist_name, play_count, last_played
FROM dim_artist
WHERE play_count >= 10
  AND last_played < (SELECT MAX(ts_utc) FROM plays) - INTERVAL 6 MONTH
ORDER BY play_count DESC;

-- Your most-skipped tracks (at least 5 plays, so one skip isn't noise)
SELECT artist_name, track_name, plays, derived_skip_rate
FROM v_skip
WHERE plays >= 5
ORDER BY derived_skip_rate DESC
LIMIT 20;
```

## What `query_listening_history` will refuse, and why

- More than one statement (e.g. anything with a `;` separating two queries)
- Anything that isn't a `SELECT`/`WITH` at the top level — no `INSERT`,
  `UPDATE`, `DELETE`, `CREATE`, `DROP`, `ATTACH`, `PRAGMA`, `SET`, `COPY`
- File- or extension-reaching functions: `read_csv`, `read_json`,
  `read_parquet`, `glob`, `install`, `load`, and anything ending `_auto`
  (also blocked at the connection level — `enable_external_access = false`)
- Results are capped at `analytics_max_rows` (default 5000; `truncated: true`
  if hit) and the query is cancelled if it runs past `query_timeout_s`
  (default 10s)

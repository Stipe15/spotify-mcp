"""Named, parameterized analytics queries — PLAN.md §4's Option A, the
tested layer the model reaches for before falling back to guarded free-form
SQL (`db.run_guarded` / `sqlguard`, Option B). Every query here is plain
Python building a fixed SQL template with bound parameters; no string ever
gets built from a caller-supplied SQL fragment.
"""

from __future__ import annotations

from typing import Any, Literal

import duckdb

Entity = Literal["artist", "track", "album"]


def listening_summary(
    con: duckdb.DuckDBPyConnection, start: str | None, end: str | None
) -> dict[str, Any]:
    where, params = _date_range_where("played_date", start, end)
    row = con.execute(
        f"""
        SELECT
            COUNT(*) AS total_plays,
            COUNT(DISTINCT track_uri) AS distinct_tracks,
            COUNT(DISTINCT artist_name) AS distinct_artists,
            SUM(ms_played) / 3600000.0 AS total_hours,
            MIN(played_date) AS earliest_play,
            MAX(played_date) AS latest_play,
            AVG(CASE WHEN is_skip THEN 1.0 ELSE 0.0 END) AS overall_skip_rate
        FROM plays
        {where}
        """,
        params,
    ).fetchone()
    columns = [
        "total_plays",
        "distinct_tracks",
        "distinct_artists",
        "total_hours",
        "earliest_play",
        "latest_play",
        "overall_skip_rate",
    ]
    return dict(zip(columns, row, strict=True))


def listening_by_hour(
    con: duckdb.DuckDBPyConnection, start: str | None, end: str | None, by: str
) -> list[dict[str, Any]]:
    where, params = _date_range_where("played_date", start, end)
    group_cols = {
        "hour": ["played_hour"],
        "dow": ["played_dow"],
        "hour_dow": ["played_hour", "played_dow"],
    }
    if by not in group_cols:
        raise ValueError(f"by must be one of {sorted(group_cols)}, got {by!r}")
    cols = group_cols[by]
    select_cols = ", ".join(cols)
    group_by = ", ".join(cols)
    rows = con.execute(
        f"""
        SELECT {select_cols}, COUNT(*) AS plays, SUM(ms_played) / 3600000.0 AS hours
        FROM plays
        {where}
        GROUP BY {group_by}
        ORDER BY {group_by}
        """,
        params,
    ).fetchall()
    result_cols = [*cols, "plays", "hours"]
    return [dict(zip(result_cols, row, strict=True)) for row in rows]


def skip_stats(
    con: duckdb.DuckDBPyConnection,
    group_by: Literal["artist", "track", "month"],
    min_plays: int,
    limit: int,
) -> list[dict[str, Any]]:
    if group_by == "artist":
        rows = con.execute(
            """
            SELECT artist_name, SUM(plays) AS plays, SUM(derived_skips) AS derived_skips,
                   SUM(derived_skips) * 1.0 / NULLIF(SUM(plays), 0) AS derived_skip_rate,
                   SUM(raw_skipped_count) * 1.0 / NULLIF(SUM(plays), 0) AS raw_skipped_rate
            FROM v_skip
            GROUP BY artist_name
            HAVING SUM(plays) >= ?
            ORDER BY derived_skip_rate DESC, plays DESC
            LIMIT ?
            """,
            [min_plays, limit],
        ).fetchall()
        cols = ["artist_name", "plays", "derived_skips", "derived_skip_rate", "raw_skipped_rate"]
    elif group_by == "track":
        rows = con.execute(
            """
            SELECT artist_name, track_name, track_uri, plays, derived_skips,
                   derived_skip_rate, raw_skipped_count * 1.0 / NULLIF(plays, 0) AS raw_skipped_rate
            FROM v_skip
            WHERE plays >= ?
            ORDER BY derived_skip_rate DESC, plays DESC
            LIMIT ?
            """,
            [min_plays, limit],
        ).fetchall()
        cols = [
            "artist_name",
            "track_name",
            "track_uri",
            "plays",
            "derived_skips",
            "derived_skip_rate",
            "raw_skipped_rate",
        ]
    elif group_by == "month":
        rows = con.execute(
            """
            SELECT played_month, COUNT(*) AS plays,
                   SUM(CASE WHEN is_skip THEN 1 ELSE 0 END) AS derived_skips,
                   AVG(CASE WHEN is_skip THEN 1.0 ELSE 0.0 END) AS derived_skip_rate
            FROM plays
            GROUP BY played_month
            HAVING COUNT(*) >= ?
            ORDER BY played_month
            LIMIT ?
            """,
            [min_plays, limit],
        ).fetchall()
        cols = ["played_month", "plays", "derived_skips", "derived_skip_rate"]
    else:
        raise ValueError(f"group_by must be one of artist/track/month, got {group_by!r}")
    return [dict(zip(cols, row, strict=True)) for row in rows]


def top_local(
    con: duckdb.DuckDBPyConnection,
    entity: Entity,
    start: str | None,
    end: str | None,
    metric: Literal["plays", "ms"],
    limit: int,
) -> list[dict[str, Any]]:
    where, params = _date_range_where("played_date", start, end)
    entity_col = {"artist": "artist_name", "track": "track_uri", "album": "album_name"}[entity]
    extra_select = ", any_value(track_name) AS track_name" if entity == "track" else ""
    order_expr = "COUNT(*)" if metric == "plays" else "SUM(ms_played)"
    rows = con.execute(
        _top_local_sql(where, entity_col, extra_select, order_expr),
        [*params, limit],
    ).fetchall()
    result_cols = ["entity", "artist_name", "plays", "hours"]
    if entity == "track":
        result_cols.insert(2, "track_name")
    return [dict(zip(result_cols, row, strict=True)) for row in rows]


def _top_local_sql(where: str, entity_col: str, extra_select: str, order_expr: str) -> str:
    not_null = f"{entity_col} IS NOT NULL"
    where_clause = f"{where} AND {not_null}" if where else f"WHERE {not_null}"
    return f"""
        SELECT {entity_col} AS entity, any_value(artist_name) AS artist_name{extra_select},
               COUNT(*) AS plays, SUM(ms_played) / 3600000.0 AS hours
        FROM plays
        {where_clause}
        GROUP BY {entity_col}
        ORDER BY {order_expr} DESC
        LIMIT ?
    """


def taste_drift(
    con: duckdb.DuckDBPyConnection,
    period_a: tuple[str, str],
    period_b: tuple[str, str],
    entity: Literal["artist"],
    limit: int,
) -> dict[str, Any]:
    a_start, a_end = period_a
    b_start, b_end = period_b
    a_rows = {
        r[0]: (r[1], r[2])
        for r in con.execute(
            """
            SELECT artist_name, SUM(plays) AS plays,
                   SUM(plays) * 1.0 / SUM(SUM(plays)) OVER () AS share
            FROM v_monthly_artist
            WHERE played_month >= ?::DATE AND played_month <= ?::DATE
            GROUP BY artist_name
            """,
            [a_start, a_end],
        ).fetchall()
    }
    b_rows = {
        r[0]: (r[1], r[2])
        for r in con.execute(
            """
            SELECT artist_name, SUM(plays) AS plays,
                   SUM(plays) * 1.0 / SUM(SUM(plays)) OVER () AS share
            FROM v_monthly_artist
            WHERE played_month >= ?::DATE AND played_month <= ?::DATE
            GROUP BY artist_name
            """,
            [b_start, b_end],
        ).fetchall()
    }

    gained, lost, held = [], [], []
    for artist in set(a_rows) | set(b_rows):
        a_plays, a_share = a_rows.get(artist, (0, 0.0))
        b_plays, b_share = b_rows.get(artist, (0, 0.0))
        entry = {
            "artist_name": artist,
            "period_a_plays": a_plays,
            "period_a_share": a_share,
            "period_b_plays": b_plays,
            "period_b_share": b_share,
            "share_delta": b_share - a_share,
        }
        if artist not in a_rows:
            gained.append(entry)
        elif artist not in b_rows:
            lost.append(entry)
        else:
            held.append(entry)

    gained.sort(key=lambda e: e["period_b_plays"], reverse=True)
    lost.sort(key=lambda e: e["period_a_plays"], reverse=True)
    held.sort(key=lambda e: abs(e["share_delta"]), reverse=True)
    return {"gained": gained[:limit], "lost": lost[:limit], "held": held[:limit]}


def dropped_artists(
    con: duckdb.DuckDBPyConnection,
    silent_since_months: int,
    min_plays: int,
    limit: int,
) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT artist_name, play_count, first_played, last_played
        FROM dim_artist
        WHERE play_count >= ?
          AND last_played < (SELECT MAX(ts_utc) FROM plays) - (? || ' months')::INTERVAL
        ORDER BY play_count DESC
        LIMIT ?
        """,
        [min_plays, silent_since_months, limit],
    ).fetchall()
    cols = ["artist_name", "play_count", "first_played", "last_played"]
    return [dict(zip(cols, row, strict=True)) for row in rows]


def rediscover_tracks(
    con: duckdb.DuckDBPyConnection,
    played_start: str,
    played_end: str,
    not_since_months: int,
    min_plays: int,
    limit: int,
) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT track_uri, track_name, artist_name, play_count, first_played, last_played
        FROM dim_track
        WHERE first_played >= ?::DATE AND first_played <= ?::DATE
          AND play_count >= ?
          AND last_played < (SELECT MAX(ts_utc) FROM plays) - (? || ' months')::INTERVAL
        ORDER BY play_count DESC
        LIMIT ?
        """,
        [played_start, played_end, min_plays, not_since_months, limit],
    ).fetchall()
    cols = ["track_uri", "track_name", "artist_name", "play_count", "first_played", "last_played"]
    return [dict(zip(cols, row, strict=True)) for row in rows]


def _date_range_where(column: str, start: str | None, end: str | None) -> tuple[str, list[Any]]:
    clauses = []
    params: list[Any] = []
    if start:
        clauses.append(f"{column} >= ?::DATE")
        params.append(start)
    if end:
        clauses.append(f"{column} <= ?::DATE")
        params.append(end)
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    return where, params

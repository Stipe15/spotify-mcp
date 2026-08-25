"""Local analytics tools over the DuckDB listening-history store (PLAN.md §4).

Nine tools: eight tested, named queries the model should reach for first,
plus `query_listening_history` as the guarded free-form escape hatch for
anything they don't cover. A fresh read-only connection is opened and closed
per call rather than held for the server's lifetime — `spotify-mcp ingest`
runs as a separate process and needs a read-write handle on the same file,
and a connection that isn't held open for the long haul is far less likely
to be in its way.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel

from spotify_mcp.analytics import queries
from spotify_mcp.analytics.db import QueryTimeoutError, connect_ro, run_guarded
from spotify_mcp.analytics.sqlguard import SqlGuardError
from spotify_mcp.server import AppContext

_RO = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_SCHEMA_DOC_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent / "docs" / "analytics_schema.md"
)


class ListeningSummary(BaseModel):
    total_plays: int
    distinct_tracks: int
    distinct_artists: int
    total_hours: float
    earliest_play: date | None
    latest_play: date | None
    overall_skip_rate: float | None


class ListeningByHourResult(BaseModel):
    by: str
    buckets: list[dict[str, Any]]


class SkipStatsResult(BaseModel):
    group_by: str
    rows: list[dict[str, Any]]


class TopLocalResult(BaseModel):
    entity: str
    metric: str
    rows: list[dict[str, Any]]


class TasteDriftResult(BaseModel):
    period_a: list[str]
    period_b: list[str]
    gained: list[dict[str, Any]]
    lost: list[dict[str, Any]]
    held: list[dict[str, Any]]


class DroppedArtistsResult(BaseModel):
    rows: list[dict[str, Any]]


class RediscoverTracksResult(BaseModel):
    rows: list[dict[str, Any]]


class DescribeListeningDataResult(BaseModel):
    schema_markdown: str


class QueryListeningHistoryResult(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    sql_executed: str


def _open_ro(app: AppContext):
    if not app.settings.analytics_db_path.exists():
        raise ToolError(
            "No local listening-history database yet. Run `spotify-mcp make-fixture` "
            "then `spotify-mcp ingest <fixture-dir>` to try these tools with synthetic "
            "data, or `spotify-mcp ingest <export-dir>` once your real Spotify data "
            "export has arrived."
        )
    return connect_ro(app.settings.analytics_db_path)


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        description=(
            "Describe the local listening-history schema: tables, views, columns, and "
            "example queries. Read this before writing SQL for query_listening_history — "
            "it also documents what is_skip actually means and its limitations."
        ),
        annotations=_RO,
    )
    async def describe_listening_data(ctx: Context) -> DescribeListeningDataResult:
        return DescribeListeningDataResult(
            schema_markdown=_SCHEMA_DOC_PATH.read_text(encoding="utf-8")
        )

    @mcp.tool(
        description=(
            "Overall listening totals, optionally within a date range (start/end as "
            "YYYY-MM-DD). Includes an overall skip rate — see describe_listening_data for "
            "what is_skip means and why it's a heuristic, not ground truth."
        ),
        annotations=_RO,
    )
    async def listening_summary(
        ctx: Context, start: str | None = None, end: str | None = None
    ) -> ListeningSummary:
        app: AppContext = ctx.request_context.lifespan_context
        con = _open_ro(app)
        try:
            data = queries.listening_summary(con, start, end)
        finally:
            con.close()
        return ListeningSummary(**data)

    @mcp.tool(
        description=(
            "Play counts and hours by local hour of day, day of week, or both. `by` is "
            "'hour', 'dow' (1=Monday..7=Sunday), or 'hour_dow'. Uses whatever timezone was "
            "configured at ingest (SPOTIFY_MCP_TIMEZONE) — check server_status or "
            "describe_listening_data if results look shifted from your actual clock."
        ),
        annotations=_RO,
    )
    async def listening_by_hour(
        ctx: Context,
        start: str | None = None,
        end: str | None = None,
        by: str = "hour_dow",
    ) -> ListeningByHourResult:
        app: AppContext = ctx.request_context.lifespan_context
        con = _open_ro(app)
        try:
            try:
                rows = queries.listening_by_hour(con, start, end, by)
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
        finally:
            con.close()
        return ListeningByHourResult(by=by, buckets=rows)

    @mcp.tool(
        description=(
            "Skip-rate leaderboard grouped by artist, track, or month. Reports both the "
            "derived skip rate (reason_end='fwdbtn' AND played under the skip threshold) "
            "and the export's own raw 'skipped' field's rate side by side — they can "
            "disagree; see describe_listening_data. min_plays filters out low-sample noise."
        ),
        annotations=_RO,
    )
    async def skip_stats(
        ctx: Context,
        group_by: str = "artist",
        min_plays: int = 5,
        limit: int = 20,
    ) -> SkipStatsResult:
        if group_by not in ("artist", "track", "month"):
            raise ToolError("group_by must be one of: artist, track, month.")
        app: AppContext = ctx.request_context.lifespan_context
        con = _open_ro(app)
        try:
            rows = queries.skip_stats(con, group_by, min_plays, limit)
        finally:
            con.close()
        return SkipStatsResult(group_by=group_by, rows=rows)

    @mcp.tool(
        description=(
            "Ranked artists, tracks, or albums by play count or total listening time, "
            "optionally within a date range. This is YOUR local listening history, not a "
            "live popularity signal — Spotify removed those endpoints in 2026."
        ),
        annotations=_RO,
    )
    async def top_local(
        ctx: Context,
        entity: str = "artist",
        start: str | None = None,
        end: str | None = None,
        metric: str = "plays",
        limit: int = 20,
    ) -> TopLocalResult:
        if entity not in ("artist", "track", "album"):
            raise ToolError("entity must be one of: artist, track, album.")
        if metric not in ("plays", "ms"):
            raise ToolError("metric must be one of: plays, ms.")
        app: AppContext = ctx.request_context.lifespan_context
        con = _open_ro(app)
        try:
            rows = queries.top_local(con, entity, start, end, metric, limit)
        finally:
            con.close()
        return TopLocalResult(entity=entity, metric=metric, rows=rows)

    @mcp.tool(
        description=(
            "Compare artist listening between two date periods (each [start, end] as "
            "YYYY-MM-DD) by share of total plays. Returns three lists: gained (no plays in "
            "period A, present in B), lost (present in A, none in B), held (present in "
            "both, ranked by the biggest change in share). This is how your taste drifted, "
            "by your own play counts — not an external trend signal."
        ),
        annotations=_RO,
    )
    async def taste_drift(
        ctx: Context,
        period_a_start: str,
        period_a_end: str,
        period_b_start: str,
        period_b_end: str,
        limit: int = 20,
    ) -> TasteDriftResult:
        app: AppContext = ctx.request_context.lifespan_context
        con = _open_ro(app)
        try:
            result = queries.taste_drift(
                con, (period_a_start, period_a_end), (period_b_start, period_b_end), "artist", limit
            )
        finally:
            con.close()
        return TasteDriftResult(
            period_a=[period_a_start, period_a_end],
            period_b=[period_b_start, period_b_end],
            **result,
        )

    @mcp.tool(
        description=(
            "Artists you used to play a lot but have gone quiet on: at least min_plays "
            "total plays, with none in the last silent_since_months. Ranked by historical "
            "play count."
        ),
        annotations=_RO,
    )
    async def dropped_artists(
        ctx: Context,
        silent_since_months: int = 6,
        min_plays: int = 10,
        limit: int = 20,
    ) -> DroppedArtistsResult:
        app: AppContext = ctx.request_context.lifespan_context
        con = _open_ro(app)
        try:
            rows = queries.dropped_artists(con, silent_since_months, min_plays, limit)
        finally:
            con.close()
        return DroppedArtistsResult(rows=rows)

    @mcp.tool(
        description=(
            "Tracks you played a lot during a past window (played_start/played_end, "
            "YYYY-MM-DD) but haven't touched in not_since_months — e.g. '40 tracks I played "
            "a lot in 2023 but haven't touched since'. Returns track URIs ready to hand to "
            "build_playlist_from_candidates or add_playlist_items once you've reviewed them."
        ),
        annotations=_RO,
    )
    async def rediscover_tracks(
        ctx: Context,
        played_start: str,
        played_end: str,
        not_since_months: int = 6,
        min_plays: int = 2,
        limit: int = 40,
    ) -> RediscoverTracksResult:
        app: AppContext = ctx.request_context.lifespan_context
        con = _open_ro(app)
        try:
            rows = queries.rediscover_tracks(
                con, played_start, played_end, not_since_months, min_plays, limit
            )
        finally:
            con.close()
        return RediscoverTracksResult(rows=rows)

    @mcp.tool(
        description=(
            "Run a read-only SQL query (SELECT/WITH only) against the local listening "
            "database — the escape hatch for questions the named analytics tools don't "
            "cover. Call describe_listening_data first for the schema. Exactly one "
            "statement; no INSERT/UPDATE/DELETE/CREATE/ATTACH/PRAGMA/file-reading "
            "functions — all rejected before execution. Capped at a few thousand rows "
            "(truncated: true if hit) and a several-second timeout."
        ),
        annotations=_RO,
    )
    async def query_listening_history(ctx: Context, sql: str) -> QueryListeningHistoryResult:
        app: AppContext = ctx.request_context.lifespan_context
        con = _open_ro(app)
        try:
            try:
                result = run_guarded(
                    con,
                    sql,
                    max_rows=app.settings.analytics_max_rows,
                    timeout_s=app.settings.query_timeout_s,
                )
            except SqlGuardError as exc:
                raise ToolError(str(exc)) from exc
            except QueryTimeoutError as exc:
                raise ToolError(str(exc)) from exc
        finally:
            con.close()
        return QueryListeningHistoryResult(
            columns=result.columns,
            rows=result.rows,
            row_count=result.row_count,
            truncated=result.truncated,
            sql_executed=result.sql_executed,
        )

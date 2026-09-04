"""System tools: get_me, server_status. Prove end-to-end MCP connectivity."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp_types import ToolAnnotations
from pydantic import BaseModel

from spotify_mcp.api.models import Me
from spotify_mcp.server import AppContext
from spotify_mcp.tools._util import guarded


class AnalyticsDbStatus(BaseModel):
    present: bool
    total_plays: int | None = None
    earliest_play: str | None = None
    latest_play: str | None = None


class ServerStatus(BaseModel):
    dry_run: bool
    authorized: bool
    scopes_granted: list[str]
    token_expires_in_s: float | None
    allow_removals: bool
    analytics_db: AnalyticsDbStatus
    http_cache_stats: dict[str, Any] | None


def _analytics_db_status(app: AppContext) -> AnalyticsDbStatus:
    if not app.settings.analytics_db_path.exists():
        return AnalyticsDbStatus(present=False)
    try:
        from spotify_mcp.analytics.db import connect_ro  # noqa: PLC0415

        con = connect_ro(app.settings.analytics_db_path)
        try:
            total, earliest, latest = con.execute(
                "SELECT COUNT(*), MIN(played_date), MAX(played_date) FROM plays"
            ).fetchone()
        finally:
            con.close()
        return AnalyticsDbStatus(
            present=True,
            total_plays=total,
            earliest_play=earliest.isoformat() if earliest else None,
            latest_play=latest.isoformat() if latest else None,
        )
    except Exception:  # noqa: BLE001 - status reporting must never crash the tool
        return AnalyticsDbStatus(present=True)


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        description=(
            "Return the current user's Spotify profile: id, display name, uri, images, "
            "follower count. Note: email, country, and product tier were removed from this "
            "endpoint by Spotify in February 2026 and are never present."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    @guarded
    async def get_me(ctx: Context) -> Me:
        app: AppContext = ctx.request_context.lifespan_context
        data = await app.spotify.get("/me")
        return Me.model_validate(data)

    @mcp.tool(
        description=(
            "Report this server's own state: dry-run mode, Spotify authorization, granted "
            "OAuth scopes, whether destructive playlist removals are enabled, the local "
            "listening-history database's size and date range if built, and HTTP cache stats."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def server_status(ctx: Context) -> ServerStatus:
        app: AppContext = ctx.request_context.lifespan_context
        return ServerStatus(
            dry_run=app.settings.dry_run,
            authorized=app.token_manager.is_authorized,
            scopes_granted=app.token_manager.granted_scopes,
            token_expires_in_s=app.token_manager.expires_in_s,
            allow_removals=app.settings.allow_removals,
            analytics_db=_analytics_db_status(app),
            http_cache_stats=app.spotify.cache_stats(),
        )

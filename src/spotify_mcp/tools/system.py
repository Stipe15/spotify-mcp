"""System tools: get_me, server_status. Prove end-to-end MCP connectivity."""

from __future__ import annotations

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp_types import ToolAnnotations
from pydantic import BaseModel

from spotify_mcp.api.models import Me
from spotify_mcp.server import AppContext
from spotify_mcp.tools._util import guarded


class ServerStatus(BaseModel):
    dry_run: bool
    authorized: bool
    scopes_granted: list[str]
    token_expires_in_s: float | None
    analytics_db_present: bool


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        description=(
            "Return the current user's Spotify profile: id, display name, uri, images. "
            "Note: email, country, product tier, and follower count were removed from this "
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
            "Report this server's own state: whether dry-run mode is on (writes are logged, "
            "not sent), whether Spotify authorization is complete, which OAuth scopes were "
            "granted, and whether the local listening-history database has been built yet."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def server_status(ctx: Context) -> ServerStatus:
        app: AppContext = ctx.request_context.lifespan_context
        analytics_db_present = app.settings.data_dir.joinpath("listening.duckdb").exists()
        return ServerStatus(
            dry_run=app.settings.dry_run,
            authorized=app.token_manager.is_authorized,
            scopes_granted=app.token_manager.granted_scopes,
            token_expires_in_s=app.token_manager.expires_in_s,
            analytics_db_present=analytics_db_present,
        )

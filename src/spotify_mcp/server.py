"""MCPServer construction: lifespan-managed app state and tool registration."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from mcp.server import MCPServer

from spotify_mcp.api.client import SpotifyClient
from spotify_mcp.auth.manager import TokenManager
from spotify_mcp.config import Settings
from spotify_mcp.logging import configure, get_logger
from spotify_mcp.tools.confirm import ConfirmationStore

logger = get_logger("server")


@dataclass
class AppContext:
    settings: Settings
    token_manager: TokenManager
    spotify: SpotifyClient
    confirmations: ConfirmationStore


def build_server(settings: Settings) -> MCPServer:
    configure(settings.log_level)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[AppContext]:
        token_manager = TokenManager(settings)
        spotify = SpotifyClient(settings, token_manager)
        confirmations = ConfirmationStore(ttl_s=settings.confirm_token_ttl_s)
        logger.info(
            "spotify-mcp starting: dry_run=%s authorized=%s allow_removals=%s",
            settings.dry_run,
            token_manager.is_authorized,
            settings.allow_removals,
        )
        try:
            yield AppContext(
                settings=settings,
                token_manager=token_manager,
                spotify=spotify,
                confirmations=confirmations,
            )
        finally:
            await spotify.aclose()
            await token_manager.aclose()
            logger.info("spotify-mcp shutting down")

    mcp = MCPServer(
        "spotify-mcp",
        title="Spotify (personal)",
        instructions=(
            "Tools for managing your own Spotify playlists and querying your local "
            "listening-history analytics. All write tools require a two-phase confirmation: "
            "call once to get a preview and confirm_token, then call again with that token to "
            "execute. Note: Spotify removed all popularity/recommendation/audio-feature "
            "endpoints in 2026 — no tool here can tell you what's 'trending' on Spotify's own "
            "authority."
        ),
        lifespan=lifespan,
    )

    # Deferred: tools/*.py import AppContext from this module, so importing
    # them at module scope here would be circular.
    from spotify_mcp.tools import read_live, system, write_playlists  # noqa: PLC0415

    system.register(mcp)
    read_live.register(mcp)
    write_playlists.register(mcp)

    return mcp

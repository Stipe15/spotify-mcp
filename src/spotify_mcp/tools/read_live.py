"""Live-read tools: current account state and catalog search.

Every 2026 constraint that would otherwise surprise the model on a failed
call is stated directly in these tool descriptions, per PLAN.md's guardrail
principle — search's limit=10 cap, the playlist-items ownership restriction,
and the absence of any popularity/trending signal.
"""

from __future__ import annotations

from typing import Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from spotify_mcp.api.models import (
    Artist,
    CursorPaging,
    FollowedArtistsPage,
    Owner,
    Paging,
    PlaybackState,
    PlaylistItemsPage,
    PlaylistSummary,
    RecentlyPlayedEntry,
    SavedAlbumEntry,
    SavedTrackEntry,
    SearchResults,
    SimplifiedAlbum,
    SimplifiedPlaylist,
    SimplifiedTrack,
    Track,
)
from spotify_mcp.server import AppContext
from spotify_mcp.tools._util import guarded

_RO = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_RO_OPEN = ToolAnnotations(read_only_hint=True, open_world_hint=True)

_SEARCH_TYPES = {"track", "artist", "album", "playlist"}


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        description=(
            "Current Spotify playback state: what's playing, on which device, progress, "
            "shuffle/repeat. Returns active=false (nothing else populated) when no device is "
            "active — that is normal, not an error."
        ),
        annotations=_RO,
    )
    @guarded
    async def get_playback_state(ctx: Context) -> PlaybackState:
        app: AppContext = ctx.request_context.lifespan_context
        data = await app.spotify.get("/me/player")
        if not data:
            return PlaybackState(active=False)
        return PlaybackState.model_validate({**data, "active": True})

    @mcp.tool(
        description=(
            "The user's most-played artists over a time window (short_term=~4 weeks, "
            "medium_term=~6 months, long_term=~1 year). This is Spotify's own affinity "
            "ranking, computed from listening history — not a live popularity/trending signal "
            "(Spotify removed those in 2026)."
        ),
        annotations=_RO,
    )
    @guarded
    async def get_top_artists(
        ctx: Context,
        time_range: Literal["short_term", "medium_term", "long_term"] = "medium_term",
        limit: int = 20,
        offset: int = 0,
    ) -> Paging[Artist]:
        app: AppContext = ctx.request_context.lifespan_context
        data = await app.spotify.get(
            "/me/top/artists",
            params={"time_range": time_range, "limit": min(limit, 50), "offset": offset},
        )
        return Paging[Artist].model_validate(data)

    @mcp.tool(
        description=(
            "The user's most-played tracks over a time window (short_term=~4 weeks, "
            "medium_term=~6 months, long_term=~1 year). Spotify's own affinity ranking from "
            "listening history — not a live popularity/trending signal."
        ),
        annotations=_RO,
    )
    @guarded
    async def get_top_tracks(
        ctx: Context,
        time_range: Literal["short_term", "medium_term", "long_term"] = "medium_term",
        limit: int = 20,
        offset: int = 0,
    ) -> Paging[Track]:
        app: AppContext = ctx.request_context.lifespan_context
        data = await app.spotify.get(
            "/me/top/tracks",
            params={"time_range": time_range, "limit": min(limit, 50), "offset": offset},
        )
        return Paging[Track].model_validate(data)

    @mcp.tool(
        description=(
            "The user's last-played tracks, most recent first. Spotify caps this at the last "
            "50 plays — it is NOT a full listening history (use the local analytics tools for "
            "that, once built). `after_ms`/`before_ms` are Unix ms timestamps and mutually "
            "exclusive."
        ),
        annotations=_RO,
    )
    @guarded
    async def get_recently_played(
        ctx: Context,
        limit: int = 20,
        after_ms: int | None = None,
        before_ms: int | None = None,
    ) -> CursorPaging[RecentlyPlayedEntry]:
        if after_ms is not None and before_ms is not None:
            raise ToolError("after_ms and before_ms are mutually exclusive.")
        app: AppContext = ctx.request_context.lifespan_context
        params: dict[str, int] = {"limit": min(limit, 50)}
        if after_ms is not None:
            params["after"] = after_ms
        if before_ms is not None:
            params["before"] = before_ms
        data = await app.spotify.get("/me/player/recently-played", params=params)
        return CursorPaging[RecentlyPlayedEntry].model_validate(data)

    @mcp.tool(
        description="Tracks saved to the user's library ('Liked Songs'), most recent first.",
        annotations=_RO,
    )
    @guarded
    async def get_saved_tracks(
        ctx: Context, limit: int = 20, offset: int = 0
    ) -> Paging[SavedTrackEntry]:
        app: AppContext = ctx.request_context.lifespan_context
        data = await app.spotify.get(
            "/me/tracks", params={"limit": min(limit, 50), "offset": offset}
        )
        return Paging[SavedTrackEntry].model_validate(data)

    @mcp.tool(
        description="Albums saved to the user's library, most recently added first.",
        annotations=_RO,
    )
    @guarded
    async def get_saved_albums(
        ctx: Context, limit: int = 20, offset: int = 0
    ) -> Paging[SavedAlbumEntry]:
        app: AppContext = ctx.request_context.lifespan_context
        data = await app.spotify.get(
            "/me/albums", params={"limit": min(limit, 50), "offset": offset}
        )
        return Paging[SavedAlbumEntry].model_validate(data)

    @mcp.tool(
        description=(
            "Artists the user follows. Cursor-paginated: pass the last returned artist id "
            "as `after`."
        ),
        annotations=_RO,
    )
    @guarded
    async def get_followed_artists(
        ctx: Context, limit: int = 20, after: str | None = None
    ) -> FollowedArtistsPage:
        app: AppContext = ctx.request_context.lifespan_context
        params: dict[str, str | int] = {"type": "artist", "limit": min(limit, 50)}
        if after is not None:
            params["after"] = after
        data = await app.spotify.get("/me/following", params=params)
        return FollowedArtistsPage.model_validate(data)

    @mcp.tool(
        description="Playlists the user owns or follows.",
        annotations=_RO,
    )
    @guarded
    async def get_my_playlists(
        ctx: Context, limit: int = 20, offset: int = 0
    ) -> Paging[SimplifiedPlaylist]:
        app: AppContext = ctx.request_context.lifespan_context
        data = await app.spotify.get(
            "/me/playlists", params={"limit": min(limit, 50), "offset": offset}
        )
        return Paging[SimplifiedPlaylist].model_validate(data)

    @mcp.tool(
        description=(
            "Metadata for one playlist (name, description, owner, item count) — not its "
            "contents. Use get_playlist_items for the tracks. Works for any playlist id, but "
            "the item count is only meaningful for playlists you own or collaborate on."
        ),
        annotations=_RO,
    )
    @guarded
    async def get_playlist(ctx: Context, playlist_id: str) -> PlaylistSummary:
        app: AppContext = ctx.request_context.lifespan_context
        data = await app.spotify.get(
            f"/playlists/{playlist_id}",
            params={
                "fields": (
                    "id,name,uri,description,public,collaborative,snapshot_id,"
                    "images,external_urls,owner,items.total"
                )
            },
        )
        item_count = (data.get("items") or {}).get("total")
        return PlaylistSummary(
            id=data["id"],
            name=data["name"],
            uri=data["uri"],
            description=data.get("description"),
            public=data.get("public"),
            collaborative=data.get("collaborative", False),
            owner=Owner.model_validate(data.get("owner") or {}),
            snapshot_id=data.get("snapshot_id"),
            item_count=item_count,
            images=data.get("images") or [],
            external_urls=data.get("external_urls") or {},
        )

    @mcp.tool(
        description=(
            "Tracks (and episodes) in a playlist, paginated. IMPORTANT: only works for "
            "playlists the user owns or collaborates on — Spotify returns 403 for any other "
            "playlist, including every Spotify-editorial playlist. There is no way around this "
            "restriction. Each item's `item` field holds the track/episode object."
        ),
        annotations=_RO,
    )
    @guarded
    async def get_playlist_items(
        ctx: Context, playlist_id: str, limit: int = 20, offset: int = 0
    ) -> PlaylistItemsPage:
        app: AppContext = ctx.request_context.lifespan_context
        data = await app.spotify.get(
            f"/playlists/{playlist_id}/items", params={"limit": min(limit, 50), "offset": offset}
        )
        return PlaylistItemsPage.model_validate(data)

    @mcp.tool(
        description=(
            "Search the Spotify catalog. `limit` is capped at 10 (Spotify reduced this from 50 "
            "in Feb 2026) and `offset` at 1000 — for more than 10 matches, page with offset "
            "rather than expecting a bigger single batch. Prefer field-filtered terms "
            "(track/artist/album/year/isrc) over free_text: bare title text reliably matches "
            "sped-up edits, live versions, karaoke covers, and tribute recordings instead of "
            "the real thing. There is no popularity or relevance score in the response — "
            "nothing here indicates what is currently popular or trending."
        ),
        annotations=_RO_OPEN,
    )
    @guarded
    async def search_catalog(
        ctx: Context,
        track: str | None = None,
        artist: str | None = None,
        album: str | None = None,
        year: str | None = None,
        isrc: str | None = None,
        free_text: str | None = None,
        types: list[str] | None = None,
        limit: int = 5,
        offset: int = 0,
    ) -> SearchResults:
        query_terms: list[str] = []
        if track:
            query_terms.append(f'track:"{track}"')
        if artist:
            query_terms.append(f'artist:"{artist}"')
        if album:
            query_terms.append(f'album:"{album}"')
        if year:
            query_terms.append(f"year:{year}")
        if isrc:
            query_terms.append(f"isrc:{isrc}")
        if free_text:
            query_terms.append(free_text)
        if not query_terms:
            raise ToolError(
                "search_catalog needs at least one of track/artist/album/year/isrc/free_text."
            )

        search_types = types or ["track"]
        invalid = set(search_types) - _SEARCH_TYPES
        if invalid:
            raise ToolError(
                f"Unsupported search type(s): {sorted(invalid)}. Use: {sorted(_SEARCH_TYPES)}."
            )

        app: AppContext = ctx.request_context.lifespan_context
        data = await app.spotify.get(
            "/search",
            params={
                "q": " ".join(query_terms),
                "type": ",".join(search_types),
                "limit": min(limit, 10),
                "offset": min(offset, 1000),
            },
        )
        return SearchResults.model_validate(data)

    @mcp.tool(description="Full metadata for one track by its Spotify id.", annotations=_RO)
    @guarded
    async def get_track(ctx: Context, track_id: str) -> Track:
        app: AppContext = ctx.request_context.lifespan_context
        data = await app.spotify.get(f"/tracks/{track_id}")
        return Track.model_validate(data)

    @mcp.tool(description="Full metadata for one artist by its Spotify id.", annotations=_RO)
    @guarded
    async def get_artist(ctx: Context, artist_id: str) -> Artist:
        app: AppContext = ctx.request_context.lifespan_context
        data = await app.spotify.get(f"/artists/{artist_id}")
        return Artist.model_validate(data)

    @mcp.tool(
        description=(
            "An artist's albums. `include_groups` filters by relationship — a comma-separated "
            "subset of album,single,appears_on,compilation; omit for all."
        ),
        annotations=_RO,
    )
    @guarded
    async def get_artist_albums(
        ctx: Context,
        artist_id: str,
        include_groups: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Paging[SimplifiedAlbum]:
        app: AppContext = ctx.request_context.lifespan_context
        params: dict[str, str | int] = {"limit": min(limit, 50), "offset": offset}
        if include_groups:
            params["include_groups"] = include_groups
        data = await app.spotify.get(f"/artists/{artist_id}/albums", params=params)
        return Paging[SimplifiedAlbum].model_validate(data)

    @mcp.tool(description="An album's tracklist.", annotations=_RO)
    @guarded
    async def get_album_tracks(
        ctx: Context, album_id: str, limit: int = 20, offset: int = 0
    ) -> Paging[SimplifiedTrack]:
        app: AppContext = ctx.request_context.lifespan_context
        data = await app.spotify.get(
            f"/albums/{album_id}/tracks", params={"limit": min(limit, 50), "offset": offset}
        )
        return Paging[SimplifiedTrack].model_validate(data)

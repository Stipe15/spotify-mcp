"""Playlist write tools. Every one is two-phase (PLAN.md §6): call without
`confirm_token` for a preview, call again with it — and identical arguments —
to execute. `remove_playlist_items` is additionally disabled unless
`SPOTIFY_MCP_ALLOW_REMOVALS=true`; nothing else deletes or unfollows
anything, by omission — those endpoints simply aren't called here.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from spotify_mcp.server import AppContext
from spotify_mcp.tools import confirm
from spotify_mcp.tools._util import guarded

_COVER_IMAGE_MAX_BYTES = 256 * 1024
_ITEMS_PER_REQUEST = 100

_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
_WRITE_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
_WRITE_DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, open_world_hint=False
)


def _validate_uris(uris: list[str]) -> None:
    bad = [
        u for u in uris if not (u.startswith("spotify:track:") or u.startswith("spotify:episode:"))
    ]
    if bad:
        raise ToolError(
            f"Not valid Spotify track/episode URIs: {bad[:5]}"
            + (f" (+{len(bad) - 5} more)" if len(bad) > 5 else "")
            + '. Expected the form "spotify:track:<id>" or "spotify:episode:<id>".'
        )


def _chunks(items: list[str], size: int = _ITEMS_PER_REQUEST) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def _fetch_playlist_summary(app: AppContext, playlist_id: str) -> dict[str, Any]:
    data = await app.spotify.get(
        f"/playlists/{playlist_id}",
        params={"fields": "id,name,snapshot_id,items.total"},
    )
    return {
        "id": data.get("id", playlist_id),
        "name": data.get("name"),
        "snapshot_id": data.get("snapshot_id"),
        "item_count": (data.get("items") or {}).get("total"),
    }


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        description=(
            "Create a new, empty Spotify playlist. Requires confirmation (call once for a "
            "preview, again with confirm_token to execute — see the tool's response for "
            "exactly how). collaborative and public cannot both be true."
        ),
        annotations=_WRITE,
    )
    @guarded
    async def create_playlist(
        ctx: Context,
        name: str,
        description: str = "",
        public: bool = False,
        collaborative: bool = False,
        confirm_token: str | None = None,
    ) -> confirm.WriteToolResult:
        if collaborative and public:
            raise ToolError("A playlist cannot be both collaborative and public.")
        app: AppContext = ctx.request_context.lifespan_context
        args = {
            "name": name,
            "description": description,
            "public": public,
            "collaborative": collaborative,
        }
        pending = await confirm.gate(
            ctx,
            app.confirmations,
            action="create_playlist",
            args=args,
            confirm_token=confirm_token,
            preview={
                "will_create": args,
                "irreversible": False,
            },
            elicit_message=(
                f"Create a new {'public' if public else 'private'} playlist named {name!r}?"
            ),
        )
        if pending is not None:
            return pending

        data = await app.spotify.post("/me/playlists", json=args)
        confirm.write_audit(
            app.settings,
            action="create_playlist",
            args=args,
            token=confirm_token,
            outcome="dry_run" if data.get("dry_run") else "executed",
        )
        if data.get("dry_run"):
            return confirm.executed("create_playlist", data)
        return confirm.executed(
            "create_playlist",
            {
                "id": data.get("id"),
                "uri": data.get("uri"),
                "name": data.get("name"),
                "url": (data.get("external_urls") or {}).get("spotify"),
            },
        )

    @mcp.tool(
        description=(
            "Add tracks/episodes to a playlist by URI. Spotify accepts at most 100 URIs per "
            "request; this tool chunks automatically, so 250 tracks = 3 API calls under the "
            "hood (all covered by one confirmation). Append-only and does NOT deduplicate — "
            "check get_playlist_items first if duplicates matter. `position` (0-based) only "
            "guarantees exact placement for the first 100 URIs; any beyond that append at the "
            "end regardless of `position`. The preview shows the URIs verbatim: resolve them to "
            "human-readable names yourself first (e.g. via search_catalog) so you can show the "
            "user something readable."
        ),
        annotations=_WRITE,
    )
    @guarded
    async def add_playlist_items(
        ctx: Context,
        playlist_id: str,
        uris: list[str],
        position: int | None = None,
        confirm_token: str | None = None,
    ) -> confirm.WriteToolResult:
        if not uris:
            raise ToolError("uris is empty — nothing to add.")
        _validate_uris(uris)
        app: AppContext = ctx.request_context.lifespan_context
        args: dict[str, Any] = {"playlist_id": playlist_id, "uris": uris, "position": position}

        pending = await confirm.gate(
            ctx,
            app.confirmations,
            action="add_playlist_items",
            args=args,
            confirm_token=confirm_token,
            preview=await _add_preview(app, playlist_id, uris, position),
            elicit_message=f"Add {len(uris)} item(s) to this playlist?",
        )
        if pending is not None:
            return pending

        chunks = _chunks(uris)
        snapshot_ids: list[str] = []
        for i, chunk in enumerate(chunks):
            body: dict[str, Any] = {"uris": chunk}
            if i == 0 and position is not None:
                body["position"] = position
            result = await app.spotify.post(f"/playlists/{playlist_id}/items", json=body)
            if result.get("dry_run"):
                confirm.write_audit(
                    app.settings,
                    action="add_playlist_items",
                    args=args,
                    token=confirm_token,
                    outcome="dry_run",
                )
                return confirm.executed("add_playlist_items", result)
            if result.get("snapshot_id"):
                snapshot_ids.append(result["snapshot_id"])

        confirm.write_audit(
            app.settings,
            action="add_playlist_items",
            args=args,
            token=confirm_token,
            outcome="executed",
            snapshot_id=snapshot_ids[-1] if snapshot_ids else None,
        )
        return confirm.executed(
            "add_playlist_items",
            {"added": len(uris), "chunks": len(chunks), "snapshot_ids": snapshot_ids},
        )

    @mcp.tool(
        description=(
            "Replace a playlist's entire contents with a new list of URIs — the existing "
            "items are discarded. The preview shows what will be lost alongside what replaces "
            "it. Pass an empty list to clear the playlist entirely. For more than 100 URIs, "
            "the first 100 replace in one call and the rest are appended after."
        ),
        annotations=_WRITE_IDEMPOTENT,
    )
    @guarded
    async def replace_playlist_items(
        ctx: Context,
        playlist_id: str,
        uris: list[str],
        confirm_token: str | None = None,
    ) -> confirm.WriteToolResult:
        if uris:
            _validate_uris(uris)
        app: AppContext = ctx.request_context.lifespan_context
        args = {"playlist_id": playlist_id, "uris": uris}

        pending = await confirm.gate(
            ctx,
            app.confirmations,
            action="replace_playlist_items",
            args=args,
            confirm_token=confirm_token,
            preview=await _replace_preview(app, playlist_id, uris),
            elicit_message=(
                f"Replace this playlist's entire contents with {len(uris)} item(s)? "
                "The current contents will be lost."
            ),
        )
        if pending is not None:
            return pending

        chunks = _chunks(uris) or [[]]
        first = await app.spotify.put(f"/playlists/{playlist_id}/items", json={"uris": chunks[0]})
        if first.get("dry_run"):
            confirm.write_audit(
                app.settings,
                action="replace_playlist_items",
                args=args,
                token=confirm_token,
                outcome="dry_run",
            )
            return confirm.executed("replace_playlist_items", first)

        snapshot_ids = [first["snapshot_id"]] if first.get("snapshot_id") else []
        for chunk in chunks[1:]:
            result = await app.spotify.post(f"/playlists/{playlist_id}/items", json={"uris": chunk})
            if result.get("snapshot_id"):
                snapshot_ids.append(result["snapshot_id"])

        confirm.write_audit(
            app.settings,
            action="replace_playlist_items",
            args=args,
            token=confirm_token,
            outcome="executed",
            snapshot_id=snapshot_ids[-1] if snapshot_ids else None,
        )
        return confirm.executed(
            "replace_playlist_items", {"new_size": len(uris), "snapshot_ids": snapshot_ids}
        )

    @mcp.tool(
        description=(
            "Move a contiguous slice of a playlist's items to a new position. `range_start` "
            "is the 0-based index of the first item to move, `range_length` how many items "
            "(default 1), `insert_before` the 0-based index to insert them at. Pins to the "
            "playlist's current snapshot if you don't supply one — if the playlist changed "
            "since you last looked, get_playlist_items first."
        ),
        annotations=_WRITE_IDEMPOTENT,
    )
    @guarded
    async def reorder_playlist_items(
        ctx: Context,
        playlist_id: str,
        range_start: int,
        insert_before: int,
        range_length: int = 1,
        snapshot_id: str | None = None,
        confirm_token: str | None = None,
    ) -> confirm.WriteToolResult:
        app: AppContext = ctx.request_context.lifespan_context
        args = {
            "playlist_id": playlist_id,
            "range_start": range_start,
            "insert_before": insert_before,
            "range_length": range_length,
            "snapshot_id": snapshot_id,
        }

        pending = await confirm.gate(
            ctx,
            app.confirmations,
            action="reorder_playlist_items",
            args=args,
            confirm_token=confirm_token,
            preview=await _reorder_preview(
                app, playlist_id, range_start, range_length, insert_before
            ),
            elicit_message=(
                f"Move {range_length} item(s) starting at position {range_start} to "
                f"position {insert_before}?"
            ),
        )
        if pending is not None:
            return pending

        resolved_snapshot = (
            snapshot_id or (await _fetch_playlist_summary(app, playlist_id))["snapshot_id"]
        )
        result = await app.spotify.put(
            f"/playlists/{playlist_id}/items",
            json={
                "range_start": range_start,
                "insert_before": insert_before,
                "range_length": range_length,
                "snapshot_id": resolved_snapshot,
            },
        )
        confirm.write_audit(
            app.settings,
            action="reorder_playlist_items",
            args=args,
            token=confirm_token,
            outcome="dry_run" if result.get("dry_run") else "executed",
            snapshot_id=result.get("snapshot_id"),
        )
        return confirm.executed("reorder_playlist_items", result)

    @mcp.tool(
        description=(
            "Update a playlist's name, description, and/or public/private visibility. Only "
            "the fields you pass are changed. The preview shows an old-to-new diff of just "
            "those fields."
        ),
        annotations=_WRITE_IDEMPOTENT,
    )
    @guarded
    async def update_playlist_details(
        ctx: Context,
        playlist_id: str,
        name: str | None = None,
        description: str | None = None,
        public: bool | None = None,
        confirm_token: str | None = None,
    ) -> confirm.WriteToolResult:
        if name is None and description is None and public is None:
            raise ToolError("Nothing to update — pass at least one of name/description/public.")
        app: AppContext = ctx.request_context.lifespan_context
        args = {
            "playlist_id": playlist_id,
            "name": name,
            "description": description,
            "public": public,
        }

        current = await app.spotify.get(
            f"/playlists/{playlist_id}", params={"fields": "name,description,public"}
        )
        changes: dict[str, Any] = {}
        if name is not None and name != current.get("name"):
            changes["name"] = {"from": current.get("name"), "to": name}
        if description is not None and description != current.get("description"):
            changes["description"] = {"from": current.get("description"), "to": description}
        if public is not None and public != current.get("public"):
            changes["public"] = {"from": current.get("public"), "to": public}

        if not changes:
            return confirm.executed(
                "update_playlist_details", {"changed": False, "reason": "already matches"}
            )

        pending = await confirm.gate(
            ctx,
            app.confirmations,
            action="update_playlist_details",
            args=args,
            confirm_token=confirm_token,
            preview={"playlist_id": playlist_id, "changes": changes},
            elicit_message=f"Update this playlist's details? Changing: {list(changes)}.",
        )
        if pending is not None:
            return pending

        body = {
            k: v
            for k, v in {"name": name, "description": description, "public": public}.items()
            if v is not None
        }
        result = await app.spotify.put(f"/playlists/{playlist_id}", json=body)
        confirm.write_audit(
            app.settings,
            action="update_playlist_details",
            args=args,
            token=confirm_token,
            outcome="dry_run" if result.get("dry_run") else "executed",
        )
        return confirm.executed(
            "update_playlist_details", {"changed_fields": list(changes), **result}
        )

    @mcp.tool(
        description=(
            "Set a playlist's cover image from a local JPEG file. Spotify caps the encoded "
            "payload at 256 KB — this checks that before offering confirmation, so an "
            "oversized image fails fast with a clear message instead of a wasted round trip."
        ),
        annotations=_WRITE,
    )
    @guarded
    async def set_playlist_cover(
        ctx: Context,
        playlist_id: str,
        image_path: str,
        confirm_token: str | None = None,
    ) -> confirm.WriteToolResult:
        path = Path(image_path)
        if not path.is_file():
            raise ToolError(f"No such file: {image_path}")
        raw = path.read_bytes()
        encoded = base64.b64encode(raw)
        if len(encoded) > _COVER_IMAGE_MAX_BYTES:
            raise ToolError(
                f"Encoded image is {len(encoded)} bytes; Spotify's cap is "
                f"{_COVER_IMAGE_MAX_BYTES} bytes (256 KB). Resize {image_path} and try again."
            )

        app: AppContext = ctx.request_context.lifespan_context
        args = {"playlist_id": playlist_id, "image_path": image_path}

        pending = await confirm.gate(
            ctx,
            app.confirmations,
            action="set_playlist_cover",
            args=args,
            confirm_token=confirm_token,
            preview={
                "playlist_id": playlist_id,
                "image_path": image_path,
                "encoded_size_bytes": len(encoded),
                "cap_bytes": _COVER_IMAGE_MAX_BYTES,
            },
            elicit_message=f"Set this playlist's cover image from {image_path}?",
        )
        if pending is not None:
            return pending

        result = await app.spotify.put_raw(
            f"/playlists/{playlist_id}/images",
            content=encoded,
            content_type="image/jpeg",
        )
        confirm.write_audit(
            app.settings,
            action="set_playlist_cover",
            args=args,
            token=confirm_token,
            outcome="dry_run" if result.get("dry_run") else "executed",
        )
        return confirm.executed("set_playlist_cover", result or {"status": "updated"})

    @mcp.tool(
        description=(
            "DESTRUCTIVE. Remove specific items from a playlist by URI. Disabled by default — "
            "set SPOTIFY_MCP_ALLOW_REMOVALS=true to enable. Requires a `snapshot_id` you "
            "fetched yourself (e.g. via get_playlist) immediately beforehand, so a removal "
            "only ever applies to the playlist state you actually looked at. Never call this "
            "from a broad instruction like 'clean up my library' — only on an explicit, "
            "specific removal request the user approved for these exact items."
        ),
        annotations=_WRITE_DESTRUCTIVE,
    )
    @guarded
    async def remove_playlist_items(
        ctx: Context,
        playlist_id: str,
        uris: list[str],
        snapshot_id: str,
        confirm_token: str | None = None,
    ) -> confirm.WriteToolResult:
        app: AppContext = ctx.request_context.lifespan_context
        if not app.settings.allow_removals:
            raise ToolError(
                "Removing playlist items is disabled. Set SPOTIFY_MCP_ALLOW_REMOVALS=true in "
                ".env to enable this tool — it is off by default because it is destructive."
            )
        if not uris:
            raise ToolError("uris is empty — nothing to remove.")
        _validate_uris(uris)
        args = {"playlist_id": playlist_id, "uris": uris, "snapshot_id": snapshot_id}

        pending = await confirm.gate(
            ctx,
            app.confirmations,
            action="remove_playlist_items",
            args=args,
            confirm_token=confirm_token,
            preview=await _remove_preview(app, playlist_id, uris),
            elicit_message=(
                f"Permanently remove {len(uris)} item(s) from this playlist? This cannot be undone."
            ),
        )
        if pending is not None:
            return pending

        result = await app.spotify.delete(
            f"/playlists/{playlist_id}/items",
            json={"items": [{"uri": u} for u in uris], "snapshot_id": snapshot_id},
        )
        confirm.write_audit(
            app.settings,
            action="remove_playlist_items",
            args=args,
            token=confirm_token,
            outcome="dry_run" if result.get("dry_run") else "executed",
            snapshot_id=result.get("snapshot_id"),
        )
        return confirm.executed("remove_playlist_items", {"removed": len(uris), **result})


async def _add_preview(
    app: AppContext, playlist_id: str, uris: list[str], position: int | None
) -> dict[str, Any]:
    playlist = await _fetch_playlist_summary(app, playlist_id)
    current = playlist.get("item_count") or 0
    return {
        "playlist": playlist,
        "will_add": uris,
        "position": position,
        "counts": {
            "adding": len(uris),
            "resulting_size": current + len(uris),
            "api_calls": len(_chunks(uris)),
        },
        "irreversible": False,
    }


async def _replace_preview(app: AppContext, playlist_id: str, uris: list[str]) -> dict[str, Any]:
    playlist = await _fetch_playlist_summary(app, playlist_id)
    current = playlist.get("item_count") or 0
    existing = await app.spotify.get(
        f"/playlists/{playlist_id}/items", params={"limit": 50, "offset": 0}
    )
    current_uris = [
        (item.get("item") or {}).get("uri")
        for item in existing.get("items", [])
        if item.get("item")
    ]
    return {
        "playlist": playlist,
        "current_items_being_replaced": current_uris,
        "current_items_truncated": current > len(current_uris),
        "new_items": uris,
        "counts": {"removing": current, "adding": len(uris)},
        "irreversible": True,
    }


async def _reorder_preview(
    app: AppContext, playlist_id: str, range_start: int, range_length: int, insert_before: int
) -> dict[str, Any]:
    playlist = await _fetch_playlist_summary(app, playlist_id)
    moving = await app.spotify.get(
        f"/playlists/{playlist_id}/items", params={"limit": range_length, "offset": range_start}
    )
    moving_uris = [
        (item.get("item") or {}).get("uri") for item in moving.get("items", []) if item.get("item")
    ]
    return {
        "playlist": playlist,
        "moving": moving_uris,
        "from_position": range_start,
        "to_position": insert_before,
        "irreversible": False,
    }


async def _remove_preview(app: AppContext, playlist_id: str, uris: list[str]) -> dict[str, Any]:
    playlist = await _fetch_playlist_summary(app, playlist_id)
    current = playlist.get("item_count") or 0
    return {
        "playlist": playlist,
        "will_remove": uris,
        "counts": {"removing": len(uris), "resulting_size": max(current - len(uris), 0)},
        "irreversible": True,
    }

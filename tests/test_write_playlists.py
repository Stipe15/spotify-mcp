"""Exercises the actual registered write-tool functions via MCPServer.call_tool(),
with a mocked HTTP transport. Covers the properties that matter most for a
write path: nothing executes without confirm_token, a mismatched confirm
call is rejected, add_playlist_items chunks at 100, remove_playlist_items is
gated behind allow_removals, and dry-run mode never sends the real write.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from spotify_mcp.config import Settings
from spotify_mcp.tools import write_playlists

from .conftest import make_server_and_ctx as _make_server_and_ctx


def make_server_and_ctx(settings: Settings, handler):
    return _make_server_and_ctx(settings, handler, write_playlists.register)


def _playlist_summary_response(item_count: int = 3) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "p1",
            "name": "My Playlist",
            "snapshot_id": "snap1",
            "items": {"total": item_count},
        },
    )


@pytest.mark.asyncio
async def test_create_playlist_requires_confirmation_then_executes(settings: Settings):
    calls = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append((request.method, str(request.url.path)))
        return httpx2.Response(
            201,
            json={
                "id": "new123",
                "uri": "spotify:playlist:new123",
                "name": "Road Trip",
                "external_urls": {"spotify": "https://open.spotify.com/playlist/new123"},
            },
        )

    mcp, ctx = make_server_and_ctx(settings, handler)

    preview_result = await mcp.call_tool("create_playlist", {"name": "Road Trip"}, ctx)
    preview = json.loads(preview_result.content[0].text)
    assert preview["status"] == "confirmation_required"
    assert preview["preview"]["will_create"]["name"] == "Road Trip"
    assert calls == []  # nothing sent to Spotify yet

    token = preview["confirm_token"]
    exec_result = await mcp.call_tool(
        "create_playlist", {"name": "Road Trip", "confirm_token": token}, ctx
    )
    body = json.loads(exec_result.content[0].text)
    assert body["status"] == "executed"
    assert body["result"]["id"] == "new123"
    assert calls == [("POST", "/v1/me/playlists")]


@pytest.mark.asyncio
async def test_create_playlist_rejects_collaborative_and_public(settings: Settings):
    mcp, ctx = make_server_and_ctx(settings, lambda r: httpx2.Response(200, json={}))
    with pytest.raises(ToolError, match="cannot be both"):
        await mcp.call_tool(
            "create_playlist", {"name": "x", "collaborative": True, "public": True}, ctx
        )


@pytest.mark.asyncio
async def test_confirm_token_rejects_mutated_arguments(settings: Settings):
    mcp, ctx = make_server_and_ctx(settings, lambda r: httpx2.Response(201, json={"id": "x"}))

    preview_result = await mcp.call_tool("create_playlist", {"name": "Road Trip"}, ctx)
    token = json.loads(preview_result.content[0].text)["confirm_token"]

    with pytest.raises(ToolError, match="don't match"):
        await mcp.call_tool(
            "create_playlist", {"name": "Different Name", "confirm_token": token}, ctx
        )


@pytest.mark.asyncio
async def test_confirm_token_is_single_use(settings: Settings):
    mcp, ctx = make_server_and_ctx(settings, lambda r: httpx2.Response(201, json={"id": "x"}))

    preview_result = await mcp.call_tool("create_playlist", {"name": "Road Trip"}, ctx)
    token = json.loads(preview_result.content[0].text)["confirm_token"]

    await mcp.call_tool("create_playlist", {"name": "Road Trip", "confirm_token": token}, ctx)
    with pytest.raises(ToolError, match="invalid, expired, or already used"):
        await mcp.call_tool("create_playlist", {"name": "Road Trip", "confirm_token": token}, ctx)


@pytest.mark.asyncio
async def test_add_playlist_items_preview_shows_counts_without_calling_add(settings: Settings):
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "GET"  # only the summary lookup, never a write
        return _playlist_summary_response(item_count=3)

    mcp, ctx = make_server_and_ctx(settings, handler)
    uris = [f"spotify:track:t{i}" for i in range(5)]
    result = await mcp.call_tool("add_playlist_items", {"playlist_id": "p1", "uris": uris}, ctx)
    body = json.loads(result.content[0].text)

    assert body["status"] == "confirmation_required"
    assert body["preview"]["counts"] == {"adding": 5, "resulting_size": 8, "api_calls": 1}


@pytest.mark.asyncio
async def test_add_playlist_items_rejects_malformed_uris(settings: Settings):
    mcp, ctx = make_server_and_ctx(settings, lambda r: httpx2.Response(200, json={}))
    with pytest.raises(ToolError, match="Not valid Spotify"):
        await mcp.call_tool("add_playlist_items", {"playlist_id": "p1", "uris": ["not-a-uri"]}, ctx)


@pytest.mark.asyncio
async def test_add_playlist_items_chunks_at_100_in_a_single_confirmation(settings: Settings):
    post_bodies = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            return _playlist_summary_response(item_count=0)
        payload = json.loads(request.content)
        post_bodies.append(payload["uris"])
        return httpx2.Response(200, json={"snapshot_id": f"snap{len(post_bodies)}"})

    mcp, ctx = make_server_and_ctx(settings, handler)
    uris = [f"spotify:track:t{i}" for i in range(150)]

    preview_result = await mcp.call_tool(
        "add_playlist_items", {"playlist_id": "p1", "uris": uris}, ctx
    )
    preview = json.loads(preview_result.content[0].text)
    assert preview["preview"]["counts"]["api_calls"] == 2
    token = preview["confirm_token"]

    exec_result = await mcp.call_tool(
        "add_playlist_items", {"playlist_id": "p1", "uris": uris, "confirm_token": token}, ctx
    )
    body = json.loads(exec_result.content[0].text)
    assert body["result"]["chunks"] == 2
    assert len(post_bodies) == 2
    assert len(post_bodies[0]) == 100
    assert len(post_bodies[1]) == 50


@pytest.mark.asyncio
async def test_update_playlist_details_short_circuits_when_nothing_changes(settings: Settings):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"name": "Same Name", "description": "", "public": False})

    mcp, ctx = make_server_and_ctx(settings, handler)
    result = await mcp.call_tool(
        "update_playlist_details", {"playlist_id": "p1", "name": "Same Name"}, ctx
    )
    body = json.loads(result.content[0].text)
    assert body["status"] == "executed"
    assert body["result"]["changed"] is False


@pytest.mark.asyncio
async def test_remove_playlist_items_disabled_by_default(tmp_path: Path):
    settings_no_removals = Settings(
        client_id="x",
        allow_removals=False,
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
    )
    mcp, ctx = make_server_and_ctx(settings_no_removals, lambda r: httpx2.Response(200, json={}))
    with pytest.raises(ToolError, match="disabled"):
        await mcp.call_tool(
            "remove_playlist_items",
            {"playlist_id": "p1", "uris": ["spotify:track:t1"], "snapshot_id": "snap1"},
            ctx,
        )


@pytest.mark.asyncio
async def test_remove_playlist_items_executes_when_enabled_and_confirmed(settings: Settings):
    delete_body = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            return _playlist_summary_response(item_count=5)
        delete_body.update(json.loads(request.content))
        return httpx2.Response(200, json={"snapshot_id": "snap2"})

    mcp, ctx = make_server_and_ctx(settings, handler)  # settings fixture has allow_removals=True

    preview_result = await mcp.call_tool(
        "remove_playlist_items",
        {"playlist_id": "p1", "uris": ["spotify:track:t1"], "snapshot_id": "snap1"},
        ctx,
    )
    token = json.loads(preview_result.content[0].text)["confirm_token"]

    exec_result = await mcp.call_tool(
        "remove_playlist_items",
        {
            "playlist_id": "p1",
            "uris": ["spotify:track:t1"],
            "snapshot_id": "snap1",
            "confirm_token": token,
        },
        ctx,
    )
    body = json.loads(exec_result.content[0].text)
    assert body["status"] == "executed"
    assert delete_body == {"items": [{"uri": "spotify:track:t1"}], "snapshot_id": "snap1"}


@pytest.mark.asyncio
async def test_dry_run_never_sends_the_write(settings: Settings):
    writes_seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            return _playlist_summary_response(item_count=0)
        writes_seen.append(request.method)
        return httpx2.Response(200, json={"snapshot_id": "should-not-happen"})

    dry_settings = Settings(**{**settings.__dict__, "dry_run": True})
    mcp, ctx = make_server_and_ctx(dry_settings, handler)

    preview_result = await mcp.call_tool(
        "add_playlist_items", {"playlist_id": "p1", "uris": ["spotify:track:t1"]}, ctx
    )
    token = json.loads(preview_result.content[0].text)["confirm_token"]

    exec_result = await mcp.call_tool(
        "add_playlist_items",
        {"playlist_id": "p1", "uris": ["spotify:track:t1"], "confirm_token": token},
        ctx,
    )
    body = json.loads(exec_result.content[0].text)
    assert body["status"] == "dry_run"
    assert writes_seen == []

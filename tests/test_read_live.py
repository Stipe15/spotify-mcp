"""Exercises the actual registered tool functions via MCPServer.call_tool(),
with a mocked HTTP transport — no real network, no lifespan startup. This is
the parts of read_live.py that have real logic: search query construction,
argument validation, and the empty-playback-state branch. The generic
Paging[T]/CursorPaging[T] round-trip is also verified here structurally, on
top of the live confirmation already done manually against a real account.
"""

from __future__ import annotations

import json

import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from spotify_mcp.config import Settings
from spotify_mcp.tools import read_live

from .conftest import make_server_and_ctx as _make_server_and_ctx


def make_server_and_ctx(settings: Settings, handler):
    return _make_server_and_ctx(settings, handler, read_live.register)


def _captured_query(request: httpx2.Request) -> str:
    return str(request.url.params.get("q", ""))


@pytest.mark.asyncio
async def test_search_catalog_builds_field_filtered_query(settings: Settings):
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["q"] = _captured_query(request)
        captured["type"] = str(request.url.params.get("type"))
        captured["limit"] = str(request.url.params.get("limit"))
        return httpx2.Response(200, json={"tracks": {"items": []}})

    mcp, ctx = make_server_and_ctx(settings, handler)
    result = await mcp.call_tool(
        "search_catalog", {"track": "Blinding Lights", "artist": "The Weeknd", "limit": 3}, ctx
    )

    assert not result.is_error
    assert captured["q"] == 'track:"Blinding Lights" artist:"The Weeknd"'
    assert captured["type"] == "track"
    assert captured["limit"] == "3"


@pytest.mark.asyncio
async def test_search_catalog_clamps_limit_to_ten(settings: Settings):
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["limit"] = str(request.url.params.get("limit"))
        return httpx2.Response(200, json={"tracks": {"items": []}})

    mcp, ctx = make_server_and_ctx(settings, handler)
    await mcp.call_tool("search_catalog", {"free_text": "test", "limit": 50}, ctx)

    assert captured["limit"] == "10"


@pytest.mark.asyncio
async def test_search_catalog_rejects_no_filters(settings: Settings):
    mcp, ctx = make_server_and_ctx(settings, lambda r: httpx2.Response(200, json={}))
    with pytest.raises(ToolError, match="at least one of"):
        await mcp.call_tool("search_catalog", {}, ctx)


@pytest.mark.asyncio
async def test_search_catalog_rejects_unsupported_type(settings: Settings):
    mcp, ctx = make_server_and_ctx(settings, lambda r: httpx2.Response(200, json={}))
    with pytest.raises(ToolError, match="Unsupported search type"):
        await mcp.call_tool("search_catalog", {"free_text": "x", "types": ["episode"]}, ctx)


@pytest.mark.asyncio
async def test_get_recently_played_rejects_both_cursors(settings: Settings):
    mcp, ctx = make_server_and_ctx(settings, lambda r: httpx2.Response(200, json={}))
    with pytest.raises(ToolError, match="mutually exclusive"):
        await mcp.call_tool("get_recently_played", {"after_ms": 1, "before_ms": 2}, ctx)


@pytest.mark.asyncio
async def test_get_playback_state_reports_inactive_on_empty_body(settings: Settings):
    mcp, ctx = make_server_and_ctx(settings, lambda r: httpx2.Response(204))
    result = await mcp.call_tool("get_playback_state", {}, ctx)

    assert not result.is_error
    body = json.loads(result.content[0].text)
    assert body["active"] is False
    assert body["is_playing"] is False


@pytest.mark.asyncio
async def test_get_playback_state_reports_active_with_data(settings: Settings):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"is_playing": True, "progress_ms": 1000})

    mcp, ctx = make_server_and_ctx(settings, handler)
    result = await mcp.call_tool("get_playback_state", {}, ctx)

    assert not result.is_error
    body = json.loads(result.content[0].text)
    assert body["active"] is True
    assert body["is_playing"] is True


@pytest.mark.asyncio
async def test_get_top_tracks_returns_unwrapped_paging_structure(settings: Settings):
    """Confirms Paging[Track] round-trips as a flat object, not wrapped in {"result": ...}."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "items": [
                    {
                        "id": "t1",
                        "name": "Song",
                        "uri": "spotify:track:t1",
                        "duration_ms": 1000,
                        "artists": [{"id": "a1", "name": "Artist", "uri": "spotify:artist:a1"}],
                    }
                ],
                "total": 1,
                "limit": 20,
                "offset": 0,
            },
        )

    mcp, ctx = make_server_and_ctx(settings, handler)
    result = await mcp.call_tool("get_top_tracks", {}, ctx)

    assert not result.is_error
    body = json.loads(result.content[0].text)
    assert body["total"] == 1
    assert body["items"][0]["id"] == "t1"
    assert "result" not in body


@pytest.mark.asyncio
async def test_get_playlist_extracts_item_count_and_requests_lean_fields(settings: Settings):
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["fields"] = str(request.url.params.get("fields"))
        return httpx2.Response(
            200,
            json={
                "id": "p1",
                "name": "My Playlist",
                "uri": "spotify:playlist:p1",
                "owner": {"id": "u1", "display_name": "me"},
                "items": {"total": 7},
            },
        )

    mcp, ctx = make_server_and_ctx(settings, handler)
    result = await mcp.call_tool("get_playlist", {"playlist_id": "p1"}, ctx)

    assert not result.is_error
    body = json.loads(result.content[0].text)
    assert body["item_count"] == 7
    assert "items.total" in captured["fields"]

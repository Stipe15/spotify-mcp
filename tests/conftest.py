from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest
from mcp.server import MCPServer
from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver.context import Context

from spotify_mcp.api.client import SpotifyClient
from spotify_mcp.auth.manager import TokenManager
from spotify_mcp.config import Settings
from spotify_mcp.server import AppContext
from spotify_mcp.tools.confirm import ConfirmationStore


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        client_id="test-client-id",
        redirect_port=8888,
        dry_run=False,
        max_retries=2,
        request_timeout_s=5.0,
        allow_removals=True,
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
    )


@pytest.fixture
def fixed_time(monkeypatch: pytest.MonkeyPatch):
    """Freeze time.time() at a known value; returns a mutable holder to advance it."""
    state = {"now": 1_000_000.0}
    monkeypatch.setattr(time, "time", lambda: state["now"])
    return state


class FixedTokenManager(TokenManager):
    """Bypasses real auth/store entirely; always reports authorized."""

    def __init__(self):
        self._token = None  # unused by bearer_token override below

    async def bearer_token(self) -> str:
        return "AT"

    async def force_refresh(self) -> str:
        return "AT"

    @property
    def is_authorized(self) -> bool:
        return True

    @property
    def granted_scopes(self) -> list[str]:
        return []

    @property
    def expires_in_s(self) -> float | None:
        return 3600.0

    async def aclose(self) -> None:
        pass


class _FakeSession:
    """Enough of ServerSession for tool code that reads ctx.client_capabilities."""

    client_capabilities = None


def make_server_and_ctx(
    settings: Settings,
    handler: Callable[[httpx2.Request], httpx2.Response],
    *register_fns: Callable[[MCPServer], None],
) -> tuple[MCPServer, Context]:
    """Build a real MCPServer with the given tool modules registered, plus a
    Context wired to a mocked HTTP transport — no real network, no lifespan
    startup. Used to exercise actual registered tool functions in tests.
    """
    mcp = MCPServer("test")
    for register in register_fns:
        register(mcp)

    transport = httpx2.MockTransport(handler)
    spotify = SpotifyClient(settings, FixedTokenManager(), transport=transport)
    app_ctx = AppContext(
        settings=settings,
        token_manager=FixedTokenManager(),
        spotify=spotify,
        confirmations=ConfirmationStore(ttl_s=settings.confirm_token_ttl_s),
    )

    req_ctx = ServerRequestContext(
        session=_FakeSession(),  # type: ignore[arg-type]
        lifespan_context=app_ctx,
        protocol_version="2025-06-18",
        method="tools/call",
    )
    ctx = Context(request_context=req_ctx, mcp_server=mcp)
    return mcp, ctx

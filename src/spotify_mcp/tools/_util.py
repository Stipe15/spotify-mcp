"""Shared helper for turning our own typed errors into clean MCP tool errors.

A `SpotifyMCPError` (auth failure, rate limit, a 4xx from Spotify) is a
failure we anticipated: the model should see its message and can often react
usefully. Anything else is a genuine crash and is left to propagate, so the
SDK logs it with a traceback and the client sees a generic failure rather
than an internal stack trace.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

from mcp.server.mcpserver.exceptions import ToolError

from spotify_mcp.errors import SpotifyMCPError

P = ParamSpec("P")
T = TypeVar("T")


def guarded(fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return await fn(*args, **kwargs)
        except SpotifyMCPError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper

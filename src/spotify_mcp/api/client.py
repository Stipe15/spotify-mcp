"""Thin async HTTP client for the Spotify Web API.

Handles: bearer injection via TokenManager, a bounded concurrency semaphore,
a rolling rate limiter, 429/5xx backoff, one 401-triggered re-auth retry, and
a dry-run mode that logs non-GET requests instead of sending them. Deliberately
not a general-purpose client — every call site names an actual endpoint from
PLAN.md §2; nothing here guesses at Spotify's URL shape.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx2

from spotify_mcp.api.ratelimit import RollingWindowLimiter
from spotify_mcp.auth.manager import TokenManager
from spotify_mcp.config import API_BASE_URL, Settings
from spotify_mcp.errors import RateLimited, SpotifyAPIError
from spotify_mcp.logging import get_logger

logger = get_logger("api.client")


class SpotifyClient:
    def __init__(
        self,
        settings: Settings,
        token_manager: TokenManager,
        transport: httpx2.AsyncBaseTransport | None = None,
    ):
        self._settings = settings
        self._tokens = token_manager
        self._http = httpx2.AsyncClient(
            base_url=API_BASE_URL, timeout=settings.request_timeout_s, transport=transport
        )
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._limiter = RollingWindowLimiter(settings.calls_per_30s)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request("GET", path, params=params)

    async def post(
        self, path: str, *, json: Any | None = None, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._request("POST", path, json=json, params=params)

    async def put(
        self, path: str, *, json: Any | None = None, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._request("PUT", path, json=json, params=params)

    async def delete(
        self, path: str, *, json: Any | None = None, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._request("DELETE", path, json=json, params=params)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        _retried_401: bool = False,
    ) -> dict[str, Any]:
        if self._settings.dry_run and method != "GET":
            logger.info("[DRY RUN] %s %s params=%r json=%r", method, path, params, json)
            return {"dry_run": True, "method": method, "path": path, "params": params, "json": json}

        token = await self._tokens.bearer_token()
        headers = {"Authorization": f"Bearer {token}"}

        attempt = 0
        while True:
            await self._limiter.acquire()
            async with self._semaphore:
                try:
                    response = await self._http.request(
                        method, path, params=params, json=json, headers=headers
                    )
                except httpx2.TransportError as exc:
                    attempt += 1
                    if attempt > self._settings.max_retries:
                        msg = f"transport error after {attempt} attempts: {exc}"
                        raise SpotifyAPIError(0, msg) from exc
                    await asyncio.sleep(_backoff_delay(attempt))
                    continue

            if response.status_code == 401 and not _retried_401:
                # Force a refresh (bearer_token() only refreshes on its own
                # expiry estimate, which can drift from the server's clock)
                # and replay exactly once.
                new_token = await self._tokens.force_refresh()
                headers["Authorization"] = f"Bearer {new_token}"
                return await self._request(
                    method, path, params=params, json=json, _retried_401=True
                )

            if response.status_code == 429:
                attempt += 1
                retry_after = float(response.headers.get("Retry-After", "1"))
                if attempt > self._settings.max_retries:
                    raise RateLimited(retry_after)
                await self._limiter.trip(retry_after)
                await asyncio.sleep(retry_after + random.uniform(0, 0.5))
                continue

            if response.status_code >= 500:
                attempt += 1
                if attempt > self._settings.max_retries:
                    raise SpotifyAPIError(response.status_code, "server error, retries exhausted")
                await asyncio.sleep(_backoff_delay(attempt))
                continue

            if response.status_code >= 400:
                _raise_for_error(response)

            if response.status_code == 204 or not response.content:
                return {}
            return response.json()


def _backoff_delay(attempt: int) -> float:
    base = min(2**attempt, 30)
    return random.uniform(0, base)


def _raise_for_error(response: httpx2.Response) -> None:
    reason = None
    message = response.text
    try:
        body = response.json()
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message", message)
            reason = error.get("reason")
        elif isinstance(error, str):
            message = body.get("error_description", error)
    except ValueError:
        pass
    raise SpotifyAPIError(response.status_code, message, reason)

"""Token lifecycle: initial exchange, expiry tracking, and single-flight refresh.

Concurrent tool calls all await the same `bearer_token()` coroutine's lock, so
ten calls racing an expired token produce exactly one refresh request instead
of a stampede — see PLAN.md §3.
"""

from __future__ import annotations

import asyncio
import time

import httpx2

from spotify_mcp.auth.store import StoredToken, clear_token, read_token, write_token
from spotify_mcp.config import TOKEN_URL, Settings
from spotify_mcp.errors import AuthError, TokenRefreshError
from spotify_mcp.logging import get_logger

logger = get_logger("auth.manager")


class TokenManager:
    def __init__(self, settings: Settings, http_client: httpx2.AsyncClient | None = None):
        self._settings = settings
        self._token: StoredToken | None = read_token(settings.token_path)
        self._lock = asyncio.Lock()
        self._owns_http = http_client is None
        self._http = http_client or httpx2.AsyncClient(timeout=15.0)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    @property
    def is_authorized(self) -> bool:
        return self._token is not None

    @property
    def granted_scopes(self) -> list[str]:
        return self._token.scope.split() if self._token else []

    def _expiring_soon(self) -> bool:
        if self._token is None:
            return True
        return time.time() >= (self._token.expires_at - self._settings.token_expiry_skew_s)

    @property
    def expires_in_s(self) -> float | None:
        if self._token is None:
            return None
        return max(0.0, self._token.expires_at - time.time())

    async def bearer_token(self) -> str:
        if self._token is None:
            raise AuthError("Not authorized. Run `spotify-mcp login` first.")
        if not self._expiring_soon():
            return self._token.access_token
        async with self._lock:
            # Re-check: another waiter may have already refreshed while we queued.
            if not self._expiring_soon():
                return self._token.access_token
            await self._do_refresh()
        assert self._token is not None
        return self._token.access_token

    async def force_refresh(self) -> str:
        """Refresh unconditionally, e.g. after a 401 the expiry estimate didn't predict.

        Single-flight via the same lock as `bearer_token()`: if a concurrent
        refresh is already in flight, this waits for it and returns its result
        rather than issuing a second request.
        """
        async with self._lock:
            await self._do_refresh()
        assert self._token is not None
        return self._token.access_token

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> None:
        """Initial PKCE token exchange. Called once by the `login` CLI command."""
        response = await self._http.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self._settings.client_id,
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code != 200:
            raise AuthError(f"Token exchange failed ({response.status_code}): {response.text}")
        self._store_response(response.json())

    async def _do_refresh(self) -> None:
        assert self._token is not None
        response = await self._http.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._token.refresh_token,
                "client_id": self._settings.client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code == 400:
            # invalid_grant: the refresh token itself was rejected. Clear it —
            # there is nothing left to retry, only re-authorization can fix this.
            clear_token(self._settings.token_path)
            self._token = None
            raise TokenRefreshError(
                "Refresh token was rejected by Spotify. Run `spotify-mcp login` again."
            )
        if response.status_code != 200:
            raise AuthError(f"Token refresh failed ({response.status_code}): {response.text}")
        self._store_response(response.json(), fallback_refresh_token=self._token.refresh_token)

    def _store_response(self, payload: dict, fallback_refresh_token: str | None = None) -> None:
        now = time.time()
        refresh_token = payload.get("refresh_token") or fallback_refresh_token
        if not refresh_token:
            raise AuthError("Token response had no refresh_token and none was already stored.")
        token = StoredToken(
            access_token=payload["access_token"],
            refresh_token=refresh_token,
            expires_at=now + float(payload["expires_in"]),
            scope=payload.get("scope", ""),
            obtained_at=now,
        )
        write_token(self._settings.token_path, token)
        self._token = token
        logger.info("token stored, expires_in=%s", payload.get("expires_in"))

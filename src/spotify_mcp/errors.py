"""Typed error hierarchy. Tool wrappers translate these into clean MCP tool errors."""

from __future__ import annotations


class SpotifyMCPError(Exception):
    """Base class for every error this server raises deliberately."""


class ConfigError(SpotifyMCPError):
    """Missing or invalid configuration (e.g. no client ID)."""


class AuthError(SpotifyMCPError):
    """No valid token is available and re-authorization is required."""


class TokenRefreshError(AuthError):
    """The refresh token was rejected; the stored token has been cleared."""


class RateLimited(SpotifyMCPError):
    """The Spotify API rate limit was hit and retries were exhausted."""

    def __init__(self, retry_after_s: float):
        self.retry_after_s = retry_after_s
        super().__init__(f"Rate limited by Spotify; retry after {retry_after_s:.1f}s")


class SpotifyAPIError(SpotifyMCPError):
    """A non-2xx response from the Spotify API that isn't a rate limit."""

    def __init__(self, status_code: int, message: str, reason: str | None = None):
        self.status_code = status_code
        self.reason = reason
        suffix = f" ({reason})" if reason else ""
        super().__init__(f"Spotify API error {status_code}: {message}{suffix}")

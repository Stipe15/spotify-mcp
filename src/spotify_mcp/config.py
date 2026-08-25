"""Runtime configuration: environment variables, .env, and derived paths."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import platformdirs
from dotenv import load_dotenv

from spotify_mcp.errors import ConfigError

APP_NAME = "spotify-mcp"
APP_AUTHOR = "spotify-mcp"

# Minimum scopes needed for the tools this server exposes. See PLAN.md §3 for
# the justification of each entry, and for the scopes deliberately omitted.
DEFAULT_SCOPES = [
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
    "ugc-image-upload",
    "user-top-read",
    "user-read-recently-played",
    "user-library-read",
    "user-follow-read",
    "user-read-playback-state",
]

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE_URL = "https://api.spotify.com/v1"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    client_id: str
    redirect_port: int = 8888
    redirect_path: str = "/callback"
    scopes: list[str] = field(default_factory=lambda: list(DEFAULT_SCOPES))

    dry_run: bool = False
    log_level: str = "INFO"

    # Off by default: removing playlist items is the one truly destructive
    # write this server exposes (PLAN.md §6). Must be explicitly opted into.
    allow_removals: bool = False
    confirm_token_ttl_s: int = 600

    max_concurrency: int = 4
    calls_per_30s: int = 90
    max_retries: int = 3
    request_timeout_s: float = 15.0
    token_expiry_skew_s: int = 60

    # Local analytics store (PLAN.md §4). IANA zone name, e.g. "Europe/Zagreb" —
    # there is no reliable cross-platform way to auto-detect this without an
    # extra dependency, so it's explicit. "UTC" is a safe but timezone-blind
    # default: hour-of-day analysis will be in UTC, not your local time, until set.
    local_timezone: str = "UTC"
    skip_ms_threshold: int = 30_000
    substantial_ms: int = 30_000
    query_timeout_s: float = 10.0
    analytics_max_rows: int = 5_000

    config_dir: Path = field(
        default_factory=lambda: Path(platformdirs.user_config_dir(APP_NAME, APP_AUTHOR))
    )
    data_dir: Path = field(
        default_factory=lambda: Path(platformdirs.user_data_dir(APP_NAME, APP_AUTHOR))
    )
    cache_dir: Path = field(
        default_factory=lambda: Path(platformdirs.user_cache_dir(APP_NAME, APP_AUTHOR))
    )

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.redirect_port}{self.redirect_path}"

    @property
    def token_path(self) -> Path:
        return self.config_dir / "token.json"

    @property
    def analytics_db_path(self) -> Path:
        return self.data_dir / "listening.duckdb"

    @classmethod
    def load(cls) -> Settings:
        load_dotenv()
        client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
        if not client_id:
            raise ConfigError(
                "SPOTIFY_CLIENT_ID is not set. Copy .env.example to .env, create a Spotify "
                "app at https://developer.spotify.com/dashboard, and paste its Client ID in."
            )
        settings = cls(
            client_id=client_id,
            redirect_port=_env_int("SPOTIFY_REDIRECT_PORT", 8888),
            dry_run=_env_bool("SPOTIFY_MCP_DRY_RUN", False),
            log_level=os.environ.get("SPOTIFY_MCP_LOG_LEVEL", "INFO").upper(),
            allow_removals=_env_bool("SPOTIFY_MCP_ALLOW_REMOVALS", False),
            max_concurrency=_env_int("SPOTIFY_MCP_MAX_CONCURRENCY", 4),
            calls_per_30s=_env_int("SPOTIFY_MCP_CALLS_PER_30S", 90),
            max_retries=_env_int("SPOTIFY_MCP_MAX_RETRIES", 3),
            request_timeout_s=_env_float("SPOTIFY_MCP_REQUEST_TIMEOUT_S", 15.0),
            local_timezone=os.environ.get("SPOTIFY_MCP_TIMEZONE", "UTC"),
            skip_ms_threshold=_env_int("SPOTIFY_MCP_SKIP_MS_THRESHOLD", 30_000),
            substantial_ms=_env_int("SPOTIFY_MCP_SUBSTANTIAL_MS", 30_000),
            query_timeout_s=_env_float("SPOTIFY_MCP_QUERY_TIMEOUT_S", 10.0),
            analytics_max_rows=_env_int("SPOTIFY_MCP_ANALYTICS_MAX_ROWS", 5_000),
        )
        settings.config_dir.mkdir(parents=True, exist_ok=True)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.cache_dir.mkdir(parents=True, exist_ok=True)
        return settings

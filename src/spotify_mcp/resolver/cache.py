"""On-disk resolution cache, keyed by normalized artist|title (PLAN.md §7).

Lives in its own DuckDB file, separate from the listening-history analytics
database — resolution needs to work whether or not you've ever run
`spotify-mcp ingest`, so it can't depend on that database existing.

Negative results (nothing acceptable found) are cached too, with a shorter
TTL, so re-running a chart pipeline doesn't re-pay for known failures on
every attempt — but does eventually retry them, since a track absent from
the catalog today might exist tomorrow.

The cache stores the objective resolution outcome (including its actual
confidence) regardless of the caller's `strict` setting — whether a "weak"
match is accepted into `resolved` or held back to `unresolved` is decided
fresh on every call, on top of whatever the cache (or a live search) found.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb

_SCHEMA = """
CREATE TABLE IF NOT EXISTS resolution_cache (
    norm_key VARCHAR PRIMARY KEY,
    is_negative BOOLEAN NOT NULL,
    track_uri VARCHAR,
    track_id VARCHAR,
    artist_id VARCHAR,
    matched_artist_name VARCHAR,
    matched_title VARCHAR,
    isrc VARCHAR,
    confidence VARCHAR,
    method VARCHAR,
    resolved_at TIMESTAMP NOT NULL,
    search_calls INTEGER NOT NULL
)
"""

POSITIVE_TTL_S = 30 * 24 * 3600  # 30 days — catalog entries rarely change
NEGATIVE_TTL_S = 7 * 24 * 3600  # 7 days — worth retrying sooner

_COLUMNS = (
    "is_negative",
    "track_uri",
    "track_id",
    "artist_id",
    "matched_artist_name",
    "matched_title",
    "isrc",
    "confidence",
    "method",
    "resolved_at",
)


@dataclass
class CachedResolution:
    is_negative: bool
    track_uri: str | None
    track_id: str | None
    artist_id: str | None
    matched_artist_name: str | None
    matched_title: str | None
    isrc: str | None
    confidence: str | None
    method: str | None


class ResolutionCache:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(db_path))
        self._con.execute(_SCHEMA)

    def close(self) -> None:
        self._con.close()

    def get(self, norm_key: str) -> CachedResolution | None:
        row = self._con.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM resolution_cache WHERE norm_key = ?",
            [norm_key],
        ).fetchone()
        if row is None:
            return None
        values = dict(zip(_COLUMNS, row, strict=True))
        resolved_at = values.pop("resolved_at")
        ttl = NEGATIVE_TTL_S if values["is_negative"] else POSITIVE_TTL_S
        if time.time() - resolved_at.timestamp() > ttl:
            return None
        return CachedResolution(**values)

    def put_positive(
        self,
        norm_key: str,
        *,
        track_uri: str,
        track_id: str,
        artist_id: str,
        matched_artist_name: str,
        matched_title: str,
        isrc: str | None,
        confidence: str,
        method: str,
        search_calls: int,
    ) -> None:
        self._upsert(
            norm_key,
            is_negative=False,
            track_uri=track_uri,
            track_id=track_id,
            artist_id=artist_id,
            matched_artist_name=matched_artist_name,
            matched_title=matched_title,
            isrc=isrc,
            confidence=confidence,
            method=method,
            search_calls=search_calls,
        )

    def put_negative(self, norm_key: str, *, method: str, search_calls: int) -> None:
        self._upsert(
            norm_key,
            is_negative=True,
            track_uri=None,
            track_id=None,
            artist_id=None,
            matched_artist_name=None,
            matched_title=None,
            isrc=None,
            confidence=None,
            method=method,
            search_calls=search_calls,
        )

    def _upsert(
        self,
        norm_key: str,
        *,
        is_negative: bool,
        track_uri: str | None,
        track_id: str | None,
        artist_id: str | None,
        matched_artist_name: str | None,
        matched_title: str | None,
        isrc: str | None,
        confidence: str | None,
        method: str,
        search_calls: int,
    ) -> None:
        self._con.execute("DELETE FROM resolution_cache WHERE norm_key = ?", [norm_key])
        self._con.execute(
            "INSERT INTO resolution_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                norm_key,
                is_negative,
                track_uri,
                track_id,
                artist_id,
                matched_artist_name,
                matched_title,
                isrc,
                confidence,
                method,
                _now(),
                search_calls,
            ],
        )


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

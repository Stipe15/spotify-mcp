"""Conditional-GET (ETag) cache for the Spotify client.

Verified empirically against the real API (PLAN.md §5 update, Phase 6):
every GET response carries an `ETag`, but also `Cache-Control: max-age=0` —
Spotify is explicit that a response must be revalidated on every request,
never served from a blind local TTL. So this cache never skips the network:
every cached GET still goes out with `If-None-Match`, and only skips
re-parsing (and re-transferring) the body when Spotify itself confirms
nothing changed via a 304.

This means the benefit is bandwidth and latency, not Spotify rate-limit
quota — a 304 is still one HTTP call, same as a 200 would have been. The
original plan's "costs no quota" framing doesn't hold up against what the
API actually sends; corrected here rather than carried forward.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class ETagCache:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(db_path), check_same_thread=False)
        with self._lock:
            self._con.execute(
                "CREATE TABLE IF NOT EXISTS etag_cache ("
                "cache_key TEXT PRIMARY KEY, etag TEXT NOT NULL, "
                "body TEXT NOT NULL, cached_at REAL NOT NULL)"
            )
            self._con.commit()

    def get(self, cache_key: str) -> tuple[str, dict[str, Any]] | None:
        with self._lock:
            row = self._con.execute(
                "SELECT etag, body FROM etag_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
        if row is None:
            return None
        etag, body = row
        return etag, json.loads(body)

    def put(self, cache_key: str, etag: str, body: dict[str, Any]) -> None:
        with self._lock:
            self._con.execute(
                "INSERT INTO etag_cache (cache_key, etag, body, cached_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET "
                "etag = excluded.etag, body = excluded.body, cached_at = excluded.cached_at",
                (cache_key, etag, json.dumps(body), time.time()),
            )
            self._con.commit()

    def close(self) -> None:
        with self._lock:
            self._con.close()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            row = self._con.execute(
                "SELECT COUNT(*), MIN(cached_at), MAX(cached_at) FROM etag_cache"
            ).fetchone()
        count, oldest, newest = row
        return {"entries": count, "oldest_cached_at": oldest, "newest_cached_at": newest}


def cache_key(path: str, params: dict[str, Any] | None) -> str:
    """Stable key for a GET request — path plus its query parameters,
    order-independent (a caller building the same request with dict keys in
    a different order must hit the same cache entry)."""
    normalized = json.dumps(params or {}, sort_keys=True, default=str)
    return f"{path}?{normalized}"

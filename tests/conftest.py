from __future__ import annotations

import time
from pathlib import Path

import pytest

from spotify_mcp.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        client_id="test-client-id",
        redirect_port=8888,
        dry_run=False,
        max_retries=2,
        request_timeout_s=5.0,
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

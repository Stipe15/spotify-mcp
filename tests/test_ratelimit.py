from __future__ import annotations

import asyncio
import time

import pytest

from spotify_mcp.api.ratelimit import RollingWindowLimiter


@pytest.mark.asyncio
async def test_acquire_is_immediate_under_capacity():
    limiter = RollingWindowLimiter(max_calls=5, window_s=1.0)
    start = time.monotonic()
    for _ in range(5):
        await limiter.acquire()
    assert time.monotonic() - start < 0.2


@pytest.mark.asyncio
async def test_acquire_blocks_once_capacity_is_exhausted():
    limiter = RollingWindowLimiter(max_calls=2, window_s=0.3)
    await limiter.acquire()
    await limiter.acquire()

    start = time.monotonic()
    await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.2  # had to wait for the window to free up


@pytest.mark.asyncio
async def test_trip_shortens_an_already_waiting_acquirer():
    # Without trip(), a waiter here would need the full 5s window to free up.
    limiter = RollingWindowLimiter(max_calls=1, window_s=5.0)
    await limiter.acquire()  # exhaust capacity

    async def waiter():
        start = time.monotonic()
        await limiter.acquire()
        return time.monotonic() - start

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)  # let the waiter start polling and go to sleep
    await limiter.trip(0.3)  # a 429's Retry-After: much shorter than the 5s window

    elapsed = await asyncio.wait_for(task, timeout=2.0)
    assert 0.2 <= elapsed <= 1.5

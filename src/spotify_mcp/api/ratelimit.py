"""Self-imposed rolling 30-second token bucket.

Spotify doesn't publish its Dev Mode rate-limit numbers (PLAN.md R2), so this
is a conservative ceiling we control, not a mirror of a documented limit.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque


class RollingWindowLimiter:
    def __init__(self, max_calls: int, window_s: float = 30.0):
        self._max_calls = max_calls
        self._window_s = window_s
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    # Cap on a single poll sleep. A `trip()` from a concurrent caller mutates
    # `_calls` but can't wake an acquirer already sleeping, so polling in
    # bounded chunks — rather than one sleep for the whole computed wait — is
    # what keeps this responsive to a trip within about a second.
    _MAX_POLL_S = 1.0

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                cutoff = now - self._window_s
                while self._calls and self._calls[0] < cutoff:
                    self._calls.popleft()
                if len(self._calls) < self._max_calls:
                    self._calls.append(now)
                    return
                sleep_for = self._calls[0] - cutoff
            await asyncio.sleep(max(min(sleep_for, self._MAX_POLL_S), 0.01))

    async def trip(self, seconds: float) -> None:
        """Poison the window so every acquirer — this caller included — waits
        out `seconds` before its next call. Used after a 429: one caller
        learned the real cooldown from `Retry-After`, so every other
        in-flight caller should wait it out too rather than each discovering
        the limit independently.
        """
        async with self._lock:
            now = time.monotonic()
            # Backdate entries so they age out of the window at `now + seconds`,
            # even if `seconds` exceeds the window itself.
            entry_time = now + seconds - self._window_s
            self._calls.clear()
            self._calls.extend([entry_time] * self._max_calls)

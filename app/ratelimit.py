"""In-memory limits that protect the free LLM/search quota. Single-process only (documented limitation)."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Callable


class RateLimiter:
    def __init__(self, per_ip: int, window_s: int, daily_cap: int, clock: Callable[[], float] = time.time):
        self.per_ip, self.window_s, self.daily_cap, self.clock = per_ip, window_s, daily_cap, clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._day = ""
        self._day_count = 0

    def _today(self) -> str:
        return datetime.fromtimestamp(self.clock(), tz=timezone.utc).strftime("%Y-%m-%d")

    def check_and_consume(self, ip: str) -> tuple[bool, str, int]:
        """Returns (allowed, message, retry_after_seconds)."""
        now = self.clock()
        today = self._today()
        if today != self._day:
            self._day, self._day_count = today, 0
        if self._day_count >= self.daily_cap:
            return False, "This demo has reached its daily limit for new guides. Existing guides still work. Try again tomorrow.", 3600
        q = self._hits[ip]
        while q and now - q[0] > self.window_s:
            q.popleft()
        if len(q) >= self.per_ip:
            wait = int(self.window_s - (now - q[0])) + 1
            return False, f"You can generate {self.per_ip} new guides per hour. Try again in about {max(wait // 60, 1)} minutes.", wait
        q.append(now)
        self._day_count += 1
        if len(self._hits) > 5000:  # bound memory
            for k in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window_s]:
                del self._hits[k]
        return True, "", 0

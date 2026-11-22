"""Per-client request limits, used as FastAPI dependencies on public routes.

Algorithm: sliding-window log. A client may make at most `limit` requests in any
`window_seconds` span. Only allowed requests are recorded, so a client that keeps
retrying is not locked out longer than the window, and Retry-After is exact: it is
the time until the oldest recorded request leaves the window.

Storage: MemoryWindowStore keeps state in this process only. Several uvicorn
workers or servers each keep their own counts, multiplying the effective limit.
A shared store (e.g. Redis) must implement the same `hit()` to fix that.

Client identity: request.client.host, the peer address uvicorn reports. This file
never reads X-Forwarded-For itself; uvicorn applies it only when the direct peer is
in --forwarded-allow-ips (default 127.0.0.1), so other clients cannot spoof it.
"""
from collections import deque
import logging
import math
import os
import threading
import time

from fastapi import Request

from app.api_contract import too_many_requests

logger = logging.getLogger(__name__)


class MemoryWindowStore:
    """Thread-safe sliding-window log; at most `limit` timestamps per client key."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()
        self._next_sweep = -math.inf

    def hit(self, key: str, limit: int, window: float) -> int:
        """Record one request if allowed. Return 0, or the whole seconds until one is allowed."""
        with self._lock:  # check and record together, so concurrent requests cannot overshoot
            now = self.clock()
            self._sweep(now, window)
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= limit:
                return max(1, math.ceil(hits[0] + window - now))
            hits.append(now)
            return 0

    def _sweep(self, now, window):
        # At most once per window, drop clients with no request inside the window.
        if now < self._next_sweep:
            return
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= now - window]:
            del self._hits[key]
        self._next_sweep = now + window

    def __len__(self):
        return len(self._hits)

    def clear(self):
        with self._lock:
            self._hits.clear()
            self._next_sweep = -math.inf


def positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None or raw.strip() == "" else int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive integer") from None
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def client_key(request: Request) -> str:
    # No peer address (unusual ASGI servers) puts those requests in one shared bucket.
    return request.client.host if request.client and request.client.host else "unknown"


class RateLimiter:
    """Dependency that raises the 429 contract error once a client exceeds its limit.

    Routes that share one instance share one budget per client.
    """

    def __init__(self, name: str, limit: int, window_seconds: int, store=None):
        if limit < 1 or window_seconds < 1:
            raise ValueError("limit and window_seconds must be positive")
        self.name = name
        self.limit = limit
        self.window_seconds = window_seconds
        self.store = store if store is not None else MemoryWindowStore()

    @classmethod
    def from_env(cls, name: str, prefix: str, default_limit: int, default_window_seconds: int):
        return cls(name, positive_int_env(f"{prefix}_RATE_LIMIT_REQUESTS", default_limit),
                   positive_int_env(f"{prefix}_RATE_LIMIT_WINDOW_SECONDS", default_window_seconds))

    def __call__(self, request: Request) -> None:
        try:
            retry_after = self.store.hit(client_key(request), self.limit, self.window_seconds)
        except Exception as exc:
            # Fail open: a broken limiter must not block sign-up. Unique indexes still
            # guard the data. Log no client address or request values.
            logger.error("rate limit %s unavailable, request allowed: %s", self.name, type(exc).__name__)
            return
        if retry_after:
            raise too_many_requests(retry_after)

    def reset(self):
        self.store.clear()

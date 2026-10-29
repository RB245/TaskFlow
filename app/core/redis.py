"""Shared Redis client.

One connection pool per process, created lazily so importing the app does not
require Redis to be reachable (tests and `alembic` would otherwise need it).
"""

from __future__ import annotations

import contextlib

import redis

from app.core.config import settings

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=False,  # cache payloads are JSON bytes
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
            # A slow or unreachable Redis must never become a slow API. One
            # second is far above a healthy round trip, and the cache layer
            # degrades to Postgres rather than waiting.
            health_check_interval=30,
            retry_on_timeout=False,
        )
    return _client


def reset_redis() -> None:
    """Drop the cached client. Used by tests that need a fresh connection."""
    global _client
    if _client is not None:
        with contextlib.suppress(Exception):  # best effort; we are discarding it anyway
            _client.close()
    _client = None

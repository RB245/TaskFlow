"""Cache for the filtered task feed.

The brief treats a stale read after a status change as a bug, so the design
starts from invalidation and works backwards.

Why versioned keys rather than deleting keys
--------------------------------------------
The obvious approach - ``SCAN`` for ``tasks:*`` and ``DEL`` the matches - is
wrong twice over. It is O(keyspace) on every write, and it is not atomic: a
reader that fetched a key a microsecond before the delete can still write its
stale result back under that key afterwards.

Instead every project carries a monotonic counter::

    tf:1:projver:<project_id>   ->  integer, no TTL

and the counter values of every project in scope are folded into the cache
key. Invalidation is therefore a single ``INCR`` per affected project: O(1),
atomic, and it makes every previously cached entry for that project
*unreachable* rather than merely scheduled for deletion. There is no window in
which an old key can be found.

What goes in the key
--------------------
Everything that can change the result set: the (project_id, version) pairs in
scope, every filter value, the sort, and the page coordinates. The invariant
to preserve when adding a filter is that **nothing may be resolved from the
caller's identity after the fingerprint is computed** - otherwise two callers
could share a key whose contents differ.

Note the key is scoped to the set of projects, not to the user. Two members of
the same project asking the same question must get the same answer, so giving
them separate entries would only halve the hit rate. Authorization happens
before the cache is consulted, so a non-member never reaches a lookup at all.

Failure behaviour
-----------------
A cache that can go stale is worse than no cache. If any Redis call fails the
layer enters a degraded window and *reads bypass the cache entirely* for at
least the payload TTL. That matters most for a failed ``INCR``: if a write
committed but its invalidation did not land, entries under the old version are
still reachable, and the only safe response is to stop trusting the cache
until they have expired.

On eviction: version keys are written without a TTL and Redis is configured
``maxmemory-policy volatile-lru``, so keys without a TTL are never evicted. Even
if one were lost, a recreated counter starting from zero yields a *different*
fingerprint from the live one, so the effect is a miss, not a stale hit - the
only way to serve stale data would be to coincidentally land back on a version
whose entries are still within their 60s TTL.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Iterable, Sequence
from typing import Any, cast

from redis.exceptions import RedisError

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import (
    CACHE_HITS,
    CACHE_INVALIDATION_FAILURES,
    CACHE_INVALIDATIONS,
    CACHE_MISSES,
)
from app.core.redis import get_redis

logger = get_logger(__name__)

# Bump when the cached payload's shape changes, so a rolling deploy cannot
# serve an old-shaped body to new code.
CACHE_SCHEMA_VERSION = 1

_KEY_PREFIX = f"{settings.cache_key_prefix}:{CACHE_SCHEMA_VERSION}"

# Monotonic deadline; while now() < this, reads bypass the cache.
_degraded_until: float = 0.0


def _version_key(project_id: uuid.UUID) -> str:
    return f"{_KEY_PREFIX}:projver:{project_id}"


def is_degraded() -> bool:
    return time.monotonic() < _degraded_until


def _degrade(reason: str, seconds: float | None = None) -> None:
    """Stop serving from cache for a while.

    The default window is the payload TTL: long enough for every entry that
    might have escaped invalidation to expire on its own.
    """
    global _degraded_until
    window = settings.cache_ttl_seconds if seconds is None else seconds
    _degraded_until = max(_degraded_until, time.monotonic() + window)
    logger.warning("cache.degraded", extra={"reason": reason, "window_seconds": window})


def reset_degraded() -> None:
    """Test hook - clears the degraded window."""
    global _degraded_until
    _degraded_until = 0.0


def get_project_versions(project_ids: Sequence[uuid.UUID]) -> dict[str, int] | None:
    """Current counter for each project. ``None`` means Redis is unusable."""
    if not project_ids:
        return {}
    try:
        # cast: redis-py's type stubs describe the async client's return type
        # as well, so the sync call looks like it might be awaitable.
        raw = cast(list[bytes | None], get_redis().mget([_version_key(pid) for pid in project_ids]))
    except RedisError as exc:
        _degrade(f"mget failed: {exc.__class__.__name__}")
        return None
    return {
        str(pid): int(value) if value is not None else 0
        for pid, value in zip(project_ids, raw, strict=True)
    }


def build_key(versions: dict[str, int], query_fingerprint: dict[str, Any]) -> str:
    scope = [f"{pid}:{versions[pid]}" for pid in sorted(versions)]
    material = json.dumps(
        {"scope": scope, "query": query_fingerprint},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    # SHA-1 is fine here: this is a content fingerprint, not a security
    # boundary, and a collision would only ever mean a shared cache entry
    # between two queries that are byte-identical in every field anyway.
    digest = hashlib.sha1(material.encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}:tasks:{digest}"


def get_cached(key: str) -> dict[str, Any] | None:
    if not settings.cache_enabled or is_degraded():
        return None
    try:
        raw = cast("bytes | None", get_redis().get(key))
    except RedisError as exc:
        _degrade(f"get failed: {exc.__class__.__name__}")
        return None

    if raw is None:
        CACHE_MISSES.labels(endpoint="tasks_list").inc()
        return None

    try:
        payload: dict[str, Any] = json.loads(raw)
    except (ValueError, TypeError):
        # Corrupt or old-format entry: treat as a miss rather than a 500.
        CACHE_MISSES.labels(endpoint="tasks_list").inc()
        return None

    CACHE_HITS.labels(endpoint="tasks_list").inc()
    return payload


def set_cached(key: str, payload: dict[str, Any], *, is_empty: bool) -> None:
    if not settings.cache_enabled or is_degraded():
        return
    # Empty results are cached too, with a shorter TTL: a filter that matches
    # nothing is exactly the query a client will retry in a loop, and it
    # should not reach Postgres every time.
    ttl = settings.cache_empty_ttl_seconds if is_empty else settings.cache_ttl_seconds
    try:
        get_redis().set(key, json.dumps(payload, default=str), ex=ttl)
    except RedisError as exc:
        # A failed write is harmless - it just means a miss next time - so
        # this does not degrade the cache.
        logger.warning("cache.set_failed", extra={"error": exc.__class__.__name__})


def invalidate_projects(project_ids: Iterable[uuid.UUID]) -> None:
    """Make every cached entry touching these projects unreachable.

    Must be called *after* the database transaction commits. Invalidating
    first leaves a window where a concurrent read can repopulate the cache
    from the pre-commit state and then look fresh.
    """
    unique = {pid for pid in project_ids if pid is not None}
    if not unique or not settings.cache_enabled:
        return

    try:
        pipe = get_redis().pipeline(transaction=False)
        for pid in unique:
            pipe.incr(_version_key(pid))
        pipe.execute()
    except RedisError as exc:
        CACHE_INVALIDATION_FAILURES.inc()
        # The dangerous case. The write landed in Postgres but entries under
        # the old version are still reachable, so stop serving from cache
        # until they expire.
        _degrade(f"invalidation failed: {exc.__class__.__name__}")
        logger.error(
            "cache.invalidation_failed",
            extra={"project_ids": [str(pid) for pid in unique]},
        )
        return

    CACHE_INVALIDATIONS.inc(len(unique))
    logger.debug("cache.invalidated", extra={"project_ids": [str(pid) for pid in unique]})

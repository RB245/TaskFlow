"""Operational endpoints: health, readiness and metrics.

Deliberately outside ``/api/v1``: these are for orchestrators and Prometheus,
not API clients, and their contract should not move when the API version
does. They are also unauthenticated - a probe that needs a token is a probe
that fails during exactly the incident you need it for.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from pydantic import BaseModel
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import DbSession
from app.core import metrics
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.services import cache

logger = get_logger(__name__)

router = APIRouter(tags=["ops"])


class HealthResponse(BaseModel):
    status: str


class ReadyResponse(BaseModel):
    status: str
    checks: dict[str, str]
    cache_degraded: bool


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness. Intentionally checks nothing external.

    If this returned unhealthy when Postgres was down, the orchestrator would
    kill and restart every API container during a database blip - removing
    the capacity that would otherwise serve cached reads and recover.
    """
    return HealthResponse(status="ok")


@router.get("/health/ready", response_model=ReadyResponse)
def ready(db: DbSession, response: Response) -> ReadyResponse:
    """Readiness. Checks the dependencies needed to serve traffic."""
    checks: dict[str, str] = {}

    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except SQLAlchemyError as exc:
        checks["database"] = f"error: {exc.__class__.__name__}"

    try:
        get_redis().ping()
        checks["redis"] = "ok"
    except RedisError as exc:
        checks["redis"] = f"error: {exc.__class__.__name__}"

    # Redis being down degrades the service (slower, uncached) but does not
    # make it unable to serve, so readiness hinges on the database alone.
    healthy = checks["database"] == "ok"
    if not healthy:
        response.status_code = 503

    return ReadyResponse(
        status="ok" if healthy else "unavailable",
        checks=checks,
        cache_degraded=cache.is_degraded(),
    )


@router.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    return Response(content=metrics.render(), media_type=metrics.CONTENT_TYPE_LATEST)

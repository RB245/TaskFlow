"""Prometheus metrics.

Label cardinality is the thing that kills a metrics endpoint, so HTTP series
are labelled with the *route template* (``/api/v1/tasks/{task_id}``) and never
the raw path - otherwise every task id a client has ever requested becomes a
permanent time series.

These counters live in the default process registry. That is correct for the
deployment in docker-compose.yml, where each container runs a single uvicorn
worker and Prometheus scrapes it directly. Running multiple workers in one
container would need ``prometheus_client``'s multiprocess mode; that tradeoff
is called out in the README.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST as _OM_CONTENT_TYPE

REGISTRY = CollectorRegistry(auto_describe=True)

CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
OPENMETRICS_CONTENT_TYPE = _OM_CONTENT_TYPE

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests by method, route template and status code.",
    labelnames=("method", "route", "status"),
    registry=REGISTRY,
)

HTTP_ERRORS = Counter(
    "http_errors_total",
    "HTTP responses with a 4xx or 5xx status, by route template and status class.",
    labelnames=("method", "route", "status_class"),
    registry=REGISTRY,
)

HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "Request latency by route template.",
    labelnames=("method", "route"),
    # Tuned for an API where anything past a second is already a problem.
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=REGISTRY,
)

HTTP_IN_PROGRESS = Gauge(
    "http_requests_in_progress",
    "Requests currently being served.",
    registry=REGISTRY,
)

UNHANDLED_EXCEPTIONS = Counter(
    "http_unhandled_exceptions_total",
    "Exceptions that escaped to the catch-all handler.",
    labelnames=("route",),
    registry=REGISTRY,
)

CACHE_HITS = Counter(
    "cache_hits_total",
    "Cache lookups served from Redis.",
    labelnames=("endpoint",),
    registry=REGISTRY,
)

CACHE_MISSES = Counter(
    "cache_misses_total",
    "Cache lookups that fell through to Postgres.",
    labelnames=("endpoint",),
    registry=REGISTRY,
)

CACHE_INVALIDATIONS = Counter(
    "cache_invalidations_total",
    "Project cache-version bumps performed after a write.",
    registry=REGISTRY,
)

CACHE_INVALIDATION_FAILURES = Counter(
    "cache_invalidation_failures_total",
    "Invalidations that failed. Non-zero means the cache was serving "
    "potentially stale data and entered its degraded window.",
    registry=REGISTRY,
)

NOTIFICATIONS_CREATED = Counter(
    "notifications_created_total",
    "Notification rows created, by type.",
    labelnames=("type",),
    registry=REGISTRY,
)

NOTIFICATIONS_SUPPRESSED = Counter(
    "notifications_suppressed_total",
    "Notification inserts rejected as duplicates by the idempotency "
    "constraint. Expected to be non-zero; it means retries are safe.",
    labelnames=("type",),
    registry=REGISTRY,
)

JOB_ENQUEUE_FAILURES = Counter(
    "job_enqueue_failures_total",
    "Background jobs that could not be handed to the broker. Each one is a "
    "notification that will never be created.",
    labelnames=("task",),
    registry=REGISTRY,
)

CELERY_TASKS = Counter(
    "celery_tasks_total",
    "Celery task executions by name and outcome.",
    labelnames=("task", "outcome"),
    registry=REGISTRY,
)

CELERY_TASK_DURATION = Histogram(
    "celery_task_duration_seconds",
    "Celery task execution time by name.",
    labelnames=("task",),
    registry=REGISTRY,
)

OVERDUE_SCAN_TASKS = Counter(
    "overdue_scan_tasks_total",
    "Tasks picked up by the periodic overdue scan.",
    registry=REGISTRY,
)


def render() -> bytes:
    return generate_latest(REGISTRY)

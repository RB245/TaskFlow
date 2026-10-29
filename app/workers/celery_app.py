"""Celery application and beat schedule."""

from __future__ import annotations

from typing import Any

from celery import Celery
from celery.signals import setup_logging, task_prerun

from app.core.config import settings
from app.core.logging import configure_logging, set_request_id

celery_app = Celery(
    "taskflow",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Redelivery on worker loss is only safe because every job is idempotent
    # (see the unique constraint on notifications).
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=300,
    task_soft_time_limit=270,
    result_expires=3600,
    task_always_eager=settings.celery_task_always_eager,
    task_eager_propagates=True,
    broker_connection_retry_on_startup=True,
)

celery_app.conf.beat_schedule = {
    "scan-overdue-tasks": {
        "task": "taskflow.scan_overdue_tasks",
        "schedule": float(settings.overdue_scan_interval_seconds),
        # Beat can emit a duplicate tick after a restart; expire the message
        # so a backed-up queue does not run a stale scan much later.
        "options": {"expires": settings.overdue_scan_interval_seconds * 2},
    },
}


@setup_logging.connect
def _configure_celery_logging(**_: Any) -> None:
    # Stops Celery from installing its own handlers, so worker output is the
    # same JSON shape as the API's.
    configure_logging()


@task_prerun.connect
def _bind_request_id(task: Any = None, **_: Any) -> None:
    """Carry the originating request id into the worker.

    Producers put it in the message headers; picking it up here means a
    notification's log line can be traced back to the API request that caused
    it.
    """
    headers = getattr(getattr(task, "request", None), "headers", None) or {}
    set_request_id(headers.get("request_id"))

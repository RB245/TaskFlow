"""Background jobs.

Every job here is idempotent. That is not incidental: ``task_acks_late`` is on,
so a worker that dies mid-job will have its message redelivered, and beat can
emit a duplicate tick across a restart. Idempotency is enforced by the unique
constraint on ``notifications`` rather than by any check-then-insert in this
file, because two workers running concurrently would race past such a check.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import CELERY_TASK_DURATION, CELERY_TASKS, OVERDUE_SCAN_TASKS
from app.db.session import session_scope
from app.models import NotificationType, Project, Task, TaskStatus
from app.services.notification_service import create_notification
from app.workers.celery_app import celery_app

logger = get_logger(__name__)

RETRY_KWARGS: dict[str, Any] = {
    "autoretry_for": (Exception,),
    "retry_backoff": True,
    "retry_backoff_max": 300,
    "retry_jitter": True,
    "max_retries": 5,
}


def _record(name: str, outcome: str, started: float) -> None:
    CELERY_TASKS.labels(task=name, outcome=outcome).inc()
    CELERY_TASK_DURATION.labels(task=name).observe(time.perf_counter() - started)


@celery_app.task(name="taskflow.notify_task_reassigned", **RETRY_KWARGS)
def notify_task_reassigned(
    task_id: str,
    assignee_id: str,
    occurred_at: str,
    previous_assignee_id: str | None = None,
) -> bool:
    """Tell the new assignee a task is now theirs.

    ``occurred_at`` is produced by the caller, not by this worker, so a retry
    recomputes the same dedupe key. Using ``now()`` in here would make every
    retry look like a fresh event.
    """
    started = time.perf_counter()
    with session_scope() as db:
        task = db.get(Task, uuid.UUID(task_id))
        if task is None:
            # Deleted between enqueue and execution. Not an error.
            _record("notify_task_reassigned", "skipped", started)
            return False

        create_notification(
            db,
            user_id=uuid.UUID(assignee_id),
            task_id=task.id,
            type_=NotificationType.task_reassigned,
            dedupe_key=f"to:{assignee_id}:at:{occurred_at}",
            payload={
                "title": task.title,
                "project_id": str(task.project_id),
                "previous_assignee_id": previous_assignee_id,
                "occurred_at": occurred_at,
            },
        )

    _record("notify_task_reassigned", "success", started)
    return True


@celery_app.task(name="taskflow.notify_task_status_changed", **RETRY_KWARGS)
def notify_task_status_changed(
    task_id: str,
    recipient_id: str,
    old_status: str,
    new_status: str,
    occurred_at: str,
) -> bool:
    started = time.perf_counter()
    with session_scope() as db:
        task = db.get(Task, uuid.UUID(task_id))
        if task is None:
            _record("notify_task_status_changed", "skipped", started)
            return False

        create_notification(
            db,
            user_id=uuid.UUID(recipient_id),
            task_id=task.id,
            type_=NotificationType.task_status_changed,
            # The transition and its timestamp are both in the key, so moving
            # todo -> done -> todo -> done produces two notifications while a
            # retry of either produces none.
            dedupe_key=f"{old_status}->{new_status}:at:{occurred_at}",
            payload={
                "title": task.title,
                "project_id": str(task.project_id),
                "old_status": old_status,
                "new_status": new_status,
                "occurred_at": occurred_at,
            },
        )

    _record("notify_task_status_changed", "success", started)
    return True


@celery_app.task(name="taskflow.scan_overdue_tasks", **RETRY_KWARGS)
def scan_overdue_tasks(batch_size: int | None = None) -> int:
    """Create a notification for every task whose due date has passed while
    it is still not done.

    Runs on beat rather than being scheduled per-task at write time: a
    per-task countdown would need cancelling and rescheduling on every due
    date edit, and would silently lose pending work if the broker were ever
    flushed. A periodic sweep over an indexed predicate is both cheaper to
    reason about and self-healing after downtime.
    """
    started = time.perf_counter()
    limit = batch_size or settings.overdue_scan_batch_size
    now = dt.datetime.now(tz=dt.timezone.utc)
    notified = 0

    with session_scope() as db:
        rows = db.execute(
            select(Task, Project.owner_id)
            .join(Project, Project.id == Task.project_id)
            .where(
                Task.due_date.is_not(None),
                Task.due_date < now,
                Task.status != TaskStatus.done,
                Task.overdue_notified_at.is_(None),
            )
            .order_by(Task.due_date)
            .limit(limit)
            # SKIP LOCKED lets several workers sweep in parallel without
            # either blocking on the other or handling the same row twice.
            .with_for_update(skip_locked=True, of=Task)
        ).all()

        for task, owner_id in rows:
            # An unassigned task still needs to reach a human; the owner is
            # the one accountable for the project.
            recipient_id = task.assignee_id or owner_id
            create_notification(
                db,
                user_id=recipient_id,
                task_id=task.id,
                type_=NotificationType.task_overdue,
                # Keyed on the due date, so rescheduling a task and letting
                # the new deadline lapse notifies again.
                dedupe_key=f"due:{task.due_date.isoformat() if task.due_date else 'none'}",
                payload={
                    "title": task.title,
                    "project_id": str(task.project_id),
                    "due_date": task.due_date.isoformat() if task.due_date else None,
                    "status": task.status.value,
                },
            )
            task.overdue_notified_at = now
            notified += 1

    # No cache invalidation here on purpose: overdue_notified_at is not part
    # of the task representation, so no cached list can have become wrong.
    OVERDUE_SCAN_TASKS.inc(notified)
    _record("scan_overdue_tasks", "success", started)
    if notified:
        logger.info("overdue_scan.completed", extra={"notified": notified})
    return notified

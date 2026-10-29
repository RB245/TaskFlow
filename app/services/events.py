"""Handing work to the background worker.

Two rules this module exists to enforce:

1. **Enqueue only after the transaction commits.** The worker opens its own
   connection, so a job dispatched inside the transaction can start before the
   row is visible - a race that shows up as "sometimes the notification is
   missing" and is miserable to debug. Callers must have committed first.
2. **A broker outage must not fail the write.** The user's task update
   succeeded; refusing it because Redis is unreachable would be the wrong
   trade. Transport errors are swallowed, counted and logged loudly.

Rule 2 has a real cost: a notification dropped this way is gone for good. The
proper fix is a transactional outbox - write the intent in the same
transaction as the task, and have a relay drain it - which is noted as future
work in the README rather than built here.
"""

from __future__ import annotations

import datetime as dt
import uuid

from kombu.exceptions import OperationalError

from app.core.logging import get_logger, get_request_id
from app.core.metrics import JOB_ENQUEUE_FAILURES

logger = get_logger(__name__)


def _headers() -> dict[str, str]:
    request_id = get_request_id()
    return {"request_id": request_id} if request_id else {}


def _send(task: object, name: str, **kwargs: object) -> bool:
    try:
        task.apply_async(kwargs=kwargs, headers=_headers())  # type: ignore[attr-defined]
    except OperationalError as exc:
        # Transport-level only. A failure raised by the job body itself (which
        # happens inline when CELERY_TASK_ALWAYS_EAGER is set in tests) must
        # still propagate.
        JOB_ENQUEUE_FAILURES.labels(task=name).inc()
        logger.error(
            "job.enqueue_failed",
            extra={"task": name, "error": exc.__class__.__name__, "kwargs": kwargs},
        )
        return False
    return True


def task_reassigned(
    *,
    task_id: uuid.UUID,
    assignee_id: uuid.UUID,
    occurred_at: dt.datetime,
    previous_assignee_id: uuid.UUID | None,
) -> bool:
    from app.workers.tasks import notify_task_reassigned

    return _send(
        notify_task_reassigned,
        "taskflow.notify_task_reassigned",
        task_id=str(task_id),
        assignee_id=str(assignee_id),
        occurred_at=occurred_at.isoformat(),
        previous_assignee_id=str(previous_assignee_id) if previous_assignee_id else None,
    )


def task_status_changed(
    *,
    task_id: uuid.UUID,
    recipient_id: uuid.UUID,
    old_status: str,
    new_status: str,
    occurred_at: dt.datetime,
) -> bool:
    from app.workers.tasks import notify_task_status_changed

    return _send(
        notify_task_status_changed,
        "taskflow.notify_task_status_changed",
        task_id=str(task_id),
        recipient_id=str(recipient_id),
        old_status=old_status,
        new_status=new_status,
        occurred_at=occurred_at.isoformat(),
    )

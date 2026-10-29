"""Creating and reading notifications.

"Delivery" is simulated, as the brief allows: a row in ``notifications`` plus
a structured ``notification.delivered`` log line. A real transport would slot
in at :func:`_deliver` without touching anything else.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.core.metrics import NOTIFICATIONS_CREATED, NOTIFICATIONS_SUPPRESSED
from app.models import Notification, NotificationType, User

logger = get_logger(__name__)


def create_notification(
    db: Session,
    *,
    user_id: uuid.UUID,
    task_id: uuid.UUID,
    type_: NotificationType,
    dedupe_key: str,
    payload: dict[str, Any] | None = None,
) -> Notification | None:
    """Insert a notification, or return ``None`` if it already exists.

    ``ON CONFLICT DO NOTHING`` against ``uq_notifications_task_id_type_dedupe_key``
    is what makes the background jobs safely retryable. Checking first and
    then inserting would still race two workers against each other; letting
    the database arbitrate cannot.
    """
    stmt = (
        pg_insert(Notification)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            task_id=task_id,
            type=type_,
            dedupe_key=dedupe_key,
            payload=payload or {},
        )
        .on_conflict_do_nothing(constraint="uq_notifications_task_id_type_dedupe_key")
        .returning(Notification.id)
    )
    created_id = db.execute(stmt).scalar_one_or_none()

    if created_id is None:
        NOTIFICATIONS_SUPPRESSED.labels(type=type_.value).inc()
        logger.debug(
            "notification.suppressed_duplicate",
            extra={"task_id": str(task_id), "type": type_.value, "dedupe_key": dedupe_key},
        )
        return None

    NOTIFICATIONS_CREATED.labels(type=type_.value).inc()
    notification = db.get(Notification, created_id)
    assert notification is not None
    _deliver(notification)
    return notification


def _deliver(notification: Notification) -> None:
    """Simulated delivery. Swap for email/push/webhook here."""
    logger.info(
        "notification.delivered",
        extra={
            "notification_id": str(notification.id),
            "user_id": str(notification.user_id),
            "task_id": str(notification.task_id),
            "type": notification.type.value,
            "channel": "log",
        },
    )


def list_for_user(
    db: Session,
    user: User,
    *,
    page: int,
    page_size: int,
    unread_only: bool = False,
) -> tuple[list[Notification], int]:
    base = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        base = base.where(Notification.read_at.is_(None))

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.scalars(
        base.order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(rows), total


def mark_read(db: Session, user: User, notification_id: uuid.UUID) -> Notification:
    notification = db.scalar(
        select(Notification).where(
            Notification.id == notification_id,
            # Scoped to the caller, so one user cannot mark another's
            # notifications read (or probe which ids exist).
            Notification.user_id == user.id,
        )
    )
    if notification is None:
        raise NotFoundError("Notification not found.")

    if notification.read_at is None:
        notification.read_at = dt.datetime.now(tz=dt.timezone.utc)
        db.commit()
        db.refresh(notification)
    return notification


def unread_count(db: Session, user: User) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user.id, Notification.read_at.is_(None))
        )
        or 0
    )

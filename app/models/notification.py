from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import Timestamps, UUIDPrimaryKey


class NotificationType(str, enum.Enum):
    task_overdue = "task_overdue"
    task_reassigned = "task_reassigned"
    task_status_changed = "task_status_changed"


class Notification(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "notifications"
    __table_args__ = (
        # Idempotency at the database level. A retried Celery task computes the
        # same dedupe_key, so the insert loses the race instead of creating a
        # duplicate - correctness does not depend on the worker behaving.
        UniqueConstraint("task_id", "type", "dedupe_key"),
        Index("ix_notifications_user_created", "user_id", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[NotificationType] = mapped_column(
        Enum(NotificationType, name="notification_type", native_enum=True), nullable=False
    )
    # Distinguishes repeat notifications of the same type for the same task
    # (e.g. one per status transition) while still collapsing retries.
    dedupe_key: Mapped[str] = mapped_column(String(120), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    read_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

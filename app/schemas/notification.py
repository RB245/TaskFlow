from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.notification import NotificationType


class NotificationPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    task_id: uuid.UUID
    type: NotificationType
    payload: dict[str, Any]
    read_at: dt.datetime | None
    created_at: dt.datetime


class UnreadCount(BaseModel):
    unread: int

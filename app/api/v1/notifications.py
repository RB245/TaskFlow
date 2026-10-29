from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession, PaginationDep
from app.schemas.common import Page
from app.schemas.notification import NotificationPublic, UnreadCount
from app.services import notification_service

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=Page[NotificationPublic])
def list_notifications(
    db: DbSession,
    current_user: CurrentUser,
    pagination: PaginationDep,
    unread_only: Annotated[bool, Query()] = False,
) -> Page[NotificationPublic]:
    # Always scoped to the caller - there is no "user_id" filter to abuse.
    rows, total = notification_service.list_for_user(
        db,
        current_user,
        page=pagination.page,
        page_size=pagination.page_size,
        unread_only=unread_only,
    )
    return Page.build(
        [NotificationPublic.model_validate(row) for row in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get("/unread-count", response_model=UnreadCount)
def get_unread_count(db: DbSession, current_user: CurrentUser) -> UnreadCount:
    return UnreadCount(unread=notification_service.unread_count(db, current_user))


@router.post("/{notification_id}/read", response_model=NotificationPublic)
def mark_read(
    notification_id: uuid.UUID, db: DbSession, current_user: CurrentUser
) -> NotificationPublic:
    notification = notification_service.mark_read(db, current_user, notification_id)
    return NotificationPublic.model_validate(notification)

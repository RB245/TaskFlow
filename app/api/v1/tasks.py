from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.common import Page
from app.schemas.task import TaskCreate, TaskFilters, TaskPublic, TaskUpdate
from app.services import task_service

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get(
    "",
    response_model=Page[TaskPublic],
    summary="List tasks with filtering, sorting and pagination (cache-backed)",
)
def list_tasks(
    db: DbSession,
    current_user: CurrentUser,
    filters: Annotated[TaskFilters, Query()],
) -> Page[TaskPublic]:
    # The service returns the same dict shape whether it came from Redis or
    # Postgres, and it is validated here either way - so a cache hit cannot
    # quietly return a different body from a miss.
    payload = task_service.list_tasks(db, current_user, filters, filters.pagination)
    return Page[TaskPublic].model_validate(payload)


@router.post("", response_model=TaskPublic, status_code=status.HTTP_201_CREATED)
def create_task(payload: TaskCreate, db: DbSession, current_user: CurrentUser) -> TaskPublic:
    task = task_service.create_task(db, current_user, payload)
    return TaskPublic.model_validate(task)


@router.get("/{task_id}", response_model=TaskPublic)
def get_task(task_id: uuid.UUID, db: DbSession, current_user: CurrentUser) -> TaskPublic:
    return TaskPublic.model_validate(task_service.get_task(db, current_user, task_id))


@router.patch("/{task_id}", response_model=TaskPublic)
def update_task(
    task_id: uuid.UUID, payload: TaskUpdate, db: DbSession, current_user: CurrentUser
) -> TaskPublic:
    task = task_service.update_task(db, current_user, task_id, payload)
    return TaskPublic.model_validate(task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(task_id: uuid.UUID, db: DbSession, current_user: CurrentUser) -> Response:
    task_service.delete_task(db, current_user, task_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

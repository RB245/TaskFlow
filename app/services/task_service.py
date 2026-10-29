"""Task CRUD, the filtered feed, and the writes that must invalidate it.

The ordering inside every write is deliberate and identical:

    1. authorize
    2. mutate + commit
    3. invalidate the cache for every affected project
    4. enqueue notifications

Steps 3 and 4 are after the commit. Invalidating first leaves a window where
a concurrent reader repopulates the cache from pre-commit state; enqueuing
first lets the worker read the row before it is visible.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, UnprocessableError
from app.core.logging import get_logger
from app.models import Project, Task, TaskStatus, User
from app.schemas.common import Pagination
from app.schemas.task import TaskCreate, TaskFilters, TaskPublic, TaskUpdate
from app.services import authz, cache, events

logger = get_logger(__name__)

_SORT_COLUMNS = {
    "due_date": Task.due_date,
    "created_at": Task.created_at,
    "updated_at": Task.updated_at,
    "title": Task.title,
}


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def list_tasks(
    db: Session, user: User, filters: TaskFilters, pagination: Pagination
) -> dict[str, Any]:
    """The cache-backed feed. Returns a ``Page``-shaped dict.

    A dict rather than a model because that is exactly what is stored in
    Redis; the route re-validates it against ``Page[TaskPublic]`` either way,
    so a hit and a miss cannot diverge in shape.
    """
    scope = _resolve_scope(db, user, filters)
    if not scope:
        return _empty_page(pagination)

    versions = cache.get_project_versions(scope)
    cache_key: str | None = None
    if versions is not None:
        cache_key = cache.build_key(versions, filters.cache_fingerprint())
        cached = cache.get_cached(cache_key)
        if cached is not None:
            return cached

    payload = _query_page(db, scope, filters, pagination)

    if cache_key is not None:
        cache.set_cached(cache_key, payload, is_empty=not payload["items"])
    return payload


def get_task(db: Session, user: User, task_id: uuid.UUID) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        # Same 404 the authz layer produces for a project you cannot see, so
        # existence is not observable either way.
        raise NotFoundError("Task not found.")
    authz.require_project(db, user, task.project_id)
    return task


def _resolve_scope(db: Session, user: User, filters: TaskFilters) -> Sequence[uuid.UUID]:
    if filters.project_id is not None:
        # Raises 404 for a project this user is not a member of, so an
        # unauthorized filter never reaches the query or the cache.
        authz.require_project(db, user, filters.project_id)
        return [filters.project_id]
    return authz.accessible_project_ids(db, user)


def _empty_page(pagination: Pagination) -> dict[str, Any]:
    return {
        "items": [],
        "total": 0,
        "page": pagination.page,
        "page_size": pagination.page_size,
        "pages": 0,
    }


def _apply_filters(stmt: Select, scope: Sequence[uuid.UUID], filters: TaskFilters) -> Select:
    stmt = stmt.where(Task.project_id.in_(scope))

    if filters.status:
        stmt = stmt.where(Task.status.in_(filters.status))
    if filters.assignee_id is not None:
        stmt = stmt.where(Task.assignee_id == filters.assignee_id)
    if filters.unassigned:
        stmt = stmt.where(Task.assignee_id.is_(None))
    if filters.due_from is not None:
        stmt = stmt.where(Task.due_date >= _as_utc(filters.due_from))
    if filters.due_to is not None:
        stmt = stmt.where(Task.due_date <= _as_utc(filters.due_to))
    if filters.q:
        # ILIKE with a leading wildcard cannot use a btree index. Acceptable
        # at this scale; the README notes pg_trgm as the fix.
        stmt = stmt.where(Task.title.ilike(f"%{filters.q.strip()}%"))

    return stmt


def _query_page(
    db: Session, scope: Sequence[uuid.UUID], filters: TaskFilters, pagination: Pagination
) -> dict[str, Any]:
    base = _apply_filters(select(Task), scope, filters)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0

    descending = filters.sort.startswith("-")
    column = _SORT_COLUMNS[filters.sort.lstrip("-")]
    ordering = column.desc() if descending else column.asc()

    rows = db.scalars(
        base.order_by(
            # NULLs last in both directions: a task with no due date should
            # not outrank one that is actually scheduled.
            ordering.nullslast(),
            # Deterministic tiebreaker. Without it, rows that tie on the sort
            # column can appear on two pages or on none.
            Task.id.desc(),
        )
        .offset(pagination.offset)
        .limit(pagination.page_size)
    ).all()

    items = [TaskPublic.model_validate(row).model_dump(mode="json") for row in rows]
    pages = (total + pagination.page_size - 1) // pagination.page_size

    return {
        "items": items,
        "total": total,
        "page": pagination.page,
        "page_size": pagination.page_size,
        "pages": pages,
    }


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def create_task(db: Session, user: User, payload: TaskCreate) -> Task:
    authz.require_project(db, user, payload.project_id)
    _require_assignee_is_member(db, payload.project_id, payload.assignee_id)

    now = dt.datetime.now(tz=dt.timezone.utc)
    task = Task(
        project_id=payload.project_id,
        title=payload.title.strip(),
        description=payload.description,
        status=payload.status,
        assignee_id=payload.assignee_id,
        created_by_id=user.id,
        due_date=_as_utc(payload.due_date),
        completed_at=now if payload.status is TaskStatus.done else None,
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    cache.invalidate_projects([task.project_id])

    # Being handed a task by someone else is, from the recipient's side,
    # indistinguishable from a reassignment - so it gets the same
    # notification. Assigning to yourself notifies nobody.
    if task.assignee_id is not None and task.assignee_id != user.id:
        events.task_reassigned(
            task_id=task.id,
            assignee_id=task.assignee_id,
            occurred_at=task.created_at,
            previous_assignee_id=None,
        )

    logger.info("task.created", extra={"task_id": str(task.id), "project_id": str(task.project_id)})
    return task


def update_task(db: Session, user: User, task_id: uuid.UUID, payload: TaskUpdate) -> Task:
    task = get_task(db, user, task_id)
    data = payload.model_dump(exclude_unset=True)

    previous_project_id = task.project_id
    previous_assignee_id = task.assignee_id
    previous_status = task.status

    target_project_id = data.get("project_id") or task.project_id
    if "project_id" in data and data["project_id"] != task.project_id:
        # Moving a task requires membership of the destination too, otherwise
        # a member of A could push work into project B.
        authz.require_project(db, user, target_project_id)

    if "assignee_id" in data:
        _require_assignee_is_member(db, target_project_id, data["assignee_id"])
    elif "project_id" in data and task.assignee_id is not None:
        # The existing assignee may not be a member of the new project.
        _require_assignee_is_member(db, target_project_id, task.assignee_id)

    now = dt.datetime.now(tz=dt.timezone.utc)

    for field in ("project_id", "title", "description", "assignee_id"):
        if field in data:
            value = data[field]
            setattr(task, field, value.strip() if field == "title" and value else value)

    if "due_date" in data:
        task.due_date = _as_utc(data["due_date"])
        # A rescheduled task becomes eligible for a fresh overdue notice.
        task.overdue_notified_at = None

    if "status" in data and data["status"] is not None and data["status"] != task.status:
        task.status = data["status"]
        task.completed_at = now if task.status is TaskStatus.done else None

    db.commit()
    db.refresh(task)

    # Both sides when a task moves: the old project's cached lists no longer
    # contain it and the new project's now do.
    cache.invalidate_projects({previous_project_id, task.project_id})

    _dispatch_update_events(
        db,
        task,
        actor=user,
        previous_assignee_id=previous_assignee_id,
        previous_status=previous_status,
        now=now,
    )

    logger.info("task.updated", extra={"task_id": str(task.id)})
    return task


def delete_task(db: Session, user: User, task_id: uuid.UUID) -> None:
    task = get_task(db, user, task_id)
    project_id = task.project_id
    db.delete(task)
    db.commit()
    cache.invalidate_projects([project_id])
    logger.info("task.deleted", extra={"task_id": str(task_id)})


def _dispatch_update_events(
    db: Session,
    task: Task,
    *,
    actor: User,
    previous_assignee_id: uuid.UUID | None,
    previous_status: TaskStatus,
    now: dt.datetime,
) -> None:
    new_assignee_id = task.assignee_id
    reassigned = new_assignee_id is not None and new_assignee_id != previous_assignee_id
    # Assigning something to yourself does not need an announcement.
    if reassigned and new_assignee_id is not None and new_assignee_id != actor.id:
        events.task_reassigned(
            task_id=task.id,
            assignee_id=new_assignee_id,
            occurred_at=now,
            previous_assignee_id=previous_assignee_id,
        )

    if task.status != previous_status:
        recipient_id = task.assignee_id or _project_owner_id(db, task.project_id)
        # Nobody needs telling about a change they just made themselves.
        if recipient_id is not None and recipient_id != actor.id:
            events.task_status_changed(
                task_id=task.id,
                recipient_id=recipient_id,
                old_status=previous_status.value,
                new_status=task.status.value,
                occurred_at=now,
            )


def _project_owner_id(db: Session, project_id: uuid.UUID) -> uuid.UUID | None:
    return db.scalar(select(Project.owner_id).where(Project.id == project_id))


def _require_assignee_is_member(
    db: Session, project_id: uuid.UUID, assignee_id: uuid.UUID | None
) -> None:
    if assignee_id is None:
        return
    if not authz.is_project_member(db, assignee_id, project_id):
        # 422 rather than 404: the caller can legitimately see this project,
        # so the problem is the value they sent, not the resource.
        raise UnprocessableError("Assignee must be a member of the project.")


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)

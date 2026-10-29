from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.task import TaskStatus
from app.schemas.common import PaginationQuery

SortField = Literal[
    "due_date",
    "-due_date",
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
    "title",
    "-title",
]


class TaskCreate(BaseModel):
    project_id: uuid.UUID
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20000)
    status: TaskStatus = TaskStatus.todo
    assignee_id: uuid.UUID | None = None
    due_date: dt.datetime | None = None


class TaskUpdate(BaseModel):
    # Every field optional; `exclude_unset` in the service distinguishes
    # "omitted" from "explicitly set to null", which matters for assignee_id
    # (unassigning) and due_date (clearing a deadline).
    project_id: uuid.UUID | None = None
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20000)
    status: TaskStatus | None = None
    assignee_id: uuid.UUID | None = None
    due_date: dt.datetime | None = None


class TaskPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    description: str | None
    status: TaskStatus
    assignee_id: uuid.UUID | None
    created_by_id: uuid.UUID
    due_date: dt.datetime | None
    completed_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime

    @property
    def is_overdue(self) -> bool:  # pragma: no cover - convenience only
        return (
            self.due_date is not None
            and self.status is not TaskStatus.done
            and self.due_date < dt.datetime.now(tz=dt.timezone.utc)
        )


class TaskFilters(PaginationQuery):
    """Query parameters for ``GET /tasks``, pagination included.

    ``extra="forbid"`` so a misspelled filter is a 422 rather than silently
    widening the result set - returning everything because someone typed
    ``?staus=done`` is the kind of bug that reaches production. That strictness
    is also why pagination is inherited rather than injected separately: the
    model validates the entire query string, so it has to know about every
    parameter the endpoint accepts.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: uuid.UUID | None = Field(
        default=None,
        description="Restrict to one project. Omit to search every project you are a member of.",
    )
    status: list[TaskStatus] | None = Field(
        default=None, description="Repeatable. Matches any of the given statuses."
    )
    assignee_id: uuid.UUID | None = None
    unassigned: bool = Field(
        default=False,
        description="Return only tasks with no assignee. Mutually exclusive with assignee_id.",
    )
    due_from: dt.datetime | None = Field(default=None, description="Inclusive lower bound.")
    due_to: dt.datetime | None = Field(default=None, description="Inclusive upper bound.")
    q: str | None = Field(default=None, max_length=200, description="Case-insensitive title match.")
    sort: SortField = "-created_at"

    @model_validator(mode="after")
    def _check(self) -> TaskFilters:
        if self.assignee_id is not None and self.unassigned:
            raise ValueError("Use either assignee_id or unassigned, not both.")
        if self.due_from and self.due_to and self.due_from > self.due_to:
            raise ValueError("due_from must not be after due_to.")
        return self

    def cache_fingerprint(self) -> dict[str, Any]:
        """Normalised form of the filter, for the cache key.

        Everything that can change the result set must appear here - page
        coordinates included. Sorting the status list means
        ``?status=todo&status=done`` and ``?status=done&status=todo`` share an
        entry; normalising the datetimes to UTC means the same instant
        expressed in two timezones does too.
        """
        return {
            "project_id": str(self.project_id) if self.project_id else None,
            "status": sorted(s.value for s in self.status) if self.status else None,
            "assignee_id": str(self.assignee_id) if self.assignee_id else None,
            "unassigned": self.unassigned,
            "due_from": _iso_utc(self.due_from),
            "due_to": _iso_utc(self.due_to),
            "q": self.q.strip().lower() if self.q else None,
            "sort": self.sort,
            "page": self.page,
            "page_size": self.page_size,
        }


def _iso_utc(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    # A naive datetime from the query string is interpreted as UTC, which is
    # also how it is compared against the timestamptz column.
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat()

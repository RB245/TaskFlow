from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

from app.core.config import settings

T = TypeVar("T")


@dataclass(frozen=True)
class Pagination:
    """Page coordinates.

    Lives here rather than in ``api/deps`` so the service layer can take one
    without importing anything HTTP-shaped.
    """

    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class PaginationQuery(BaseModel):
    """Page coordinates as query parameters.

    Endpoints whose filters are a Pydantic query model inherit from this
    rather than taking a separate pagination dependency: a query model with
    ``extra="forbid"`` validates the *whole* query string, so `page` arriving
    from somewhere else would be rejected as an unknown parameter.
    """

    page: int = Field(default=1, ge=1, description="1-indexed page number.")
    page_size: int = Field(default=settings.default_page_size, ge=1, le=settings.max_page_size)

    @property
    def pagination(self) -> Pagination:
        return Pagination(page=self.page, page_size=self.page_size)


class Page(BaseModel, Generic[T]):
    """Offset pagination.

    Offset rather than cursor because the task feed is explicitly sortable and
    filterable and the expected page depth is shallow. The tradeoff (and when
    it would need to become keyset pagination) is written up in the README.
    """

    items: list[T]
    total: int = Field(description="Total rows matching the filter, ignoring pagination.")
    page: int
    page_size: int
    pages: int

    @classmethod
    def build(cls, items: list[T], total: int, page: int, page_size: int) -> Page[T]:
        pages = (total + page_size - 1) // page_size if page_size else 0
        return cls(items=items, total=total, page=page, page_size=page_size, pages=pages)


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class MessageResponse(BaseModel):
    message: str

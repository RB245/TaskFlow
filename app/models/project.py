from __future__ import annotations

import enum
import uuid

from sqlalchemy import Enum, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import Timestamps, UUIDPrimaryKey
from app.models.user import User


class ProjectRole(str, enum.Enum):
    owner = "owner"
    member = "member"


class Project(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    members: Mapped[list[ProjectMember]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="selectin"
    )


class ProjectMember(UUIDPrimaryKey, Timestamps, Base):
    """Explicit membership.

    The brief says a user must not see another user's project. Membership is
    how a project becomes shared at all: it is opt-in and owner-controlled, and
    a non-member is treated as if the project does not exist. Without it, an
    "assignee" could only ever be the owner and reassignment notifications
    would be meaningless.
    """

    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "user_id"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[ProjectRole] = mapped_column(
        Enum(ProjectRole, name="project_role", native_enum=True),
        nullable=False,
        default=ProjectRole.member,
    )

    project: Mapped[Project] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(lazy="joined")

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.project import ProjectRole
from app.schemas.user import UserPublic


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)


class ProjectMemberPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user: UserPublic
    role: ProjectRole


class ProjectPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    owner_id: uuid.UUID
    created_at: dt.datetime
    updated_at: dt.datetime


class ProjectDetail(ProjectPublic):
    members: list[ProjectMemberPublic]


class MemberAdd(BaseModel):
    """Membership is granted by email rather than user id.

    An owner knows a colleague's email; making them discover a user id first
    would mean exposing a user lookup endpoint, which is a user-enumeration
    surface we do not need.
    """

    email: EmailStr
    role: ProjectRole = ProjectRole.member

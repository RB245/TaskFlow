from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status

from app.api.deps import CurrentUser, DbSession, PaginationDep
from app.schemas.common import Page
from app.schemas.project import (
    MemberAdd,
    ProjectCreate,
    ProjectDetail,
    ProjectMemberPublic,
    ProjectPublic,
    ProjectUpdate,
)
from app.services import project_service

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectPublic, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectCreate, db: DbSession, current_user: CurrentUser
) -> ProjectPublic:
    project = project_service.create_project(db, current_user, payload)
    return ProjectPublic.model_validate(project)


@router.get("", response_model=Page[ProjectPublic])
def list_projects(
    db: DbSession, current_user: CurrentUser, pagination: PaginationDep
) -> Page[ProjectPublic]:
    projects, total = project_service.list_projects(
        db, current_user, page=pagination.page, page_size=pagination.page_size
    )
    return Page.build(
        [ProjectPublic.model_validate(p) for p in projects],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get("/{project_id}", response_model=ProjectDetail)
def get_project(project_id: uuid.UUID, db: DbSession, current_user: CurrentUser) -> ProjectDetail:
    project = project_service.get_project_detail(db, current_user, project_id)
    return ProjectDetail.model_validate(project)


@router.patch("/{project_id}", response_model=ProjectPublic)
def update_project(
    project_id: uuid.UUID, payload: ProjectUpdate, db: DbSession, current_user: CurrentUser
) -> ProjectPublic:
    project = project_service.update_project(db, current_user, project_id, payload)
    return ProjectPublic.model_validate(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: uuid.UUID, db: DbSession, current_user: CurrentUser) -> Response:
    project_service.delete_project(db, current_user, project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{project_id}/members",
    response_model=ProjectMemberPublic,
    status_code=status.HTTP_201_CREATED,
)
def add_member(
    project_id: uuid.UUID, payload: MemberAdd, db: DbSession, current_user: CurrentUser
) -> ProjectMemberPublic:
    member = project_service.add_member(db, current_user, project_id, payload)
    return ProjectMemberPublic.model_validate(member)


@router.delete("/{project_id}/members/{member_user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    project_id: uuid.UUID, member_user_id: uuid.UUID, db: DbSession, current_user: CurrentUser
) -> Response:
    project_service.remove_member(db, current_user, project_id, member_user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

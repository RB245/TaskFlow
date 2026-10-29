from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models import Project, ProjectMember, ProjectRole, User
from app.schemas.project import MemberAdd, ProjectCreate, ProjectUpdate
from app.services import authz

logger = get_logger(__name__)


def create_project(db: Session, user: User, payload: ProjectCreate) -> Project:
    project = Project(
        name=payload.name.strip(),
        description=payload.description,
        owner_id=user.id,
    )
    db.add(project)
    db.flush()
    # The owner is a member row like anyone else - see services/authz.py for
    # why the read path must only ever ask one question.
    db.add(ProjectMember(project_id=project.id, user_id=user.id, role=ProjectRole.owner))
    db.commit()
    db.refresh(project)
    logger.info("project.created", extra={"project_id": str(project.id)})
    return project


def list_projects(
    db: Session, user: User, *, page: int, page_size: int
) -> tuple[list[Project], int]:
    base = (
        select(Project)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(ProjectMember.user_id == user.id)
    )
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.scalars(
        base.order_by(Project.created_at.desc(), Project.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(rows), total


def get_project_detail(db: Session, user: User, project_id: uuid.UUID) -> Project:
    authz.require_project(db, user, project_id)
    project = db.scalar(
        select(Project)
        .where(Project.id == project_id)
        .options(selectinload(Project.members).joinedload(ProjectMember.user))
    )
    if project is None:  # pragma: no cover - require_project already covers it
        raise NotFoundError("Project not found.")
    return project


def update_project(
    db: Session, user: User, project_id: uuid.UUID, payload: ProjectUpdate
) -> Project:
    project = authz.require_project(db, user, project_id, require_owner=True)
    # exclude_unset distinguishes "field omitted" from "field set to null";
    # without it a PATCH of only `name` would silently blank the description.
    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"] is not None:
        project.name = data["name"].strip()
    if "description" in data:
        project.description = data["description"]
    db.commit()
    db.refresh(project)
    return project


def delete_project(db: Session, user: User, project_id: uuid.UUID) -> None:
    project = authz.require_project(db, user, project_id, require_owner=True)
    db.delete(project)
    db.commit()
    logger.info("project.deleted", extra={"project_id": str(project_id)})


def add_member(db: Session, user: User, project_id: uuid.UUID, payload: MemberAdd) -> ProjectMember:
    authz.require_project(db, user, project_id, require_owner=True)

    email = payload.email.strip().lower()
    target = db.scalar(select(User).where(User.email == email))
    if target is None or not target.is_active:
        # Same response whether the address is unregistered or inactive, so
        # this endpoint cannot be used to probe who has an account.
        raise NotFoundError("No active user with that email.")

    member = ProjectMember(project_id=project_id, user_id=target.id, role=payload.role)
    db.add(member)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ConflictError("That user is already a member of this project.") from None

    db.refresh(member)
    logger.info(
        "project.member_added",
        extra={"project_id": str(project_id), "member_user_id": str(target.id)},
    )
    return member


def remove_member(
    db: Session, user: User, project_id: uuid.UUID, member_user_id: uuid.UUID
) -> None:
    project = authz.require_project(db, user, project_id, require_owner=True)

    if member_user_id == project.owner_id:
        # Removing the owner would orphan the project: nobody could manage
        # membership afterwards. Deleting the project is the intended action.
        raise BadRequestError("The project owner cannot be removed from the project.")

    membership = authz.get_membership(db, member_user_id, project_id)
    if membership is None:
        raise NotFoundError("That user is not a member of this project.")

    db.delete(membership)
    db.commit()
    logger.info(
        "project.member_removed",
        extra={"project_id": str(project_id), "member_user_id": str(member_user_id)},
    )

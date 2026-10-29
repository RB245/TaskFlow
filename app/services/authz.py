"""The single place access to a project is decided.

Everything that touches a project goes through here - routes, services and
Celery tasks alike - so there is one rule to audit instead of a check copied
into every handler.

Two deliberate choices:

* **404, not 403, for non-members.** Returning 403 confirms that a project id
  exists, which turns the id space into an oracle. A non-member gets exactly
  the response they would get for an id that was never issued.
* **Owner is a member row too.** Creating a project inserts a
  ``ProjectMember`` with ``role=owner``, so membership is the only question
  ever asked on the read path. A second code path for "or you're the owner"
  is precisely where this kind of bug hides.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ForbiddenError, NotFoundError
from app.models import Project, ProjectMember, ProjectRole, User


def get_membership(db: Session, user_id: uuid.UUID, project_id: uuid.UUID) -> ProjectMember | None:
    return db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    )


def require_project(
    db: Session,
    user: User,
    project_id: uuid.UUID,
    *,
    require_owner: bool = False,
) -> Project:
    """Return the project, or raise 404 if this user has no business knowing
    it exists.

    ``require_owner`` is for operations only the owner may perform (rename,
    delete, manage membership). A member who is not the owner gets a 403 here
    rather than a 404, because they already legitimately know the project
    exists - hiding it at that point would be noise, not security.
    """
    membership = get_membership(db, user.id, project_id)
    if membership is None:
        raise NotFoundError("Project not found.")

    project = db.get(Project, project_id)
    if project is None:
        # Membership without a project means the FK cascade failed; treat it
        # as absent rather than trusting the stale row.
        raise NotFoundError("Project not found.")

    if require_owner and membership.role != ProjectRole.owner:
        raise ForbiddenError("Only the project owner can perform this action.")

    return project


def accessible_project_ids(db: Session, user: User) -> Sequence[uuid.UUID]:
    """Every project this user may see. Used to scope unfiltered task queries
    and, importantly, to build the cache key fingerprint."""
    return db.scalars(
        select(ProjectMember.project_id)
        .where(ProjectMember.user_id == user.id)
        .order_by(ProjectMember.project_id)
    ).all()


def is_project_member(db: Session, user_id: uuid.UUID, project_id: uuid.UUID) -> bool:
    return get_membership(db, user_id, project_id) is not None


def project_member_ids(db: Session, project_id: uuid.UUID) -> Sequence[uuid.UUID]:
    return db.scalars(
        select(ProjectMember.user_id).where(ProjectMember.project_id == project_id)
    ).all()

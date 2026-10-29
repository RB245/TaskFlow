"""SQLAlchemy models.

Imported as a package so Alembic's autogenerate sees every table via
``Base.metadata``.
"""

from app.models.user import User  # noqa: I001  (must precede relationship targets)
from app.models.notification import Notification, NotificationType
from app.models.project import Project, ProjectMember, ProjectRole
from app.models.refresh_token import RefreshToken
from app.models.task import Task, TaskStatus

__all__ = [
    "Notification",
    "NotificationType",
    "Project",
    "ProjectMember",
    "ProjectRole",
    "RefreshToken",
    "Task",
    "TaskStatus",
    "User",
]

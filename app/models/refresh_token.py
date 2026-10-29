from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import Timestamps, UUIDPrimaryKey


class RefreshToken(UUIDPrimaryKey, Timestamps, Base):
    """Server-side half of the refresh-token rotation scheme.

    Only the SHA-256 of the token is stored, so a database leak does not hand
    out live sessions. This table is what makes JWT auth revocable.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Set when this token is rotated, so a replayed token can be traced to the
    # chain it belonged to.
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

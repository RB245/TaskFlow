"""Signup, login, refresh-token rotation and logout."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AuthenticationError, ConflictError
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    password_needs_rehash,
    verify_password,
)
from app.core.config import settings
from app.models import RefreshToken, User
from app.schemas.auth import TokenPair
from app.schemas.user import UserCreate

logger = get_logger(__name__)

# A real argon2 hash of a throwaway value. Verifying against it when the email
# is unknown keeps login timing roughly constant, so the endpoint cannot be
# used to enumerate which addresses are registered.
_DUMMY_HASH = hash_password("timing-equalisation-placeholder")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def signup(db: Session, payload: UserCreate) -> User:
    user = User(
        email=normalize_email(payload.email),
        full_name=payload.full_name.strip(),
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Deliberately the same wording a caller would get from any duplicate,
        # and signup is the one place we accept leaking existence: you cannot
        # register an address twice without being told.
        raise ConflictError("An account with that email already exists.") from None
    db.refresh(user)
    logger.info("user.signup", extra={"user_id": str(user.id)})
    return user


def authenticate(db: Session, email: str, password: str) -> User:
    user = db.scalar(select(User).where(User.email == normalize_email(email)))

    if user is None:
        verify_password(password, _DUMMY_HASH)
        raise AuthenticationError("Invalid email or password.")

    if not verify_password(password, user.password_hash):
        raise AuthenticationError("Invalid email or password.")

    if not user.is_active:
        raise AuthenticationError("Account is disabled.")

    # Transparently upgrade the hash if our cost parameters have increased
    # since this password was last set.
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        db.commit()

    return user


def issue_token_pair(db: Session, user: User, *, replaces: RefreshToken | None = None) -> TokenPair:
    now = dt.datetime.now(tz=dt.timezone.utc)
    access_token, expires_in = create_access_token(user.id, now=now)
    raw_refresh, refresh_hash = generate_refresh_token()

    record = RefreshToken(
        user_id=user.id,
        token_hash=refresh_hash,
        expires_at=now + dt.timedelta(seconds=settings.refresh_token_ttl_seconds),
    )
    db.add(record)
    db.flush()

    if replaces is not None:
        replaces.revoked_at = now
        replaces.replaced_by_id = record.id

    db.commit()
    return TokenPair(access_token=access_token, refresh_token=raw_refresh, expires_in=expires_in)


def refresh_token_pair(db: Session, raw_refresh_token: str) -> tuple[User, TokenPair]:
    """Rotate a refresh token.

    Rotation means a refresh token is single-use. Presenting one that was
    already rotated is the signature of a stolen token being replayed, so the
    whole family is revoked and the user is forced to log in again - the
    attacker and the legitimate holder both lose access, which is the correct
    failure direction.
    """
    token_hash = hash_refresh_token(raw_refresh_token)
    record = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    now = dt.datetime.now(tz=dt.timezone.utc)

    if record is None:
        raise AuthenticationError("Invalid refresh token.")

    if record.revoked_at is not None:
        logger.warning(
            "auth.refresh_token_reuse_detected",
            extra={"user_id": str(record.user_id), "token_id": str(record.id)},
        )
        _revoke_all_for_user(db, record.user_id, now)
        raise AuthenticationError("Refresh token has been revoked.")

    if record.expires_at <= now:
        raise AuthenticationError("Refresh token has expired.")

    user = db.get(User, record.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Account is unavailable.")

    tokens = issue_token_pair(db, user, replaces=record)
    return user, tokens


def logout(db: Session, raw_refresh_token: str) -> None:
    """Idempotent: an unknown or already-revoked token is not an error, since
    the caller's intent (this session should not work) is already satisfied."""
    token_hash = hash_refresh_token(raw_refresh_token)
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == token_hash, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=dt.datetime.now(tz=dt.timezone.utc))
    )
    db.commit()


def _revoke_all_for_user(db: Session, user_id: uuid.UUID, now: dt.datetime) -> None:
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    db.commit()

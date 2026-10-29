"""Password hashing and token issuance.

Two different token mechanisms on purpose:

* The **access token** is a short-lived signed JWT. Nothing is looked up to
  validate it, so the hot path costs one signature verification and no
  database round trip. That is the whole reason for choosing JWT here - the
  API, the Celery worker and beat scale independently and share no session
  store.
* The **refresh token** is an opaque random string whose SHA-256 lives in
  Postgres. This is what buys back the thing stateless JWT gives up:
  revocation. Logging out, or detecting a replayed token, can actually kill a
  session.

Only the hash of a refresh token is stored, so a database dump does not hand
an attacker live sessions.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.core.config import settings

_hasher = PasswordHasher(
    time_cost=settings.argon2_time_cost,
    memory_cost=settings.argon2_memory_cost_kib,
    parallelism=settings.argon2_parallelism,
)

ACCESS_TOKEN_TYPE = "access"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False
    return True


def password_needs_rehash(password_hash: str) -> bool:
    """True when the stored hash used weaker parameters than we use now, so
    the cost can be upgraded transparently on the user's next login."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, ValueError):
        return True


def create_access_token(user_id: uuid.UUID, now: dt.datetime | None = None) -> tuple[str, int]:
    """Return ``(token, expires_in_seconds)``."""
    issued_at = now or dt.datetime.now(tz=dt.timezone.utc)
    expires_at = issued_at + dt.timedelta(seconds=settings.access_token_ttl_seconds)
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "type": ACCESS_TOKEN_TYPE,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, settings.access_token_ttl_seconds


def decode_access_token(token: str) -> dict[str, Any]:
    """Raises ``jwt.PyJWTError`` on anything invalid.

    ``algorithms`` is pinned to a single value: accepting a list the attacker
    can influence is how the classic "alg: none" and HS/RS confusion attacks
    get in.
    """
    claims: dict[str, Any] = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        options={"require": ["exp", "sub", "type"]},
    )
    if claims.get("type") != ACCESS_TOKEN_TYPE:
        raise jwt.InvalidTokenError("wrong token type")
    return claims


def generate_refresh_token() -> tuple[str, str]:
    """Return ``(raw_token, sha256_hex)``. Only the hash is persisted."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    # A plain SHA-256 is right here, unlike for passwords: the token is 384
    # bits of CSPRNG output, so there is nothing to brute-force and no reason
    # to pay argon2's cost on every refresh.
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

from __future__ import annotations

import uuid
from typing import Annotated

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.errors import AuthenticationError
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models import User

# auto_error=False so a missing header raises our AuthenticationError with the
# standard error body, rather than FastAPI's own 403 shape.
_bearer = HTTPBearer(auto_error=False, description="Bearer <access_token>")

DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> User:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Missing bearer token.")

    try:
        claims = decode_access_token(credentials.credentials)
    except jwt.ExpiredSignatureError:
        raise AuthenticationError("Access token has expired.", code="token_expired") from None
    except jwt.PyJWTError:
        raise AuthenticationError("Invalid access token.") from None

    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError):
        raise AuthenticationError("Invalid access token.") from None

    # One lookup per request. It is what makes a deactivated account stop
    # working before its access token expires; a pure-JWT read of the claims
    # would keep honouring it for up to the full TTL.
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Account is unavailable.")

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]

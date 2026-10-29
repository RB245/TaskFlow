from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.auth import AuthResponse, LogoutRequest, RefreshRequest, TokenPair
from app.schemas.common import MessageResponse
from app.schemas.user import UserCreate, UserLogin, UserPublic
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def signup(payload: UserCreate, db: DbSession) -> AuthResponse:
    user = auth_service.signup(db, payload)
    tokens = auth_service.issue_token_pair(db, user)
    return AuthResponse(user=UserPublic.model_validate(user), tokens=tokens)


@router.post("/login", response_model=AuthResponse)
def login(payload: UserLogin, db: DbSession) -> AuthResponse:
    user = auth_service.authenticate(db, payload.email, payload.password)
    tokens = auth_service.issue_token_pair(db, user)
    return AuthResponse(user=UserPublic.model_validate(user), tokens=tokens)


@router.post("/refresh", response_model=TokenPair)
def refresh(payload: RefreshRequest, db: DbSession) -> TokenPair:
    _, tokens = auth_service.refresh_token_pair(db, payload.refresh_token)
    return tokens


@router.post("/logout", response_model=MessageResponse)
def logout(payload: LogoutRequest, db: DbSession) -> MessageResponse:
    auth_service.logout(db, payload.refresh_token)
    return MessageResponse(message="Logged out.")


@router.get("/me", response_model=UserPublic)
def me(current_user: CurrentUser) -> UserPublic:
    return UserPublic.model_validate(current_user)

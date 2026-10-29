from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserPublic(BaseModel):
    """The only user shape that is ever serialised outbound.

    There is deliberately no ``password``/``password_hash`` field anywhere on
    an outbound model, so a hash cannot leak by someone adding
    ``model_config = {"from_attributes": True}`` to the wrong class.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    created_at: dt.datetime


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    # Length is the only requirement enforced. Composition rules ("one digit,
    # one symbol") measurably push users toward weaker, more predictable
    # passwords; NIST SP 800-63B advises against them.
    password: str = Field(min_length=12, max_length=128)


class UserLogin(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

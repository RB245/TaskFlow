"""A single error vocabulary for the whole app.

Services raise these; they know nothing about HTTP beyond a status code, and
the handlers in ``main`` turn them into a consistent JSON body. The important
one is :class:`NotFoundError` - the authorization layer raises it instead of
:class:`ForbiddenError` so that project ids are not enumerable (see
``services/authz.py``).
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code = 500
    code = "internal_error"
    message = "Something went wrong."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details or {}
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            body["error"]["details"] = self.details
        return body


class BadRequestError(AppError):
    status_code = 400
    code = "bad_request"
    message = "Invalid request."


class AuthenticationError(AppError):
    status_code = 401
    code = "unauthenticated"
    message = "Authentication required."


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"
    message = "You do not have permission to perform this action."


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    message = "Resource not found."


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    message = "Resource already exists."


class UnprocessableError(AppError):
    status_code = 422
    code = "unprocessable"
    message = "Request could not be processed."

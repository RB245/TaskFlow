"""Structured JSON logging with a request-id that follows work into Celery.

A hand-rolled formatter avoids another dependency and keeps the field names
stable, which matters more than flexibility when something downstream is
parsing these lines.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Any

from app.core.config import settings

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

# Attributes LogRecord always carries; anything else was passed via `extra=`
# and is worth emitting.
_RESERVED = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys() | {"message", "asctime", "taskName"}
)


def set_request_id(value: str | None = None) -> str:
    request_id = value or uuid.uuid4().hex
    _request_id.set(request_id)
    return request_id


def get_request_id() -> str | None:
    return _request_id.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, tz=dt.timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = get_request_id()
        if request_id:
            payload["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())

    # uvicorn installs its own handlers; drop them so everything is JSON.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    # SQLAlchemy's pool chatter is noise at INFO.
    logging.getLogger("sqlalchemy.engine").setLevel("WARNING")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

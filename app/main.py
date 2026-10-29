from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import ops
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger, set_request_id
from app.core.metrics import (
    HTTP_ERRORS,
    HTTP_IN_PROGRESS,
    HTTP_REQUEST_DURATION,
    HTTP_REQUESTS,
    UNHANDLED_EXCEPTIONS,
)

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    logger.info("app.startup", extra={"environment": settings.environment})
    yield
    logger.info("app.shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="Task management API with background notifications and a cached task feed.",
        lifespan=lifespan,
        # Hide the interactive docs in production; the schema is still
        # available to anything that needs it internally.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    _register_middleware(app)
    _register_exception_handlers(app)

    app.include_router(api_router, prefix=settings.api_v1_prefix)
    # Unversioned on purpose - see app/api/ops.py.
    app.include_router(ops.router)
    return app


def _route_template(request: Request) -> str:
    """The matched route's path template, e.g. ``/api/v1/tasks/{task_id}``.

    Never the raw path: using it would mint a new Prometheus time series for
    every task id anyone has ever requested, and that is how a metrics
    endpoint takes down the thing it is supposed to observe.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"


def _register_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Honour an inbound id so a trace survives across services; mint one
        # otherwise. It is attached to every log line via a contextvar and
        # handed back on the response.
        request_id = set_request_id(request.headers.get(REQUEST_ID_HEADER))
        started = time.perf_counter()
        HTTP_IN_PROGRESS.inc()

        try:
            response = await call_next(request)
        finally:
            HTTP_IN_PROGRESS.dec()

        duration = time.perf_counter() - started
        # Read the route *after* the call: it is only attached to the scope
        # once routing has run.
        route = _route_template(request)
        status_code = response.status_code

        # /metrics is excluded from its own counters - otherwise every scrape
        # changes the numbers it is reporting.
        if route != "/metrics":
            HTTP_REQUESTS.labels(method=request.method, route=route, status=str(status_code)).inc()
            HTTP_REQUEST_DURATION.labels(method=request.method, route=route).observe(duration)
            if status_code >= 400:
                HTTP_ERRORS.labels(
                    method=request.method, route=route, status_class=f"{status_code // 100}xx"
                ).inc()

        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "http.request",
            extra={
                "method": request.method,
                "route": route,
                "path": request.url.path,
                "status_code": status_code,
                "duration_ms": round(duration * 1000, 2),
            },
        )
        return response


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.exception("app.error", extra={"code": exc.code})
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Reshaped into the same envelope as every other error so clients have
        # exactly one error format to parse.
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed.",
                    "details": {"errors": _safe_validation_errors(exc)},
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        UNHANDLED_EXCEPTIONS.labels(route=_route_template(request)).inc()
        logger.exception("app.unhandled_exception", extra={"path": request.url.path})
        # Never echo the exception text: it is the easiest way to leak a
        # connection string or a row's contents to a caller.
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "Something went wrong."}},
        )


def _safe_validation_errors(exc: RequestValidationError) -> list[dict[str, object]]:
    """Strip the offending input out of validation errors.

    Pydantic includes the rejected value, which for /auth/login means the
    submitted password would end up in the response body and in logs.
    """
    cleaned: list[dict[str, object]] = []
    for error in exc.errors():
        cleaned.append(
            {
                "loc": [str(part) for part in error.get("loc", [])],
                "type": error.get("type", "invalid"),
                "msg": error.get("msg", "Invalid value."),
            }
        )
    return cleaned


app = create_app()

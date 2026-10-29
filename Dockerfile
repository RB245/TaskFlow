# syntax=docker/dockerfile:1

# Multi-stage so the runtime image carries no compiler toolchain and none of
# the dev dependencies. One image serves all three roles (api, worker, beat);
# only the command differs, which keeps them impossible to skew apart.

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# build-essential is needed to compile argon2-cffi's bindings; it stays in
# this stage and never reaches the final image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install -r requirements.txt


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:$PATH"

# curl is for the compose healthcheck; tini reaps zombies and forwards
# signals, which Celery needs to shut down cleanly on SIGTERM.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 taskflow

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=taskflow:taskflow alembic.ini pyproject.toml ./
COPY --chown=taskflow:taskflow alembic ./alembic
COPY --chown=taskflow:taskflow app ./app
COPY --chown=taskflow:taskflow scripts ./scripts

RUN chmod +x scripts/*.sh

# Never run as root: a container escape should not start from uid 0.
USER taskflow

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

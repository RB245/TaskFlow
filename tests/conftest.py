"""Test configuration.

Tests run against a real Postgres and a real Redis, not fakes. The cache
correctness tests are the reason: they depend on INCR/MGET semantics and on
how Redis behaves when a pipeline fails, and a fake would let a broken
invalidation strategy pass.

    docker compose -f docker-compose.test.yml up -d
    pytest

Environment is set before any app import, because settings, the argon2
hasher and the Celery app are all built at import time.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

_TEST_ENV = {
    "ENVIRONMENT": "test",
    "DATABASE_URL": "postgresql+psycopg://taskflow:taskflow@localhost:55432/taskflow_test",
    # A dedicated Redis database so a stray flush cannot touch dev data.
    "REDIS_URL": "redis://localhost:56379/15",
    "CELERY_BROKER_URL": "redis://localhost:56379/14",
    "CELERY_RESULT_BACKEND": "redis://localhost:56379/14",
    "JWT_SECRET": "test-only-secret-not-used-anywhere-else",
    "CELERY_TASK_ALWAYS_EAGER": "true",
    # Argon2 at production cost would add ~100ms to every signup in the
    # suite. The algorithm under test is the same; only the work factor
    # differs.
    "ARGON2_TIME_COST": "1",
    "ARGON2_MEMORY_COST_KIB": "8192",
    "ARGON2_PARALLELISM": "1",
    "LOG_LEVEL": "WARNING",
}
for _key, _value in _TEST_ENV.items():
    os.environ.setdefault(_key, _value)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core import metrics  # noqa: E402
from app.core.redis import get_redis, reset_redis  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services import cache  # noqa: E402

TABLES = (
    "notifications",
    "tasks",
    "project_members",
    "projects",
    "refresh_tokens",
    "users",
)


@pytest.fixture(scope="session", autouse=True)
def _schema() -> Iterator[None]:
    """Build the schema by running the real migrations.

    create_all would be faster but would stop the migrations from being
    exercised at all - and a migration that does not match the models is a
    production outage, not a test failure.
    """
    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    command.upgrade(config, "head")
    yield


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    """Reset Postgres, Redis and in-process counters between tests."""
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))

    reset_redis()
    get_redis().flushdb()
    cache.reset_degraded()
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


class User:
    """A registered user plus the headers needed to act as them."""

    def __init__(self, client: TestClient, name: str, password: str = "correct-horse-battery"):
        self.email = f"{name}-{uuid.uuid4().hex[:10]}@example.com"
        self.password = password
        response = client.post(
            "/api/v1/auth/signup",
            json={"email": self.email, "full_name": name, "password": password},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        self.id: str = body["user"]["id"]
        self.refresh_token: str = body["tokens"]["refresh_token"]
        self.headers = {"Authorization": f"Bearer {body['tokens']['access_token']}"}


@pytest.fixture
def make_user(client: TestClient):
    def _make(name: str) -> User:
        return User(client, name)

    return _make


@pytest.fixture
def alice(make_user) -> User:
    return make_user("alice")


@pytest.fixture
def bob(make_user) -> User:
    return make_user("bob")


@pytest.fixture
def carol(make_user) -> User:
    """A user who is a member of nothing - the outsider in authz tests."""
    return make_user("carol")


@pytest.fixture
def project(client: TestClient, alice: User) -> str:
    response = client.post(
        "/api/v1/projects", json={"name": "Apollo", "description": "moon"}, headers=alice.headers
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


@pytest.fixture
def shared_project(client: TestClient, alice: User, bob: User, project: str) -> str:
    """Alice's project, with Bob added as a member."""
    response = client.post(
        f"/api/v1/projects/{project}/members",
        json={"email": bob.email},
        headers=alice.headers,
    )
    assert response.status_code == 201, response.text
    return project


def metric(name: str, **labels: str) -> float:
    """Read a counter out of the app's registry."""
    value = metrics.REGISTRY.get_sample_value(name, labels or None)
    return float(value or 0.0)

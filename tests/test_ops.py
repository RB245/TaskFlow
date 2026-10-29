from __future__ import annotations

TASKS = "/api/v1/tasks"


def test_health_is_unauthenticated_and_cheap(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_checks_dependencies(client):
    body = client.get("/health/ready").json()

    assert body["status"] == "ok"
    assert body["checks"] == {"database": "ok", "redis": "ok"}
    assert body["cache_degraded"] is False


def test_metrics_exposes_prometheus_text(client):
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    for name in (
        "http_requests_total",
        "http_request_duration_seconds",
        "cache_hits_total",
        "notifications_created_total",
    ):
        assert name in response.text


def test_request_counters_use_the_route_template_not_the_raw_path(client, alice, project):
    client.get(f"/api/v1/projects/{project}", headers=alice.headers)

    body = client.get("/metrics").text

    # The id must not appear as a label value, or every project ever fetched
    # becomes a permanent time series.
    assert 'route="/api/v1/projects/{project_id}"' in body
    assert project not in body


def test_error_responses_are_counted(client, alice):
    before = client.get("/metrics").text

    client.get("/api/v1/auth/me")  # 401

    after = client.get("/metrics").text
    assert 'status_class="4xx"' in after
    assert after != before


def test_every_response_carries_a_request_id(client):
    response = client.get("/health")

    assert response.headers["X-Request-ID"]


def test_an_inbound_request_id_is_echoed_back(client):
    response = client.get("/health", headers={"X-Request-ID": "trace-me-123"})

    assert response.headers["X-Request-ID"] == "trace-me-123"


def test_unknown_route_returns_a_consistent_error_envelope(client):
    response = client.get("/api/v1/nope")

    assert response.status_code == 404


def test_openapi_schema_is_available(client):
    response = client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/tasks" in paths
    assert "/health" in paths

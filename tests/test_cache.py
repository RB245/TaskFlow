"""Cache correctness.

The brief calls a stale read after a status change a bug, so these are the
tests the implementation is actually accountable to.

A caching test suite has one classic way of being worthless: if the cache
never gets hit, every "is it stale?" assertion passes trivially. So the first
test proves hits are really happening, and the staleness tests assert against
the hit counter as well as the data.
"""

from __future__ import annotations

import datetime as dt

import pytest
from redis.exceptions import RedisError

from app.services import cache
from tests.conftest import metric

TASKS = "/api/v1/tasks"
HITS = "cache_hits_total"
MISSES = "cache_misses_total"
INVALIDATIONS = "cache_invalidations_total"
FAILURES = "cache_invalidation_failures_total"


def hits() -> float:
    return metric(HITS, endpoint="tasks_list")


def misses() -> float:
    return metric(MISSES, endpoint="tasks_list")


@pytest.fixture
def task(client, alice, shared_project):
    response = client.post(
        TASKS,
        json={"project_id": shared_project, "title": "Ship it", "status": "todo"},
        headers=alice.headers,
    )
    assert response.status_code == 201
    return response.json()


# --------------------------------------------------------------------------
# The cache is actually being used
# --------------------------------------------------------------------------


def test_second_identical_read_is_served_from_cache(client, alice, shared_project, task):
    url = f"{TASKS}?project_id={shared_project}&status=todo"
    before_hits, before_misses = hits(), misses()

    first = client.get(url, headers=alice.headers)
    second = client.get(url, headers=alice.headers)

    assert first.json() == second.json()
    assert misses() - before_misses == 1
    assert hits() - before_hits == 1


def test_two_members_share_one_cache_entry(client, alice, bob, shared_project, task):
    url = f"{TASKS}?project_id={shared_project}"
    client.get(url, headers=alice.headers)
    before = hits()

    response = client.get(url, headers=bob.headers)

    # Same question, same answer: keying per-user would only halve the hit
    # rate. Authorization has already run before the lookup.
    assert hits() - before == 1
    assert response.json()["total"] == 1


def test_different_filters_do_not_share_an_entry(client, alice, shared_project, task):
    client.get(f"{TASKS}?project_id={shared_project}&status=todo", headers=alice.headers)
    before = hits()

    other = client.get(f"{TASKS}?project_id={shared_project}&status=done", headers=alice.headers)

    assert hits() - before == 0
    assert other.json()["total"] == 0


def test_pagination_is_part_of_the_key(client, alice, shared_project, task):
    client.get(f"{TASKS}?project_id={shared_project}&page=1&page_size=1", headers=alice.headers)
    before = hits()

    client.get(f"{TASKS}?project_id={shared_project}&page=2&page_size=1", headers=alice.headers)

    assert hits() - before == 0


def test_equivalent_filters_normalise_to_the_same_entry(client, alice, shared_project, task):
    client.get(
        f"{TASKS}?project_id={shared_project}&status=todo&status=done", headers=alice.headers
    )
    before = hits()

    # Same set of statuses, different order in the query string.
    client.get(
        f"{TASKS}?project_id={shared_project}&status=done&status=todo", headers=alice.headers
    )

    assert hits() - before == 1


# --------------------------------------------------------------------------
# No stale reads after a write
# --------------------------------------------------------------------------


def test_status_change_is_visible_on_the_very_next_read(client, alice, shared_project, task):
    todo_url = f"{TASKS}?project_id={shared_project}&status=todo"
    done_url = f"{TASKS}?project_id={shared_project}&status=done"

    # Warm both views so a stale implementation has something to serve.
    assert client.get(todo_url, headers=alice.headers).json()["total"] == 1
    assert client.get(done_url, headers=alice.headers).json()["total"] == 0
    before_invalidations = metric(INVALIDATIONS)

    client.patch(f"{TASKS}/{task['id']}", json={"status": "done"}, headers=alice.headers)

    assert metric(INVALIDATIONS) - before_invalidations == 1
    assert client.get(todo_url, headers=alice.headers).json()["total"] == 0
    assert client.get(done_url, headers=alice.headers).json()["total"] == 1
    assert metric(FAILURES) == 0


def test_creating_a_task_invalidates_the_list(client, alice, shared_project, task):
    url = f"{TASKS}?project_id={shared_project}"
    assert client.get(url, headers=alice.headers).json()["total"] == 1

    client.post(TASKS, json={"project_id": shared_project, "title": "new"}, headers=alice.headers)

    assert client.get(url, headers=alice.headers).json()["total"] == 2


def test_deleting_a_task_invalidates_the_list(client, alice, shared_project, task):
    url = f"{TASKS}?project_id={shared_project}"
    client.get(url, headers=alice.headers)

    client.delete(f"{TASKS}/{task['id']}", headers=alice.headers)

    assert client.get(url, headers=alice.headers).json()["total"] == 0


def test_reassignment_is_visible_in_both_assignee_views(client, alice, bob, shared_project, task):
    alice_url = f"{TASKS}?project_id={shared_project}&assignee_id={alice.id}"
    bob_url = f"{TASKS}?project_id={shared_project}&assignee_id={bob.id}"
    client.patch(f"{TASKS}/{task['id']}", json={"assignee_id": alice.id}, headers=alice.headers)

    assert client.get(alice_url, headers=alice.headers).json()["total"] == 1
    assert client.get(bob_url, headers=alice.headers).json()["total"] == 0

    client.patch(f"{TASKS}/{task['id']}", json={"assignee_id": bob.id}, headers=alice.headers)

    assert client.get(alice_url, headers=alice.headers).json()["total"] == 0
    assert client.get(bob_url, headers=alice.headers).json()["total"] == 1


def test_moving_a_task_invalidates_both_projects(client, alice, shared_project):
    second = client.post("/api/v1/projects", json={"name": "Gemini"}, headers=alice.headers).json()[
        "id"
    ]
    moving = client.post(
        TASKS, json={"project_id": shared_project, "title": "movable"}, headers=alice.headers
    ).json()["id"]

    source_url = f"{TASKS}?project_id={shared_project}"
    target_url = f"{TASKS}?project_id={second}"
    assert client.get(source_url, headers=alice.headers).json()["total"] == 1
    assert client.get(target_url, headers=alice.headers).json()["total"] == 0

    client.patch(f"{TASKS}/{moving}", json={"project_id": second}, headers=alice.headers)

    # Both sides must be invalidated, not just the destination.
    assert client.get(source_url, headers=alice.headers).json()["total"] == 0
    assert client.get(target_url, headers=alice.headers).json()["total"] == 1


def test_unscoped_listing_updates_when_any_member_project_changes(
    client, alice, bob, shared_project
):
    assert client.get(TASKS, headers=bob.headers).json()["total"] == 0

    client.post(TASKS, json={"project_id": shared_project, "title": "new"}, headers=alice.headers)

    # Bob's cross-project feed folds the same project version into its key,
    # so Alice's write invalidates it too.
    assert client.get(TASKS, headers=bob.headers).json()["total"] == 1


def test_empty_results_are_cached(client, alice, shared_project):
    url = f"{TASKS}?project_id={shared_project}&q=nothing-matches-this"
    client.get(url, headers=alice.headers)
    before = hits()

    assert client.get(url, headers=alice.headers).json()["total"] == 0
    # A filter that matches nothing is exactly what a client retries in a
    # loop; it should not reach Postgres every time.
    assert hits() - before == 1


def test_a_new_member_sees_the_project_without_a_write_to_invalidate(
    client, alice, bob, project, task=None
):
    client.post(TASKS, json={"project_id": project, "title": "work"}, headers=alice.headers)
    assert client.get(TASKS, headers=bob.headers).json()["total"] == 0

    client.post(
        f"/api/v1/projects/{project}/members", json={"email": bob.email}, headers=alice.headers
    )

    # Membership changes Bob's *scope*, which is part of the key, so his
    # previous empty entry is simply a different key - no invalidation needed.
    assert client.get(TASKS, headers=bob.headers).json()["total"] == 1


# --------------------------------------------------------------------------
# Degradation: wrong answers are worse than slow ones
# --------------------------------------------------------------------------


class _BrokenPipelineRedis:
    """Delegates everything to the real client except pipeline(), which is
    how invalidation is issued."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def pipeline(self, *args, **kwargs):
        raise RedisError("simulated outage")


def test_failed_invalidation_degrades_instead_of_serving_stale(
    client, alice, shared_project, task, monkeypatch
):
    url = f"{TASKS}?project_id={shared_project}&status=todo"
    assert client.get(url, headers=alice.headers).json()["total"] == 1

    real = cache.get_redis()
    monkeypatch.setattr(cache, "get_redis", lambda: _BrokenPipelineRedis(real))
    before_failures = metric(FAILURES)

    # The write succeeds; only its invalidation fails.
    assert (
        client.patch(
            f"{TASKS}/{task['id']}", json={"status": "done"}, headers=alice.headers
        ).status_code
        == 200
    )

    assert metric(FAILURES) - before_failures == 1
    assert cache.is_degraded()

    monkeypatch.undo()

    # The stale entry is still physically in Redis under the un-bumped
    # version. The degraded window is what stops it being served.
    assert client.get(url, headers=alice.headers).json()["total"] == 0
    assert (
        client.get(
            f"{TASKS}?project_id={shared_project}&status=done", headers=alice.headers
        ).json()["total"]
        == 1
    )


class _DeadRedis:
    def __getattr__(self, name):
        def _raise(*args, **kwargs):
            raise RedisError("simulated outage")

        return _raise


def test_reads_still_work_when_redis_is_entirely_down(
    client, alice, shared_project, task, monkeypatch
):
    monkeypatch.setattr(cache, "get_redis", lambda: _DeadRedis())

    response = client.get(f"{TASKS}?project_id={shared_project}", headers=alice.headers)

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert cache.is_degraded()


def test_writes_still_work_when_redis_is_entirely_down(
    client, alice, shared_project, task, monkeypatch
):
    monkeypatch.setattr(cache, "get_redis", lambda: _DeadRedis())

    response = client.patch(f"{TASKS}/{task['id']}", json={"status": "done"}, headers=alice.headers)

    assert response.status_code == 200
    assert response.json()["status"] == "done"


def test_readiness_reports_the_degraded_cache(client, alice, shared_project, monkeypatch):
    monkeypatch.setattr(cache, "get_redis", lambda: _DeadRedis())
    client.get(f"{TASKS}?project_id={shared_project}", headers=alice.headers)
    monkeypatch.undo()

    body = client.get("/health/ready").json()

    # Degraded, but still ready: the service can serve, just not quickly.
    assert body["cache_degraded"] is True
    assert body["status"] == "ok"


def test_corrupt_cache_entry_is_treated_as_a_miss(client, alice, shared_project, task):
    url = f"{TASKS}?project_id={shared_project}"
    client.get(url, headers=alice.headers)

    redis = cache.get_redis()
    for key in redis.scan_iter(match="tf:*:tasks:*"):
        redis.set(key, b"{not json", ex=60)

    response = client.get(url, headers=alice.headers)

    assert response.status_code == 200
    assert response.json()["total"] == 1


def test_due_date_edit_invalidates_a_date_filtered_view(client, alice, shared_project):
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
    later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=10)).isoformat()
    created = client.post(
        TASKS,
        json={"project_id": shared_project, "title": "dated", "due_date": soon},
        headers=alice.headers,
    ).json()

    from urllib.parse import quote

    window = (
        f"{TASKS}?project_id={shared_project}"
        f"&due_to={quote((dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=5)).isoformat())}"
    )
    assert client.get(window, headers=alice.headers).json()["total"] == 1

    client.patch(f"{TASKS}/{created['id']}", json={"due_date": later}, headers=alice.headers)

    assert client.get(window, headers=alice.headers).json()["total"] == 0

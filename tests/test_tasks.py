from __future__ import annotations

import datetime as dt
from urllib.parse import quote

import pytest

TASKS = "/api/v1/tasks"


def iso(delta: dt.timedelta) -> str:
    return (dt.datetime.now(dt.timezone.utc) + delta).isoformat()


def q(value: str) -> str:
    """URL-encode a datetime so its ``+00:00`` offset is not read as a space."""
    return quote(value)


@pytest.fixture
def seeded(client, alice, bob, shared_project):
    """Three tasks with distinct statuses, assignees and due dates."""

    def create(**overrides):
        payload = {"project_id": shared_project, "title": "task"}
        payload.update(overrides)
        response = client.post(TASKS, json=payload, headers=alice.headers)
        assert response.status_code == 201, response.text
        return response.json()

    return {
        "overdue": create(
            title="Alpha overdue", assignee_id=bob.id, due_date=iso(dt.timedelta(hours=-2))
        ),
        "soon": create(
            title="Bravo soon", assignee_id=alice.id, due_date=iso(dt.timedelta(days=2))
        ),
        "floating": create(title="Charlie unassigned"),
    }


def test_create_defaults_to_todo(client, alice, project):
    response = client.post(TASKS, json={"project_id": project, "title": "x"}, headers=alice.headers)

    assert response.status_code == 201
    assert response.json()["status"] == "todo"
    assert response.json()["completed_at"] is None
    assert response.json()["created_by_id"] == alice.id


def test_marking_done_stamps_completed_at_and_clearing_it_unstamps(client, alice, project):
    task = client.post(
        TASKS, json={"project_id": project, "title": "x"}, headers=alice.headers
    ).json()["id"]

    done = client.patch(f"{TASKS}/{task}", json={"status": "done"}, headers=alice.headers).json()
    assert done["completed_at"] is not None

    reopened = client.patch(
        f"{TASKS}/{task}", json={"status": "in_progress"}, headers=alice.headers
    ).json()
    assert reopened["completed_at"] is None


def test_assignee_can_be_cleared(client, alice, bob, shared_project):
    task = client.post(
        TASKS,
        json={"project_id": shared_project, "title": "x", "assignee_id": bob.id},
        headers=alice.headers,
    ).json()["id"]

    updated = client.patch(f"{TASKS}/{task}", json={"assignee_id": None}, headers=alice.headers)

    assert updated.json()["assignee_id"] is None


def test_filter_by_status(client, alice, shared_project, seeded):
    client.patch(f"{TASKS}/{seeded['soon']['id']}", json={"status": "done"}, headers=alice.headers)

    todo = client.get(f"{TASKS}?project_id={shared_project}&status=todo", headers=alice.headers)
    done = client.get(f"{TASKS}?project_id={shared_project}&status=done", headers=alice.headers)
    both = client.get(
        f"{TASKS}?project_id={shared_project}&status=todo&status=done", headers=alice.headers
    )

    assert todo.json()["total"] == 2
    assert done.json()["total"] == 1
    assert both.json()["total"] == 3


def test_filter_by_assignee_and_unassigned(client, alice, bob, shared_project, seeded):
    by_bob = client.get(
        f"{TASKS}?project_id={shared_project}&assignee_id={bob.id}", headers=alice.headers
    )
    unassigned = client.get(
        f"{TASKS}?project_id={shared_project}&unassigned=true", headers=alice.headers
    )

    assert by_bob.json()["total"] == 1
    assert by_bob.json()["items"][0]["title"] == "Alpha overdue"
    assert unassigned.json()["total"] == 1
    assert unassigned.json()["items"][0]["title"] == "Charlie unassigned"


def test_assignee_and_unassigned_are_mutually_exclusive(client, alice, bob, shared_project):
    response = client.get(
        f"{TASKS}?project_id={shared_project}&assignee_id={bob.id}&unassigned=true",
        headers=alice.headers,
    )

    assert response.status_code == 422


def test_filter_by_due_date_range(client, alice, shared_project, seeded):
    future = client.get(
        f"{TASKS}?project_id={shared_project}&due_from={q(iso(dt.timedelta(days=1)))}",
        headers=alice.headers,
    )
    past = client.get(
        f"{TASKS}?project_id={shared_project}&due_to={q(iso(dt.timedelta(0)))}",
        headers=alice.headers,
    )

    assert [i["title"] for i in future.json()["items"]] == ["Bravo soon"]
    assert [i["title"] for i in past.json()["items"]] == ["Alpha overdue"]
    # The undated task appears in neither range.
    assert future.json()["total"] + past.json()["total"] == 2


def test_inverted_due_range_is_rejected(client, alice, shared_project):
    response = client.get(
        f"{TASKS}?due_from={q(iso(dt.timedelta(days=5)))}&due_to={q(iso(dt.timedelta(days=1)))}",
        headers=alice.headers,
    )

    assert response.status_code == 422


def test_title_search_is_case_insensitive(client, alice, shared_project, seeded):
    response = client.get(f"{TASKS}?project_id={shared_project}&q=BRAVO", headers=alice.headers)

    assert [i["title"] for i in response.json()["items"]] == ["Bravo soon"]


def test_misspelled_filter_is_rejected_rather_than_ignored(client, alice):
    # Silently returning everything because someone typed ?staus=done is the
    # failure mode this guards against.
    assert client.get(f"{TASKS}?staus=done", headers=alice.headers).status_code == 422


def test_sorting_and_null_handling(client, alice, shared_project, seeded):
    ascending = client.get(
        f"{TASKS}?project_id={shared_project}&sort=due_date", headers=alice.headers
    ).json()

    titles = [i["title"] for i in ascending["items"]]
    assert titles[:2] == ["Alpha overdue", "Bravo soon"]
    # No due date sorts last in either direction, rather than outranking a
    # task that is actually scheduled.
    assert titles[-1] == "Charlie unassigned"

    descending = client.get(
        f"{TASKS}?project_id={shared_project}&sort=-due_date", headers=alice.headers
    ).json()
    assert [i["title"] for i in descending["items"]][-1] == "Charlie unassigned"


def test_pagination_covers_every_row_exactly_once(client, alice, shared_project, seeded):
    seen = []
    for page in (1, 2, 3):
        body = client.get(
            f"{TASKS}?project_id={shared_project}&page={page}&page_size=1&sort=title",
            headers=alice.headers,
        ).json()
        assert body["total"] == 3
        assert body["pages"] == 3
        seen.extend(item["id"] for item in body["items"])

    assert len(seen) == len(set(seen)) == 3


def test_unscoped_listing_spans_every_project_you_belong_to(client, alice, bob, shared_project):
    own = client.post("/api/v1/projects", json={"name": "Bobs own"}, headers=bob.headers).json()[
        "id"
    ]
    client.post(
        TASKS, json={"project_id": shared_project, "title": "shared"}, headers=alice.headers
    )
    client.post(TASKS, json={"project_id": own, "title": "private"}, headers=bob.headers)

    assert client.get(TASKS, headers=bob.headers).json()["total"] == 2
    assert client.get(TASKS, headers=alice.headers).json()["total"] == 1


def test_moving_a_task_between_projects(client, alice, project, shared_project):
    second = client.post("/api/v1/projects", json={"name": "Gemini"}, headers=alice.headers).json()[
        "id"
    ]
    task = client.post(
        TASKS, json={"project_id": shared_project, "title": "movable"}, headers=alice.headers
    ).json()["id"]

    moved = client.patch(f"{TASKS}/{task}", json={"project_id": second}, headers=alice.headers)

    assert moved.status_code == 200
    assert client.get(f"{TASKS}?project_id={second}", headers=alice.headers).json()["total"] == 1
    assert (
        client.get(f"{TASKS}?project_id={shared_project}", headers=alice.headers).json()["total"]
        == 0
    )


def test_delete_removes_the_task(client, alice, project):
    task = client.post(
        TASKS, json={"project_id": project, "title": "x"}, headers=alice.headers
    ).json()["id"]

    assert client.delete(f"{TASKS}/{task}", headers=alice.headers).status_code == 204
    assert client.get(f"{TASKS}/{task}", headers=alice.headers).status_code == 404

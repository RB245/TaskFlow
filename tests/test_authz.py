"""Authorization boundaries.

One user must never reach another user's data. These tests are written from
the attacker's side: Carol is a member of nothing, and every request she can
construct should fail.
"""

from __future__ import annotations

import uuid

import pytest


def call(client, method: str, path: str, headers: dict, body: dict | None = None):
    """httpx's get/delete take no json= argument, so it has to be omitted
    rather than passed as None."""
    kwargs = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    return getattr(client, method)(path, **kwargs)


@pytest.fixture
def task(client, alice, project) -> str:
    response = client.post(
        "/api/v1/tasks", json={"project_id": project, "title": "Secret work"}, headers=alice.headers
    )
    assert response.status_code == 201
    return str(response.json()["id"])


# --------------------------------------------------------------------------
# A non-member sees nothing, and cannot tell the resource exists
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path_template", "body"),
    [
        ("get", "/api/v1/projects/{project}", None),
        ("patch", "/api/v1/projects/{project}", {"name": "hijacked"}),
        ("delete", "/api/v1/projects/{project}", None),
        ("post", "/api/v1/projects/{project}/members", {"email": "x@example.com"}),
    ],
)
def test_non_member_cannot_touch_a_project(client, carol, project, method, path_template, body):
    response = call(client, method, path_template.format(project=project), carol.headers, body)

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("method", "body"),
    [("get", None), ("patch", {"status": "done"}), ("delete", None)],
)
def test_non_member_cannot_touch_a_task(client, carol, task, method, body):
    response = call(client, method, f"/api/v1/tasks/{task}", carol.headers, body)

    assert response.status_code == 404


def test_404_is_identical_for_hidden_and_nonexistent_resources(client, carol, project):
    hidden = client.get(f"/api/v1/projects/{project}", headers=carol.headers)
    missing = client.get(f"/api/v1/projects/{uuid.uuid4()}", headers=carol.headers)

    # Same status *and* same body: a 403 here would confirm the id exists and
    # turn the id space into an oracle.
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()


def test_non_member_filtering_by_a_foreign_project_is_rejected(client, carol, project):
    response = client.get(f"/api/v1/tasks?project_id={project}", headers=carol.headers)

    assert response.status_code == 404


def test_unscoped_listing_only_returns_your_own_projects(client, carol, task):
    assert client.get("/api/v1/tasks", headers=carol.headers).json()["total"] == 0
    assert client.get("/api/v1/projects", headers=carol.headers).json()["total"] == 0


def test_non_member_cannot_create_a_task_in_a_foreign_project(client, carol, project):
    response = client.post(
        "/api/v1/tasks", json={"project_id": project, "title": "intrusion"}, headers=carol.headers
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------
# A member is not an owner
# --------------------------------------------------------------------------


def test_member_can_read_and_work_but_not_administer(client, bob, shared_project):
    assert client.get(f"/api/v1/projects/{shared_project}", headers=bob.headers).status_code == 200

    created = client.post(
        "/api/v1/tasks",
        json={"project_id": shared_project, "title": "member task"},
        headers=bob.headers,
    )
    assert created.status_code == 201

    # Owner-only operations. 403 rather than 404 here is deliberate: Bob
    # already legitimately knows the project exists.
    assert (
        client.patch(
            f"/api/v1/projects/{shared_project}", json={"name": "renamed"}, headers=bob.headers
        ).status_code
        == 403
    )
    assert (
        client.delete(f"/api/v1/projects/{shared_project}", headers=bob.headers).status_code == 403
    )


def test_owner_cannot_be_removed_from_their_own_project(client, alice, shared_project):
    response = client.delete(
        f"/api/v1/projects/{shared_project}/members/{alice.id}", headers=alice.headers
    )

    assert response.status_code == 400


def test_removing_a_member_revokes_access_immediately(client, alice, bob, shared_project):
    assert client.get(f"/api/v1/projects/{shared_project}", headers=bob.headers).status_code == 200

    client.delete(f"/api/v1/projects/{shared_project}/members/{bob.id}", headers=alice.headers)

    assert client.get(f"/api/v1/projects/{shared_project}", headers=bob.headers).status_code == 404
    assert client.get("/api/v1/tasks", headers=bob.headers).json()["total"] == 0


# --------------------------------------------------------------------------
# Assignment cannot be used to leak a task to an outsider
# --------------------------------------------------------------------------


def test_task_cannot_be_assigned_to_a_non_member(client, alice, project, carol):
    response = client.post(
        "/api/v1/tasks",
        json={"project_id": project, "title": "x", "assignee_id": carol.id},
        headers=alice.headers,
    )

    # 422, not 404: Alice may see this project, so the problem is the value
    # she sent rather than the resource she addressed.
    assert response.status_code == 422


def test_task_cannot_be_reassigned_to_a_non_member(client, alice, project, carol):
    task = client.post(
        "/api/v1/tasks", json={"project_id": project, "title": "x"}, headers=alice.headers
    ).json()["id"]

    response = client.patch(
        f"/api/v1/tasks/{task}", json={"assignee_id": carol.id}, headers=alice.headers
    )

    assert response.status_code == 422


def test_task_cannot_be_moved_into_a_project_you_are_not_in(client, alice, bob, shared_project):
    bobs_own = client.post(
        "/api/v1/projects", json={"name": "Bobs private"}, headers=bob.headers
    ).json()["id"]
    task = client.post(
        "/api/v1/tasks", json={"project_id": shared_project, "title": "x"}, headers=alice.headers
    ).json()["id"]

    response = client.patch(
        f"/api/v1/tasks/{task}", json={"project_id": bobs_own}, headers=alice.headers
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------
# Notifications are private
# --------------------------------------------------------------------------


def test_one_user_cannot_read_or_mark_anothers_notifications(client, alice, bob, shared_project):
    client.post(
        "/api/v1/tasks",
        json={"project_id": shared_project, "title": "for bob", "assignee_id": bob.id},
        headers=alice.headers,
    )

    bobs = client.get("/api/v1/notifications", headers=bob.headers).json()
    assert bobs["total"] == 1
    notification_id = bobs["items"][0]["id"]

    assert client.get("/api/v1/notifications", headers=alice.headers).json()["total"] == 0
    assert (
        client.post(
            f"/api/v1/notifications/{notification_id}/read", headers=alice.headers
        ).status_code
        == 404
    )

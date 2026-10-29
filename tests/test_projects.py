from __future__ import annotations

PROJECTS = "/api/v1/projects"


def test_create_and_fetch_a_project(client, alice):
    created = client.post(
        PROJECTS, json={"name": "  Apollo  ", "description": "moon"}, headers=alice.headers
    )

    assert created.status_code == 201
    assert created.json()["name"] == "Apollo"  # whitespace trimmed
    assert created.json()["owner_id"] == alice.id

    fetched = client.get(f"{PROJECTS}/{created.json()['id']}", headers=alice.headers)
    assert fetched.status_code == 200
    # The creator is a member row, not a special case in the read path.
    assert [m["role"] for m in fetched.json()["members"]] == ["owner"]


def test_patch_leaves_omitted_fields_alone(client, alice, project):
    response = client.patch(
        f"{PROJECTS}/{project}", json={"name": "Apollo 2"}, headers=alice.headers
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Apollo 2"
    assert response.json()["description"] == "moon"


def test_patch_can_explicitly_null_a_field(client, alice, project):
    response = client.patch(
        f"{PROJECTS}/{project}", json={"description": None}, headers=alice.headers
    )

    assert response.json()["description"] is None


def test_delete_removes_the_project_and_its_tasks(client, alice, project):
    client.post("/api/v1/tasks", json={"project_id": project, "title": "x"}, headers=alice.headers)

    assert client.delete(f"{PROJECTS}/{project}", headers=alice.headers).status_code == 204
    assert client.get(f"{PROJECTS}/{project}", headers=alice.headers).status_code == 404
    assert client.get("/api/v1/tasks", headers=alice.headers).json()["total"] == 0


def test_listing_is_paginated(client, alice):
    for index in range(5):
        client.post(PROJECTS, json={"name": f"P{index}"}, headers=alice.headers)

    page = client.get(f"{PROJECTS}?page=2&page_size=2", headers=alice.headers).json()

    assert page["total"] == 5
    assert page["pages"] == 3
    assert page["page"] == 2
    assert len(page["items"]) == 2


def test_page_size_above_the_cap_is_rejected(client, alice):
    # Rejected rather than silently clamped - a client that thinks it asked
    # for 5000 rows and got 100 will paginate wrongly.
    assert client.get(f"{PROJECTS}?page_size=5000", headers=alice.headers).status_code == 422


def test_adding_a_member_by_email(client, alice, bob, project):
    response = client.post(
        f"{PROJECTS}/{project}/members", json={"email": bob.email.upper()}, headers=alice.headers
    )

    assert response.status_code == 201
    assert response.json()["role"] == "member"
    assert response.json()["user"]["email"] == bob.email
    assert "password" not in response.text


def test_adding_the_same_member_twice_conflicts(client, alice, bob, shared_project):
    response = client.post(
        f"{PROJECTS}/{shared_project}/members", json={"email": bob.email}, headers=alice.headers
    )

    assert response.status_code == 409


def test_adding_an_unregistered_email_is_a_404(client, alice, project):
    response = client.post(
        f"{PROJECTS}/{project}/members", json={"email": "ghost@example.com"}, headers=alice.headers
    )

    assert response.status_code == 404


def test_member_sees_the_project_in_their_own_listing(client, bob, shared_project):
    listing = client.get(PROJECTS, headers=bob.headers).json()

    assert listing["total"] == 1
    assert listing["items"][0]["id"] == shared_project

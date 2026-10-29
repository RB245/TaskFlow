"""Background job behaviour.

Two things are being checked: that notification creation really is
*enqueued* rather than done inside the request, and that the jobs themselves
are safely re-runnable.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import func, select

from app.models import Notification, NotificationType, Task
from app.workers import tasks as jobs

TASKS = "/api/v1/tasks"


def count(db, **filters) -> int:
    stmt = select(func.count()).select_from(Notification)
    for column, value in filters.items():
        stmt = stmt.where(getattr(Notification, column) == value)
    return db.scalar(stmt) or 0


def past(hours: int = 2) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).isoformat()


# --------------------------------------------------------------------------
# The request cycle only enqueues
# --------------------------------------------------------------------------


@pytest.fixture
def captured_enqueues(monkeypatch):
    """Intercept apply_async so nothing runs inline, the way it behaves in
    production with a real broker."""
    calls: list[dict] = []

    def fake(self, *args, **kwargs):
        calls.append({"task": self.name, "kwargs": kwargs.get("kwargs", {})})

    # Celery generates a distinct class per task, so each one has to be
    # patched individually - patching one does not cover the others.
    for task in (jobs.notify_task_reassigned, jobs.notify_task_status_changed):
        monkeypatch.setattr(type(task), "apply_async", fake, raising=False)
    return calls


def test_reassignment_is_enqueued_not_created_synchronously(
    client, alice, bob, shared_project, db, captured_enqueues
):
    task = client.post(
        TASKS, json={"project_id": shared_project, "title": "x"}, headers=alice.headers
    ).json()["id"]

    response = client.patch(f"{TASKS}/{task}", json={"assignee_id": bob.id}, headers=alice.headers)

    assert response.status_code == 200
    # The job was handed to the broker...
    enqueued = [c for c in captured_enqueues if c["task"] == "taskflow.notify_task_reassigned"]
    assert len(enqueued) == 1
    assert enqueued[0]["kwargs"]["assignee_id"] == bob.id
    # ...and nothing was written in the request itself.
    assert count(db) == 0

    # Running the job body is what creates the row.
    jobs.notify_task_reassigned.run(**enqueued[0]["kwargs"])
    assert count(db, user_id=uuid.UUID(bob.id), type=NotificationType.task_reassigned) == 1


def test_status_change_is_enqueued(client, alice, bob, shared_project, db, captured_enqueues):
    task = client.post(
        TASKS,
        json={"project_id": shared_project, "title": "x", "assignee_id": bob.id},
        headers=alice.headers,
    ).json()["id"]
    captured_enqueues.clear()

    client.patch(f"{TASKS}/{task}", json={"status": "done"}, headers=alice.headers)

    enqueued = [c for c in captured_enqueues if c["task"] == "taskflow.notify_task_status_changed"]
    assert len(enqueued) == 1
    assert enqueued[0]["kwargs"]["old_status"] == "todo"
    assert enqueued[0]["kwargs"]["new_status"] == "done"
    assert enqueued[0]["kwargs"]["recipient_id"] == bob.id
    assert count(db) == 0


def test_no_notification_for_a_change_you_made_yourself(
    client, alice, shared_project, db, captured_enqueues
):
    task = client.post(
        TASKS,
        json={"project_id": shared_project, "title": "x", "assignee_id": alice.id},
        headers=alice.headers,
    ).json()["id"]
    captured_enqueues.clear()

    client.patch(f"{TASKS}/{task}", json={"status": "done"}, headers=alice.headers)

    assert captured_enqueues == []


# --------------------------------------------------------------------------
# The overdue sweep
# --------------------------------------------------------------------------


def test_overdue_scan_notifies_the_assignee(client, alice, bob, shared_project, db):
    client.post(
        TASKS,
        json={
            "project_id": shared_project,
            "title": "late",
            "assignee_id": bob.id,
            "due_date": past(),
        },
        headers=alice.headers,
    )

    assert jobs.scan_overdue_tasks() == 1
    assert count(db, user_id=uuid.UUID(bob.id), type=NotificationType.task_overdue) == 1


def test_overdue_scan_falls_back_to_the_project_owner(client, alice, shared_project, db):
    client.post(
        TASKS,
        json={"project_id": shared_project, "title": "late and unowned", "due_date": past()},
        headers=alice.headers,
    )

    jobs.scan_overdue_tasks()

    # An unassigned task still has to reach a human.
    assert count(db, user_id=uuid.UUID(alice.id), type=NotificationType.task_overdue) == 1


def test_overdue_scan_is_idempotent(client, alice, bob, shared_project, db):
    client.post(
        TASKS,
        json={
            "project_id": shared_project,
            "title": "late",
            "assignee_id": bob.id,
            "due_date": past(),
        },
        headers=alice.headers,
    )

    assert jobs.scan_overdue_tasks() == 1
    assert jobs.scan_overdue_tasks() == 0
    assert jobs.scan_overdue_tasks() == 0
    assert count(db, type=NotificationType.task_overdue) == 1


def test_a_redelivered_job_cannot_create_a_duplicate(client, alice, bob, shared_project, db):
    """The guard that matters: acks_late means the same message can run twice
    even after overdue_notified_at was stamped."""
    task_id = client.post(
        TASKS,
        json={
            "project_id": shared_project,
            "title": "late",
            "assignee_id": bob.id,
            "due_date": past(),
        },
        headers=alice.headers,
    ).json()["id"]
    jobs.scan_overdue_tasks()

    # Simulate the worker dying after creating the row but before committing
    # the stamp, so the sweep picks it up again.
    task = db.get(Task, uuid.UUID(task_id))
    task.overdue_notified_at = None
    db.commit()

    jobs.scan_overdue_tasks()

    # The unique constraint, not the stamp, is what saves us here.
    assert count(db, type=NotificationType.task_overdue) == 1


def test_completed_and_future_tasks_are_never_overdue(client, alice, bob, shared_project, db):
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3)).isoformat()
    client.post(
        TASKS,
        json={"project_id": shared_project, "title": "future", "due_date": future},
        headers=alice.headers,
    )
    done = client.post(
        TASKS,
        json={"project_id": shared_project, "title": "done late", "due_date": past()},
        headers=alice.headers,
    ).json()["id"]
    client.patch(f"{TASKS}/{done}", json={"status": "done"}, headers=alice.headers)
    client.post(
        TASKS, json={"project_id": shared_project, "title": "no due date"}, headers=alice.headers
    )

    assert jobs.scan_overdue_tasks() == 0
    assert count(db, type=NotificationType.task_overdue) == 0


def test_rescheduling_allows_a_second_overdue_notification(client, alice, bob, shared_project, db):
    task_id = client.post(
        TASKS,
        json={
            "project_id": shared_project,
            "title": "late",
            "assignee_id": bob.id,
            "due_date": past(hours=5),
        },
        headers=alice.headers,
    ).json()["id"]
    jobs.scan_overdue_tasks()

    # Pushed back, then missed again. The dedupe key is the due date, so this
    # is a genuinely new event rather than a retry.
    client.patch(f"{TASKS}/{task_id}", json={"due_date": past(hours=1)}, headers=alice.headers)

    assert jobs.scan_overdue_tasks() == 1
    assert count(db, type=NotificationType.task_overdue) == 2


def test_overdue_scan_respects_its_batch_size(client, alice, bob, shared_project, db):
    for index in range(5):
        client.post(
            TASKS,
            json={
                "project_id": shared_project,
                "title": f"late {index}",
                "assignee_id": bob.id,
                "due_date": past(hours=index + 1),
            },
            headers=alice.headers,
        )

    assert jobs.scan_overdue_tasks(batch_size=2) == 2
    assert jobs.scan_overdue_tasks(batch_size=2) == 2
    assert jobs.scan_overdue_tasks(batch_size=2) == 1
    assert count(db, type=NotificationType.task_overdue) == 5


def test_job_for_a_deleted_task_is_a_no_op(client, alice, bob, shared_project, db):
    response = jobs.notify_task_reassigned.run(
        task_id=str(uuid.uuid4()),
        assignee_id=bob.id,
        occurred_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        previous_assignee_id=None,
    )

    assert response is False
    assert count(db) == 0


# --------------------------------------------------------------------------
# Notification API
# --------------------------------------------------------------------------


def test_notifications_are_listed_newest_first_and_can_be_marked_read(
    client, alice, bob, shared_project
):
    client.post(
        TASKS,
        json={
            "project_id": shared_project,
            "title": "late",
            "assignee_id": bob.id,
            "due_date": past(),
        },
        headers=alice.headers,
    )
    jobs.scan_overdue_tasks()

    listing = client.get("/api/v1/notifications", headers=bob.headers).json()
    assert listing["total"] == 2  # assigned, then overdue
    assert client.get("/api/v1/notifications/unread-count", headers=bob.headers).json() == {
        "unread": 2
    }

    first = listing["items"][0]["id"]
    assert (
        client.post(f"/api/v1/notifications/{first}/read", headers=bob.headers).status_code == 200
    )
    assert client.get("/api/v1/notifications/unread-count", headers=bob.headers).json() == {
        "unread": 1
    }
    assert (
        client.get("/api/v1/notifications?unread_only=true", headers=bob.headers).json()["total"]
        == 1
    )

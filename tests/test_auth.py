from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.models import RefreshToken, User

SIGNUP = "/api/v1/auth/signup"
LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/auth/me"

PASSWORD = "correct-horse-battery"


def test_signup_returns_user_and_tokens(client):
    response = client.post(
        SIGNUP, json={"email": "Ada@Example.COM", "full_name": "Ada", "password": PASSWORD}
    )

    assert response.status_code == 201
    body = response.json()
    # Stored and echoed lowercased, so Ada@ and ada@ cannot both register.
    assert body["user"]["email"] == "ada@example.com"
    assert body["tokens"]["token_type"] == "bearer"
    assert body["tokens"]["expires_in"] > 0


def test_password_never_appears_in_any_response(client):
    response = client.post(
        SIGNUP, json={"email": "ada@example.com", "full_name": "Ada", "password": PASSWORD}
    )
    token = response.json()["tokens"]["access_token"]
    me = client.get(ME, headers={"Authorization": f"Bearer {token}"})

    for payload in (response.text, me.text):
        assert PASSWORD not in payload
        assert "password" not in payload
        assert "argon2" not in payload


def test_password_is_stored_as_an_argon2id_hash(client, db):
    client.post(SIGNUP, json={"email": "ada@example.com", "full_name": "Ada", "password": PASSWORD})

    user = db.scalar(select(User).where(User.email == "ada@example.com"))
    assert user.password_hash.startswith("$argon2id$")
    assert PASSWORD not in user.password_hash


def test_duplicate_email_is_rejected(client):
    payload = {"email": "ada@example.com", "full_name": "Ada", "password": PASSWORD}
    client.post(SIGNUP, json=payload)

    response = client.post(SIGNUP, json={**payload, "email": "ADA@example.com"})

    assert response.status_code == 409


def test_short_password_is_rejected_without_echoing_it(client):
    response = client.post(
        SIGNUP, json={"email": "ada@example.com", "full_name": "Ada", "password": "hunter2"}
    )

    assert response.status_code == 422
    # Pydantic would normally include the rejected input; it must not here,
    # or every failed login puts a password in the response and the logs.
    assert "hunter2" not in response.text


def test_login_succeeds_and_wrong_password_does_not(client, alice):
    assert client.post(LOGIN, json={"email": alice.email, "password": PASSWORD}).status_code == 200

    bad = client.post(LOGIN, json={"email": alice.email, "password": "wrong-password-here"})
    assert bad.status_code == 401


def test_unknown_email_and_wrong_password_are_indistinguishable(client, alice):
    unknown = client.post(LOGIN, json={"email": "nobody@example.com", "password": PASSWORD})
    wrong = client.post(LOGIN, json={"email": alice.email, "password": "wrong-password-here"})

    assert unknown.status_code == wrong.status_code == 401
    # Identical bodies, so the endpoint cannot be used to enumerate accounts.
    assert unknown.json() == wrong.json()


def test_me_requires_a_token(client):
    assert client.get(ME).status_code == 401
    assert client.get(ME, headers={"Authorization": "Bearer nonsense"}).status_code == 401


def test_token_signed_with_another_secret_is_rejected(client, alice):
    import jwt

    forged = jwt.encode(
        {
            "sub": alice.id,
            "type": "access",
            "exp": int((dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).timestamp()),
        },
        "a-different-secret",
        algorithm="HS256",
    )

    assert client.get(ME, headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_refresh_token_is_rotated_and_old_one_is_rejected(client, alice):
    first = client.post(REFRESH, json={"refresh_token": alice.refresh_token})
    assert first.status_code == 200
    rotated = first.json()["refresh_token"]
    assert rotated != alice.refresh_token

    replayed = client.post(REFRESH, json={"refresh_token": alice.refresh_token})
    assert replayed.status_code == 401

    # Replay is treated as theft, so the whole family dies - including the
    # token the legitimate holder just received.
    assert client.post(REFRESH, json={"refresh_token": rotated}).status_code == 401


def test_only_the_hash_of_a_refresh_token_is_stored(client, alice, db):
    stored = db.scalars(select(RefreshToken)).all()

    assert len(stored) == 1
    assert stored[0].token_hash != alice.refresh_token
    assert len(stored[0].token_hash) == 64


def test_logout_revokes_the_refresh_token(client, alice):
    assert client.post(LOGOUT, json={"refresh_token": alice.refresh_token}).status_code == 200
    assert client.post(REFRESH, json={"refresh_token": alice.refresh_token}).status_code == 401


def test_logout_is_idempotent(client, alice):
    client.post(LOGOUT, json={"refresh_token": alice.refresh_token})

    # Already revoked: the caller's intent is satisfied, so this is not an error.
    assert client.post(LOGOUT, json={"refresh_token": alice.refresh_token}).status_code == 200
    assert client.post(LOGOUT, json={"refresh_token": "never-existed"}).status_code == 200


def test_expired_refresh_token_is_rejected(client, alice, db):
    record = db.scalars(select(RefreshToken)).one()
    record.expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    db.commit()

    response = client.post(REFRESH, json={"refresh_token": alice.refresh_token})
    assert response.status_code == 401


def test_deactivated_user_cannot_use_an_existing_access_token(client, alice, db):
    user = db.get(User, __import__("uuid").UUID(alice.id))
    user.is_active = False
    db.commit()

    # This is what the per-request user lookup buys: the token is still
    # cryptographically valid but stops working immediately.
    assert client.get(ME, headers=alice.headers).status_code == 401

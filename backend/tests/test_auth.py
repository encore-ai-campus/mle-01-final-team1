
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from api import auth
from api.main import app
from services.auth_service import JWT_SECRET_KEY, JWT_ALGORITHM


TEST_USER = {
    "email": "test@example.com",
    "password": "Test1234!",
    "nickname": "테스트",
    "gender": "선택안함",
    "birth_date": "2000-01-01",
}


class FakeSession:
    """Test-only user storage; never connects to PostgreSQL."""

    def __init__(self, users):
        self.users = users
        self.pending = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def scalar(self, statement):
        params = statement.compile().params
        email = next(
            (v for v in params.values() if isinstance(v, str)),
            None,
        )
        if email is None:
            raise AssertionError("Unexpected user lookup")
        return next(
            (
                user for user in self.users.values()
                if user.email.lower() == email.lower()
            ),
            None,
        )

    def get(self, model, user_id):
        return self.users.get(user_id)

    def add(self, user):
        self.pending = user
        user.id = uuid4()

    def commit(self):
        if self.pending is not None:
            self.users[self.pending.id] = self.pending
            self.pending = None

    def refresh(self, user):
        pass

    def rollback(self):
        self.pending = None


@pytest.fixture
def client(monkeypatch):
    users = {}

    monkeypatch.setattr(
        auth,
        "SessionLocal",
        lambda: FakeSession(users),
    )

    with TestClient(app) as test_client:
        yield test_client


def register(client, user_data=None):
    return client.post(
        "/api/auth/register",
        json=TEST_USER if user_data is None else user_data,
    )


def login(client, email="test@example.com", password="Test1234!"):
    return client.post(
        "/api/auth/login",
        json={
            "email": email,
            "password": password,
        },
    )


# 1. Successful registration
def test_register_success(client):
    response = register(client)

    assert response.status_code == 201
    assert response.json()["email"] == "test@example.com"
    assert response.json()["nickname"] == "테스트"
    assert UUID(response.json()["user_id"])


# 2. Duplicate email rejected
def test_duplicate_email_rejected(client):
    assert register(client).status_code == 201

    response = register(client)

    assert response.status_code == 409


# 3. Weak password rejected
def test_invalid_password_rejected(client):
    user = {**TEST_USER, "password": "test1234"}

    response = register(client, user)

    assert response.status_code == 422


# 4. Invalid email rejected
def test_invalid_email_rejected(client):
    user = {**TEST_USER, "email": "not-an-email"}

    response = register(client, user)

    assert response.status_code == 422


# 5. Successful login
def test_login_success(client):
    assert register(client).status_code == 201

    response = login(client)

    assert response.status_code == 200
    assert response.json()["access_token"]
    assert response.json()["token_type"] == "bearer"


# 6. Wrong password rejected
def test_wrong_password_rejected(client):
    register(client)

    response = login(client, password="Wrong1234!")

    assert response.status_code == 401


# 7. Unauthenticated request rejected
def test_unauthenticated_request_rejected(client):
    response = client.get("/api/users/me")

    assert response.status_code == 401


# 8. JWT allows own profile access
def test_get_my_profile(client):
    register(client)
    token = login(client).json()["access_token"]

    response = client.get(
        "/api/users/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["email"] == "test@example.com"
    assert response.json()["nickname"] == "테스트"
    assert "password_hash" not in response.json()


# 9. Forged JWT rejected
def test_forged_token_rejected(client):
    response = client.get(
        "/api/users/me",
        headers={"Authorization": "Bearer invalid.token.value"},
    )

    assert response.status_code == 401


# 10. Expired JWT rejected
def test_expired_token_rejected(client):
    expired_token = jwt.encode(
        {
            "sub": str(uuid4()),
            "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
        },
        JWT_SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )

    response = client.get(
        "/api/users/me",
        headers={"Authorization": f"Bearer {expired_token}"},
    )

    assert response.status_code == 401


# 11. User isolation
def test_user_isolation(client):
    assert register(client).status_code == 201

    second_user = {
        **TEST_USER,
        "email": "second@example.com",
        "nickname": "두번째",
    }
    assert register(client, second_user).status_code == 201

    first_token = login(client).json()["access_token"]
    second_token = login(
        client,
        email="second@example.com",
    ).json()["access_token"]

    first_response = client.get(
        "/api/users/me",
        headers={"Authorization": f"Bearer {first_token}"},
    )
    second_response = client.get(
        "/api/users/me",
        headers={"Authorization": f"Bearer {second_token}"},
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200

    assert first_response.json()["email"] == "test@example.com"
    assert first_response.json()["nickname"] == "테스트"

    assert second_response.json()["email"] == "second@example.com"
    assert second_response.json()["nickname"] == "두번째"

    assert (
        first_response.json()["user_id"]
        != second_response.json()["user_id"]
    )

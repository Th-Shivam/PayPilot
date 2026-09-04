from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app.auth import AuthError, AuthenticatedUser, SupabaseAuthenticator
from backend.app.config import ConfigurationError, Settings
from backend.app.main import create_app
from backend.app.repository import InMemoryRepository


class FakeAuthenticator:
    def __init__(self) -> None:
        self.users = {
            "support-token": AuthenticatedUser("support", "support_agent"),
            "owner-a-token": AuthenticatedUser("owner-a", "business_owner"),
            "owner-b-token": AuthenticatedUser("owner-b", "business_owner"),
        }

    def authenticate(self, token: str) -> AuthenticatedUser:
        if token in {"expired-token", "invalid-token"}:
            raise AuthError(401, "INVALID_TOKEN", "The access token is invalid or expired.")
        return self.users[token]


def _repo() -> InMemoryRepository:
    return InMemoryRepository(
        {
            "txn-a": {
                "owner_id": "owner-a",
                "transaction_id": "txn-a",
                "ticket_id": "ticket-a",
                "diagnosis": "clean",
                "status": "clean",
                "explanation": "Owner A transaction",
                "action_taken": "no_action_needed",
                "confidence": "high",
                "occurred_at": datetime(2025, 1, 15, tzinfo=timezone.utc),
            },
            "txn-b": {
                "owner_id": "owner-b",
                "transaction_id": "txn-b",
                "ticket_id": "ticket-b",
                "diagnosis": "anomaly",
                "status": "anomaly",
                "explanation": "Owner B transaction",
                "action_taken": "escalated",
                "confidence": "low_flagged_for_review",
                "occurred_at": datetime(2025, 1, 15, tzinfo=timezone.utc),
            },
        }
    )


def _client(repo: InMemoryRepository) -> TestClient:
    return TestClient(create_app(Settings(require_auth=True), repo, FakeAuthenticator()))


def test_missing_invalid_and_expired_tokens_return_safe_401():
    client = _client(_repo())
    for headers in ({}, {"Authorization": "Bearer invalid-token"}, {"Authorization": "Bearer expired-token"}):
        response = client.get("/tickets", headers=headers)
        assert response.status_code == 401
        assert response.json()["error"]["code"] in {"AUTH_REQUIRED", "INVALID_TOKEN"}
        assert response.json()["error"]["message"] in {"A valid access token is required.", "The access token is invalid or expired."}


def test_support_agent_can_resolve_and_read_all_data():
    repo = _repo()
    client = _client(repo)
    headers = {"Authorization": "Bearer support-token"}
    assert client.post("/resolve", json={"txn_id": "txn-a"}, headers=headers).status_code == 200
    assert len(client.get("/tickets", headers=headers).json()) == 2
    assert client.get("/analytics", headers=headers).status_code == 200
    assert client.get("/exceptions", headers=headers).status_code == 200
    assert client.post("/reconcile", json={"date_from": "2025-01-14", "date_to": "2025-01-16"}, headers=headers).status_code == 200


def test_business_owner_is_scoped_and_cannot_mutate_support_resources():
    repo = _repo()
    client = _client(repo)
    headers = {"Authorization": "Bearer owner-a-token"}
    tickets = client.get("/tickets", headers=headers)
    assert tickets.status_code == 200
    assert {row["transaction_id"] for row in tickets.json()} == {"txn-a"}
    assert client.get("/trace/txn-b", headers=headers).status_code == 403
    assert client.post("/resolve", json={"txn_id": "txn-a", "role": "support_agent"}, headers=headers).status_code == 403
    assert client.post("/reconcile", json={"date_from": "2025-01-14", "date_to": "2025-01-16"}, headers=headers).status_code == 403


def test_owner_a_can_read_own_trace_but_not_owner_b():
    repo = _repo()
    repo.resolve("txn-a")
    repo.resolve("txn-b")
    client = _client(repo)
    headers = {"Authorization": "Bearer owner-a-token"}
    assert client.get("/trace/txn-a", headers=headers).status_code == 200
    assert client.get("/trace/txn-b", headers=headers).status_code == 403


def test_owner_a_cannot_read_owner_b_ticket_or_stream_a_resolution():
    repo = _repo()
    repo.resolve("txn-a")
    client = _client(repo)
    headers = {"Authorization": "Bearer owner-a-token"}
    tickets = client.get("/tickets", headers=headers).json()
    assert all(row["transaction_id"] != "txn-b" for row in tickets)
    assert client.post(
        "/resolve",
        json={"txn_id": "txn-a"},
        headers={**headers, "Accept": "text/event-stream"},
    ).status_code == 403


def test_auth_disabled_is_rejected_outside_local():
    with pytest.raises(ConfigurationError, match="only permitted with APP_ENV=local"):
        Settings(app_env="production", require_auth=False).validate_for_runtime()
    with pytest.raises(ConfigurationError, match="only permitted with APP_ENV=local"):
        Settings(app_env="staging", require_auth=False).validate_for_runtime()


class FakeAuthClient:
    class Auth:
        @staticmethod
        def get_user(token: str):
            if token != "valid":
                raise RuntimeError("invalid")
            return type("Response", (), {"user": type("User", (), {"id": "user-1", "email": "u@example.com"})()})()

    auth = Auth()

    def table(self, _name: str):
        return self

    def select(self, _fields: str):
        return self

    def eq(self, _key: str, _value: str):
        return self

    def limit(self, _value: int):
        return self

    def execute(self):
        return type("Response", (), {"data": [{"id": "user-1", "role": "business_owner"}]})()


def test_supabase_authenticator_uses_profile_role_not_user_metadata():
    user = SupabaseAuthenticator(FakeAuthClient()).authenticate("valid")
    assert user.user_id == "user-1"
    assert user.role == "business_owner"
    with pytest.raises(AuthError) as error:
        SupabaseAuthenticator(FakeAuthClient()).authenticate("invalid")
    assert error.value.status_code == 401

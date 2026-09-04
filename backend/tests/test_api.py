import json
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.repository import InMemoryRepository


def test_resolve_trace_and_error_contract():
    repo = InMemoryRepository(
        {"txn-1": {"transaction_id": "txn-1", "status": "resolved", "explanation": "Matched", "action": "no_action_needed"}}
    )
    client = TestClient(create_app(Settings(require_auth=False), repo))

    # Successful resolution
    response = client.post("/resolve", json={"transaction_id": "txn-1"}, headers={"x-request-id": "req-1"})
    assert response.status_code == 200
    data = response.json()
    assert data["transaction_id"] == "txn-1"
    assert data["trace"]["request_id"] == "req-1"

    # Trace retrieval
    trace_res = client.get("/trace/txn-1")
    assert trace_res.status_code == 200
    assert trace_res.json()["request_id"] == "req-1"
    assert client.get("/trace/txn-1").status_code == 200

    # Trace not found
    missing_trace = client.get("/trace/nonexistent")
    assert missing_trace.status_code == 404
    assert missing_trace.json()["error"]["code"] == "TXN_NOT_FOUND"

    # Missing transaction resolution
    missing = client.post("/resolve", json={"transaction_id": "missing"}, headers={"x-request-id": "req-2"})
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "TXN_NOT_FOUND"
    assert missing.json()["error"]["request_id"] == "req-2"

    # Invalid transaction ID (schema validation failure)
    invalid = client.post("/resolve", json={"transaction_id": "bad id"}, headers={"x-request-id": "req-3"})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "INVALID_REQUEST"
    assert invalid.json()["error"]["request_id"] == "req-3"


def test_tickets_analytics_exceptions_endpoints():
    test_tickets = {
        "t1": {
            "ticket_id": "t1",
            "transaction_id": "txn-1",
            "status": "clean",
            "explanation": "Matched",
            "action_taken": "no_action_needed",
            "confidence": "high",
        },
        "t2": {
            "ticket_id": "t2",
            "transaction_id": "txn-2",
            "status": "anomaly",
            "explanation": "Delayed bank record",
            "action_taken": "escalated",
            "confidence": "low_flagged_for_review",
        },
        "t3": {
            "ticket_id": "t3",
            "transaction_id": "txn-3",
            "status": "ledger_gap",
            "explanation": "Missing ledger entry",
            "action_taken": "ledger_entry_created",
            "confidence": 0.85,
        },
    }
    repo = InMemoryRepository(test_tickets)
    client = TestClient(create_app(Settings(require_auth=False), repo))

    # GET /tickets
    all_tickets = client.get("/tickets").json()
    assert len(all_tickets) == 3

    # Filter by action_taken
    escalated = client.get("/tickets?action_taken=escalated").json()
    assert len(escalated) == 1
    assert escalated[0]["transaction_id"] == "txn-2"

    # Filter by confidence
    low_conf = client.get("/tickets?confidence=low").json()
    assert len(low_conf) == 1
    assert low_conf[0]["transaction_id"] == "txn-2"

    # GET /exceptions
    exceptions = client.get("/exceptions").json()
    assert any(e["transaction_id"] == "txn-2" for e in exceptions)

    # GET /analytics
    analytics = client.get("/analytics").json()
    assert "by_action" in analytics
    assert "by_confidence" in analytics
    assert analytics["by_action"]["escalated"] == 1
    assert analytics["by_confidence"]["low_flagged_for_review"] == 1


def test_batch_reconcile_endpoint():
    repo = InMemoryRepository(
        {
            "txn-10": {
                "transaction_id": "txn-10",
                "status": "clean",
                "explanation": "Matched",
                "action": "no_action_needed",
                "occurred_at": datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc),
            }
        }
    )
    client = TestClient(create_app(Settings(require_auth=False), repo))

    # Valid date range
    res = client.post("/reconcile", json={"date_from": "2025-01-14", "date_to": "2025-01-16"})
    assert res.status_code == 200
    results = res.json()["results"]
    assert len(results) == 1
    assert results[0]["transaction_id"] == "txn-10"

    # Invalid date range (date_from > date_to)
    err = client.post("/reconcile", json={"date_from": "2025-01-20", "date_to": "2025-01-10"})
    assert err.status_code == 422
    assert err.json()["error"]["code"] == "INVALID_REQUEST"

    value_error = InMemoryRepository({"txn-10": {"transaction_id": "txn-10", "status": "clean", "explanation": "Matched", "action": "no_action_needed", "occurred_at": datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc)}})
    value_error.resolve = lambda *_args: (_ for _ in ()).throw(ValueError("bad persisted value"))
    value_client = TestClient(create_app(Settings(require_auth=False), value_error))
    value_response = value_client.post("/resolve", json={"txn_id": "txn-10"})
    assert value_response.status_code == 422
    assert value_response.json()["error"]["code"] == "INVALID_REQUEST"


def test_openapi_and_cors_contract():
    client = TestClient(create_app(Settings(require_auth=False), InMemoryRepository()))
    schema = client.get("/openapi.json").json()
    assert {"/resolve", "/trace/{transaction_id}", "/tickets", "/exceptions", "/analytics", "/reconcile"} <= set(schema["paths"])
    assert "/trace/{txn_id}" not in schema["paths"]
    resolve_responses = schema["paths"]["/resolve"]["post"]["responses"]
    assert "text/event-stream" in resolve_responses["200"]["content"]
    cors = client.options("/resolve", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"})
    assert cors.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_resolve_sse_streams_canonical_events_before_completion_and_replays_them():
    repo = InMemoryRepository({"txn-stream": {"status": "clean", "action": "no_action_needed", "explanation": "Records match."}})
    client = TestClient(create_app(Settings(require_auth=False), repo))
    response = client.post("/resolve", json={"txn_id": "txn-stream"}, headers={"accept": "text/event-stream", "x-request-id": "stream-1"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = []
    for frame in response.text.strip().split("\n\n"):
        data = next(line[6:] for line in frame.splitlines() if line.startswith("data: "))
        events.append(json.loads(data))
    assert len(events) >= 9
    assert [event["step_number"] for event in events] == list(range(1, len(events) + 1))
    assert events[0]["event_type"] == "tool_start"
    assert events[-1]["event_type"] == "completion"
    assert events[-1]["status"] == "completed"
    assert {event["event_type"] for event in events} >= {"tool_start", "tool_result", "decision", "action", "completion"}
    replay = client.get("/trace/txn-stream")
    assert replay.status_code == 200
    assert [event["event_id"] for event in replay.json()["steps"]] == [event["event_id"] for event in events]
    assert client.get("/trace/txn-stream").json()["steps"] == replay.json()["steps"]


def test_resolve_sse_persists_failed_partial_trace():
    client = TestClient(create_app(Settings(require_auth=False), InMemoryRepository()))
    response = client.post("/resolve", json={"txn_id": "missing-stream"}, headers={"accept": "text/event-stream"})
    assert response.status_code == 200
    assert '"status":"failed"' in response.text
    trace = client.get("/trace/missing-stream")
    assert trace.status_code == 200
    assert trace.json()["steps"][-1]["status"] == "failed"


def test_repository_failures_return_a_safe_dependency_error():
    class BrokenRepository:
        def tickets(self, *_args):
            raise OSError("database socket unavailable")

    client = TestClient(create_app(Settings(require_auth=False), BrokenRepository()))
    response = client.get("/tickets")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert "socket" not in response.json()["error"]["message"]

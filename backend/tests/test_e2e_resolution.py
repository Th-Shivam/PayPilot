"""End-to-end tests: HTTP POST /resolve -> real SupabaseRepository ->
compare_records -> action guards -> response, over the seeded fake Supabase.

This is the layer the unit suite doesn't cover: it exercises the genuine
reconciliation verdict and the resolution actions through the API, using the
same fixtures the demo runs on. Assertions target specific fields and status
codes so a failure points at the broken layer rather than a snapshot diff.
"""

from __future__ import annotations

import json

import pytest

from .conftest import EXPECTED, FakeOrchestrator


def _sse_events(text: str) -> list[dict]:
    events = []
    for frame in text.strip().split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def _agent_ledger_rows(repo, txn_id: str) -> list[dict]:
    return [
        r for r in repo.client.tables["ledger_entries"]
        if r.get("txn_id") == txn_id and r.get("source") == "agent_reconciliation"
    ]


def _tickets_for(repo, txn_id: str) -> list[dict]:
    return [r for r in repo.client.tables["tickets"] if r.get("txn_id") == txn_id]


# --- the five diagnosis paths, end to end through HTTP --------------------


@pytest.mark.parametrize("txn_id,expected", list(EXPECTED.items()))
def test_resolve_classifies_every_path_through_the_api(client, txn_id, expected):
    expected_status, expected_action = expected
    response = client.post("/resolve", json={"txn_id": txn_id}, headers={"x-request-id": f"e2e-{txn_id}"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == expected_status
    assert body["action"] == expected_action
    assert body["explanation"].strip()
    assert body["trace"]["request_id"] == f"e2e-{txn_id}"
    # The trace carries the real ordered steps ending in completion.
    steps = body["trace"]["steps"]
    assert steps
    assert steps[0]["step_number"] == 1
    assert [s["step_number"] for s in steps] == sorted(s["step_number"] for s in steps)


def test_resolved_trace_is_replayable_from_get_trace(client):
    client.post("/resolve", json={"txn_id": "TXNLEDGERGAP001"}, headers={"x-request-id": "replay-1"})
    trace = client.get("/trace/TXNLEDGERGAP001")
    assert trace.status_code == 200
    body = trace.json()
    assert body["request_id"] == "replay-1"
    # The decision step records the deterministic verdict, traceable to the DB.
    decisions = [s for s in body["steps"] if s.get("event_type") == "decision"]
    assert decisions and decisions[0]["step_name"] == "compare_records"


def test_unknown_txn_is_a_clean_404(client):
    response = client.post("/resolve", json={"txn_id": "TXNDOESNOTEXIST"}, headers={"x-request-id": "missing-1"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TXN_NOT_FOUND"
    assert response.json()["error"]["request_id"] == "missing-1"


# --- agent actions end to end, with a deterministic orchestrator ----------


def test_ledger_gap_creates_one_agent_sourced_entry(make_client):
    client, repo = make_client(FakeOrchestrator())
    response = client.post("/resolve", json={"txn_id": "TXNLEDGERGAP001"})
    assert response.status_code == 200
    assert response.json()["status"] == "ledger_gap"
    rows = _agent_ledger_rows(repo, "TXNLEDGERGAP001")
    assert len(rows) == 1
    assert rows[0]["source"] == "agent_reconciliation"


def test_repeated_resolve_does_not_duplicate_the_ledger_entry(make_client):
    client, repo = make_client(FakeOrchestrator())
    client.post("/resolve", json={"txn_id": "TXNLEDGERGAP001"})
    client.post("/resolve", json={"txn_id": "TXNLEDGERGAP001"})
    # Idempotent: the second run sees the agent-created row and does not add another.
    assert len(_agent_ledger_rows(repo, "TXNLEDGERGAP001")) == 1


def test_repeated_resolve_does_not_duplicate_the_ticket(make_client):
    client, repo = make_client(FakeOrchestrator())
    client.post("/resolve", json={"txn_id": "TXNANOMALY001"})
    client.post("/resolve", json={"txn_id": "TXNANOMALY001"})
    tickets = _tickets_for(repo, "TXNANOMALY001")
    assert len(tickets) == 1
    assert tickets[0]["action_taken"] == "escalated"


def test_llm_explanation_cannot_override_the_deterministic_verdict(make_client):
    # The orchestrator returns a confidently wrong explanation.
    client, _ = make_client(FakeOrchestrator(explanation="[LLM] Everything matches, no issue at all."))
    response = client.post("/resolve", json={"txn_id": "TXNANOMALY001"})
    body = response.json()
    # Verdict and action still come from compare_records, not the model.
    assert body["status"] == "anomaly"
    assert body["action"] == "escalated"
    # The wording is the model's, but the facts are not.
    assert body["explanation"] == "[LLM] Everything matches, no issue at all."


def test_clean_transaction_takes_no_action(make_client):
    client, repo = make_client(FakeOrchestrator())
    response = client.post("/resolve", json={"txn_id": "TXNCLEAN001"})
    assert response.json()["status"] == "clean"
    assert _tickets_for(repo, "TXNCLEAN001") == []
    assert _agent_ledger_rows(repo, "TXNCLEAN001") == []


# --- streaming drives the same real reconciliation ------------------------


def test_streaming_resolve_emits_real_decision_and_completion(client):
    response = client.post(
        "/resolve",
        json={"txn_id": "TXNAMOUNTMISMATCH001"},
        headers={"accept": "text/event-stream", "x-request-id": "stream-e2e"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(response.text)
    assert [e["step_number"] for e in events] == list(range(1, len(events) + 1))
    decision = next(e for e in events if e["event_type"] == "decision")
    assert decision["detail"]["match_status"] == "amount_mismatch"
    completion = events[-1]
    assert completion["event_type"] == "completion"
    assert completion["status"] == "completed"


def test_streaming_unknown_txn_persists_a_failed_partial_trace(client):
    response = client.post(
        "/resolve",
        json={"txn_id": "TXNNOPE"},
        headers={"accept": "text/event-stream"},
    )
    assert response.status_code == 200
    events = _sse_events(response.text)
    assert events[-1]["status"] == "failed"
    # The failed step names the layer that stopped the run.
    assert events[-1]["event_type"] == "completion"

import json

import pytest

from backend.agent.orchestrator import GroqOrchestrator, GroqOrchestrationError, MAX_AGENT_STEPS, tool_schemas


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class FakeCompletion:
    def __init__(self, message):
        self.choices = [type("Choice", (), {"message": message})()]


class FakeClient:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.models = []
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        self.models.append(kwargs["model"])
        return FakeCompletion(next(self.messages))


def test_tool_registry_excludes_compare_records():
    names = [item["function"]["name"] for item in tool_schemas()]
    assert "compare_records" not in names
    assert {"lookup_gateway", "lookup_bank", "lookup_ledger", "create_ledger_entry", "raise_ticket", "close_as_resolved", "search_similar_tickets"} <= set(names)


def test_orchestrator_retains_tool_trace_and_diagnosis_wins():
    call = type("Call", (), {"id": "c1", "function": type("Fn", (), {"name": "lookup_gateway", "arguments": '{"txn_id":"txn-1"}'})()})()
    client = FakeClient([FakeMessage(tool_calls=[call]), FakeMessage(content=json.dumps({"status": "wrong", "action": "no_action_needed", "explanation": "matched"}))])
    result = GroqOrchestrator(client, {"lookup_gateway": lambda txn_id: {"txn_id": txn_id, "amount": 10}}, model="primary").run("txn-1", {"match_status": "clean"})
    assert result.response.status == "clean"
    assert [event.kind for event in result.trace][:2] == ["tool_call", "tool_result"]


def test_orchestrator_records_model_diagnosis_divergence():
    client = FakeClient([FakeMessage(content='{"status":"anomaly","action":"escalated","explanation":"ok"}')])
    result = GroqOrchestrator(client, {}, model="primary").run("txn-1", {"match_status": "clean", "action": "no_action_needed"})
    assert result.response.status == "clean"
    assert result.response.action == "no_action_needed"
    assert any(event.kind == "diagnosis_divergence" for event in result.trace)


def test_orchestrator_falls_back_then_returns_structured_result():
    client = FakeClient([RuntimeError("primary")])
    client.messages = iter([FakeMessage(content='{"status":"clean","action":"no_action_needed","explanation":"ok"}')])
    class FailingPrimary:
        def __init__(self):
            self.chat = type("Chat", (), {"completions": self})()
            self.models = []
        def create(self, **kwargs):
            self.models.append(kwargs["model"])
            if kwargs["model"] == "primary":
                raise RuntimeError("rate limit")
            return FakeCompletion(FakeMessage(content='{"status":"clean","action":"no_action_needed","explanation":"ok"}'))
    client = FailingPrimary()
    result = GroqOrchestrator(client, {}, model="primary", fallback_model="fallback", max_retries=0).run("txn-1", {"match_status": "clean"})
    assert result.fallback_used is True
    assert client.models == ["primary", "fallback"]


def test_orchestrator_stops_at_max_steps():
    call = type("Call", (), {"id": "c1", "function": type("Fn", (), {"name": "lookup_gateway", "arguments": '{"txn_id":"txn-1"}'})()})()
    client = FakeClient([FakeMessage(tool_calls=[call])] * MAX_AGENT_STEPS)
    with pytest.raises(GroqOrchestrationError, match="Maximum agent steps"):
        GroqOrchestrator(client, {"lookup_gateway": lambda txn_id: {}}, max_steps=MAX_AGENT_STEPS).run("txn-1", {"match_status": "clean"})

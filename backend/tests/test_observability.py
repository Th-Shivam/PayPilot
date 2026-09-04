"""Tracing, redaction, and correlation.

The security-relevant assertions here are the negative ones: a customer name, an
authorization header, or a service key must not appear in a span under any
configuration. They are checked against the serialised span attributes rather
than against `redact` alone, because a value can also reach a span through
Logfire's own capture of endpoint arguments.

Logfire's scrubber would catch some of the same leaks. It is deliberately not
what these tests exercise — `scrubbing` is a configuration option and the
guarantee has to hold at PayPilot's own boundary.
"""

import json
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from backend.agent import GroqOrchestrator
from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.repository import InMemoryRepository, SupabaseRepository
from backend.domain.models import BankSettlement, GatewayTransaction
from backend.observability import (
    REDACTED,
    configure_observability,
    is_sensitive_key,
    log_error,
    log_info,
    log_warn,
    observability_enabled,
    redact,
    redact_prompt,
    reset_observability,
    span,
    truncate_text,
)
from backend.observability.middleware import _incoming_request_id
from backend.observability.tracing import annotate_current_span
from backend.reconciliation.rules import compare_records, default_reference_time
from backend.tests.test_supabase_repository import FakeClient

SECRETS = (
    "Asha Menon",
    "UTR99887766",
    "asha@example.com",
    "pylf_v1_us_KtsZk9MFWkpdQmq7BSDKmnvpLTRYGhRqKhRL1rbk83qg",
    "gsk_liveGroqKeyMaterial0123456789",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
)


@pytest.fixture
def exporter():
    """Turn tracing on with a local exporter, then hand the process back.

    `configure_observability` is process-global and configures once, so the
    fixture owns both ends: it forces a reconfigure on the way in and clears the
    flag on the way out so later modules are not left reading these spans.
    """
    from logfire.testing import SimpleSpanProcessor, TestExporter

    sink = TestExporter()
    reset_observability()
    assert configure_observability(Settings(require_auth=False), span_processors=[SimpleSpanProcessor(sink)])
    try:
        yield sink
    finally:
        reset_observability()


def finished_spans(sink):
    """Only completed spans; Logfire also exports a 'pending' twin for each."""
    return [s for s in sink.exported_spans if s.attributes.get("logfire.span_type") == "span"]


def attributes_blob(sink) -> str:
    return json.dumps([dict(s.attributes) for s in finished_spans(sink)], default=str)


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


def test_redaction_drops_identity_and_keeps_the_reconciliation_signal():
    gateway = GatewayTransaction(
        txn_id="txn-1",
        amount=1420.75,
        currency="INR",
        status="captured",
        captured_at=default_reference_time(),
        customer_name="Asha Menon",
    )
    bank = BankSettlement(txn_id="txn-1", amount=1200.00, currency="INR", status="settled", utr="UTR99887766")

    rendered = json.dumps(redact({"gateway": gateway, "bank": bank}), default=str)

    assert "Asha Menon" not in rendered
    assert "UTR99887766" not in rendered
    # The discrepancy itself has to survive, or the trace cannot show why
    # amount_mismatch fired.
    assert "1420.75" in rendered
    assert "1200.0" in rendered
    assert "captured" in rendered
    assert "txn-1" in rendered


def test_redaction_keeps_action_key_visible():
    """`action_key` ends in "_key" and is the idempotency handle, not a secret."""
    assert not is_sensitive_key("action_key")
    assert is_sensitive_key("supabase_service_role_key")
    assert is_sensitive_key("X-Api-Key")
    assert is_sensitive_key("AUTHORIZATION")

    result = redact({"status": "created", "action_key": "raise_ticket:txn-1"})
    assert result["action_key"] == "raise_ticket:txn-1"


def test_redaction_masks_secrets_whatever_shape_they_arrive_in():
    @dataclass
    class User:
        user_id: str
        email: str
        is_support_agent: bool

    payload = {
        "user": User("u-1", "asha@example.com", True),
        "settings": {"logfire_token": SecretStr(SECRETS[3])},
        "bare_secret": SecretStr("plaintext-value"),
        "headers": {"Authorization": "Bearer abc.def.ghi", "X-Request-Id": "rid-1"},
        "message": f"upstream rejected token {SECRETS[4]}",
        "session_jwt": SECRETS[5],
    }

    rendered = json.dumps(redact(payload), default=str)

    for secret in ("asha@example.com", SECRETS[3], "plaintext-value", "abc.def.ghi", SECRETS[4], SECRETS[5]):
        assert secret not in rendered, secret
    # Non-sensitive neighbours in the same mapping stay readable.
    assert "rid-1" in rendered
    assert "u-1" in rendered
    assert "is_support_agent" in rendered


def test_redaction_is_bounded_and_never_raises():
    class Hostile:
        def __getattr__(self, name):
            raise RuntimeError("no introspection for you")

        def __str__(self):
            raise RuntimeError("nor stringification")

    deep = {"level": None}
    cursor = deep
    for _ in range(30):
        cursor["level"] = {"level": None}
        cursor = cursor["level"]

    assert redact(Hostile()) == REDACTED
    assert "max depth" in json.dumps(redact(deep))
    assert "more items" in json.dumps(redact(list(range(500))))
    assert "more keys" in json.dumps(redact({f"k{i}": i for i in range(200)}))
    assert truncate_text("x" * 50, 10).endswith("[truncated 40 chars]")


def test_prompt_capture_is_redacted_structurally_and_truncated():
    diagnosis = {
        "match_status": "amount_mismatch",
        "reason_code": "AMOUNT_DISAGREEMENT_ACROSS_SOURCES",
        "detail": {
            "gateway": {"txn_id": "txn-1", "amount": 1420.75, "status": "captured", "customer_name": "Asha Menon"},
            "bank": {"txn_id": "txn-1", "amount": 1200.0, "status": "settled", "utr": "UTR99887766"},
        },
    }
    messages = [
        {"role": "system", "content": "You word a finished diagnosis."},
        {"role": "user", "content": json.dumps({"txn_id": "txn-1", "diagnosis": diagnosis})},
        {"role": "tool", "content": json.dumps({"customer_name": "Asha Menon", "action_key": "raise_ticket:txn-1"})},
        {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "lookup_bank"}}]},
        {"role": "user", "content": "x" * 5_000},
    ]

    prompt = redact_prompt(messages)

    assert "Asha Menon" not in prompt
    assert "UTR99887766" not in prompt
    # The evidence the capture exists for: the model was handed a finished
    # verdict, and the amounts behind it.
    assert "amount_mismatch" in prompt
    assert "1420.75" in prompt
    assert "raise_ticket:txn-1" in prompt
    assert "lookup_bank" in prompt
    assert "truncated" in prompt
    assert len(prompt) <= 8_100


# --------------------------------------------------------------------------- #
# Tracing is optional
# --------------------------------------------------------------------------- #


def test_tracing_disabled_is_a_working_no_op():
    """`LOGFIRE_TOKEN` unset must cost nothing but the spans."""
    reset_observability()
    try:
        assert configure_observability(Settings(require_auth=False, logfire_token=None)) is False
        assert observability_enabled() is False

        with span("reconciliation.compare_records", **{"reconciliation.txn_id": "txn-1"}) as handle:
            assert handle.recording is False
            handle.set(**{"reconciliation.match_status": "clean"})
        annotate_current_span(**{"request_id": "rid-1"})
        log_info("resolve.completed", **{"request_id": "rid-1"})
        log_warn("resolve.failed", **{"request_id": "rid-1"})
        log_error("resolve.failed", **{"request_id": "rid-1"})
    finally:
        reset_observability()


def test_span_never_swallows_or_replaces_the_callers_exception(exporter):
    class Marker(RuntimeError):
        pass

    with pytest.raises(Marker):
        with span("supabase.select"):
            raise Marker("the real failure")

    assert any(s.name == "supabase.select" for s in finished_spans(exporter))


def test_resolve_still_succeeds_when_the_exporter_is_broken(exporter, monkeypatch):
    """A failing tracing backend must not turn a good resolve into an error."""
    from backend.observability import tracing

    def explode(*_args, **_kwargs):
        raise RuntimeError("logfire unreachable")

    monkeypatch.setattr(tracing._logfire, "span", explode)

    repo = SupabaseRepository(FakeClient(), reference_time=default_reference_time())
    assert repo.resolve("txn-pending", "rid-broken")["status"] == "pending"


# --------------------------------------------------------------------------- #
# Correlation
# --------------------------------------------------------------------------- #


def test_incoming_request_id_is_validated_before_it_is_trusted():
    def scope(value: str):
        return {"headers": [(b"x-request-id", value.encode("latin-1"))]}

    assert _incoming_request_id(scope("req-1")) == "req-1"
    assert _incoming_request_id(scope("  req-1  ")) == "req-1"
    # Header injection and unbounded ids are rejected, not sanitised: a caller
    # that sent one gets a server-generated id instead.
    assert _incoming_request_id(scope("req-1\r\nX-Evil: 1")) is None
    assert _incoming_request_id(scope("a" * 129)) is None
    assert _incoming_request_id(scope("")) is None
    assert _incoming_request_id({"headers": []}) is None


@pytest.mark.parametrize(
    "call,status,code",
    [
        (lambda c: c.post("/resolve", json={"txn_id": "missing"}), 404, "TXN_NOT_FOUND"),
        (lambda c: c.post("/resolve", json={"txn_id": "bad id"}), 422, "INVALID_REQUEST"),
        (lambda c: c.post("/reconcile", json={"date_from": "2025-01-20", "date_to": "2025-01-10"}), 422, "INVALID_REQUEST"),
    ],
)
def test_every_error_response_agrees_with_its_header_on_one_request_id(call, status, code):
    client = TestClient(create_app(Settings(require_auth=False), InMemoryRepository()))

    supplied = call(client.__class__(client.app, headers={"x-request-id": "rid-supplied"}))
    assert supplied.status_code == status
    assert supplied.json()["error"]["code"] == code
    assert supplied.json()["error"]["request_id"] == "rid-supplied"
    assert supplied.headers["x-request-id"] == "rid-supplied"

    generated = call(client)
    body_id = generated.json()["error"]["request_id"]
    assert body_id and body_id == generated.headers["x-request-id"]


def test_unhandled_failures_return_a_stable_internal_error_with_a_request_id():
    class MalformedRepository:
        """Returns a resolve result the route cannot read: an application bug."""

        def resolve(self, txn_id, request_id="local-request", on_event=None):
            return {"status": "clean", "action": "no_action_needed", "explanation": "ok"}

    app = create_app(Settings(require_auth=False), MalformedRepository())
    # ServerErrorMiddleware responds and then re-raises, so the exception has to
    # be allowed through for the response to be observable at all.
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/resolve", json={"txn_id": "txn-1"}, headers={"x-request-id": "rid-bug"})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["error"]["request_id"] == "rid-bug"
    # The 500 handler runs outside every user middleware, so this header proves
    # the handler sets it itself rather than relying on the correlation wrapper.
    assert response.headers["x-request-id"] == "rid-bug"
    assert "run_id" not in response.text


def test_dependency_failures_stay_a_503_with_no_internal_detail():
    class BrokenRepository:
        def tickets(self, *_args, **_kwargs):
            raise OSError("database socket unavailable at 10.0.0.4:5432")

    client = TestClient(create_app(Settings(require_auth=False), BrokenRepository()))
    response = client.get("/tickets", headers={"x-request-id": "rid-dep"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert response.json()["error"]["request_id"] == "rid-dep"
    assert "socket" not in response.text
    assert "10.0.0.4" not in response.text


def test_logfire_scrubber_does_not_erase_our_own_redacted_attributes(exporter):
    """Its default patterns include a bare `auth`, matched against values too.

    Unhandled, that replaces the entire captured prompt with a scrub marker as
    soon as the system prompt says "authorized", and flattens the
    `not_authorized` tool result that is the whole reason to read the span.
    """
    with span("agent.tool.raise_ticket", **{"tool.result": {"status": "not_authorized"}, "groq.prompt": "you are authorized only to word it"}):
        pass

    [recorded] = [s for s in finished_spans(exporter) if s.name == "agent.tool.raise_ticket"]
    assert "not_authorized" in str(recorded.attributes["tool.result"])
    assert "authorized only to word it" in recorded.attributes["groq.prompt"]
    assert "Scrubbed" not in attributes_blob(exporter)


def test_logfire_scrubber_still_guards_attributes_we_did_not_redact(exporter):
    """The exemption is scoped to PayPilot's namespaces, not switched off."""
    with span("supabase.select") as active:
        active.set(**{"values": {"authorization": "Bearer live-token-value"}})

    blob = attributes_blob(exporter)
    assert "live-token-value" not in blob


# --------------------------------------------------------------------------- #
# One resolve, one trace
# --------------------------------------------------------------------------- #


def test_compare_records_span_carries_the_verdict(exporter):
    gateway = GatewayTransaction(
        txn_id="txn-1",
        amount=1420.75,
        currency="INR",
        status="captured",
        captured_at=default_reference_time(),
        customer_name="Asha Menon",
    )
    bank = BankSettlement(txn_id="txn-1", amount=1200.0, currency="INR", status="settled", utr="UTR99887766")

    verdict = compare_records(gateway, bank, None, default_reference_time(), txn_id="txn-1")

    assert verdict.match_status.value == "amount_mismatch"
    [recorded] = [s for s in finished_spans(exporter) if s.name == "reconciliation.compare_records"]
    assert recorded.attributes["reconciliation.match_status"] == "amount_mismatch"
    assert recorded.attributes["reconciliation.reason_code"] == "AMOUNT_DISAGREEMENT_ACROSS_SOURCES"
    assert recorded.attributes["reconciliation.confidence"] == verdict.confidence.value
    assert "bank.amount" in str(recorded.attributes["reconciliation.mismatched_fields"])
    # The verdict, and nothing from the records themselves.
    assert "Asha Menon" not in attributes_blob(exporter)


def test_one_resolve_is_one_followable_trace(exporter):
    client = FakeClient()
    client.tables["gateway_transactions"][0]["customer_name"] = "Asha Menon"
    client.tables["bank_settlements"][0]["utr"] = "UTR99887766"
    repo = SupabaseRepository(
        client,
        reference_time=default_reference_time(),
        orchestrator=GroqOrchestrator(_FakeGroq(), {}),
    )

    result = repo.resolve("txn-pending", "rid-trace")
    assert result["status"] == "pending"

    spans = finished_spans(exporter)
    names = {s.name for s in spans}

    # Every step of the run, in one trace.
    assert len({s.context.trace_id for s in spans}) == 1
    assert {"repository.resolve", "supabase.select", "supabase.upsert", "reconciliation.compare_records", "agent.run", "groq.chat_completion"} <= names

    [root] = [s for s in spans if s.name == "repository.resolve"]
    assert root.attributes["request_id"] == "rid-trace"
    assert root.attributes["paypilot.outcome"] == "completed"
    assert root.attributes["paypilot.match_status"] == "pending"
    assert root.attributes["paypilot.action"] == "no_action_needed"

    # The deterministic verdict is recorded before the model is called, and the
    # model's own output is recorded next to it. Together: the model was handed a
    # finished diagnosis and never voted on it.
    [verdict] = [s for s in spans if s.name == "reconciliation.compare_records"]
    [groq] = [s for s in spans if s.name == "groq.chat_completion"]
    assert verdict.start_time < groq.start_time
    assert verdict.attributes["reconciliation.match_status"] == "pending"
    assert "diagnosis" in groq.attributes["groq.prompt"]
    assert "pending" in groq.attributes["groq.prompt"]
    assert "Bank settlement is pending" in groq.attributes["groq.response"]
    assert groq.attributes["groq.attempts"] == 1

    blob = attributes_blob(exporter)
    for secret in SECRETS[:2]:
        assert secret not in blob, secret


def test_failed_resolve_is_classified_on_the_span(exporter):
    from backend.app.repository import TransactionNotFound

    repo = SupabaseRepository(FakeClient(), reference_time=default_reference_time())
    with pytest.raises(TransactionNotFound):
        repo.resolve("txn-absent", "rid-missing")

    [root] = [s for s in finished_spans(exporter) if s.name == "repository.resolve"]
    assert root.attributes["paypilot.outcome"] == "failed"
    assert root.attributes["paypilot.error_code"] == "TXN_NOT_FOUND"
    # A missing transaction is the caller's problem, not a dependency's or ours.
    assert root.attributes["paypilot.error_class"] == "user_error"
    assert root.attributes["request_id"] == "rid-missing"


def test_api_request_span_is_stamped_with_the_request_id(exporter):
    repo = InMemoryRepository(
        {"txn-1": {"transaction_id": "txn-1", "status": "clean", "explanation": "Matched", "action": "no_action_needed"}}
    )
    client = TestClient(create_app(Settings(require_auth=False), repo))

    response = client.post("/resolve", json={"txn_id": "txn-1"}, headers={"x-request-id": "rid-http"})

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "rid-http"
    request_spans = [s for s in finished_spans(exporter) if s.name.startswith("POST /resolve")]
    assert request_spans, [s.name for s in finished_spans(exporter)]
    assert all(s.attributes.get("request_id") == "rid-http" for s in request_spans)


class _FakeGroq:
    """A Groq client that answers once, with a valid final response."""

    def __init__(self) -> None:
        payload = json.dumps(
            {"explanation": "Bank settlement is pending inside the window.", "status": "pending", "action": "no_action_needed"}
        )

        class Completions:
            def create(self, **_kwargs):
                message = type("Message", (), {"content": payload, "tool_calls": None})()
                return type("Completion", (), {"choices": [type("Choice", (), {"message": message})()]})()

        self.chat = type("Chat", (), {"completions": Completions()})()

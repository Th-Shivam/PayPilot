"""SSE streaming tests for /resolve/stream (issue #25).

Covers: incremental events before completion, server ordering, Last-Event-ID
dedup on reconnect, unknown-txn 404, and the mid-stream failure path. No real
Groq or Supabase — a fake repository drives the event contract.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.repository import InMemoryRepository, TransactionNotFound
from backend.app.trace_events import (
    KIND_COMPLETION,
    KIND_DECISION,
    KIND_TOOL_RESULT,
    STATUS_ERROR,
    STATUS_OK,
    make_event,
)


class ScriptedRepository:
    """Yields a fixed event script so the endpoint can be tested in isolation."""

    def __init__(self, events: list[dict[str, Any]] | None = None, missing: bool = False):
        self._events = events
        self._missing = missing

    def iter_resolve(self, txn_id: str, request_id: str = "local-request") -> Iterator[dict[str, Any]]:
        if self._missing:
            raise TransactionNotFound(txn_id)
        run = "run-1"
        if self._events is not None:
            for event in self._events:
                yield event
            return
        n = 0

        def emit(kind: str, name: str, status: str, summary: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
            nonlocal n
            n += 1
            return make_event(run_id=run, txn_id=txn_id, step_number=n, kind=kind, name=name, status=status, summary=summary, detail=detail or {})

        yield emit("tool_start", "lookup_gateway", "pending", "Checking gateway...")
        yield emit(KIND_TOOL_RESULT, "lookup_gateway", STATUS_OK, "Gateway found")
        yield emit(KIND_TOOL_RESULT, "lookup_bank", STATUS_OK, "Bank settled")
        yield emit(KIND_TOOL_RESULT, "lookup_ledger", "not_found", "Ledger missing")
        yield emit(KIND_DECISION, "compare_records", STATUS_OK, "Diagnosis: ledger_gap")
        yield emit(
            KIND_COMPLETION, "resolve", STATUS_OK, "Resolution complete: ledger_gap",
            {"status": "ledger_gap", "explanation": "Ledger entry missing.", "action": "ledger_entry_created", "run_id": run, "created_at": "2025-01-15T12:00:00+00:00", "steps": []},
        )


def _client(repo: Any) -> TestClient:
    return TestClient(create_app(Settings(), repo))


def _parse_sse(text: str) -> list[dict[str, str]]:
    """Parse a raw SSE body into a list of {event, id, data} frames."""
    frames: list[dict[str, str]] = []
    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue
        frame: dict[str, str] = {}
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                frame["event"] = line[len("event:"):].strip()
            elif line.startswith("id:"):
                frame["id"] = line[len("id:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        frame["data"] = "\n".join(data_lines)
        frames.append(frame)
    return frames


def test_stream_emits_trace_events_then_done():
    client = _client(ScriptedRepository())
    with client.stream("GET", "/resolve/stream?txn_id=TXN1") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    frames = _parse_sse(body)
    trace = [f for f in frames if f["event"] == "trace"]
    done = [f for f in frames if f["event"] == "done"]

    # Multiple trace events, exactly one terminal done, in that order.
    assert len(trace) >= 5
    assert len(done) == 1
    assert frames[-1]["event"] == "done"

    done_payload = json.loads(done[0]["data"])
    assert done_payload["txn_id"] == "TXN1"
    assert done_payload["status"] == "ledger_gap"
    assert done_payload["action"] == "ledger_entry_created"
    assert done_payload["trace"]["run_id"] == "run-1"


def test_stream_events_are_in_server_order():
    client = _client(ScriptedRepository())
    with client.stream("GET", "/resolve/stream?txn_id=TXN1") as response:
        body = "".join(response.iter_text())
    ids = [int(f["id"]) for f in _parse_sse(body) if f.get("id")]
    assert ids == sorted(ids)
    assert ids == list(range(1, len(ids) + 1))


def test_last_event_id_skips_already_seen_steps():
    client = _client(ScriptedRepository())
    with client.stream("GET", "/resolve/stream?txn_id=TXN1", headers={"Last-Event-ID": "3"}) as response:
        body = "".join(response.iter_text())
    frames = _parse_sse(body)
    ids = [int(f["id"]) for f in frames if f.get("id")]
    # Nothing at or below 3 replays.
    assert all(i > 3 for i in ids)
    # The run still completes.
    assert any(f["event"] == "done" for f in frames)


def test_unknown_txn_is_404_not_a_stream():
    client = _client(ScriptedRepository(missing=True))
    response = client.get("/resolve/stream?txn_id=TXNMISSING")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TXN_NOT_FOUND"


def test_mid_stream_error_is_reported_as_done_error():
    good = make_event(run_id="r", txn_id="TXN1", step_number=1, kind=KIND_TOOL_RESULT, name="lookup_gateway", status=STATUS_OK, summary="ok")

    class Boom:
        def iter_resolve(self, txn_id: str, request_id: str = "local-request") -> Iterator[dict[str, Any]]:
            yield good
            raise RuntimeError("bank feed exploded")

    client = _client(Boom())
    with client.stream("GET", "/resolve/stream?txn_id=TXN1") as response:
        assert response.status_code == 200  # already committed to the stream
        body = "".join(response.iter_text())
    frames = _parse_sse(body)
    # The one good event streamed, then an error surfaced on the done channel.
    assert frames[0]["event"] == "trace"
    assert frames[-1]["event"] == "done"
    assert json.loads(frames[-1]["data"])["error"]["code"] == "STREAM_ERROR"


def test_inmemory_repository_streams_from_stored_record():
    repo = InMemoryRepository({"txn-1": {"transaction_id": "txn-1", "status": "clean", "explanation": "Matched", "action": "no_action_needed"}})
    client = _client(repo)
    with client.stream("GET", "/resolve/stream?txn_id=txn-1") as response:
        body = "".join(response.iter_text())
    frames = _parse_sse(body)
    done = json.loads([f for f in frames if f["event"] == "done"][0]["data"])
    assert done["status"] == "clean"
    # Streaming also records a trace that /trace can serve afterwards.
    trace = client.get("/trace/txn-1")
    assert trace.status_code == 200
    assert trace.json()["run_id"]

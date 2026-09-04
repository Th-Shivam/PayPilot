"""Trace event contract and SSE plumbing for streaming resolution (issue #25).

The event shape here is the contract shared with the frontend panel (#24):

    {
      "run_id": "uuid",
      "txn_id": "TXN...",
      "step_number": 1,
      "kind": tool_start | tool_result | decision | action | retry | completion,
      "name": "lookup_gateway",
      "status": ok | not_found | warning | error | pending,
      "summary": "Checked gateway -> found, captured",
      "detail": { ... }
    }

`kind` and `status` are fixed enums. Adding a value is a contract change and
needs a heads-up on #24.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Callable, Iterator

# Event kinds, in the order they typically occur within a run.
KIND_TOOL_START = "tool_start"
KIND_TOOL_RESULT = "tool_result"
KIND_DECISION = "decision"
KIND_ACTION = "action"
KIND_RETRY = "retry"
KIND_COMPLETION = "completion"

EVENT_KINDS = frozenset({
    KIND_TOOL_START, KIND_TOOL_RESULT, KIND_DECISION,
    KIND_ACTION, KIND_RETRY, KIND_COMPLETION,
})

# Event statuses. `pending` marks an in-flight tool_start; the rest are outcomes.
STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
STATUS_WARNING = "warning"
STATUS_ERROR = "error"
STATUS_PENDING = "pending"

EVENT_STATUSES = frozenset({
    STATUS_OK, STATUS_NOT_FOUND, STATUS_WARNING, STATUS_ERROR, STATUS_PENDING,
})

# The subset agent_trace_logs.step_status accepts (see migration 0003). Events
# whose status is outside this set (pending) are streamed but not persisted.
PERSISTABLE_STATUSES = frozenset({
    STATUS_OK, STATUS_NOT_FOUND, "skipped", STATUS_WARNING, STATUS_ERROR,
})

# SSE event-type names on the wire.
SSE_TRACE = "trace"
SSE_DONE = "done"
SSE_ERROR = "error"


def make_event(
    *,
    run_id: str,
    txn_id: str,
    step_number: int,
    kind: str,
    name: str,
    status: str,
    summary: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one trace event, validating the enums so drift fails fast."""
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind: {kind!r}")
    if status not in EVENT_STATUSES:
        raise ValueError(f"unknown event status: {status!r}")
    return {
        "run_id": run_id,
        "txn_id": txn_id,
        "step_number": step_number,
        "kind": kind,
        "name": name,
        "status": status,
        "summary": summary,
        "detail": detail or {},
    }


def as_trace_step(event: dict[str, Any]) -> dict[str, Any]:
    """Project an event onto the agent_trace_logs / TraceMetadata step shape."""
    return {
        "run_id": event["run_id"],
        "txn_id": event["txn_id"],
        "step_number": event["step_number"],
        "step_name": f"{event['kind']}:{event['name']}",
        "step_status": event["status"] if event["status"] in PERSISTABLE_STATUSES else "skipped",
        "step_result": event["summary"],
        "detail": event["detail"],
    }


def format_sse(data: str, *, event: str | None = None, event_id: int | None = None) -> str:
    """Format a single SSE frame. Multi-line data is split per the SSE spec."""
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if event is not None:
        lines.append(f"event: {event}")
    for line in (data.split("\n") or [""]):
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


def sse_json(payload: dict[str, Any], *, event: str, event_id: int | None = None) -> str:
    return format_sse(json.dumps(payload, default=str), event=event, event_id=event_id)


async def aiter_sync(
    gen_factory: Callable[[], Iterator[dict[str, Any]]],
) -> AsyncIterator[tuple[str, Any]]:
    """Drive a blocking sync generator from async code without stalling the loop.

    The generator is run in a worker thread; items are handed back through an
    asyncio.Queue. This is what lets events flush incrementally instead of all
    arriving at once when a blocking coroutine finally yields.

    Yields ("event", item) for each produced item, or ("error", exception) if
    the generator raises. Always terminates.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    done = object()

    def produce() -> None:
        try:
            for item in gen_factory():
                loop.call_soon_threadsafe(queue.put_nowait, ("event", item))
        except BaseException as exc:  # noqa: BLE001 - surfaced to the consumer
            loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, ("__done__", done))

    task = loop.run_in_executor(None, produce)
    try:
        while True:
            kind, payload = await queue.get()
            if kind == "__done__":
                break
            yield kind, payload
    finally:
        await task

"""Canonical trace events shared by persistence, API, and the UI stream."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


TraceEventType = Literal[
    "tool_start",
    "tool_result",
    "decision",
    "action",
    "retry",
    "completion",
]
TraceEventStatus = Literal[
    "running",
    "success",
    "warning",
    "not_found",
    "failed",
    "completed",
]


class TraceEvent(BaseModel):
    """One ordered, replayable event from a resolution run."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    transaction_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    step_number: int = Field(ge=1)
    event_type: TraceEventType
    step_name: str = Field(min_length=1)
    status: TraceEventStatus
    summary: str = Field(min_length=1)
    detail: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime

    @classmethod
    def create(
        cls,
        *,
        transaction_id: str,
        run_id: str,
        request_id: str,
        step_number: int,
        event_type: TraceEventType,
        step_name: str,
        status: TraceEventStatus,
        summary: str,
        detail: dict[str, Any] | None = None,
    ) -> "TraceEvent":
        return cls(
            event_id=f"{run_id}:{step_number}",
            transaction_id=transaction_id,
            run_id=run_id,
            request_id=request_id,
            step_number=step_number,
            event_type=event_type,
            step_name=step_name,
            status=status,
            summary=summary,
            detail=detail or {},
            timestamp=datetime.now(timezone.utc),
        )

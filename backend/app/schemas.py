from __future__ import annotations

from datetime import date, datetime
from typing import Any, Self
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ResolveRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    txn_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    transaction_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")

    @model_validator(mode="after")
    def validate_id(self) -> Self:
        if not self.txn_id and not self.transaction_id:
            raise ValueError("Either txn_id or transaction_id is required")
        if not self.txn_id:
            self.txn_id = self.transaction_id
        if not self.transaction_id:
            self.transaction_id = self.txn_id
        return self


class TraceStep(BaseModel):
    step_number: int
    step_name: str
    step_status: str
    step_result: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class TraceMetadata(BaseModel):
    request_id: str
    run_id: str
    created_at: datetime
    steps: list[TraceStep] = Field(default_factory=list)


class ResolveResponse(BaseModel):
    txn_id: str
    transaction_id: str | None = None
    status: str
    explanation: str
    action: str
    trace: TraceMetadata


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class TicketResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    txn_id: str | None = None
    transaction_id: str | None = None
    diagnosis: str | None = None
    status: str | None = None
    explanation: str
    action_taken: str = "no_action_needed"
    confidence: str | float | None = None

    @model_validator(mode="after")
    def populate_aliases(self) -> Self:
        if not self.txn_id and self.transaction_id:
            self.txn_id = self.transaction_id
        if not self.transaction_id and self.txn_id:
            self.transaction_id = self.txn_id
        if not self.diagnosis and self.status:
            self.diagnosis = self.status
        if not self.status and self.diagnosis:
            self.status = self.diagnosis
        return self


class AnalyticsResponse(BaseModel):
    by_action: dict[str, int]
    by_confidence: dict[str, int]


class ReconcileRequest(BaseModel):
    date_from: date
    date_to: date


class ReconcileResponse(BaseModel):
    date_from: date
    date_to: date
    results: list[ResolveResponse]

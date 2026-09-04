"""Typed records shared by fixture generation and reconciliation."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ResolutionPath(StrEnum):
    CLEAN = "clean"
    LEDGER_GAP = "ledger_gap"
    PENDING = "pending"
    ANOMALY = "anomaly"
    AMOUNT_MISMATCH = "amount_mismatch"
    DUPLICATE = "duplicate"
    ALREADY_RESOLVED = "already_resolved"


class RecordBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_id: str = Field(min_length=1)
    amount: float = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    occurred_at: datetime


class GatewayRecord(RecordBase):
    status: str = "captured"


class BankSettlementRecord(RecordBase):
    status: str
    settled_at: datetime | None = None


class LedgerRecord(RecordBase):
    recorded_at: datetime
    status: str = "recorded"


class TicketRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(min_length=1)
    transaction_id: str = Field(min_length=1)
    status: str
    explanation: str = Field(min_length=1)
    resolution_path: ResolutionPath


class FixtureCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_id: str = Field(min_length=1)
    path: ResolutionPath
    gateway: GatewayRecord
    bank: BankSettlementRecord | None = None
    ledger: LedgerRecord | None = None
    expected_settlement_at: datetime

"""Typed records mirroring the live Supabase schema.

Field names match the database exactly (see supabase/migrations/0002, 0003) so
records round-trip without a translation layer.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MatchStatus(StrEnum):
    """Deterministic diagnosis outcomes.

    Mirrors the tickets_diagnosis_check constraint. UNKNOWN is the honest
    fallback for states no rule recognises, and routes to the exception list
    rather than being reported as a confirmed anomaly.
    """

    CLEAN = "clean"
    LEDGER_GAP = "ledger_gap"
    PENDING = "pending"
    ANOMALY = "anomaly"
    AMOUNT_MISMATCH = "amount_mismatch"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    """Diagnostic certainty. Mirrors tickets_confidence_check."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW_FLAGGED_FOR_REVIEW = "low_flagged_for_review"


class ActionTaken(StrEnum):
    """Mirrors tickets_action_taken_check."""

    AUTO_RESOLVED = "auto_resolved"
    LEDGER_ENTRY_CREATED = "ledger_entry_created"
    ESCALATED = "escalated"
    NO_ACTION_NEEDED = "no_action_needed"


# Confidence is a property of the diagnosis, not of whether the case can be
# auto-fixed. AMOUNT_MISMATCH is high confidence: an exact field discrepancy is
# certain. It escalates because it is unsafe to fix automatically, not because
# the finding is in doubt.
#
# ANOMALY is the genuinely uncertain case. A missing bank record past T+2 could
# be bank lag, a failed payout, or a gap in the data feed, and the three sources
# cannot distinguish between them.
CONFIDENCE_BY_STATUS: dict[MatchStatus, Confidence] = {
    MatchStatus.CLEAN: Confidence.HIGH,
    MatchStatus.PENDING: Confidence.HIGH,
    MatchStatus.LEDGER_GAP: Confidence.HIGH,
    MatchStatus.AMOUNT_MISMATCH: Confidence.HIGH,
    MatchStatus.ANOMALY: Confidence.LOW_FLAGGED_FOR_REVIEW,
    MatchStatus.UNKNOWN: Confidence.LOW_FLAGGED_FOR_REVIEW,
}


class GatewayTransaction(BaseModel):
    """A row of gateway_transactions."""

    model_config = ConfigDict(extra="forbid")

    txn_id: str = Field(min_length=1)
    amount: float = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3, default="INR")
    status: str = "captured"
    captured_at: datetime
    # Persisted at generation time so the settlement window is a comparison of
    # two stored values, never a function of wall-clock at query time.
    expected_settlement_at: datetime | None = None
    customer_name: str | None = None


class BankSettlement(BaseModel):
    """A row of bank_settlements."""

    model_config = ConfigDict(extra="forbid")

    txn_id: str = Field(min_length=1)
    amount: float | None = Field(default=None, gt=0)
    currency: str = Field(min_length=3, max_length=3, default="INR")
    status: str | None = None
    settled_at: datetime | None = None
    utr: str | None = None


class LedgerEntry(BaseModel):
    """A row of ledger_entries.

    There is no 'missing' status: a missing entry is the absence of a row.
    """

    model_config = ConfigDict(extra="forbid")

    txn_id: str = Field(min_length=1)
    amount: float | None = Field(default=None, gt=0)
    currency: str = Field(min_length=3, max_length=3, default="INR")
    status: str = "recorded"
    recorded_at: datetime | None = None
    source: str = "system"


class Diagnosis(BaseModel):
    """Output of compare_records. The deterministic verdict.

    The LLM receives this as read-only context and may not alter match_status or
    confidence. Every factual claim in a generated explanation must correspond
    to something in `detail`.
    """

    model_config = ConfigDict(extra="forbid")

    txn_id: str
    match_status: MatchStatus
    confidence: Confidence
    reason_code: str
    detail: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_exception(self) -> bool:
        """True when this case belongs on the human review list."""
        return self.confidence is Confidence.LOW_FLAGGED_FOR_REVIEW


class FixtureCase(BaseModel):
    """One generated transaction plus the path it was built to exercise."""

    model_config = ConfigDict(extra="forbid")

    txn_id: str = Field(min_length=1)
    path: MatchStatus
    gateway: GatewayTransaction
    bank: BankSettlement | None = None
    ledger: LedgerEntry | None = None

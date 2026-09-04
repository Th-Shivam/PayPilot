"""Deterministic reconciliation logic. The core of PS-8's hard constraint.

`_compare_records` calls no LLM, touches no network, and never reads the clock.
Given the same three records and the same reference time, the verdict is always
identical. The LLM's only job downstream is wording the result.

`compare_records` is a thin traced wrapper over it. The span exists so the
verdict — match_status, confidence, reason_code — is visible in Logfire
*upstream* of the Groq span that follows: the decision is already made, by code,
before the model is handed anything. Tracing only observes; disabling it changes
nothing about the result.

Precedence, highest first:

    1. amount_mismatch  A value discrepancy outranks any status agreement. Two
                        sources disagreeing on money is the finding, regardless
                        of what the statuses say.
    2. gateway not captured
                        A failed or unstarted payment explains the absence of
                        settlement. Reporting this as an anomaly would be the
                        system flagging correct behaviour as a fault.
    3. anomaly          Bank has no record and the settlement window has closed.
    4. pending          Bank has no record, or reports pending, and the window
                        is still open.
    5. ledger_gap       Gateway and bank agree; the ledger row is absent.
    6. clean            All three agree.
    7. unknown          No rule matched. Routes to human review rather than
                        being asserted as an anomaly.
"""

from datetime import datetime, timedelta, timezone

from backend.domain.models import (
    CONFIDENCE_BY_STATUS,
    BankSettlement,
    Diagnosis,
    GatewayTransaction,
    LedgerEntry,
    MatchStatus,
)
from backend.observability import span

# Amounts are numeric(14,2) in Postgres. A tolerance of half a paisa absorbs
# float representation error without masking a real discrepancy.
AMOUNT_TOLERANCE = 0.005

DEFAULT_SETTLEMENT_WINDOW = timedelta(days=2)

# Statuses that mean the payment succeeded and settlement is expected.
CAPTURED_STATUSES = frozenset({"captured"})


def _amounts_match(left: float | None, right: float | None) -> bool:
    """None on either side is not a mismatch, only an absence of evidence."""
    if left is None or right is None:
        return True
    return abs(left - right) <= AMOUNT_TOLERANCE


def _mismatched_amount_fields(
    gateway: GatewayTransaction,
    bank: BankSettlement | None,
    ledger: LedgerEntry | None,
) -> list[str]:
    """Name the exact sources disagreeing with the gateway on amount."""
    fields: list[str] = []
    if bank is not None and not _amounts_match(gateway.amount, bank.amount):
        fields.append("bank.amount")
    if ledger is not None and not _amounts_match(gateway.amount, ledger.amount):
        fields.append("ledger.amount")
    return fields


def _settlement_deadline(
    gateway: GatewayTransaction,
    window: timedelta,
) -> datetime:
    """Prefer the persisted deadline; derive one only when it is absent."""
    if gateway.expected_settlement_at is not None:
        return gateway.expected_settlement_at
    return gateway.captured_at + window


def compare_records(
    gateway: GatewayTransaction | None,
    bank: BankSettlement | None,
    ledger: LedgerEntry | None,
    reference_time: datetime,
    txn_id: str | None = None,
    settlement_window: timedelta = DEFAULT_SETTLEMENT_WINDOW,
) -> Diagnosis:
    """Return the deterministic diagnosis for one transaction, traced.

    The span carries the verdict and nothing else: no record fields, so no
    customer identity crosses the boundary here. `Diagnosis.detail` does embed
    the source records, and it reaches Logfire only via the redacted repository
    span downstream.
    """
    with span("reconciliation.compare_records") as active:
        verdict = _compare_records(
            gateway, bank, ledger, reference_time, txn_id, settlement_window
        )
        active.set(
            **{
                "reconciliation.txn_id": verdict.txn_id,
                "reconciliation.match_status": verdict.match_status.value,
                "reconciliation.confidence": verdict.confidence.value,
                "reconciliation.reason_code": verdict.reason_code,
                "reconciliation.mismatched_fields": verdict.detail.get("mismatched_fields", []),
                "reconciliation.sources_present": [
                    name
                    for name, record in (("gateway", gateway), ("bank", bank), ("ledger", ledger))
                    if record is not None
                ],
            }
        )
        return verdict


def _compare_records(
    gateway: GatewayTransaction | None,
    bank: BankSettlement | None,
    ledger: LedgerEntry | None,
    reference_time: datetime,
    txn_id: str | None = None,
    settlement_window: timedelta = DEFAULT_SETTLEMENT_WINDOW,
) -> Diagnosis:
    """The pure rule engine. Every rule test exercises this through the wrapper.

    `reference_time` is passed in rather than read from the clock so the result
    is reproducible and testable.
    """
    resolved_txn_id = txn_id or (
        gateway.txn_id if gateway else bank.txn_id if bank else ledger.txn_id if ledger else ""
    )

    def build(
        status: MatchStatus,
        reason_code: str,
        **extra: object,
    ) -> Diagnosis:
        detail: dict[str, object] = {
            "gateway": gateway.model_dump(mode="json") if gateway else None,
            "bank": bank.model_dump(mode="json") if bank else None,
            "ledger": ledger.model_dump(mode="json") if ledger else None,
            "reference_time": reference_time.isoformat(),
        }
        detail.update(extra)
        detail.setdefault("mismatched_fields", [])
        return Diagnosis(
            txn_id=resolved_txn_id,
            match_status=status,
            confidence=CONFIDENCE_BY_STATUS[status],
            reason_code=reason_code,
            detail=detail,
        )

    # No gateway record: nothing anchors the transaction. A ledger or bank row
    # without a gateway capture is an orphan and needs a human.
    if gateway is None:
        if bank is None and ledger is None:
            return build(MatchStatus.UNKNOWN, "NO_RECORDS_FOUND")
        return build(MatchStatus.UNKNOWN, "ORPHAN_RECORD_NO_GATEWAY")

    deadline = _settlement_deadline(gateway, settlement_window)
    window_closed = reference_time > deadline

    # 1. Amount discrepancies outrank everything. Certain finding, unsafe to fix.
    mismatched = _mismatched_amount_fields(gateway, bank, ledger)
    if mismatched:
        return build(
            MatchStatus.AMOUNT_MISMATCH,
            "AMOUNT_DISAGREEMENT_ACROSS_SOURCES",
            mismatched_fields=mismatched,
            gateway_amount=gateway.amount,
            bank_amount=bank.amount if bank else None,
            ledger_amount=ledger.amount if ledger else None,
        )

    # 2. The payment never succeeded, so absent settlement is correct.
    if gateway.status not in CAPTURED_STATUSES:
        return build(
            MatchStatus.UNKNOWN,
            f"GATEWAY_NOT_CAPTURED_{gateway.status.upper()}",
            gateway_status=gateway.status,
            settlement_expected=False,
        )

    # A reversal is a real, explainable outcome and must not be auto-closed.
    if bank is not None and bank.status == "reversed":
        return build(
            MatchStatus.ANOMALY,
            "BANK_SETTLEMENT_REVERSED",
            bank_status=bank.status,
        )

    # 3/4. No bank record at all: anomaly once the window closes, else pending.
    if bank is None:
        if window_closed:
            return build(
                MatchStatus.ANOMALY,
                "BANK_NO_RECORD_PAST_SETTLEMENT_WINDOW",
                expected_settlement_at=deadline.isoformat(),
            )
        return build(
            MatchStatus.PENDING,
            "BANK_NO_RECORD_WITHIN_SETTLEMENT_WINDOW",
            expected_settlement_at=deadline.isoformat(),
        )

    # Bank still processing: pending inside the window, anomaly past it.
    if bank.status == "pending":
        if window_closed:
            return build(
                MatchStatus.ANOMALY,
                "BANK_PENDING_PAST_SETTLEMENT_WINDOW",
                expected_settlement_at=deadline.isoformat(),
            )
        return build(
            MatchStatus.PENDING,
            "BANK_PENDING_WITHIN_SETTLEMENT_WINDOW",
            expected_settlement_at=deadline.isoformat(),
        )

    if bank.status == "settled":
        # 5. Gateway and bank agree, ledger row absent. Safely auto-fixable.
        if ledger is None:
            return build(
                MatchStatus.LEDGER_GAP,
                "LEDGER_ENTRY_ABSENT_DESPITE_SETTLEMENT",
                settled_at=bank.settled_at.isoformat() if bank.settled_at else None,
                recoverable_amount=gateway.amount,
            )
        # 6. All three agree.
        return build(
            MatchStatus.CLEAN,
            "ALL_SOURCES_AGREE",
            settled_at=bank.settled_at.isoformat() if bank.settled_at else None,
        )

    # 7. Unrecognised bank status. Do not guess.
    return build(
        MatchStatus.UNKNOWN,
        "BANK_STATUS_UNRECOGNISED",
        bank_status=bank.status,
    )


def duplicate_txn_ids(txn_ids: list[str]) -> set[str]:
    """IDs appearing more than once in a source feed.

    The live schema enforces unique (txn_id) per table, so duplicates can only
    arise in an inbound file. Detecting them before load turns a constraint
    violation into an actionable report.
    """
    seen: set[str] = set()
    duplicates: set[str] = set()
    for txn_id in txn_ids:
        if txn_id in seen:
            duplicates.add(txn_id)
        seen.add(txn_id)
    return duplicates


def default_reference_time() -> datetime:
    """Fixed clock for reproducible fixtures and tests."""
    return datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc)

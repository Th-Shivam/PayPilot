"""Wall-clock-independent, deterministic reconciliation rules."""

from datetime import datetime, timedelta, timezone

from backend.domain.models import FixtureCase, ResolutionPath

AMOUNT_TOLERANCE_ABSOLUTE = 0.01
TIMESTAMP_TOLERANCE = timedelta(minutes=5)


def duplicate_transaction_ids(transaction_ids: list[str]) -> set[str]:
    """Return IDs repeated in a source stream, without changing source records."""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for transaction_id in transaction_ids:
        if transaction_id in seen:
            duplicates.add(transaction_id)
        seen.add(transaction_id)
    return duplicates


def _amounts_match(left: float, right: float) -> bool:
    return abs(left - right) <= AMOUNT_TOLERANCE_ABSOLUTE


def classify_transaction(case: FixtureCase, reference_time: datetime) -> ResolutionPath:
    """Classify using persisted expected_settlement_at, never datetime.now()."""
    gateway = case.gateway
    bank = case.bank
    ledger = case.ledger
    if bank and not _amounts_match(gateway.amount, bank.amount):
        return ResolutionPath.AMOUNT_MISMATCH
    if ledger and not _amounts_match(gateway.amount, ledger.amount):
        return ResolutionPath.AMOUNT_MISMATCH
    if bank and abs((bank.occurred_at - gateway.occurred_at)) > TIMESTAMP_TOLERANCE:
        return ResolutionPath.ANOMALY
    if ledger and abs((ledger.occurred_at - gateway.occurred_at)) > TIMESTAMP_TOLERANCE:
        return ResolutionPath.ANOMALY
    if ledger and bank and bank.status == "settled":
        return ResolutionPath.CLEAN
    if bank and bank.status == "settled" and ledger is None:
        return ResolutionPath.LEDGER_GAP
    if bank and bank.status == "pending" and reference_time <= case.expected_settlement_at:
        return ResolutionPath.PENDING
    if bank is None and reference_time > case.expected_settlement_at:
        return ResolutionPath.ANOMALY
    return ResolutionPath.ANOMALY


def default_reference_time() -> datetime:
    return datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc)

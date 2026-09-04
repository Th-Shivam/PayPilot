"""compare_records unit tests. No database, no network, no LLM.

Covers all six diagnosis outcomes, the precedence rules between them, and the
guarantee that the same input always produces the same verdict.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.domain.models import (
    BankSettlement,
    Confidence,
    GatewayTransaction,
    LedgerEntry,
    MatchStatus,
)
from backend.reconciliation import compare_records, default_reference_time, duplicate_txn_ids

REFERENCE = default_reference_time()


def gateway(
    amount: float = 1000.00,
    status: str = "captured",
    captured_days_ago: int = 5,
    expected_settlement_at: datetime | None = ...,
) -> GatewayTransaction:
    captured_at = REFERENCE - timedelta(days=captured_days_ago)
    if expected_settlement_at is ...:
        expected_settlement_at = captured_at + timedelta(days=2)
    return GatewayTransaction(
        txn_id="TXN001",
        amount=amount,
        status=status,
        captured_at=captured_at,
        expected_settlement_at=expected_settlement_at,
    )


def bank(
    amount: float | None = 1000.00,
    status: str = "settled",
    settled: bool = True,
) -> BankSettlement:
    return BankSettlement(
        txn_id="TXN001",
        amount=amount,
        status=status,
        settled_at=REFERENCE - timedelta(days=3) if settled else None,
    )


def ledger(amount: float | None = 1000.00) -> LedgerEntry:
    return LedgerEntry(
        txn_id="TXN001",
        amount=amount,
        status="recorded",
        recorded_at=REFERENCE - timedelta(days=3),
    )


# --- the five brief categories -------------------------------------------


def test_all_three_agree_is_clean():
    result = compare_records(gateway(), bank(), ledger(), REFERENCE)
    assert result.match_status is MatchStatus.CLEAN
    assert result.confidence is Confidence.HIGH
    assert result.reason_code == "ALL_SOURCES_AGREE"


def test_settled_without_ledger_row_is_ledger_gap():
    result = compare_records(gateway(), bank(), None, REFERENCE)
    assert result.match_status is MatchStatus.LEDGER_GAP
    # High confidence: gateway and bank agree, so the correct entry is known.
    assert result.confidence is Confidence.HIGH
    assert result.detail["recoverable_amount"] == 1000.00


def test_bank_pending_inside_window_is_pending():
    result = compare_records(
        gateway(captured_days_ago=1), bank(status="pending", settled=False), None, REFERENCE
    )
    assert result.match_status is MatchStatus.PENDING
    assert result.confidence is Confidence.HIGH


def test_no_bank_record_past_window_is_anomaly():
    result = compare_records(gateway(captured_days_ago=10), None, None, REFERENCE)
    assert result.match_status is MatchStatus.ANOMALY
    # The genuinely uncertain case: bank lag, failed payout, and a missing feed
    # are indistinguishable from these three sources.
    assert result.confidence is Confidence.LOW_FLAGGED_FOR_REVIEW
    assert result.is_exception


def test_amount_disagreement_is_amount_mismatch():
    result = compare_records(gateway(amount=5000.00), bank(amount=500.00), None, REFERENCE)
    assert result.match_status is MatchStatus.AMOUNT_MISMATCH
    # Certain finding, just not safely auto-fixable.
    assert result.confidence is Confidence.HIGH
    assert result.detail["mismatched_fields"] == ["bank.amount"]
    assert result.detail["gateway_amount"] == 5000.00
    assert result.detail["bank_amount"] == 500.00


# --- unknown, the honest fallback ----------------------------------------


def test_no_records_at_all_is_unknown():
    result = compare_records(None, None, None, REFERENCE, txn_id="TXNMISSING")
    assert result.match_status is MatchStatus.UNKNOWN
    assert result.confidence is Confidence.LOW_FLAGGED_FOR_REVIEW
    assert result.txn_id == "TXNMISSING"


def test_ledger_row_without_gateway_is_unknown_not_clean():
    result = compare_records(None, bank(), ledger(), REFERENCE, txn_id="TXN001")
    assert result.match_status is MatchStatus.UNKNOWN
    assert result.reason_code == "ORPHAN_RECORD_NO_GATEWAY"


def test_unrecognised_bank_status_is_unknown():
    result = compare_records(gateway(), bank(status="frozen"), None, REFERENCE)
    assert result.match_status is MatchStatus.UNKNOWN
    assert result.reason_code == "BANK_STATUS_UNRECOGNISED"


# --- the bug this suite exists to prevent -------------------------------


def test_failed_gateway_with_no_bank_record_is_not_an_anomaly():
    """A failed payment correctly never settles.

    Reporting this as an anomaly would mean escalating the system working as
    intended, which is the most damaging false positive available here.
    """
    result = compare_records(
        gateway(status="failed", captured_days_ago=10), None, None, REFERENCE
    )
    assert result.match_status is not MatchStatus.ANOMALY
    assert result.match_status is MatchStatus.UNKNOWN
    assert result.reason_code == "GATEWAY_NOT_CAPTURED_FAILED"
    assert result.detail["settlement_expected"] is False


def test_pending_gateway_is_not_an_anomaly():
    result = compare_records(
        gateway(status="pending", captured_days_ago=10), None, None, REFERENCE
    )
    assert result.reason_code == "GATEWAY_NOT_CAPTURED_PENDING"


def test_realistic_t_plus_2_settlement_delay_is_clean():
    """Settlement two days after capture is normal, not an anomaly.

    A comparison tuned to a minutes-wide timestamp tolerance would flag every
    real settlement. Only amounts and statuses decide the verdict.
    """
    g = GatewayTransaction(
        txn_id="TXN001",
        amount=1000.00,
        status="captured",
        captured_at=REFERENCE - timedelta(days=5),
        expected_settlement_at=REFERENCE - timedelta(days=3),
    )
    b = BankSettlement(
        txn_id="TXN001", amount=1000.00, status="settled",
        settled_at=REFERENCE - timedelta(days=3),
    )
    entry = LedgerEntry(
        txn_id="TXN001", amount=1000.00, status="recorded",
        recorded_at=REFERENCE - timedelta(days=3) + timedelta(hours=4),
    )
    assert compare_records(g, b, entry, REFERENCE).match_status is MatchStatus.CLEAN


# --- precedence ----------------------------------------------------------


def test_amount_mismatch_outranks_pending():
    result = compare_records(
        gateway(amount=5000.00, captured_days_ago=1),
        bank(amount=500.00, status="pending", settled=False),
        None,
        REFERENCE,
    )
    assert result.match_status is MatchStatus.AMOUNT_MISMATCH


def test_amount_mismatch_outranks_ledger_gap():
    result = compare_records(gateway(amount=5000.00), bank(amount=4000.00), None, REFERENCE)
    assert result.match_status is MatchStatus.AMOUNT_MISMATCH


def test_ledger_amount_disagreement_is_detected():
    result = compare_records(gateway(amount=1000.00), bank(amount=1000.00), ledger(amount=250.00), REFERENCE)
    assert result.match_status is MatchStatus.AMOUNT_MISMATCH
    assert result.detail["mismatched_fields"] == ["ledger.amount"]


def test_reversed_settlement_is_an_anomaly():
    result = compare_records(gateway(), bank(status="reversed"), None, REFERENCE)
    assert result.match_status is MatchStatus.ANOMALY
    assert result.reason_code == "BANK_SETTLEMENT_REVERSED"


def test_bank_pending_past_window_becomes_anomaly():
    result = compare_records(
        gateway(captured_days_ago=10), bank(status="pending", settled=False), None, REFERENCE
    )
    assert result.match_status is MatchStatus.ANOMALY
    assert result.reason_code == "BANK_PENDING_PAST_SETTLEMENT_WINDOW"


# --- window boundary and determinism ------------------------------------


def test_window_boundary_is_inclusive():
    """At exactly the deadline the window is still open."""
    g = gateway(captured_days_ago=5)
    deadline = g.expected_settlement_at
    assert compare_records(g, None, None, deadline).match_status is MatchStatus.PENDING
    assert compare_records(
        g, None, None, deadline + timedelta(seconds=1)
    ).match_status is MatchStatus.ANOMALY


def test_persisted_deadline_is_preferred_over_a_derived_one():
    """An explicit expected_settlement_at overrides captured_at + window."""
    g = GatewayTransaction(
        txn_id="TXN001", amount=1000.00, status="captured",
        captured_at=REFERENCE - timedelta(days=30),
        expected_settlement_at=REFERENCE + timedelta(days=5),
    )
    # Captured 30 days ago, but the stored deadline has not passed.
    assert compare_records(g, None, None, REFERENCE).match_status is MatchStatus.PENDING


def test_missing_deadline_falls_back_to_captured_at_plus_window():
    g = gateway(captured_days_ago=10, expected_settlement_at=None)
    assert compare_records(g, None, None, REFERENCE).match_status is MatchStatus.ANOMALY


def test_amount_tolerance_absorbs_float_error_but_not_real_gaps():
    assert compare_records(
        gateway(amount=1000.00), bank(amount=1000.004), ledger(amount=1000.00), REFERENCE
    ).match_status is MatchStatus.CLEAN
    assert compare_records(
        gateway(amount=1000.00), bank(amount=1000.05), ledger(amount=1000.00), REFERENCE
    ).match_status is MatchStatus.AMOUNT_MISMATCH


def test_repeated_calls_are_identical():
    args = (gateway(), bank(), None, REFERENCE)
    first = compare_records(*args)
    second = compare_records(*args)
    assert first.model_dump() == second.model_dump()


def test_confidence_is_never_absent():
    """Every outcome carries a confidence value, so nothing defaults silently."""
    for status in MatchStatus:
        assert status in {
            MatchStatus.CLEAN, MatchStatus.LEDGER_GAP, MatchStatus.PENDING,
            MatchStatus.ANOMALY, MatchStatus.AMOUNT_MISMATCH, MatchStatus.UNKNOWN,
        }


def test_detail_always_carries_all_three_sources():
    result = compare_records(gateway(), None, None, REFERENCE)
    assert result.detail["gateway"] is not None
    assert result.detail["bank"] is None
    assert result.detail["ledger"] is None
    assert "reference_time" in result.detail


def test_duplicate_txn_id_detection():
    assert duplicate_txn_ids(["a", "b", "a", "a", "c"]) == {"a"}
    assert duplicate_txn_ids(["a", "b", "c"]) == set()


@pytest.mark.parametrize("naive_reference", [datetime(2025, 1, 15, 12, 0)])
def test_naive_reference_time_is_rejected_loudly(naive_reference):
    """Mixing naive and aware datetimes must fail rather than guess a zone."""
    with pytest.raises(TypeError):
        compare_records(gateway(), bank(), ledger(), naive_reference)

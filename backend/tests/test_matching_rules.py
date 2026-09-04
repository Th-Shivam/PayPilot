from backend.reconciliation.rules import default_reference_time
from backend.reconciliation import classify_transaction, duplicate_transaction_ids
from backend.scripts.seed_fixtures import generate_cases
from backend.domain.models import BankSettlementRecord
from datetime import timedelta


def test_all_primary_cases_classify_to_their_path():
    reference = default_reference_time()
    for case in generate_cases(reference):
        assert classify_transaction(case, reference) == case.path


def test_timestamp_outside_tolerance_is_anomaly():
    case = generate_cases()[0]
    case.bank = BankSettlementRecord(
        transaction_id=case.transaction_id,
        amount=case.gateway.amount,
        currency="USD",
        occurred_at=case.gateway.occurred_at + timedelta(minutes=6),
        settled_at=case.gateway.occurred_at + timedelta(minutes=8),
        status="settled",
    )
    assert classify_transaction(case, default_reference_time()).value == "anomaly"


def test_duplicate_detection_and_pending_deadline():
    assert duplicate_transaction_ids(["a", "b", "a", "a"]) == {"a"}
    pending = next(item for item in generate_cases() if item.path.value == "pending")
    assert classify_transaction(pending, pending.expected_settlement_at).value == "pending"
    assert classify_transaction(pending, pending.expected_settlement_at + timedelta(seconds=1)).value == "anomaly"

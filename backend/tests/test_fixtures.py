"""Fixture generation tests: distribution, reproducibility, and self-consistency."""

from datetime import timedelta
from pathlib import Path

from backend.domain.models import MatchStatus
from backend.reconciliation import compare_records, default_reference_time
from backend.scripts.seed_fixtures import COUNTS, generate_cases, seed_csv

REFERENCE = default_reference_time()


def test_distribution_matches_the_brief():
    cases = generate_cases(REFERENCE)
    assert len(cases) == 100
    actual = {path: sum(1 for c in cases if c.path is path) for path in COUNTS}
    assert actual == COUNTS
    assert COUNTS[MatchStatus.CLEAN] == 60
    assert COUNTS[MatchStatus.LEDGER_GAP] == 15
    assert COUNTS[MatchStatus.PENDING] == 10
    assert COUNTS[MatchStatus.ANOMALY] == 10
    assert COUNTS[MatchStatus.AMOUNT_MISMATCH] == 5


def test_every_case_classifies_to_the_path_it_was_built_for():
    """The generator and the comparison logic must agree.

    This is the check that keeps fixtures honest: if either side drifts, the
    demo would show a transaction diagnosed as something other than what it was
    constructed to be.
    """
    for case in generate_cases(REFERENCE):
        result = compare_records(case.gateway, case.bank, case.ledger, REFERENCE)
        assert result.match_status is case.path, (
            f"{case.txn_id} built as {case.path.value} "
            f"but classified as {result.match_status.value} "
            f"({result.reason_code})"
        )


def test_seed_is_reproducible():
    first = generate_cases(REFERENCE)
    second = generate_cases(REFERENCE)
    assert [c.model_dump() for c in first] == [c.model_dump() for c in second]


def test_csv_output_is_byte_identical_across_runs(tmp_path: Path):
    a, b = tmp_path / "a", tmp_path / "b"
    seed_csv(a, REFERENCE)
    seed_csv(b, REFERENCE)
    for name in (
        "gateway_transactions.csv",
        "bank_settlements.csv",
        "ledger_entries.csv",
        "historical_tickets.csv",
    ):
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_txn_ids_are_unique_and_stable(tmp_path: Path):
    cases = generate_cases(REFERENCE)
    ids = [c.txn_id for c in cases]
    assert len(ids) == len(set(ids))
    # Stable IDs let the demo runbook name specific transactions.
    assert "TXNCLEAN001" in ids
    assert "TXNLEDGERGAP001" in ids
    assert "TXNPENDING001" in ids
    assert "TXNANOMALY001" in ids
    assert "TXNAMOUNTMISMATCH001" in ids


def test_pending_cases_sit_inside_the_window_and_anomalies_outside():
    for case in generate_cases(REFERENCE):
        deadline = case.gateway.expected_settlement_at
        assert deadline is not None
        if case.path is MatchStatus.PENDING:
            assert REFERENCE <= deadline
        elif case.path is MatchStatus.ANOMALY:
            assert REFERENCE > deadline


def test_settlement_timestamps_are_realistic():
    """Bank settlement lands days after capture, not minutes.

    Fixtures with near-identical timestamps across all three sources would let a
    minutes-wide tolerance rule pass here and fail on real data.
    """
    for case in generate_cases(REFERENCE):
        if case.bank is None or case.bank.settled_at is None:
            continue
        lag = case.bank.settled_at - case.gateway.captured_at
        assert lag >= timedelta(days=1)
        assert lag <= timedelta(days=3)


def test_ledger_gap_cases_have_bank_but_no_ledger():
    for case in generate_cases(REFERENCE):
        if case.path is MatchStatus.LEDGER_GAP:
            assert case.bank is not None
            assert case.bank.status == "settled"
            assert case.ledger is None


def test_anomaly_cases_have_no_bank_record():
    for case in generate_cases(REFERENCE):
        if case.path is MatchStatus.ANOMALY:
            assert case.bank is None


def test_amount_mismatch_cases_actually_differ():
    for case in generate_cases(REFERENCE):
        if case.path is MatchStatus.AMOUNT_MISMATCH:
            assert case.bank is not None
            assert case.bank.amount != case.gateway.amount


def test_generated_amounts_satisfy_the_schema_check():
    """amount > 0 is a CHECK constraint; a zero would fail at insert."""
    for case in generate_cases(REFERENCE):
        assert case.gateway.amount > 0
        if case.bank and case.bank.amount is not None:
            assert case.bank.amount > 0
        if case.ledger and case.ledger.amount is not None:
            assert case.ledger.amount > 0


def test_csv_headers_match_the_live_schema_columns(tmp_path: Path):
    seed_csv(tmp_path, REFERENCE)
    expected = {
        "gateway_transactions.csv": "txn_id,amount,currency,status,captured_at,expected_settlement_at,customer_name",
        "bank_settlements.csv": "txn_id,amount,currency,status,settled_at,utr",
        "ledger_entries.csv": "txn_id,amount,currency,status,recorded_at,source",
        "historical_tickets.csv": "txn_id,diagnosis,reason_code,explanation,action_taken,confidence",
    }
    for name, header in expected.items():
        assert (tmp_path / name).read_text(encoding="utf-8").splitlines()[0] == header

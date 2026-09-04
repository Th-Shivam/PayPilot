from pathlib import Path

from backend.scripts.seed_fixtures import COUNTS, generate_cases, seed_csv


def test_distribution_and_stable_ids():
    cases = generate_cases()
    assert len(cases) == 100
    assert {case.path: sum(item.path == case.path for item in cases) for case in cases} == COUNTS
    assert cases[0].transaction_id == "txn-clean-001"


def test_seed_is_byte_identical(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    seed_csv(first)
    seed_csv(second)
    for filename in ("gateway_records.csv", "bank_settlements.csv", "ledger_records.csv"):
        assert (first / filename).read_bytes() == (second / filename).read_bytes()

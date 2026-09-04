"""Generate deterministic reconciliation fixtures and optional Supabase loads."""

from __future__ import annotations

import argparse
import csv
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from backend.domain.models import (
    BankSettlementRecord,
    FixtureCase,
    GatewayRecord,
    LedgerRecord,
    ResolutionPath,
)
from backend.reconciliation.rules import default_reference_time

COUNTS = {
    ResolutionPath.CLEAN: 60,
    ResolutionPath.LEDGER_GAP: 15,
    ResolutionPath.PENDING: 10,
    ResolutionPath.ANOMALY: 10,
    ResolutionPath.AMOUNT_MISMATCH: 5,
}
CSV_FIELDS = ["transaction_id", "amount", "currency", "occurred_at", "status", "expected_settlement_at"]
TICKET_FIELDS = ["ticket_id", "transaction_id", "status", "explanation", "resolution_path"]


def generate_cases(reference_time: datetime | None = None, seed: int = 42) -> list[FixtureCase]:
    reference_time = reference_time or default_reference_time()
    rng = random.Random(seed)
    cases: list[FixtureCase] = []
    for path, count in COUNTS.items():
        for index in range(1, count + 1):
            transaction_id = f"txn-{path.value}-{index:03d}"
            occurred_at = reference_time - timedelta(hours=rng.randint(1, 72))
            amount = float(rng.randint(1000, 100000)) / 100
            expected = reference_time + timedelta(days=2) if path == ResolutionPath.PENDING else reference_time - timedelta(days=1)
            gateway = GatewayRecord(transaction_id=transaction_id, amount=amount, currency="USD", occurred_at=occurred_at)
            bank = None
            ledger = None
            if path in (ResolutionPath.CLEAN, ResolutionPath.LEDGER_GAP, ResolutionPath.AMOUNT_MISMATCH):
                bank_amount = amount + (1.00 if path == ResolutionPath.AMOUNT_MISMATCH else 0)
                bank = BankSettlementRecord(transaction_id=transaction_id, amount=bank_amount, currency="USD", occurred_at=occurred_at, settled_at=occurred_at + timedelta(minutes=2), status="settled")
            elif path == ResolutionPath.PENDING:
                bank = BankSettlementRecord(transaction_id=transaction_id, amount=amount, currency="USD", occurred_at=occurred_at, status="pending")
            if path == ResolutionPath.CLEAN:
                ledger = LedgerRecord(transaction_id=transaction_id, amount=amount, currency="USD", occurred_at=occurred_at, recorded_at=occurred_at + timedelta(minutes=3))
            cases.append(FixtureCase(transaction_id=transaction_id, path=path, gateway=gateway, bank=bank, ledger=ledger, expected_settlement_at=expected))
    return cases


def _rows(cases: Iterable[FixtureCase], source: str) -> Iterable[dict[str, str]]:
    for case in cases:
        record = getattr(case, source)
        if record is None:
            continue
        yield {
            "transaction_id": case.transaction_id,
            "amount": f"{record.amount:.2f}",
            "currency": record.currency,
            "occurred_at": record.occurred_at.isoformat(),
            "status": record.status,
            "expected_settlement_at": case.expected_settlement_at.isoformat(),
        }


def write_csv(path: Path, rows: Iterable[dict[str, str]], fieldnames: list[str] = CSV_FIELDS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _ticket_rows(cases: list[FixtureCase]) -> Iterable[dict[str, str]]:
    selected = [cases[index] for index in (0, 1, 60, 75, 85, 95)] + cases[2:16]
    for case in selected[:20]:
        yield {
            "ticket_id": f"ticket-historical-{case.transaction_id}",
            "transaction_id": case.transaction_id,
            "status": "resolved",
            "explanation": f"Historical reconciliation case for {case.path.value}: transaction {case.transaction_id}.",
            "resolution_path": case.path.value,
        }


def _edge_rows() -> Iterable[dict[str, str]]:
    yield {"transaction_id": "txn-duplicate-001", "path": ResolutionPath.DUPLICATE.value, "note": "intentional duplicate source rows"}
    yield {"transaction_id": "txn-duplicate-001", "path": ResolutionPath.DUPLICATE.value, "note": "intentional duplicate source rows"}
    yield {"transaction_id": "txn-already-resolved-001", "path": ResolutionPath.ALREADY_RESOLVED.value, "note": "ticket already resolved"}


def seed_csv(output_dir: Path, reference_time: datetime | None = None) -> list[FixtureCase]:
    cases = generate_cases(reference_time)
    write_csv(output_dir / "gateway_records.csv", _rows(cases, "gateway"))
    write_csv(output_dir / "bank_settlements.csv", _rows(cases, "bank"))
    write_csv(output_dir / "ledger_records.csv", _rows(cases, "ledger"))
    write_csv(output_dir / "historical_tickets.csv", _ticket_rows(cases), TICKET_FIELDS)
    edge_path = output_dir / "edge_fixtures.csv"
    with edge_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["transaction_id", "path", "note"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(_edge_rows())
    return cases


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    parser.add_argument("--reference-date", type=datetime.fromisoformat, default=default_reference_time())
    args = parser.parse_args()
    seed_csv(args.output_dir, args.reference_date.astimezone(timezone.utc))


if __name__ == "__main__":
    main()

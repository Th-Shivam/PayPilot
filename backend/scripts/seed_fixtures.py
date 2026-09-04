"""Generate deterministic reconciliation fixtures for the live schema.

Emits three CSVs matching the live table columns, plus historical tickets for
similar-case search. Seeded, so reruns are byte-identical.

Timestamps are realistic: bank settlement lands T+1 to T+2 days after gateway
capture, not minutes. A comparison rule tuned to a minutes-wide tolerance would
pass against unrealistic fixtures and fail against real settlement data.
"""

from __future__ import annotations

import argparse
import csv
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from backend.domain.models import (
    BankSettlement,
    FixtureCase,
    GatewayTransaction,
    LedgerEntry,
    MatchStatus,
)
from backend.reconciliation.rules import (
    DEFAULT_SETTLEMENT_WINDOW,
    default_reference_time,
)

# Distribution from the brief: 100 transactions total.
COUNTS: dict[MatchStatus, int] = {
    MatchStatus.CLEAN: 60,
    MatchStatus.LEDGER_GAP: 15,
    MatchStatus.PENDING: 10,
    MatchStatus.ANOMALY: 10,
    MatchStatus.AMOUNT_MISMATCH: 5,
}

CUSTOMER_NAMES = [
    "Aarav Sharma", "Diya Patel", "Vivaan Reddy", "Ananya Iyer", "Aditya Nair",
    "Ishaan Gupta", "Saanvi Menon", "Kabir Joshi", "Myra Desai", "Arjun Rao",
]

GATEWAY_FIELDS = [
    "txn_id", "amount", "currency", "status",
    "captured_at", "expected_settlement_at", "customer_name",
]
BANK_FIELDS = ["txn_id", "amount", "currency", "status", "settled_at", "utr"]
LEDGER_FIELDS = ["txn_id", "amount", "currency", "status", "recorded_at", "source"]
TICKET_FIELDS = [
    "txn_id", "diagnosis", "reason_code", "explanation",
    "action_taken", "confidence",
]


def generate_cases(
    reference_time: datetime | None = None,
    seed: int = 42,
) -> list[FixtureCase]:
    """Build the full fixture set.

    Every case is positioned relative to `reference_time` so its classification
    is stable no matter when the generator runs.
    """
    reference_time = reference_time or default_reference_time()
    rng = random.Random(seed)
    cases: list[FixtureCase] = []

    for path, count in COUNTS.items():
        for index in range(1, count + 1):
            txn_id = f"TXN{path.value.upper().replace('_', '')}{index:03d}"
            amount = round(rng.uniform(150.0, 95_000.0), 2)
            customer = CUSTOMER_NAMES[rng.randrange(len(CUSTOMER_NAMES))]
            utr = f"UTR{rng.randrange(10**11, 10**12)}"

            if path is MatchStatus.PENDING:
                # Captured recently: the window is still open at reference_time.
                captured_at = reference_time - timedelta(hours=rng.randint(2, 20))
            else:
                # Captured long enough ago that the window has closed.
                captured_at = reference_time - timedelta(
                    days=rng.randint(4, 20), hours=rng.randint(0, 23)
                )

            expected_settlement_at = captured_at + DEFAULT_SETTLEMENT_WINDOW
            settled_at = captured_at + timedelta(
                days=rng.randint(1, 2), hours=rng.randint(0, 8)
            )
            recorded_at = settled_at + timedelta(minutes=rng.randint(5, 240))

            gateway = GatewayTransaction(
                txn_id=txn_id,
                amount=amount,
                currency="INR",
                status="captured",
                captured_at=captured_at,
                expected_settlement_at=expected_settlement_at,
                customer_name=customer,
            )

            bank: BankSettlement | None = None
            ledger: LedgerEntry | None = None

            if path is MatchStatus.CLEAN:
                bank = BankSettlement(
                    txn_id=txn_id, amount=amount, currency="INR",
                    status="settled", settled_at=settled_at, utr=utr,
                )
                ledger = LedgerEntry(
                    txn_id=txn_id, amount=amount, currency="INR",
                    status="recorded", recorded_at=recorded_at, source="system",
                )

            elif path is MatchStatus.LEDGER_GAP:
                # Gateway and bank agree; the ledger row never landed.
                bank = BankSettlement(
                    txn_id=txn_id, amount=amount, currency="INR",
                    status="settled", settled_at=settled_at, utr=utr,
                )

            elif path is MatchStatus.PENDING:
                # Bank acknowledges but has not settled, still inside the window.
                bank = BankSettlement(
                    txn_id=txn_id, amount=amount, currency="INR",
                    status="pending", settled_at=None, utr=None,
                )

            elif path is MatchStatus.ANOMALY:
                # No bank row at all, window long closed.
                pass

            elif path is MatchStatus.AMOUNT_MISMATCH:
                # Settled, but for a materially different amount. Skewing by a
                # visible margin rather than rounding error makes the case
                # unambiguous in a demo.
                skew = round(amount * rng.uniform(0.10, 0.40), 2)
                bank_amount = round(max(amount - skew, 1.0), 2)
                bank = BankSettlement(
                    txn_id=txn_id, amount=bank_amount, currency="INR",
                    status="settled", settled_at=settled_at, utr=utr,
                )
                ledger = LedgerEntry(
                    txn_id=txn_id, amount=bank_amount, currency="INR",
                    status="recorded", recorded_at=recorded_at, source="system",
                )

            cases.append(
                FixtureCase(
                    txn_id=txn_id, path=path,
                    gateway=gateway, bank=bank, ledger=ledger,
                )
            )

    return cases


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _gateway_rows(cases: Iterable[FixtureCase]) -> Iterable[dict[str, str]]:
    for case in cases:
        g = case.gateway
        yield {
            "txn_id": g.txn_id,
            "amount": f"{g.amount:.2f}",
            "currency": g.currency,
            "status": g.status,
            "captured_at": _iso(g.captured_at),
            "expected_settlement_at": _iso(g.expected_settlement_at),
            "customer_name": g.customer_name or "",
        }


def _bank_rows(cases: Iterable[FixtureCase]) -> Iterable[dict[str, str]]:
    for case in cases:
        if case.bank is None:
            continue
        b = case.bank
        yield {
            "txn_id": b.txn_id,
            "amount": f"{b.amount:.2f}" if b.amount is not None else "",
            "currency": b.currency,
            "status": b.status or "",
            "settled_at": _iso(b.settled_at),
            "utr": b.utr or "",
        }


def _ledger_rows(cases: Iterable[FixtureCase]) -> Iterable[dict[str, str]]:
    for case in cases:
        if case.ledger is None:
            continue
        entry = case.ledger
        yield {
            "txn_id": entry.txn_id,
            "amount": f"{entry.amount:.2f}" if entry.amount is not None else "",
            "currency": entry.currency,
            "status": entry.status,
            "recorded_at": _iso(entry.recorded_at),
            "source": entry.source,
        }


# Historical tickets exist so similar-case search returns real matches on the
# first run. Without them the feature reads as broken rather than as empty.
_HISTORICAL_TEMPLATES: list[tuple[MatchStatus, str, str, str, str]] = [
    (
        MatchStatus.LEDGER_GAP, "LEDGER_ENTRY_ABSENT_DESPITE_SETTLEMENT",
        "Gateway captured and the bank settled, but no ledger entry was written. "
        "The missing entry was created from the settled amount.",
        "ledger_entry_created", "high",
    ),
    (
        MatchStatus.ANOMALY, "BANK_NO_RECORD_PAST_SETTLEMENT_WINDOW",
        "Gateway captured the payment but the bank has no settlement record past "
        "the expected window. Cause could not be determined from the available "
        "sources, so the case was escalated.",
        "escalated", "low_flagged_for_review",
    ),
    (
        MatchStatus.AMOUNT_MISMATCH, "AMOUNT_DISAGREEMENT_ACROSS_SOURCES",
        "The gateway and the bank report different amounts for the same "
        "transaction. The discrepancy is certain but cannot be corrected "
        "automatically, so it was escalated.",
        "escalated", "high",
    ),
    (
        MatchStatus.PENDING, "BANK_PENDING_WITHIN_SETTLEMENT_WINDOW",
        "Settlement is still inside the expected window. No action was needed.",
        "no_action_needed", "high",
    ),
    (
        MatchStatus.CLEAN, "ALL_SOURCES_AGREE",
        "Gateway, bank, and ledger all agree. Settlement completed normally.",
        "auto_resolved", "high",
    ),
]


def _ticket_rows(count: int = 20) -> Iterable[dict[str, str]]:
    """Historical tickets on IDs distinct from the generated transactions.

    A HIST prefix keeps them from colliding with the fixture set, so the
    exception list and the similarity corpus stay separable in the demo.
    """
    for index in range(count):
        status, reason, explanation, action, confidence = _HISTORICAL_TEMPLATES[
            index % len(_HISTORICAL_TEMPLATES)
        ]
        yield {
            "txn_id": f"TXNHIST{index + 1:03d}",
            "diagnosis": status.value,
            "reason_code": reason,
            "explanation": explanation,
            "action_taken": action,
            "confidence": confidence,
        }


def write_csv(path: Path, rows: Iterable[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def seed_csv(
    output_dir: Path,
    reference_time: datetime | None = None,
) -> list[FixtureCase]:
    """Write all fixture CSVs and return the generated cases."""
    cases = generate_cases(reference_time)
    write_csv(output_dir / "gateway_transactions.csv", _gateway_rows(cases), GATEWAY_FIELDS)
    write_csv(output_dir / "bank_settlements.csv", _bank_rows(cases), BANK_FIELDS)
    write_csv(output_dir / "ledger_entries.csv", _ledger_rows(cases), LEDGER_FIELDS)
    write_csv(output_dir / "historical_tickets.csv", _ticket_rows(), TICKET_FIELDS)
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    parser.add_argument(
        "--reference-date",
        type=datetime.fromisoformat,
        default=default_reference_time(),
        help="Fixed clock the fixtures are positioned against.",
    )
    args = parser.parse_args()
    reference = args.reference_date
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    cases = seed_csv(args.output_dir, reference.astimezone(timezone.utc))
    print(f"wrote {len(cases)} transactions to {args.output_dir}")


if __name__ == "__main__":
    main()

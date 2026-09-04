"""Load generated CSV artifacts with idempotent Supabase upserts."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def load_csvs(output_dir: Path, supabase: Any, embeddings: Any | None = None) -> dict[str, list[str]]:
    tables = {
        "gateway_records.csv": ("gateway_transactions", "txn_id"),
        "bank_settlements.csv": ("bank_settlements", "txn_id"),
        "ledger_records.csv": ("ledger_entries", "txn_id"),
    }
    for filename, (table, conflict_key) in tables.items():
        rows = read_rows(output_dir / filename)
        rows = [_canonical_row(table, row) for row in rows]
        if rows:
            supabase.table(table).upsert(rows, on_conflict=conflict_key).execute()
    tickets = read_rows(output_dir / "historical_tickets.csv")
    failed_embeddings: list[str] = []
    if embeddings is not None:
        for ticket in tickets:
            try:
                ticket["embedding"] = embeddings.embed(ticket["explanation"])
            except Exception:
                failed_embeddings.append(ticket["ticket_id"])
        tickets = [ticket for ticket in tickets if "embedding" in ticket]
    tickets = [_canonical_ticket(ticket) for ticket in tickets]
    if tickets:
        supabase.table("tickets").upsert(tickets, on_conflict="txn_id").execute()
    return {"failed_ticket_embeddings": failed_embeddings}


def _canonical_row(table: str, row: dict[str, str]) -> dict[str, str]:
    result = dict(row)
    result["txn_id"] = result.pop("transaction_id")
    result.pop("expected_settlement_at", None) if table != "gateway_transactions" else None
    occurred = result.pop("occurred_at")
    if table == "gateway_transactions": result["captured_at"] = occurred
    elif table == "bank_settlements": result["settled_at"] = occurred if result.get("status") == "settled" else None
    else: result["recorded_at"] = occurred
    return result


def _canonical_ticket(ticket: dict[str, object]) -> dict[str, object]:
    path = str(ticket.pop("resolution_path"))
    ticket.pop("ticket_id", None)
    ticket["txn_id"] = ticket.pop("transaction_id")
    ticket["diagnosis"] = path
    ticket["action_taken"] = "ledger_entry_created" if path == "ledger_gap" else "escalated" if path in {"anomaly", "amount_mismatch"} else "no_action_needed"
    ticket["confidence"] = "low_flagged_for_review" if path == "anomaly" else "high"
    ticket.pop("status", None)
    return ticket

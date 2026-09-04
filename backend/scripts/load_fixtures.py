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
        "gateway_records.csv": "gateway_records",
        "bank_settlements.csv": "bank_settlements",
        "ledger_records.csv": "ledger_records",
    }
    for filename, table in tables.items():
        rows = read_rows(output_dir / filename)
        if rows:
            supabase.table(table).upsert(rows, on_conflict="transaction_id").execute()
    tickets = read_rows(output_dir / "historical_tickets.csv")
    failed_embeddings: list[str] = []
    if embeddings is not None:
        for ticket in tickets:
            try:
                ticket["explanation_embedding"] = embeddings.embed(ticket["explanation"])
            except Exception:
                failed_embeddings.append(ticket["ticket_id"])
        tickets = [ticket for ticket in tickets if "explanation_embedding" in ticket]
    if tickets:
        supabase.table("tickets").upsert(tickets, on_conflict="ticket_id").execute()
    return {"failed_ticket_embeddings": failed_embeddings}

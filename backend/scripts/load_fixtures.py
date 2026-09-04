"""Load fixture CSVs into Supabase with idempotent upserts.

Table and conflict targets match the live schema. Every business table has a
unique txn_id constraint, so on_conflict="txn_id" makes reloading safe.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from backend.embeddings import EMBEDDING_DIMENSION

# CSV filename -> live table name.
TABLE_FILES: dict[str, str] = {
    "gateway_transactions.csv": "gateway_transactions",
    "bank_settlements.csv": "bank_settlements",
    "ledger_entries.csv": "ledger_entries",
}

# Postgres rejects "" for timestamptz and numeric, so empty CSV cells have to
# become SQL NULL rather than being passed through as empty strings.
NULLABLE_FIELDS = frozenset({
    "amount", "settled_at", "recorded_at", "utr",
    "expected_settlement_at", "customer_name", "status", "reason_code",
})


def read_rows(path: Path) -> list[dict[str, Any]]:
    """Read a CSV, converting empty cells in nullable columns to None."""
    with path.open(encoding="utf-8", newline="") as handle:
        rows: list[dict[str, Any]] = []
        for raw in csv.DictReader(handle):
            row: dict[str, Any] = {}
            for key, value in raw.items():
                if value == "" and key in NULLABLE_FIELDS:
                    row[key] = None
                else:
                    row[key] = value
            rows.append(row)
        return rows


def load_csvs(
    data_dir: Path,
    supabase: Any,
    embeddings: Any | None = None,
) -> dict[str, Any]:
    """Upsert fixtures, embedding historical ticket explanations when possible.

    A model-load failure degrades to loading tickets without embeddings rather
    than failing the whole load; those rows are reported back to the caller.
    """
    loaded: dict[str, int] = {}

    for filename, table in TABLE_FILES.items():
        path = data_dir / filename
        if not path.exists():
            continue
        rows = read_rows(path)
        if rows:
            supabase.table(table).upsert(rows, on_conflict="txn_id").execute()
        loaded[table] = len(rows)

    ticket_path = data_dir / "historical_tickets.csv"
    failed_embeddings: list[str] = []
    tickets: list[dict[str, Any]] = []

    if ticket_path.exists():
        tickets = read_rows(ticket_path)
        if embeddings is not None:
            for ticket in tickets:
                try:
                    vector = embeddings.embed(ticket["explanation"])
                    if len(vector) != EMBEDDING_DIMENSION:
                        raise ValueError("embedding has unexpected dimension")
                    ticket["embedding"] = vector
                except Exception:
                    failed_embeddings.append(ticket["txn_id"])
        if tickets:
            supabase.table("tickets").upsert(tickets, on_conflict="txn_id").execute()

    loaded["tickets"] = len(tickets)
    return {"loaded": loaded, "failed_ticket_embeddings": failed_embeddings}


def backfill_ticket_embeddings(supabase: Any, embeddings: Any) -> dict[str, list[str]]:
    """Populate missing ticket vectors without rewriting existing ticket facts.

    Lets the demo seed tickets first (fast, no model download) and add
    embeddings later, so similar-case search comes online without a reload.
    """
    response = (
        supabase.table("tickets")
        .select("txn_id,explanation")
        .is_("embedding", "null")
        .execute()
    )
    failed: list[str] = []
    updated: list[str] = []
    for ticket in getattr(response, "data", None) or []:
        txn_id = ticket.get("txn_id")
        try:
            vector = embeddings.embed(ticket["explanation"])
            if len(vector) != EMBEDDING_DIMENSION:
                raise ValueError("embedding has unexpected dimension")
            supabase.table("tickets").upsert(
                {"txn_id": txn_id, "embedding": vector}, on_conflict="txn_id"
            ).execute()
            updated.append(txn_id)
        except Exception:
            if txn_id:
                failed.append(txn_id)
    return {"updated_ticket_embeddings": updated, "failed_ticket_embeddings": failed}

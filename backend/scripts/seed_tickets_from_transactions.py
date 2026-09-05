"""Populate the tickets table from the reconciliation feeds.

For every gateway transaction, this looks up its bank and ledger records, runs
the deterministic compare_records diagnosis, and upserts a ticket carrying that
verdict. It exists so the dashboard (which reads the tickets table) reflects the
full gateway/bank/ledger dataset, not only the handful of historical tickets.

Nothing here is guessed: the diagnosis, action, and confidence are the exact
deterministic outputs of compare_records over the real seeded records. Safe to
re-run — upserts key on txn_id.

Usage (from repo root, with backend/.env populated):
    python -m backend.scripts.seed_tickets_from_transactions
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

from backend.domain.models import CONFIDENCE_BY_STATUS, MatchStatus
from backend.reconciliation.rules import compare_records, default_reference_time

# Action taken per diagnosis, matching the API's _outcome() and satisfying the
# tickets_* CHECK constraints (e.g. ledger_entry_created requires ledger_gap;
# anomaly/unknown may only escalate).
ACTION_BY_STATUS = {
    MatchStatus.CLEAN: ("auto_resolved", "Gateway, bank, and ledger records match."),
    MatchStatus.PENDING: ("no_action_needed", "Bank settlement is pending within the T+2 window."),
    MatchStatus.LEDGER_GAP: ("ledger_entry_created", "Settlement exists but the ledger entry is missing."),
    MatchStatus.ANOMALY: ("escalated", "Settlement data is missing or outside the expected window."),
    MatchStatus.AMOUNT_MISMATCH: ("escalated", "Gateway and settlement amounts differ beyond tolerance."),
    MatchStatus.UNKNOWN: ("escalated", "The transaction could not be classified from the available records."),
}


def _one(client, table: str, txn_id: str) -> dict | None:
    rows = client.table(table).select("*").eq("txn_id", txn_id).limit(1).execute().data or []
    return rows[0] if rows else None


def _prune(row: dict, model) -> dict:
    fields = set(model.model_fields)
    return {k: v for k, v in row.items() if k in fields}


def main() -> None:
    load_dotenv(Path("backend/.env"))
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    client = create_client(url, key)

    ref_raw = os.environ.get("RECONCILIATION_REFERENCE_DATE", "").strip()
    reference = datetime.fromisoformat(ref_raw) if ref_raw else default_reference_time()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    from backend.domain.models import BankSettlement, GatewayTransaction, LedgerEntry

    gateways = client.table("gateway_transactions").select("*").execute().data or []
    rows_to_upsert = []
    counts: dict[str, int] = {}

    for g in gateways:
        txn_id = g["txn_id"]
        bank = _one(client, "bank_settlements", txn_id)
        ledger = _one(client, "ledger_entries", txn_id)
        gateway = GatewayTransaction(**_prune(g, GatewayTransaction))
        bank_rec = BankSettlement(**_prune(bank, BankSettlement)) if bank else None
        ledger_rec = LedgerEntry(**_prune(ledger, LedgerEntry)) if ledger else None

        verdict = compare_records(gateway, bank_rec, ledger_rec, reference, txn_id=txn_id)
        status = verdict.match_status
        action, explanation = ACTION_BY_STATUS[status]
        confidence = CONFIDENCE_BY_STATUS[status].value
        counts[status.value] = counts.get(status.value, 0) + 1

        rows_to_upsert.append({
            "txn_id": txn_id,
            "diagnosis": status.value,
            "reason_code": verdict.reason_code,
            "explanation": explanation,
            "action_taken": action,
            "confidence": confidence,
            "detail": verdict.detail,
        })

    if rows_to_upsert:
        client.table("tickets").upsert(rows_to_upsert, on_conflict="txn_id").execute()

    print(f"upserted {len(rows_to_upsert)} tickets from transactions")
    for status, n in sorted(counts.items()):
        print(f"  {status:16} {n}")


if __name__ == "__main__":
    main()

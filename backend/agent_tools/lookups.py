"""Deterministic lookup tools for the three reconciliation feeds.

Each tool fetches one transaction from one source and returns a typed record or
None. No comparison happens here: absence is reported as absence, and
compare_records decides what it means.

The live schema enforces unique (txn_id) per table, so a lookup returning more
than one row means the constraint is missing. That is raised rather than
silently resolved, because picking one of two conflicting rows would make the
diagnosis non-deterministic.
"""

from __future__ import annotations

from typing import Any

from backend.domain.models import BankSettlement, GatewayTransaction, LedgerEntry


class LookupError_(RuntimeError):
    """Raised when a source returns data the schema should have prevented."""


def _fetch_one(supabase: Any, table: str, txn_id: str) -> dict[str, Any] | None:
    """Fetch at most one row by txn_id, rejecting ambiguity."""
    if not txn_id or not txn_id.strip():
        return None

    response = (
        supabase.table(table)
        .select("*")
        .eq("txn_id", txn_id.strip())
        .limit(2)  # Fetch 2 to detect duplicates the constraint should prevent.
        .execute()
    )
    rows = getattr(response, "data", None) or []
    if not rows:
        return None
    if len(rows) > 1:
        raise LookupError_(
            f"{table} returned {len(rows)} rows for txn_id={txn_id!r}; "
            "unique (txn_id) is expected to prevent this"
        )
    return rows[0]


def _clean(row: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    """Keep only model fields, dropping DB-managed columns like id/created_at."""
    return {k: v for k, v in row.items() if k in allowed}


def lookup_gateway(supabase: Any, txn_id: str) -> GatewayTransaction | None:
    """Fetch the gateway capture record, or None if absent."""
    row = _fetch_one(supabase, "gateway_transactions", txn_id)
    if row is None:
        return None
    return GatewayTransaction(
        **_clean(row, set(GatewayTransaction.model_fields.keys()))
    )


def lookup_bank(supabase: Any, txn_id: str) -> BankSettlement | None:
    """Fetch the bank settlement record, or None if absent.

    None is meaningful: past the settlement window it is an anomaly, inside it
    the payout is still pending.
    """
    row = _fetch_one(supabase, "bank_settlements", txn_id)
    if row is None:
        return None
    return BankSettlement(**_clean(row, set(BankSettlement.model_fields.keys())))


def lookup_ledger(supabase: Any, txn_id: str) -> LedgerEntry | None:
    """Fetch the ledger entry, or None if absent.

    None where gateway and bank agree is a ledger_gap, the one auto-fixable case.
    """
    row = _fetch_one(supabase, "ledger_entries", txn_id)
    if row is None:
        return None
    return LedgerEntry(**_clean(row, set(LedgerEntry.model_fields.keys())))

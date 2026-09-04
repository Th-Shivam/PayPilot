"""Lookup tool, loader, and similarity search tests against a fake Supabase."""

from pathlib import Path
from typing import Any

import pytest

from backend.agent_tools import (
    LookupError_,
    lookup_bank,
    lookup_gateway,
    lookup_ledger,
    search_similar_tickets,
)
from backend.embeddings import EmbeddingService
from backend.reconciliation import default_reference_time
from backend.scripts.load_fixtures import load_csvs
from backend.scripts.seed_fixtures import seed_csv

REFERENCE = default_reference_time()


class FakeModel:
    """Stands in for all-MiniLM-L6-v2 so tests need no torch and no download."""

    def encode(self, text: str, **_: Any) -> list[float]:
        return [0.1] * 384


class FakeSelect:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows
        self.data = rows

    def select(self, *_: Any) -> "FakeSelect":
        return self

    def eq(self, *_: Any) -> "FakeSelect":
        return self

    def limit(self, *_: Any) -> "FakeSelect":
        return self

    def upsert(self, rows: list[dict[str, Any]], on_conflict: str) -> "FakeSelect":
        self._rows = rows
        self.on_conflict = on_conflict
        return self

    def execute(self) -> "FakeSelect":
        return self


class FakeSupabase:
    def __init__(self, rows_by_table: dict[str, list[dict[str, Any]]] | None = None):
        self.rows_by_table = rows_by_table or {}
        self.upserts: list[tuple[str, list[dict[str, Any]], str]] = []
        self.rpc_params: dict[str, Any] | None = None
        self.rpc_rows: list[dict[str, Any]] = []

    def table(self, name: str) -> Any:
        rows = self.rows_by_table.get(name, [])
        outer = self

        class Tracked(FakeSelect):
            def upsert(self, upsert_rows, on_conflict):
                outer.upserts.append((name, upsert_rows, on_conflict))
                return self

        return Tracked(rows)

    def rpc(self, name: str, params: dict[str, Any]) -> Any:
        assert name == "match_tickets"
        self.rpc_params = params
        outer = self

        class Result:
            data = outer.rpc_rows

            def execute(self):
                return self

        return Result()


GATEWAY_ROW = {
    "id": "uuid-1",
    "txn_id": "TXN001",
    "amount": 1000.00,
    "currency": "INR",
    "status": "captured",
    "captured_at": "2025-01-10T12:00:00+00:00",
    "expected_settlement_at": "2025-01-12T12:00:00+00:00",
    "customer_name": "Aarav Sharma",
    "owner_id": None,
    "created_at": "2025-01-10T12:00:00+00:00",
}


# --- lookups -------------------------------------------------------------


def test_lookup_gateway_returns_typed_record_and_drops_db_columns():
    client = FakeSupabase({"gateway_transactions": [GATEWAY_ROW]})
    record = lookup_gateway(client, "TXN001")
    assert record is not None
    assert record.txn_id == "TXN001"
    assert record.amount == 1000.00
    # id, created_at, and owner_id are DB-managed and not model fields.
    assert not hasattr(record, "created_at")


def test_lookup_returns_none_when_absent():
    client = FakeSupabase({})
    assert lookup_gateway(client, "TXNMISSING") is None
    assert lookup_bank(client, "TXNMISSING") is None
    assert lookup_ledger(client, "TXNMISSING") is None


def test_blank_txn_id_returns_none_without_querying():
    client = FakeSupabase({"gateway_transactions": [GATEWAY_ROW]})
    assert lookup_gateway(client, "") is None
    assert lookup_gateway(client, "   ") is None


def test_duplicate_rows_raise_rather_than_guessing():
    """Two rows for one txn_id means the unique constraint is gone.

    Picking one would make the diagnosis depend on row order, so this fails
    loudly instead.
    """
    client = FakeSupabase({"gateway_transactions": [GATEWAY_ROW, GATEWAY_ROW]})
    with pytest.raises(LookupError_, match="unique"):
        lookup_gateway(client, "TXN001")


def test_bank_lookup_handles_null_amount_and_status():
    client = FakeSupabase({
        "bank_settlements": [{
            "id": "uuid-2", "txn_id": "TXN001", "amount": None, "currency": "INR",
            "status": None, "settled_at": None, "utr": None,
            "created_at": "2025-01-10T12:00:00+00:00",
        }]
    })
    record = lookup_bank(client, "TXN001")
    assert record is not None
    assert record.amount is None
    assert record.status is None


def test_ledger_lookup_preserves_source_provenance():
    client = FakeSupabase({
        "ledger_entries": [{
            "id": "uuid-3", "txn_id": "TXN001", "amount": 1000.00, "currency": "INR",
            "status": "recorded", "recorded_at": "2025-01-12T14:00:00+00:00",
            "source": "agent_reconciliation",
            "created_at": "2025-01-12T14:00:00+00:00",
        }]
    })
    record = lookup_ledger(client, "TXN001")
    assert record is not None
    assert record.source == "agent_reconciliation"


# --- loader --------------------------------------------------------------


def test_loader_targets_live_tables_and_conflicts_on_txn_id(tmp_path: Path):
    seed_csv(tmp_path, REFERENCE)
    client = FakeSupabase()
    result = load_csvs(tmp_path, client, EmbeddingService(model=FakeModel()))

    assert [name for name, _, _ in client.upserts] == [
        "gateway_transactions", "bank_settlements", "ledger_entries", "tickets",
    ]
    assert all(conflict == "txn_id" for _, _, conflict in client.upserts)
    assert result["loaded"]["gateway_transactions"] == 100
    assert result["failed_ticket_embeddings"] == []


def test_loader_embeds_historical_tickets(tmp_path: Path):
    seed_csv(tmp_path, REFERENCE)
    client = FakeSupabase()
    load_csvs(tmp_path, client, EmbeddingService(model=FakeModel()))
    tickets = client.upserts[-1][1]
    assert len(tickets) == 20
    assert all(len(t["embedding"]) == 384 for t in tickets)
    # Every diagnosis category is represented, so first-run similarity search
    # can return a relevant match whatever the live case turns out to be.
    assert {t["diagnosis"] for t in tickets} >= {
        "clean", "ledger_gap", "pending", "anomaly", "amount_mismatch",
    }


def test_loader_converts_empty_cells_to_null(tmp_path: Path):
    seed_csv(tmp_path, REFERENCE)
    client = FakeSupabase()
    load_csvs(tmp_path, client, None)
    bank_rows = next(rows for name, rows, _ in client.upserts if name == "bank_settlements")
    pending = [r for r in bank_rows if r["status"] == "pending"]
    assert pending
    # Postgres rejects "" for timestamptz; it has to be None.
    assert all(r["settled_at"] is None for r in pending)


def test_loader_survives_embedding_failure(tmp_path: Path):
    class BrokenModel:
        def encode(self, *_: Any, **__: Any):
            raise RuntimeError("model unavailable")

    seed_csv(tmp_path, REFERENCE)
    client = FakeSupabase()
    result = load_csvs(tmp_path, client, EmbeddingService(model=BrokenModel()))
    # Tickets still load; the failures are reported rather than raised.
    assert len(result["failed_ticket_embeddings"]) == 20
    assert result["loaded"]["tickets"] == 20


def test_loader_is_rerunnable(tmp_path: Path):
    seed_csv(tmp_path, REFERENCE)
    client = FakeSupabase()
    load_csvs(tmp_path, client, None)
    load_csvs(tmp_path, client, None)
    assert len(client.upserts) == 8
    assert all(conflict == "txn_id" for _, _, conflict in client.upserts)


# --- similarity search ---------------------------------------------------


def test_similarity_search_calls_match_tickets_and_bounds_the_limit():
    client = FakeSupabase()
    client.rpc_rows = [{
        "txn_id": "TXNHIST001", "similarity": 0.92, "diagnosis": "ledger_gap",
        "reason_code": "LEDGER_ENTRY_ABSENT_DESPITE_SETTLEMENT",
        "explanation": "Ledger entry was missing and has been created.",
        "action_taken": "ledger_entry_created", "confidence": "high",
    }]
    results = search_similar_tickets(
        "ledger entry missing", client, EmbeddingService(model=FakeModel()), limit=500
    )
    assert results[0]["txn_id"] == "TXNHIST001"
    assert results[0]["similarity"] == 0.92
    assert client.rpc_params["match_count"] == 20
    assert len(client.rpc_params["query_embedding"]) == 384


def test_similarity_search_filters_below_threshold():
    client = FakeSupabase()
    client.rpc_rows = [
        {"txn_id": "A", "similarity": 0.95, "explanation": "close"},
        {"txn_id": "B", "similarity": 0.20, "explanation": "far"},
    ]
    results = search_similar_tickets(
        "query", client, EmbeddingService(model=FakeModel()), threshold=0.70
    )
    assert [r["txn_id"] for r in results] == ["A"]


def test_similarity_search_fails_soft():
    """Similar cases enrich an explanation; they never determine a diagnosis."""
    class BrokenClient:
        def rpc(self, *_: Any, **__: Any):
            raise RuntimeError("network down")

    assert search_similar_tickets("q", BrokenClient(), EmbeddingService(model=FakeModel())) == []
    assert search_similar_tickets("", FakeSupabase(), EmbeddingService(model=FakeModel())) == []
    assert search_similar_tickets(
        "q", FakeSupabase(), EmbeddingService(model=FakeModel()), threshold=5.0
    ) == []


def test_similarity_search_returns_empty_on_empty_corpus():
    """Expected on a fresh database, and must not look like an error."""
    client = FakeSupabase()
    client.rpc_rows = []
    assert search_similar_tickets("q", client, EmbeddingService(model=FakeModel())) == []

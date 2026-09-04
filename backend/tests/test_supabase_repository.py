from datetime import datetime, timezone

from backend.app.repository import SupabaseRepository
from backend.reconciliation.rules import default_reference_time
from backend.embeddings import EmbeddingService


class FakeEmbeddingModel:
    def encode(self, text, **kwargs):
        return [1.0] + [0.0] * 383


class Query:
    def __init__(self, client, table, rows):
        self.client, self.table, self.rows = client, table, rows

    def select(self, *_args): return self
    def eq(self, key, value): self.rows = [r for r in self.rows if r.get(key) == value]; return self
    def lt(self, key, value): self.rows = [r for r in self.rows if (r.get(key) or 0) < value]; return self
    def gte(self, key, value): return self
    def lte(self, key, value): return self
    def limit(self, value): self.rows = self.rows[:value]; return self
    def upsert(self, row, on_conflict): self.client.upserted.append((self.table, row, on_conflict)); return self
    def insert(self, row): self.client.inserted.append((self.table, row)); return self
    def execute(self): return type("Response", (), {"data": self.rows})()


class FakeClient:
    def __init__(self):
        self.upserted = []
        self.inserted = []
        self.tables = {
            "gateway_transactions": [{"txn_id": "txn-pending", "amount": 10, "currency": "USD", "captured_at": "2025-01-14T12:00:00+00:00", "status": "captured", "expected_settlement_at": "2025-01-17T12:00:00+00:00"}],
            "bank_settlements": [{"txn_id": "txn-pending", "amount": 10, "currency": "USD", "created_at": "2025-01-14T12:00:00+00:00", "status": "pending"}],
            "ledger_entries": [],
            "tickets": [{"txn_id": "txn-pending", "diagnosis": "pending", "explanation": "x", "action_taken": "no_action_needed", "confidence": "high"}],
            "agent_trace_logs": [],
        }

    def table(self, name): return Query(self, name, list(self.tables.get(name, [])))


def test_supabase_repository_uses_shared_pending_rule_and_persists_trace():
    client = FakeClient()
    repo = SupabaseRepository(client, reference_time=default_reference_time())
    result = repo.resolve("txn-pending")
    assert result["status"] == "pending"
    assert len(client.inserted) == 4
    assert all(table == "agent_trace_logs" for table, _ in client.inserted)


def test_supabase_reads_tickets_and_analytics():
    repo = SupabaseRepository(FakeClient(), reference_time=default_reference_time())
    assert repo.tickets()[0]["txn_id"] == "txn-pending"
    assert repo.analytics()["by_confidence"] == {"high": 1}


def test_authorized_ticket_action_persists_embedding():
    client = FakeClient()
    repo = SupabaseRepository(client, embedding_service=EmbeddingService(model=FakeEmbeddingModel()))
    result = repo._raise_ticket("txn-pending", "Review mismatch", {"match_status": "anomaly", "confidence": "low_flagged_for_review", "detail": {}})
    assert result["status"] == "created"
    table, row, conflict = client.upserted[-1]
    assert table == "tickets"
    assert conflict == "txn_id"
    assert len(row["embedding"]) == 384

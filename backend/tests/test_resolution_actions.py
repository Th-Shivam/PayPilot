from datetime import datetime, timezone

from backend.app.repository import SupabaseRepository
from backend.reconciliation.rules import default_reference_time


class Query:
    def __init__(self, client, table):
        self.client, self.table_name = client, table
        self.filters = {}
        self.payload = None

    def select(self, *_): return self
    def eq(self, key, value): self.filters[key] = value; return self
    def limit(self, *_): return self
    def insert(self, row): self.client.inserts.append((self.table_name, row)); return self
    def upsert(self, row, on_conflict): self.client.upserts.append((self.table_name, row, on_conflict)); self.client._upsert(self.table_name, row); return self
    def update(self, row): self.payload = row; return self
    def execute(self):
        if self.payload is not None:
            for row in self.client.tables[self.table_name]:
                if all(row.get(k) == v for k, v in self.filters.items()): row.update(self.payload)
        rows = [row for row in self.client.tables.get(self.table_name, []) if all(row.get(k) == v for k, v in self.filters.items())]
        return type("Response", (), {"data": rows})()


class Client:
    def __init__(self, path="ledger_gap"):
        self.inserts, self.upserts = [], []
        self.tables = {
            "gateway_transactions": [{"txn_id": "txn-1", "amount": 10, "currency": "INR", "captured_at": "2025-01-14T12:00:00+00:00", "status": "captured", "expected_settlement_at": "2025-01-13T12:00:00+00:00"}],
            "bank_settlements": [{"txn_id": "txn-1", "amount": 10, "currency": "INR", "settled_at": "2025-01-14T12:02:00+00:00", "status": "settled"}],
            "ledger_entries": [] if path == "ledger_gap" else [{"txn_id": "txn-1", "amount": 10, "currency": "INR", "recorded_at": "2025-01-14T12:03:00+00:00", "status": "recorded"}],
            "tickets": [],
            "agent_trace_logs": [],
        }

    def table(self, name): return Query(self, name)
    def _upsert(self, table, row):
        key = "txn_id"
        existing = next((item for item in self.tables[table] if item.get(key) == row.get(key)), None)
        if existing: existing.update(row)
        else: self.tables[table].append(dict(row))


def test_ledger_action_is_guarded_and_idempotent():
    repo = SupabaseRepository(Client(), reference_time=default_reference_time())
    evidence = {"match_status": "ledger_gap", "sources": {"gateway": True, "bank": True, "ledger": False}}
    first = repo.create_ledger_entry("txn-1", evidence)
    second = repo.create_ledger_entry("txn-1", evidence)
    assert first["status"] == "created"
    assert second["status"] == "already_exists"
    assert len(repo.client.tables["ledger_entries"]) == 1
    assert repo.create_ledger_entry("txn-1", {"match_status": "anomaly"})["status"] == "not_authorized"


def test_close_requires_evidence_and_matching_transaction():
    client = Client(path="clean")
    client.tables["tickets"] = [{"txn_id": "txn-1", "diagnosis": "clean", "action_taken": "escalated", "confidence": "high", "explanation": "review"}]
    repo = SupabaseRepository(client, reference_time=default_reference_time())
    assert repo.close_as_resolved("txn-1")["status"] == "not_authorized"
    result = repo.close_as_resolved("txn-1", {"match_status": "clean", "sources": {"gateway": True, "bank": True, "ledger": True}})
    assert result["status"] == "closed"
    assert client.tables["tickets"][0]["action_taken"] == "no_action_needed"

from pathlib import Path

import pytest

from backend.agent_tools import search_similar_tickets
from backend.embeddings import EmbeddingService, EmbeddingServiceError
from backend.scripts.load_fixtures import backfill_ticket_embeddings, load_csvs
from backend.scripts.seed_fixtures import seed_csv


class FakeModel:
    def encode(self, text, **kwargs):
        return [1.0] + [0.0] * 383


class WrongDimensionModel:
    def encode(self, text, **kwargs):
        return [1.0, 0.0]


class BrokenModel:
    def encode(self, text, **kwargs):
        raise RuntimeError("model unavailable")


class FakeQuery:
    def __init__(self, owner, table=None):
        self.owner, self.table_name = owner, table

    def upsert(self, rows, on_conflict):
        self.owner.upserts.append((self.table_name, rows, on_conflict))
        return self

    def execute(self):
        return self.owner


class FakeSupabase:
    def __init__(self):
        self.upserts = []
        self.data = [{"ticket_id": "ticket-1", "score": 0.92, "status": "resolved", "explanation": "Ledger gap fixed"}]

    def table(self, name):
        return FakeQuery(self, name)

    def rpc(self, name, params):
        assert name == "match_tickets"
        assert len(params["query_embedding"]) == 384
        self.rpc_params = params
        return self

    def execute(self):
        return self


class BackfillQuery:
    def __init__(self, owner, rows):
        self.owner = owner
        self.rows = rows

    def select(self, *_args):
        return self

    def is_(self, *_args):
        return self

    def upsert(self, row, on_conflict):
        self.owner.upserts.append(("tickets", row, on_conflict))
        return self

    def execute(self):
        return type("Response", (), {"data": self.rows})()


class BackfillClient:
    def __init__(self):
        self.upserts = []
        self.rows = [{"txn_id": "txn-1", "explanation": "historical case"}]

    def table(self, name):
        return BackfillQuery(self, self.rows if name == "tickets" else [])


def test_loader_persists_ticket_embeddings_and_is_rerunnable(tmp_path: Path):
    seed_csv(tmp_path)
    client = FakeSupabase()
    load_csvs(tmp_path, client, EmbeddingService(model=FakeModel()))
    assert [name for name, _, _ in client.upserts] == ["gateway_transactions", "bank_settlements", "ledger_entries", "tickets"]
    tickets = client.upserts[-1][1]
    assert len(tickets) == 20
    assert all("embedding" in ticket for ticket in tickets)
    assert {ticket["diagnosis"] for ticket in tickets} >= {"clean", "ledger_gap", "pending", "anomaly", "amount_mismatch"}


def test_similarity_search_is_bounded_and_structured():
    client = FakeSupabase()
    result = search_similar_tickets("ledger issue", client, EmbeddingService(model=FakeModel()), limit=100)
    assert result[0]["ticket_id"] == "ticket-1"
    assert client.rpc_params["match_count"] == 20


def test_embedding_service_requires_384_dimensions():
    with pytest.raises(EmbeddingServiceError, match="384"):
        EmbeddingService(model=WrongDimensionModel()).embed("query")


def test_similarity_search_safely_handles_model_failure_and_no_match():
    client = FakeSupabase()
    assert search_similar_tickets("query", client, EmbeddingService(model=BrokenModel())) == []
    client.data = []
    assert search_similar_tickets("query", client, EmbeddingService(model=FakeModel()), threshold=0.99, limit=1) == []


def test_similarity_search_rejects_invalid_inputs():
    client = FakeSupabase()
    embeddings = EmbeddingService(model=FakeModel())
    assert search_similar_tickets("query", client, embeddings, threshold=-0.1) == []
    assert search_similar_tickets("query", client, embeddings, limit="bad") == []
    assert search_similar_tickets("query", client, embeddings, threshold=float("nan")) == []


def test_similarity_search_skips_malformed_rows():
    client = FakeSupabase()
    client.data = [
        {"txn_id": "ok", "similarity": 0.9, "diagnosis": "clean", "explanation": "matched"},
        {"similarity": 0.95, "diagnosis": "clean", "explanation": "missing id"},
        {"txn_id": "bad-score", "similarity": "bad", "diagnosis": "clean", "explanation": "bad score"},
        {"txn_id": "missing-status", "similarity": 0.95, "explanation": "missing status"},
    ]
    result = search_similar_tickets("query", client, EmbeddingService(model=FakeModel()))
    assert result == [{"ticket_id": "ok", "score": 0.9, "status": "clean", "explanation": "matched"}]


def test_backfill_ticket_embeddings_updates_only_missing_vectors():
    client = BackfillClient()
    result = backfill_ticket_embeddings(client, EmbeddingService(model=FakeModel()))
    assert result == {"updated_ticket_embeddings": ["txn-1"], "failed_ticket_embeddings": []}
    assert len(client.upserts) == 1
    assert len(client.upserts[0][1]["embedding"]) == 384

from pathlib import Path

from backend.agent_tools import search_similar_tickets
from backend.embeddings import EmbeddingService
from backend.scripts.load_fixtures import load_csvs
from backend.scripts.seed_fixtures import seed_csv


class FakeModel:
    def encode(self, text, **kwargs):
        return [1.0, 0.0]


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
        assert len(params["query_embedding"]) == 2
        self.rpc_params = params
        return self

    def execute(self):
        return self


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
    assert client.rpc_params["match_count"] == 50

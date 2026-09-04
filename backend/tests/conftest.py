"""Shared e2e fixtures: a realistic in-memory Supabase double seeded from the
real generated fixtures, wired to the actual SupabaseRepository behind the API.

Unlike the InMemoryRepository used elsewhere (which returns pre-baked rows),
this drives the genuine reconciliation path — compare_records, the action
guards, trace persistence — through the HTTP layer. That closes the integration
seam the unit tests leave open.

No network, no Groq, no Supabase credentials.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.repository import SupabaseRepository
from backend.embeddings import EmbeddingService
from backend.reconciliation.rules import default_reference_time
from backend.scripts.seed_fixtures import generate_cases

REFERENCE = default_reference_time()

# Stable IDs the demo runbook and these tests reference, one per diagnosis path.
EXPECTED = {
    "TXNCLEAN001": ("clean", "no_action_needed"),
    "TXNLEDGERGAP001": ("ledger_gap", "ledger_entry_created"),
    "TXNPENDING001": ("pending", "no_action_needed"),
    "TXNANOMALY001": ("anomaly", "escalated"),
    "TXNAMOUNTMISMATCH001": ("amount_mismatch", "escalated"),
}


class _Query:
    """One table's query builder. Supports the exact chain the repo calls."""

    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows  # live reference to the table's list
        self._filters: list[tuple[str, str, Any]] = []
        self._limit: int | None = None
        self._update: dict[str, Any] | None = None

    def select(self, *_: Any) -> "_Query":
        return self

    def eq(self, key: str, value: Any) -> "_Query":
        self._filters.append(("eq", key, value))
        return self

    def gte(self, key: str, value: Any) -> "_Query":
        self._filters.append(("gte", key, value))
        return self

    def lte(self, key: str, value: Any) -> "_Query":
        self._filters.append(("lte", key, value))
        return self

    def limit(self, n: int) -> "_Query":
        self._limit = n
        return self

    def insert(self, row: dict[str, Any]) -> "_Query":
        self._rows.append(dict(row))
        return _Result([dict(row)])

    def upsert(self, row: dict[str, Any], on_conflict: str) -> "_Query":
        keys = [k.strip() for k in on_conflict.split(",")]
        existing = next(
            (r for r in self._rows if all(r.get(k) == row.get(k) for k in keys)),
            None,
        )
        if existing is not None:
            existing.update(row)
        else:
            self._rows.append(dict(row))
        return _Result([dict(row)])

    def update(self, row: dict[str, Any]) -> "_Query":
        self._update = row
        return self

    def _matches(self, r: dict[str, Any]) -> bool:
        for op, key, value in self._filters:
            cell = r.get(key)
            if op == "eq" and cell != value:
                return False
            if op == "gte" and not (cell is not None and str(cell) >= str(value)):
                return False
            if op == "lte" and not (cell is not None and str(cell) <= str(value)):
                return False
        return True

    def execute(self) -> "_Result":
        selected = [r for r in self._rows if self._matches(r)]
        if self._update is not None:
            for r in selected:
                r.update(self._update)
        if self._limit is not None:
            selected = selected[: self._limit]
        return _Result([dict(r) for r in selected])


class _Result:
    def __init__(self, data: list[dict[str, Any]]):
        self.data = data

    def execute(self) -> "_Result":  # insert/upsert return self, then .execute()
        return self


class FakeSupabase:
    """In-memory stand-in. Presence of `.tables` marks it as the test client."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]]):
        self.tables = tables

    def table(self, name: str) -> _Query:
        return _Query(self.tables.setdefault(name, []))


class FakeEmbeddingModel:
    def encode(self, text: str, **_: Any) -> list[float]:
        return [1.0] + [0.0] * 383


def _embedding_service() -> EmbeddingService:
    return EmbeddingService(model=FakeEmbeddingModel())


def _seed_tables() -> dict[str, list[dict[str, Any]]]:
    """Build the three feed tables from the real fixture generator."""
    gateway: list[dict[str, Any]] = []
    bank: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    for case in generate_cases(REFERENCE):
        gateway.append(case.gateway.model_dump(mode="json"))
        if case.bank is not None:
            bank.append(case.bank.model_dump(mode="json"))
        if case.ledger is not None:
            ledger.append(case.ledger.model_dump(mode="json"))
    return {
        "gateway_transactions": gateway,
        "bank_settlements": bank,
        "ledger_entries": ledger,
        "tickets": [],
        "agent_trace_logs": [],
    }


class FakeOrchestrator:
    """Deterministic stand-in for GroqOrchestrator.

    Performs the action the diagnosis authorizes via the real repository
    handlers (exercising the guarded action path end to end), and returns a
    canned explanation. It cannot influence the verdict — resolve() takes
    status/action from compare_records, not from here.
    """

    def __init__(self, explanation: str | None = None):
        self.handlers: dict[str, Any] = {}
        self.explanation = explanation

    def run(self, txn_id: str, diagnosis: dict[str, Any], on_trace: Any = None) -> Any:
        status = diagnosis["match_status"]
        if status == "ledger_gap":
            self.handlers["create_ledger_entry"](txn_id)
        elif status in ("anomaly", "amount_mismatch", "unknown"):
            self.handlers["raise_ticket"](txn_id, f"Escalated: {status}")
        explanation = self.explanation or f"[LLM] Explanation for {status}."
        return SimpleNamespace(
            response=SimpleNamespace(explanation=explanation),
            trace=[],
            model="fake-model",
            fallback_used=False,
        )


@pytest.fixture
def seeded_tables() -> dict[str, list[dict[str, Any]]]:
    return _seed_tables()


@pytest.fixture
def repo(seeded_tables: dict[str, list[dict[str, Any]]]) -> SupabaseRepository:
    """Real repository over the fake, no orchestrator (deterministic only)."""
    return SupabaseRepository(
        FakeSupabase(seeded_tables),
        reference_time=REFERENCE,
        embedding_service=_embedding_service(),
    )


@pytest.fixture
def client(repo: SupabaseRepository) -> TestClient:
    return TestClient(create_app(Settings(require_auth=False), repo))


@pytest.fixture
def make_client(seeded_tables: dict[str, list[dict[str, Any]]]):
    """Factory to build a client with an orchestrator wired in."""

    def _build(orchestrator: Any | None = None) -> tuple[TestClient, SupabaseRepository]:
        built = SupabaseRepository(
            FakeSupabase(seeded_tables),
            reference_time=REFERENCE,
            orchestrator=orchestrator,
            embedding_service=_embedding_service(),
        )
        return TestClient(create_app(Settings(require_auth=False), built)), built

    return _build

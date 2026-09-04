from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from backend.domain.models import BankSettlementRecord, FixtureCase, GatewayRecord, LedgerRecord
from backend.reconciliation.rules import classify_transaction


class TransactionNotFound(LookupError):
    pass


class UnavailableRepository:
    def __getattr__(self, name: str) -> Any:
        def unavailable(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Supabase is not configured")
        return unavailable


class InMemoryRepository:
    def __init__(self, records: dict[str, Any] | None = None) -> None:
        self.records = records or {}
        self._traces: dict[str, Any] = {}

    def resolve(self, txn_id: str, request_id: str = "local-request") -> dict[str, Any]:
        if txn_id not in self.records:
            raise TransactionNotFound(txn_id)
        row = dict(self.records[txn_id])
        row.setdefault("run_id", str(uuid4()))
        row.setdefault("created_at", datetime.now(timezone.utc))
        row.setdefault("steps", [])
        row.setdefault("request_id", request_id)
        self._traces[txn_id] = row
        return row

    def trace(self, txn_id: str) -> dict[str, Any]:
        if txn_id not in self._traces:
            raise TransactionNotFound(txn_id)
        row = self._traces[txn_id]
        return {"request_id": row["request_id"], "run_id": row["run_id"], "created_at": row["created_at"], "steps": row["steps"]}

    def tickets(self, action_taken: str | None = None, confidence: str | None = None) -> list[dict[str, Any]]:
        rows = []
        for row in self.records.values():
            if "diagnosis" in row or "status" in row and "ticket_id" in row:
                normalized = dict(row)
                normalized.setdefault("txn_id", normalized.get("transaction_id"))
                normalized.setdefault("diagnosis", normalized.get("status", "unknown"))
                normalized.setdefault("action_taken", normalized.get("action", "no_action_needed"))
                if isinstance(normalized.get("confidence"), (int, float)):
                    normalized["confidence"] = "high" if normalized["confidence"] >= 0.7 else "low_flagged_for_review"
                else:
                    normalized.setdefault("confidence", "high")
                rows.append(normalized)
        if action_taken:
            rows = [row for row in rows if row.get("action_taken") == action_taken]
        if confidence:
            rows = [row for row in rows if row.get("confidence") == ("low_flagged_for_review" if confidence == "low" else confidence) or (confidence == "low" and isinstance(row.get("confidence"), (int, float)) and row["confidence"] < 0.7)]
        return rows

    def analytics(self) -> dict[str, dict[str, int]]:
        rows = self.tickets()
        return {"by_action": _counts(rows, "action_taken"), "by_confidence": _counts(rows, "confidence")}

    def exceptions(self) -> list[dict[str, Any]]:
        return self.tickets(confidence="low_flagged_for_review")

    def reconcile(self, date_from: date, date_to: date, request_id: str) -> list[dict[str, Any]]:
        return [self.resolve(txn_id, request_id) for txn_id, row in self.records.items() if date_from <= row.get("captured_at", row.get("occurred_at", datetime.min)).date() <= date_to]


def _counts(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = row.get(key, "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


class SupabaseRepository:
    """Synchronous canonical-schema adapter. API callers run it off-loop."""

    def __init__(self, client: Any, reference_time: datetime | None = None) -> None:
        self.client = client
        self.reference_time = reference_time or datetime.now(timezone.utc)

    def _one(self, table: str, txn_id: str) -> dict[str, Any] | None:
        aliases = {"gateway_transactions": "gateway_records", "ledger_entries": "ledger_records"}
        available = getattr(self.client, "tables", None)
        query_table = aliases.get(table, table) if isinstance(available, dict) and table not in available else table
        response = self.client.table(query_table).select("*").eq("txn_id", txn_id).limit(1).execute()
        row = (response.data or [None])[0]
        if row is None:
            response = self.client.table(query_table).select("*").eq("transaction_id", txn_id).limit(1).execute()
            row = (response.data or [None])[0]
        return row

    def resolve(self, txn_id: str, request_id: str = "local-request") -> dict[str, Any]:
        gateway = self._one("gateway_transactions", txn_id)
        if gateway is None:
            raise TransactionNotFound(txn_id)
        bank = self._one("bank_settlements", txn_id)
        ledger = self._one("ledger_entries", txn_id)
        case = self._case(gateway, bank, ledger)
        status = classify_transaction(case, self.reference_time).value
        action, explanation = self._outcome(status)
        run_id = str(uuid4())
        steps = [
            {"run_id": run_id, "txn_id": txn_id, "step_number": 1, "step_name": "gateway_lookup", "step_status": "ok", "step_result": "found"},
            {"run_id": run_id, "txn_id": txn_id, "step_number": 2, "step_name": "bank_lookup", "step_status": "ok", "step_result": "found" if bank else "not_found"},
            {"run_id": run_id, "txn_id": txn_id, "step_number": 3, "step_name": "ledger_lookup", "step_status": "ok", "step_result": "found" if ledger else "not_found"},
            {"run_id": run_id, "txn_id": txn_id, "step_number": 4, "step_name": "diagnosis", "step_status": "ok", "step_result": status},
        ]
        for step in steps:
            self.client.table("agent_trace_logs").insert(step).execute()
        return {"txn_id": txn_id, "status": status, "action": action, "explanation": explanation, "request_id": request_id, "run_id": run_id, "created_at": datetime.now(timezone.utc), "steps": steps}

    def _case(self, gateway: dict[str, Any], bank: dict[str, Any] | None, ledger: dict[str, Any] | None) -> FixtureCase:
        expected = gateway.get("expected_settlement_at")
        if expected is None:
            raise ValueError("expected_settlement_at is required")
        g = GatewayRecord(transaction_id=gateway.get("txn_id", gateway.get("transaction_id")), amount=gateway["amount"], currency=gateway["currency"], occurred_at=gateway.get("captured_at", gateway.get("occurred_at")), status=gateway["status"])
        b = BankSettlementRecord(transaction_id=bank.get("txn_id", bank.get("transaction_id")), amount=bank.get("amount") or 0, currency=bank["currency"], occurred_at=bank.get("settled_at") or bank.get("occurred_at") or bank.get("created_at"), status=bank.get("status") or "pending", settled_at=bank.get("settled_at")) if bank else None
        l = LedgerRecord(transaction_id=ledger.get("txn_id", ledger.get("transaction_id")), amount=ledger.get("amount") or 0, currency=ledger["currency"], occurred_at=ledger.get("recorded_at") or ledger.get("occurred_at") or ledger.get("created_at"), recorded_at=ledger.get("recorded_at") or ledger.get("occurred_at") or ledger.get("created_at"), status=ledger.get("status", "recorded")) if ledger else None
        return FixtureCase(transaction_id=g.transaction_id, path="clean", gateway=g, bank=b, ledger=l, expected_settlement_at=expected)

    @staticmethod
    def _outcome(status: str) -> tuple[str, str]:
        outcomes = {"clean": ("no_action_needed", "Gateway, bank, and ledger records match."), "ledger_gap": ("ledger_entry_created", "Settlement exists but the ledger entry is missing."), "pending": ("no_action_needed", "Bank settlement is pending within the T+2 window."), "anomaly": ("escalated", "Settlement data is missing or outside the expected window."), "amount_mismatch": ("escalated", "Gateway and settlement amounts differ beyond tolerance.")}
        return outcomes[status]

    def tickets(self, action_taken: str | None = None, confidence: str | None = None) -> list[dict[str, Any]]:
        query = self.client.table("tickets").select("txn_id,diagnosis,explanation,action_taken,confidence")
        if action_taken:
            query = query.eq("action_taken", action_taken)
        if confidence:
            query = query.eq("confidence", confidence)
        return query.execute().data or []

    def analytics(self) -> dict[str, dict[str, int]]:
        rows = self.tickets()
        by_action: dict[str, int] = {}
        by_confidence: dict[str, int] = {}
        for row in rows:
            by_action[row["action_taken"]] = by_action.get(row["action_taken"], 0) + 1
            value = row["confidence"]
            bucket = ("high" if value >= 0.7 else "low") if isinstance(value, (int, float)) else value
            by_confidence[bucket] = by_confidence.get(bucket, 0) + 1
        return {"by_action": by_action, "by_confidence": by_confidence}

    def exceptions(self) -> list[dict[str, Any]]:
        return self.tickets(confidence="low_flagged_for_review")

    def trace(self, txn_id: str) -> dict[str, Any]:
        response = self.client.table("agent_trace_logs").select("*").eq("txn_id", txn_id).order("created_at").execute()
        rows = response.data or []
        if not rows:
            raise TransactionNotFound(txn_id)
        return {"request_id": rows[-1].get("detail", {}).get("request_id", "unknown"), "run_id": rows[-1]["run_id"], "created_at": rows[0]["created_at"], "steps": rows}

    def reconcile(self, date_from: date, date_to: date, request_id: str) -> list[dict[str, Any]]:
        response = self.client.table("gateway_transactions").select("txn_id").gte("captured_at", date_from.isoformat()).lte("captured_at", f"{date_to.isoformat()}T23:59:59+00:00").execute()
        return [self.resolve(row["txn_id"], request_id) for row in (response.data or [])]

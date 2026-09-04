from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from backend.domain.models import (
    BankSettlement,
    Confidence,
    GatewayTransaction,
    LedgerEntry,
    MatchStatus,
)
from backend.reconciliation.rules import compare_records
from backend.agent import GroqOrchestrationError
from backend.agent_tools import search_similar_tickets
from backend.embeddings import EmbeddingService, EmbeddingServiceError
from backend.domain.trace import TraceEvent


TraceCallback = Callable[[dict[str, Any]], None]
ACTION_TOOL_NAMES = {"create_ledger_entry", "raise_ticket", "close_as_resolved"}

# Which record model owns which columns, and how each source's canonical
# timestamp column maps onto the model field.
_GATEWAY_FIELDS = set(GatewayTransaction.model_fields)
_BANK_FIELDS = set(BankSettlement.model_fields)
_LEDGER_FIELDS = set(LedgerEntry.model_fields)


def _prune(row: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    """Keep only model fields; the DB row carries id/created_at/owner_id too."""
    return {key: value for key, value in row.items() if key in allowed}


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

    def resolve(
        self,
        txn_id: str,
        request_id: str = "local-request",
        on_event: TraceCallback | None = None,
    ) -> dict[str, Any]:
        run_id = str(uuid4())
        created_at = datetime.now(timezone.utc)
        steps: list[dict[str, Any]] = []

        def emit(event_type: Any, step_name: str, status: Any, summary: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
            event = TraceEvent.create(
                transaction_id=txn_id,
                run_id=run_id,
                request_id=request_id,
                step_number=len(steps) + 1,
                event_type=event_type,
                step_name=step_name,
                status=status,
                summary=summary,
                detail=detail,
            ).model_dump(mode="json")
            steps.append(event)
            if on_event is not None:
                on_event(event)
            return event

        emit("tool_start", "lookup_gateway", "running", "Checking gateway record")
        row = self.records.get(txn_id)
        if row is None:
            emit("tool_result", "lookup_gateway", "not_found", "Checked gateway -> no record")
            emit("completion", "resolve", "failed", "Resolution stopped: transaction was not found", {"error_code": "TXN_NOT_FOUND"})
            self._traces[txn_id] = {"request_id": request_id, "run_id": run_id, "created_at": created_at, "steps": steps}
            raise TransactionNotFound(txn_id)

        row = dict(row)
        emit("tool_result", "lookup_gateway", "success", "Checked gateway -> record found", {"status": row.get("status")})
        emit("tool_start", "lookup_bank", "running", "Checking bank settlement")
        bank_present = bool(row.get("bank_present", True))
        emit("tool_result", "lookup_bank", "success" if bank_present else "not_found", "Checked bank -> record found" if bank_present else "Checked bank -> no record")
        emit("tool_start", "lookup_ledger", "running", "Checking ledger record")
        ledger_present = bool(row.get("ledger_present", row.get("ledger")))
        emit("tool_result", "lookup_ledger", "success" if ledger_present else "not_found", "Checked ledger -> record found" if ledger_present else "Checked ledger -> no record")
        status = str(row.get("status", row.get("diagnosis", "unknown")))
        action = str(row.get("action", row.get("action_taken", "no_action_needed")))
        detail = {"match_status": status, "reason_code": status.upper(), "confidence": row.get("confidence", "high")}
        emit("decision", "compare_records", "success", f"Decision: {status.replace('_', ' ')}", detail)
        emit("action", "resolution_action", "success", f"Action: {action.replace('_', ' ')}", {"action": action})
        explanation = row.get("explanation", "Resolution completed.")
        result = {"txn_id": txn_id, "transaction_id": txn_id, "status": status, "action": action, "explanation": explanation, "request_id": request_id, "run_id": run_id, "created_at": created_at, "steps": steps}
        emit("completion", "resolve", "completed", "Resolution completed", {"resolution": {"txn_id": txn_id, "transaction_id": txn_id, "status": status, "action": action, "explanation": explanation}})
        self._traces[txn_id] = {"request_id": request_id, "run_id": run_id, "created_at": created_at, "steps": steps}
        return result

    def trace(self, txn_id: str, owner_id: str | None = None) -> dict[str, Any]:
        if owner_id is not None and not self.can_access_transaction(txn_id, owner_id):
            raise TransactionNotFound(txn_id)
        if txn_id not in self._traces:
            raise TransactionNotFound(txn_id)
        row = self._traces[txn_id]
        return {"request_id": row["request_id"], "run_id": row["run_id"], "created_at": row["created_at"], "steps": row["steps"]}

    def can_access_transaction(self, txn_id: str, user_id: str) -> bool:
        row = self.records.get(txn_id)
        return bool(row and str(row.get("owner_id", "")) == user_id)

    def tickets(self, action_taken: str | None = None, confidence: str | None = None, owner_id: str | None = None) -> list[dict[str, Any]]:
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
                if owner_id is not None and str(normalized.get("owner_id", "")) != owner_id:
                    continue
                rows.append(normalized)
        if action_taken:
            rows = [row for row in rows if row.get("action_taken") == action_taken]
        if confidence:
            rows = [row for row in rows if row.get("confidence") == ("low_flagged_for_review" if confidence == "low" else confidence) or (confidence == "low" and isinstance(row.get("confidence"), (int, float)) and row["confidence"] < 0.7)]
        return rows

    def analytics(self, owner_id: str | None = None) -> dict[str, dict[str, int]]:
        rows = self.tickets(owner_id=owner_id)
        return {"by_action": _counts(rows, "action_taken"), "by_confidence": _counts(rows, "confidence")}

    def exceptions(self, owner_id: str | None = None) -> list[dict[str, Any]]:
        return self.tickets(confidence="low_flagged_for_review", owner_id=owner_id)

    def reconcile(self, date_from: date, date_to: date, request_id: str, owner_id: str | None = None) -> list[dict[str, Any]]:
        return [
            self.resolve(txn_id, request_id)
            for txn_id, row in self.records.items()
            if (owner_id is None or str(row.get("owner_id", "")) == owner_id)
            and date_from <= row.get("captured_at", row.get("occurred_at", datetime.min)).date() <= date_to
        ]


def _counts(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = row.get(key, "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def json_safe(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


class SupabaseRepository:
    """Synchronous canonical-schema adapter. API callers run it off-loop."""

    def __init__(self, client: Any, reference_time: datetime | None = None, orchestrator: Any | None = None, embedding_service: EmbeddingService | None = None, similarity_threshold: float = 0.75, similarity_match_count: int = 3) -> None:
        self.client = client
        self.reference_time = reference_time or datetime.now(timezone.utc)
        self.orchestrator = orchestrator
        self.embedding_service = embedding_service or EmbeddingService()
        self.similarity_threshold = similarity_threshold
        self.similarity_match_count = similarity_match_count

    def _one(self, table: str, txn_id: str) -> dict[str, Any] | None:
        aliases = {"gateway_transactions": "gateway_records", "ledger_entries": "ledger_records"}
        available = getattr(self.client, "tables", None)
        query_table = aliases.get(table, table) if isinstance(available, dict) and table not in available else table
        response = self.client.table(query_table).select("*").eq("txn_id", txn_id).limit(1).execute()
        row = (response.data or [None])[0]
        if row is None and isinstance(available, dict):
            # Legacy fixture clients keyed rows on transaction_id. The live
            # schema has no such column, so only try this fallback against the
            # in-memory test client, never against Postgres (where it 500s).
            response = self.client.table(query_table).select("*").eq("transaction_id", txn_id).limit(1).execute()
            row = (response.data or [None])[0]
        return row

    def can_access_transaction(self, txn_id: str, user_id: str) -> bool:
        gateway = self._one("gateway_transactions", txn_id)
        return bool(gateway and str(gateway.get("owner_id", "")) == user_id)

    def _owned_transaction_ids(self, owner_id: str) -> set[str]:
        response = (
            self.client.table("gateway_transactions")
            .select("txn_id")
            .eq("owner_id", owner_id)
            .execute()
        )
        return {
            str(row.get("txn_id", row.get("transaction_id")))
            for row in (response.data or [])
            if row.get("txn_id", row.get("transaction_id"))
        }

    def resolve(
        self,
        txn_id: str,
        request_id: str = "local-request",
        on_event: TraceCallback | None = None,
    ) -> dict[str, Any]:
        run_id = str(uuid4())
        created_at = datetime.now(timezone.utc)
        steps: list[dict[str, Any]] = []

        def emit(event_type: Any, step_name: str, status: Any, summary: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
            event = TraceEvent.create(
                transaction_id=txn_id,
                run_id=run_id,
                request_id=request_id,
                step_number=len(steps) + 1,
                event_type=event_type,
                step_name=step_name,
                status=status,
                summary=summary,
                detail=detail,
            ).model_dump(mode="json")
            self._persist_trace_event(event)
            steps.append(event)
            if on_event is not None:
                on_event(event)
            return event

        emit("tool_start", "lookup_gateway", "running", "Checking gateway record")
        try:
            gateway = self._one("gateway_transactions", txn_id)
        except Exception as exc:
            emit("tool_result", "lookup_gateway", "failed", "Gateway lookup failed", {"error": "gateway_unavailable"})
            emit("completion", "resolve", "failed", "Resolution stopped after gateway failure", {"error_code": "DEPENDENCY_UNAVAILABLE"})
            raise RuntimeError("Gateway lookup failed") from exc
        if gateway is None:
            emit("tool_result", "lookup_gateway", "not_found", "Checked gateway -> no record")
            emit("completion", "resolve", "failed", "Resolution stopped: transaction was not found", {"error_code": "TXN_NOT_FOUND"})
            raise TransactionNotFound(txn_id)
        emit("tool_result", "lookup_gateway", "success", "Checked gateway -> record found", {"status": gateway.get("status")})

        emit("tool_start", "lookup_bank", "running", "Checking bank settlement")
        try:
            bank = self._one("bank_settlements", txn_id)
        except Exception as exc:
            emit("tool_result", "lookup_bank", "failed", "Bank lookup failed", {"error": "bank_unavailable"})
            emit("completion", "resolve", "failed", "Resolution stopped after bank failure", {"error_code": "DEPENDENCY_UNAVAILABLE"})
            raise RuntimeError("Bank lookup failed") from exc
        emit("tool_result", "lookup_bank", "success" if bank else "not_found", "Checked bank -> record found" if bank else "Checked bank -> no record")

        emit("tool_start", "lookup_ledger", "running", "Checking ledger record")
        try:
            ledger = self._one("ledger_entries", txn_id)
        except Exception as exc:
            emit("tool_result", "lookup_ledger", "failed", "Ledger lookup failed", {"error": "ledger_unavailable"})
            emit("completion", "resolve", "failed", "Resolution stopped after ledger failure", {"error_code": "DEPENDENCY_UNAVAILABLE"})
            raise RuntimeError("Ledger lookup failed") from exc
        emit("tool_result", "lookup_ledger", "success" if ledger else "not_found", "Checked ledger -> record found" if ledger else "Checked ledger -> no record")

        g_record, b_record, l_record = self._records(gateway, bank, ledger)
        verdict = compare_records(g_record, b_record, l_record, self.reference_time, txn_id=txn_id)
        classified_status = verdict.match_status.value
        action, explanation = self._outcome(classified_status)
        diagnosis = {
            "match_status": classified_status,
            "confidence": verdict.confidence.value,
            "reason_code": verdict.reason_code,
            "action": action,
            "detail": verdict.detail,
        }
        emit("decision", "compare_records", "success", f"Decision: {classified_status.replace('_', ' ')}", diagnosis)

        if self.orchestrator is not None:
            try:
                self.orchestrator.handlers = {
                    "lookup_gateway": lambda txn_id: self._one("gateway_transactions", txn_id),
                    "lookup_bank": lambda txn_id: self._one("bank_settlements", txn_id),
                    "lookup_ledger": lambda txn_id: self._one("ledger_entries", txn_id),
                    "search_similar_tickets": lambda query, limit=None: self._similar_tickets(query, limit),
                    "create_ledger_entry": lambda txn_id, evidence=None: self.create_ledger_entry(txn_id, evidence=evidence or diagnosis),
                    "raise_ticket": lambda txn_id, reason, evidence=None: self.raise_ticket(txn_id, reason=reason, evidence=evidence or diagnosis),
                    "close_as_resolved": lambda txn_id, evidence=None: self.close_as_resolved(txn_id, evidence=evidence or diagnosis),
                }
                run = self.orchestrator.run(txn_id, diagnosis, on_trace=lambda event: self._record_agent_trace(event, emit))
                explanation = run.response.explanation
            except GroqOrchestrationError:
                emit("action", "groq_fallback", "warning", "Groq unavailable; deterministic explanation retained", {"action": action})
        else:
            emit("action", "resolution_action", "success", f"Action: {action.replace('_', ' ')}", {"action": action})

        result = {"txn_id": txn_id, "transaction_id": txn_id, "status": classified_status, "action": action, "explanation": explanation, "request_id": request_id, "run_id": run_id, "created_at": created_at, "steps": steps}
        emit("completion", "resolve", "completed", "Resolution completed", {"resolution": {"txn_id": txn_id, "transaction_id": txn_id, "status": classified_status, "action": action, "explanation": explanation}})
        result["steps"] = steps
        return result

    def _persist_trace_event(self, event: dict[str, Any]) -> None:
        legacy_status = {"running": "ok", "success": "ok", "completed": "ok", "warning": "warning", "not_found": "not_found", "failed": "error"}[event["status"]]
        detail = dict(event.get("detail") or {})
        detail.setdefault("request_id", event["request_id"])
        row = {
            "event_id": event["event_id"],
            "event_type": event["event_type"],
            "status": event["status"],
            "summary": event["summary"],
            "event_timestamp": event["timestamp"],
            "run_id": event["run_id"],
            "txn_id": event["transaction_id"],
            "step_number": event["step_number"],
            "step_name": event["step_name"],
            "step_status": legacy_status,
            "step_result": event["summary"],
            "detail": detail,
        }
        query = self.client.table("agent_trace_logs")
        upsert = getattr(query, "upsert", None)
        if callable(upsert):
            upsert(row, on_conflict="run_id,step_number").execute()
        else:
            query.insert(row).execute()

    @staticmethod
    def _record_agent_trace(agent_event: Any, emit: Callable[..., dict[str, Any]]) -> None:
        name = agent_event.name
        kind = agent_event.kind
        detail = dict(agent_event.payload or {})
        if kind == "tool_call":
            event_type = "action" if name in ACTION_TOOL_NAMES else "tool_start"
            emit(event_type, name, "running", f"Starting {name.replace('_', ' ')}", detail)
        elif kind == "tool_result":
            event_type = "action" if name in ACTION_TOOL_NAMES else "tool_result"
            missing_result = detail.get("value") is None and not detail.get("status")
            status = "failed" if detail.get("error") else "not_found" if missing_result else "success"
            if detail.get("status") in {"not_authorized", "failed"}:
                status = "warning"
            if status == "failed":
                summary = f"{name.replace('_', ' ').capitalize()} failed"
            elif status == "warning":
                summary = f"{name.replace('_', ' ').capitalize()} returned a warning"
            elif status == "not_found":
                summary = f"{name.replace('_', ' ').capitalize()} -> no record"
            else:
                summary = f"Completed {name.replace('_', ' ')}"
            emit(event_type, name, status, summary, detail)
        elif kind == "retry":
            emit("retry", name or "groq_completion", "warning", "Retrying external model call", detail)
        elif kind == "diagnosis_divergence":
            emit("decision", "model_output", "warning", "Model output differed; deterministic diagnosis retained", detail)

    def create_ledger_entry(self, txn_id: str, evidence: dict[str, Any] | None = None, action_key: str | None = None) -> dict[str, Any]:
        if not evidence or evidence.get("match_status") != "ledger_gap":
            return {"status": "not_authorized", "txn_id": txn_id, "reason": "structured ledger_gap evidence is required"}
        gateway = self._one("gateway_transactions", txn_id)
        if gateway is None:
            raise TransactionNotFound(txn_id)
        bank = self._one("bank_settlements", txn_id)
        ledger = self._one("ledger_entries", txn_id)
        if ledger is not None and ledger.get("source", "agent_reconciliation") == "agent_reconciliation":
            return {"status": "already_exists", "txn_id": txn_id, "record": ledger, "action_key": action_key or f"create_ledger_entry:{txn_id}"}
        if self._status(gateway, bank, ledger) != "ledger_gap" or not bank or abs(float(gateway["amount"]) - float(bank["amount"])) > 0.01:
            return {"status": "not_authorized", "txn_id": txn_id, "reason": "current records are not a valid ledger_gap"}
        if ledger is not None:
            return {"status": "already_exists", "txn_id": txn_id, "record": ledger, "action_key": action_key or f"create_ledger_entry:{txn_id}"}
        row = {"txn_id": txn_id, "amount": gateway["amount"], "currency": gateway["currency"], "status": "recorded", "source": "agent_reconciliation", "recorded_at": datetime.now(timezone.utc).isoformat()}
        try:
            self.client.table("ledger_entries").upsert(row, on_conflict="txn_id").execute()
        except Exception:
            existing = self._one("ledger_entries", txn_id)
            if existing is not None:
                return {"status": "already_exists", "txn_id": txn_id, "record": existing, "action_key": action_key or f"create_ledger_entry:{txn_id}"}
            raise
        return {"status": "created", "txn_id": txn_id, "record": row, "action_key": action_key or f"create_ledger_entry:{txn_id}"}

    def raise_ticket(self, txn_id: str, reason: str, evidence: dict[str, Any] | None = None, action_key: str | None = None) -> dict[str, Any]:
        if not reason or not reason.strip() or not evidence:
            return {"status": "not_authorized", "txn_id": txn_id, "reason": "reason and structured evidence are required"}
        gateway = self._one("gateway_transactions", txn_id)
        if gateway is None:
            raise TransactionNotFound(txn_id)
        status = self._status(gateway, self._one("bank_settlements", txn_id), self._one("ledger_entries", txn_id))
        if status not in {"anomaly", "amount_mismatch"} or evidence.get("match_status") != status:
            return {"status": "not_authorized", "txn_id": txn_id, "reason": "current diagnosis does not authorize a ticket"}
        confidence = "low_flagged_for_review" if status == "anomaly" else "high"
        row = {"txn_id": txn_id, "diagnosis": status, "explanation": reason.strip(), "action_taken": "escalated", "confidence": confidence, "detail": {"evidence": evidence, "action_key": action_key or f"raise_ticket:{txn_id}"}}
        try:
            row["embedding"] = self.embedding_service.embed(reason)
        except (EmbeddingServiceError, ValueError, TypeError):
            pass
        self.client.table("tickets").upsert(row, on_conflict="txn_id").execute()
        return {"status": "created", "txn_id": txn_id, "record": row, "action_key": action_key or f"raise_ticket:{txn_id}"}

    def close_as_resolved(self, txn_id: str, evidence: dict[str, Any] | None = None, action_key: str | None = None) -> dict[str, Any]:
        if not evidence:
            return {"status": "not_authorized", "txn_id": txn_id, "reason": "structured evidence is required"}
        gateway = self._one("gateway_transactions", txn_id)
        ticket = self.client.table("tickets").select("*").eq("txn_id", txn_id).limit(1).execute().data
        if gateway is None or not ticket:
            raise TransactionNotFound(txn_id)
        status = self._status(gateway, self._one("bank_settlements", txn_id), self._one("ledger_entries", txn_id))
        if status not in {"clean", "pending"} or evidence.get("match_status") != status:
            return {"status": "not_authorized", "txn_id": txn_id, "reason": "transaction is not safely resolvable"}
        update = {"action_taken": "no_action_needed", "resolved_at": datetime.now(timezone.utc).isoformat(), "detail": {"evidence": evidence, "action_key": action_key or f"close_as_resolved:{txn_id}"}}
        self.client.table("tickets").update(update).eq("txn_id", txn_id).execute()
        return {"status": "closed", "txn_id": txn_id, "record": {**ticket[0], **update}, "action_key": action_key or f"close_as_resolved:{txn_id}"}

    _create_ledger_entry = create_ledger_entry
    _raise_ticket = raise_ticket
    _close_as_resolved = close_as_resolved

    def _similar_tickets(self, query: str, limit: int | None = None) -> dict[str, Any]:
        try:
            return {"results": search_similar_tickets(query, self.client, self.embedding_service, threshold=self.similarity_threshold, limit=limit or self.similarity_match_count)}
        except Exception:
            return {"results": []}

    def _status(
        self,
        gateway: dict[str, Any],
        bank: dict[str, Any] | None,
        ledger: dict[str, Any] | None,
    ) -> str:
        """match_status string for a set of raw rows, via compare_records."""
        g, b, l = self._records(gateway, bank, ledger)
        return compare_records(g, b, l, self.reference_time, txn_id=g.txn_id).match_status.value

    def _records(
        self,
        gateway: dict[str, Any],
        bank: dict[str, Any] | None,
        ledger: dict[str, Any] | None,
    ) -> tuple[GatewayTransaction, BankSettlement | None, LedgerEntry | None]:
        """Turn raw DB rows into the typed records compare_records expects.

        Older callers used transaction_id/occurred_at; the live schema uses
        txn_id and per-source timestamp columns. Both are tolerated on read.
        """
        def canonicalise(row: dict[str, Any]) -> dict[str, Any]:
            out = dict(row)
            if "txn_id" not in out and "transaction_id" in out:
                out["txn_id"] = out["transaction_id"]
            return out

        g_row = canonicalise(gateway)
        if "captured_at" not in g_row and "occurred_at" in g_row:
            g_row["captured_at"] = g_row["occurred_at"]
        g = GatewayTransaction(**_prune(g_row, _GATEWAY_FIELDS))

        b: BankSettlement | None = None
        if bank is not None:
            b_row = canonicalise(bank)
            b = BankSettlement(**_prune(b_row, _BANK_FIELDS))

        l: LedgerEntry | None = None
        if ledger is not None:
            l_row = canonicalise(ledger)
            l = LedgerEntry(**_prune(l_row, _LEDGER_FIELDS))

        return g, b, l

    @staticmethod
    def _outcome(status: str) -> tuple[str, str]:
        outcomes = {
            "clean": ("no_action_needed", "Gateway, bank, and ledger records match."),
            "ledger_gap": ("ledger_entry_created", "Settlement exists but the ledger entry is missing."),
            "pending": ("no_action_needed", "Bank settlement is pending within the T+2 window."),
            "anomaly": ("escalated", "Settlement data is missing or outside the expected window."),
            "amount_mismatch": ("escalated", "Gateway and settlement amounts differ beyond tolerance."),
            "unknown": ("escalated", "The transaction could not be classified from the available records."),
        }
        return outcomes.get(status, ("escalated", "Unclassified transaction flagged for review."))

    def tickets(self, action_taken: str | None = None, confidence: str | None = None, owner_id: str | None = None) -> list[dict[str, Any]]:
        query = self.client.table("tickets").select("txn_id,diagnosis,explanation,action_taken,confidence")
        if action_taken:
            query = query.eq("action_taken", action_taken)
        if confidence:
            query = query.eq("confidence", confidence)
        rows = query.execute().data or []
        if owner_id is not None:
            owned_ids = self._owned_transaction_ids(owner_id)
            rows = [row for row in rows if str(row.get("txn_id", row.get("transaction_id"))) in owned_ids]
        return rows

    def analytics(self, owner_id: str | None = None) -> dict[str, dict[str, int]]:
        rows = self.tickets(owner_id=owner_id)
        by_action: dict[str, int] = {}
        by_confidence: dict[str, int] = {}
        for row in rows:
            by_action[row["action_taken"]] = by_action.get(row["action_taken"], 0) + 1
            value = row["confidence"]
            bucket = ("high" if value >= 0.7 else "low") if isinstance(value, (int, float)) else value
            by_confidence[bucket] = by_confidence.get(bucket, 0) + 1
        return {"by_action": by_action, "by_confidence": by_confidence}

    def exceptions(self, owner_id: str | None = None) -> list[dict[str, Any]]:
        return self.tickets(confidence="low_flagged_for_review", owner_id=owner_id)

    def trace(self, txn_id: str, owner_id: str | None = None) -> dict[str, Any]:
        if owner_id is not None and not self.can_access_transaction(txn_id, owner_id):
            raise TransactionNotFound(txn_id)
        response = self.client.table("agent_trace_logs").select("*").eq("txn_id", txn_id).execute()
        rows = response.data or []
        if not rows:
            raise TransactionNotFound(txn_id)
        latest_run = max(rows, key=lambda row: self._trace_time(row)).get("run_id")
        selected = [row for row in rows if row.get("run_id") == latest_run]
        events = [self._canonical_event(row) for row in selected]
        events.sort(key=lambda event: event["step_number"])
        return {"request_id": events[0]["request_id"], "run_id": events[0]["run_id"], "created_at": events[0]["timestamp"], "steps": events}

    @staticmethod
    def _trace_time(row: dict[str, Any]) -> datetime:
        value = row.get("event_timestamp") or row.get("created_at")
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass
        return datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _canonical_event(row: dict[str, Any]) -> dict[str, Any]:
        run_id = str(row.get("run_id", "unknown"))
        step_number = int(row.get("step_number", 1))
        detail = dict(row.get("detail") or {})
        request_id = str(detail.get("request_id", "unknown"))
        event_type = row.get("event_type")
        if event_type not in {"tool_start", "tool_result", "decision", "action", "retry", "completion"}:
            name = str(row.get("step_name", "trace"))
            event_type = "decision" if name in {"diagnosis", "compare_records"} else "tool_start" if name.startswith("tool_call:") else "tool_result"
        status = row.get("status")
        if status not in {"running", "success", "warning", "not_found", "failed", "completed"}:
            status = {"ok": "success", "not_found": "not_found", "warning": "warning", "error": "failed"}.get(row.get("step_status"), "warning")
        timestamp = row.get("event_timestamp") or row.get("created_at") or datetime.now(timezone.utc)
        return TraceEvent(
            event_id=str(row.get("event_id") or f"{run_id}:{step_number}"),
            transaction_id=str(row.get("transaction_id") or row.get("txn_id")),
            run_id=run_id,
            request_id=request_id,
            step_number=step_number,
            event_type=event_type,
            step_name=str(row.get("step_name", "trace")),
            status=status,
            summary=str(row.get("summary") or row.get("step_result") or row.get("step_name", "Trace event")),
            detail=detail,
            timestamp=timestamp,
        ).model_dump(mode="json")

    def reconcile(self, date_from: date, date_to: date, request_id: str, owner_id: str | None = None) -> list[dict[str, Any]]:
        query = self.client.table("gateway_transactions").select("txn_id").gte("captured_at", date_from.isoformat()).lte("captured_at", f"{date_to.isoformat()}T23:59:59+00:00")
        if owner_id is not None:
            query = query.eq("owner_id", owner_id)
        response = query.execute()
        return [self.resolve(row["txn_id"], request_id) for row in (response.data or [])]

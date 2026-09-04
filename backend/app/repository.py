from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Iterator
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
from .trace_events import (
    KIND_ACTION,
    KIND_COMPLETION,
    KIND_DECISION,
    KIND_RETRY,
    KIND_TOOL_RESULT,
    KIND_TOOL_START,
    PERSISTABLE_STATUSES,
    STATUS_NOT_FOUND,
    STATUS_OK,
    STATUS_PENDING,
    STATUS_WARNING,
    as_trace_step,
    make_event,
)

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

    def iter_resolve(self, txn_id: str, request_id: str = "local-request") -> Iterator[dict[str, Any]]:
        """Yield trace events for a stored record (local/demo path, no Supabase)."""
        if txn_id not in self.records:
            raise TransactionNotFound(txn_id)
        row = dict(self.records[txn_id])
        run_id = str(uuid4())
        status = row.get("status", "unknown")
        action = row.get("action", "no_action_needed")
        explanation = row.get("explanation", "")
        counter = 0
        steps: list[dict[str, Any]] = []

        def emit(kind: str, name: str, st: str, summary: str, detail: dict[str, Any] | None = None, persist: bool = True) -> dict[str, Any]:
            nonlocal counter
            counter += 1
            event = make_event(run_id=run_id, txn_id=txn_id, step_number=counter, kind=kind, name=name, status=st, summary=summary, detail=detail or {})
            if persist and st in PERSISTABLE_STATUSES:
                steps.append(as_trace_step(event) | {"detail": event["detail"]})
            return event

        yield emit(KIND_TOOL_START, "lookup_gateway", STATUS_PENDING, "Checking payment gateway...", persist=False)
        yield emit(KIND_TOOL_RESULT, "lookup_gateway", STATUS_OK, "Gateway checked")
        yield emit(KIND_TOOL_START, "lookup_bank", STATUS_PENDING, "Checking bank settlement...", persist=False)
        yield emit(KIND_TOOL_RESULT, "lookup_bank", STATUS_OK, "Bank checked")
        yield emit(KIND_TOOL_START, "lookup_ledger", STATUS_PENDING, "Checking internal ledger...", persist=False)
        yield emit(KIND_TOOL_RESULT, "lookup_ledger", STATUS_OK, "Ledger checked")
        yield emit(KIND_DECISION, "compare_records", STATUS_OK, f"Diagnosis: {status}", {"match_status": status})
        yield emit(KIND_ACTION, "recommended", STATUS_OK, f"Recommended action: {action}", {"action": action, "executed": False})

        created_at = datetime.now(timezone.utc)
        self._traces[txn_id] = {"request_id": request_id, "run_id": run_id, "created_at": created_at, "steps": steps}
        yield emit(KIND_COMPLETION, "resolve", STATUS_OK, f"Resolution complete: {status}", {"status": status, "explanation": explanation, "action": action, "run_id": run_id, "created_at": created_at.isoformat(), "steps": steps}, persist=False)

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
            # in-memory test client, never against Postgres.
            response = self.client.table(query_table).select("*").eq("transaction_id", txn_id).limit(1).execute()
            row = (response.data or [None])[0]
        return row

    def resolve(self, txn_id: str, request_id: str = "local-request") -> dict[str, Any]:
        gateway = self._one("gateway_transactions", txn_id)
        if gateway is None:
            raise TransactionNotFound(txn_id)
        bank = self._one("bank_settlements", txn_id)
        ledger = self._one("ledger_entries", txn_id)
        g_record, b_record, l_record = self._records(gateway, bank, ledger)
        verdict = compare_records(g_record, b_record, l_record, self.reference_time, txn_id=txn_id)
        status = verdict.match_status.value
        action, explanation = self._outcome(status)
        diagnosis = {
            "match_status": status,
            "confidence": verdict.confidence.value,
            "reason_code": verdict.reason_code,
            "action": action,
            "detail": verdict.detail,
        }
        run_id = str(uuid4())
        steps = [
            {"run_id": run_id, "txn_id": txn_id, "step_number": 1, "step_name": "gateway_lookup", "step_status": "ok", "step_result": "found", "detail": {"request_id": request_id}},
            {"run_id": run_id, "txn_id": txn_id, "step_number": 2, "step_name": "bank_lookup", "step_status": "ok", "step_result": "found" if bank else "not_found", "detail": {"request_id": request_id}},
            {"run_id": run_id, "txn_id": txn_id, "step_number": 3, "step_name": "ledger_lookup", "step_status": "ok", "step_result": "found" if ledger else "not_found", "detail": {"request_id": request_id}},
            {"run_id": run_id, "txn_id": txn_id, "step_number": 4, "step_name": "diagnosis", "step_status": "ok", "step_result": status, "detail": {"request_id": request_id, "confidence": diagnosis["confidence"]}},
        ]
        for step in steps:
            self.client.table("agent_trace_logs").insert(step).execute()
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
                run = self.orchestrator.run(txn_id, diagnosis)
                explanation = run.response.explanation
                for offset, event in enumerate(run.trace, start=len(steps) + 1):
                    self.client.table("agent_trace_logs").insert({
                        "run_id": run_id,
                        "txn_id": txn_id,
                        "step_number": offset,
                        "step_name": f"{event.kind}:{event.name}",
                        "step_status": "ok",
                        "step_result": json_safe(event.payload),
                        "detail": {"request_id": request_id, "model": run.model, "fallback_used": run.fallback_used},
                    }).execute()
            except GroqOrchestrationError:
                # Deterministic diagnosis remains usable when Groq is unavailable.
                pass
        return {"txn_id": txn_id, "status": status, "action": action, "explanation": explanation, "request_id": request_id, "run_id": run_id, "created_at": datetime.now(timezone.utc), "steps": steps}

    def iter_resolve(self, txn_id: str, request_id: str = "local-request") -> Iterator[dict[str, Any]]:
        """Yield trace events as each step completes, then a terminal completion.

        Reuses the same deterministic pieces as resolve() (_one, _records,
        compare_records, _outcome) so the streamed verdict can never diverge
        from the non-streaming one. Each outcome event is persisted to
        agent_trace_logs as it is emitted, so a run that dies mid-way still
        leaves a partial trace for GET /trace/{txn_id}.
        """
        run_id = str(uuid4())
        counter = 0
        steps: list[dict[str, Any]] = []

        def emit(kind: str, name: str, status: str, summary: str, detail: dict[str, Any] | None = None, persist: bool = True) -> dict[str, Any]:
            nonlocal counter
            counter += 1
            event = make_event(run_id=run_id, txn_id=txn_id, step_number=counter, kind=kind, name=name, status=status, summary=summary, detail=detail or {})
            if persist and status in PERSISTABLE_STATUSES:
                step = as_trace_step(event)
                try:
                    self.client.table("agent_trace_logs").insert(step).execute()
                except Exception:
                    pass  # Persistence is best-effort; a DB hiccup must not kill the stream.
                steps.append(step)
            return event

        gateway = self._one("gateway_transactions", txn_id)
        if gateway is None:
            # No events emitted yet, so the endpoint can still return a clean 404.
            raise TransactionNotFound(txn_id)

        yield emit(KIND_TOOL_START, "lookup_gateway", STATUS_PENDING, "Checking payment gateway...", persist=False)
        yield emit(KIND_TOOL_RESULT, "lookup_gateway", STATUS_OK, f"Gateway found: {gateway.get('status', '?')}, amount {gateway.get('amount', '?')}", {"present": True})

        yield emit(KIND_TOOL_START, "lookup_bank", STATUS_PENDING, "Checking bank settlement...", persist=False)
        bank = self._one("bank_settlements", txn_id)
        if bank is None:
            yield emit(KIND_TOOL_RESULT, "lookup_bank", STATUS_NOT_FOUND, "Bank: no settlement record found", {"present": False})
        else:
            yield emit(KIND_TOOL_RESULT, "lookup_bank", STATUS_OK, f"Bank: {bank.get('status', '?')}", {"present": True})

        yield emit(KIND_TOOL_START, "lookup_ledger", STATUS_PENDING, "Checking internal ledger...", persist=False)
        ledger = self._one("ledger_entries", txn_id)
        if ledger is None:
            yield emit(KIND_TOOL_RESULT, "lookup_ledger", STATUS_NOT_FOUND, "Ledger: no entry found", {"present": False})
        else:
            yield emit(KIND_TOOL_RESULT, "lookup_ledger", STATUS_OK, f"Ledger: {ledger.get('status', 'recorded')}", {"present": True})

        g, b, l = self._records(gateway, bank, ledger)
        verdict = compare_records(g, b, l, self.reference_time, txn_id=txn_id)
        status = verdict.match_status.value
        action, explanation = self._outcome(status)
        diagnosis = {"match_status": status, "confidence": verdict.confidence.value, "reason_code": verdict.reason_code, "action": action, "detail": verdict.detail}
        yield emit(KIND_DECISION, "compare_records", STATUS_OK, f"Diagnosis: {status} ({verdict.reason_code})", {"match_status": status, "confidence": verdict.confidence.value, "reason_code": verdict.reason_code})

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
                run = self.orchestrator.run(txn_id, diagnosis)
                explanation = run.response.explanation
                if run.fallback_used:
                    yield emit(KIND_RETRY, "groq", STATUS_WARNING, "Primary model failed; used fallback model.", {"model": run.model, "next_state": "continue_on_fallback"})
                for trace_event in run.trace:
                    if trace_event.kind == "tool_result":
                        yield emit(KIND_ACTION, trace_event.name, STATUS_OK, f"Action executed: {trace_event.name}", {"result": trace_event.payload, "executed": True})
            except GroqOrchestrationError:
                # The deterministic verdict stands; only the wording falls back.
                yield emit(KIND_RETRY, "groq", STATUS_WARNING, "LLM explanation unavailable; using deterministic summary.", {"next_state": "fallback_to_template"})
        else:
            # No LLM configured: surface the deterministic recommended action.
            yield emit(KIND_ACTION, "recommended", STATUS_OK, f"Recommended action: {action}", {"action": action, "executed": False})

        created_at = datetime.now(timezone.utc)
        yield emit(KIND_COMPLETION, "resolve", STATUS_OK, f"Resolution complete: {status}", {"status": status, "explanation": explanation, "action": action, "run_id": run_id, "created_at": created_at.isoformat(), "steps": steps}, persist=False)

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

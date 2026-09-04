from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Settings, get_settings
from .repository import SupabaseRepository, TransactionNotFound, UnavailableRepository
from backend.agent import GroqOrchestrator
from backend.embeddings import EmbeddingService
from .schemas import AnalyticsResponse, ErrorResponse, ReconcileRequest, ReconcileResponse, ResolveRequest, ResolveResponse, TicketResponse, TraceMetadata
from .trace_events import KIND_COMPLETION, SSE_DONE, SSE_TRACE, aiter_sync, sse_json


def create_app(settings: Settings | None = None, repository: Any | None = None) -> FastAPI:
    runtime = settings or get_settings()
    runtime.validate_for_runtime()
    repo = repository or _repository(runtime)
    app = FastAPI(title=runtime.app_name, version="0.2.0", description="PayPilot transaction reconciliation API")
    app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in runtime.allowed_origins.split(",") if x.strip()], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(TransactionNotFound)
    async def not_found(request: Request, exc: TransactionNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": {"code": "TXN_NOT_FOUND", "message": "Transaction was not found.", "request_id": request.headers.get("x-request-id", str(uuid4()))}})

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": {"code": "INVALID_REQUEST", "message": "Request validation failed.", "request_id": request.headers.get("x-request-id", str(uuid4()))}})

    @app.exception_handler(ValueError)
    async def invalid_value(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": {"code": "INVALID_REQUEST", "message": str(exc), "request_id": request.headers.get("x-request-id", str(uuid4()))}})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": runtime.app_name, "environment": runtime.app_env}

    @app.post("/resolve", response_model=ResolveResponse, responses={404: {"model": ErrorResponse}})
    async def resolve(payload: ResolveRequest, request: Request) -> ResolveResponse:
        request_id = request.headers.get("x-request-id", str(uuid4()))
        row = await asyncio.to_thread(repo.resolve, payload.txn_id, request_id)
        trace = TraceMetadata.model_validate({"request_id": request_id, "run_id": row["run_id"], "created_at": row["created_at"], "steps": row["steps"]})
        return ResolveResponse(txn_id=payload.txn_id, transaction_id=payload.txn_id, status=row["status"], explanation=row["explanation"], action=row["action"], trace=trace)

    @app.get("/resolve/stream")
    async def resolve_stream(txn_id: str, request: Request) -> StreamingResponse:
        """Server-Sent Events stream of the resolution trace (issue #25).

        Emits one `trace` event per step as it is computed, then a terminal
        `done` event carrying the full ResolveResponse. The blocking generator
        runs in a worker thread (via aiter_sync) so events flush incrementally
        instead of all at once. Reconnecting clients may send Last-Event-ID to
        skip already-seen steps; deterministic actions are idempotent, so
        re-running on reconnect is safe.
        """
        request_id = request.headers.get("x-request-id", str(uuid4()))
        last_event_id = _parse_last_event_id(request.headers.get("last-event-id"))
        stream = aiter_sync(lambda: repo.iter_resolve(txn_id, request_id))

        # Peek the first item so an unknown txn_id is a real 404 rather than a
        # 200 event-stream carrying an error.
        try:
            first = await stream.__anext__()
        except StopAsyncIteration:
            first = None
        if first is not None and first[0] == "error":
            raise first[1]

        async def body() -> Any:
            pending = first
            while pending is not None:
                kind, item = pending
                if kind == "error":
                    yield sse_json({"error": {"code": "STREAM_ERROR", "message": str(item), "request_id": request_id}}, event=SSE_DONE)
                    break
                for frame in _stream_frames(item, last_event_id, request_id, txn_id):
                    yield frame
                try:
                    pending = await stream.__anext__()
                except StopAsyncIteration:
                    pending = None

        headers = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
        return StreamingResponse(body(), media_type="text/event-stream", headers=headers)

    @app.get("/trace/{txn_id}", response_model=TraceMetadata, include_in_schema=False, responses={404: {"model": ErrorResponse}})
    async def trace_alias(txn_id: str) -> TraceMetadata:
        return TraceMetadata.model_validate(await asyncio.to_thread(repo.trace, txn_id))

    @app.get("/trace/{transaction_id}", response_model=TraceMetadata, include_in_schema=True, responses={404: {"model": ErrorResponse}})
    async def trace(transaction_id: str) -> TraceMetadata:
        return TraceMetadata.model_validate(await asyncio.to_thread(repo.trace, transaction_id))

    @app.get("/tickets", response_model=list[TicketResponse])
    async def tickets(action_taken: str | None = None, confidence: str | None = None) -> list[TicketResponse]:
        return [TicketResponse.model_validate(row) for row in await asyncio.to_thread(repo.tickets, action_taken, confidence)]

    @app.get("/exceptions", response_model=list[TicketResponse])
    async def exceptions() -> list[TicketResponse]:
        return [TicketResponse.model_validate(row) for row in await asyncio.to_thread(repo.exceptions)]

    @app.get("/analytics", response_model=AnalyticsResponse)
    async def analytics() -> AnalyticsResponse:
        return AnalyticsResponse.model_validate(await asyncio.to_thread(repo.analytics))

    @app.post("/reconcile", response_model=ReconcileResponse)
    async def reconcile(payload: ReconcileRequest, request: Request) -> ReconcileResponse:
        if payload.date_from > payload.date_to:
            return JSONResponse(status_code=422, content={"error": {"code": "INVALID_REQUEST", "message": "date_from must be before date_to", "request_id": request.headers.get("x-request-id", str(uuid4()))}})  # type: ignore[return-value]
        request_id = request.headers.get("x-request-id", str(uuid4()))
        rows = await asyncio.to_thread(repo.reconcile, payload.date_from, payload.date_to, request_id)
        return ReconcileResponse(date_from=payload.date_from, date_to=payload.date_to, results=[ResolveResponse(txn_id=row.get("txn_id", row.get("transaction_id")), transaction_id=row.get("txn_id", row.get("transaction_id")), status=row["status"], explanation=row["explanation"], action=row["action"], trace=TraceMetadata(request_id=request_id, run_id=row["run_id"], created_at=row["created_at"], steps=row["steps"])) for row in rows])

    return app


def _parse_last_event_id(raw: str | None) -> int:
    """Highest step_number a reconnecting client already has, or 0."""
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _stream_frames(event: dict[str, Any], last_event_id: int, request_id: str, txn_id: str) -> list[str]:
    """Turn one trace event into the SSE frames to send.

    Skips events at or below last_event_id so a reconnect does not replay.
    The terminal completion event is re-shaped into the ResolveResponse the
    non-streaming endpoint returns, sent as a `done` frame.
    """
    if event["step_number"] <= last_event_id:
        return []
    frames: list[str] = []
    if event["kind"] == KIND_COMPLETION:
        detail = event["detail"]
        payload = {
            "txn_id": txn_id,
            "transaction_id": txn_id,
            "status": detail["status"],
            "explanation": detail["explanation"],
            "action": detail["action"],
            "trace": {
                "request_id": request_id,
                "run_id": detail["run_id"],
                "created_at": detail["created_at"],
                "steps": detail["steps"],
            },
        }
        frames.append(sse_json(event, event=SSE_TRACE, event_id=event["step_number"]))
        frames.append(sse_json(payload, event=SSE_DONE))
    else:
        frames.append(sse_json(event, event=SSE_TRACE, event_id=event["step_number"]))
    return frames


def _repository(settings: Settings) -> Any:
    if not settings.supabase_url or not settings.supabase_service_role_key:
        return UnavailableRepository()
    from supabase import create_client
    client = create_client(settings.supabase_url, settings.supabase_service_role_key.get_secret_value())
    orchestrator = None
    if settings.groq_api_key:
        from groq import Groq
        orchestrator = GroqOrchestrator(
            Groq(api_key=settings.groq_api_key.get_secret_value()),
            {},
            model=settings.groq_model,
            fallback_model=settings.groq_fallback_model,
            max_steps=settings.agent_max_steps,
            timeout_seconds=settings.groq_timeout_seconds,
        )
    return SupabaseRepository(
        client,
        orchestrator=orchestrator,
        embedding_service=EmbeddingService(settings.embedding_model),
        similarity_threshold=settings.similarity_threshold,
        similarity_match_count=settings.similarity_match_count,
    )


app = create_app()

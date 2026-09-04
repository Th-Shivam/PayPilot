from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import Settings, get_settings
from .repository import SupabaseRepository, TransactionNotFound, UnavailableRepository
from backend.agent import GroqOrchestrator
from backend.embeddings import EmbeddingService
from .schemas import AnalyticsResponse, ErrorResponse, ReconcileRequest, ReconcileResponse, ResolveRequest, ResolveResponse, TicketResponse, TraceMetadata


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

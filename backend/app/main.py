from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Settings, get_settings
from .repository import SupabaseRepository, TransactionNotFound, UnavailableRepository
from backend.agent import GroqOrchestrator
from backend.domain.trace import TraceEvent
from backend.embeddings import EmbeddingService
from backend.observability import (
    ERROR_CLASS_BUG,
    ERROR_CLASS_DEPENDENCY,
    ERROR_CLASS_USER,
    REQUEST_ID_HEADER,
    REQUEST_ID_KEY,
    RequestCorrelationMiddleware,
    annotate_current_span,
    configure_observability,
    instrument_fastapi,
    log_error,
    log_exception,
    log_warn,
    request_id_for,
)
from .schemas import AnalyticsResponse, ErrorResponse, ReconcileRequest, ReconcileResponse, ResolveRequest, ResolveResponse, TicketResponse, TraceMetadata


class DependencyUnavailable(RuntimeError):
    """A repository/dependency failure safe for the public API boundary."""


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    error_class: str,
    exc: BaseException | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build one error response and one structured log line from one id.

    The `request_id` in the body, the `X-Request-Id` header, and the trace this
    request produced all carry the same value, so a support agent can read the
    id off a failed response and land on the Logfire trace for it. Every handler
    goes through here; deriving the id per handler — which is what this API used
    to do — hands out a different one at each site for a single failed request.

    The header is set explicitly rather than left to the correlation middleware
    because the catch-all 500 handler runs inside Starlette's
    `ServerErrorMiddleware`, which sits outside every user middleware.

    `error_class` is the part a reader filters on: whose fault the failure was.
    """
    request_id = request_id_for(request)
    detail: dict[str, Any] = {
        REQUEST_ID_KEY: request_id,
        "paypilot.error_code": code,
        "paypilot.error_class": error_class,
        "http.status_code": status_code,
        "http.method": request.method,
        "http.path": request.url.path,
    }
    if exc is not None:
        detail["paypilot.failure_reason"] = type(exc).__name__
    if error_class == ERROR_CLASS_BUG:
        log_exception("request.failed", **detail)
    elif error_class == ERROR_CLASS_DEPENDENCY:
        log_error("request.failed", **detail)
    else:
        log_warn("request.failed", **detail)
    annotate_current_span(**detail)
    return JSONResponse(
        status_code=status_code,
        headers={**(headers or {}), REQUEST_ID_HEADER: request_id},
        content={"error": {"code": code, "message": message, REQUEST_ID_KEY: request_id}},
    )


def create_app(settings: Settings | None = None, repository: Any | None = None) -> FastAPI:
    runtime = settings or get_settings()
    runtime.validate_for_runtime()
    configure_observability(runtime)
    repo = repository or _repository(runtime)
    app = FastAPI(title=runtime.app_name, version="0.2.0", description="PayPilot transaction reconciliation API")
    app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in runtime.allowed_origins.split(",") if x.strip()], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(RequestCorrelationMiddleware)

    @app.exception_handler(TransactionNotFound)
    async def not_found(request: Request, exc: TransactionNotFound) -> JSONResponse:
        return _error_response(request, status_code=404, code="TXN_NOT_FOUND", message="Transaction was not found.", error_class=ERROR_CLASS_USER, exc=exc)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(request, status_code=422, code="INVALID_REQUEST", message="Request validation failed.", error_class=ERROR_CLASS_USER, exc=exc)

    @app.exception_handler(ValueError)
    async def invalid_value(request: Request, exc: ValueError) -> JSONResponse:
        return _error_response(request, status_code=422, code="INVALID_REQUEST", message=str(exc), error_class=ERROR_CLASS_USER, exc=exc)

    @app.exception_handler(RuntimeError)
    async def dependency_error(request: Request, exc: RuntimeError) -> JSONResponse:
        return _error_response(
            request,
            status_code=503,
            code="DEPENDENCY_UNAVAILABLE",
            message="A required backend dependency is temporarily unavailable.",
            error_class=ERROR_CLASS_DEPENDENCY,
            exc=exc,
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Anything that reached here is a bug, not a caller or dependency fault.

        Without this the caller gets Starlette's bare `Internal Server Error`
        with no id to quote, which is the one failure mode nobody can follow up
        on. The message stays generic; the traceback goes to the log.
        """
        return _error_response(request, status_code=500, code="INTERNAL_ERROR", message="An unexpected error occurred.", error_class=ERROR_CLASS_BUG, exc=exc)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": runtime.app_name, "environment": runtime.app_env}

    async def stream_resolution(payload: ResolveRequest, request: Request, request_id: str) -> StreamingResponse:
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def publish(event: dict[str, Any]) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, ("event", event))
            except RuntimeError:
                pass

        def run_resolution() -> None:
            try:
                repo.resolve(payload.txn_id, request_id, publish)
            except BaseException as exc:
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
                except RuntimeError:
                    pass
            finally:
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, ("done", None))
                except RuntimeError:
                    pass

        async def body() -> Any:
            worker = asyncio.create_task(asyncio.to_thread(run_resolution))
            try:
                while True:
                    kind, value = await queue.get()
                    if kind == "event":
                        event = TraceEvent.model_validate(value).model_dump(mode="json")
                        if await request.is_disconnected():
                            break
                        yield f"event: trace\nid: {event['event_id']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
                    elif kind == "error":
                        if not isinstance(value, TransactionNotFound):
                            # The repository already logged why it failed; this
                            # records that the caller was mid-stream when it did,
                            # and hands them the id to quote.
                            log_warn(
                                "resolve.stream_failed",
                                **{
                                    REQUEST_ID_KEY: request_id,
                                    "paypilot.txn_id": payload.txn_id,
                                    "paypilot.error_code": "RESOLUTION_FAILED",
                                    "paypilot.error_class": ERROR_CLASS_DEPENDENCY,
                                    "paypilot.failure_reason": type(value).__name__,
                                },
                            )
                            yield f"event: error\ndata: {json.dumps({'code': 'RESOLUTION_FAILED', 'message': 'Resolution could not be completed.', 'request_id': request_id}, separators=(',', ':'))}\n\n"
                    elif kind == "done":
                        break
            except asyncio.CancelledError:
                worker.cancel()
                raise
            finally:
                if not worker.done():
                    worker.cancel()

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no", REQUEST_ID_HEADER: request_id},
        )

    def repo_call(method: Any, *args: Any) -> Any:
        try:
            return method(*args)
        except (TransactionNotFound, ValueError):
            raise
        except Exception as exc:
            raise DependencyUnavailable from exc

    @app.post(
        "/resolve",
        response_model=ResolveResponse,
        responses={
            200: {
                "description": "JSON resolution by default; request with Accept: text/event-stream for progressive trace events.",
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
            },
            404: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def resolve(payload: ResolveRequest, request: Request) -> Any:
        request_id = request_id_for(request)
        if "text/event-stream" in request.headers.get("accept", "").lower():
            return await stream_resolution(payload, request, request_id)
        row = await asyncio.to_thread(repo_call, repo.resolve, payload.txn_id, request_id)
        trace = TraceMetadata.model_validate({"request_id": request_id, "run_id": row["run_id"], "created_at": row["created_at"], "steps": row["steps"]})
        return ResolveResponse(txn_id=payload.txn_id, transaction_id=payload.txn_id, status=row["status"], explanation=row["explanation"], action=row["action"], trace=trace)

    @app.get("/trace/{txn_id}", response_model=TraceMetadata, include_in_schema=False, responses={404: {"model": ErrorResponse}})
    async def trace_alias(txn_id: str) -> TraceMetadata:
        return TraceMetadata.model_validate(await asyncio.to_thread(repo_call, repo.trace, txn_id))

    @app.get("/trace/{transaction_id}", response_model=TraceMetadata, include_in_schema=True, responses={404: {"model": ErrorResponse}})
    async def trace(transaction_id: str) -> TraceMetadata:
        return TraceMetadata.model_validate(await asyncio.to_thread(repo_call, repo.trace, transaction_id))

    @app.get("/tickets", response_model=list[TicketResponse])
    async def tickets(action_taken: str | None = None, confidence: str | None = None) -> list[TicketResponse]:
        rows = await asyncio.to_thread(repo_call, repo.tickets, action_taken, confidence)
        return [TicketResponse.model_validate(row) for row in rows]

    @app.get("/exceptions", response_model=list[TicketResponse])
    async def exceptions() -> list[TicketResponse]:
        rows = await asyncio.to_thread(repo_call, repo.exceptions)
        return [TicketResponse.model_validate(row) for row in rows]

    @app.get("/analytics", response_model=AnalyticsResponse)
    async def analytics() -> AnalyticsResponse:
        return AnalyticsResponse.model_validate(await asyncio.to_thread(repo_call, repo.analytics))

    @app.post("/reconcile", response_model=ReconcileResponse)
    async def reconcile(payload: ReconcileRequest, request: Request) -> ReconcileResponse:
        request_id = request_id_for(request)
        if payload.date_from > payload.date_to:
            return _error_response(request, status_code=422, code="INVALID_REQUEST", message="date_from must be before date_to", error_class=ERROR_CLASS_USER)  # type: ignore[return-value]
        rows = await asyncio.to_thread(repo_call, repo.reconcile, payload.date_from, payload.date_to, request_id)
        return ReconcileResponse(date_from=payload.date_from, date_to=payload.date_to, results=[ResolveResponse(txn_id=row.get("txn_id", row.get("transaction_id")), transaction_id=row.get("txn_id", row.get("transaction_id")), status=row["status"], explanation=row["explanation"], action=row["action"], trace=TraceMetadata(request_id=request_id, run_id=row["run_id"], created_at=row["created_at"], steps=row["steps"])) for row in rows])

    # Last, deliberately: `add_middleware` inserts at the front of the stack, so
    # whatever registers last ends up outermost. Instrumenting here puts the
    # per-request span outside the correlation middleware, which is what lets
    # that middleware stamp `request_id` onto a span that is already open.
    instrument_fastapi(app)
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
    reference_time = None
    if settings.reconciliation_reference_date:
        # Pin the settlement-window reference (e.g. to the seeded fixture date)
        # so diagnoses stay consistent instead of drifting as real time passes.
        reference_time = datetime.fromisoformat(settings.reconciliation_reference_date)
        if reference_time.tzinfo is None:
            reference_time = reference_time.replace(tzinfo=timezone.utc)
    return SupabaseRepository(
        client,
        reference_time=reference_time,
        orchestrator=orchestrator,
        embedding_service=EmbeddingService(settings.embedding_model),
        similarity_threshold=settings.similarity_threshold,
        similarity_match_count=settings.similarity_match_count,
    )


app = create_app()

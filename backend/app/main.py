from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Settings, get_settings
from .auth import AuthError, AuthenticatedUser, SupabaseAuthenticator, build_auth_dependencies
from .repository import SupabaseRepository, TransactionNotFound, UnavailableRepository
from backend.agent import GroqOrchestrator
from backend.domain.trace import TraceEvent
from backend.embeddings import EmbeddingService
from .schemas import AnalyticsResponse, ErrorResponse, ReconcileRequest, ReconcileResponse, ResolveRequest, ResolveResponse, TicketResponse, TraceMetadata


class DependencyUnavailable(RuntimeError):
    """A repository/dependency failure safe for the public API boundary."""


def create_app(settings: Settings | None = None, repository: Any | None = None, authenticator: Any | None = None) -> FastAPI:
    runtime = settings or get_settings()
    runtime.validate_for_runtime()
    repo = repository or _repository(runtime)
    token_authenticator = authenticator or _authenticator(repo)
    current_user, support_agent = build_auth_dependencies(runtime, token_authenticator)
    app = FastAPI(title=runtime.app_name, version="0.2.0", description="PayPilot transaction reconciliation API")
    app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in runtime.allowed_origins.split(",") if x.strip()], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(TransactionNotFound)
    async def not_found(request: Request, exc: TransactionNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": {"code": "TXN_NOT_FOUND", "message": "Transaction was not found.", "request_id": request.headers.get("x-request-id", str(uuid4()))}})

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            headers={"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "request_id": request.headers.get("x-request-id", str(uuid4())),
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": {"code": "INVALID_REQUEST", "message": "Request validation failed.", "request_id": request.headers.get("x-request-id", str(uuid4()))}})

    @app.exception_handler(ValueError)
    async def invalid_value(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": {"code": "INVALID_REQUEST", "message": str(exc), "request_id": request.headers.get("x-request-id", str(uuid4()))}})

    @app.exception_handler(RuntimeError)
    async def dependency_error(request: Request, exc: RuntimeError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "DEPENDENCY_UNAVAILABLE",
                    "message": "A required backend dependency is temporarily unavailable.",
                    "request_id": request.headers.get("x-request-id", str(uuid4())),
                }
            },
        )

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
                            yield f"event: error\ndata: {json.dumps({'code': 'RESOLUTION_FAILED', 'message': 'Resolution could not be completed.'}, separators=(',', ':'))}\n\n"
                    elif kind == "done":
                        break
            except asyncio.CancelledError:
                worker.cancel()
                raise
            finally:
                if not worker.done():
                    worker.cancel()

        return StreamingResponse(body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

    async def ensure_transaction_access(txn_id: str, user: AuthenticatedUser) -> None:
        if user.is_support_agent:
            return
        checker = getattr(repo, "can_access_transaction", None)
        try:
            allowed = bool(checker) and await asyncio.to_thread(checker, txn_id, user.user_id)
        except Exception as exc:
            raise DependencyUnavailable from exc
        if not allowed:
            raise AuthError(403, "FORBIDDEN", "You do not have access to this transaction.")

    def scoped_repository_call(method: Any, *args: Any, owner_id: str | None = None) -> Any:
        try:
            if owner_id is None:
                return method(*args)
            return method(*args, owner_id=owner_id)
        except (TransactionNotFound, ValueError):
            raise
        except TypeError as exc:
            if owner_id is not None:
                raise AuthError(503, "OWNERSHIP_UNAVAILABLE", "Ownership checks are temporarily unavailable.") from exc
            raise DependencyUnavailable from exc
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
            401: {"model": ErrorResponse},
            403: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def resolve(payload: ResolveRequest, request: Request, _user: AuthenticatedUser = Depends(support_agent)) -> Any:
        request_id = request.headers.get("x-request-id", str(uuid4()))
        if "text/event-stream" in request.headers.get("accept", "").lower():
            return await stream_resolution(payload, request, request_id)
        row = await asyncio.to_thread(scoped_repository_call, repo.resolve, payload.txn_id, request_id)
        trace = TraceMetadata.model_validate({"request_id": request_id, "run_id": row["run_id"], "created_at": row["created_at"], "steps": row["steps"]})
        return ResolveResponse(txn_id=payload.txn_id, transaction_id=payload.txn_id, status=row["status"], explanation=row["explanation"], action=row["action"], trace=trace)

    @app.get("/trace/{txn_id}", response_model=TraceMetadata, include_in_schema=False, responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
    async def trace_alias(txn_id: str, user: AuthenticatedUser = Depends(current_user)) -> TraceMetadata:
        await ensure_transaction_access(txn_id, user)
        owner_id = None if user.is_support_agent else user.user_id
        return TraceMetadata.model_validate(await asyncio.to_thread(scoped_repository_call, repo.trace, txn_id, owner_id=owner_id))

    @app.get("/trace/{transaction_id}", response_model=TraceMetadata, include_in_schema=True, responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
    async def trace(transaction_id: str, user: AuthenticatedUser = Depends(current_user)) -> TraceMetadata:
        await ensure_transaction_access(transaction_id, user)
        owner_id = None if user.is_support_agent else user.user_id
        return TraceMetadata.model_validate(await asyncio.to_thread(scoped_repository_call, repo.trace, transaction_id, owner_id=owner_id))

    @app.get("/tickets", response_model=list[TicketResponse], responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}})
    async def tickets(action_taken: str | None = None, confidence: str | None = None, user: AuthenticatedUser = Depends(current_user)) -> list[TicketResponse]:
        owner_id = None if user.is_support_agent else user.user_id
        rows = await asyncio.to_thread(scoped_repository_call, repo.tickets, action_taken, confidence, owner_id=owner_id)
        return [TicketResponse.model_validate(row) for row in rows]

    @app.get("/exceptions", response_model=list[TicketResponse], responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}})
    async def exceptions(user: AuthenticatedUser = Depends(current_user)) -> list[TicketResponse]:
        owner_id = None if user.is_support_agent else user.user_id
        rows = await asyncio.to_thread(scoped_repository_call, repo.exceptions, owner_id=owner_id)
        return [TicketResponse.model_validate(row) for row in rows]

    @app.get("/analytics", response_model=AnalyticsResponse, responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}})
    async def analytics(user: AuthenticatedUser = Depends(current_user)) -> AnalyticsResponse:
        owner_id = None if user.is_support_agent else user.user_id
        return AnalyticsResponse.model_validate(await asyncio.to_thread(scoped_repository_call, repo.analytics, owner_id=owner_id))

    @app.post("/reconcile", response_model=ReconcileResponse, responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}})
    async def reconcile(payload: ReconcileRequest, request: Request, _user: AuthenticatedUser = Depends(support_agent)) -> ReconcileResponse:
        if payload.date_from > payload.date_to:
            return JSONResponse(status_code=422, content={"error": {"code": "INVALID_REQUEST", "message": "date_from must be before date_to", "request_id": request.headers.get("x-request-id", str(uuid4()))}})  # type: ignore[return-value]
        request_id = request.headers.get("x-request-id", str(uuid4()))
        rows = await asyncio.to_thread(scoped_repository_call, repo.reconcile, payload.date_from, payload.date_to, request_id)
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


def _authenticator(repository: Any) -> SupabaseAuthenticator | None:
    client = getattr(repository, "client", None)
    return SupabaseAuthenticator(client) if client is not None else None


app = create_app()

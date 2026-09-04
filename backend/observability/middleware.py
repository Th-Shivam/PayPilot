"""Per-request correlation: one id, visible in the trace and in the response.

The id has to be identical in three places or it is worthless: the span a
reviewer opens in Logfire, the `request_id` field of the error body the UI
receives, and the `X-Request-Id` response header. Deriving it independently at
each of those points — which is what the API used to do — produces three
different ids for a single failed request whenever the client does not supply
one, so it is derived once here and read back from request state everywhere
else.

Written as raw ASGI rather than a `BaseHTTPMiddleware` subclass because
`/resolve` streams Server-Sent Events and depends on
`request.is_disconnected()`. `BaseHTTPMiddleware` pumps the response through an
anyio stream and interferes with both.
"""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable, MutableMapping
from uuid import uuid4

from .tracing import REQUEST_ID_KEY, annotate_current_span, correlation

REQUEST_ID_HEADER = "x-request-id"

# A client-supplied id is echoed back in a response header, so it is accepted
# only in a shape that cannot carry a header injection or bloat the response.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class RequestCorrelationMiddleware:
    """Assign one request id per request and publish it everywhere it is needed."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or str(uuid4())
        # `Request.state` is a view over scope["state"], so writing here is what
        # makes `request_id_for(request)` work in routes and error handlers —
        # including the 500 handler, which runs in ServerErrorMiddleware outside
        # this middleware but against the same scope object.
        scope.setdefault("state", {})[REQUEST_ID_KEY] = request_id
        encoded = (REQUEST_ID_HEADER.encode("latin-1"), request_id.encode("latin-1"))

        async def send_with_request_id(message: Message) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                if not any(name.lower() == encoded[0] for name, _ in headers):
                    headers.append(encoded)
                message = {**message, "headers": headers}
            await send(message)

        with correlation(request_id):
            # Baggage covers every span opened downstream. This covers the one
            # opened upstream: FastAPI instrumentation installs its middleware
            # outside this one, so its request span is already active here and
            # would otherwise be the only span without the id.
            annotate_current_span(**{REQUEST_ID_KEY: request_id})
            await self.app(scope, receive, send_with_request_id)


def request_id_for(request: Any) -> str:
    """The id assigned to this request, or a fresh one if the middleware is absent."""
    return str(getattr(request.state, REQUEST_ID_KEY, None) or uuid4())


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers") or ():
        if name.lower() != REQUEST_ID_HEADER.encode("latin-1"):
            continue
        candidate = value.decode("latin-1", "ignore").strip()
        return candidate if _SAFE_REQUEST_ID.match(candidate) else None
    return None

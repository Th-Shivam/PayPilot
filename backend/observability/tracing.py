"""Logfire wiring, with tracing as a strictly optional dependency.

Two rules shape this module.

**A resolve must never fail because of observability.** `LOGFIRE_TOKEN` unset,
the logfire package missing, Logfire unreachable, an exporter raising in the
middle of a span — every one of those degrades to a no-op that still returns the
real answer to the caller. That is why each entry point here swallows its own
failures, and why `span()` re-implements `with` by hand: it guards entering and
exiting the span, but lets any exception from the *body* propagate untouched.
Instrumentation observes the request path; it never alters it.

**Nothing reaches a span unredacted.** Every attribute passes through
`redaction.redact` on the way in, including the endpoint arguments FastAPI
captures for us, because PayPilot's auth dependency resolves to a user object
carrying an email address.

Attribute naming follows a namespace per layer — `reconciliation.*`, `groq.*`,
`tool.*`, `supabase.*`, `paypilot.*` — with one deliberate exception:
`request_id` sits at the top level, unprefixed, because it is the field a
reviewer pastes into Logfire's search box after reading it off an error
response.
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Sequence

from .redaction import RESPONSE_TEXT_LIMIT, redact, redact_attributes, truncate_text

try:  # Tracing is optional; the resolve path must not require it.
    import logfire as _logfire
    from opentelemetry import trace as _otel_trace
except Exception:  # pragma: no cover - exercised only on installs without logfire
    _logfire = None
    _otel_trace = None

SERVICE_NAME = "paypilot-api"
SERVICE_VERSION = "0.2.0"

# The correlation key, used identically as OpenTelemetry baggage, as a span
# attribute, and as the `request_id` field of every error response.
REQUEST_ID_KEY = "request_id"

# How a failure is classified in the structured logs. The distinction is the
# whole point of the log line: a 422 is the caller's problem, a 503 is a
# dependency's, and a 500 is ours.
ERROR_CLASS_USER = "user_error"
ERROR_CLASS_DEPENDENCY = "dependency_failure"
ERROR_CLASS_BUG = "application_bug"

# Belt-and-braces on top of `redaction`: Logfire's own scrubber already covers
# password/token/secret-shaped paths, and these extend it to the names this
# schema uses.
EXTRA_SCRUB_PATTERNS = ("customer_name", "beneficiary_name", r"\butr\b", "service_role")

# Attribute namespaces only PayPilot writes, and only through `span()` or
# `log_*` — both of which run every value through `redaction.redact` first.
# Logfire's scrubber is left switched on for everything else, which is where it
# earns its keep: instrumentation-captured values PayPilot never sees.
SELF_REDACTED_NAMESPACES = (
    "agent.",
    "groq.",
    "paypilot.",
    "reconciliation.",
    "supabase.",
    "tool.",
    "trace.",
)

_logger = logging.getLogger("paypilot")

_enabled = False
_configured = False


def configure_observability(settings: Any, *, span_processors: Sequence[Any] | None = None) -> bool:
    """Configure Logfire once per process. Returns whether tracing is live.

    Called from `create_app`, before instrumentation, because
    `instrument_fastapi` installs middleware and so has to run before the app
    starts serving.

    Under pytest the exporter is switched off but instrumentation stays on. The
    test suite therefore still exercises every span and every redaction path —
    which is where a non-serialisable attribute would surface — without
    shipping fixture data to the production Logfire project. `span_processors`
    is the seam the tests use to read those spans back locally; production
    passes nothing.
    """
    global _enabled, _configured
    if _configured:
        return _enabled

    token = _token_from(settings)
    if _logfire is None:
        if token:
            _logger.warning("LOGFIRE_TOKEN is set but the logfire package is not installed; tracing is disabled.")
        _configured = True
        return False
    if not token and not span_processors:
        _logger.info("LOGFIRE_TOKEN is unset; PayPilot runs with local logging only.")
        _configured = True
        return False

    environment = str(getattr(settings, "app_env", "development"))
    try:
        _logfire.configure(
            token=token or None,
            service_name=SERVICE_NAME,
            service_version=SERVICE_VERSION,
            environment=environment,
            console=False,
            send_to_logfire="pytest" not in sys.modules,
            # Span names and attributes are always passed explicitly here, never
            # via f-string magic, so source introspection buys nothing and warns
            # loudly wherever source is unavailable.
            inspect_arguments=False,
            scrubbing=_logfire.ScrubbingOptions(
                extra_patterns=list(EXTRA_SCRUB_PATTERNS),
                callback=_keep_self_redacted,
            ),
            additional_span_processors=list(span_processors) if span_processors else None,
        )
    except Exception as exc:
        _logger.warning("Logfire configuration failed (%s); tracing is disabled.", exc)
        _configured = True
        return False

    _enabled = True
    _configured = True
    _logger.info("Logfire tracing enabled for environment %s.", environment)
    return True


def observability_enabled() -> bool:
    return _enabled


def reset_observability() -> None:
    """Forget the configured state. For tests only."""
    global _enabled, _configured
    _enabled = False
    _configured = False


def instrument_fastapi(app: Any) -> None:
    """Create one span per HTTP request, with captured arguments redacted."""
    if not _enabled or _logfire is None:
        return
    try:
        _logfire.instrument_fastapi(
            app,
            capture_headers=False,
            request_attributes_mapper=_request_attributes_mapper,
        )
    except Exception as exc:
        _logger.warning("FastAPI instrumentation unavailable (%s); request spans are disabled.", exc)


class SpanHandle:
    """The slice of the Logfire span API PayPilot uses, with redaction attached.

    A single class covers both states. When `_span` is None every method is a
    no-op, which lets the disabled path share one stateless instance rather than
    branch at each call site.
    """

    __slots__ = ("_span",)

    def __init__(self, span: Any | None = None) -> None:
        self._span = span

    @property
    def recording(self) -> bool:
        return self._span is not None

    def set(self, **attributes: Any) -> None:
        self.set_attributes(attributes)

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        if self._span is None:
            return
        try:
            self._span.set_attributes(redact_attributes(dict(attributes)))
        except Exception:
            pass

    def set_attribute(self, name: str, value: Any) -> None:
        self.set_attributes({name: value})


_NULL_SPAN = SpanHandle(None)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[SpanHandle]:
    """Open a span, or yield an inert handle when tracing is off or broken."""
    if not _enabled or _logfire is None:
        yield _NULL_SPAN
        return

    try:
        active = _logfire.span(name, **redact_attributes(attributes))
        active.__enter__()
    except Exception:
        yield _NULL_SPAN
        return

    # `with active:` would be shorter, but the exits have to be guarded
    # individually so a failing exporter cannot replace the caller's exception
    # with its own.
    try:
        yield SpanHandle(active)
    except BaseException as exc:
        _safe_exit(active, exc)
        raise
    _safe_exit(active, None)


@contextmanager
def correlation(request_id: str) -> Iterator[None]:
    """Attach `request_id` to the OpenTelemetry context for one request.

    Baggage rather than a span attribute because it has to reach the whole call
    tree, including the worker thread `asyncio.to_thread` spins up for the
    synchronous repository — `to_thread` copies the caller's contextvars, and
    the OpenTelemetry context lives in one. That is what makes a single resolve
    followable from the HTTP span down to the Supabase writes.
    """
    handle = None
    if _enabled and _logfire is not None:
        try:
            handle = _logfire.set_baggage(**{REQUEST_ID_KEY: request_id})
            handle.__enter__()
        except Exception:
            handle = None
    if handle is None:
        yield
        return
    try:
        yield
    finally:
        try:
            handle.__exit__(None, None, None)
        except Exception:
            pass


def annotate_current_span(**attributes: Any) -> None:
    """Hang attributes on whichever span is already active.

    Used to stamp `request_id` onto the request span FastAPI instrumentation
    created, rather than opening a second span for the same unit of work.
    OpenTelemetry hands back a non-recording span when nothing is active and
    setting attributes on it is a documented no-op, so "no active trace" needs
    no special case.
    """
    if not _enabled or _otel_trace is None:
        return
    try:
        current = _otel_trace.get_current_span()
        for key, value in redact_attributes(attributes).items():
            current.set_attribute(key, _otel_safe(value))
    except Exception:
        pass


def log_info(message: str, **attributes: Any) -> None:
    _emit("info", message, attributes)


def log_warn(message: str, **attributes: Any) -> None:
    _emit("warn", message, attributes)


def log_error(message: str, **attributes: Any) -> None:
    _emit("error", message, attributes)


def log_exception(message: str, **attributes: Any) -> None:
    """Record the exception currently being handled, with its traceback."""
    payload = redact_attributes(attributes)
    if _enabled and _logfire is not None:
        try:
            _logfire.exception(message, **payload)
            return
        except Exception:
            pass
    _logger.exception("%s %s", message, _flatten(payload))


def truncate_response(value: str) -> str:
    """Bound a captured model response before it becomes a span attribute."""
    return truncate_text(value, RESPONSE_TEXT_LIMIT)


def _emit(level: str, message: str, attributes: dict[str, Any]) -> None:
    payload = redact_attributes(attributes)
    if _enabled and _logfire is not None:
        try:
            getattr(_logfire, level)(message, **payload)
            return
        except Exception:
            pass
    getattr(_logger, {"info": "info", "warn": "warning", "error": "error"}[level])(
        "%s %s", message, _flatten(payload)
    )


def _flatten(payload: dict[str, Any]) -> str:
    return " ".join(f"{key}={_otel_safe(value)!r}" for key, value in payload.items())


def _safe_exit(active: Any, exc: BaseException | None) -> None:
    try:
        if exc is None:
            active.__exit__(None, None, None)
        else:
            active.__exit__(type(exc), exc, exc.__traceback__)
    except Exception:
        pass


def _otel_safe(value: Any) -> Any:
    """Coerce to something the OpenTelemetry attribute API accepts."""
    if isinstance(value, (str, bool, int, float)):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, default=str, sort_keys=True)
    except Exception:
        return str(value)


def _keep_self_redacted(match: Any) -> Any:
    """Stop Logfire's scrubber from erasing values PayPilot already redacted.

    Its default patterns include a bare `auth`, and they are matched against
    values as well as keys. Left alone, that replaces the whole captured Groq
    prompt with `[Scrubbed due to 'auth']` the moment the system prompt says
    "authorized", and turns a tool result of `not_authorized` — the interesting
    outcome — into the same. The prompt capture is the evidence that the model
    only ever worded a finished verdict, so losing it defeats the purpose of
    capturing it.

    Returning the value keeps it; returning None accepts the default scrubbing.
    Only PayPilot's own namespaces are exempted, and everything written under
    them has already been through `redaction.redact`, which removes credentials
    by key and by value pattern. Attributes from anywhere else — including any
    header or dependency value FastAPI instrumentation captures — stay scrubbed.
    """
    try:
        path = getattr(match, "path", ())
        if len(path) >= 2 and str(path[0]) == "attributes":
            if str(path[1]).startswith(SELF_REDACTED_NAMESPACES):
                return match.value
    except Exception:
        pass
    return None


def _request_attributes_mapper(_request: Any, attributes: dict[str, Any]) -> dict[str, Any]:
    """Redact the endpoint arguments FastAPI instrumentation captures.

    Logfire records solved dependency values under `values`, and PayPilot's auth
    dependency resolves to an `AuthenticatedUser` carrying an email address. The
    route arguments themselves are worth keeping — `txn_id` is the reason to
    look at the span at all — so this redacts rather than drops.
    """
    return redact_attributes(attributes)


def _token_from(settings: Any) -> str:
    raw = getattr(settings, "logfire_token", None)
    if raw is None:
        return ""
    getter = getattr(raw, "get_secret_value", None)
    value = getter() if callable(getter) else raw
    return str(value or "").strip()

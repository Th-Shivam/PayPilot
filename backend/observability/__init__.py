"""Tracing, redaction, and request correlation for PayPilot.

`backend.observability` is the only place that knows Logfire exists. Everything
else imports `span`, `log_*`, or `redact` from here and works identically
whether or not `LOGFIRE_TOKEN` is set.
"""

from .middleware import REQUEST_ID_HEADER, RequestCorrelationMiddleware, request_id_for
from .redaction import (
    PROMPT_TOTAL_LIMIT,
    REDACTED,
    RESPONSE_TEXT_LIMIT,
    is_sensitive_key,
    redact,
    redact_attributes,
    redact_prompt,
    scrub_text,
    truncate_text,
)
from .tracing import (
    ERROR_CLASS_BUG,
    ERROR_CLASS_DEPENDENCY,
    ERROR_CLASS_USER,
    REQUEST_ID_KEY,
    SpanHandle,
    annotate_current_span,
    configure_observability,
    correlation,
    instrument_fastapi,
    log_error,
    log_exception,
    log_info,
    log_warn,
    observability_enabled,
    reset_observability,
    span,
    truncate_response,
)

__all__ = [
    "ERROR_CLASS_BUG",
    "ERROR_CLASS_DEPENDENCY",
    "ERROR_CLASS_USER",
    "PROMPT_TOTAL_LIMIT",
    "REDACTED",
    "REQUEST_ID_HEADER",
    "REQUEST_ID_KEY",
    "RESPONSE_TEXT_LIMIT",
    "RequestCorrelationMiddleware",
    "SpanHandle",
    "annotate_current_span",
    "configure_observability",
    "correlation",
    "instrument_fastapi",
    "is_sensitive_key",
    "log_error",
    "log_exception",
    "log_info",
    "log_warn",
    "observability_enabled",
    "redact",
    "redact_attributes",
    "redact_prompt",
    "request_id_for",
    "reset_observability",
    "scrub_text",
    "span",
    "truncate_response",
    "truncate_text",
]

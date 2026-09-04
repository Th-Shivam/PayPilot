"""Redaction and truncation for everything crossing the tracing boundary.

A PayPilot trace is meant to be read. Capturing the Groq prompt verbatim is the
whole point: it is the evidence that the model was handed a finished
deterministic diagnosis and asked only to word it, never to decide the verdict.
That evidence is only safe to keep if the records travelling alongside it are
stripped first, and the records are exactly where the problem is:
`Diagnosis.detail` embeds the full gateway, bank, and ledger rows, so a span
would otherwise carry `customer_name` and the bank `utr` — neither of which any
reconciliation rule reads.

What is deliberately *not* redacted: amounts, currencies, statuses, timestamps,
and `txn_id`. Those are the reconciliation signal. A trace with the amounts
blanked out cannot show why `amount_mismatch` fired, which would trade all of
the diagnostic value for none of the privacy.

Two conventions matter when extending the key sets below:

* Match on exact names and narrow suffixes, never on a bare `key` substring.
  The repository returns a legitimate, non-sensitive `action_key` on every
  action result, and it must stay visible in the trace.
* Nothing in this module raises. A redactor that throws would take down the
  request it was only supposed to describe, which is the one outcome worse
  than an unredacted log line.
"""

from __future__ import annotations

import json
import re
from typing import Any

REDACTED = "[redacted]"

# Bounds on structure walking. A span attribute is a description of a payload,
# not the payload itself, so it is capped rather than faithfully reproduced.
MAX_DEPTH = 6
MAX_ITEMS = 60
DEFAULT_TEXT_LIMIT = 2_000

# Prompt capture is bounded twice: per message, so one long tool result cannot
# crowd the system prompt out of the window, and then overall.
PROMPT_MESSAGE_LIMIT = 1_500
PROMPT_TOTAL_LIMIT = 8_000

# Response capture is smaller: the final response is a three-field JSON object.
RESPONSE_TEXT_LIMIT = 4_000

# Compared after lowercasing and folding "-" to "_", so "X-Api-Key" arrives here
# as "x_api_key".
SENSITIVE_KEYS = frozenset(
    {
        # Credentials and transport authentication.
        "authorization",
        "auth",
        "authorization_header",
        "proxy_authorization",
        "www_authenticate",
        "bearer",
        "jwt",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "apikey",
        "x_api_key",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "pwd",
        "credential",
        "credentials",
        "private_key",
        # pydantic's SecretStr keeps the plaintext here.
        "_secret_value",
        "signature",
        "cookie",
        "cookies",
        "set_cookie",
        "session",
        "session_id",
        # The concrete service credentials this deployment holds.
        "anon_key",
        "service_role_key",
        "supabase_key",
        "supabase_anon_key",
        "supabase_service_role_key",
        "groq_api_key",
        "logfire_token",
        # Customer identity. Named in the schema, unused by every rule.
        "customer_name",
        "customer_email",
        "beneficiary_name",
        "email",
        "phone",
        "phone_number",
        "address",
        # Payment instrument and settlement references.
        "utr",
        "account_number",
        "bank_account",
        "card_number",
        "pan",
        "cvv",
        "ifsc",
        "upi_id",
        "vpa",
    }
)

# Suffixes stay narrow on purpose. "_key" is absent because `action_key` ends
# with it and is a non-sensitive idempotency handle the trace needs.
SENSITIVE_KEY_SUFFIXES = (
    "_token",
    "_secret",
    "_password",
    "_api_key",
    "_apikey",
    "_credential",
    "_credentials",
    "_email",
    "_private_key",
)

# Value-level scrubbing, for secrets that arrive inside free text rather than
# under a name of their own — an error message quoting a header, say. The
# prefixes are the ones this stack actually issues: Supabase, Logfire, Groq.
_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), f"Bearer {REDACTED}"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]+"), REDACTED),
    (re.compile(r"\b(?:sb[a-z]*_|pylf_v1_[a-z]{2}_|gsk_|sk_)[A-Za-z0-9_\-]{16,}"), REDACTED),
)


def is_sensitive_key(key: str) -> bool:
    """True when a mapping key names something that must not reach a span."""
    lowered = key.strip().lower().replace("-", "_")
    return lowered in SENSITIVE_KEYS or lowered.endswith(SENSITIVE_KEY_SUFFIXES)


def scrub_text(value: str) -> str:
    """Replace credential-shaped substrings inside free text."""
    for pattern, replacement in _VALUE_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def truncate_text(value: str, limit: int = DEFAULT_TEXT_LIMIT) -> str:
    """Cap a string, saying how much was dropped rather than trailing off."""
    if limit <= 0 or len(value) <= limit:
        return value
    return f"{value[:limit]}... [truncated {len(value) - limit} chars]"


def redact(value: Any, *, text_limit: int = DEFAULT_TEXT_LIMIT) -> Any:
    """Return a bounded, credential-free copy of `value`, whatever it is.

    Never raises: an unexpected shape degrades to the redaction marker instead
    of propagating into the request it was describing.
    """
    try:
        return _redact(value, text_limit, 0)
    except Exception:
        return REDACTED


def redact_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """`redact` specialised to a span/log attribute mapping."""
    result = redact(attributes)
    return result if isinstance(result, dict) else {"attributes": result}


def redact_prompt(messages: list[dict[str, Any]]) -> str:
    """Render a Groq message list as one bounded, redacted string.

    Captured on purpose. A reviewer scrolling the trace should be able to see
    that the user message contained only `txn_id` and the finished diagnosis,
    and that no message ever asked the model for a verdict.

    Message content reaches here as JSON — the diagnosis, and every tool result
    the loop fed back — so it is parsed and walked with the same key rules as any
    other payload before being re-rendered. Scrubbing the serialised string
    instead would leave `"customer_name": "..."` intact, since no value-level
    pattern can recognise a name. Logfire's own scrubber would still catch it,
    but the guarantee belongs at this boundary, not in the exporter's config.
    """
    lines: list[str] = []
    for message in messages:
        try:
            role = str(message.get("role", "unknown"))
            content = message.get("content")
            if content is None and message.get("tool_calls"):
                rendered = f"tool_calls={redact(message['tool_calls'])}"
            else:
                rendered = _redact_content(content)
            body = truncate_text(scrub_text(rendered), PROMPT_MESSAGE_LIMIT)
        except Exception:
            role, body = "unknown", REDACTED
        lines.append(f"[{role}] {body}")
    return truncate_text("\n".join(lines), PROMPT_TOTAL_LIMIT)


def _redact_content(content: Any) -> str:
    """Redact one message body, structurally when it is JSON."""
    if content is None:
        return ""
    if not isinstance(content, str):
        return str(redact(content))
    stripped = content.strip()
    if not stripped.startswith(("{", "[")):
        return content
    try:
        parsed = json.loads(stripped)
    except Exception:
        return content
    return json.dumps(redact(parsed), default=str, sort_keys=True)


def _redact(value: Any, text_limit: int, depth: int) -> Any:
    if depth > MAX_DEPTH:
        return "[truncated: max depth]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return truncate_text(scrub_text(value), text_limit)
    if isinstance(value, dict):
        return _redact_mapping(value, text_limit, depth)
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        rendered = [_redact(item, text_limit, depth + 1) for item in items[:MAX_ITEMS]]
        if len(items) > MAX_ITEMS:
            rendered.append(f"[truncated {len(items) - MAX_ITEMS} more items]")
        return rendered
    # An arbitrary object: expose its public fields as a mapping so the key
    # rules apply to them. Falling back to repr() here would be the leak —
    # AuthenticatedUser reprs its email, and no key-based rule can see inside a
    # formatted string. Private attributes are dropped rather than walked, which
    # is also what keeps pydantic's SecretStr from surrendering `_secret_value`;
    # with nothing public left it falls through to its own masked str().
    fields = getattr(value, "__dict__", None)
    if isinstance(fields, dict):
        public = {key: item for key, item in fields.items() if not str(key).startswith("_")}
        if public:
            rendered = _redact_mapping(public, text_limit, depth)
            rendered["__type__"] = type(value).__name__
            return rendered
    return truncate_text(scrub_text(str(value)), text_limit)


def _redact_mapping(value: dict[Any, Any], text_limit: int, depth: int) -> dict[str, Any]:
    rendered: dict[str, Any] = {}
    for index, (key, item) in enumerate(value.items()):
        if index >= MAX_ITEMS:
            rendered["__truncated__"] = f"{len(value) - MAX_ITEMS} more keys"
            break
        name = str(key)
        rendered[name] = REDACTED if is_sensitive_key(name) else _redact(item, text_limit, depth + 1)
    return rendered

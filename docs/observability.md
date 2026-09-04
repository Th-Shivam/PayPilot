# Observability

PayPilot traces one resolve as one tree. The point is to be able to answer, from
the trace alone, *why* a transaction was classified the way it was and *who* is
at fault when it fails — without any customer identity or credential reaching
the tracing backend.

Tracing is [Pydantic Logfire](https://logfire.pydantic.dev). It is optional:
`LOGFIRE_TOKEN` unset, the `logfire` package missing, or Logfire unreachable all
degrade to local structured logging. The resolve path is unchanged either way.

## The span tree for one resolve

```
POST /resolve                         request span, request_id
└── repository.resolve                paypilot.outcome, match_status, action, run_id
    ├── supabase.select               ×3, one per source (gateway, bank, ledger)
    ├── supabase.upsert               one per persisted trace event
    ├── reconciliation.compare_records  the verdict — before any model call
    └── agent.run
        ├── groq.chat_completion      prompt in, response out, attempts
        └── agent.tool.<name>         arguments, result_status, result
```

`reconciliation.compare_records` sits **above and before** `groq.chat_completion`
on purpose. Read in order, the two spans are the evidence that the deterministic
rule engine had already produced `match_status`, `confidence` and `reason_code`,
and that the model was handed a finished verdict and only asked to word it. The
captured prompt and response are what make that checkable rather than asserted.

## Correlation

One id per request, generated once in `RequestCorrelationMiddleware`:

* `X-Request-Id` on every response, including errors
* `request_id` in every error body
* `request_id` as OpenTelemetry baggage, so it lands on every descendant span —
  including the ones opened in the worker thread `asyncio.to_thread` spins up
  for the synchronous repository

A client-supplied `X-Request-Id` is honoured when it matches
`[A-Za-z0-9._:-]{1,128}`, and replaced with a generated one when it does not.
Rejecting rather than sanitising keeps a header-injection attempt out of both the
response headers and the trace.

So: read the id off a failed response, paste it into Logfire, get the whole run.

## Error classes

Every failure is logged with `paypilot.error_class`, which is the field to filter
on. The wire format (`code`, `message`, `request_id`) is owned by issue #11 and
is unchanged here.

| class | meaning | status codes |
| --- | --- | --- |
| `user_error` | the caller's request was wrong | 401, 403, 404, 422 |
| `dependency_failure` | Supabase, Groq or the ownership check was unreachable | 503 |
| `application_bug` | reached the catch-all handler; ours to fix | 500 |

A degraded-but-successful run is logged too: when Groq is unavailable the resolve
still returns the deterministic explanation with a 200, and
`resolve.groq_unavailable` records the dependency failure that would otherwise be
invisible behind it.

## Redaction

Redaction happens at PayPilot's own boundary, in `backend/observability/redaction.py`.
Every span attribute and log field passes through it. Logfire's own scrubber stays
on as a second net for values PayPilot never sees, such as anything FastAPI
instrumentation captures.

**Removed** — by exact key name or narrow suffix, at any depth:

* credentials: `authorization`, `token`, `*_api_key`, `secret`, `password`,
  `cookie`, `session`, Supabase/Groq/Logfire keys, and pydantic `SecretStr`
* identity: `customer_name`, `beneficiary_name`, `email`, `phone`, `address`
* payment instruments and references: `utr`, `account_number`, `card_number`,
  `pan`, `cvv`, `ifsc`, `upi_id`, `vpa`

Credential-shaped values are also matched inside free text: `Bearer …`, JWTs, and
the `sb…_`, `pylf_v1_…`, `gsk_`, `sk_` prefixes this stack issues.

**Deliberately kept**: amounts, currencies, statuses, timestamps, `txn_id`,
`run_id`, `reason_code`, and `action_key`. These are the reconciliation signal. A
trace with the amounts blanked out cannot show why `amount_mismatch` fired, which
trades all of the diagnostic value for none of the privacy. `action_key` is an
idempotency handle, not a secret — which is why the suffix rules stop short of a
bare `_key`.

Two details worth knowing before changing this file:

* Prompt content is JSON, so it is parsed and walked with the key rules rather
  than string-scrubbed. Scrubbing the serialised form would leave
  `"customer_name": "..."` intact, because no value pattern can recognise a name.
* Logfire's default patterns include a bare `auth`, matched against values as
  well as keys. Unhandled, that replaces a captured prompt with
  `[Scrubbed due to 'auth']` the moment the system prompt says "authorized", and
  flattens a `not_authorized` tool result — the interesting outcome — into the
  same marker. `_keep_self_redacted` exempts PayPilot's own namespaces, which
  have already been redacted, and leaves everything else scrubbed.

Payloads are bounded as well as redacted: 1.5 KB per prompt message, 8 KB per
prompt, 4 KB per model response, depth 6, 60 items per collection.

## Failure is never the request's problem

`backend/observability/tracing.py` swallows its own errors everywhere. `span()`
re-implements `with` by hand so that entering and exiting the span are guarded
individually while exceptions from the body propagate untouched — a failing
exporter can never replace the caller's exception with its own. `redact()` returns
`[redacted]` rather than raising on an unexpected shape.

Two tests pin this: `test_resolve_still_succeeds_when_the_exporter_is_broken`
and `test_tracing_disabled_is_a_working_no_op`.

## Running the tests

Under pytest the exporter is off but instrumentation stays on, so the suite
exercises every span and every redaction path without shipping fixture data to a
real Logfire project. `backend/tests/test_observability.py` reads spans back
through a local exporter injected via `configure_observability(..., span_processors=[...])`.

## Out of scope here

Retry, timeout and rate-limit policy is issue #4. The wire error format and the
stable error-code set the UI switches on is issue #11. This document covers
instrumentation, correlation, redaction and log structure only.

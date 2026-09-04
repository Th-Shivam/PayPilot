# Testing

The backend suite protects the full resolve workflow — deterministic
reconciliation, the agent action guards, the API contract, auth, and the SSE
stream — before a demo.

## Running locally

From the repository root:

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate   # macOS/Linux

pip install -r backend/requirements-dev.txt
python -m pytest backend/tests
```

That is the whole setup. The suite uses in-memory fakes for Supabase and Groq
and never opens a network connection, so:

- **No credentials are required.** `GROQ_API_KEY`, `SUPABASE_URL`, and the rest
  can all be unset. A run that needs them is a bug in the test, not the config.
- **No heavy runtime deps.** `supabase`, `groq`, and `sentence-transformers`
  (torch) are imported lazily by the app and are not installed for tests, which
  keeps the run to a second or two.

To run the full application locally you still need `backend/requirements.txt`
and a populated `backend/.env`; that is separate from testing.

## Continuous integration

`.github/workflows/backend-tests.yml` runs `python -m pytest backend/tests` on
every push to `master` and every pull request, installing only
`backend/requirements-dev.txt`. The job references no secrets.

## What is covered

| Area | Files |
|---|---|
| Deterministic diagnosis, all six statuses, precedence, boundaries | `test_matching_rules.py` |
| Fixture distribution, reproducibility, self-consistency | `test_fixtures.py` |
| Lookup tools, loader, similarity search, embedding backfill | `test_search_and_loading.py` |
| Resolution actions: idempotency and guards | `test_resolution_actions.py` |
| Agent loop with a mocked Groq client | `test_agent_orchestrator.py` |
| API contract, errors, OpenAPI, CORS, SSE framing | `test_api.py` |
| Auth and role-aware access | `test_auth.py` |
| Migration ↔ code column contract | `test_migration_contract.py` |
| **End-to-end: HTTP → real repository → compare_records → actions** | `test_e2e_resolution.py` |

## The end-to-end layer

`test_e2e_resolution.py` is the integration seam the unit tests leave open.
Everywhere else, the API is tested against a pre-baked `InMemoryRepository` and
the reconciliation logic is tested in isolation — they never run together. The
e2e tests wire the **real** `SupabaseRepository` over a fake Supabase seeded
from the actual fixture generator, then drive `POST /resolve` through HTTP.

They assert, for the five diagnosis paths on their stable demo IDs
(`TXNCLEAN001`, `TXNLEDGERGAP001`, `TXNPENDING001`, `TXNANOMALY001`,
`TXNAMOUNTMISMATCH001`):

- the verdict and action that emerge through the whole stack,
- that a repeated `/resolve` does not duplicate a ledger entry or a ticket
  (idempotency),
- that the LLM explanation cannot override the deterministic verdict,
- that streaming emits the real decision and completion events,
- that an unknown transaction is a clean 404 with a partial failed trace.

Assertions target specific fields and status codes, so a failure points at the
layer that broke rather than at a snapshot diff.

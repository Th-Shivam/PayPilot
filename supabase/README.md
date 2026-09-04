# Supabase schema

Migrations for PayPilot's reconciliation schema. Issue #10.

## Apply

Numbered files must run in order — `0004` defines a function that `0005` grants
on, and `0005` references the `exception_list` view from `0003`.

**Option A — Supabase SQL editor** (no tooling required)

Paste each file in order and run:

```
0001_extensions.sql
0002_core_tables.sql
0003_tickets_and_traces.sql
0004_similarity_search.sql
0005_rls_policies.sql
```

**Option B — Supabase CLI**

```bash
supabase link --project-ref <ref>
supabase db push
```

Every file is idempotent: `create table if not exists`, `create or replace
function`, `drop policy if exists` before create, and index creation guarded by
existence checks. Re-running the full set is safe.

## Tables

| Table | Purpose |
|---|---|
| `gateway_transactions` | Payment gateway capture feed. Did the payment succeed? |
| `bank_settlements` | Bank settlement feed. No row past T+2 is an anomaly. |
| `ledger_entries` | Internal ledger. No row where gateway and bank agree is a `ledger_gap`. |
| `tickets` | One row per resolved transaction, with the diagnosis and its embedding. |
| `agent_trace_logs` | Ordered agent steps per resolve run. |
| `exception_list` (view) | Tickets at `low_flagged_for_review`. The PS-8 exception list. |

## Decisions worth knowing

**No foreign keys between the three feeds.** In production these are separate
exports from separate systems. A row in one without the others is exactly what
the agent diagnoses — an FK would make the `anomaly` and orphan-ledger cases
unrepresentable.

**`unique (txn_id)` on all four business tables.** Lookups return a single
record, and repeated `/resolve` must be idempotent. Without it a second run
inserts a duplicate ledger row, which is the most likely thing to happen during
a demo.

**`expected_settlement_at` is stored, not derived.** Computing T+2 from `now()`
at query time means a transaction drifts from `pending` to `anomaly` between
seeding and the demo. Seeding writes the value; the diagnosis compares two
stored timestamps.

**`ledger_entries.source`** separates `system` from `agent_reconciliation`. An
agent-written entry must be distinguishable from a genuine one.

**No `'missing'` ledger status.** A missing entry is the absence of a row.
Encoding it as a row would give the comparison logic two representations of one
state.

**`captured_at`, not `timestamp`.** Reserved word; would need quoting in every
query and PostgREST filter.

**Taxonomy enforced by CHECK constraints.** `diagnosis` accepts only the six
`match_status` values, so naming drift fails at write time instead of appearing
as an empty chart segment.

**Three cross-field constraints on `tickets`:**
- `ledger_entry_created` requires `diagnosis = 'ledger_gap'`
- `anomaly` and `unknown` cannot be auto-closed, only escalated
- `unknown` forces `confidence = 'low_flagged_for_review'`

These hold regardless of what the calling code does.

**RLS is on now, permissive where needed.** The dashboard reads Supabase REST
directly from the browser, so backend token validation doesn't apply to that
path — only RLS does. Enabling it later is a trap: RLS on with no policy returns
an empty set with HTTP 200, so the table renders blank with nothing in the logs.

Current: `tickets` and `agent_trace_logs` readable by `anon`; the three source
feeds have no client policy and are revoked from `anon`. Writes are service-role
only. Phase 5 replaces the read policies with owner-scoped versions — an edit to
an existing policy, not a change in enforcement.

## Verification status

Written and statically reviewed. **Not yet executed against a live database** —
local Docker pulls of `pgvector/pgvector:pg16` failed on network, and no `psql`
is installed on this machine.

Two things specifically worth watching on first apply:

1. `match_tickets` sets `search_path = public, extensions`. Supabase installs
   `vector` into `extensions`; a plain Postgres container installs it into
   `public`. Listing both should resolve `<=>` either way, but this is the most
   environment-sensitive line in the set.
2. `exception_list` sets `security_invoker` only on Postgres 15+. Supabase is
   15+, so it should apply; on an older instance the view would run with owner
   privileges and bypass RLS.

After applying, confirm:

```sql
-- 6 tables/views
select table_name from information_schema.tables
where table_schema = 'public' order by table_name;

-- 5 tables, all rowsecurity = true
select tablename, rowsecurity from pg_tables
where schemaname = 'public' order by tablename;

-- 2 policies
select tablename, policyname from pg_policies
where schemaname = 'public' order by tablename;

-- embedding index exists
select indexname, indexdef from pg_indexes
where tablename = 'tickets' and indexname = 'tickets_embedding_idx';

-- RPC resolves (expect 0 rows on an empty table, not an error)
select * from match_tickets(array_fill(0::real, array[384])::vector, 0.75, 3);
```

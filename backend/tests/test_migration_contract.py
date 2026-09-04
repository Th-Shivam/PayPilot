from pathlib import Path


MIGRATIONS = Path(__file__).parents[2] / "supabase" / "migrations"


def test_ticket_schema_contains_repository_contract_columns():
    schema = (MIGRATIONS / "0003_tickets_and_traces.sql").read_text(encoding="utf-8")
    assert "action_taken text" in schema
    assert "confidence   text" in schema
    assert "embedding    vector(384)" in schema


def test_canonical_migrations_are_ordered_without_legacy_duplicates():
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    assert names[:8] == ["0001_extensions.sql", "0002_core_tables.sql", "0003_tickets_and_traces.sql", "0004_similarity_search.sql", "0005_rls_policies.sql", "0006_action_idempotency.sql", "0007_trace_event_contract.sql", "0008_auth_rbac.sql"]
    assert names == sorted(names)


def test_similarity_rpc_is_bounded_and_uses_pgvector_cosine_distance():
    migration = (MIGRATIONS / "0004_similarity_search.sql").read_text(encoding="utf-8")
    assert "create or replace function match_tickets" in migration
    assert "query_embedding vector(384)" in migration
    assert "1 - (t.embedding <=> query_embedding)" in migration
    assert "limit least(greatest(coalesce(match_count, 1), 1), 20)" in migration
    assert "using hnsw (embedding vector_cosine_ops)" in migration


def test_trace_event_contract_has_stable_identity_and_safe_statuses():
    migration = (MIGRATIONS / "0007_trace_event_contract.sql").read_text(encoding="utf-8")
    assert "alter table agent_trace_logs add column if not exists event_id text" in migration
    assert "create unique index if not exists agent_trace_logs_event_id_idx" in migration
    assert "event_type in ('tool_start', 'tool_result', 'decision', 'action', 'retry', 'completion')" in migration
    assert "status in ('running', 'success', 'warning', 'not_found', 'failed', 'completed')" in migration
    assert "step_name like 'tool_call:%'" in migration


def test_auth_migration_defines_profiles_and_owner_scoped_rls():
    migration = (MIGRATIONS / "0008_auth_rbac.sql").read_text(encoding="utf-8")
    assert "create table if not exists public.profiles" in migration
    assert "profiles_role_check" in migration
    assert "references auth.users" in migration
    assert "create or replace function public.is_support_agent()" in migration
    assert "owner_id = auth.uid()" in migration
    assert "drop policy if exists tickets_read_all" in migration
    assert "revoke all on public.tickets from anon, authenticated" in migration
    assert "grant execute on function match_tickets(vector, float, int, text) to authenticated" in migration

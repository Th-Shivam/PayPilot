from pathlib import Path


MIGRATIONS = Path(__file__).parents[2] / "supabase" / "migrations"


def test_ticket_schema_contains_repository_contract_columns():
    schema = (MIGRATIONS / "0003_tickets_and_traces.sql").read_text(encoding="utf-8")
    assert "action_taken text" in schema
    assert "confidence   text" in schema
    assert "embedding    vector(384)" in schema


def test_canonical_migrations_are_ordered_without_legacy_duplicates():
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    assert names == ["0001_extensions.sql", "0002_core_tables.sql", "0003_tickets_and_traces.sql", "0004_similarity_search.sql", "0005_rls_policies.sql"]

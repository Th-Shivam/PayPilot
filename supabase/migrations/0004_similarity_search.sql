-- 0004_similarity_search.sql
-- pgvector cosine similarity over ticket explanations, backing
-- search_similar_tickets (issue #1).
--
-- Exposed as an RPC rather than raw SQL in the client so the threshold and
-- limit are enforced server-side and the query cannot be rewritten to return
-- the whole table.

create or replace function match_tickets(
    query_embedding vector(384),
    match_threshold float  default 0.75,
    match_count     int    default 3,
    exclude_txn_id  text   default null
)
returns table (
    txn_id       text,
    diagnosis    text,
    reason_code  text,
    explanation  text,
    action_taken text,
    confidence   text,
    similarity   float,
    created_at   timestamptz
)
language sql
stable
-- search_path must include the schema holding the `vector` type and its
-- operators. Supabase installs extensions into `extensions`; a plain Postgres
-- container installs into `public`. Listing both makes `<=>` and vector(384)
-- resolve either way. An empty search_path would fail at parse time, since
-- operator lookup cannot be schema-qualified inline without OPERATOR() syntax.
set search_path = public, extensions
as $$
    select
        t.txn_id,
        t.diagnosis,
        t.reason_code,
        t.explanation,
        t.action_taken,
        t.confidence,
        -- `<=>` is cosine distance in [0,2]; similarity is its complement.
        1 - (t.embedding <=> query_embedding) as similarity,
        t.created_at
    from public.tickets t
    where t.embedding is not null
      and (exclude_txn_id is null or t.txn_id <> exclude_txn_id)
      and 1 - (t.embedding <=> query_embedding) >= match_threshold
    order by t.embedding <=> query_embedding
    limit least(greatest(coalesce(match_count, 1), 1), 20);
$$;

comment on function match_tickets is
    'Top-N similar past tickets by cosine similarity. Bounded to 20 results regardless of caller input.';

-- HNSW gives good recall without needing a populated table at build time,
-- unlike ivfflat which requires training data to pick centroids. Falls back to
-- ivfflat if the pgvector build predates HNSW support (< 0.5.0).
do $$
begin
    perform set_config('search_path', 'public, extensions', true);

    if not exists (
        select 1 from pg_indexes
        where schemaname = 'public' and indexname = 'tickets_embedding_idx'
    ) then
        begin
            create index tickets_embedding_idx
                on public.tickets using hnsw (embedding vector_cosine_ops);
        exception when others then
            -- Subtransaction rolls back the failed HNSW attempt, so the
            -- ivfflat fallback starts clean.
            create index tickets_embedding_idx
                on public.tickets using ivfflat (embedding vector_cosine_ops)
                with (lists = 10);
        end;
    end if;
end
$$;

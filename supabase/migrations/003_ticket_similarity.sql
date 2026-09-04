create index if not exists tickets_explanation_embedding_hnsw_idx
  on public.tickets using hnsw (explanation_embedding vector_cosine_ops);

create or replace function public.search_similar_tickets(
  query_embedding vector(384), match_threshold float, match_count integer
)
returns table (ticket_id text, score float, status text, explanation text)
language sql stable
as $$
  select t.ticket_id, (1 - (t.explanation_embedding <=> query_embedding))::float,
         t.status, t.explanation
  from public.tickets t
  where t.explanation_embedding is not null
    and (1 - (t.explanation_embedding <=> query_embedding)) >= greatest(0, least(match_threshold, 1))
  order by t.explanation_embedding <=> query_embedding
  limit least(greatest(match_count, 1), 50);
$$;

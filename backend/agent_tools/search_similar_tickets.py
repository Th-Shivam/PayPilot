"""Similar-case retrieval over past tickets via pgvector.

Calls the match_tickets RPC defined in supabase/migrations/0004. The threshold
and result cap are enforced server-side; the bounds here are a second layer.
"""

from __future__ import annotations

from typing import Any

from backend.embeddings import EMBEDDING_DIMENSION, EmbeddingService, EmbeddingServiceError

RPC_NAME = "match_tickets"
MAX_RESULTS = 20  # Matches the server-side cap in match_tickets.


def search_similar_tickets(
    query: str,
    supabase: Any,
    embeddings: EmbeddingService,
    threshold: float = 0.70,
    limit: int = 3,
    exclude_txn_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return the closest past tickets, or an empty list on any failure.

    Similar-case context is an enhancement to an explanation, never the basis of
    a diagnosis, so failing soft here cannot corrupt a verdict.
    """
    if not query or not query.strip():
        return []
    if not 0.0 <= threshold <= 1.0:
        return []

    bounded_limit = max(1, min(int(limit), MAX_RESULTS))

    try:
        embedding = embeddings.embed(query)
    except (EmbeddingServiceError, ValueError):
        return []

    if len(embedding) != EMBEDDING_DIMENSION:
        return []

    try:
        response = supabase.rpc(
            RPC_NAME,
            {
                "query_embedding": embedding,
                "match_threshold": threshold,
                "match_count": bounded_limit,
                "exclude_txn_id": exclude_txn_id,
            },
        ).execute()
    except Exception:
        return []

    results: list[dict[str, Any]] = []
    for row in getattr(response, "data", None) or []:
        similarity = float(row.get("similarity", 0.0))
        if similarity < threshold:
            continue
        results.append(
            {
                "txn_id": row.get("txn_id"),
                "similarity": round(similarity, 4),
                "diagnosis": row.get("diagnosis"),
                "reason_code": row.get("reason_code"),
                "explanation": row.get("explanation"),
                "action_taken": row.get("action_taken"),
                "confidence": row.get("confidence"),
            }
        )
    return results[:bounded_limit]

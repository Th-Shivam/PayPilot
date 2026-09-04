from __future__ import annotations

from typing import Any

from backend.embeddings import EmbeddingService, EmbeddingServiceError


def search_similar_tickets(
    query: str,
    supabase: Any,
    embeddings: EmbeddingService,
    threshold: float = 0.70,
    limit: int = 5,
) -> list[dict[str, Any]]:
    if not query or not query.strip() or not 0 <= threshold <= 1:
        return []
    bounded_limit = max(1, min(int(limit), 50))
    try:
        response = supabase.rpc(
            "match_tickets",
            {
                "query_embedding": embeddings.embed(query),
                "match_threshold": threshold,
                "match_count": bounded_limit,
                "exclude_txn_id": None,
            },
        ).execute()
        results = []
        for row in response.data or []:
            score = float(row.get("score", row.get("similarity", 0)))
            if score >= threshold:
                results.append({
                "ticket_id": row.get("ticket_id", row.get("txn_id")),
                    "score": score,
                "status": row.get("status", row.get("diagnosis")),
                    "explanation": row.get("explanation"),
                })
        return results
    except (EmbeddingServiceError, ValueError, TypeError, Exception):
        return []

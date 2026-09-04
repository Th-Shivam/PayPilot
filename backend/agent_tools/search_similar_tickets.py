from __future__ import annotations

import math
from typing import Any

from backend.embeddings import EMBEDDING_DIMENSION, EmbeddingService, EmbeddingServiceError


MAX_MATCH_COUNT = 20


def search_similar_tickets(
    query: str,
    supabase: Any,
    embeddings: EmbeddingService,
    threshold: float = 0.75,
    limit: int = 5,
) -> list[dict[str, Any]]:
    if (
        not query
        or not query.strip()
        or not isinstance(threshold, (int, float))
        or not math.isfinite(float(threshold))
        or not 0 <= threshold <= 1
    ):
        return []
    try:
        bounded_limit = max(1, min(int(limit), MAX_MATCH_COUNT))
    except (TypeError, ValueError, OverflowError):
        return []
    try:
        vector = embeddings.embed(query)
        if len(vector) != EMBEDDING_DIMENSION:
            return []
        response = supabase.rpc(
            "match_tickets",
            {
                "query_embedding": vector,
                "match_threshold": threshold,
                "match_count": bounded_limit,
                "exclude_txn_id": None,
            },
        ).execute()
        results = []
        for row in response.data or []:
            try:
                score = float(row.get("score", row.get("similarity", 0)))
            except (TypeError, ValueError):
                continue
            ticket_id = row.get("ticket_id", row.get("txn_id"))
            status = row.get("status", row.get("diagnosis"))
            explanation = row.get("explanation")
            if ticket_id and isinstance(status, str) and isinstance(explanation, str) and score >= threshold:
                results.append({
                    "ticket_id": ticket_id,
                    "score": score,
                    "status": status,
                    "explanation": explanation,
                })
        return results
    except (EmbeddingServiceError, ValueError, TypeError, Exception):
        return []

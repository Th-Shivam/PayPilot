from __future__ import annotations

from threading import Lock
from typing import Any, Sequence


class EmbeddingServiceError(RuntimeError):
    pass


class EmbeddingService:
    """Lazy-load all-MiniLM-L6-v2 once per process and reuse it."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", model: Any | None = None) -> None:
        self.model_name = model_name
        self._model = model
        self._lock = Lock()

    def _get_model(self) -> Any:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    try:
                        from sentence_transformers import SentenceTransformer

                        self._model = SentenceTransformer(self.model_name)
                    except Exception as exc:
                        raise EmbeddingServiceError(f"Unable to load model {self.model_name}") from exc
        return self._model

    def embed(self, text: str) -> list[float]:
        if not text or not text.strip():
            raise ValueError("text must not be blank")
        try:
            values: Sequence[float] = self._get_model().encode(
                text.strip(), convert_to_numpy=False, normalize_embeddings=True
            )
            return [float(value) for value in values]
        except EmbeddingServiceError:
            raise
        except Exception as exc:
            raise EmbeddingServiceError("Embedding inference failed") from exc

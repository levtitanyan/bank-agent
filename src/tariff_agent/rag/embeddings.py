"""Semantic vectors for chunks and queries.

One implementation, plus a documented absence. When a key is configured the
chunks are embedded with Gemini; when it is not - or when the API fails - the
system retrieves with BM25 alone and says so, marking the result *degraded*
rather than quietly returning worse answers.

There is deliberately no offline stand-in embedder. A hashed-n-gram model would
have made the demos look complete while measuring nothing real: its numbers
would not predict the behaviour of the configured system, and a reviewer reading
"hybrid retrieval" would be told something misleading. BM25-only is an honest
degraded mode, and its quality is measured and reported next to the full path.

Task types matter: Gemini embeds documents and queries into deliberately
different spaces, and using ``RETRIEVAL_DOCUMENT`` for both measurably weakens
results. Each is embedded with its own task type.
"""

from __future__ import annotations

import time
from typing import Protocol

from tariff_agent.errors import EmbeddingError
from tariff_agent.observability.logging import get_logger

logger = get_logger(__name__)

BATCH_SIZE = 64
"""Chunks per embedding request."""

MAX_ATTEMPTS = 4
"""Attempts per batch before giving up and degrading to BM25."""

BACKOFF_BASE_SECONDS = 2.0
"""First retry delay. Embedding APIs are quota-limited rather than flaky, and a
sub-second retry simply spends the next attempt against the same limit - a whole
document failed to embed this way before the budget was widened."""


class Embedder(Protocol):
    """What retrieval needs from an embedding model."""

    @property
    def name(self) -> str:
        """Identifier stored in the index, so a model change invalidates it."""
        ...

    @property
    def similarity_floor(self) -> float:
        """Cosine above which a match counts as relevant in this model's space."""
        ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed chunk texts."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed a query."""
        ...


class GeminiEmbedder:
    """Embeddings from the Gemini API.

    Args:
        api_key: AI Studio key.
        model: Embedding model id.
        similarity_floor: Cosine above which a match counts as relevant.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gemini-embedding-001",
        similarity_floor: float = 0.55,
    ) -> None:
        """Create the client and cache the query vectors."""
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._similarity_floor = similarity_floor
        self._query_cache: dict[str, list[float]] = {}

    @property
    def name(self) -> str:
        """Identifier stored in the index."""
        return self._model

    @property
    def similarity_floor(self) -> float:
        """Cosine above which a match counts as relevant."""
        return self._similarity_floor

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed chunk texts, in batches.

        Args:
            texts: The chunk texts.

        Returns:
            One vector per text, in order.

        Raises:
            EmbeddingError: If a batch cannot be embedded within the permitted
                attempts. Callers degrade to BM25 rather than failing the run.
        """
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            batch = texts[start : start + BATCH_SIZE]
            vectors.extend(self._embed(batch, task="RETRIEVAL_DOCUMENT"))
        logger.info(
            "chunks_embedded", extra={"count": len(vectors), "model": self._model}
        )
        return vectors

    def embed_query(self, text: str) -> list[float]:
        """Embed a query, caching the result.

        The ten field queries are fixed, so each is embedded once per process
        however many documents are searched.

        Args:
            text: The query text.

        Returns:
            The query vector.

        Raises:
            EmbeddingError: If the query cannot be embedded.
        """
        if text not in self._query_cache:
            self._query_cache[text] = self._embed([text], task="RETRIEVAL_QUERY")[0]
        return self._query_cache[text]

    def _embed(self, texts: list[str], *, task: str) -> list[list[float]]:
        """Call the API with bounded retries.

        Args:
            texts: Texts to embed.
            task: Task type, which selects the embedding space.

        Returns:
            The vectors.

        Raises:
            EmbeddingError: After the permitted attempts.
        """
        from google.genai import types

        last: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._client.models.embed_content(
                    model=self._model,
                    contents=texts,  # type: ignore[arg-type]
                    config=types.EmbedContentConfig(task_type=task),
                )
                embeddings = response.embeddings or []
                return [list(embedding.values or []) for embedding in embeddings]
            except Exception as exc:  # the SDK raises several unrelated types
                last = exc
                logger.warning(
                    "embedding_attempt_failed",
                    extra={
                        "attempt": attempt,
                        "max_attempts": MAX_ATTEMPTS,
                        "error_type": type(exc).__name__,
                    },
                )
                if attempt < MAX_ATTEMPTS:
                    time.sleep(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
        raise EmbeddingError(f"could not embed {len(texts)} texts: {last}")


def build_embedder(
    api_key: str | None, *, model: str = "gemini-embedding-001"
) -> Embedder | None:
    """Create an embedder when one is configured.

    Args:
        api_key: The configured key, or None.
        model: Embedding model id.

    Returns:
        The embedder, or ``None`` when no key is available - in which case
        retrieval runs on BM25 alone and reports itself degraded.
    """
    if not api_key:
        logger.info("embeddings_unavailable", extra={"reason": "no api key configured"})
        return None
    return GeminiEmbedder(api_key, model=model)

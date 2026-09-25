"""Lexical retrieval over chunks.

BM25 is the half of the hybrid that does not need an API key, and on this corpus
it is not a poor relation: tariff documents state their fields in the bank's own
vocabulary, which is exactly what the field query terms are written from. What
it cannot do is recognise a paraphrase, which is where embeddings earn their
place.

The tokenizer matters more than the ranking function here - see
:mod:`tariff_agent.rag.text` for the Armenian morphology and OCR folding.
"""

from __future__ import annotations

from dataclasses import dataclass

from rank_bm25 import BM25Okapi

from tariff_agent.rag.chunking import Chunk
from tariff_agent.rag.text import tokenize


@dataclass(frozen=True, slots=True)
class LexicalHit:
    """One chunk matched lexically.

    Attributes:
        index: Position of the chunk in the indexed sequence.
        score: Raw BM25 score. Comparable within one query, never across
            queries, which is why fusion is rank-based.
    """

    index: int
    score: float


class LexicalIndex:
    """A BM25 index over a fixed set of chunks.

    Args:
        chunks: The chunks to index, in a stable order.
    """

    def __init__(self, chunks: list[Chunk]) -> None:
        """Tokenize every chunk and build the index."""
        self.chunks = chunks
        self._tokens = [tokenize(chunk.text) for chunk in chunks]
        self._document_frequency: dict[str, int] = {}
        for tokens in self._tokens:
            for token in set(tokens):
                self._document_frequency[token] = self._document_frequency.get(token, 0) + 1
        # rank_bm25 cannot be built from an empty corpus, and an empty document
        # set is a legitimate state (a product whose supporting source failed).
        self._bm25 = BM25Okapi(self._tokens) if any(self._tokens) else None

    def search(self, query: str, *, limit: int = 10) -> list[LexicalHit]:
        """Rank chunks against a query.

        Args:
            query: The query text, tokenized the same way as the chunks.
            limit: Maximum hits to return.

        Returns:
            Hits with a non-zero score, best first.
        """
        if self._bm25 is None:
            return []
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        scores = self._bm25.get_scores(query_tokens)
        ranked = sorted(enumerate(scores), key=lambda pair: pair[1], reverse=True)
        return [
            LexicalHit(index=index, score=float(score))
            for index, score in ranked[:limit]
            if score > 0
        ]

    def document_frequency(self, token: str) -> int:
        """How many chunks contain a token.

        «վճար» (fee) and «հայտ» (application) appear throughout a tariff
        document and separate nothing; «ուսումնասիրության» appears only where
        the application-review fee is described. The relevance gate ranks a
        term's tokens by this, and requires the rarest one to be present - or a
        product would be reported as having a fee it never states.

        Args:
            token: A stemmed token.

        Returns:
            The document frequency, zero when the token is absent entirely.
        """
        return self._document_frequency.get(token, 0)

    def __len__(self) -> int:
        """Number of indexed chunks."""
        return len(self.chunks)

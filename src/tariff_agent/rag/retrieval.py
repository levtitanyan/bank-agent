"""Answering, per tariff field: which passages state this, and are any relevant?

Two decisions live here.

**Fusion.** BM25 scores and cosine similarities are not commensurable - one is
an unbounded lexical score, the other a bounded angle - so they are combined by
*rank* rather than by value, with Reciprocal Rank Fusion. A chunk that only one
retriever finds still surfaces, which matters because the two fail differently:
BM25 misses paraphrase, embeddings miss exact identifiers like «TC 01-03#6».

**The relevance gate is lexical, and deliberately so.** A retriever always
returns its nearest neighbour, so for a field the bank does not offer it returns
*something*, and an extractor will dutifully quote it. The gate therefore asks
two things of the retrieved evidence: that a field's *identifying* term occurs
in it, and that the same chunk contains a value of the field's declared kind.
Requiring only the term was not enough - the consumer page discusses service
fees in a disclaimer that states no fee, and a term-only gate passed on it.

Similarity was measured as an alternative and rejected on the evidence. Over the
consumer-loan page, the maximum cosine for «հայտի ուսումնասիրության վճար» - a
fee that page never mentions - was **0.687**, while «Արժույթ», which it states
plainly, reached **0.682**. Gemini's similarities for this corpus sit in a narrow
0.63-0.81 band whatever the question, so no floor separates present from absent,
and every floor that admits the real fields also admits the missing one.
Embeddings therefore contribute to *ranking*; they are not allowed to decide
whether an answer exists.

Primary and supporting sources are searched **separately**. The bank's own
documents can disagree - the 2023 mortgage summary states a nominal rate the
current product page contradicts - and keeping the two apart is what lets the
extraction step see the disagreement instead of averaging over it.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field

from tariff_agent.fields import FieldSpec
from tariff_agent.observability.logging import get_logger
from tariff_agent.rag.bm25 import LexicalIndex
from tariff_agent.rag.chunking import Chunk, SourceRole
from tariff_agent.rag.text import has_informative_hit
from tariff_agent.rag.value_shapes import has_value_shape

logger = get_logger(__name__)

RRF_K = 60
"""The constant in 1/(k + rank). 60 is the value from the original RRF paper;
it flattens the difference between ranks 1-3 so one retriever cannot dominate."""

TERM_WEIGHTS = (1.0, 0.7, 0.5, 0.4)
"""Weight per query term, by position.

The registry lists each field's terms from most to least canonical: the exact
Armenian field name first, then paraphrases, then English. Treating them equally
let a chunk that ranked first for a *secondary* phrasing outrank one that ranked
first for the field's own name - «ամսական վճարումների» beating the service-fee
table because the term «ամսական սպասարկման վճար» half-matched it."""

SHAPE_WEIGHT = 1.5
"""Weight of the value-shape ranking.

Higher than any single phrasing because it is a conjunction of two conditions -
the field is mentioned *and* a value of the right kind is present - and so is
strictly more specific than a lexical match alone."""


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    """A chunk with the evidence for its ranking.

    Attributes:
        chunk: The chunk itself.
        score: Fused RRF score.
        has_value: Whether the chunk contains a value of the field's kind.
        lexical_rank: 1-based BM25 rank, or None if BM25 did not return it.
        semantic_rank: 1-based vector rank, or None.
        lexical_score: Raw BM25 score, kept for the gate and for debugging.
        similarity: Cosine similarity, when embeddings were used.
        has_term: Whether a field query term literally occurs in the chunk.
    """

    chunk: Chunk
    score: float
    lexical_rank: int | None = None
    semantic_rank: int | None = None
    lexical_score: float = 0.0
    similarity: float | None = None
    has_term: bool = False
    has_value: bool = False

    def explain(self) -> str:
        """Describe why this chunk was returned, for logs and reviewers."""
        parts = []
        if self.lexical_rank:
            parts.append(f"bm25 #{self.lexical_rank} ({self.lexical_score:.1f})")
        if self.semantic_rank:
            similarity = f" ({self.similarity:.2f})" if self.similarity is not None else ""
            parts.append(f"vector #{self.semantic_rank}{similarity}")
        if self.has_term:
            parts.append("contains query term")
        if self.has_value:
            parts.append("contains a value of the right kind")
        return ", ".join(parts) or "no signal"


@dataclass(frozen=True, slots=True)
class FieldRetrieval:
    """Everything retrieval can say about one tariff field.

    Attributes:
        field_id: The registry field.
        primary: Chunks from the product's primary source, best first.
        supporting: Chunks from supporting sources, best first.
        is_relevant: Whether anything retrieved is worth extracting from.
        reason: Why, in one human-readable sentence.
        degraded: True when embeddings were unavailable and only BM25 ran.
    """

    field_id: str
    primary: list[ScoredChunk] = dataclass_field(default_factory=list)
    supporting: list[ScoredChunk] = dataclass_field(default_factory=list)
    is_relevant: bool = False
    reason: str = ""
    degraded: bool = False

    @property
    def best(self) -> ScoredChunk | None:
        """The single best chunk, preferring the primary source."""
        ranked = self.primary or self.supporting
        return ranked[0] if ranked else None

    @property
    def all_chunks(self) -> list[ScoredChunk]:
        """Primary chunks followed by supporting ones."""
        return [*self.primary, *self.supporting]


def reciprocal_rank_fusion(
    rankings: list[list[int]],
    *,
    weights: list[float] | None = None,
    k: int = RRF_K,
) -> dict[int, float]:
    """Fuse several rankings of the same items by rank.

    Args:
        rankings: Each inner list holds item indices, best first.
        weights: Per-ranking weights; defaults to 1.0 for each. Weighting is
            needed because the rankings are not equally trustworthy - see
            :data:`TERM_WEIGHTS` and :data:`SHAPE_WEIGHT`.
        k: The RRF constant.

    Returns:
        Item index to fused score, higher being better.
    """
    fused: dict[int, float] = {}
    for position_in_list, ranking in enumerate(rankings):
        weight = weights[position_in_list] if weights else 1.0
        for position, index in enumerate(ranking, start=1):
            fused[index] = fused.get(index, 0.0) + weight / (k + position)
    return fused


class Retriever:
    """Hybrid retrieval over one product's chunks.

    Args:
        chunks: All chunks for the product, both roles.
        vectors: Chunk vectors aligned with ``chunks``, or None for BM25 only.
            Coverage may be partial: a document whose embedding failed
            contributes zero vectors, and its chunks then compete lexically
            only rather than disabling semantic ranking for the whole product.
        embed_query: Callable returning a query vector, or None.
        similarity_floor: Recorded for logging and debugging only. It does not
            gate relevance - see the module docstring for the measurement that
            ruled that out.
    """

    def __init__(
        self,
        chunks: list[Chunk],
        *,
        vectors: list[list[float]] | None = None,
        embed_query: object | None = None,
        similarity_floor: float = 0.55,
    ) -> None:
        """Build one lexical index per source role."""
        self._chunks = chunks
        self._vectors = vectors
        self._embed_query = embed_query
        self._similarity_floor = similarity_floor
        self._by_role = {
            role: [index for index, chunk in enumerate(chunks) if chunk.source_role is role]
            for role in SourceRole
        }
        self._lexical = {
            role: LexicalIndex([chunks[index] for index in indices])
            for role, indices in self._by_role.items()
        }

    @property
    def uses_embeddings(self) -> bool:
        """Whether a semantic ranking is available."""
        return self._vectors is not None and self._embed_query is not None

    def search_field(self, spec: FieldSpec, *, k: int = 4) -> FieldRetrieval:
        """Retrieve the passages that may state one tariff field.

        Args:
            spec: The field, whose ``query_terms`` drive the search.
            k: How many chunks to keep per source role.

        Returns:
            The retrieval result, including whether it is relevant at all.
        """
        primary = self._search_role(SourceRole.PRIMARY, spec, k)
        supporting = self._search_role(SourceRole.SUPPORTING, spec, k)

        ranked = primary or supporting
        best = ranked[0] if ranked else None
        is_relevant, reason = self._judge(spec, [*primary, *supporting])
        result = FieldRetrieval(
            field_id=spec.id,
            primary=primary,
            supporting=supporting,
            is_relevant=is_relevant,
            reason=reason,
            degraded=not self.uses_embeddings,
        )
        logger.info(
            "field_retrieved",
            extra={
                "field": spec.id,
                "primary_hits": len(primary),
                "supporting_hits": len(supporting),
                "relevant": is_relevant,
                "degraded": result.degraded,
                "top": best.chunk.chunk_id if best else None,
                "top_page": best.chunk.page if best else None,
                "reason": reason,
            },
        )
        return result

    def _query_vector(self, query: str) -> list[float] | None:  # noqa: D401
        """Embed a query, if embeddings are in use.

        Args:
            query: The query text.

        Returns:
            The query vector, or None.
        """
        if not self.uses_embeddings:
            return None
        embed = self._embed_query
        assert callable(embed)
        vector: list[float] = embed(query)
        return vector

    def _search_role(self, role: SourceRole, spec: FieldSpec, k: int) -> list[ScoredChunk]:
        """Retrieve within one source role.

        Each query term is searched **separately** and the rankings are fused.
        Joining the terms into one query dilutes it: «անվանական տոկոսադրույք
        տարեկան տոկոսադրույք nominal interest rate» as a single bag of words
        ranked a marketing banner above the actual rate table, because the
        banner happened to contain more of the common tokens. Searching each
        phrasing on its own and rewarding chunks that several of them agree on
        is both more accurate and, unlike a single ranking, produces a fused
        score that carries information.

        Args:
            role: Primary or supporting.
            spec: The field being retrieved.
            k: How many chunks to keep.

        Returns:
            Scored chunks, best first.
        """
        indices = self._by_role[role]
        if not indices:
            return []

        # Each *signal* votes once, not each phrasing. A field with three
        # query terms would otherwise outvote the value-shape signal three to
        # one purely by having been written three ways, so the per-term results
        # are merged first: a chunk's lexical rank is its best rank across the
        # phrasings.
        rankings: list[list[int]] = []
        lexical_scores: dict[int, float] = {}
        best_rank: dict[int, int] = {}
        # One ranking per phrasing, weighted by how canonical that phrasing is.
        weights: list[float] = []
        for position, term in enumerate(spec.query_terms):
            hits = self._lexical[role].search(term, limit=k * 3)
            if not hits:
                continue
            rankings.append([hit.index for hit in hits])
            weights.append(TERM_WEIGHTS[min(position, len(TERM_WEIGHTS) - 1)])
            for rank_position, hit in enumerate(hits):
                best_rank[hit.index] = min(best_rank.get(hit.index, 10**6), rank_position)
                lexical_scores[hit.index] = max(lexical_scores.get(hit.index, 0.0), hit.score)
        lexical_ranking = sorted(
            best_rank, key=lambda index: (best_rank[index], -lexical_scores[index])
        )

        # A third ranking: chunks that both mention the field and contain a
        # value of the right shape, tables first. In a tariff document the
        # authoritative statement of a numeric field is almost always tabular,
        # and without this BM25 ranks a marketing banner above the rate table
        # that the banner is advertising.
        shaped = [
            local
            for local in range(len(indices))
            if self._has_term(self._chunks[indices[local]], spec, role)
            and has_value_shape(self._chunks[indices[local]].text, spec.kind)
        ]
        shaped.sort(
            key=lambda local: (
                self._chunks[indices[local]].is_table,
                lexical_scores.get(local, 0.0),
            ),
            reverse=True,
        )
        if shaped:
            rankings.append(shaped[: k * 3])
            weights.append(SHAPE_WEIGHT)

        semantic_ranking: list[int] = []
        similarities: dict[int, float] = {}
        if self.uses_embeddings and self._vectors is not None:
            for position, term in enumerate(spec.query_terms):
                query_vector = self._query_vector(term)
                if query_vector is None:
                    continue
                scored = [
                    (local, _cosine(query_vector, self._vectors[global_index]))
                    for local, global_index in enumerate(indices)
                ]
                scored.sort(key=lambda pair: pair[1], reverse=True)
                ranking = [local for local, _ in scored[: k * 3]]
                rankings.append(ranking)
                weights.append(TERM_WEIGHTS[min(position, len(TERM_WEIGHTS) - 1)])
                if not semantic_ranking:
                    semantic_ranking = ranking
                for local, similarity in scored:
                    similarities[local] = max(similarities.get(local, -1.0), similarity)

        fused = reciprocal_rank_fusion(rankings, weights=weights)
        ordered = sorted(fused.items(), key=lambda pair: pair[1], reverse=True)[:k]

        # The gate judges what retrieval returns, so ranking noise could decide
        # whether a field is answerable: adding semantic ranking pushed the one
        # chunk carrying an application fee out of the top four, and the field
        # flipped to NOT_FOUND without any lexical fact changing. If the corpus
        # holds a chunk that both mentions the field and states a value of its
        # kind, one such chunk is always returned.
        if shaped and not any(local in {index for index, _ in ordered} for local in shaped[:1]):
            already = {index for index, _ in ordered}
            guaranteed = next((local for local in shaped if local not in already), None)
            if guaranteed is not None:
                ordered = [*ordered[: max(0, k - 1)], (guaranteed, fused.get(guaranteed, 0.0))]
                logger.info(
                    "lexical_hit_guaranteed",
                    extra={"field": spec.id, "role": role.value, "chunk_index": guaranteed},
                )

        results: list[ScoredChunk] = []
        for local_index, score in ordered:
            chunk = self._chunks[indices[local_index]]
            results.append(
                ScoredChunk(
                    chunk=chunk,
                    score=score,
                    lexical_rank=(
                        lexical_ranking.index(local_index) + 1
                        if local_index in lexical_ranking
                        else None
                    ),
                    semantic_rank=(
                        semantic_ranking.index(local_index) + 1
                        if local_index in semantic_ranking
                        else None
                    ),
                    lexical_score=lexical_scores.get(local_index, 0.0),
                    similarity=similarities.get(local_index),
                    has_term=self._has_term(chunk, spec, role),
                    has_value=has_value_shape(chunk.text, spec.kind),
                )
            )
        return results

    def _has_term(self, chunk: Chunk, spec: FieldSpec, role: SourceRole) -> bool:
        """Whether a field's discriminating terms occur in a chunk.

        Args:
            chunk: The chunk to test.
            spec: The field being retrieved.
            role: Which corpus the chunk belongs to, since what counts as a
                common word differs between a product page and a tariff book.

        Returns:
            True when the field is substantially mentioned.
        """
        index = self._lexical[role]
        return has_informative_hit(
            chunk.text, spec.query_terms, is_informative=index.document_frequency
        )

    def _judge(self, spec: FieldSpec, retrieved: list[ScoredChunk]) -> tuple[bool, str]:
        """Decide whether anything retrieved is worth extracting from.

        Judged over **everything** returned, not just rank 1. The extraction
        step is shown all k chunks, so a field whose value sits at rank 2 is
        answerable; gating on the top chunk alone reported such fields as
        NOT_FOUND while the answer was sitting in the evidence.

        Args:
            spec: The field being retrieved.
            retrieved: Every chunk returned, best first.

        Returns:
            Whether the retrieval is relevant, and why.
        """
        if not retrieved:
            return False, (
                f"no chunk mentions {spec.id} at all; reporting NOT_FOUND rather "
                "than offering the nearest unrelated passage"
            )
        # A mention is not a statement. The consumer page contains «ՎԱՐԿԻ
        # ՏՐԱՄԱԴՐՄԱՆ և ՍՊԱՍԱՐԿՄԱՆ ԳԾՈՎ ԲՈԼՈՐ ՊԱՐՏԱԴԻՐ ՎՃԱՐՆԵՐԸ» - a disclaimer
        # *about* service fees that states no fee. Passing the gate on that would
        # send the extractor to a passage with nothing to extract, so the field
        # must be mentioned **and** a value of its kind must be present in the
        # same chunk.
        stating = next(
            (chunk for chunk in retrieved if chunk.has_term and chunk.has_value), None
        )
        if stating is not None:
            return True, (
                f"{stating.chunk.chunk_id} mentions {spec.id} and contains a value "
                f"of kind {spec.kind.value}"
            )
        mentioned = next((chunk for chunk in retrieved if chunk.has_term), None)
        if mentioned is not None:
            return False, (
                f"{spec.id} is mentioned in {mentioned.chunk.chunk_id} but no retrieved "
                f"chunk states a {spec.kind.value} value; reporting NOT_FOUND rather than "
                "extracting from a passage that only refers to the field"
            )
        return False, (
            f"no identifying term for {spec.id} occurs in the retrieved evidence; "
            "reporting NOT_FOUND rather than quoting the nearest paragraph"
        )


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors.

    Args:
        a: First vector.
        b: Second vector.

    Returns:
        Similarity in -1..1. Vectors are stored L2-normalised, so this is a dot
        product; the norms are recomputed anyway to keep the function honest for
        callers that pass raw vectors.
    """
    import math

    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a)) or 1.0
    norm_b = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (norm_a * norm_b)

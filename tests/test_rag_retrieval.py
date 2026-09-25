"""Tests for the index and for hybrid retrieval.

Embeddings are represented by a fake with fixed vectors: the fusion logic is
what needs testing, and a real API would make the result non-deterministic and
slow. The quality of actual Gemini embeddings is measured separately, against
the real documents, and reported in the decision log.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tariff_agent.documents.document import DocumentKind
from tariff_agent.errors import EmbeddingError, KnowledgeIndexError
from tariff_agent.fields import get_field
from tariff_agent.models import Language
from tariff_agent.rag.chunking import Chunk, ChunkType, SourceRole
from tariff_agent.rag.index import (
    BM25_ONLY,
    INDEX_VERSION,
    build_document_index,
    index_path,
    load_index,
    save_index,
)
from tariff_agent.rag.retrieval import Retriever, reciprocal_rank_fusion

RATE_TEXT = "Տարեկան անվանական տոկոսադրույքը կազմում է 13,5%, փաստացի՝ 15,1%"
TERM_TEXT = "Վարկի ժամկետը սահմանվում է 12-ից 60 ամիս ընկած միջակայքում"
NOISE_TEXT = "Բանկը կարող է պահանջել լրացուցիչ փաստաթղթեր հաճախորդից"
SALARY_TEXT = "Աշխատավարձը Բանկի միջոցով ստանալու դեպքում կիրառվում է արտոնյալ պայման"


def make_chunk(
    index: int,
    text: str,
    *,
    role: SourceRole = SourceRole.PRIMARY,
    chunk_type: ChunkType = ChunkType.TEXT,
    page: int = 1,
) -> Chunk:
    """Build a chunk for retrieval tests."""
    return Chunk(
        chunk_id=f"c{index:03d}",
        doc_id="d" * 64,
        text=text,
        page=page,
        chunk_type=chunk_type,
        source_role=role,
        document_kind=DocumentKind.PDF,
        document_name="Տեղեկատվական ամփոփագիր",
        source_url="https://www.acba.am/files/loan%20info.pdf",
        language=Language.HY,
        retrieved_at=datetime.now(UTC),
        section="Տոկոսադրույք",
    )


class FakeEmbedder:
    """A deterministic embedder with hand-set vectors.

    Args:
        vectors: Text to vector. Anything unseen embeds to zeros, which scores
            zero similarity and so ranks last.
        fail: Whether embedding raises, to exercise the degraded path.
    """

    def __init__(self, vectors: dict[str, list[float]], *, fail: bool = False) -> None:
        """Record the fixed vectors."""
        self._vectors = vectors
        self._fail = fail
        self.document_calls = 0
        self.query_calls = 0

    @property
    def name(self) -> str:
        """Model identifier stored in the index."""
        return "fake-embedder"

    @property
    def similarity_floor(self) -> float:
        """Unused by the gate; kept for interface compatibility."""
        return 0.5

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Return the fixed vectors, counting the call."""
        if self._fail:
            raise EmbeddingError("the embedding service is unavailable")
        self.document_calls += 1
        return [self._vectors.get(text, [0.0, 0.0, 0.0]) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        """Return the fixed query vector, counting the call."""
        if self._fail:
            raise EmbeddingError("the embedding service is unavailable")
        self.query_calls += 1
        return self._vectors.get(text, [0.0, 0.0, 0.0])


# --------------------------------------------------------------------------- #
# Fusion
# --------------------------------------------------------------------------- #


def test_rrf_rewards_agreement_between_rankings() -> None:
    """A chunk both retrievers like beats one that only one of them likes."""
    fused = reciprocal_rank_fusion([[7, 1, 2], [7, 3, 4]])
    assert max(fused, key=lambda index: fused[index]) == 7


def test_rrf_surfaces_a_chunk_only_one_retriever_found() -> None:
    """The two retrievers fail differently, so neither may veto the other."""
    fused = reciprocal_rank_fusion([[1, 2], [3]])
    assert 3 in fused


def test_rrf_weights_shift_the_outcome() -> None:
    """Weighting is what stops three phrasings outvoting a stronger signal."""
    unweighted = reciprocal_rank_fusion([[1], [2], [2]])
    assert unweighted[2] > unweighted[1]
    weighted = reciprocal_rank_fusion([[1], [2], [2]], weights=[3.0, 0.5, 0.5])
    assert weighted[1] > weighted[2]


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #


def test_the_field_that_is_stated_is_retrieved() -> None:
    """The baseline: a rate query finds the passage stating a rate."""
    chunks = [make_chunk(0, NOISE_TEXT), make_chunk(1, RATE_TEXT), make_chunk(2, TERM_TEXT)]
    result = Retriever(chunks).search_field(get_field("nominal_rate"))
    assert result.is_relevant
    assert result.best is not None
    assert "13,5%" in result.best.chunk.text


def test_a_field_nobody_states_is_reported_irrelevant() -> None:
    """The retriever always returns something; the gate is what refuses it."""
    chunks = [make_chunk(0, NOISE_TEXT), make_chunk(1, TERM_TEXT)]
    result = Retriever(chunks).search_field(get_field("application_fee"))
    assert not result.is_relevant
    assert "NOT_FOUND" in result.reason
    # Nothing is offered either: handing the extractor the nearest unrelated
    # paragraph is how a field the bank never states acquires a value.
    assert result.primary == []


def test_primary_and_supporting_are_kept_apart() -> None:
    """Phase 6 compares the two to detect a conflict, so they cannot be merged."""
    chunks = [
        make_chunk(0, RATE_TEXT, role=SourceRole.PRIMARY),
        make_chunk(
            1,
            "Տարեկան անվանական տոկոսադրույքը կազմում է 17,9%",
            role=SourceRole.SUPPORTING,
        ),
    ]
    result = Retriever(chunks).search_field(get_field("nominal_rate"))
    assert len(result.primary) == 1
    assert len(result.supporting) == 1
    assert "13,5%" in result.primary[0].chunk.text
    assert "17,9%" in result.supporting[0].chunk.text


def test_a_table_stating_the_value_outranks_prose_mentioning_it() -> None:
    """In a tariff document the authoritative statement is usually tabular."""
    chunks = [
        make_chunk(0, "Տոկոսադրույքը կարող է փոփոխվել բանկի որոշմամբ առանց ծանուցման"),
        make_chunk(
            1,
            "Տոկոսադրույք\nԱրժույթ | Տարեկան անվանական տոկոսադրույք\nՀՀ դրամ | 13,5%",
            chunk_type=ChunkType.TABLE,
        ),
    ]
    result = Retriever(chunks).search_field(get_field("nominal_rate"))
    assert result.best is not None
    assert result.best.chunk.is_table


def test_without_embeddings_the_result_says_so() -> None:
    """A degraded answer has to announce itself, not look like a full one."""
    result = Retriever([make_chunk(0, RATE_TEXT)]).search_field(get_field("nominal_rate"))
    assert result.degraded is True


def test_with_embeddings_the_result_is_not_degraded() -> None:
    """And the semantic ranking actually participates."""
    chunks = [make_chunk(0, NOISE_TEXT), make_chunk(1, RATE_TEXT)]
    embedder = FakeEmbedder(
        {
            NOISE_TEXT: [0.0, 1.0, 0.0],
            RATE_TEXT: [1.0, 0.0, 0.0],
            "անվանական տոկոսադրույք": [1.0, 0.0, 0.0],
        }
    )
    retriever = Retriever(
        chunks,
        vectors=embedder.embed_documents([chunk.text for chunk in chunks]),
        embed_query=embedder.embed_query,
    )
    result = retriever.search_field(get_field("nominal_rate"))
    assert result.degraded is False
    assert result.best is not None
    assert result.best.semantic_rank is not None


def test_similarity_alone_cannot_declare_a_field_present() -> None:
    """Similarity cannot decide that a field exists.

    Measured on the real corpus: an absent field reached a higher cosine than
    two fields the page states, so no floor separates them. Embeddings rank;
    only a lexical hit decides that an answer exists.
    """
    chunks = [make_chunk(0, NOISE_TEXT)]
    embedder = FakeEmbedder(
        {NOISE_TEXT: [1.0, 0.0, 0.0], "հայտի ուսումնասիրության վճար": [1.0, 0.0, 0.0]}
    )
    retriever = Retriever(
        chunks,
        vectors=embedder.embed_documents([NOISE_TEXT]),
        embed_query=embedder.embed_query,
    )
    result = retriever.search_field(get_field("application_fee"))
    assert result.best is not None
    assert result.best.similarity == pytest.approx(1.0)
    assert not result.is_relevant


def test_retrieval_explains_itself() -> None:
    """A reviewer has to be able to see why a chunk was chosen."""
    chunks = [make_chunk(0, SALARY_TEXT)]
    result = Retriever(chunks).search_field(get_field("salary_privileges"))
    assert result.best is not None
    explanation = result.best.explain()
    assert "contains query term" in explanation
    assert explanation != "no signal"
    assert "salary_privileges" in result.reason


def test_an_empty_corpus_returns_nothing_rather_than_failing() -> None:
    """A product whose only source failed to download must not crash retrieval."""
    result = Retriever([]).search_field(get_field("nominal_rate"))
    assert result.primary == []
    assert not result.is_relevant


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #


def test_an_index_round_trips(tmp_path: Path) -> None:
    """Vectors survive base64 float32 encoding, and so does the metadata."""
    chunks = [make_chunk(0, RATE_TEXT), make_chunk(1, TERM_TEXT)]
    embedder = FakeEmbedder({RATE_TEXT: [0.1, 0.2, 0.3], TERM_TEXT: [0.4, 0.5, 0.6]})
    built = build_document_index("f" * 64, chunks, embedder=embedder, directory=tmp_path)
    loaded = load_index(tmp_path, "f" * 64, "fake-embedder")
    assert loaded is not None
    assert [chunk.text for chunk in loaded.chunks] == [RATE_TEXT, TERM_TEXT]
    assert loaded.vectors[0] == pytest.approx(built.vectors[0], abs=1e-6)
    assert loaded.chunks[0].section == "Տոկոսադրույք"


def test_an_unchanged_document_is_never_embedded_twice(tmp_path: Path) -> None:
    """Content-addressed identity exists precisely to make a re-run cheap."""
    chunks = [make_chunk(0, RATE_TEXT)]
    embedder = FakeEmbedder({RATE_TEXT: [1.0, 0.0, 0.0]})
    build_document_index("f" * 64, chunks, embedder=embedder, directory=tmp_path)
    assert embedder.document_calls == 1
    build_document_index("f" * 64, chunks, embedder=embedder, directory=tmp_path)
    assert embedder.document_calls == 1, "the second build must reuse the stored index"


def test_a_different_embedder_does_not_reuse_the_index(tmp_path: Path) -> None:
    """Vectors from another model are not comparable and must not be reused."""
    chunks = [make_chunk(0, RATE_TEXT)]
    build_document_index(
        "f" * 64, chunks, embedder=FakeEmbedder({RATE_TEXT: [1.0, 0.0, 0.0]}), directory=tmp_path
    )
    assert load_index(tmp_path, "f" * 64, "gemini-embedding-001") is None


def test_a_version_mismatch_forces_a_rebuild(tmp_path: Path) -> None:
    """Stale chunking with fresh vectors would corrupt every answer."""
    import json

    chunks = [make_chunk(0, RATE_TEXT)]
    build_document_index(
        "f" * 64, chunks, embedder=FakeEmbedder({RATE_TEXT: [1.0, 0.0, 0.0]}), directory=tmp_path
    )
    path = index_path(tmp_path, "f" * 64, "fake-embedder")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = INDEX_VERSION + 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_index(tmp_path, "f" * 64, "fake-embedder") is None


def test_a_corrupt_index_is_treated_as_absent(tmp_path: Path) -> None:
    """A rebuild costs one document; a mistrusted index costs every answer."""
    path = index_path(tmp_path, "f" * 64, "fake-embedder")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert load_index(tmp_path, "f" * 64, "fake-embedder") is None


def test_embedding_failure_degrades_to_lexical_instead_of_failing(tmp_path: Path) -> None:
    """A run that BM25 can still answer must not be lost to a quota error."""
    chunks = [make_chunk(0, RATE_TEXT)]
    index = build_document_index(
        "f" * 64, chunks, embedder=FakeEmbedder({}, fail=True), directory=tmp_path
    )
    assert index.embedder == BM25_ONLY
    assert index.vectors == []
    assert index.chunks == chunks


def test_an_unwritable_index_directory_is_reported(tmp_path: Path) -> None:
    """Storage problems are named, not swallowed into a silent cache miss."""
    from tariff_agent.rag.index import DocumentIndex

    blocked = tmp_path / "file"
    blocked.write_text("not a directory", encoding="utf-8")
    index = DocumentIndex(
        doc_id="f" * 64, embedder="fake-embedder", version=INDEX_VERSION, chunks=[], vectors=[]
    )
    with pytest.raises(KnowledgeIndexError):
        save_index(index, blocked / "sub")


# --------------------------------------------------------------------------- #
# The gate distinguishes a mention from a statement
# --------------------------------------------------------------------------- #

DISCLAIMER = (
    "ՓԱՍՏԱՑԻ ՏՈԿՈՍԱԴՐՈՒՅՔԸ ՑՈՒՅՑ Է ՏԱԼԻՍ, ԹԵ ՈՐՔԱՆ ԿԱՐԺԵՆԱ ՎԱՐԿԸ ՁԵԶ ՀԱՄԱՐ "
    "ՎԱՐԿԻ ՏՐԱՄԱԴՐՄԱՆ և ՍՊԱՍԱՐԿՄԱՆ ԳԾՈՎ ԲՈԼՈՐ ՊԱՐՏԱԴԻՐ ՎՃԱՐՆԵՐԸ ԿԱՏԱՐԵԼՈՒ ԴԵՊՔՈՒՄ"
)
STATED_FEE = "Սպասարկման վճար՝ ամսական 0.5% վարկի մնացորդից"


def test_a_mention_without_a_value_does_not_pass_the_gate() -> None:
    """Taken verbatim from the consumer page.

    That sentence discusses service fees and states none. A gate that asks only
    whether the field is mentioned passed on it, which would have sent the
    extractor to a passage with nothing to extract.
    """
    # A realistic corpus: with a single chunk, BM25's IDF scores every term at
    # zero and nothing is retrieved at all, which tests a different thing.
    corpus = [make_chunk(index, f"{NOISE_TEXT} {index}") for index in range(5)]
    result = Retriever([*corpus, make_chunk(9, DISCLAIMER)]).search_field(
        get_field("service_fee")
    )
    assert not result.is_relevant
    assert "mentioned" in result.reason
    assert "NOT_FOUND" in result.reason


def test_a_mention_with_a_value_does_pass_the_gate() -> None:
    """The same field, in a chunk that actually states a fee."""
    corpus = [make_chunk(index, f"{NOISE_TEXT} {index}") for index in range(5)]
    result = Retriever([*corpus, make_chunk(9, STATED_FEE)]).search_field(
        get_field("service_fee")
    )
    assert result.is_relevant
    assert result.best is not None
    assert result.best.has_term and result.best.has_value


def test_uppercase_armenian_is_matched() -> None:
    """ACBA writes whole paragraphs in capitals; matching must be case-folded."""
    upper = "ՍՊԱՍԱՐԿՄԱՆ ՎՃԱՐ՝ ԱՄՍԱԿԱՆ 0.5%"
    corpus = [make_chunk(index, f"{NOISE_TEXT} {index}") for index in range(5)]
    result = Retriever([*corpus, make_chunk(9, upper)]).search_field(get_field("service_fee"))
    assert result.is_relevant


def test_a_value_stating_chunk_is_always_returned() -> None:
    """The gate judges what retrieval returns, so ranking must not decide it.

    Semantic ranking once pushed the only chunk stating an application fee out
    of the top four, and the field flipped to NOT_FOUND with no lexical fact
    having changed. One chunk that both mentions the field and states a value
    of its kind is now guaranteed a place.
    """
    noise = [make_chunk(index, f"{NOISE_TEXT} {index}") for index in range(6)]
    stating = make_chunk(99, STATED_FEE)
    retriever = Retriever([*noise, stating])
    result = retriever.search_field(get_field("service_fee"), k=2)
    assert any(chunk.chunk.chunk_id == "c099" for chunk in result.primary)
    assert result.is_relevant


def test_an_index_is_not_reused_when_the_chunk_text_changed(tmp_path: Path) -> None:
    """Chunk count is not chunk identity.

    A change to how documents are chunked can produce the same number of chunks
    with different text. Reusing vectors across that would answer questions
    about text the index no longer holds - and the same gap once let a cached
    index return HTML chunks labelled as PDF, whose evidence then claimed page
    numbers the source does not have.
    """
    original = [make_chunk(0, RATE_TEXT)]
    embedder = FakeEmbedder({RATE_TEXT: [1.0, 0.0, 0.0], TERM_TEXT: [0.0, 1.0, 0.0]})
    build_document_index("f" * 64, original, embedder=embedder, directory=tmp_path)
    assert embedder.document_calls == 1

    rechunked = [make_chunk(0, TERM_TEXT)]
    rebuilt = build_document_index("f" * 64, rechunked, embedder=embedder, directory=tmp_path)
    assert embedder.document_calls == 2, "changed text must be re-embedded"
    assert rebuilt.chunks[0].text == TERM_TEXT


def test_the_chunk_schema_cannot_change_without_bumping_the_index_version() -> None:
    """A chunk field added without a version bump serves stale data silently.

    That happened: ``document_kind`` was added, cached indexes kept loading and
    defaulted HTML chunks to PDF, and their evidence would have cited page
    numbers the source does not have. This test pins the schema to the version,
    so adding a field fails here until INDEX_VERSION is raised - which is the
    moment to think about what the stored entries now mean.
    """
    import dataclasses

    fields = tuple(sorted(field.name for field in dataclasses.fields(Chunk)))
    expected_by_version = {
        2: (
            "char_end",
            "char_start",
            "chunk_id",
            "chunk_type",
            "doc_id",
            "document_date",
            "document_kind",
            "document_name",
            "language",
            "page",
            "retrieved_at",
            "section",
            "source_role",
            "source_url",
            "text",
        ),
    }
    assert INDEX_VERSION in expected_by_version, (
        f"Chunk schema or index format changed: INDEX_VERSION is {INDEX_VERSION} but this test "
        "only knows how to check the versions listed here. Add the new field list, and decide "
        "what happens to indexes written under the previous version."
    )
    assert fields == expected_by_version[INDEX_VERSION], (
        "Chunk fields changed without bumping INDEX_VERSION. A cached index written under the "
        f"current version {INDEX_VERSION} would be loaded with missing or defaulted fields."
    )

"""One entry point: documents in, a searchable knowledge layer out.

Keeps the assembly order in a single place - chunk, index (reusing anything
unchanged), then search - so the agent tools in Phase 8 and the deterministic
pipeline call exactly the same code path.
"""

from __future__ import annotations

from dataclasses import dataclass

from tariff_agent.config import RagSettings
from tariff_agent.documents.document import Document
from tariff_agent.observability.logging import get_logger
from tariff_agent.rag.chunking import Chunk, SourceRole, chunk_document
from tariff_agent.rag.embeddings import Embedder
from tariff_agent.rag.index import build_document_index
from tariff_agent.rag.retrieval import Retriever

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """A processed document together with the role it plays for a product.

    Attributes:
        document: The processed document.
        role: Whether it is the product's primary source.
    """

    document: Document
    role: SourceRole


def build_knowledge(
    sources: list[SourceDocument],
    settings: RagSettings,
    *,
    embedder: Embedder | None = None,
) -> Retriever:
    """Chunk and index a product's documents, and return a retriever over them.

    Args:
        sources: The product's documents, each with its role.
        settings: Chunking and retrieval settings.
        embedder: Embedding model, or None to retrieve lexically only.

    Returns:
        A retriever. Vector coverage may be partial - a document whose
        embedding failed contributes none, and its chunks then compete
        lexically rather than disabling semantic ranking for the whole product.
    """
    chunks: list[Chunk] = []
    vectors: list[list[float]] = []
    dimension = 0

    for source in sources:
        document_chunks = chunk_document(
            source.document,
            source_role=source.role,
            max_chars=settings.chunk_chars,
            overlap=settings.chunk_overlap,
        )
        # The role is part of the identity: the same tariff PDF supports one
        # product and is primary for none, and its chunks differ accordingly.
        index = build_document_index(
            f"{source.document.doc_id}-{source.role.value}",
            document_chunks,
            embedder=embedder,
            directory=settings.index_dir,
        )
        chunks.extend(index.chunks)
        if index.vectors:
            dimension = dimension or len(index.vectors[0])
            vectors.extend(index.vectors)
        else:
            vectors.extend([] if dimension == 0 else [[0.0] * dimension] * len(index.chunks))

    usable = len(vectors) == len(chunks) and any(any(vector) for vector in vectors)
    if vectors and not usable:
        # Zero-padding only works when at least one document was embedded and
        # the dimension is known; otherwise fall back cleanly.
        vectors = []

    logger.info(
        "knowledge_built",
        extra={
            "documents": len(sources),
            "chunks": len(chunks),
            "embedded_chunks": sum(1 for vector in vectors if any(vector)),
            "degraded": not usable,
        },
    )
    return Retriever(
        chunks,
        vectors=vectors if usable else None,
        embed_query=embedder.embed_query if (usable and embedder) else None,
        similarity_floor=settings.similarity_floor,
    )

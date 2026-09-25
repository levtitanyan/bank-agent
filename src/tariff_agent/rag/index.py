"""Building, persisting and reloading a product's knowledge index.

The index is keyed by **content hash, embedder name and index version**. That
combination is what makes re-running cheap and correct: a document whose bytes
have not changed is never re-embedded, a change of embedding model invalidates
everything that depended on it, and a change to chunking invalidates the chunks
it produced without silently keeping vectors that belong to different text.

Persistence is JSON metadata plus base64 float32 vectors. Plain JSON floats are
roughly five times larger and no more readable in a diff; a binary array format
would be smaller still but unreadable by anyone reviewing what was stored.
"""

from __future__ import annotations

import base64
import json
import struct
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from tariff_agent.documents.document import DocumentKind
from tariff_agent.errors import EmbeddingError, KnowledgeIndexError
from tariff_agent.models import Language
from tariff_agent.observability.logging import get_logger
from tariff_agent.rag.chunking import Chunk, ChunkType, SourceRole
from tariff_agent.rag.embeddings import Embedder

logger = get_logger(__name__)

INDEX_VERSION = 2
"""Bumped when chunking or serialization changes in a way that invalidates
stored entries.

Version 2 added ``document_kind`` and ``document_date`` to chunks. Without the
bump, a cached version-1 index would load HTML chunks defaulted to ``pdf``, and
their evidence would cite a page number that the source does not have.
"""

BM25_ONLY = "bm25-only"
"""Embedder name recorded when an index holds no vectors."""


def _pack(vector: list[float]) -> str:
    """Encode a vector as base64 float32.

    Args:
        vector: The vector.

    Returns:
        The encoded string.
    """
    return base64.b64encode(struct.pack(f"{len(vector)}f", *vector)).decode("ascii")


def _unpack(encoded: str) -> list[float]:
    """Decode a base64 float32 vector.

    Args:
        encoded: The encoded string.

    Returns:
        The vector.
    """
    raw = base64.b64decode(encoded)
    return list(struct.unpack(f"{len(raw) // 4}f", raw))


@dataclass(frozen=True, slots=True)
class DocumentIndex:
    """The indexed form of one document.

    Attributes:
        doc_id: Content hash of the document.
        embedder: Name of the embedding model, or ``bm25-only``.
        version: Index format version.
        chunks: The document's chunks.
        vectors: One vector per chunk, or an empty list when none were made.
    """

    doc_id: str
    embedder: str
    version: int
    chunks: list[Chunk]
    vectors: list[list[float]]

    @property
    def has_vectors(self) -> bool:
        """Whether this document was embedded."""
        return bool(self.vectors)


def index_path(directory: Path, doc_id: str, embedder: str) -> Path:
    """Return where one document's index is stored.

    Args:
        directory: The index directory.
        doc_id: Content hash of the document.
        embedder: Embedding model name.

    Returns:
        The file path.
    """
    safe = embedder.replace("/", "_")
    return directory / f"{doc_id[:16]}.{safe}.json"


def save_index(index: DocumentIndex, directory: Path) -> Path:
    """Write a document index to disk.

    Args:
        index: The index to store.
        directory: Where to store it.

    Returns:
        The path written.

    Raises:
        KnowledgeIndexError: If the index cannot be written.
    """
    path = index_path(directory, index.doc_id, index.embedder)
    payload = {
        "doc_id": index.doc_id,
        "embedder": index.embedder,
        "version": index.version,
        "chunks": [_chunk_to_json(chunk) for chunk in index.chunks],
        "vectors": [_pack(vector) for vector in index.vectors],
    }
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        raise KnowledgeIndexError(f"could not write the index to {path}: {exc}") from exc
    logger.info(
        "index_saved",
        extra={
            "doc_id": index.doc_id[:12],
            "chunks": len(index.chunks),
            "vectors": len(index.vectors),
            "embedder": index.embedder,
            "bytes": path.stat().st_size,
        },
    )
    return path


def load_index(directory: Path, doc_id: str, embedder: str) -> DocumentIndex | None:
    """Read a stored index, if one is usable.

    Args:
        directory: The index directory.
        doc_id: Content hash of the document.
        embedder: Embedding model name.

    Returns:
        The index, or ``None`` when it is absent, corrupt, or written by a
        different format version. A rebuild costs one document's embeddings;
        using a mismatched index would corrupt every answer it produced.
    """
    path = index_path(directory, doc_id, embedder)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != INDEX_VERSION:
        logger.info("index_version_mismatch", extra={"doc_id": doc_id[:12], "path": str(path)})
        return None
    if payload.get("embedder") != embedder or payload.get("doc_id") != doc_id:
        return None
    try:
        chunks = [_chunk_from_json(item) for item in payload["chunks"]]
        vectors = [_unpack(item) for item in payload.get("vectors", [])]
    except (KeyError, TypeError, ValueError, struct.error):
        logger.warning("index_unreadable", extra={"path": str(path)})
        return None
    return DocumentIndex(
        doc_id=doc_id,
        embedder=embedder,
        version=INDEX_VERSION,
        chunks=chunks,
        vectors=vectors,
    )


def build_document_index(
    doc_id: str,
    chunks: list[Chunk],
    *,
    embedder: Embedder | None,
    directory: Path | None = None,
) -> DocumentIndex:
    """Index one document, reusing stored vectors when nothing has changed.

    Args:
        doc_id: Content hash of the document.
        chunks: The document's chunks.
        embedder: The embedding model, or None for lexical-only.
        directory: Where indexes are cached; None disables persistence.

    Returns:
        The index. When embedding fails, a lexical-only index is returned and
        the failure is logged - a degraded answer beats no answer, and the
        caller reports the degradation.
    """
    name = embedder.name if embedder else BM25_ONLY
    if directory is not None:
        cached = load_index(directory, doc_id, name)
        # Length alone is not identity: a chunk schema change keeps the count
        # and alters the content, which is what INDEX_VERSION guards. The text
        # check catches a re-chunking that happens to produce the same number.
        if (
            cached is not None
            and len(cached.chunks) == len(chunks)
            and all(old.text == new.text for old, new in zip(cached.chunks, chunks, strict=True))
        ):
            logger.info(
                "index_reused",
                extra={"doc_id": doc_id[:12], "chunks": len(cached.chunks), "embedder": name},
            )
            return cached

    vectors: list[list[float]] = []
    if embedder is not None:
        try:
            vectors = embedder.embed_documents([chunk.text for chunk in chunks])
        except EmbeddingError as exc:
            logger.warning(
                "embedding_failed_degrading_to_lexical",
                extra={"doc_id": doc_id[:12], "error": str(exc)[:200]},
            )
            name = BM25_ONLY
            vectors = []

    index = DocumentIndex(
        doc_id=doc_id, embedder=name, version=INDEX_VERSION, chunks=chunks, vectors=vectors
    )
    if directory is not None:
        save_index(index, directory)
    return index


def _chunk_to_json(chunk: Chunk) -> dict[str, Any]:
    """Serialize a chunk.

    Args:
        chunk: The chunk.

    Returns:
        A JSON-compatible mapping carrying the §5.5 metadata.
    """
    return {
        "chunk_id": chunk.chunk_id,
        "doc_id": chunk.doc_id,
        "text": chunk.text,
        "page": chunk.page,
        "chunk_type": chunk.chunk_type.value,
        "document_kind": chunk.document_kind.value,
        "source_role": chunk.source_role.value,
        "document_name": chunk.document_name,
        "source_url": chunk.source_url,
        "language": chunk.language.value,
        "retrieved_at": chunk.retrieved_at.isoformat(),
        "document_date": chunk.document_date.isoformat() if chunk.document_date else None,
        "section": chunk.section,
        "char_start": chunk.char_start,
        "char_end": chunk.char_end,
    }


def _chunk_from_json(payload: dict[str, Any]) -> Chunk:
    """Deserialize a chunk.

    Args:
        payload: The stored mapping.

    Returns:
        The chunk.
    """
    return Chunk(
        chunk_id=payload["chunk_id"],
        doc_id=payload["doc_id"],
        text=payload["text"],
        page=payload["page"],
        chunk_type=ChunkType(payload["chunk_type"]),
        document_kind=DocumentKind(payload.get("document_kind", "pdf")),
        source_role=SourceRole(payload["source_role"]),
        document_name=payload["document_name"],
        source_url=payload["source_url"],
        language=Language(payload["language"]),
        retrieved_at=datetime.fromisoformat(payload["retrieved_at"]),
        document_date=(
            date.fromisoformat(payload["document_date"]) if payload.get("document_date") else None
        ),
        section=payload.get("section"),
        char_start=payload.get("char_start"),
        char_end=payload.get("char_end"),
    )

"""The deterministic run: one product, start to finish, no model deciding anything.

This is the scheduled path. A cron job calls :func:`run_product` for each
monitored product and nothing chooses an order, skips a step or asks a question
that was not configured. The agent in :mod:`tariff_agent.agent` is a different
entry point over the same parts, and both go through :func:`load_sources` so
that "what counts as this product's documents" has exactly one answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field

from tariff_agent.config import (
    Allowlist,
    DiscoveryConfig,
    MonitoringConfig,
    Product,
    Settings,
)
from tariff_agent.discovery.sources import SourceKind, discover_product_sources
from tariff_agent.documents.document import Document
from tariff_agent.documents.processing import process_document
from tariff_agent.extraction.extractor import Extractor
from tariff_agent.http.client import ContentKind, SafeHttpClient
from tariff_agent.observability.logging import get_logger
from tariff_agent.rag.chunking import SourceRole
from tariff_agent.rag.embeddings import Embedder
from tariff_agent.rag.knowledge import SourceDocument, build_knowledge
from tariff_agent.rag.retrieval import Retriever
from tariff_agent.snapshots.pipeline import MonitoringResult, run_monitoring
from tariff_agent.snapshots.review import Reviewer
from tariff_agent.snapshots.store import SnapshotStore

logger = get_logger(__name__)

MAX_SUPPORTING = 2
"""Supporting documents read beyond the primary one.

Each costs a download, a parse and a share of every prompt. Two is what the
measured products need: a product page plus the shared tariff book.
"""


@dataclass
class LoadedSources:
    """A product's documents, fetched, parsed and indexed.

    Attributes:
        product_id: The product.
        retriever: The index over every document read.
        primary_document: The document supplying the report's dates.
        doc_id: Content hash of the primary document.
        primary_title: What the primary source is called.
        supporting_titles: What the supporting sources are called.
        requires_review: True when two documents were equally plausible.
        unreadable: Sources that failed to download or parse, by name and cause.
        pages_fetched: How many pages discovery actually retrieved.
        notes: Anything discovery wants a reviewer to know.
    """

    product_id: str
    retriever: Retriever
    primary_document: Document | None
    doc_id: str
    primary_title: str
    supporting_titles: list[str] = dataclass_field(default_factory=list)
    requires_review: bool = False
    unreadable: list[str] = dataclass_field(default_factory=list)
    pages_fetched: int = 0
    notes: list[str] = dataclass_field(default_factory=list)


def load_sources(
    client: SafeHttpClient,
    product: Product,
    discovery_config: DiscoveryConfig,
    settings: Settings,
    *,
    embedder: Embedder | None = None,
) -> LoadedSources:
    """Discover, fetch, parse and index a product's official documents.

    A source that cannot be downloaded or parsed is recorded and skipped rather
    than ending the run: one broken PDF must not cost the other three.

    Args:
        client: The guarded HTTP client.
        product: The product to read.
        discovery_config: Source-ranking weights and limits.
        settings: Parsing and retrieval settings.
        embedder: Embeddings, or None for BM25 only.

    Returns:
        The indexed sources.

    Raises:
        SourceNotFoundError: When discovery found nothing above the floor.
        DocumentError: When every discovered source failed to be read.
    """
    from tariff_agent.errors import DocumentError

    discovery = discover_product_sources(client, product, discovery_config, sitemap_urls=None)

    documents: list[SourceDocument] = []
    primary_document: Document | None = None
    doc_id = ""
    unreadable: list[str] = []

    for candidate in discovery.all_sources[: 1 + MAX_SUPPORTING]:
        expect = ContentKind.PDF if candidate.kind is SourceKind.PDF else ContentKind.HTML
        role = (
            SourceRole.PRIMARY if candidate.url == discovery.primary.url else SourceRole.SUPPORTING
        )
        try:
            fetched = client.fetch(candidate.url, expect=expect)
            document = process_document(fetched, settings.documents)
        except Exception as exc:  # noqa: BLE001 - one bad source must not end the run
            logger.warning(
                "source_unreadable",
                extra={
                    "product_id": product.id,
                    "role": role.value,
                    "error_type": type(exc).__name__,
                },
            )
            unreadable.append(f"{candidate.title or candidate.kind.value}: {type(exc).__name__}")
            continue
        documents.append(SourceDocument(document, role))
        if role is SourceRole.PRIMARY:
            primary_document = document
            doc_id = fetched.sha256

    if not documents:
        raise DocumentError(f"every source discovered for {product.id} failed to download or parse")

    return LoadedSources(
        product_id=product.id,
        retriever=build_knowledge(documents, settings.rag, embedder=embedder),
        primary_document=primary_document,
        doc_id=doc_id,
        primary_title=discovery.primary.title,
        supporting_titles=[candidate.title for candidate in discovery.supporting],
        requires_review=discovery.requires_review,
        unreadable=unreadable,
        pages_fetched=discovery.pages_fetched,
        notes=list(discovery.notes),
    )


def run_product(
    product: Product,
    bank: str,
    client: SafeHttpClient,
    extractor: Extractor,
    store: SnapshotStore,
    *,
    settings: Settings,
    allowlist: Allowlist,
    discovery_config: DiscoveryConfig,
    monitoring: MonitoringConfig,
    embedder: Embedder | None = None,
    reviewer: Reviewer | None = None,
) -> MonitoringResult:
    """Run one full monitoring cycle for one product.

    Args:
        product: The product to monitor.
        bank: Bank name, recorded on the snapshot.
        client: The guarded HTTP client.
        extractor: The extraction backend.
        store: Where snapshots and decisions are kept.
        settings: Parsing and retrieval settings.
        allowlist: Domains evidence may come from.
        discovery_config: Source-ranking weights and limits.
        monitoring: Thresholds and review policy.
        embedder: Embeddings, or None for BM25 only.
        reviewer: Who to ask when something needs a human.

    Returns:
        The result, including the rendered report.
    """
    loaded = load_sources(client, product, discovery_config, settings, embedder=embedder)
    return run_monitoring(
        product,
        bank,
        loaded.retriever,
        extractor,
        allowlist,
        store,
        monitoring=monitoring,
        reviewer=reviewer,
        doc_id=loaded.doc_id,
        top_k=settings.rag.top_k,
        primary_document=loaded.primary_document,
    )

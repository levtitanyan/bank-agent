"""Shared plumbing for the demos: an offline bank, and a claim that can fail.

Every demo runs offline by default, against trimmed copies of real ACBA pages
and a small Armenian PDF, served through ``httpx.MockTransport``. There is no
cached-download fixture to ship, because a cache rots quietly: the demo would
keep passing against bytes the bank replaced months ago.

The second thing here is :func:`check`. A demo that prints something plausible
while its claim has stopped being true is worse than no demo, so each claim is
asserted and the script exits non-zero when one fails.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from tariff_agent.config import (
    Allowlist,
    DiscoveryConfig,
    HttpSettings,
    MonitoringConfig,
    ProductCatalog,
    Settings,
    get_settings,
    load_allowlist,
    load_discovery_config,
    load_monitoring_config,
    load_products,
)
from tariff_agent.extraction.extractor import Extractor, GeminiExtractor, RuleBasedExtractor
from tariff_agent.http.client import SafeHttpClient
from tariff_agent.http.robots import build_client
from tariff_agent.observability.logging import configure_logging
from tariff_agent.rag.embeddings import Embedder, build_embedder

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "discovery"
DOCUMENTS = ROOT / "tests" / "fixtures" / "documents"

CONSUMER_PAGE = "https://acba.am/hy/individual/loan/consumer-loan--up-to-10mln"
CONSUMER_CATEGORY = "https://acba.am/hy/individual/loans/consumer-loans"
MORTGAGE_PAGE = "https://acba.am/hy/individual/loan/purchase-mortgage"
TARIFF_PDF = "https://www.acba.am/files/loans-tariffs.pdf"
# The same document, as the product page actually links it. ACBA serves this
# PDF from three URLs; content-addressing collapses them, but the fixture
# server has to answer whichever one discovery followed.
TARIFF_PDF_LINKED = "https://acba.am/media/uploaded/loans-tariffs.pdf"

PAGES = {
    CONSUMER_PAGE: FIXTURES / "consumer_loan_10mln_page.html",
    CONSUMER_CATEGORY: FIXTURES / "consumer_loans_page.html",
    MORTGAGE_PAGE: FIXTURES / "purchase_mortgage_page.html",
}
PDFS = {
    TARIFF_PDF: DOCUMENTS / "tariff_summary.pdf",
    TARIFF_PDF_LINKED: DOCUMENTS / "tariff_summary.pdf",
}


BOLD, DIM, GREEN, RED, OFF = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[0m"


@dataclass
class Demo:
    """One demo's output and the claims it makes.

    Attributes:
        title: What the demo shows.
        failures: Claims that did not hold.
    """

    title: str
    failures: list[str] = field(default_factory=list)

    def heading(self, text: str) -> None:
        """Print a section heading.

        Args:
            text: The heading.
        """
        print(f"\n{BOLD}{text}{OFF}")
        print("─" * min(len(text), 78))

    def note(self, text: str) -> None:
        """Print an explanatory line.

        Args:
            text: The note.
        """
        print(f"{DIM}{text}{OFF}")

    def check(self, claim: str, held: bool) -> bool:
        """Assert one claim and record it.

        Args:
            claim: What is being claimed, in words a reviewer can judge.
            held: Whether it held.

        Returns:
            ``held``, so a caller can branch on it.
        """
        mark = f"{GREEN}✓{OFF}" if held else f"{RED}✗{OFF}"
        print(f"  {mark} {claim}")
        if not held:
            self.failures.append(claim)
        return held

    def finish(self) -> None:
        """Print the tally and exit non-zero if any claim failed."""
        print()
        if self.failures:
            print(f"{RED}{self.title}: {len(self.failures)} claim(s) failed{OFF}")
            for failure in self.failures:
                print(f"  - {failure}")
            sys.exit(1)
        print(f"{GREEN}{self.title}: every claim held{OFF}")


def start(title: str) -> Demo:
    """Begin a demo.

    Args:
        title: What it shows.

    Returns:
        The demo recorder.
    """
    configure_logging("ERROR")
    print(f"\n{BOLD}{'=' * 78}{OFF}")
    print(f"{BOLD}{title}{OFF}")
    print(f"{BOLD}{'=' * 78}{OFF}")
    return Demo(title)


def is_live() -> bool:
    """Whether the demo was asked to use the real site.

    Returns:
        True when ``--live`` was passed.
    """
    return "--live" in sys.argv


def offline_client(*, serve_pdfs: bool = True) -> SafeHttpClient:
    """Build a client that serves the fixtures and 404s everything else.

    Args:
        serve_pdfs: False makes every PDF 404, for demonstrating a failure.

    Returns:
        The client.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if serve_pdfs and url in PDFS:
            return httpx.Response(
                200,
                content=PDFS[url].read_bytes(),
                headers={"content-type": "application/pdf"},
            )
        page = PAGES.get(url)
        if page is None:
            return httpx.Response(404)
        return httpx.Response(
            200,
            content=page.read_bytes(),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    return SafeHttpClient(
        load_allowlist(),
        HttpSettings(respect_robots=False, max_attempts=1, cache_dir=ROOT / "data" / "cache"),
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )


@dataclass
class Parts:
    """Everything the demos build things from.

    Attributes:
        settings: Process configuration.
        catalog: The monitored products.
        allowlist: Domains that may be fetched.
        discovery: Source-ranking weights.
        monitoring: Thresholds and review policy.
        client: The HTTP client, real or mocked.
        extractor: The extraction backend.
        embedder: Embeddings when a key is configured and this is a live run,
            else None. Offline runs are BM25-only so that they need no key and
            stay reproducible.
        live: Whether this is running against the real site.
    """

    settings: Settings
    catalog: ProductCatalog
    allowlist: Allowlist
    discovery: DiscoveryConfig
    monitoring: MonitoringConfig
    client: SafeHttpClient
    extractor: Extractor
    embedder: Embedder | None
    live: bool


def parts(*, serve_pdfs: bool = True) -> Parts:
    """Assemble the pieces, offline unless ``--live`` was passed.

    Args:
        serve_pdfs: Passed through to :func:`offline_client`.

    Returns:
        The assembled parts. Offline runs use the rule-based extractor, which
        is plainly worse at reading prose than a model and is repeatable, needs
        no key, and exercises every stage after it.
    """
    settings = get_settings()
    live = is_live()
    allowlist = load_allowlist()
    embedder: Embedder | None = None
    if live:
        client = build_client(allowlist, settings.http)
        extractor: Extractor = (
            GeminiExtractor(
                settings.google_api_key.get_secret_value()  # type: ignore[union-attr]
                if settings.google_api_key
                else "",
                model=settings.gemini_model,
            )
            if settings.has_api_key
            else RuleBasedExtractor()
        )
        # A live run must measure what actually ships, which is hybrid
        # retrieval. Measuring BM25-only here and reporting it as the live
        # result would describe a configuration nobody runs.
        embedder = build_embedder(
            settings.google_api_key.get_secret_value()  # type: ignore[union-attr]
            if settings.has_api_key and settings.google_api_key
            else None
        )
    else:
        client = offline_client(serve_pdfs=serve_pdfs)
        extractor = RuleBasedExtractor()
    return Parts(
        settings=settings,
        catalog=load_products(),
        allowlist=allowlist,
        discovery=load_discovery_config(),
        monitoring=load_monitoring_config(),
        client=client,
        extractor=extractor,
        embedder=embedder,
        live=live,
    )

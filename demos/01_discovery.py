#!/usr/bin/env python
"""Demo 1 — a fuzzy product name becomes a ranked set of official documents.

Shows the part of the system that decides *what to read*, and that the decision
is deterministic: fuzzy matching against a fixed catalogue, then scoring with
weights a reviewer can read in `config/discovery.yaml`. No model is involved.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _offline import parts, start  # noqa: E402

from tariff_agent.discovery.product_matcher import ResolutionStatus, resolve_product  # noqa: E402
from tariff_agent.discovery.sources import discover_product_sources  # noqa: E402


def main() -> None:
    """Resolve three spellings, then discover one product's sources."""
    demo = start("Demo 1 — finding the official documents")
    p = parts()

    demo.heading("Three languages, one product")
    demo.note("Synonyms and rapidfuzz, never a model: the same query must always resolve the same.")
    for query in ("потребительский кредит", "consumer loan", "սպառողական վարկ", "consumr laon"):
        resolution = resolve_product(query, p.catalog)
        best = resolution.best
        shown = (
            f"{best.product_id} ({best.score:.0f}/100 via «{best.matched_name}»)" if best else "—"
        )
        print(f"  {query:28s} → {shown}")
        demo.check(f"{query!r} resolves to consumer_loan", resolution.product_id == "consumer_loan")

    demo.heading("A product we do not monitor is refused, not guessed")
    demo.note('"business mortgage" literally contains "mortgage" and scores 100 against it.')
    unsupported = resolve_product("business mortgage", p.catalog)
    print(f"  status: {unsupported.status.value} — {unsupported.reason}")
    demo.check(
        "'business mortgage' does not resolve", unsupported.status is not ResolutionStatus.RESOLVED
    )

    demo.heading("Ranked sources, with the reason for every point")
    product = p.catalog.get("consumer_loan")
    assert product is not None
    result = discover_product_sources(p.client, product, p.discovery, sitemap_urls=None)
    primary = result.primary
    print(f"  PRIMARY  [{primary.score:5.1f}] {primary.kind.value:4s} {primary.title[:52]}")
    for reason in result.primary.reasons[:4]:
        print(f"           {reason}")
    for candidate in result.supporting:
        print(
            f"  SUPPORT  [{candidate.score:5.1f}] {candidate.kind.value:4s} {candidate.title[:52]}"
        )

    demo.check("a primary source was chosen", bool(result.primary.url))
    demo.check("the ranking explains itself", bool(result.primary.reasons))
    demo.check(
        "nothing outside acba.am was selected", all("acba.am" in c.url for c in result.all_sources)
    )
    demo.finish()


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""Demo 4 — every value carries a quote, and an unverifiable quote is refused.

This is the promise the project rests on. The model returns a value, a verbatim
quote and the id of the passage it came from; deterministic code then checks
that the quote really occurs there. Anything that cannot be verified becomes
NOT_FOUND rather than a reported tariff.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _offline import parts, start  # noqa: E402

from tariff_agent.extraction.pipeline import extract_tariffs  # noqa: E402
from tariff_agent.extraction.verify import verify_quote  # noqa: E402
from tariff_agent.fields import FIELDS_BY_ID  # noqa: E402
from tariff_agent.models import FieldStatus  # noqa: E402
from tariff_agent.pipeline import load_sources  # noqa: E402


def main() -> None:
    """Extract the consumer loan, then try to smuggle a fabricated quote past."""
    demo = start("Demo 4 — extraction, with a verified quote behind every value")
    p = parts()
    product = p.catalog.get("consumer_loan")
    assert product is not None

    loaded = load_sources(p.client, product, p.discovery, p.settings, embedder=None)
    outcome = extract_tariffs(
        product,
        p.catalog.bank,
        loaded.retriever,
        p.extractor,
        p.allowlist,
        primary_document=loaded.primary_document,
    )
    demo.note(f"extractor={outcome.extraction.extraction_method}  calls={outcome.model_calls}")

    demo.heading("Values, and where each came from")
    for field_id, value in outcome.extraction.fields.items():
        label = FIELDS_BY_ID[field_id].label_en
        if value.status is FieldStatus.FOUND and value.evidence is not None:
            print(f"  {label:22s} {value.value[:44]}")
            print(f"  {'':22s} ← «{' '.join(value.evidence.quote.split())[:58]}»")
        else:
            print(f"  {label:22s} {value.status.value}")

    demo.heading("A fabricated quote cannot become evidence")
    demo.note("The same verifier, handed a rate the documents never state:")
    chunks = [
        scored.chunk
        for scored in loaded.retriever.search_field(FIELDS_BY_ID["nominal_rate"], k=4).all_chunks
    ]
    invented = "Տարեկան անվանական տոկոսադրույք՝ 4.9%"
    check = verify_quote(invented, chunks[0].chunk_id if chunks else "x", chunks)
    print(f"  quote   : «{invented}»")
    print(f"  verified: {check.verified}")
    print(f"  reason  : {check.reason}")

    found = [v for v in outcome.extraction.fields.values() if v.status is FieldStatus.FOUND]
    demo.check("something was extracted", bool(found))
    demo.check("every found value has evidence", all(v.evidence is not None for v in found))
    demo.check(
        "every found value has a non-empty quote",
        all(v.evidence and v.evidence.quote.strip() for v in found),
    )
    demo.check(
        "every quote occurs in its cited passage",
        all(
            verify_quote(
                v.evidence.quote,
                "",
                chunks
                + [
                    s.chunk
                    for fid in outcome.extraction.fields
                    for s in loaded.retriever.search_field(FIELDS_BY_ID[fid], k=4).all_chunks
                ],
            ).verified
            for v in found
            if v.evidence
        ),
    )
    demo.check("the fabricated quote was refused", not check.verified)
    demo.finish()


if __name__ == "__main__":
    main()

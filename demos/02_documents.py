#!/usr/bin/env python
"""Demo 2 — reading a PDF, and deciding whether OCR did better than the parser.

The bank publishes scanned summaries. Two readings of every page are scored and
the better one wins, with one rule: OCR must win by a clear margin, because an
OCR win by a hair once cost a stated «չի գանձվում» that the parser had right.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _offline import TARIFF_PDF, parts, start  # noqa: E402

from tariff_agent.documents.processing import process_document  # noqa: E402
from tariff_agent.documents.strategies import OCR_MARGIN  # noqa: E402
from tariff_agent.http.client import ContentKind  # noqa: E402


def main() -> None:
    """Fetch and parse a PDF, showing the quality scores behind the choice."""
    demo = start("Demo 2 — parsing a PDF, and the OCR margin")
    p = parts()

    fetched = p.client.fetch(TARIFF_PDF, expect=ContentKind.PDF)
    document = process_document(fetched, p.settings.documents)

    demo.heading("Identity is the content, not the URL")
    demo.note("ACBA serves the same tariff PDF from three URLs; the sha256 collapses them to one.")
    print(f"  url     : {fetched.url}")
    print(f"  sha256  : {fetched.sha256[:32]}…")
    print(f"  bytes   : {fetched.size:,}")

    demo.heading("Per-page reading, scored")
    for page in document.pages[:3]:
        confidence = f" ocr_confidence={page.ocr_confidence:.0f}" if page.ocr_confidence else ""
        print(
            f"  page {page.number}: method={page.method.value:9s} quality={page.quality:.2f} "
            f"chars={len(page.text):5d}{confidence}"
        )
        for warning in page.warnings:
            print(f"           warning: {warning}")

    demo.heading("What came out")
    text = "\n".join(page.text for page in document.pages)
    for line in [ln for ln in text.splitlines() if ln.strip()][:6]:
        print(f"  {line[:70]}")

    demo.note(
        f"\nOCR only wins when it beats the parser by {OCR_MARGIN:.2f}. Measured once at 0.076,"
    )
    demo.note("OCR won and had turned «չի» into «sh» — a stated absence of a fee, unreadable.")

    demo.check("the document parsed", bool(document.pages))
    demo.check("Armenian survived the round trip", "տոկոսադրույք" in text)
    demo.check("a rate is present in the text", "20.1-21.6%" in text)
    demo.check(
        "every page carries a quality score", all(page.quality >= 0 for page in document.pages)
    )
    demo.finish()


if __name__ == "__main__":
    main()

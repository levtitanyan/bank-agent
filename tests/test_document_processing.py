"""Tests for strategy selection, PDF and HTML processing, and the façade.

The PDF fixtures are synthetic but reproduce defects observed on the real ACBA
documents: one word per line, and repeated headers, footers and page numbers.
The OCR test uses a real ACBA page rendered to an image, committed under
``data/samples/ocr/`` for the assignment's OCR demonstration.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tariff_agent.config import DocumentSettings
from tariff_agent.documents.document import Document, DocumentKind, ExtractionMethod, Table
from tariff_agent.documents.pdf import open_pdf, page_candidates, text_from_word_positions
from tariff_agent.documents.processing import process_document
from tariff_agent.documents.quality import score_text
from tariff_agent.documents.strategies import TextCandidate, choose_best_text, make_candidate
from tariff_agent.documents.tables import is_plausible_table
from tariff_agent.errors import DocumentError, PdfParseError
from tariff_agent.http.client import FetchResult

FIXTURES = Path(__file__).parent / "fixtures" / "documents"
OCR_SAMPLE = Path(__file__).parent.parent / "data" / "samples" / "ocr"
SETTINGS = DocumentSettings()


def fetch_of(path: Path, content_type: str = "application/pdf") -> FetchResult:
    """Wrap a fixture file as if it had just been downloaded."""
    content = path.read_bytes()
    now = datetime.now(UTC)
    import hashlib

    return FetchResult(
        url=f"https://acba.am/files/{path.name}",
        requested_url=f"https://acba.am/files/{path.name}",
        content=content,
        content_type=content_type,
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        retrieved_at=now,
        checked_at=now,
        from_cache=False,
    )


def html_fetch(body: str, url: str = "https://acba.am/hy/individual/loan/x") -> FetchResult:
    """Wrap an HTML string as a fetch result."""
    import hashlib

    content = body.encode("utf-8")
    now = datetime.now(UTC)
    return FetchResult(
        url=url,
        requested_url=url,
        content=content,
        content_type="text/html",
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        retrieved_at=now,
        checked_at=now,
        from_cache=False,
    )


# --------------------------------------------------------------------------- #
# Strategy selection
# --------------------------------------------------------------------------- #


def candidate(
    method: ExtractionMethod, text: str, confidence: float | None = None
) -> TextCandidate:
    """Build a scored candidate for selection tests."""
    return make_candidate(method, text, SETTINGS, ocr_confidence=confidence)


GOOD_TEXT = (
    "Անվանական տոկոսադրույք՝ 13,5% տարեկան։ Վարկի ժամկետը մինչև 60 ամիս է։\n"
    "Գումարը 300,000-ից 10,000,000 ՀՀ դրամ։ Ապահովվածությունը՝ գրավ։\n"
) * 3
BROKEN_TEXT = "\n".join(GOOD_TEXT.split())


def test_the_better_text_wins_even_when_it_is_ocr() -> None:
    """On the real mortgage summary OCR beats the parser; that is normal."""
    best = choose_best_text(
        [
            candidate(ExtractionMethod.PDF_TEXT, BROKEN_TEXT),
            candidate(ExtractionMethod.OCR, GOOD_TEXT, confidence=92.0),
        ],
        SETTINGS,
    )
    assert best is not None
    assert best.method is ExtractionMethod.OCR


def test_a_worse_ocr_reading_is_discarded() -> None:
    """OCR competes; it does not win by existing."""
    best = choose_best_text(
        [
            candidate(ExtractionMethod.PDF_TEXT, GOOD_TEXT),
            candidate(ExtractionMethod.OCR, BROKEN_TEXT, confidence=92.0),
        ],
        SETTINGS,
    )
    assert best is not None
    assert best.method is ExtractionMethod.PDF_TEXT


def test_low_confidence_ocr_is_ineligible_however_good_it_looks() -> None:
    """Confidence is a gate, not a score: the two scales are not comparable."""
    best = choose_best_text(
        [
            candidate(ExtractionMethod.PDF_TEXT, BROKEN_TEXT),
            candidate(ExtractionMethod.OCR, GOOD_TEXT, confidence=20.0),
        ],
        SETTINGS,
    )
    assert best is not None
    assert best.method is ExtractionMethod.PDF_TEXT


def test_a_tie_goes_to_the_parser() -> None:
    """Cheaper, and Tesseract's Armenian model misreads «և» as «ն»."""
    best = choose_best_text(
        [
            candidate(ExtractionMethod.OCR, GOOD_TEXT, confidence=95.0),
            candidate(ExtractionMethod.PDF_TEXT, GOOD_TEXT),
        ],
        SETTINGS,
    )
    assert best is not None
    assert best.method is ExtractionMethod.PDF_TEXT


def test_nothing_to_choose_from_returns_none() -> None:
    """An unreadable page is reported, not guessed at."""
    assert choose_best_text([], SETTINGS) is None
    gated = choose_best_text([candidate(ExtractionMethod.OCR, GOOD_TEXT, confidence=5.0)], SETTINGS)
    assert gated is None


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #


def test_a_corrupt_pdf_raises_a_named_error() -> None:
    """Not a bare library exception, and never an empty document."""
    with pytest.raises(PdfParseError):
        open_pdf((FIXTURES / "corrupt.pdf").read_bytes())


def test_an_empty_file_raises() -> None:
    """Zero bytes is a failure, not a document with no pages."""
    with pytest.raises(PdfParseError):
        open_pdf(b"")


def test_word_positions_rebuild_lines_that_flat_extraction_splits() -> None:
    """The fixture draws every word on its own line, as the real summary does."""
    document = open_pdf((FIXTURES / "synthetic_one_word_per_line.pdf").read_bytes())
    try:
        flat = document[0].get_text()
        rebuilt = text_from_word_positions(document[0])
    finally:
        document.close()
    flat_lines = [line for line in flat.splitlines() if line.strip()]
    assert all(len(line.split()) == 1 for line in flat_lines)
    assert score_text(rebuilt).score >= score_text(flat).score


def test_candidates_carry_their_line_structure_into_scoring() -> None:
    """Scoring must see the defect, so candidates are not line-joined yet."""
    document = open_pdf((FIXTURES / "synthetic_one_word_per_line.pdf").read_bytes())
    try:
        candidates = page_candidates(document[0], SETTINGS, page_number=1)
    finally:
        document.close()
    flat = next(c for c in candidates if c.method is ExtractionMethod.PDF_TEXT)
    assert "one_word_per_line" in flat.quality.defects


def test_headers_footers_and_page_numbers_are_removed_from_a_real_parse() -> None:
    """End to end over three pages with known furniture."""
    document = process_document(
        fetch_of(FIXTURES / "synthetic_furniture.pdf"), SETTINGS, use_ocr=False
    )
    assert len(document.pages) == 3
    assert "ՍԱԿԱԳՆԵՐ" not in document.text
    assert "էջ 1/3" not in document.text
    assert "Բաժին 2" in document.text


def test_the_page_cap_is_respected() -> None:
    """A long document cannot make one run unbounded."""
    settings = DocumentSettings(max_pages=2)
    document = process_document(
        fetch_of(FIXTURES / "synthetic_furniture.pdf"), settings, use_ocr=False
    )
    assert len(document.pages) == 2


def test_thousand_groups_are_repaired_in_a_processed_document() -> None:
    """«10 000 000» arrives as one value; the phone number is untouched."""
    document = process_document(
        fetch_of(FIXTURES / "synthetic_furniture.pdf"), SETTINGS, use_ocr=False
    )
    assert "10000000 ՀՀ դրամ" in document.text
    assert "10 59 10 10" in document.text


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #


def test_junk_tables_are_refused() -> None:
    """PyMuPDF reports cells like ['ն','և','',''] on graphics-heavy pages."""
    assert not is_plausible_table((("ն", "և", "", ""),), SETTINGS)
    assert not is_plausible_table((("", "", ""), ("", "", "")), SETTINGS)
    assert not is_plausible_table((("միայն մեկ սյուն",), ("երկրորդ տող",)), SETTINGS)


def test_a_real_table_is_kept() -> None:
    """Shape and fill are what distinguish a table from a layout accident."""
    rows = (("Արժույթ", "Տոկոսադրույք"), ("ՀՀ դրամ", "13,5%"), ("ԱՄՆ դոլար", "9,5%"))
    assert is_plausible_table(rows, SETTINGS)
    assert Table(page=1, rows=rows).as_text().startswith("Արժույթ | Տոկոսադրույք")
    assert Table(page=1, rows=rows).shape == (3, 2)


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

PAGE = """
<html><head><title>Սպառողական վարկ | Ակբա</title></head>
<body>
  <nav>Գլխավոր Վարկեր Ավանդներ</nav>
  <script>var rates = {"hidden": "13.9%"};</script>
  <main>
    <h2>Տոկոսադրույքներ</h2>
    <div><span>Անվանական տոկոսադրույք՝ 13,9%</span></div>
    <div class="wrapper"><div class="inner">Փաստացի տոկոսադրույք՝ 15,1%</div></div>
    <table><tr><th>Արժույթ</th><th>Տոկոսադրույք</th></tr>
           <tr><td>ՀՀ դրամ</td><td>13,9%</td></tr></table>
    <h2>Վճարներ</h2>
    <p>Սպասարկման վճար՝ 0%</p>
  </main>
  <footer>© ԱԿԲԱ ԲԱՆԿ</footer>
</body></html>
"""


def test_html_keeps_content_including_text_inside_nested_divs() -> None:
    """A tag allow-list dropped 19 of 21 rate mentions on the real page."""
    document = process_document(html_fetch(PAGE), SETTINGS)
    assert document.kind is DocumentKind.HTML
    assert "Անվանական տոկոսադրույք՝ 13,9%" in document.text
    assert "Փաստացի տոկոսադրույք՝ 15,1%" in document.text


def test_html_drops_navigation_scripts_and_footers() -> None:
    """Boilerplate would be retrieved and quoted as if it were content."""
    document = process_document(html_fetch(PAGE), SETTINGS)
    assert "Ավանդներ" not in document.text
    assert "var rates" not in document.text
    assert "© ԱԿԲԱ ԲԱՆԿ" not in document.text


def test_html_headings_become_sections() -> None:
    """Sections become the section field of evidence, so they are provenance."""
    document = process_document(html_fetch(PAGE), SETTINGS)
    assert [section.title for section in document.sections] == ["Տոկոսադրույքներ", "Վճարներ"]


def test_html_tables_are_extracted_structurally() -> None:
    """Validation is shared with the PDF path, so both are judged alike."""
    document = process_document(html_fetch(PAGE), SETTINGS)
    assert len(document.tables) == 1
    assert document.tables[0].rows[0] == ("Արժույթ", "Տոկոսադրույք")


def test_html_evidence_never_claims_a_page_number() -> None:
    """One page internally, so code paths are uniform; None in the evidence."""
    document = process_document(html_fetch(PAGE), SETTINGS)
    assert document.pages[0].number == 1
    assert document.evidence_page(1) is None


def test_an_empty_page_is_an_error_not_an_empty_document() -> None:
    """Missing data is reported; it is never quietly empty."""
    with pytest.raises(DocumentError, match="no readable text"):
        process_document(html_fetch("<html><body><nav>միայն մենյու</nav></body></html>"), SETTINGS)


# --------------------------------------------------------------------------- #
# The façade
# --------------------------------------------------------------------------- #


def test_identity_is_the_content_not_the_url() -> None:
    """ACBA serves one tariff PDF from three URLs; it is one document."""
    first = fetch_of(FIXTURES / "synthetic_furniture.pdf")
    same_bytes = FetchResult(
        url="https://www.acba.am/media/uploaded/synthetic_furniture.pdf",
        requested_url="https://www.acba.am/media/uploaded/synthetic_furniture.pdf",
        content=first.content,
        content_type=first.content_type,
        sha256=first.sha256,
        size=first.size,
        retrieved_at=first.retrieved_at,
        checked_at=first.checked_at,
        from_cache=False,
    )
    a = process_document(first, SETTINGS, use_ocr=False)
    b = process_document(same_bytes, SETTINGS, use_ocr=False)
    assert a.doc_id == b.doc_id
    assert a.source_url != b.source_url


def test_an_unsupported_content_type_is_refused() -> None:
    """The document layer does not guess at what it was handed."""
    fetch = html_fetch("ok")
    odd = FetchResult(
        url=fetch.url,
        requested_url=fetch.url,
        content=b"\x00\x01binary",
        content_type="application/octet-stream",
        sha256=fetch.sha256,
        size=7,
        retrieved_at=fetch.retrieved_at,
        checked_at=fetch.checked_at,
        from_cache=False,
    )
    with pytest.raises(DocumentError, match="unsupported content type"):
        process_document(odd, SETTINGS)


def test_timestamps_are_carried_through_from_the_fetch() -> None:
    """A report needs both when it changed and when it was last verified."""
    fetch = fetch_of(FIXTURES / "synthetic_furniture.pdf")
    document = process_document(fetch, SETTINGS, use_ocr=False)
    assert document.retrieved_at == fetch.retrieved_at
    assert document.checked_at == fetch.checked_at


def test_document_helpers_report_what_a_reviewer_needs() -> None:
    """Which strategies were used, and where a quote sits."""
    document = process_document(html_fetch(PAGE), SETTINGS)
    assert document.methods == (ExtractionMethod.HTML,)
    offset = document.text.find("Սպասարկման վճար")
    assert document.section_for(1, offset) == "Վճարներ"


# --------------------------------------------------------------------------- #
# OCR
# --------------------------------------------------------------------------- #


def test_ocr_is_skipped_gracefully_when_tesseract_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poor reading a human can check beats no reading at all."""
    import tariff_agent.documents.ocr as ocr_module

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("tesseract is not installed")

    monkeypatch.setattr(ocr_module, "ocr_page", explode)
    document = process_document(
        fetch_of(FIXTURES / "synthetic_furniture.pdf"), SETTINGS, use_ocr=False
    )
    assert len(document.pages) == 3


def test_the_render_is_capped_so_a_document_cannot_dictate_memory_use() -> None:
    """A large page at 300 dpi is tens of megapixels."""
    from tariff_agent.documents.ocr import effective_dpi

    settings = DocumentSettings(ocr_dpi=300, ocr_max_pixels=1_000_000)
    assert effective_dpi(595, 842, settings) < 300
    assert effective_dpi(595, 842, DocumentSettings()) == 300


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract is not installed")
def test_ocr_reads_a_real_scanned_acba_page() -> None:
    """The document-processing fallback, on a real page with no text layer.

    The sample under data/samples/ocr/ is page 1 of ACBA's mortgage information
    summary, rendered to an image. It has no text layer at all, so the parser
    returns nothing and OCR is the only reading available.
    """
    sample = OCR_SAMPLE / "acba_mortgage_summary_page1_scanned.pdf"
    document = process_document(fetch_of(sample), SETTINGS)
    assert document.methods == (ExtractionMethod.OCR,)
    assert document.pages[0].ocr_confidence is not None
    assert document.pages[0].ocr_confidence > 60
    assert "ամփոփագիր" in document.text
    assert "1,000,000" in document.text


def test_document_is_a_frozen_contract() -> None:
    """Stages pass documents around; none of them may edit one in place."""
    document = process_document(html_fetch(PAGE), SETTINGS)
    assert isinstance(document, Document)
    with pytest.raises(AttributeError):
        document.quality = 0.1  # type: ignore[misc]


def test_a_bullet_item_is_not_a_section_heading() -> None:
    """The mortgage summary yielded «o մինչև 6 ամիս …» as a detected heading.

    PDF bullets extract as a bare "o", and a short unpunctuated line followed by
    a longer one is exactly what a heading looks like - so list items had to be
    excluded explicitly, or evidence would cite a bullet as its section.
    """
    from tariff_agent.documents.sections import detect_sections

    page = (
        "o մինչև 6 ամիս (ներառյալ) վաղեմության դեպքում\n"
        "տեքստ որը բավական երկար է որպեսզի համարվի մարմին և ոչ թե վերնագիր\n"
        "Տոկոսադրույք\n"
        "Անվանական տոկոսադրույքը կազմում է տասներեք և կես տոկոս տարեկան"
    )
    assert [section.title for section in detect_sections(page, 1)] == ["Տոկոսադրույք"]


def test_ocr_must_win_by_a_margin_not_by_a_hair() -> None:
    """A marginally better OCR reading is not worth its known corruptions.

    On page 2 of the real tariff book OCR scored 1.00 against the parser's 0.92
    and won - and turned «չի գանձվում» ("is not charged") into «sh գանձվում»,
    losing the negation that made a fee zero. The quality score cannot see that:
    it measures whether text reads like text, not whether it says what the page
    says.
    """
    from tariff_agent.documents.strategies import OCR_MARGIN

    parsed = candidate(ExtractionMethod.PDF_TEXT, GOOD_TEXT)
    slightly_better = GOOD_TEXT + "\nԼրացուցիչ տող որը մի փոքր բարելավում է գնահատականը։"
    ocr = candidate(ExtractionMethod.OCR, slightly_better, confidence=93.0)
    assert ocr.quality.score - parsed.quality.score < OCR_MARGIN

    best = choose_best_text([parsed, ocr], SETTINGS)
    assert best is not None
    assert best.method is ExtractionMethod.PDF_TEXT


def test_ocr_still_wins_when_the_parse_is_genuinely_broken() -> None:
    """The mortgage summary is why OCR exists here; the margin must not lose it."""
    from tariff_agent.documents.strategies import OCR_MARGIN

    broken = candidate(ExtractionMethod.PDF_TEXT, BROKEN_TEXT)
    ocr = candidate(ExtractionMethod.OCR, GOOD_TEXT, confidence=92.0)
    assert ocr.quality.score - broken.quality.score >= OCR_MARGIN

    best = choose_best_text([broken, ocr], SETTINGS)
    assert best is not None
    assert best.method is ExtractionMethod.OCR


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract is not installed")
def test_a_page_with_no_text_layer_still_chooses_ocr() -> None:
    """The margin must not cost us the documents OCR exists for.

    The sample is a real ACBA page rendered to an image: the parser finds
    nothing at all, so OCR clears any margin and must win.
    """
    sample = OCR_SAMPLE / "acba_mortgage_summary_page1_scanned.pdf"
    document = process_document(fetch_of(sample), SETTINGS)
    assert document.methods == (ExtractionMethod.OCR,)
    assert "ամփոփագիր" in document.text


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract is not installed")
def test_a_page_that_parses_cleanly_keeps_the_parsed_text() -> None:
    """And it must stop OCR displacing a working parse.

    On page 2 of the real tariff book OCR scored 1.00 against the parser's 0.92,
    won, and turned «չի գանձվում» into «sh գանձվում» - losing the negation that
    made a fee zero. This fixture parses cleanly, so the parse must be kept even
    though OCR would also read it.
    """
    document = process_document(fetch_of(FIXTURES / "synthetic_furniture.pdf"), SETTINGS)
    assert ExtractionMethod.OCR not in document.methods
    assert "Անվանական տոկոսադրույքը" in document.text

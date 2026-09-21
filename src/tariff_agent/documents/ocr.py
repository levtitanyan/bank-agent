"""Reading a rendered page with Tesseract.

Not a last resort. ACBA's mortgage information summary - the authoritative
document for that product - has a text layer that is broken three different ways,
while OCR reads the same page at 92% mean confidence with the amounts intact.
So OCR competes on equal terms, and :mod:`tariff_agent.documents.strategies`
decides by measurement.

Two guards:

* **Render size is capped.** A large page at 300 dpi is tens of megapixels, and
  a document must not be able to dictate our memory use. Over the cap, the dpi
  is reduced and the reduction logged.
* **A missing Tesseract is not a crash.** The document is marked for review with
  whatever the parser produced: a poor reading a human can check beats no
  reading at all.

Known defect, documented rather than repaired: the Armenian model reads «և» as
«ն» («Տևողություն» → «Տնողություն»). Digits are unaffected, so extracted values
are sound, but term matching suffers. Repairing it here would be a lossy guess;
folding the two spellings together at retrieval time is the Phase 5 answer.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from tariff_agent.config import DocumentSettings
from tariff_agent.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OcrResult:
    """What Tesseract made of one page.

    Attributes:
        text: The recognised text.
        mean_confidence: Mean per-word confidence, 0-100. Used only as a gate.
        words: How many words were recognised.
        dpi: The resolution actually used, which may be below the configured
            value if the render had to be capped.
    """

    text: str
    mean_confidence: float
    words: int
    dpi: int


def effective_dpi(width_pt: float, height_pt: float, settings: DocumentSettings) -> int:
    """Return the highest dpi that keeps the rendered page within the cap.

    Args:
        width_pt: Page width in points.
        height_pt: Page height in points.
        settings: Provides the configured dpi and pixel cap.

    Returns:
        The configured dpi, or a lower one if rendering at it would exceed
        :attr:`DocumentSettings.ocr_max_pixels`.
    """
    dpi = settings.ocr_dpi
    if width_pt <= 0 or height_pt <= 0:
        return dpi
    pixels = (width_pt / 72.0 * dpi) * (height_pt / 72.0 * dpi)
    if pixels <= settings.ocr_max_pixels:
        return dpi
    scale = (settings.ocr_max_pixels / pixels) ** 0.5
    return max(72, int(dpi * scale))


def ocr_page(page: Any, settings: DocumentSettings, *, page_number: int = 0) -> OcrResult | None:
    """Render a PDF page and read it with Tesseract.

    Args:
        page: A PyMuPDF page.
        settings: Render resolution, language and cap.
        page_number: 1-based page number, for logging.

    Returns:
        The OCR result, or ``None`` when OCR could not run at all - Tesseract
        missing, or the render failing. Both are logged; neither raises, because
        the parser's text is still available.
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        logger.warning("ocr_unavailable", extra={"reason": f"import failed: {exc}"})
        return None

    rect = page.rect
    dpi = effective_dpi(rect.width, rect.height, settings)
    if dpi < settings.ocr_dpi:
        logger.info(
            "ocr_render_downscaled",
            extra={"page": page_number, "requested_dpi": settings.ocr_dpi, "used_dpi": dpi},
        )

    try:
        pixmap = page.get_pixmap(dpi=dpi)
        image = Image.open(io.BytesIO(pixmap.tobytes("png")))
        data = pytesseract.image_to_data(
            image, lang=settings.ocr_languages, output_type=pytesseract.Output.DICT
        )
    except Exception as exc:  # pytesseract raises TesseractNotFound and others
        logger.warning(
            "ocr_failed",
            extra={"page": page_number, "error_type": type(exc).__name__},
        )
        return None

    words = [word for word in data.get("text", []) if word.strip()]
    confidences = [float(value) for value in data.get("conf", []) if float(value) >= 0]
    mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    text = _lines_from(data)

    logger.info(
        "ocr_completed",
        extra={
            "page": page_number,
            "words": len(words),
            "mean_confidence": round(mean_confidence, 1),
            "dpi": dpi,
        },
    )
    return OcrResult(
        text=text, mean_confidence=round(mean_confidence, 2), words=len(words), dpi=dpi
    )


def _lines_from(data: dict[str, list[Any]]) -> str:
    """Rebuild lines from Tesseract's per-word output.

    Using the block/paragraph/line numbers Tesseract already assigns keeps
    columns apart, which joining by vertical position alone does not.

    Args:
        data: The dictionary returned by ``image_to_data``.

    Returns:
        The recognised text, one line per detected line.
    """
    lines: dict[tuple[int, int, int, int], list[str]] = {}
    texts = data.get("text", [])
    for index, word in enumerate(texts):
        if not word.strip():
            continue
        key = (
            int(data["page_num"][index]),
            int(data["block_num"][index]),
            int(data["par_num"][index]),
            int(data["line_num"][index]),
        )
        lines.setdefault(key, []).append(word)
    return "\n".join(" ".join(words) for _, words in sorted(lines.items()))

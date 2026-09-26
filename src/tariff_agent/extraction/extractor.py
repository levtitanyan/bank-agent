"""Getting structured answers out of the passages - with or without a model.

Two backends behind one interface:

* :class:`GeminiExtractor` - the real one. ``temperature=0`` and a response
  schema, so the output is a shape rather than prose; bounded retries on
  malformed output; a failure raises rather than returning something plausible.
* :class:`RuleBasedExtractor` - deterministic pattern matching over the same
  passages. It exists so the whole suite and every demonstration run without a
  key, and it stamps ``rule_based`` on its output so that a demo can never be
  mistaken for a model extraction.

Neither backend decides whether an answer is *usable*. That is verification and
validation, which are deterministic and tested.
"""

from __future__ import annotations

import json
import re
import time
from typing import Protocol

from tariff_agent.errors import ExtractionError
from tariff_agent.extraction.prompt import build_prompt
from tariff_agent.extraction.schema import ExtractedField, ExtractionResponse
from tariff_agent.fields import FieldSpec, ValueKind
from tariff_agent.observability.logging import get_logger
from tariff_agent.rag.chunking import Chunk
from tariff_agent.rag.value_shapes import has_value_shape

logger = get_logger(__name__)

MAX_ATTEMPTS = 3
"""Attempts per call before giving up. A malformed response is worth retrying;
a third identical failure is not."""

RULE_BASED = "rule_based"
"""Stamped on output produced without a model."""


class Extractor(Protocol):
    """What the pipeline needs from an extraction backend."""

    @property
    def method(self) -> str:
        """Identifier recorded on the extraction and shown in the report."""
        ...

    def extract(
        self, specs: list[FieldSpec], chunks: list[Chunk], *, product: str | None = None
    ) -> ExtractionResponse:
        """Read the requested fields out of the passages."""
        ...


class GeminiExtractor:
    """Structured extraction with Gemini.

    Args:
        api_key: AI Studio key.
        model: Model id.
        max_attempts: Attempts per call before raising.
    """

    def __init__(
        self, api_key: str, *, model: str = "gemini-2.5-flash", max_attempts: int = MAX_ATTEMPTS
    ) -> None:
        """Create the client."""
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._max_attempts = max_attempts
        self.calls = 0

    @property
    def method(self) -> str:
        """Identifier recorded on the extraction."""
        return f"gemini:{self._model}"

    def extract(
        self, specs: list[FieldSpec], chunks: list[Chunk], *, product: str | None = None
    ) -> ExtractionResponse:
        """Extract one group of fields from the passages shown for them.

        Args:
            specs: The fields to extract.
            chunks: The passages retrieved for those fields.
            product: The product being monitored, so a shared document's scope
                is not mistaken for this product's terms.

        Returns:
            The model's structured answer.

        Raises:
            ExtractionError: After the permitted attempts. A run stops rather
                than proceeding with output nobody could parse.
        """
        from google.genai import types

        prompt = build_prompt(specs, chunks, product=product)
        last: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                self.calls += 1
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.0,
                        response_mime_type="application/json",
                        response_schema=ExtractionResponse,
                    ),
                )
                parsed = self._parse(response)
                logger.info(
                    "fields_extracted",
                    extra={
                        "fields": [spec.id for spec in specs],
                        "passages": len(chunks),
                        "attempt": attempt,
                        "answers": len(parsed.fields),
                    },
                )
                return parsed
            except Exception as exc:  # the SDK raises several unrelated types
                last = exc
                logger.warning(
                    "extraction_attempt_failed",
                    extra={
                        "fields": [spec.id for spec in specs],
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                    },
                )
                if attempt < self._max_attempts:
                    time.sleep(1.0 * attempt)
        raise ExtractionError(
            f"could not extract {[spec.id for spec in specs]} after "
            f"{self._max_attempts} attempts: {last}"
        )

    def _parse(self, response: object) -> ExtractionResponse:
        """Turn a model response into the schema, or fail loudly.

        Args:
            response: The SDK response.

        Returns:
            The parsed answer.

        Raises:
            ExtractionError: If the response holds nothing usable.
        """
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, ExtractionResponse):
            return parsed
        text = getattr(response, "text", None)
        if not text:
            raise ExtractionError("the model returned an empty response")
        return ExtractionResponse.model_validate(json.loads(text))


class RuleBasedExtractor:
    """Deterministic extraction, for runs without a key.

    Finds, for each field, the first passage that mentions it and contains a
    value of the expected kind, and quotes the sentence that does so. It is
    plainly worse than a model at reading prose - and it is honest, repeatable,
    and enough to exercise every downstream stage offline.
    """

    @property
    def method(self) -> str:
        """Identifier recorded on the extraction and shown in the report."""
        return RULE_BASED

    def extract(
        self, specs: list[FieldSpec], chunks: list[Chunk], *, product: str | None = None
    ) -> ExtractionResponse:
        """Extract fields by pattern matching over the passages.

        Args:
            specs: The fields to extract.
            chunks: The passages retrieved for those fields.
            product: Unused: pattern matching has no notion of product scope,
                which is one of the ways it is worse than a model.

        Returns:
            One answer per field, NOT_FOUND where no sentence qualifies.
        """
        answers = [self._extract_one(spec, chunks) for spec in specs]
        logger.info(
            "fields_extracted",
            extra={
                "fields": [spec.id for spec in specs],
                "passages": len(chunks),
                "method": RULE_BASED,
                "found": sum(1 for answer in answers if not answer.is_not_found),
            },
        )
        return ExtractionResponse(fields=answers)

    def _extract_one(self, spec: FieldSpec, chunks: list[Chunk]) -> ExtractedField:
        """Find one field's value in the passages.

        Args:
            spec: The field.
            chunks: The passages.

        Returns:
            The field's answer.
        """
        from tariff_agent.rag.text import has_informative_hit

        for chunk in chunks:
            for sentence in _sentences(chunk.text):
                if not has_informative_hit(sentence, spec.query_terms, is_informative=None):
                    continue
                if not has_value_shape(sentence, spec.kind):
                    continue
                value = _value_from(sentence, spec.kind) or sentence.strip()
                return ExtractedField(
                    field_id=spec.id,
                    value=value,
                    quote=sentence.strip(),
                    chunk_id=chunk.chunk_id,
                )
        return ExtractedField(field_id=spec.id)


_SENTENCE_SPLIT = re.compile(r"(?<=[։.!?])\s+|\n")
_VALUE_PATTERNS: dict[ValueKind, re.Pattern[str]] = {
    ValueKind.PERCENT: re.compile(
        r"\d{1,3}(?:[.,]\d{1,2})?\s*(?:-|–)?\s*\d{0,3}(?:[.,]\d{1,2})?\s*%"
    ),
    ValueKind.AMOUNT: re.compile(
        r"\d{1,3}(?:[ ,.]\d{3})+(?:\s*(?:-|–)\s*\d{1,3}(?:[ ,.]\d{3})+)?[^։.\n]{0,20}"
    ),
    ValueKind.TERM: re.compile(r"\d{1,4}\s*(?:-|–|ից)?\s*\d{0,4}\s*(?:ամիս|տարի)"),
}


def _sentences(text: str) -> list[str]:
    """Split a passage into sentence-like fragments.

    Args:
        text: The passage.

    Returns:
        Fragments long enough to quote.
    """
    return [part for part in _SENTENCE_SPLIT.split(text) if len(part.strip()) >= 12]


def _value_from(sentence: str, kind: ValueKind) -> str | None:
    """Pull the value itself out of a sentence, when its shape is recognisable.

    Args:
        sentence: The sentence.
        kind: The expected value kind.

    Returns:
        The matched value, or None to fall back to quoting the sentence.
    """
    pattern = _VALUE_PATTERNS.get(kind)
    if pattern is None:
        return None
    match = pattern.search(sentence)
    return match.group(0).strip() if match else None

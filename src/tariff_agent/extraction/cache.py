"""Remembering what the model answered, so a demonstration cannot be killed by a quota.

The free tier allows twenty generate-content requests a day for
``gemini-2.5-flash``. One full two-product run makes about sixteen, so an
unlucky reviewer watching a second run would see a `429` rather than a tariff
report. That is an unacceptable way for a demonstration to fail, and the fix is
not a larger quota but not asking twice for the same answer.

An answer is cached against everything that could change it: the passages it was
read from (by document and chunk id), the fields requested, the model, and the
prompt version. Change any of those and the key changes, so a stale answer can
never be served for a different question. A re-run over unchanged documents
makes **no model calls at all** and says so in the report.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tariff_agent.extraction.extractor import Extractor
from tariff_agent.extraction.prompt import PROMPT_VERSION
from tariff_agent.extraction.schema import ExtractionResponse
from tariff_agent.fields import FieldSpec
from tariff_agent.observability.logging import get_logger
from tariff_agent.rag.chunking import Chunk

logger = get_logger(__name__)


def cache_key(specs: list[FieldSpec], chunks: list[Chunk], model: str) -> str:
    """Build the key an answer is stored under.

    Args:
        specs: The fields requested.
        chunks: The passages shown.
        model: The model that answered.

    Returns:
        A hex digest covering every input that could change the answer.
    """
    parts = [
        f"v{PROMPT_VERSION}",
        model,
        ",".join(sorted(spec.id for spec in specs)),
        ";".join(sorted(f"{chunk.doc_id[:16]}:{chunk.chunk_id}" for chunk in chunks)),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


class CachedExtractor:
    """An extractor that answers from disk when it already knows.

    Args:
        inner: The backend to fall back on when an answer is not stored.
        directory: Where answers are stored.
        enabled: False makes every call go to the backend, which is what the
            ``--no-cache`` flag is for when a reviewer wants to watch a real
            call happen.
    """

    def __init__(self, inner: Extractor, directory: Path, *, enabled: bool = True) -> None:
        """Wrap a backend."""
        self._inner = inner
        self._directory = directory
        self._enabled = enabled
        self.cache_hits = 0
        self.cache_misses = 0

    @property
    def method(self) -> str:
        """Identifier recorded on the extraction, taken from the backend."""
        return self._inner.method

    @property
    def calls(self) -> int:
        """How many calls the backend actually made."""
        return int(getattr(self._inner, "calls", 0))

    @property
    def served_from_cache(self) -> bool:
        """Whether every answer in this run came from disk."""
        return self.cache_hits > 0 and self.cache_misses == 0

    def extract(
        self, specs: list[FieldSpec], chunks: list[Chunk], *, product: str | None = None
    ) -> ExtractionResponse:
        """Return a stored answer, or ask the backend and store its answer.

        Args:
            specs: The fields to extract.
            chunks: The passages retrieved for them.
            product: The product being monitored, passed through to the backend.

        Returns:
            The answer. A cached answer is identical to the one the backend
            gave, because it *is* that answer, replayed.
        """
        if not self._enabled:
            self.cache_misses += 1
            return self._ask(specs, chunks, product)

        key = cache_key(specs, chunks, self.method)
        path = self._directory / f"{key}.json"
        stored = self._read(path)
        if stored is not None:
            self.cache_hits += 1
            logger.info(
                "extraction_served_from_cache",
                extra={"fields": [spec.id for spec in specs], "key": key},
            )
            return stored

        self.cache_misses += 1
        response = self._ask(specs, chunks, product)
        self._write(path, response, specs)
        return response

    def _ask(
        self, specs: list[FieldSpec], chunks: list[Chunk], product: str | None
    ) -> ExtractionResponse:
        """Call the wrapped backend.

        Args:
            specs: The fields to extract.
            chunks: The passages.
            product: The product being monitored.

        Returns:
            The backend's answer.
        """
        extract = self._inner.extract
        result: ExtractionResponse = extract(specs, chunks)
        return result

    def _read(self, path: Path) -> ExtractionResponse | None:
        """Load a stored answer, if one is usable.

        Args:
            path: Where it would be stored.

        Returns:
            The answer, or None. A corrupt entry is treated as absent: the cost
            is one model call, and trusting it would be worse.
        """
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return ExtractionResponse.model_validate(payload["response"])
        except (KeyError, TypeError, ValueError):
            logger.warning("extraction_cache_unreadable", extra={"path": str(path)})
            return None

    def _write(self, path: Path, response: ExtractionResponse, specs: list[FieldSpec]) -> None:
        """Store an answer.

        Failures are logged and swallowed: an unwritable cache must not fail a
        run whose answer is already in hand.

        Args:
            path: Where to store it.
            response: The answer.
            specs: The fields it answers, recorded for readability.
        """
        payload = {
            "prompt_version": PROMPT_VERSION,
            "model": self.method,
            "fields": [spec.id for spec in specs],
            "response": response.model_dump(mode="json"),
        }
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "extraction_cache_write_failed",
                extra={"path": str(path), "error_type": type(exc).__name__},
            )

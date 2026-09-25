"""Exception hierarchy for the tariff agent.

One base class (:class:`TariffAgentError`) with one subclass per pipeline stage,
so that callers can decide *how* to react without string-matching messages:

* transient network problems are retryable (Phase 2 retries only on these);
* a :class:`NeedsReviewError` is not a bug - it is the pipeline correctly
  refusing to decide alone, and routes to the human reviewer (Phase 7);
* everything else stops the run with a clear reason.

Phase 8 wraps tool functions so these exceptions become
``{"status": "error", "error_type": ..., "message": ...}`` dictionaries: the LLM
sees a controlled result, never a Python traceback, and never fabricated data.
"""

from __future__ import annotations


class TariffAgentError(Exception):
    """Base class for every error this project raises deliberately."""


class ConfigError(TariffAgentError):
    """Configuration is missing, unreadable or invalid (bad YAML, absent key)."""


class FetchError(TariffAgentError):
    """A URL could not be retrieved, or retrieving it was not permitted.

    Covers HTTP errors, timeouts, oversized downloads, unexpected content types
    and blocked (non-allowlisted) URLs.
    """


class DomainNotAllowedError(FetchError):
    """A URL - or a redirect hop - left the configured allowlist."""


class HttpStatusError(FetchError):
    """The server answered with an error status.

    Attributes:
        status_code: The HTTP status received.
        url: The URL that produced it.
        retry_after: Raw ``Retry-After`` header, when the server sent one. The
            retry logic caps it, so a server cannot stall a run.
    """

    def __init__(self, status_code: int, url: str, retry_after: str | None = None) -> None:
        """Initialize the error.

        Args:
            status_code: The HTTP status received.
            url: The URL that produced it.
            retry_after: Raw ``Retry-After`` header value, if present.
        """
        super().__init__(f"HTTP {status_code} for {url}")
        self.status_code = status_code
        self.url = url
        self.retry_after = retry_after


class ResponseTooLargeError(FetchError):
    """A download exceeded the configured size cap and was aborted."""


class UnexpectedContentTypeError(FetchError):
    """The response was not the kind of document the caller expected.

    Raised when the ``Content-Type`` header or the leading magic bytes do not
    match, e.g. an HTML error page served as ``application/pdf``.
    """


class RobotsDisallowedError(FetchError):
    """robots.txt forbids fetching this URL, so we do not fetch it."""


class RobotsUnavailableError(TariffAgentError):
    """robots.txt could not be read, so permission is unknown.

    A missing file (4xx) means "no rules exist" and is not an error. This is
    raised only when the server failed to answer (5xx, timeout): permission is
    genuinely unknown, so the run stops rather than guessing in our own favour.

    Deliberately **not** a :class:`FetchError`. Callers that crawl several pages
    treat a failed fetch as "skip this page and carry on", and inheriting from
    FetchError let those handlers swallow this one - turning a run-stopping
    condition into a skipped page. Being outside that hierarchy makes the stop
    unignorable unless someone catches it by name.
    """


class DocumentError(TariffAgentError):
    """A downloaded document could not be parsed, OCR'd or cleaned."""


class PdfParseError(DocumentError):
    """A PDF could not be opened or read - corrupt, encrypted or empty."""


class OcrUnavailableError(DocumentError):
    """OCR was needed but Tesseract could not be used.

    Handled rather than raised in most paths: a document that cannot be OCR'd
    is marked for review with whatever text the parser produced, since a poor
    reading a human can check beats no reading at all.
    """


class ProductNotFoundError(TariffAgentError):
    """The user's product query could not be resolved to a known product."""


class SourceNotFoundError(TariffAgentError):
    """No official document or page was discovered for a resolved product."""


class ExtractionError(TariffAgentError):
    """The model failed to return a usable structured extraction.

    Raised after bounded retries, e.g. on invalid JSON or schema violations.
    """


class ValidationFailedError(TariffAgentError):
    """Deterministic validation rejected the extraction."""


class EmbeddingError(TariffAgentError):
    """Chunks or a query could not be embedded.

    Callers degrade to lexical retrieval and mark the result, rather than
    failing a run that BM25 can still answer.
    """


class KnowledgeIndexError(TariffAgentError):
    """A knowledge index could not be built, read or written.

    Named rather than reusing ``IndexError``, which is a builtin meaning
    something entirely different.
    """


class SnapshotError(TariffAgentError):
    """A tariff snapshot could not be stored or read."""


class NeedsReviewError(TariffAgentError):
    """The pipeline cannot proceed safely without a human decision.

    Attributes:
        reason: Machine-readable trigger, e.g. ``"multiple_candidate_documents"``,
            ``"large_change"``, ``"low_extraction_quality"``.
        details: Evidence the reviewer needs in order to decide.
    """

    def __init__(self, reason: str, details: dict[str, object] | None = None) -> None:
        """Initialize the error.

        Args:
            reason: Machine-readable HITL trigger.
            details: Supporting evidence for the reviewer.
        """
        super().__init__(f"human review required: {reason}")
        self.reason = reason
        self.details = details or {}


class ReviewRejectedError(TariffAgentError):
    """A human reviewer rejected the result, so the run stops without saving."""

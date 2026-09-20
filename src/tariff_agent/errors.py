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


class DocumentError(TariffAgentError):
    """A downloaded document could not be parsed, OCR'd or cleaned."""


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

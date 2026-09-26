"""What one agent run cost, and how well it went.

Requirement 5.13 asks for execution time, tool failure rate, extraction
completeness, validation failures, HITL rate and token usage. They are
collected here rather than scattered through the tools, so that one
``run_metrics`` log line carries the whole picture and the CLI can print the
same numbers without recomputing them.

Nothing here estimates. Token usage comes from the model's own reported usage;
when the SDK does not report any, the field stays ``None`` and the line says so
rather than carrying a plausible number.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from tariff_agent.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One completed tool call.

    Attributes:
        name: The tool.
        duration_s: Wall-clock seconds it took.
        status: The ``status`` field the tool returned.
        error_type: The exception class name, when one was caught.
    """

    name: str
    duration_s: float
    status: str
    error_type: str | None = None


@dataclass
class RunMetrics:
    """Everything measured about one agent run.

    Attributes:
        tool_calls: Every call, in order.
        model_calls: Generate-content requests the extractor actually made.
        cache_hits: Extraction answers replayed from disk instead.
        prompt_tokens: Reported input tokens, or None if the SDK reported none.
        response_tokens: Reported output tokens, or None.
        total_tokens: Reported total, or None.
        fields_required: Required tariff fields for the products touched.
        fields_found: How many of them came back with a verified value.
        validation_failures: Values the deterministic checks downgraded.
        reviews_asked: Questions put to a human this run.
        reviews_remembered: Questions answered from a stored decision instead.
        stop_reason: Why the run ended.
    """

    tool_calls: list[ToolCall] = dataclass_field(default_factory=list)
    model_calls: int = 0
    cache_hits: int = 0
    prompt_tokens: int | None = None
    response_tokens: int | None = None
    total_tokens: int | None = None
    fields_required: int = 0
    fields_found: int = 0
    validation_failures: int = 0
    reviews_asked: int = 0
    reviews_remembered: int = 0
    stop_reason: str = "completed"
    _started: float = dataclass_field(default_factory=time.monotonic)

    def record(self, call: ToolCall) -> None:
        """Add one completed tool call.

        Args:
            call: The call to record.
        """
        self.tool_calls.append(call)

    def add_tokens(self, prompt: int | None, response: int | None, total: int | None) -> None:
        """Accumulate usage reported by the model.

        Args:
            prompt: Input tokens for one response, or None.
            response: Output tokens for one response, or None.
            total: Total tokens for one response, or None.
        """
        for name, value in (
            ("prompt_tokens", prompt),
            ("response_tokens", response),
            ("total_tokens", total),
        ):
            if value is None:
                continue
            current = getattr(self, name)
            setattr(self, name, value if current is None else current + value)

    @property
    def duration_s(self) -> float:
        """Wall-clock seconds since the run began."""
        return round(time.monotonic() - self._started, 3)

    @property
    def tool_failures(self) -> int:
        """Calls that returned an error status."""
        return sum(1 for call in self.tool_calls if call.status == "error")

    @property
    def tool_failure_rate(self) -> float:
        """Share of calls that failed, 0-1."""
        return round(self.tool_failures / len(self.tool_calls), 3) if self.tool_calls else 0.0

    @property
    def completeness(self) -> float | None:
        """Share of required fields found, 0-1, or None if nothing was extracted.

        ``0.0`` would say the run looked and found nothing. A turn answered from
        a stored snapshot never looked, and the two must not read alike.
        """
        if not self.fields_required:
            return None
        return round(self.fields_found / self.fields_required, 3)

    @property
    def hitl_rate(self) -> float:
        """Share of review questions that actually reached a person.

        A question answered from a stored decision did not cost anyone's
        attention, so it counts in the denominator and not the numerator. A
        monitor whose rate falls over time is one whose reviewer is being asked
        only about genuinely new things.
        """
        total = self.reviews_asked + self.reviews_remembered
        return round(self.reviews_asked / total, 3) if total else 0.0

    def as_dict(self) -> dict[str, Any]:
        """Return the metrics as a flat JSON-safe mapping.

        Returns:
            Every measured value, including the per-tool timings.
        """
        return {
            "duration_s": self.duration_s,
            "tool_calls": len(self.tool_calls),
            "tool_failures": self.tool_failures,
            "tool_failure_rate": self.tool_failure_rate,
            "model_calls": self.model_calls,
            "cache_hits": self.cache_hits,
            "prompt_tokens": self.prompt_tokens,
            "response_tokens": self.response_tokens,
            "total_tokens": self.total_tokens,
            "fields_required": self.fields_required,
            "fields_found": self.fields_found,
            "completeness": self.completeness,
            "validation_failures": self.validation_failures,
            "reviews_asked": self.reviews_asked,
            "reviews_remembered": self.reviews_remembered,
            "hitl_rate": self.hitl_rate,
            "stop_reason": self.stop_reason,
            "per_tool_s": {
                call.name: round(
                    sum(c.duration_s for c in self.tool_calls if c.name == call.name), 3
                )
                for call in self.tool_calls
            },
        }

    def emit(self) -> dict[str, Any]:
        """Log the metrics as one line and return them.

        Returns:
            The same mapping :meth:`as_dict` produces.
        """
        payload = self.as_dict()
        logger.info("run_metrics", extra=payload)
        return payload

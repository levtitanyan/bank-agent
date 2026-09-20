"""Structured JSON logging with a per-run correlation id.

Every line a run emits carries the same ``run_id``, so one monitoring run can be
reconstructed end to end from the log: request, tool activity, source retrieval,
RAG result, extraction, validation, HITL outcome, final status.

Two deliberate rules:

* **JSON, not prose.** Lines are machine-readable, so extraction completeness or
  tool failure rates can be computed from logs without parsing English.
* **No chain-of-thought.** We log decisions and their inputs/outputs (which
  document, which fields, which validation failed), never the model's internal
  reasoning, and never secrets.

The ``run_id`` lives in a :class:`~contextvars.ContextVar`, so callers do not
have to thread it through every function signature.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Final

_run_id: ContextVar[str | None] = ContextVar("run_id", default=None)

_RESERVED_RECORD_KEYS: Final[frozenset[str]] = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}
"""Attributes the logging module itself sets; anything else came from ``extra``."""


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        """Format one record.

        Args:
            record: The record to render. Any keyword passed via ``extra=`` is
                included as a top-level key.

        Returns:
            A single-line JSON string.
        """
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        if run_id := _run_id.get():
            payload["run_id"] = run_id
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_KEYS:
                payload[key] = value
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger.

    Safe to call more than once: existing handlers are replaced rather than
    stacked, so log lines are never duplicated.

    Args:
        level: Log level name, e.g. ``"INFO"`` or ``"DEBUG"``.
    """
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger.

    Args:
        name: Usually ``__name__``.

    Returns:
        The logger for that name.
    """
    return logging.getLogger(name)


def new_run_id() -> str:
    """Generate an identifier for one monitoring run.

    Returns:
        A short random hex string, e.g. ``"run-9f3a1c2b"``.
    """
    return f"run-{uuid.uuid4().hex[:8]}"


def current_run_id() -> str | None:
    """Return the run id bound to the current context, if any."""
    return _run_id.get()


@contextmanager
def run_context(run_id: str | None = None) -> Iterator[str]:
    """Bind a run id to every log line emitted inside the block.

    Args:
        run_id: Existing id to reuse; a new one is generated when omitted.

    Yields:
        The bound run id.
    """
    run_id = run_id or new_run_id()
    token = _run_id.set(run_id)
    try:
        yield run_id
    finally:
        _run_id.reset(token)

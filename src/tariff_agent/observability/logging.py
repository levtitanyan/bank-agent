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

When file logging is armed - ``configure_logging(runs_dir=...)`` - each
:func:`run_context` block additionally writes every line to
``<runs_dir>/<run_id>/log.jsonl``, so one run's audit trail is a single file
that can be attached to a report or replayed later. It is off unless armed, so
importing this module never creates directories and tests write nothing they did
not ask for.
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
from pathlib import Path
from typing import Any, Final

_run_id: ContextVar[str | None] = ContextVar("run_id", default=None)
_runs_dir: Path | None = None
"""Where per-run log files are written. ``None`` disables file logging."""

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


def configure_logging(level: str = "INFO", runs_dir: Path | None = None) -> None:
    """Install the JSON formatter on the root logger.

    Safe to call more than once: existing handlers are replaced rather than
    stacked, so log lines are never duplicated.

    Args:
        level: Log level name, e.g. ``"INFO"`` or ``"DEBUG"``.
        runs_dir: When given, arms per-run file logging - each
            :func:`run_context` block writes ``<runs_dir>/<run_id>/log.jsonl``
            in addition to stderr. ``None`` leaves file logging off.
    """
    global _runs_dir
    _runs_dir = runs_dir
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def run_log_path(run_id: str) -> Path | None:
    """Return where a run's log file is written, if file logging is armed.

    Args:
        run_id: The run identifier.

    Returns:
        The path to that run's ``log.jsonl``, or None when file logging is off.
    """
    return None if _runs_dir is None else _runs_dir / run_id / "log.jsonl"


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
    handler = _open_run_file(run_id)
    try:
        yield run_id
    finally:
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            handler.close()
        _run_id.reset(token)


def _open_run_file(run_id: str) -> logging.Handler | None:
    """Attach a file handler for this run, when file logging is armed.

    The same :class:`JsonFormatter` is reused, so the file contains exactly the
    lines stderr shows - including the secret masking that formatter relies on.

    Args:
        run_id: The run identifier, which names the directory.

    Returns:
        The handler, or None when file logging is off or the file cannot be
        opened. An unwritable log directory must not fail the run.
    """
    path = run_log_path(run_id)
    if path is None:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        logging.getLogger(__name__).warning("run_log_unavailable", extra={"path": str(path)})
        return None
    handler.setFormatter(JsonFormatter())
    logging.getLogger().addHandler(handler)
    return handler

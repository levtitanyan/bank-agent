"""Storing tariff snapshots and reviewer decisions in SQLite.

SQLite because it ships with Python, needs no service, is transactional, and a
monitoring history of two products is kilobytes. Postgres is the production
answer and nothing here needs it; the store is four methods wide so that swap
touches one module.

Three columns exist because of things learned in earlier phases:

* ``extraction_method`` and ``model`` - a diff between a rule-based snapshot and
  a model-extracted one compares the extractors, not the bank. That comparison
  is refused rather than reported as a tariff change.
* ``document_date`` and ``document_edition`` - "the tariff changed" and "the
  bank published a new edition" are different events, and a report that
  conflates them tells a reviewer the wrong story.
* ``status`` - a snapshot whose changes need a human is stored immediately as
  ``pending_review`` rather than withheld. An unattended nightly run must record
  history exactly when the change matters most; the reviewer's later decision
  resolves the stored row rather than creating it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from tariff_agent.errors import SnapshotError
from tariff_agent.models import TariffExtraction
from tariff_agent.observability.logging import get_logger

logger = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    bank              TEXT    NOT NULL,
    product_id        TEXT    NOT NULL,
    run_id            TEXT    NOT NULL,
    taken_at          TEXT    NOT NULL,
    source_url        TEXT    NOT NULL,
    doc_id            TEXT    NOT NULL,
    document_date     TEXT,
    document_edition  TEXT,
    schema_version    INTEGER NOT NULL,
    extraction_method TEXT    NOT NULL,
    prompt_version    INTEGER NOT NULL DEFAULT 0,
    status            TEXT    NOT NULL,
    payload           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshots_product ON snapshots(product_id, taken_at DESC);

CREATE TABLE IF NOT EXISTS reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   TEXT    NOT NULL,
    trigger      TEXT    NOT NULL,
    subject      TEXT    NOT NULL,
    decision     TEXT    NOT NULL,
    decided_by   TEXT    NOT NULL,
    decided_at   TEXT    NOT NULL,
    note         TEXT,
    snapshot_id  INTEGER,
    FOREIGN KEY(snapshot_id) REFERENCES snapshots(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS reviews_subject
    ON reviews(product_id, trigger, subject);
"""


class SnapshotStatus(StrEnum):
    """Whether a stored snapshot is confirmed."""

    STORED = "stored"
    """Nothing about it needed a human."""

    PENDING_REVIEW = "pending_review"
    """Stored, but carrying a change or conflict a reviewer has not settled.
    The report marks its values unconfirmed until a decision exists."""

    CONFIRMED = "confirmed"
    """A reviewer approved it."""

    REJECTED = "rejected"
    """A reviewer rejected it; it stays for the audit trail and is never used
    as a baseline."""


@dataclass(frozen=True, slots=True)
class StoredSnapshot:
    """One snapshot as it came back from the database.

    Attributes:
        id: Row identifier.
        run_id: The run that produced it.
        taken_at: When it was stored.
        status: Whether a human has settled it.
        extraction_method: What produced the values, so that a diff across
            methods can be refused.
        document_date: The date the source document stated for itself.
        document_edition: The edition the source document stated for itself.
        extraction: The values themselves.
    """

    id: int
    run_id: str
    taken_at: datetime
    status: SnapshotStatus
    extraction_method: str
    document_date: str | None
    document_edition: str | None
    extraction: TariffExtraction


class SnapshotStore:
    """The monitoring history.

    Args:
        path: Database file. Parent directories are created.
    """

    def __init__(self, path: Path) -> None:
        """Open the database and ensure the schema exists."""
        self._path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.executescript(SCHEMA)
        except (OSError, sqlite3.Error) as exc:
            raise SnapshotError(f"could not open the snapshot store at {path}: {exc}") from exc

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection with sensible durability settings.

        Yields:
            The connection, committed on success and closed either way.
        """
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def save(
        self,
        extraction: TariffExtraction,
        *,
        run_id: str,
        doc_id: str,
        status: SnapshotStatus = SnapshotStatus.STORED,
    ) -> int:
        """Store a snapshot.

        Args:
            extraction: The values to store.
            run_id: The run that produced them.
            doc_id: Content hash of the primary document.
            status: Whether it still needs a human.

        Returns:
            The new row's identifier.

        Raises:
            SnapshotError: If it cannot be written. A monitoring run that
                cannot record what it found has not really succeeded.
        """
        payload = extraction.model_dump_json()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO snapshots (
                        bank, product_id, run_id, taken_at, source_url, doc_id,
                        document_date, document_edition, schema_version,
                        extraction_method, prompt_version, status, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        extraction.bank,
                        extraction.product_id,
                        run_id,
                        datetime.now(UTC).isoformat(),
                        str(extraction.source_url),
                        doc_id,
                        extraction.document_date.isoformat() if extraction.document_date else None,
                        extraction.document_edition,
                        extraction.schema_version,
                        extraction.extraction_method,
                        extraction.prompt_version,
                        status.value,
                        payload,
                    ),
                )
        except sqlite3.Error as exc:
            raise SnapshotError(f"could not store a snapshot: {exc}") from exc

        snapshot_id = int(cursor.lastrowid or 0)
        logger.info(
            "snapshot_stored",
            extra={
                "snapshot_id": snapshot_id,
                "product_id": extraction.product_id,
                "status": status.value,
                "method": extraction.extraction_method,
                "document_date": (
                    extraction.document_date.isoformat() if extraction.document_date else None
                ),
                "edition": extraction.document_edition,
            },
        )
        return snapshot_id

    def latest(
        self, product_id: str, *, before_id: int | None = None
    ) -> StoredSnapshot | None:
        """Return the most recent usable snapshot for a product.

        Args:
            product_id: The product.
            before_id: Ignore rows at or after this id, so a run can find the
                snapshot *preceding* the one it just stored.

        Returns:
            The snapshot, or ``None`` when there is no history yet - which is a
            first run, not an error. Rejected snapshots are never returned:
            a baseline a human refused is not a baseline.
        """
        query = (
            "SELECT * FROM snapshots WHERE product_id = ? AND status != ? "
            + ("AND id < ? " if before_id is not None else "")
            + "ORDER BY id DESC LIMIT 1"
        )
        parameters: tuple[Any, ...] = (product_id, SnapshotStatus.REJECTED.value)
        if before_id is not None:
            parameters = (*parameters, before_id)
        try:
            with self._connect() as connection:
                row = connection.execute(query, parameters).fetchone()
        except sqlite3.Error as exc:
            raise SnapshotError(f"could not read snapshots for {product_id}: {exc}") from exc
        return self._to_snapshot(row) if row else None

    def history(self, product_id: str, *, limit: int = 20) -> list[StoredSnapshot]:
        """Return recent snapshots for a product, newest first.

        Args:
            product_id: The product.
            limit: How many to return.

        Returns:
            The snapshots, including rejected ones, because the audit trail is
            the point of keeping them.
        """
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM snapshots WHERE product_id = ? ORDER BY id DESC LIMIT ?",
                    (product_id, limit),
                ).fetchall()
        except sqlite3.Error as exc:
            raise SnapshotError(f"could not read history for {product_id}: {exc}") from exc
        return [snapshot for snapshot in (self._to_snapshot(row) for row in rows) if snapshot]

    def set_status(self, snapshot_id: int, status: SnapshotStatus) -> None:
        """Resolve a stored snapshot after a reviewer decides.

        Args:
            snapshot_id: The row to update.
            status: Its new status.

        Raises:
            SnapshotError: If the update fails.
        """
        try:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE snapshots SET status = ? WHERE id = ?", (status.value, snapshot_id)
                )
        except sqlite3.Error as exc:
            raise SnapshotError(f"could not update snapshot {snapshot_id}: {exc}") from exc
        logger.info(
            "snapshot_status_changed",
            extra={"snapshot_id": snapshot_id, "status": status.value},
        )

    def _to_snapshot(self, row: sqlite3.Row) -> StoredSnapshot | None:
        """Turn a database row into a snapshot.

        Args:
            row: The row.

        Returns:
            The snapshot, or ``None`` when the stored payload cannot be read.
            A corrupt row is skipped rather than failing the run: the next
            usable snapshot is a serviceable baseline, and a crash is not.
        """
        try:
            extraction = TariffExtraction.from_stored(json.loads(row["payload"]))
        except (SnapshotError, json.JSONDecodeError, TypeError) as exc:
            logger.warning(
                "snapshot_unreadable",
                extra={"snapshot_id": row["id"], "error_type": type(exc).__name__},
            )
            return None
        return StoredSnapshot(
            id=int(row["id"]),
            run_id=str(row["run_id"]),
            taken_at=datetime.fromisoformat(row["taken_at"]),
            status=SnapshotStatus(row["status"]),
            extraction_method=str(row["extraction_method"]),
            document_date=row["document_date"],
            document_edition=row["document_edition"],
            extraction=extraction,
        )

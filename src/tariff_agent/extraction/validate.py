"""Deterministic checks on what the model returned.

Nothing here asks the model whether its answer is good. Each check is a rule
that can be stated, tested and argued with:

* a rate is a number between 0 and 100, and a range runs low to high;
* an effective rate is never below the nominal one it is derived from;
* an amount range runs low to high;
* a term is a plausible number of months;
* evidence points at an allow-listed URL;
* required fields are present, and completeness is counted rather than felt.

A failed check never edits the value. It downgrades the field to UNVERIFIED and
records why, because silently correcting a bank's published number is the one
thing this system must never do.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from urllib.parse import urlsplit

from tariff_agent.config import Allowlist
from tariff_agent.fields import FIELDS_BY_ID, REQUIRED_FIELD_IDS, ValueKind
from tariff_agent.models import FieldStatus, FieldValue, TariffExtraction
from tariff_agent.observability.logging import get_logger

logger = get_logger(__name__)

MAX_TERM_MONTHS = 600
"""50 years. Longer than any retail loan; beyond this it is a misparse."""


@dataclass(frozen=True, slots=True)
class FieldIssue:
    """One thing wrong with one field.

    Attributes:
        field_id: The field.
        problem: What is wrong, in words a reviewer can act on.
    """

    field_id: str
    problem: str


@dataclass
class ValidationReport:
    """The outcome of validating an extraction.

    Attributes:
        issues: Everything found wrong.
        completeness: Share of required fields that hold a usable value.
        checked: How many fields were examined.
    """

    issues: list[FieldIssue] = dataclass_field(default_factory=list)
    completeness: float = 0.0
    checked: int = 0

    @property
    def is_valid(self) -> bool:
        """Whether the extraction passed every check."""
        return not self.issues

    def issues_for(self, field_id: str) -> list[str]:
        """Return the problems recorded for one field.

        Args:
            field_id: The field.

        Returns:
            The problem descriptions.
        """
        return [issue.problem for issue in self.issues if issue.field_id == field_id]


def validate_extraction(
    extraction: TariffExtraction, allowlist: Allowlist
) -> tuple[TariffExtraction, ValidationReport]:
    """Check an extraction and downgrade anything that fails.

    Args:
        extraction: The extraction to check.
        allowlist: Domains evidence is permitted to come from.

    Returns:
        The extraction with failed fields downgraded to UNVERIFIED, and the
        report explaining every downgrade.
    """
    report = ValidationReport()
    fields = dict(extraction.fields)

    for field_id, value in fields.items():
        report.checked += 1
        if value.status is FieldStatus.NOT_FOUND:
            continue
        problems = _check_field(field_id, value, allowlist)
        problems.extend(_cross_check(field_id, value, fields))
        if problems:
            for problem in problems:
                report.issues.append(FieldIssue(field_id=field_id, problem=problem))
            fields[field_id] = value.model_copy(update={"status": FieldStatus.UNVERIFIED})

    usable = sum(
        1 for field_id in REQUIRED_FIELD_IDS if fields[field_id].status is FieldStatus.FOUND
    )
    report.completeness = usable / len(REQUIRED_FIELD_IDS)

    logger.info(
        "extraction_validated",
        extra={
            "product_id": extraction.product_id,
            "checked": report.checked,
            "issues": len(report.issues),
            "completeness": round(report.completeness, 2),
            "downgraded": [issue.field_id for issue in report.issues],
        },
    )
    return extraction.model_copy(update={"fields": fields}), report


def _check_field(field_id: str, value: FieldValue, allowlist: Allowlist) -> list[str]:
    """Check one field against the rules for its kind.

    Args:
        field_id: The field.
        value: Its extracted value.
        allowlist: Domains evidence may come from.

    Returns:
        The problems found.
    """
    problems: list[str] = []
    spec = FIELDS_BY_ID[field_id]

    if value.evidence is not None:
        host = urlsplit(str(value.evidence.source_url)).hostname or ""
        if host not in allowlist.domains:
            problems.append(f"evidence points at {host!r}, which is not an approved source")

    normalized = value.normalized
    if normalized is None:
        # Not an error in itself - free text has no numeric form - but a
        # numeric field we could not parse is worth a reviewer's attention.
        if spec.kind in (ValueKind.PERCENT, ValueKind.AMOUNT, ValueKind.TERM):
            problems.append(f"the value {value.value!r} could not be read as a {spec.kind.value}")
        return problems

    if spec.kind is ValueKind.PERCENT:
        problems.extend(_check_range(normalized, "min", "max", 0.0, 100.0, "rate"))
    elif spec.kind is ValueKind.AMOUNT:
        problems.extend(_check_range(normalized, "min", "max", 0.0, None, "amount"))
    elif spec.kind is ValueKind.TERM:
        problems.extend(
            _check_range(normalized, "min_months", "max_months", 0.0, MAX_TERM_MONTHS, "term")
        )
    return problems


def _check_range(
    normalized: dict[str, object],
    low_key: str,
    high_key: str,
    minimum: float,
    maximum: float | None,
    label: str,
) -> list[str]:
    """Check that a normalized range is ordered and within bounds.

    Args:
        normalized: The normalized mapping.
        low_key: Key holding the lower bound.
        high_key: Key holding the upper bound.
        minimum: Smallest permitted value.
        maximum: Largest permitted value, or None for no upper bound.
        label: What to call the value in a message.

    Returns:
        The problems found.
    """
    low = normalized.get(low_key)
    high = normalized.get(high_key)
    if not isinstance(low, int | float) or not isinstance(high, int | float):
        return [f"the {label} has no numeric bounds"]
    problems: list[str] = []
    if low > high:
        problems.append(f"the {label} range runs backwards ({low} to {high})")
    if low < minimum:
        problems.append(f"the {label} is below {minimum}")
    if maximum is not None and high > maximum:
        problems.append(f"the {label} exceeds {maximum}")
    return problems


def _cross_check(field_id: str, value: FieldValue, fields: dict[str, FieldValue]) -> list[str]:
    """Check a field against another field it must agree with.

    Args:
        field_id: The field being checked.
        value: Its value.
        fields: All fields, for comparison.

    Returns:
        The problems found.
    """
    if field_id != "effective_rate":
        return []
    nominal = fields.get("nominal_rate")
    if nominal is None or nominal.normalized is None or value.normalized is None:
        return []
    nominal_min = nominal.normalized.get("min")
    effective_max = value.normalized.get("max")
    if not isinstance(nominal_min, int | float) or not isinstance(effective_max, int | float):
        return []
    if effective_max < nominal_min:
        # The effective rate includes the nominal rate plus mandatory charges,
        # so it cannot be smaller. When it is, one of the two was misread.
        return [
            f"the effective rate ({effective_max}%) is below the nominal rate "
            f"({nominal_min}%), which is not possible"
        ]
    return []

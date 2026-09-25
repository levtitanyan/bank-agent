"""The tariff field registry: the single source of truth for what we extract.

Every downstream stage derives from this module rather than hard-coding field
names of its own:

* Phase 5 (RAG)        - ``query_terms`` become the per-field retrieval queries.
* Phase 6 (extraction) - the registry drives the Gemini response schema and the
  prompt's field list; ``kind`` selects the normalizer used in validation.
* Phase 6 (validation) - ``required`` decides what counts as a completeness gap.
* Phase 7 (diff)       - ``kind`` decides how a change magnitude is measured
  (percentage points for rates, relative percent for amounts).
* Reporting            - ``label_hy`` / ``label_en`` are the display labels.

Adding an eleventh tariff field should mean adding one entry here and nothing else.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field


class ValueKind(StrEnum):
    """How a field's raw text is normalized and compared.

    The kind is deliberately coarse. It answers only "which normalizer and which
    change-magnitude rule apply", not "what does this value mean".
    """

    CURRENCY = "currency"
    """ISO-like currency code, e.g. AMD / USD / EUR."""

    TERM = "term"
    """A duration or duration range, e.g. «մինչև 60 ամիս»."""

    AMOUNT = "amount"
    """A money amount or amount range, e.g. «300 000 - 10 000 000 ՀՀ դրամ»."""

    PERCENT = "percent"
    """A rate or rate range, e.g. "13,5%" or "17.5-21.6%"."""

    FEE = "fee"
    """A fee, which may be a money amount, a percent, or «անվճար» (free)."""

    TEXT = "text"
    """Free text that is reported verbatim, e.g. collateral requirements."""


class FieldSpec(BaseModel):
    """Definition of one tariff field.

    Attributes:
        id: Stable machine identifier. Used as the key in extractions, snapshots
            and diffs, so it must never change once snapshots exist.
        label_hy: Armenian label, shown in the business report.
        label_en: English label, used in logs and English-language output.
        kind: Which normalizer / comparison rule applies to the value.
        required: Whether a missing value counts against extraction completeness.
            Optional fields may legitimately be absent from a document.
        query_terms: Retrieval queries for this field, in Armenian and English.
            Armenian first: the authoritative documents are Armenian, so those
            terms carry the retrieval.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label_hy: str
    label_en: str
    kind: ValueKind
    required: bool
    query_terms: tuple[str, ...] = Field(min_length=1)


TARIFF_FIELDS: Final[tuple[FieldSpec, ...]] = (
    FieldSpec(
        id="currency",
        label_hy="Արժույթ",
        label_en="Currency",
        kind=ValueKind.CURRENCY,
        required=True,
        query_terms=("արժույթ", "ՀՀ դրամ AMD USD EUR", "currency"),
    ),
    FieldSpec(
        id="term",
        label_hy="Ժամկետ",
        label_en="Term",
        kind=ValueKind.TERM,
        required=True,
        query_terms=("վարկի ժամկետ", "մինչև ամիս տարի", "loan term months"),
    ),
    FieldSpec(
        id="amount",
        label_hy="Գումար",
        label_en="Amount",
        kind=ValueKind.AMOUNT,
        required=True,
        query_terms=("վարկի գումար", "նվազագույն առավելագույն գումար", "loan amount limits"),
    ),
    FieldSpec(
        id="nominal_rate",
        label_hy="Անվանական տոկոսադրույք",
        label_en="Nominal interest rate",
        kind=ValueKind.PERCENT,
        required=True,
        query_terms=(
            "անվանական տոկոսադրույք",
            "տարեկան տոկոսադրույք",
            "nominal interest rate",
        ),
    ),
    FieldSpec(
        id="effective_rate",
        label_hy="Փաստացի տոկոսադրույք",
        label_en="Effective interest rate",
        kind=ValueKind.PERCENT,
        required=True,
        query_terms=(
            "փաստացի տոկոսադրույք",
            "տարեկան փաստացի տոկոսադրույք",
            "effective interest rate EIR",
        ),
    ),
    FieldSpec(
        id="collateral",
        label_hy="Ապահովվածություն",
        label_en="Collateral / security",
        kind=ValueKind.TEXT,
        required=True,
        query_terms=("ապահովվածություն", "գրավ երաշխավորություն", "collateral pledge guarantee"),
    ),
    FieldSpec(
        id="application_fee",
        label_hy="Հայտի ուսումնասիրության վճար",
        label_en="Application review fee",
        kind=ValueKind.FEE,
        required=False,
        query_terms=(
            "հայտի ուսումնասիրության վճար",
            "դիմումի ուսումնասիրման վճար",
            "հայտի քննարկման միջնորդավճար",
            "application review fee",
        ),
    ),
    FieldSpec(
        id="disbursement_fee",
        label_hy="Տրամադրման վճար",
        label_en="Disbursement fee",
        kind=ValueKind.FEE,
        required=False,
        # ACBA does not write «վճար» for these: it writes «միջնորդավճար»
        # (commission), as in «վարկի տրամադրման պահին ... գանձվում է միանվագ
        # միջնորդավճար». Query terms taken from the field's title instead of the
        # documents are the commonest reason a stated value is reported missing.
        query_terms=(
            "վարկի տրամադրման միանվագ միջնորդավճար",
            "վարկի տրամադրման վճար",
            "միանվագ վճար տրամադրման",
            "disbursement fee",
        ),
    ),
    FieldSpec(
        id="service_fee",
        label_hy="Սպասարկման վճար",
        label_en="Service fee",
        kind=ValueKind.FEE,
        required=False,
        # Same again: the tariff book states this as «վարկային հաշվի բացման,
        # վարման և սպասարկման նպատակով ... միջնորդավճար», and the version with
        # «վճար» alone never reached the chunk that answers it.
        query_terms=(
            "վարկային հաշվի սպասարկման միջնորդավճար",
            "սպասարկման վճար",
            "ամսական սպասարկման վճար",
            "service fee",
        ),
    ),
    FieldSpec(
        id="salary_privileges",
        label_hy="Աշխատավարձը բանկով ստանալիս արտոնություններ",
        label_en="Salary customer privileges",
        kind=ValueKind.TEXT,
        required=False,
        # Corrected against the documents: ACBA writes «աշխատավարձը Բանկի
        # միջոցով ստանալու դեպքում ... արտոնյալ տոկոսադրույք», not the phrasing
        # the field name suggests. Query terms written from a field's title
        # rather than from the corpus are the commonest cause of a field being
        # reported NOT_FOUND while its answer sits in the evidence.
        query_terms=(
            "աշխատավարձը բանկի միջոցով",
            "արտոնյալ պայման աշխատավարձային հաճախորդ",
            "աշխատավարձային նախագիծ",
            "salary client privileges",
        ),
    ),
)
"""All tariff fields, in business-report display order."""

FIELDS_BY_ID: Final[dict[str, FieldSpec]] = {spec.id: spec for spec in TARIFF_FIELDS}
"""Registry indexed by field id, for O(1) lookup."""

FIELD_IDS: Final[tuple[str, ...]] = tuple(FIELDS_BY_ID)
"""All field ids, in registry order."""

REQUIRED_FIELD_IDS: Final[tuple[str, ...]] = tuple(
    spec.id for spec in TARIFF_FIELDS if spec.required
)
"""Ids of fields whose absence is a completeness gap."""


def get_field(field_id: str) -> FieldSpec:
    """Return the specification of one tariff field.

    Args:
        field_id: Registry id, e.g. ``"nominal_rate"``.

    Returns:
        The matching :class:`FieldSpec`.

    Raises:
        KeyError: If no field with that id is registered.
    """
    return FIELDS_BY_ID[field_id]

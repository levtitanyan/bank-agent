"""Phase 1 tests: the contracts everything downstream depends on.

There is no business logic yet, so these tests cover exactly what Phase 1
promises: the field registry is well formed, the configuration files parse,
and the NOT_FOUND / registry-completeness invariants in the models hold.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from tariff_agent.config import (
    Allowlist,
    load_allowlist,
    load_products,
)
from tariff_agent.errors import ConfigError
from tariff_agent.fields import FIELD_IDS, REQUIRED_FIELD_IDS, TARIFF_FIELDS, get_field
from tariff_agent.models import (
    NOT_FOUND,
    Evidence,
    FieldStatus,
    FieldValue,
    TariffExtraction,
)
from tariff_agent.observability.logging import current_run_id, run_context

# --------------------------------------------------------------------------- #
# Field registry
# --------------------------------------------------------------------------- #


def test_registry_covers_the_ten_assignment_fields() -> None:
    """The ten fields required by the assignment are registered, in order."""
    assert FIELD_IDS == (
        "currency",
        "term",
        "amount",
        "nominal_rate",
        "effective_rate",
        "collateral",
        "application_fee",
        "disbursement_fee",
        "service_fee",
        "salary_privileges",
    )


def test_registry_ids_are_unique() -> None:
    """Duplicate ids would silently collapse in FIELDS_BY_ID."""
    ids = [spec.id for spec in TARIFF_FIELDS]
    assert len(set(ids)) == len(ids)


def test_every_field_has_labels_and_query_terms() -> None:
    """Retrieval and reporting both depend on these being populated."""
    for spec in TARIFF_FIELDS:
        assert spec.label_hy.strip()
        assert spec.label_en.strip()
        assert spec.query_terms


def test_required_fields_are_the_core_financial_terms() -> None:
    """Fees may be absent from a document; the core terms may not."""
    assert REQUIRED_FIELD_IDS == (
        "currency",
        "term",
        "amount",
        "nominal_rate",
        "effective_rate",
        "collateral",
    )


def test_get_field_rejects_unknown_id() -> None:
    """Typos must fail loudly rather than return a default."""
    with pytest.raises(KeyError):
        get_field("interest")


def test_field_specs_are_immutable() -> None:
    """A stage must not be able to mutate the shared registry."""
    with pytest.raises(ValidationError):
        TARIFF_FIELDS[0].id = "changed"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_allowlist_loads_and_is_https_only() -> None:
    """Only ACBA hosts, only https, no wildcard entries."""
    allowlist = load_allowlist()
    assert allowlist.allowed_schemes == ("https",)
    assert set(allowlist.domains) == {"acba.am", "www.acba.am"}
    assert not any("*" in domain for domain in allowlist.domains)


def test_products_load_with_both_required_products() -> None:
    """The assignment requires at least two supported products."""
    catalog = load_products()
    assert catalog.bank == "ACBA Bank"
    assert catalog.product_ids == ("consumer_loan", "mortgage")


def test_product_synonyms_cover_three_languages() -> None:
    """A user may type the product name in hy, en or ru."""
    catalog = load_products()
    for product in catalog.products:
        assert set(product.synonyms) == {"hy", "en", "ru"}
        assert product.name_hy in product.all_names
        assert len(product.all_names) > 5


def test_product_seed_urls_are_on_allowlisted_hosts() -> None:
    """Seeds are a fallback source, so they must obey the same domain policy."""
    allowlist = load_allowlist()
    catalog = load_products()
    for product in catalog.products:
        for url in (*product.seed_pages, *product.seed_documents):
            assert url.startswith("https://")
            host = url.split("/")[2]
            assert host in allowlist.domains, f"{url} is off the allowlist"


def test_catalog_get_returns_none_for_unknown_product() -> None:
    """Unknown ids resolve to None; Phase 3 turns that into ProductNotFoundError."""
    catalog = load_products()
    assert catalog.get("consumer_loan") is not None
    assert catalog.get("car_leasing") is None


def test_missing_config_file_raises_config_error(tmp_path: Path) -> None:
    """A missing file must fail as ConfigError, not FileNotFoundError."""
    with pytest.raises(ConfigError, match="not found"):
        load_products(tmp_path / "nope.yaml")


def test_malformed_config_file_raises_config_error(tmp_path: Path) -> None:
    """A schema violation in YAML must be reported as a configuration problem."""
    bad = tmp_path / "allowlist.yaml"
    bad.write_text("domains: []\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid allowlist"):
        load_allowlist(bad)


def test_allowlist_model_is_frozen() -> None:
    """Policy must not be mutable at runtime."""
    allowlist = Allowlist(allowed_schemes=("https",), domains=("acba.am",))
    with pytest.raises(ValidationError):
        allowlist.domains = ("evil.com",)  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Models: NOT_FOUND and evidence invariants
# --------------------------------------------------------------------------- #


def _evidence() -> Evidence:
    """Build a minimal valid evidence record for tests."""
    return Evidence(
        document_name="Consumer loan information summary",
        source_url="https://www.acba.am/files/loans-tariffs.pdf",
        page=3,
        section="Տոկոսադրույք",
        quote="Անվանական տոկոսադրույք՝ 13,5%",
    )


def test_not_found_helper_produces_the_sentinel() -> None:
    """The canonical missing value carries no data and no evidence."""
    value = FieldValue.not_found()
    assert value.value == NOT_FOUND
    assert value.status is FieldStatus.NOT_FOUND
    assert value.evidence is None
    assert value.normalized is None
    assert not value.is_found


def test_found_value_requires_evidence() -> None:
    """A value without a source location is not reportable."""
    with pytest.raises(ValidationError, match="no evidence"):
        FieldValue(value="13.5%", status=FieldStatus.EXTRACTED, confidence=0.9)


def test_not_found_value_must_not_carry_evidence() -> None:
    """NOT_FOUND and evidence together would be a contradiction."""
    with pytest.raises(ValidationError, match="must not carry evidence"):
        FieldValue(
            value=NOT_FOUND,
            status=FieldStatus.NOT_FOUND,
            confidence=1.0,
            evidence=_evidence(),
        )


def test_sentinel_and_status_must_agree() -> None:
    """Status 'extracted' with the sentinel value is rejected, and vice versa."""
    with pytest.raises(ValidationError, match="disagree"):
        FieldValue(
            value=NOT_FOUND,
            status=FieldStatus.EXTRACTED,
            confidence=0.9,
            evidence=_evidence(),
        )
    with pytest.raises(ValidationError, match="disagree"):
        FieldValue(value="13.5%", status=FieldStatus.NOT_FOUND, confidence=0.9)


def test_confidence_is_bounded() -> None:
    """Confidence outside 0..1 indicates a bug upstream."""
    with pytest.raises(ValidationError):
        FieldValue.not_found(confidence=1.5)


def _extraction(fields: dict[str, FieldValue]) -> TariffExtraction:
    """Build an extraction with the given field map."""
    return TariffExtraction(
        bank="ACBA Bank",
        product_id="consumer_loan",
        document_name="Consumer loan information summary",
        source_url="https://www.acba.am/files/loans-tariffs.pdf",
        retrieved_at=datetime(2026, 9, 20, tzinfo=UTC),
        fields=fields,
    )


def test_extraction_requires_every_registry_field() -> None:
    """Silently dropping a field would hide a gap from the report."""
    fields = {fid: FieldValue.not_found() for fid in FIELD_IDS}
    del fields["service_fee"]
    with pytest.raises(ValidationError, match="missing tariff fields"):
        _extraction(fields)


def test_extraction_rejects_invented_fields() -> None:
    """The model must not be able to add fields of its own."""
    fields = {fid: FieldValue.not_found() for fid in FIELD_IDS}
    fields["secret_bonus_rate"] = FieldValue.not_found()
    with pytest.raises(ValidationError, match="unknown tariff fields"):
        _extraction(fields)


def test_extraction_splits_found_and_missing_fields() -> None:
    """The report lists found values and missing fields separately."""
    fields = {fid: FieldValue.not_found() for fid in FIELD_IDS}
    fields["nominal_rate"] = FieldValue(
        value="13,5%",
        status=FieldStatus.EXTRACTED,
        confidence=0.95,
        evidence=_evidence(),
    )
    extraction = _extraction(fields)
    assert extraction.found_field_ids == ("nominal_rate",)
    assert "nominal_rate" not in extraction.not_found_field_ids
    assert len(extraction.not_found_field_ids) == len(FIELD_IDS) - 1


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def test_run_context_binds_and_clears_the_run_id() -> None:
    """Every line of a run is correlated, and the id does not leak afterwards."""
    assert current_run_id() is None
    with run_context() as run_id:
        assert run_id.startswith("run-")
        assert current_run_id() == run_id
    assert current_run_id() is None


def test_json_formatter_emits_run_id_and_extras(caplog: pytest.LogCaptureFixture) -> None:
    """Structured extras land as top-level JSON keys, not inside the message."""
    import json
    import logging

    from tariff_agent.observability.logging import JsonFormatter

    record = logging.LogRecord(
        name="tariff_agent.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="document_downloaded",
        args=None,
        exc_info=None,
    )
    record.source_url = "https://acba.am/hy"
    with run_context("run-test1234"):
        line = json.loads(JsonFormatter().format(record))
    assert line["event"] == "document_downloaded"
    assert line["level"] == "INFO"
    assert line["run_id"] == "run-test1234"
    assert line["source_url"] == "https://acba.am/hy"

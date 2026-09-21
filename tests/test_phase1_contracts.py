"""Phase 1 tests: the contracts everything downstream depends on.

There is no business logic yet, so these tests cover exactly what Phase 1
promises: the field registry is well formed, the configuration files parse,
and the NOT_FOUND / registry-completeness invariants in the models hold.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from tariff_agent.config import (
    Allowlist,
    Settings,
    load_allowlist,
    load_products,
)
from tariff_agent.errors import ConfigError, SnapshotError
from tariff_agent.fields import (
    FIELD_IDS,
    REQUIRED_FIELD_IDS,
    TARIFF_FIELDS,
    ValueKind,
    get_field,
)
from tariff_agent.models import (
    NOT_FOUND,
    SCHEMA_VERSION,
    Evidence,
    FieldStatus,
    FieldValue,
    TariffExtraction,
)
from tariff_agent.observability.logging import (
    JsonFormatter,
    configure_logging,
    current_run_id,
    run_context,
)

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
    """The canonical missing value carries no data, no evidence, no fake score."""
    value = FieldValue.not_found()
    assert value.value == NOT_FOUND
    assert value.status is FieldStatus.NOT_FOUND
    assert value.evidence is None
    assert value.normalized is None
    assert value.confidence is None
    assert not value.is_found


def test_confidence_defaults_to_none_not_a_made_up_score() -> None:
    """Confidence is computed deterministically in Phase 6; until then it is unset."""
    value = FieldValue(
        value="13,5%", status=FieldStatus.FOUND, evidence=_evidence()
    )
    assert value.confidence is None


def test_found_value_requires_evidence() -> None:
    """A value without a source location is not reportable."""
    with pytest.raises(ValidationError, match="no evidence"):
        FieldValue(value="13.5%", status=FieldStatus.FOUND, confidence=0.9)


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
            status=FieldStatus.FOUND,
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


def test_unverified_and_conflict_still_require_evidence() -> None:
    """A questionable value must still say where it came from, or it is useless."""
    for status in (FieldStatus.UNVERIFIED, FieldStatus.CONFLICT):
        with pytest.raises(ValidationError, match="no evidence"):
            FieldValue(value="13.5%", status=status)
        assert FieldValue(value="13.5%", status=status, evidence=_evidence()).is_found is False


def test_html_evidence_needs_no_page_number() -> None:
    """HTML product pages have no pagination, and are first-class sources."""
    evidence = Evidence(
        document_name="Consumer loans | ACBA",
        source_url="https://acba.am/hy/individual/loans/consumer-loans",
        section="Տոկոսադրույքներ",
        quote="Անվանական տոկոսադրույքը՝ 13,5%",
    )
    assert evidence.page is None
    assert FieldValue(value="13,5%", status=FieldStatus.FOUND, evidence=evidence).is_found


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
        status=FieldStatus.FOUND,
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


# --------------------------------------------------------------------------- #
# Stored snapshots: forward migration
# --------------------------------------------------------------------------- #


def _stored_payload() -> dict[str, object]:
    """Build a snapshot payload as it would be read back from SQLite."""
    fields = {fid: FieldValue.not_found() for fid in FIELD_IDS}
    fields["nominal_rate"] = FieldValue(
        value="13,5%", status=FieldStatus.FOUND, evidence=_evidence()
    )
    return json.loads(_extraction(fields).model_dump_json())


def test_from_stored_round_trips_a_current_snapshot() -> None:
    """A snapshot written by this version loads back unchanged."""
    restored = TariffExtraction.from_stored(_stored_payload())
    assert restored.schema_version == SCHEMA_VERSION
    assert restored.found_field_ids == ("nominal_rate",)
    assert restored.fields["nominal_rate"].value == "13,5%"


def test_from_stored_drops_fields_no_longer_in_the_registry() -> None:
    """A field removed from the registry must not block loading old data."""
    payload = _stored_payload()
    payload["fields"]["legacy_penalty_fee"] = {  # type: ignore[index]
        "value": "0.1%",
        "status": "found",
        "evidence": _evidence().model_dump(mode="json"),
    }
    restored = TariffExtraction.from_stored(payload)
    assert "legacy_penalty_fee" not in restored.fields
    assert set(restored.fields) == set(FIELD_IDS)


def test_from_stored_treats_unversioned_snapshots_as_version_zero() -> None:
    """Snapshots written before versioning existed are still loadable."""
    payload = _stored_payload()
    del payload["schema_version"]
    assert TariffExtraction.from_stored(payload).schema_version == 0


def test_from_stored_rejects_a_payload_that_is_not_a_snapshot() -> None:
    """Corrupt storage must fail as SnapshotError, not AttributeError."""
    with pytest.raises(SnapshotError, match="no 'fields' mapping"):
        TariffExtraction.from_stored({"bank": "ACBA Bank"})


def test_from_stored_rejects_a_snapshot_with_broken_field_data() -> None:
    """A value that violates the evidence invariant must not load silently."""
    payload = _stored_payload()
    payload["fields"]["term"] = {"value": "60 months", "status": "found"}  # type: ignore[index]
    with pytest.raises(SnapshotError, match="could not be loaded"):
        TariffExtraction.from_stored(payload)


def test_strict_constructor_is_unaffected_by_the_lenient_loader() -> None:
    """Leniency is for storage only; fresh model output stays strict."""
    fields = {fid: FieldValue.not_found() for fid in FIELD_IDS}
    del fields["service_fee"]
    with pytest.raises(ValidationError, match="missing tariff fields"):
        _extraction(fields)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def test_settings_read_prefixed_and_unprefixed_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """App vars use TARIFF_; the Google vars keep the names the SDK expects."""
    monkeypatch.setenv("TARIFF_GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("TARIFF_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key-value")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.gemini_model == "gemini-2.5-flash"
    assert settings.log_level == "DEBUG"
    assert settings.use_vertexai is True
    assert settings.has_api_key


def test_settings_never_expose_the_api_key_in_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SecretStr keeps the key out of logs, reprs and tracebacks."""
    monkeypatch.setenv("GOOGLE_API_KEY", "super-secret-key")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert "super-secret-key" not in repr(settings)
    assert "super-secret-key" not in str(settings.model_dump())
    assert settings.google_api_key is not None
    assert settings.google_api_key.get_secret_value() == "super-secret-key"


def test_missing_api_key_is_valid_so_offline_runs_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No key is a supported mode: demos and tests use the offline path."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert not settings.has_api_key
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    assert not Settings(_env_file=None).has_api_key  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# Field kinds and logging plumbing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("field_id", "expected_kind"),
    [
        ("currency", ValueKind.CURRENCY),
        ("term", ValueKind.TERM),
        ("amount", ValueKind.AMOUNT),
        ("nominal_rate", ValueKind.PERCENT),
        ("effective_rate", ValueKind.PERCENT),
        ("collateral", ValueKind.TEXT),
        ("application_fee", ValueKind.FEE),
        ("disbursement_fee", ValueKind.FEE),
        ("service_fee", ValueKind.FEE),
        ("salary_privileges", ValueKind.TEXT),
    ],
)
def test_each_field_has_the_kind_its_normalizer_expects(
    field_id: str, expected_kind: ValueKind
) -> None:
    """Kind drives normalization and diff magnitude, so a wrong kind is a real bug."""
    assert get_field(field_id).kind is expected_kind


def test_configure_logging_is_idempotent() -> None:
    """Calling it twice must not duplicate every log line."""
    import logging

    configure_logging("INFO")
    configure_logging("DEBUG")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert root.level == logging.DEBUG


def test_armenian_text_survives_json_logging() -> None:
    r"""Logs must stay readable in Armenian, not \u0531-escaped."""
    import logging

    record = logging.LogRecord(
        name="tariff_agent.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="field_extracted",
        args=None,
        exc_info=None,
    )
    record.value = "Անվանական տոկոսադրույք՝ 13,5%"
    line = JsonFormatter().format(record)
    assert "Անվանական" in line
    assert json.loads(line)["value"] == "Անվանական տոկոսադրույք՝ 13,5%"


def test_old_snapshot_missing_a_field_comes_back_as_not_found() -> None:
    """A snapshot written before a field existed must still load and diff.

    This is the migration case that matters in practice: the registry gains an
    eleventh field, and every snapshot already in SQLite lacks it. Loading must
    fill the gap with the explicit NOT_FOUND sentinel - never with a guess, and
    never by refusing to load - so the next diff still has a baseline.
    """
    payload = _stored_payload()
    del payload["fields"]["service_fee"]  # type: ignore[attr-defined]

    restored = TariffExtraction.from_stored(payload)

    backfilled = restored.fields["service_fee"]
    assert backfilled.value == NOT_FOUND
    assert backfilled.status is FieldStatus.NOT_FOUND
    assert backfilled.evidence is None
    assert backfilled.confidence is None
    assert "service_fee" in restored.not_found_field_ids

    # the rest of the snapshot is untouched
    assert set(restored.fields) == set(FIELD_IDS)
    assert restored.fields["nominal_rate"].value == "13,5%"
    assert restored.fields["nominal_rate"].evidence is not None
    assert restored.found_field_ids == ("nominal_rate",)


# --------------------------------------------------------------------------- #
# Per-run log files
# --------------------------------------------------------------------------- #


def test_each_run_writes_its_own_log_file(tmp_path: Path) -> None:
    """One run's audit trail is one file, named by its run id."""
    import logging

    from tariff_agent.observability.logging import configure_logging, run_log_path

    configure_logging("INFO", runs_dir=tmp_path)
    try:
        with run_context("run-aaa1"):
            logging.getLogger("tariff_agent.test").info(
                "document_fetched", extra={"url": "https://acba.am/hy"}
            )
        with run_context("run-bbb2"):
            logging.getLogger("tariff_agent.test").info("product_resolved")

        first = tmp_path / "run-aaa1" / "log.jsonl"
        second = tmp_path / "run-bbb2" / "log.jsonl"
        assert first.exists() and second.exists()
        assert run_log_path("run-aaa1") == first

        lines = [json.loads(line) for line in first.read_text(encoding="utf-8").splitlines()]
        assert [entry["event"] for entry in lines] == ["document_fetched"]
        assert lines[0]["run_id"] == "run-aaa1"
        assert lines[0]["url"] == "https://acba.am/hy"
        assert "run-bbb2" not in first.read_text(encoding="utf-8")
    finally:
        configure_logging("INFO")


def test_file_logging_is_off_until_it_is_armed(tmp_path: Path) -> None:
    """Importing the package must never create directories of its own."""
    import logging

    from tariff_agent.observability.logging import configure_logging, run_log_path

    configure_logging("INFO")
    assert run_log_path("run-none") is None
    with run_context("run-none"):
        logging.getLogger("tariff_agent.test").info("nothing_written")
    assert not any(tmp_path.iterdir())


def test_a_run_log_handler_is_removed_after_the_run(tmp_path: Path) -> None:
    """A finished run must not keep receiving another run's lines."""
    import logging

    from tariff_agent.observability.logging import configure_logging

    configure_logging("INFO", runs_dir=tmp_path)
    try:
        before = len(logging.getLogger().handlers)
        with run_context("run-ccc3"):
            assert len(logging.getLogger().handlers) == before + 1
        assert len(logging.getLogger().handlers) == before
    finally:
        configure_logging("INFO")

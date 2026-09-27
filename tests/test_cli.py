"""Tests for the command line, including the project's first whole-flow test.

``test_a_whole_run_goes_from_a_typed_query_to_a_report_with_evidence`` is the
one that matters: a Russian product name goes in at the top of the CLI and a
rendered Armenian tariff report comes out at the bottom, having gone through
resolution, discovery, fetching, parsing, chunking, retrieval, extraction,
verification, validation, storage and diffing. Every stage below it has its own
tests; none of them proves the stages fit together.

It runs entirely offline: the network is ``httpx.MockTransport`` over the real
trimmed ACBA fixture, and extraction is the deterministic rule-based backend, so
there is no model and no key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from tariff_agent import cli
from tariff_agent.config import (
    HttpSettings,
    get_settings,
    load_allowlist,
    load_discovery_config,
    load_monitoring_config,
    load_products,
)
from tariff_agent.extraction.extractor import RuleBasedExtractor
from tariff_agent.http.client import SafeHttpClient
from tariff_agent.snapshots.review import AutoReviewer, Decision
from tariff_agent.snapshots.store import SnapshotStore

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"
DOCUMENTS = Path(__file__).parent / "fixtures" / "documents"
TARIFF_PDF = "https://www.acba.am/files/loans-tariffs.pdf"
CONSUMER_PAGE = "https://acba.am/hy/individual/loan/consumer-loan--up-to-10mln"
CONSUMER_CATEGORY = "https://acba.am/hy/individual/loans/consumer-loans"
PAGES = {
    CONSUMER_PAGE: "consumer_loan_10mln_page.html",
    CONSUMER_CATEGORY: "consumer_loans_page.html",
}
# The consumer loan's configured seed document. Serving a real PDF here is the
# point of the fixture: PyMuPDF only writes its banner to stdout once it parses
# one, and the HTML-only fixtures hid that from every earlier CLI test.
PDFS = {TARIFF_PDF: "tariff_summary.pdf"}

runner = CliRunner()


def offline_context(
    tmp_path: Path, *, reviewer: Any | None = None, serve_pdfs: bool = True
) -> cli.Context:
    """Build a CLI context with no network and no model.

    Args:
        tmp_path: Where the snapshot database goes.
        reviewer: Who answers review questions, if anyone.
        serve_pdfs: False makes every PDF 404, which is how a product with no
            readable source is simulated.

    Returns:
        The context the commands will use.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in PDFS and serve_pdfs:
            return httpx.Response(
                200,
                content=(DOCUMENTS / PDFS[url]).read_bytes(),
                headers={"content-type": "application/pdf"},
            )
        filename = PAGES.get(url)
        if filename is None:
            return httpx.Response(404)
        return httpx.Response(
            200,
            content=(FIXTURES / filename).read_bytes(),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    allowlist = load_allowlist()
    return cli.Context(
        settings=get_settings(),
        catalog=load_products(),
        allowlist=allowlist,
        discovery=load_discovery_config(),
        monitoring=load_monitoring_config(),
        store=SnapshotStore(tmp_path / "snapshots.db"),
        client=SafeHttpClient(
            allowlist,
            HttpSettings(respect_robots=False, max_attempts=1, cache_dir=tmp_path / "http"),
            transport=httpx.MockTransport(handler),
            sleep=lambda _: None,
        ),
        extractor=RuleBasedExtractor(),
        embedder=None,
        reviewer=reviewer,
    )


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> cli.Context:
    """Install an offline context for every command in the test.

    Args:
        monkeypatch: Pytest's patcher.
        tmp_path: Per-test temporary directory.

    Returns:
        The context, so a test can inspect the store afterwards.
    """
    context = offline_context(tmp_path, reviewer=AutoReviewer(Decision.APPROVED))
    monkeypatch.setattr(cli, "build_context", lambda **_: context)
    return context


# --------------------------------------------------------------------------- #
# The whole flow
# --------------------------------------------------------------------------- #


def test_a_whole_run_goes_from_a_typed_query_to_a_report_with_evidence(
    offline: cli.Context,
) -> None:
    """Query in at the top, rendered report out at the bottom.

    The query is Russian, the product is resolved from a synonym, the document
    is a real ACBA page, and the report is in Armenian with a quote behind every
    value. Nothing is mocked between those two ends except the socket.
    """
    result = runner.invoke(cli.app, ["run", "потребительский кредит"])
    assert result.exit_code in {0, 3}, result.output

    report = result.stdout
    assert "ACBA Bank" in report
    assert "consumer_loan" in report
    assert "Առանց գրավ" in report, "the report must quote the bank's own Armenian"

    stored = offline.store.latest("consumer_loan")
    assert stored is not None, "the run must have stored a snapshot"

    found = {
        field_id: value
        for field_id, value in stored.extraction.fields.items()
        if value.status.value == "found"
    }
    assert found, "the rule-based extractor should read something from the real page"

    for field_id, value in found.items():
        assert value.evidence is not None, f"{field_id} has a value but no evidence"
        assert value.evidence.quote, f"{field_id} has evidence with no quote"
        assert "acba.am" in str(value.evidence.source_url)
        assert value.value in report or value.value[:20] in report

    assert stored.extraction_method == "rule_based"


def test_the_run_reports_its_evidence_in_the_rendered_output(offline: cli.Context) -> None:
    """A report without quotes is a claim; with them it is a citation."""
    result = runner.invoke(cli.app, ["run", "consumer loan"])
    assert result.exit_code in {0, 3}
    assert "evidence:" in result.stdout
    assert "quote:" in result.stdout


def test_json_output_carries_the_values_and_their_quotes(offline: cli.Context) -> None:
    """The machine-readable form must not be thinner than the printed one."""
    import json

    result = runner.invoke(cli.app, ["run", "consumer loan", "--json"])
    assert result.exit_code in {0, 3}
    payload = json.loads(result.stdout)
    assert payload["product_id"] == "consumer_loan"
    assert payload["baseline"] is True
    assert set(payload["fields"]) >= {"nominal_rate", "currency", "term"}
    for value in payload["fields"].values():
        if value["status"] == "found":
            assert value["quote"]
            assert value["document"]


# --------------------------------------------------------------------------- #
# Behaviour of the commands
# --------------------------------------------------------------------------- #


def test_an_unknown_product_exits_with_a_usable_message(offline: cli.Context) -> None:
    """Exit 2 is "you asked for something we do not monitor", not a crash."""
    result = runner.invoke(cli.app, ["run", "business deposit"])
    assert result.exit_code == 2
    assert "business deposit" in result.output or "not" in result.output.lower()


def test_a_second_run_reports_no_change(offline: cli.Context) -> None:
    """The monitor's core claim, through the CLI."""
    first = runner.invoke(cli.app, ["run", "consumer loan"])
    assert first.exit_code in {0, 3}
    second = runner.invoke(cli.app, ["run", "consumer loan", "--json"])
    assert second.exit_code in {0, 3}

    import json

    payload = json.loads(second.stdout)
    assert payload["baseline"] is False
    assert payload["changes"] == []


def test_snapshots_list_shows_what_was_stored(offline: cli.Context) -> None:
    """The history has to be readable without opening SQLite."""
    runner.invoke(cli.app, ["run", "consumer loan"])
    result = runner.invoke(cli.app, ["snapshots", "list", "consumer loan"])
    assert result.exit_code == 0
    assert "consumer_loan" in result.output
    assert "found" in result.output


def test_snapshots_list_says_so_when_there_is_no_history(offline: cli.Context) -> None:
    """An empty store is a first run, not an error."""
    result = runner.invoke(cli.app, ["snapshots", "list"])
    assert result.exit_code == 0
    assert "no snapshots yet" in result.output


def test_snapshots_show_prints_the_evidence(offline: cli.Context) -> None:
    """One snapshot, its values, and where each came from."""
    runner.invoke(cli.app, ["run", "consumer loan"])
    stored = offline.store.latest("consumer_loan")
    assert stored is not None
    result = runner.invoke(cli.app, ["snapshots", "show", str(stored.id)])
    assert result.exit_code == 0
    assert "nominal_rate" in result.output
    assert "←" in result.output


def test_snapshots_show_refuses_an_id_that_does_not_exist(offline: cli.Context) -> None:
    """Exit 2, with the id echoed back."""
    result = runner.invoke(cli.app, ["snapshots", "show", "9999"])
    assert result.exit_code == 2
    assert "9999" in result.output


def test_monitor_runs_every_configured_product(offline: cli.Context) -> None:
    """The scheduled path covers the whole catalogue in one command."""
    import json

    result = runner.invoke(cli.app, ["monitor", "--json"])
    assert result.exit_code in {0, 3}, result.output
    payloads = json.loads(result.stdout)
    assert {payload["product_id"] for payload in payloads} == {"consumer_loan", "mortgage"}
    assert offline.store.latest("consumer_loan") is not None


def test_one_unreadable_product_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A broken PDF must cost one product, not the run."""
    import json

    context = offline_context(tmp_path, serve_pdfs=False)
    monkeypatch.setattr(cli, "build_context", lambda **kw: context)
    result = runner.invoke(cli.app, ["monitor", "--json"])
    assert result.exit_code == 1, "a product that could not be read is a failure"

    payloads = json.loads(result.stdout)
    assert len(payloads) == 2
    assert any("error_type" in payload for payload in payloads)
    assert context.store.latest("consumer_loan") is not None


def test_the_agent_command_refuses_without_a_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The agent needs a model; `run` does not, and the message says so."""
    context = offline_context(tmp_path)
    context.settings.google_api_key = None
    monkeypatch.setattr(cli, "build_context", lambda **_: context)
    result = runner.invoke(cli.app, ["agent", "what is the rate?"])
    assert result.exit_code == 2
    assert "GOOGLE_API_KEY" in result.output

# --------------------------------------------------------------------------- #
# Defects found by running it, not by testing it
# --------------------------------------------------------------------------- #


def test_json_stays_parseable_when_a_pdf_is_parsed(offline: cli.Context) -> None:
    """PyMuPDF writes a banner to stdout the first time it opens a document.

    Every earlier CLI test served HTML only, so the banner never appeared and
    `--json` looked fine while being unparseable in real use. The missing PDF
    fixture was the defect; this is the test that would have caught it.
    """
    import json

    result = runner.invoke(cli.app, ["run", "consumer loan", "--json"])
    assert result.exit_code in {0, 3}, result.output
    payload = json.loads(result.stdout)  # must not raise
    assert payload["product_id"] == "consumer_loan"
    assert "pymupdf" not in result.stdout.lower()


def test_the_report_is_not_polluted_by_library_chatter(offline: cli.Context) -> None:
    """Same guarantee for the human-readable form."""
    result = runner.invoke(cli.app, ["run", "consumer loan"])
    assert result.exit_code in {0, 3}
    assert "pymupdf" not in result.stdout.lower()
    assert result.stdout.lstrip().startswith(("─", "ACBA", "┌", "\n"))


def test_a_run_without_a_terminal_does_not_stop_to_ask(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Piped, in cron or in CI there is nobody to answer a review prompt.

    Before this, `tariff-agent run` built a CliReviewer regardless, read EOF
    from a non-terminal stdin and aborted with exit 1 - so the default command
    died whenever its output was redirected.
    """
    context = offline_context(tmp_path)
    monkeypatch.setattr(cli, "build_context", lambda **kw: context)
    monkeypatch.setattr(cli, "interactive_terminal", lambda: False)
    # Seed a snapshot so the second run has a large change to escalate.
    runner.invoke(cli.app, ["run", "consumer loan"])
    result = runner.invoke(cli.app, ["run", "consumer loan"])
    assert result.exit_code in {0, 3}, result.output
    assert "Aborted" not in result.output
    assert "[a]pprove" not in result.output


def test_an_empty_api_key_is_treated_as_no_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """GOOGLE_API_KEY="" reached the SDK and produced a traceback.

    An empty variable is how a key arrives from a half-filled .env or a CI
    secret that was never set, so it must give the same clear refusal as no
    variable at all.
    """
    from pydantic import SecretStr

    context = offline_context(tmp_path)
    context.settings.google_api_key = SecretStr("")
    monkeypatch.setattr(cli, "build_context", lambda **kw: context)
    result = runner.invoke(cli.app, ["agent", "what is the rate?"])
    assert result.exit_code == 2
    assert "GOOGLE_API_KEY" in result.output
    assert "Traceback" not in result.output

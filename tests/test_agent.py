"""Tests for the agent: the tool contract, the budget, and that it can vary.

The last one is the point of the phase. An agent that calls the same tools in
the same order every time is a script with a language model attached, and a
reviewer is right to ask why it is not written as one. So four scenarios drive a
*scripted model* through the real ADK runner and assert four different tool
sequences, with the cheapest one making no network call at all.

Nothing here touches the network or a real model.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from google.adk.models import BaseLlm

from tariff_agent.agent.session import AgentSession, Budget, SourceRecord, use_session
from tariff_agent.agent.tools import (
    TOOLS,
    diff_against_previous,
    extract_tariffs,
    find_sources,
    get_latest_snapshot,
    request_review,
    resolve_product,
)
from tariff_agent.config import (
    HttpSettings,
    get_settings,
    load_allowlist,
    load_discovery_config,
    load_monitoring_config,
    load_products,
)
from tariff_agent.extraction.extractor import RuleBasedExtractor
from tariff_agent.fields import FIELD_IDS
from tariff_agent.http.client import SafeHttpClient
from tariff_agent.models import Evidence, FieldStatus, FieldValue, TariffExtraction
from tariff_agent.snapshots.review import (
    AutoReviewer,
    Decision,
    ReviewLog,
    ReviewOutcome,
    ReviewRequest,
    ReviewTrigger,
)
from tariff_agent.snapshots.store import SnapshotStatus, SnapshotStore

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"
ALLOWLIST = load_allowlist()
CATALOG = load_products()
DISCOVERY = load_discovery_config()
MONITORING = load_monitoring_config()

CONSUMER_PAGE = "https://acba.am/hy/individual/loan/consumer-loan--up-to-10mln"
CONSUMER_CATEGORY = "https://acba.am/hy/individual/loans/consumer-loans"
PAGES = {
    CONSUMER_PAGE: "consumer_loan_10mln_page.html",
    CONSUMER_CATEGORY: "consumer_loans_page.html",
}


def offline_client(pages: dict[str, str] | None = None) -> SafeHttpClient:
    """Build a client that serves fixture pages and 404s everything else.

    Args:
        pages: URL to fixture filename; defaults to the consumer loan's pages.

    Returns:
        A client backed by ``httpx.MockTransport``.
    """
    served = PAGES if pages is None else pages

    def handler(request: httpx.Request) -> httpx.Response:
        filename = served.get(str(request.url))
        if filename is None:
            return httpx.Response(404)
        return httpx.Response(
            200,
            content=(FIXTURES / filename).read_bytes(),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    return SafeHttpClient(
        ALLOWLIST,
        HttpSettings(respect_robots=False, max_attempts=1, cache_dir=Path("/tmp/no-cache")),
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )


def build_session(
    tmp_path: Path,
    *,
    reviewer: Any | None = None,
    budget: Budget | None = None,
    client: SafeHttpClient | None = None,
) -> AgentSession:
    """Build a session wired entirely to offline parts.

    Args:
        tmp_path: Where the snapshot database goes.
        reviewer: Who answers review questions, if anyone.
        budget: Limits for the run.
        client: Override for the HTTP client.

    Returns:
        The session.
    """
    return AgentSession(
        settings=get_settings(),
        catalog=CATALOG,
        allowlist=ALLOWLIST,
        discovery=DISCOVERY,
        monitoring=MONITORING,
        store=SnapshotStore(tmp_path / "snapshots.db"),
        client=client or offline_client(),
        extractor=RuleBasedExtractor(),
        embedder=None,
        reviewer=reviewer,
        budget=budget,
    )


def stored_extraction(rate: str = "20.1-21.6%") -> TariffExtraction:
    """Build an extraction to seed the store with.

    Args:
        rate: The nominal rate it states.

    Returns:
        A minimal but valid extraction.
    """
    evidence = Evidence(
        document_name="Առանց գրավի սպառողական վարկի հայտ",
        source_url="https://acba.am/hy/individual/loan/consumer-loan--up-to-10mln",
        section="Պայմաններ",
        quote=f"Տարեկան անվանական տոկոսադրույք՝ {rate}",
    )
    fields = {field_id: FieldValue.not_found() for field_id in FIELD_IDS}
    fields["nominal_rate"] = FieldValue(
        value=rate,
        normalized={"min": 20.1, "max": 21.6, "unit": "percent"},
        evidence=evidence,
        status=FieldStatus.FOUND,
    )
    return TariffExtraction(
        bank="ACBA Bank",
        product_id="consumer_loan",
        product_name="Սպառողական վարկ",
        document_name="Առանց գրավի սպառողական վարկի հայտ",
        source_url=CONSUMER_PAGE,
        retrieved_at=datetime.now(UTC),
        checked_at=datetime.now(UTC),
        fields=fields,
        extraction_method="scripted",
    )


# --------------------------------------------------------------------------- #
# The contract every tool keeps
# --------------------------------------------------------------------------- #


def test_every_tool_returns_a_status_and_never_raises(tmp_path: Path) -> None:
    """The model must always receive something it can route around.

    Each tool is called with an id that does not exist, which is the commonest
    way a model gets it wrong.
    """
    session = build_session(tmp_path)
    with use_session(session):
        results = [
            resolve_product("something the bank does not sell"),
            find_sources("no_such_product"),
            get_latest_snapshot("consumer_loan"),
            extract_tariffs("src-999"),
            diff_against_previous("ext-999"),
            request_review("rev-999"),
        ]
    assert all(result["status"] in {"ok", "error", "needs_review"} for result in results)
    assert [result["status"] for result in results[3:]] == ["error", "error", "error"]


def test_a_tool_never_receives_a_url_or_a_path() -> None:
    """The model's reach is bounded by what it is able to say.

    Every tool parameter is an id or the user's own product query. Nothing takes
    a URL, a filesystem path or a query fragment, so a document that asks to be
    fetched from somewhere has no way to express it.
    """
    import inspect
    import typing

    allowed = {"query", "product_id", "source_set_id", "extraction_id", "review_id"}
    for tool in TOOLS:
        parameters = inspect.signature(tool).parameters
        assert set(parameters) <= allowed, tool.__name__
        hints = typing.get_type_hints(tool)
        for name in parameters:
            assert hints[name] is str, f"{tool.__name__}.{name}"


def test_an_unknown_product_is_an_error_not_an_exception(tmp_path: Path) -> None:
    """A product we do not monitor is a normal outcome, reported as one."""
    session = build_session(tmp_path)
    with use_session(session):
        result = resolve_product("business mortgage")
    assert result["status"] == "error"
    assert result["error_type"] == "product_not_found"
    assert "consumer_loan" in result["monitored"]


def test_an_ambiguous_query_asks_rather_than_guesses(tmp_path: Path) -> None:
    """Two plausible products is a question for a person, not a coin toss."""
    session = build_session(tmp_path)
    with use_session(session):
        result = resolve_product("վարկ")
    assert result["status"] in {"needs_review", "error"}
    if result["status"] == "needs_review":
        assert len(result["candidates"]) >= 2


# --------------------------------------------------------------------------- #
# Stop conditions
# --------------------------------------------------------------------------- #


def test_the_budget_stops_a_runaway_agent(tmp_path: Path) -> None:
    """A model that loops must not be able to spend a quota doing it."""
    session = build_session(tmp_path, budget=Budget(max_tool_calls=3))
    with use_session(session):
        for index in range(3):
            assert get_latest_snapshot(f"consumer_loan{'' if index == 0 else index}")["status"]
        refused = get_latest_snapshot("mortgage")
    assert refused["status"] == "error"
    assert refused["error_type"] == "budget_exhausted"
    assert session.metrics.stop_reason == "budget_exhausted"


def test_repeating_a_call_is_refused_as_no_progress(tmp_path: Path) -> None:
    """The same call with the same arguments cannot produce a new answer."""
    session = build_session(tmp_path)
    with use_session(session):
        first = get_latest_snapshot("consumer_loan")
        second = get_latest_snapshot("consumer_loan")
    assert first["status"] == "ok"
    assert second["status"] == "error"
    assert second["error_type"] == "no_progress"


def test_the_time_limit_stops_a_slow_run(tmp_path: Path) -> None:
    """Wall clock is the backstop for work that is slow rather than looping."""
    session = build_session(tmp_path, budget=Budget(max_seconds=-1.0))
    with use_session(session):
        result = get_latest_snapshot("consumer_loan")
    assert result["error_type"] == "time_exhausted"


def test_sessions_do_not_share_ids_or_budgets(tmp_path: Path) -> None:
    """Two runs in one process must not see each other's state."""
    first = build_session(tmp_path / "a")
    second = build_session(tmp_path / "b")
    with use_session(first):
        assert first.mint("src") == "src-1"
        get_latest_snapshot("consumer_loan")
    with use_session(second):
        assert second.mint("src") == "src-1"
    assert first.calls_made == 1
    assert second.calls_made == 0


# --------------------------------------------------------------------------- #
# Reading what we already know
# --------------------------------------------------------------------------- #


def test_an_empty_store_is_reported_as_no_history(tmp_path: Path) -> None:
    """A first run is not an error."""
    session = build_session(tmp_path)
    with use_session(session):
        result = get_latest_snapshot("consumer_loan")
    assert result == {"status": "ok", "found": False, "product_id": "consumer_loan"}


def test_a_stored_snapshot_comes_back_with_its_age_and_evidence(tmp_path: Path) -> None:
    """Enough to answer from, and enough to judge whether it is fresh enough."""
    session = build_session(tmp_path)
    session.store.save(stored_extraction(), run_id="r1", doc_id="d" * 64)
    with use_session(session):
        result = get_latest_snapshot("consumer_loan")
    assert result["found"] is True
    assert result["age_hours"] < 1
    rate = next(field for field in result["fields"] if field["field_id"] == "nominal_rate")
    assert rate["value"] == "20.1-21.6%"
    assert "20.1-21.6%" in rate["quote"]


# --------------------------------------------------------------------------- #
# Review, and the memory behind it
# --------------------------------------------------------------------------- #


def review_request() -> ReviewRequest:
    """Build one large-change question.

    Returns:
        The request.
    """
    return ReviewRequest(
        trigger=ReviewTrigger.LARGE_CHANGE,
        product_id="consumer_loan",
        subject="change:nominal_rate:8.0%->20.1-21.6%",
        question="Անվանական տոկոսադրույք: 8.0% → 20.1-21.6% — is this correct?",
    )


def test_a_decision_given_earlier_is_not_asked_again(tmp_path: Path) -> None:
    """The agent path must not reopen the gap the scheduled path closed.

    A reviewer asked the same question every run stops reading it, so a
    remembered answer has to come back without anyone being disturbed - even
    when the question is reached through the agent rather than the pipeline.
    """
    session = build_session(tmp_path, reviewer=AutoReviewer(Decision.REJECTED))
    request = review_request()
    ReviewLog(session.store).record(
        request, ReviewOutcome(decision=Decision.APPROVED, decided_by="levon")
    )
    snapshot_id = session.store.save(stored_extraction(), run_id="r1", doc_id="d" * 64)
    session.reviews["rev-1"] = (request, snapshot_id)

    with use_session(session):
        result = request_review("rev-1")

    assert result["decision"] == "approved", "the stored answer, not the reviewer's"
    assert result["remembered"] is True
    assert result["decided_by"] == "levon"
    assert session.metrics.reviews_asked == 0
    assert session.metrics.reviews_remembered == 1


def test_a_new_question_reaches_the_reviewer(tmp_path: Path) -> None:
    """Remembering must not mean never asking."""
    session = build_session(tmp_path, reviewer=AutoReviewer(Decision.APPROVED))
    snapshot_id = session.store.save(stored_extraction(), run_id="r1", doc_id="d" * 64)
    session.reviews["rev-1"] = (review_request(), snapshot_id)
    with use_session(session):
        result = request_review("rev-1")
    assert result["remembered"] is False
    assert result["decision"] == "approved"
    assert result["snapshot_status"] == SnapshotStatus.CONFIRMED.value
    assert session.metrics.reviews_asked == 1


def test_without_a_reviewer_the_question_stays_open(tmp_path: Path) -> None:
    """An unattended run records the question rather than deciding for itself."""
    session = build_session(tmp_path, reviewer=None)
    snapshot_id = session.store.save(
        stored_extraction(), run_id="r1", doc_id="d" * 64, status=SnapshotStatus.PENDING_REVIEW
    )
    session.reviews["rev-1"] = (review_request(), snapshot_id)
    with use_session(session):
        result = request_review("rev-1")
    assert result["status"] == "needs_review"
    assert session.store.latest("consumer_loan") is not None


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def test_metrics_report_failures_completeness_and_hitl(tmp_path: Path) -> None:
    """Requirement 5.13's numbers come out of one run without recomputation."""
    session = build_session(tmp_path, reviewer=AutoReviewer(Decision.APPROVED))
    snapshot_id = session.store.save(stored_extraction(), run_id="r1", doc_id="d" * 64)
    session.reviews["rev-1"] = (review_request(), snapshot_id)
    with use_session(session):
        extract_tariffs("src-nope")
        get_latest_snapshot("consumer_loan")
        request_review("rev-1")

    session.metrics.fields_required = 10
    session.metrics.fields_found = 8
    payload = session.metrics.as_dict()
    assert payload["tool_calls"] == 3
    assert payload["tool_failures"] == 1
    assert payload["tool_failure_rate"] == pytest.approx(0.333, abs=0.001)
    assert payload["completeness"] == 0.8
    assert payload["hitl_rate"] == 1.0
    assert set(payload["per_tool_s"]) == {
        "extract_tariffs",
        "get_latest_snapshot",
        "request_review",
    }


def test_completeness_is_absent_when_nothing_was_extracted() -> None:
    """Zero would claim the run looked and found nothing; it never looked."""
    from tariff_agent.agent.metrics import RunMetrics

    metrics = RunMetrics()
    assert metrics.as_dict()["completeness"] is None
    metrics.fields_required, metrics.fields_found = 10, 9
    assert metrics.completeness == 0.9


def test_token_usage_is_absent_rather_than_estimated() -> None:
    """A number nobody reported is worse than no number."""
    from tariff_agent.agent.metrics import RunMetrics

    metrics = RunMetrics()
    assert metrics.as_dict()["total_tokens"] is None
    metrics.add_tokens(100, 20, 120)
    metrics.add_tokens(None, None, 80)
    assert metrics.total_tokens == 200
    assert metrics.prompt_tokens == 100


# --------------------------------------------------------------------------- #
# The agent genuinely varies
# --------------------------------------------------------------------------- #


class ScriptedLlm(BaseLlm):
    """A model that returns prepared tool calls, then a final sentence.

    Subclasses ADK's own base class so the real runner executes the calls: the
    sequences asserted below are the sequences the runner ran, not a list this
    test wrote down.

    Attributes:
        turns: Each entry is ``("call", name, args)`` or ``("text", answer)``.
        seen_instruction: The system instruction ADK sent, kept so a test can
            assert what the agent was told.
    """

    model: str = "scripted"
    turns: list[Any] = []
    seen_instruction: str = ""

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[Any, None]:
        """Yield the next scripted response.

        Args:
            llm_request: What ADK would have sent; its instruction is recorded.
            stream: Ignored.

        Yields:
            One ``LlmResponse``.
        """
        from google.adk.models import LlmResponse
        from google.genai import types

        self.seen_instruction = str(getattr(llm_request.config, "system_instruction", ""))
        if not self.turns:
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="done")]))
            return
        turn = self.turns.pop(0)
        if turn[0] == "call":
            part = types.Part(function_call=types.FunctionCall(name=turn[1], args=dict(turn[2])))
        else:
            part = types.Part(text=turn[1])
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


def drive(session: AgentSession, turns: list[tuple[str, ...]]) -> Any:
    """Run the agent with a scripted model.

    Args:
        session: The run's state.
        turns: The script.

    Returns:
        The :class:`~tariff_agent.agent.agent.AgentRun`.
    """
    import asyncio

    from tariff_agent.agent.agent import run_agent_async

    return asyncio.run(run_agent_async("q", session, model=ScriptedLlm(turns=list(turns))))  # type: ignore[arg-type]


def test_a_fresh_snapshot_is_answered_without_touching_the_bank(tmp_path: Path) -> None:
    """The cheapest path, and the one that proves the agent is not a script.

    A question about a rate read minutes ago needs no discovery, no download and
    no model call for extraction. The client is one that 404s everything, so if
    the agent fetched anything this test would fail.
    """
    session = build_session(tmp_path, client=offline_client({}))
    session.store.save(stored_extraction(), run_id="r1", doc_id="d" * 64)
    result = drive(
        session,
        [
            ("call", "resolve_product", {"query": "consumer loan"}),
            ("call", "get_latest_snapshot", {"product_id": "consumer_loan"}),
            ("text", "Տարեկան անվանական տոկոսադրույք՝ 20.1-21.6%"),
        ],
    )
    assert result.tool_sequence == ["resolve_product", "get_latest_snapshot"]
    assert "20.1-21.6%" in result.answer
    assert result.metrics.model_calls == 0


def test_no_snapshot_drives_the_full_path(tmp_path: Path) -> None:
    """With nothing stored, the agent has to read the bank."""
    session = build_session(tmp_path)
    result = drive(
        session,
        [
            ("call", "resolve_product", {"query": "consumer loan"}),
            ("call", "get_latest_snapshot", {"product_id": "consumer_loan"}),
            ("call", "find_sources", {"product_id": "consumer_loan"}),
            ("call", "extract_tariffs", {"source_set_id": "src-1"}),
            ("call", "diff_against_previous", {"extraction_id": "ext-1"}),
            ("text", "Read from the bank and stored."),
        ],
    )
    assert result.tool_sequence == [
        "resolve_product",
        "get_latest_snapshot",
        "find_sources",
        "extract_tariffs",
        "diff_against_previous",
    ]
    assert session.store.latest("consumer_loan") is not None


def test_an_ambiguous_product_stops_before_anything_is_fetched(tmp_path: Path) -> None:
    """The shortest path of all: one tool, then a question."""
    session = build_session(tmp_path, client=offline_client({}))
    result = drive(
        session,
        [
            ("call", "resolve_product", {"query": "վարկ"}),
            ("text", "Which product do you mean?"),
        ],
    )
    assert result.tool_sequence == ["resolve_product"]
    assert "Which product" in result.answer


def test_a_large_change_adds_a_review_step(tmp_path: Path) -> None:
    """The longest path, and the only one that involves a person."""
    session = build_session(tmp_path, reviewer=AutoReviewer(Decision.APPROVED))
    session.store.save(stored_extraction(rate="8.0%"), run_id="r0", doc_id="d" * 64)
    result = drive(
        session,
        [
            ("call", "resolve_product", {"query": "consumer loan"}),
            ("call", "find_sources", {"product_id": "consumer_loan"}),
            ("call", "extract_tariffs", {"source_set_id": "src-1"}),
            ("call", "diff_against_previous", {"extraction_id": "ext-1"}),
            ("call", "request_review", {"review_id": "rev-1"}),
            ("text", "The rate moved and a reviewer confirmed it."),
        ],
    )
    assert "request_review" in result.tool_sequence
    assert len(result.tool_sequence) == 5


def test_the_agent_is_told_not_to_report_another_products_terms(tmp_path: Path) -> None:
    """The scope leak was a real failure; the guard belongs in both prompts."""
    session = build_session(tmp_path, client=offline_client({}))
    llm = ScriptedLlm(turns=[("text", "hello")])
    import asyncio

    from tariff_agent.agent.agent import run_agent_async

    asyncio.run(run_agent_async("q", session, model=llm))  # type: ignore[arg-type]
    assert "shared price lists" in llm.seen_instruction
    assert "another product's terms" in llm.seen_instruction
    assert "NOT_FOUND" in llm.seen_instruction


def test_the_four_scenarios_take_four_different_paths(tmp_path: Path) -> None:
    """Stated as one assertion, because it is the phase's whole claim."""
    sequences = set()

    session = build_session(tmp_path / "1", client=offline_client({}))
    session.store.save(stored_extraction(), run_id="r1", doc_id="d" * 64)
    sequences.add(
        tuple(
            drive(
                session,
                [
                    ("call", "resolve_product", {"query": "consumer loan"}),
                    ("call", "get_latest_snapshot", {"product_id": "consumer_loan"}),
                    ("text", "20.1-21.6%"),
                ],
            ).tool_sequence
        )
    )

    session = build_session(tmp_path / "2", client=offline_client({}))
    sequences.add(
        tuple(
            drive(
                session,
                [("call", "resolve_product", {"query": "վարկ"}), ("text", "which?")],
            ).tool_sequence
        )
    )

    session = build_session(tmp_path / "3")
    sequences.add(
        tuple(
            drive(
                session,
                [
                    ("call", "resolve_product", {"query": "consumer loan"}),
                    ("call", "find_sources", {"product_id": "consumer_loan"}),
                    ("call", "extract_tariffs", {"source_set_id": "src-1"}),
                    ("call", "diff_against_previous", {"extraction_id": "ext-1"}),
                    ("text", "stored"),
                ],
            ).tool_sequence
        )
    )

    session = build_session(tmp_path / "4", reviewer=AutoReviewer(Decision.APPROVED))
    session.store.save(stored_extraction(rate="8.0%"), run_id="r0", doc_id="d" * 64)
    sequences.add(
        tuple(
            drive(
                session,
                [
                    ("call", "resolve_product", {"query": "consumer loan"}),
                    ("call", "find_sources", {"product_id": "consumer_loan"}),
                    ("call", "extract_tariffs", {"source_set_id": "src-1"}),
                    ("call", "diff_against_previous", {"extraction_id": "ext-1"}),
                    ("call", "request_review", {"review_id": "rev-1"}),
                    ("text", "confirmed"),
                ],
            ).tool_sequence
        )
    )

    assert len(sequences) == 4, sequences
    assert min(len(sequence) for sequence in sequences) == 1

# --------------------------------------------------------------------------- #
# The `adk web` surface
# --------------------------------------------------------------------------- #


def test_a_budget_is_per_turn_not_per_process(tmp_path: Path) -> None:
    """Behind `adk web` one session serves every conversation.

    Without a per-turn reset the twelfth tool call anywhere killed the server
    for good, and the no-progress detector refused a question because somebody
    else had already asked it. Found by running three conversations through
    `adk web` and watching the third fail.
    """
    session = build_session(tmp_path, budget=Budget(max_tool_calls=2))
    with use_session(session):
        assert get_latest_snapshot("consumer_loan")["status"] == "ok"
        assert get_latest_snapshot("mortgage")["status"] == "ok"
        exhausted = get_latest_snapshot("consumer_loan")
        assert exhausted["error_type"] in {"budget_exhausted", "no_progress"}

        session.begin_turn()

        revived = get_latest_snapshot("consumer_loan")
        assert revived["status"] == "ok", "a new turn gets a new budget"
        assert session.calls_made == 1
        assert session.metrics.stop_reason == "completed"


def test_a_new_turn_keeps_the_ids_an_earlier_turn_minted(tmp_path: Path) -> None:
    """A follow-up question may still refer to `src-1`."""
    session = build_session(tmp_path)
    session.sources["src-1"] = SourceRecord(product_id="consumer_loan", retriever=None)  # type: ignore[arg-type]
    session.begin_turn()
    assert "src-1" in session.sources


def test_the_package_exposes_the_agent_adk_looks_for() -> None:
    """`adk web src` imports this and looks for `root_agent`.

    Asserted because the name is a contract with a tool outside this codebase:
    rename it and the web UI silently stops discovering the agent.
    """
    from tariff_agent.agent import root_agent

    assert root_agent.name == "acba_tariff_agent"
    assert {tool.__name__ for tool in root_agent.tools} == {
        "resolve_product",
        "find_sources",
        "get_latest_snapshot",
        "extract_tariffs",
        "diff_against_previous",
        "request_review",
    }


def test_tools_work_without_a_session_being_installed(tmp_path: Path) -> None:
    """`adk web` never calls use_session, so the tools must still function.

    The default is built once from the same configuration the CLI uses, and it
    attaches no reviewer: nobody is at a terminal behind a web UI, so a question
    is recorded and reported rather than asked.
    """
    from tariff_agent.agent.session import default_session, reset_default_session

    reset_default_session()
    try:
        result = resolve_product("consumer loan")
        assert result["status"] == "ok"
        assert result["product_id"] == "consumer_loan"
        assert default_session().reviewer is None
    finally:
        reset_default_session()

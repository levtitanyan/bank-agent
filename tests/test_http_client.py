"""Tests for the guarded HTTP client.

Every test runs against ``httpx.MockTransport``: the suite never touches the
network, and the retry tests use an injected sleep so they never actually wait.

The transport call count is asserted deliberately in several places - "did it
retry?" and "did it revalidate?" are the properties that matter here, and they
are invisible from the return value alone.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tariff_agent.config import Allowlist, HttpSettings
from tariff_agent.errors import (
    DomainNotAllowedError,
    FetchError,
    HttpStatusError,
    ResponseTooLargeError,
    UnexpectedContentTypeError,
)
from tariff_agent.http.client import ContentKind, SafeHttpClient

ALLOWLIST = Allowlist(allowed_schemes=("https",), domains=("acba.am", "www.acba.am"))
PDF_URL = "https://www.acba.am/files/loans-tariffs.pdf"
PAGE_URL = "https://acba.am/hy/individual/loans/consumer-loans"
PDF_BYTES = b"%PDF-1.7\n" + b"x" * 200
HTML_BYTES = "<html><body>Անվանական տոկոսադրույք՝ 13,5%</body></html>".encode()


class Recorder:
    """A MockTransport handler that records calls and replays queued responses."""

    def __init__(self, *responses: httpx.Response) -> None:
        """Queue the responses to return, in order.

        Args:
            *responses: Returned one per call; the last one repeats.
        """
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record the request and return the next queued response."""
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        return self.responses[index]

    @property
    def calls(self) -> int:
        """How many requests reached the transport."""
        return len(self.requests)


@pytest.fixture
def settings(tmp_path: Path) -> HttpSettings:
    """Fast, isolated settings: tiny backoff, no robots, cache in tmp_path."""
    return HttpSettings(
        max_attempts=3,
        backoff_base=0.01,
        backoff_max=0.05,
        max_download_bytes=1024,
        respect_robots=False,
        cache_dir=tmp_path / "cache",
    )


def build(
    recorder: Recorder, settings: HttpSettings, slept: list[float] | None = None
) -> SafeHttpClient:
    """Build a client wired to a recorder and a non-sleeping sleep."""
    return SafeHttpClient(
        ALLOWLIST,
        settings,
        transport=httpx.MockTransport(recorder),
        sleep=(slept.append if slept is not None else lambda _: None),
    )


def pdf_response(**kwargs: object) -> httpx.Response:
    """A well-formed PDF response."""
    headers = {"content-type": "application/pdf"}
    headers.update(kwargs.pop("headers", {}))  # type: ignore[arg-type]
    return httpx.Response(200, content=PDF_BYTES, headers=headers, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #


def test_fetches_a_pdf_and_reports_its_hash(settings: HttpSettings) -> None:
    """A normal download returns bytes, type, size and a content hash."""
    rec = Recorder(pdf_response())
    result = build(rec, settings).fetch(PDF_URL, expect=ContentKind.PDF)
    assert result.content == PDF_BYTES
    assert result.content_type == "application/pdf"
    assert result.size == len(PDF_BYTES)
    assert len(result.sha256) == 64
    assert result.from_cache is False
    assert rec.calls == 1


def test_sends_a_truthful_user_agent(settings: HttpSettings) -> None:
    """The bank can identify our traffic; we do not disguise it."""
    rec = Recorder(pdf_response())
    build(rec, settings).fetch(PDF_URL)
    assert "ACBA-Tariff-Monitor" in rec.requests[0].headers["user-agent"]


def test_fetch_text_decodes_armenian(settings: HttpSettings) -> None:
    """Armenian must survive decoding, or every later stage is garbage."""
    rec = Recorder(httpx.Response(200, content=HTML_BYTES, headers={"content-type": "text/html"}))
    text = build(rec, settings).fetch_text(PAGE_URL)
    assert "Անվանական տոկոսադրույք՝ 13,5%" in text


# --------------------------------------------------------------------------- #
# Status handling and retries
# --------------------------------------------------------------------------- #


def test_404_is_not_retried(settings: HttpSettings) -> None:
    """A missing document is an answer, not a hiccup."""
    rec = Recorder(httpx.Response(404))
    with pytest.raises(HttpStatusError) as exc:
        build(rec, settings).fetch(PDF_URL)
    assert exc.value.status_code == 404
    assert rec.calls == 1


def test_403_is_not_retried_and_is_logged_as_access_blocked(
    settings: HttpSettings, caplog: pytest.LogCaptureFixture
) -> None:
    """An access decision is respected, not hammered."""
    rec = Recorder(httpx.Response(403))
    with caplog.at_level("WARNING"), pytest.raises(HttpStatusError) as exc:
        build(rec, settings).fetch(PDF_URL)
    assert exc.value.status_code == 403
    assert rec.calls == 1
    assert any(r.message == "access_blocked" for r in caplog.records)


def test_transient_server_errors_are_retried_then_succeed(settings: HttpSettings) -> None:
    """500, 500, 200 succeeds on the third attempt with two backoffs."""
    slept: list[float] = []
    rec = Recorder(httpx.Response(500), httpx.Response(500), pdf_response())
    result = build(rec, settings, slept).fetch(PDF_URL, expect=ContentKind.PDF)
    assert result.content == PDF_BYTES
    assert rec.calls == 3
    assert len(slept) == 2
    assert slept[1] > slept[0]  # exponential


def test_retries_are_bounded(settings: HttpSettings) -> None:
    """Persistent failure stops after max_attempts and raises, never fabricates."""
    slept: list[float] = []
    rec = Recorder(httpx.Response(503))
    with pytest.raises(FetchError, match="after 3 attempts"):
        build(rec, settings, slept).fetch(PDF_URL)
    assert rec.calls == 3
    assert len(slept) == 2


def test_timeouts_are_retried(settings: HttpSettings) -> None:
    """A read timeout is transient, so it is worth another attempt."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    slept: list[float] = []
    client = SafeHttpClient(
        ALLOWLIST, settings, transport=httpx.MockTransport(handler), sleep=slept.append
    )
    with pytest.raises(FetchError, match="after 3 attempts"):
        client.fetch(PDF_URL)
    assert len(slept) == 2


def test_retry_after_is_honoured_but_capped(settings: HttpSettings) -> None:
    """A server hint is respected, but must not stall the run for minutes."""
    slept: list[float] = []
    rec = Recorder(
        httpx.Response(429, headers={"retry-after": "600"}),
        pdf_response(),
    )
    build(rec, settings, slept).fetch(PDF_URL, expect=ContentKind.PDF)
    assert slept == [settings.backoff_max]


# --------------------------------------------------------------------------- #
# Redirects
# --------------------------------------------------------------------------- #


def test_redirect_within_the_allowlist_is_followed(settings: HttpSettings) -> None:
    """acba.am -> www.acba.am is legitimate and is recorded in the result."""
    rec = Recorder(
        httpx.Response(301, headers={"location": PDF_URL}),
        pdf_response(),
    )
    result = build(rec, settings).fetch(
        "https://acba.am/files/loans-tariffs.pdf", expect=ContentKind.PDF
    )
    assert result.requested_url == "https://acba.am/files/loans-tariffs.pdf"
    assert result.url == PDF_URL
    assert rec.calls == 2


def test_redirect_off_the_allowlist_is_refused(settings: HttpSettings) -> None:
    """The whole point of manual redirects: a hop off-domain must not be followed."""
    rec = Recorder(httpx.Response(302, headers={"location": "https://evil.com/x.pdf"}))
    with pytest.raises(DomainNotAllowedError, match="evil.com"):
        build(rec, settings).fetch(PDF_URL)
    assert rec.calls == 1  # the second request was never made


def test_redirect_loops_are_bounded(settings: HttpSettings) -> None:
    """A redirect cycle ends in an error, not a hang."""
    rec = Recorder(httpx.Response(302, headers={"location": PDF_URL}))
    with pytest.raises(FetchError, match="more than 5 redirects"):
        build(rec, settings).fetch(PDF_URL)
    assert rec.calls == settings.max_redirects + 1


# --------------------------------------------------------------------------- #
# Size and content-type guards
# --------------------------------------------------------------------------- #


def test_oversize_body_is_aborted(settings: HttpSettings) -> None:
    """The cap is enforced on bytes received, not on the declared length."""
    rec = Recorder(
        httpx.Response(
            200, content=b"%PDF-" + b"x" * 5000, headers={"content-type": "application/pdf"}
        )
    )
    with pytest.raises(ResponseTooLargeError):
        build(rec, settings).fetch(PDF_URL, expect=ContentKind.PDF)


def test_oversize_content_length_is_refused_before_reading(settings: HttpSettings) -> None:
    """An honest server declaring a huge file saves us the download."""
    rec = Recorder(
        httpx.Response(
            200,
            content=PDF_BYTES,
            headers={"content-type": "application/pdf", "content-length": "99999999"},
        )
    )
    with pytest.raises(ResponseTooLargeError, match="declares"):
        build(rec, settings).fetch(PDF_URL, expect=ContentKind.PDF)


def test_html_served_as_pdf_is_rejected(settings: HttpSettings) -> None:
    """A 404 page mislabelled as a PDF must not reach the parser."""
    rec = Recorder(
        httpx.Response(
            200, content=b"<html>not found</html>", headers={"content-type": "application/pdf"}
        )
    )
    with pytest.raises(UnexpectedContentTypeError, match="PDF signature"):
        build(rec, settings).fetch(PDF_URL, expect=ContentKind.PDF)


def test_pdf_served_where_a_page_was_expected_is_rejected(settings: HttpSettings) -> None:
    """The header is checked too, so the kinds cannot be swapped."""
    rec = Recorder(pdf_response())
    with pytest.raises(UnexpectedContentTypeError, match="expected an HTML page"):
        build(rec, settings).fetch(PAGE_URL, expect=ContentKind.HTML)


def test_html_with_a_bom_and_leading_whitespace_is_accepted(settings: HttpSettings) -> None:
    """Real pages start with a BOM or blank lines; that is not a reason to fail."""
    body = b"\xef\xbb\xbf\n\n  <!DOCTYPE html><html></html>"
    rec = Recorder(
        httpx.Response(200, content=body, headers={"content-type": "text/html; charset=utf-8"})
    )
    assert build(rec, settings).fetch(PAGE_URL, expect=ContentKind.HTML).size == len(body)


# --------------------------------------------------------------------------- #
# Caching: revalidate, never short-circuit
# --------------------------------------------------------------------------- #


def test_second_fetch_revalidates_and_reuses_cached_bytes_on_304(
    settings: HttpSettings,
) -> None:
    """A monitoring run always asks the server; 304 means 'nothing changed'."""
    rec = Recorder(
        pdf_response(headers={"etag": '"v1"', "last-modified": "Wed, 01 Jan 2025 00:00:00 GMT"}),
        httpx.Response(304),
    )
    client = build(rec, settings)
    first = client.fetch(PDF_URL, expect=ContentKind.PDF)
    second = client.fetch(PDF_URL, expect=ContentKind.PDF)

    assert rec.calls == 2, "the cache must not skip the network"
    assert rec.requests[1].headers["if-none-match"] == '"v1"'
    assert rec.requests[1].headers["if-modified-since"] == "Wed, 01 Jan 2025 00:00:00 GMT"
    assert second.from_cache is True
    assert second.content == first.content
    assert second.sha256 == first.sha256


def test_changed_document_is_downloaded_again(settings: HttpSettings) -> None:
    """When the bank publishes a new edition, we must see the new bytes."""
    new_bytes = b"%PDF-1.7\nnew edition"
    rec = Recorder(
        pdf_response(headers={"etag": '"v1"'}),
        httpx.Response(
            200,
            content=new_bytes,
            headers={"content-type": "application/pdf", "etag": '"v2"'},
        ),
    )
    client = build(rec, settings)
    first = client.fetch(PDF_URL, expect=ContentKind.PDF)
    second = client.fetch(PDF_URL, expect=ContentKind.PDF)
    assert second.from_cache is False
    assert second.content == new_bytes
    assert second.sha256 != first.sha256


def test_offline_mode_serves_the_cache_without_any_request(tmp_path: Path) -> None:
    """Demos and tests can run with no network at all."""
    online = HttpSettings(respect_robots=False, cache_dir=tmp_path / "cache")
    rec = Recorder(pdf_response())
    build(rec, online).fetch(PDF_URL, expect=ContentKind.PDF)

    offline = HttpSettings(respect_robots=False, offline=True, cache_dir=tmp_path / "cache")
    rec2 = Recorder(pdf_response())
    result = build(rec2, offline).fetch(PDF_URL, expect=ContentKind.PDF)
    assert result.from_cache is True
    assert result.content == PDF_BYTES
    assert rec2.calls == 0


def test_offline_without_a_cached_copy_fails_honestly(tmp_path: Path) -> None:
    """Missing data is reported, never invented."""
    offline = HttpSettings(respect_robots=False, offline=True, cache_dir=tmp_path / "cache")
    rec = Recorder(pdf_response())
    with pytest.raises(FetchError, match="offline mode and no cached copy"):
        build(rec, offline).fetch(PDF_URL)


# --------------------------------------------------------------------------- #
# Policy is applied before anything else
# --------------------------------------------------------------------------- #


def test_off_allowlist_url_never_opens_a_connection(settings: HttpSettings) -> None:
    """Policy runs before the socket, so a blocked host is never contacted."""
    rec = Recorder(pdf_response())
    with pytest.raises(DomainNotAllowedError):
        build(rec, settings).fetch("https://evil.com/x.pdf")
    assert rec.calls == 0


def test_plain_http_is_refused(settings: HttpSettings) -> None:
    """Only https is permitted: evidence must not arrive over a modifiable channel."""
    rec = Recorder(pdf_response())
    with pytest.raises(DomainNotAllowedError):
        build(rec, settings).fetch("http://acba.am/hy")
    assert rec.calls == 0


def test_validators_survive_a_permanent_redirect(settings: HttpSettings) -> None:
    """ACBA 301s www.acba.am to acba.am; losing the ETag there re-downloads 1 MB.

    The validators must be re-attached after the hop, but only because the
    target is exactly the URL the cached copy was served from last time.
    """
    www_url = "https://www.acba.am/files/loans-tariffs.pdf"
    canonical = "https://acba.am/files/loans-tariffs.pdf"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "www.acba.am":
            return httpx.Response(301, headers={"location": canonical})
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(
            200, content=PDF_BYTES, headers={"content-type": "application/pdf", "etag": '"v1"'}
        )

    requests: list[httpx.Request] = []
    client = SafeHttpClient(
        ALLOWLIST, settings, transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    first = client.fetch(www_url, expect=ContentKind.PDF)
    second = client.fetch(www_url, expect=ContentKind.PDF)

    assert first.from_cache is False
    assert second.from_cache is True, "the ETag was lost across the redirect"
    assert second.content == PDF_BYTES
    assert requests[-1].headers.get("if-none-match") == '"v1"'


def test_validators_are_not_sent_to_a_different_resource(settings: HttpSettings) -> None:
    """A 304 from another document would say nothing about the one we cached."""
    rec = Recorder(
        pdf_response(headers={"etag": '"v1"'}),
        httpx.Response(302, headers={"location": "https://acba.am/files/other.pdf"}),
        pdf_response(),
    )
    client = build(rec, settings)
    client.fetch(PDF_URL, expect=ContentKind.PDF)
    client.fetch(PDF_URL, expect=ContentKind.PDF)
    assert "if-none-match" not in rec.requests[-1].headers

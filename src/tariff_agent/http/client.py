"""The guarded HTTP client: the single network door of this project.

What it guarantees to everything downstream:

* the URL, **and every redirect hop**, is on the allow-list and https;
* no more than :attr:`HttpSettings.max_download_bytes` is ever read, enforced
  while streaming rather than by trusting ``Content-Length``;
* the bytes are the kind of document the caller asked for, checked against both
  the ``Content-Type`` header and the leading magic bytes;
* failures are retried only when retrying can help, a bounded number of times;
* a failure is never replaced by fabricated content.

**Caching and monitoring.** A tariff monitor that serves a cached PDF without
asking the server would never notice a new edition, so the cache is not a
short-circuit: every fetch contacts the server and sends ``If-None-Match`` /
``If-Modified-Since`` from the stored metadata. A ``304 Not Modified`` reuses the
cached bytes - one cheap round-trip, no re-download, and a positive statement
from the bank that nothing changed. Only explicit offline mode skips the network.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Protocol, Self

import httpx

from tariff_agent.config import Allowlist, HttpSettings
from tariff_agent.errors import (
    FetchError,
    HttpStatusError,
    ResponseTooLargeError,
    UnexpectedContentTypeError,
)
from tariff_agent.http.url_policy import assert_allowed
from tariff_agent.observability.logging import get_logger

logger = get_logger(__name__)

_CHUNK_SIZE: Final[int] = 64 * 1024
_RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({408, 425, 429, 500, 502, 503, 504})
"""Statuses where the same request may succeed later.

Note that 403 is absent: it is an access decision, not a hiccup. Retrying it
would be hammering a door the bank has closed.
"""

_BOM: Final[bytes] = b"\xef\xbb\xbf"


class RobotsChecker(Protocol):
    """The slice of robots.txt handling the client depends on.

    A Protocol rather than an import of the concrete class, so the client does
    not depend on the module that depends on it.
    """

    def assert_allowed(self, url: str) -> None:
        """Raise if the site's rules forbid fetching ``url``."""
        ...


class ContentKind(StrEnum):
    """What the caller expects to receive, so a mismatch fails here."""

    PDF = "pdf"
    HTML = "html"
    ANY = "any"


@dataclass(frozen=True, slots=True)
class FetchResult:
    """One successfully retrieved document.

    Attributes:
        url: Final URL, after any redirects.
        requested_url: URL originally asked for. Differs from ``url`` when the
            bank redirected us, which the evidence trail should record.
        content: The raw bytes.
        content_type: Content type reported by the server, without parameters.
        sha256: Hash of ``content``. Phase 5 uses it to skip re-indexing a
            document whose bytes have not changed.
        size: Length of ``content`` in bytes.
        retrieved_at: When the request completed (UTC), or when the cached copy
            was originally stored for a 304.
        from_cache: True when the server answered 304 and cached bytes were
            reused, or when running offline.
    """

    url: str
    requested_url: str
    content: bytes
    content_type: str
    sha256: str
    size: int
    retrieved_at: datetime
    from_cache: bool


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    """A cached document plus the validators needed to revalidate it."""

    meta: dict[str, Any]
    content: bytes

    @property
    def etag(self) -> str | None:
        """The stored ``ETag``, if the server provided one."""
        value = self.meta.get("etag")
        return value if isinstance(value, str) else None

    @property
    def last_modified(self) -> str | None:
        """The stored ``Last-Modified``, if the server provided one."""
        value = self.meta.get("last_modified")
        return value if isinstance(value, str) else None


class SafeHttpClient:
    """An httpx client with the project's safety rules wrapped around it.

    Args:
        allowlist: Which hosts and schemes may be reached.
        settings: Timeouts, caps, retry bounds and cache location.
        transport: Injected for tests, so the suite never touches the network.
        sleep: Injected so retry tests do not actually wait.
        robots: Consulted before each fetch when
            :attr:`HttpSettings.respect_robots` is set. Usually installed later
            with :meth:`attach_robots`, since the policy needs this client.
    """

    def __init__(
        self,
        allowlist: Allowlist,
        settings: HttpSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        robots: RobotsChecker | None = None,
    ) -> None:
        """Initialize the client and its underlying httpx session."""
        self._allowlist = allowlist
        self._settings = settings
        self._sleep = sleep
        self._robots = robots
        self._client = httpx.Client(
            timeout=httpx.Timeout(
                connect=settings.connect_timeout,
                read=settings.read_timeout,
                write=settings.read_timeout,
                pool=settings.connect_timeout,
            ),
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": settings.user_agent},
        )

    # ----------------------------------------------------------------- public

    def attach_robots(self, policy: RobotsChecker) -> None:
        """Install the robots.txt policy consulted before each fetch.

        Set after construction because the policy needs this client in order to
        fetch robots.txt itself. See
        :func:`tariff_agent.http.robots.build_client`.

        Args:
            policy: Anything exposing ``assert_allowed(url)``.
        """
        self._robots = policy

    def fetch(
        self,
        url: str,
        *,
        expect: ContentKind = ContentKind.ANY,
        check_robots: bool = True,
    ) -> FetchResult:
        """Retrieve a document, applying every safety rule.

        Args:
            url: Absolute URL to fetch.
            expect: The kind of document expected, so a mislabelled or wrong
                response is rejected here instead of confusing the parser.
            check_robots: Consult robots.txt first. Only the fetch of
                robots.txt itself passes False, to avoid recursing.

        Returns:
            The retrieved document.

        Raises:
            DomainNotAllowedError: The URL, or a redirect hop, is off-allowlist.
            RobotsDisallowedError: robots.txt forbids this URL.
            RobotsUnavailableError: robots.txt could not be read.
            HttpStatusError: The server answered with an error status.
            ResponseTooLargeError: The body exceeded the size cap.
            UnexpectedContentTypeError: The body was not the expected kind.
            FetchError: The request failed after the permitted attempts.
        """
        normalized = assert_allowed(url, self._allowlist)
        if check_robots and self._robots is not None and self._settings.respect_robots:
            self._robots.assert_allowed(normalized)
        return self._fetch_checked(normalized, expect)

    def fetch_text(self, url: str, *, expect: ContentKind = ContentKind.HTML) -> str:
        """Retrieve a document and decode it as text.

        Args:
            url: Absolute URL to fetch.
            expect: Expected content kind; HTML by default.

        Returns:
            The decoded body. UTF-8 is assumed, with replacement on bad bytes,
            because ACBA serves UTF-8 and Armenian text must survive.
        """
        result = self.fetch(url, expect=expect)
        return result.content.decode("utf-8", errors="replace")

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._client.close()

    def __enter__(self) -> Self:
        """Enter a context manager that closes the client on exit."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the client when leaving the context."""
        self.close()

    # ---------------------------------------------------------------- internal

    def _fetch_checked(self, normalized: str, expect: ContentKind) -> FetchResult:
        """Fetch a URL that has already passed policy checks.

        Shared by the public fetch and by the robots.txt fetch.

        Args:
            normalized: An allow-listed, normalized URL.
            expect: Expected content kind.

        Returns:
            The retrieved document.

        Raises:
            FetchError: If the document cannot be retrieved within the limits.
        """
        cached = self._read_cache(normalized)
        if self._settings.offline:
            if cached is None:
                raise FetchError(f"offline mode and no cached copy of {normalized}")
            logger.info("fetch_offline_cache_hit", extra={"url": normalized})
            return self._result_from_cache(normalized, cached, from_cache=True)

        started = time.monotonic()
        result = self._with_retries(normalized, expect, cached)
        logger.info(
            "document_fetched",
            extra={
                "url": result.url,
                "requested_url": result.requested_url,
                "bytes": result.size,
                "content_type": result.content_type,
                "sha256": result.sha256[:12],
                "from_cache": result.from_cache,
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return result

    def _with_retries(
        self, url: str, expect: ContentKind, cached: _CacheEntry | None
    ) -> FetchResult:
        """Attempt a fetch, retrying only failures that retrying can fix.

        Args:
            url: Allow-listed URL.
            expect: Expected content kind.
            cached: Cached copy used for conditional revalidation.

        Returns:
            The retrieved document.

        Raises:
            FetchError: After the last permitted attempt.
        """
        last_error: Exception | None = None
        for attempt in range(1, self._settings.max_attempts + 1):
            try:
                return self._fetch_with_redirects(url, expect, cached)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                retry_after = None
            except HttpStatusError as exc:
                if exc.status_code not in _RETRYABLE_STATUSES:
                    if exc.status_code in (401, 403):
                        logger.warning(
                            "access_blocked",
                            extra={"url": url, "status": exc.status_code},
                        )
                    raise
                last_error = exc
                retry_after = self._retry_after_seconds(exc)

            if attempt == self._settings.max_attempts:
                break
            delay = self._backoff_delay(attempt, retry_after)
            logger.warning(
                "fetch_retry",
                extra={
                    "url": url,
                    "attempt": attempt,
                    "max_attempts": self._settings.max_attempts,
                    "delay_s": round(delay, 2),
                    "error_type": type(last_error).__name__,
                },
            )
            self._sleep(delay)

        raise FetchError(
            f"giving up on {url} after {self._settings.max_attempts} attempts: {last_error}"
        ) from last_error

    def _fetch_with_redirects(
        self, url: str, expect: ContentKind, cached: _CacheEntry | None
    ) -> FetchResult:
        """Follow redirects manually, re-checking the allowlist at every hop.

        httpx's automatic redirects would follow a hop off the bank's domain
        without telling us, so they are disabled and handled here instead.

        Conditional headers are sent on the first hop, and re-attached after a
        redirect **only** when the target is exactly the URL the cached copy was
        finally served from. ACBA permanently redirects www.acba.am to acba.am,
        so without this the validators are lost on every hop and a 1 MB PDF is
        re-downloaded on every monitoring run. A 304 from any *other* resource
        would say nothing about the document we cached, so no validators are
        sent there.

        Args:
            url: Allow-listed starting URL.
            expect: Expected content kind.
            cached: Cached copy used for conditional revalidation.

        Returns:
            The retrieved document.

        Raises:
            DomainNotAllowedError: A redirect left the allowlist.
            FetchError: Too many redirects, or a redirect without a target.
        """
        current = url
        conditional = cached
        cached_final = str(cached.meta.get("final_url") or "") if cached else ""
        for hop in range(self._settings.max_redirects + 1):
            response = self._send(current, expect, conditional, requested_url=url)
            if isinstance(response, FetchResult):
                return response
            location = response
            target = assert_allowed(location, self._allowlist, base=current)
            logger.info("redirect_followed", extra={"from": current, "to": target, "hop": hop + 1})
            current = target
            conditional = cached if cached is not None and target == cached_final else None
        raise FetchError(f"more than {self._settings.max_redirects} redirects starting at {url}")

    def _send(
        self,
        url: str,
        expect: ContentKind,
        cached: _CacheEntry | None,
        *,
        requested_url: str,
    ) -> FetchResult | str:
        """Perform one request and classify the answer.

        Args:
            url: The URL of this hop.
            expect: Expected content kind.
            cached: Cached copy, when conditional revalidation applies.
            requested_url: The URL the caller originally asked for.

        Returns:
            A :class:`FetchResult` when the document was obtained, or the raw
            ``Location`` header when the server redirected.

        Raises:
            HttpStatusError: On any error status.
            ResponseTooLargeError: If the body exceeds the cap.
            UnexpectedContentTypeError: If the body is not the expected kind.
            FetchError: On a redirect without a ``Location``.
        """
        headers = self._conditional_headers(cached)
        with self._client.stream("GET", url, headers=headers) as response:
            if response.status_code == 304 and cached is not None:
                logger.info("document_unchanged", extra={"url": url, "status": 304})
                return self._result_from_cache(requested_url, cached, from_cache=True, url=url)
            if response.is_redirect:
                location = str(response.headers.get("location") or "")
                if not location:
                    raise FetchError(f"redirect from {url} without a Location header")
                response.close()
                return location
            if response.status_code >= 400:
                raise HttpStatusError(
                    response.status_code, url, response.headers.get("retry-after")
                )

            declared = response.headers.get("content-type", "").split(";")[0].strip().lower()
            self._reject_oversize_header(response, url)
            content = self._read_capped(response, url)

        _check_content_kind(content, declared, expect, url)
        result = FetchResult(
            url=url,
            requested_url=requested_url,
            content=content,
            content_type=declared,
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            retrieved_at=datetime.now(UTC),
            from_cache=False,
        )
        self._write_cache(requested_url, result, response.headers)
        return result

    def _conditional_headers(self, cached: _CacheEntry | None) -> dict[str, str]:
        """Build revalidation headers from a cached copy.

        Args:
            cached: The cached copy, if any.

        Returns:
            ``If-None-Match`` / ``If-Modified-Since`` headers, or an empty dict.
        """
        if cached is None:
            return {}
        headers: dict[str, str] = {}
        if cached.etag:
            headers["If-None-Match"] = cached.etag
        if cached.last_modified:
            headers["If-Modified-Since"] = cached.last_modified
        return headers

    def _reject_oversize_header(self, response: httpx.Response, url: str) -> None:
        """Refuse an obviously oversized body before reading it.

        ``Content-Length`` is only a claim, so this is an optimization, not the
        real defence - :meth:`_read_capped` enforces the limit on actual bytes.

        Args:
            response: The streaming response.
            url: The URL being read, for the error message.

        Raises:
            ResponseTooLargeError: If the declared length exceeds the cap.
        """
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self._settings.max_download_bytes:
            raise ResponseTooLargeError(
                f"{url} declares {declared} bytes, over the "
                f"{self._settings.max_download_bytes} byte cap"
            )

    def _read_capped(self, response: httpx.Response, url: str) -> bytes:
        """Read a streaming body, aborting as soon as the cap is exceeded.

        Args:
            response: The streaming response.
            url: The URL being read, for the error message.

        Returns:
            The body bytes.

        Raises:
            ResponseTooLargeError: If the body exceeds the configured cap.
        """
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes(_CHUNK_SIZE):
            total += len(chunk)
            if total > self._settings.max_download_bytes:
                logger.warning(
                    "download_aborted_oversize",
                    extra={"url": url, "cap_bytes": self._settings.max_download_bytes},
                )
                raise ResponseTooLargeError(
                    f"{url} exceeded the {self._settings.max_download_bytes} byte cap"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _retry_after_seconds(self, exc: HttpStatusError) -> float | None:
        """Read a ``Retry-After`` hint, if the error carried one.

        Args:
            exc: The status error raised for this attempt.

        Returns:
            The hint in seconds, or None. Callers cap it at ``backoff_max``.
        """
        if exc.retry_after is None:
            return None
        try:
            return float(exc.retry_after)
        except ValueError:
            # An HTTP-date form is valid but rare; fall back to normal backoff.
            return None

    def _backoff_delay(self, attempt: int, retry_after: float | None) -> float:
        """Compute how long to wait before the next attempt.

        Exponential with jitter, so concurrent retries do not synchronise. A
        server-supplied ``Retry-After`` wins, but is capped at ``backoff_max``:
        a server must not be able to stall a monitoring run for minutes.

        Args:
            attempt: 1-based number of the attempt that just failed.
            retry_after: Server hint in seconds, if any.

        Returns:
            Seconds to sleep.
        """
        if retry_after is not None:
            return float(min(retry_after, self._settings.backoff_max))
        base: float = self._settings.backoff_base * (2.0 ** (attempt - 1))
        capped: float = min(base, self._settings.backoff_max)
        return capped * (1 + random.random() * 0.1)

    # ------------------------------------------------------------------- cache

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        """Return the body and metadata paths for a URL.

        The file name hashes the *URL*, which locates the entry; the *content*
        hash lives inside the metadata and tells later phases whether the
        document itself changed.

        Args:
            url: The normalized URL originally requested.

        Returns:
            A ``(body_path, meta_path)`` pair.
        """
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        directory = self._settings.cache_dir
        return directory / f"{key}.bin", directory / f"{key}.json"

    def _read_cache(self, url: str) -> _CacheEntry | None:
        """Load a cached document, if one exists and is readable.

        Args:
            url: The normalized URL.

        Returns:
            The cache entry, or None. A corrupt entry is treated as absent, not
            as an error: the worst case is one extra download.
        """
        body_path, meta_path = self._cache_paths(url)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            content = body_path.read_bytes()
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(meta, dict):
            return None
        return _CacheEntry(meta=meta, content=content)

    def _write_cache(self, url: str, result: FetchResult, headers: httpx.Headers) -> None:
        """Store a freshly fetched document and its revalidation metadata.

        Cache failures are logged and swallowed: an unwritable cache must not
        fail a run that already has the bytes it needs.

        Args:
            url: The normalized URL originally requested.
            result: The fetched document.
            headers: Response headers, read for ``ETag`` / ``Last-Modified``.
        """
        body_path, meta_path = self._cache_paths(url)
        meta = {
            "requested_url": url,
            "final_url": result.url,
            "content_type": result.content_type,
            "sha256": result.sha256,
            "size": result.size,
            "retrieved_at": result.retrieved_at.isoformat(),
            "etag": headers.get("etag"),
            "last_modified": headers.get("last-modified"),
        }
        try:
            self._settings.cache_dir.mkdir(parents=True, exist_ok=True)
            body_path.write_bytes(result.content)
            meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "cache_write_failed", extra={"url": url, "error_type": type(exc).__name__}
            )

    def _result_from_cache(
        self,
        requested_url: str,
        cached: _CacheEntry,
        *,
        from_cache: bool,
        url: str | None = None,
    ) -> FetchResult:
        """Build a result from cached bytes.

        Args:
            requested_url: The URL the caller asked for.
            cached: The cache entry.
            from_cache: Recorded on the result so callers can tell.
            url: Final URL, when known from this response.

        Returns:
            The cached document as a :class:`FetchResult`.
        """
        meta = cached.meta
        retrieved_raw = meta.get("retrieved_at")
        try:
            retrieved_at = datetime.fromisoformat(str(retrieved_raw))
        except (TypeError, ValueError):
            retrieved_at = datetime.now(UTC)
        return FetchResult(
            url=url or str(meta.get("final_url") or requested_url),
            requested_url=requested_url,
            content=cached.content,
            content_type=str(meta.get("content_type") or ""),
            sha256=str(meta.get("sha256") or hashlib.sha256(cached.content).hexdigest()),
            size=len(cached.content),
            retrieved_at=retrieved_at,
            from_cache=from_cache,
        )


def _check_content_kind(
    content: bytes, declared: str, expect: ContentKind, url: str
) -> None:
    """Verify the body is the kind of document that was expected.

    Both signals are checked, because either alone is weak: the header is a
    claim the server makes, and magic bytes alone would accept a PDF served
    where an HTML page was expected.

    Args:
        content: The body bytes.
        declared: Content type from the header, without parameters.
        expect: What the caller expected.
        url: The URL, for the error message.

    Raises:
        UnexpectedContentTypeError: If header or body contradicts ``expect``.
    """
    if expect is ContentKind.ANY:
        return
    if expect is ContentKind.PDF:
        if "pdf" not in declared:
            raise UnexpectedContentTypeError(
                f"expected a PDF at {url} but the server declared {declared!r}"
            )
        if not content[:1024].lstrip().startswith(b"%PDF-"):
            raise UnexpectedContentTypeError(
                f"{url} was declared {declared!r} but does not start with the PDF signature"
            )
        return
    if "html" not in declared and "xml" not in declared:
        raise UnexpectedContentTypeError(
            f"expected an HTML page at {url} but the server declared {declared!r}"
        )
    head = content[:1024]
    if head.startswith(_BOM):
        head = head[len(_BOM) :]
    if not head.lstrip().startswith(b"<"):
        raise UnexpectedContentTypeError(
            f"{url} was declared {declared!r} but the body does not look like markup"
        )

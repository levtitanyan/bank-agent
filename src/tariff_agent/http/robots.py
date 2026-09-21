"""robots.txt handling, using the same guarded client as everything else.

Two rules, following the convention Google's crawler documents, because a
blanket "fail open" is hard to defend and a blanket "fail closed" makes the
whole agent hostage to one file:

* **4xx (including 404)** - no rules exist for this site, so fetching is
  allowed. A missing robots.txt is the normal case for most servers.
* **5xx or a network failure** - the server could not tell us. Permission is
  genuinely unknown, so the run stops rather than resolving the doubt in our own
  favour.

The file is fetched through :class:`~tariff_agent.http.client.SafeHttpClient`
itself - via the internal path that skips the robots check, to avoid recursion -
so there is exactly one network door in the project, with one set of timeouts,
caps and retries.
"""

from __future__ import annotations

from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from tariff_agent.config import Allowlist, HttpSettings
from tariff_agent.errors import (
    FetchError,
    HttpStatusError,
    RobotsDisallowedError,
    RobotsUnavailableError,
)
from tariff_agent.http.client import ContentKind, SafeHttpClient
from tariff_agent.observability.logging import get_logger

logger = get_logger(__name__)


class RobotsPolicy:
    """Caches and applies each host's robots.txt for the life of a run.

    Args:
        client: The guarded client used to fetch robots.txt.
        settings: Provides the User-Agent the rules are evaluated against.
    """

    def __init__(self, client: SafeHttpClient, settings: HttpSettings) -> None:
        """Initialize the policy with an empty per-host cache."""
        self._client = client
        self._settings = settings
        self._parsers: dict[str, RobotFileParser | None] = {}

    def assert_allowed(self, url: str) -> None:
        """Refuse a URL that robots.txt disallows.

        Args:
            url: A normalized, allow-listed URL.

        Raises:
            RobotsDisallowedError: The site's rules forbid this path.
            RobotsUnavailableError: robots.txt could not be read at all.
        """
        parser = self._parser_for(url)
        if parser is None:
            return
        agent = self._settings.user_agent.split("/")[0]
        if not parser.can_fetch(agent, url):
            logger.warning("robots_disallowed", extra={"url": url, "user_agent": agent})
            raise RobotsDisallowedError(f"robots.txt disallows {url}")

    def _parser_for(self, url: str) -> RobotFileParser | None:
        """Return the parsed rules for a URL's host, fetching them once.

        Args:
            url: A normalized, allow-listed URL.

        Returns:
            The parser, or None when the host publishes no rules.

        Raises:
            RobotsUnavailableError: The server failed to answer.
        """
        parts = urlsplit(url)
        host = parts.hostname or ""
        if host in self._parsers:
            return self._parsers[host]

        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        parser: RobotFileParser | None
        try:
            result = self._client.fetch(
                robots_url, expect=ContentKind.ANY, check_robots=False
            )
        except HttpStatusError as exc:
            if 400 <= exc.status_code < 500:
                logger.info(
                    "robots_absent",
                    extra={"host": host, "status": exc.status_code, "decision": "allow"},
                )
                parser = None
            else:
                raise RobotsUnavailableError(
                    f"robots.txt for {host} returned HTTP {exc.status_code}; "
                    "stopping rather than assuming permission"
                ) from exc
        except FetchError as exc:
            raise RobotsUnavailableError(
                f"robots.txt for {host} could not be read ({exc}); "
                "stopping rather than assuming permission"
            ) from exc
        else:
            parser = RobotFileParser()
            parser.parse(result.content.decode("utf-8", errors="replace").splitlines())
            logger.info("robots_loaded", extra={"host": host, "bytes": result.size})

        self._parsers[host] = parser
        return parser


def build_client(
    allowlist: Allowlist, settings: HttpSettings, **kwargs: object
) -> SafeHttpClient:
    """Create a client with robots.txt checking wired in.

    This is the constructor the rest of the project should use; the bare
    :class:`SafeHttpClient` exists for tests and for fetching robots.txt itself.

    Args:
        allowlist: Which hosts and schemes may be reached.
        settings: Network limits.
        **kwargs: Passed through to :class:`SafeHttpClient` (``transport``,
            ``sleep``).

    Returns:
        A client that consults robots.txt before each fetch, when
        :attr:`HttpSettings.respect_robots` is set.
    """
    client = SafeHttpClient(allowlist, settings, **kwargs)  # type: ignore[arg-type]
    if settings.respect_robots:
        client.attach_robots(RobotsPolicy(client, settings))
    return client

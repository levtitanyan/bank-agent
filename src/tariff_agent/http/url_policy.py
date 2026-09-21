"""Deciding whether a URL may be fetched. Pure functions, no I/O.

Separate from the client on purpose:

* the rule is testable without any HTTP machinery;
* Phase 3 reuses it to filter hundreds of harvested links, where raising per
  rejected link would be the wrong shape;
* the client can therefore re-apply it on *every* redirect hop cheaply.

The matching rule is deliberately strict: exact host membership, https only.
Suffix matching (``host.endswith(".acba.am")``) is the classic mistake - it
accepts ``acba.am.evil.com`` and rejects nothing an attacker cares about - so it
is not used here.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

from tariff_agent.config import Allowlist
from tariff_agent.errors import DomainNotAllowedError
from tariff_agent.observability.logging import get_logger

logger = get_logger(__name__)


def normalize_url(raw: str, base: str | None = None) -> str:
    """Put a URL into the single form the rest of the code compares and caches.

    Lowercases the scheme and host (they are case-insensitive), resolves a
    relative reference against ``base``, and drops the fragment (never sent to a
    server, and it would otherwise split the cache). Percent-encoding in the path
    is preserved exactly - ``loan%20info.pdf`` must stay encoded or the request
    404s.

    Args:
        raw: URL or relative reference.
        base: Absolute URL to resolve ``raw`` against, when it is relative.

    Returns:
        The normalized absolute URL.

    Raises:
        DomainNotAllowedError: If the result has no host at all, which cannot be
            checked against the allowlist and so cannot be fetched.
    """
    candidate = urljoin(base, raw.strip()) if base else raw.strip()
    parts = urlsplit(candidate)
    if not parts.hostname:
        raise DomainNotAllowedError(f"URL has no host and cannot be checked: {raw!r}")
    host = parts.hostname.lower().rstrip(".")
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, ""))


def is_allowed_url(url: str, allowlist: Allowlist) -> bool:
    """Report whether a URL may be fetched, without raising.

    Args:
        url: Absolute URL, normalized or not.
        allowlist: The configured policy.

    Returns:
        True when the scheme is permitted and the host is an exact member of
        the allowlist.
    """
    try:
        parts = urlsplit(normalize_url(url))
    except DomainNotAllowedError:
        return False
    if parts.scheme not in allowlist.allowed_schemes:
        return False
    if parts.port is not None:
        return False
    return (parts.hostname or "") in allowlist.domains


def assert_allowed(url: str, allowlist: Allowlist, base: str | None = None) -> str:
    """Return the normalized URL, or refuse to let it be fetched.

    Args:
        url: URL or relative reference.
        allowlist: The configured policy.
        base: Base URL for resolving a relative reference.

    Returns:
        The normalized, permitted URL.

    Raises:
        DomainNotAllowedError: If the scheme or host is not permitted. The
            message names the host, since that is what a reviewer needs to see.
    """
    normalized = normalize_url(url, base)
    if not is_allowed_url(normalized, allowlist):
        host = urlsplit(normalized).hostname or "?"
        raise DomainNotAllowedError(
            f"refusing to fetch {normalized!r}: host {host!r} is not on the allowlist "
            f"{list(allowlist.domains)} over {list(allowlist.allowed_schemes)}"
        )
    return normalized


def filter_allowed(
    urls: Iterable[str], allowlist: Allowlist, base: str | None = None
) -> list[str]:
    """Keep only the fetchable URLs from a batch of harvested links.

    Used by discovery, where most rejected links are ordinary outbound links
    rather than anything suspicious, so they are dropped and counted instead of
    raising.

    Args:
        urls: Candidate URLs or relative references.
        allowlist: The configured policy.
        base: Base URL for resolving relative references.

    Returns:
        Normalized, permitted URLs, de-duplicated, in first-seen order.
    """
    kept: list[str] = []
    seen: set[str] = set()
    rejected = 0
    for raw in urls:
        try:
            normalized = assert_allowed(raw, allowlist, base)
        except DomainNotAllowedError:
            rejected += 1
            continue
        if normalized not in seen:
            seen.add(normalized)
            kept.append(normalized)
    if rejected:
        logger.debug(
            "links_filtered", extra={"kept": len(kept), "rejected_off_domain": rejected}
        )
    return kept

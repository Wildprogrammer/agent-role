"""Information collection routing state machine and URL scope rules.

This module is pure and side-effect free: it performs no network access,
never launches a browser or a search tool, and never installs anything.
Callers map real observations onto :class:`RouteEvent` values and drive
:class:`RouteState` forward with :func:`transition`. The scope helpers
decide whether a candidate or redirect URL stays inside the approved
collection scope.

Route order: direct -> tavily -> browser layer. Inside the browser layer
Playwright is preferred and ``agent-browser`` is the same-layer fallback.
A success event is terminal; failed/unavailable events advance to the next
implementation; the final browser fallback failing is terminal.
When the current task does not allow Tavily, callers emit ``UNAVAILABLE``
so the route skips the Tavily layer and moves straight to the browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit


HTTP_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}


class RouteImplementation(StrEnum):
    DIRECT = "direct"
    TAVILY = "tavily"
    PLAYWRIGHT = "playwright"
    AGENT_BROWSER = "agent-browser"


class RouteStatus(StrEnum):
    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RouteEvent(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class RouteState:
    current_implementation: RouteImplementation | None
    status: RouteStatus
    attempted: tuple[RouteImplementation, ...]
    preferred_browser: RouteImplementation = RouteImplementation.PLAYWRIGHT

    @classmethod
    def start(cls, *, prefer_agent_browser: bool = False) -> "RouteState":
        preferred_browser = (
            RouteImplementation.AGENT_BROWSER
            if prefer_agent_browser
            else RouteImplementation.PLAYWRIGHT
        )
        return cls(
            current_implementation=RouteImplementation.DIRECT,
            status=RouteStatus.ACTIVE,
            attempted=(),
            preferred_browser=preferred_browser,
        )


def _append_once(
    attempted: tuple[RouteImplementation, ...],
    implementation: RouteImplementation,
) -> tuple[RouteImplementation, ...]:
    if implementation in attempted:
        return attempted
    return (*attempted, implementation)


def transition(state: RouteState, event: RouteEvent) -> RouteState:
    """Advance the route machine by one event.

    ``SUCCESS`` is terminal (status ``succeeded``). ``FAILED`` and
    ``UNAVAILABLE`` move ``DIRECT -> TAVILY -> preferred browser``; with
    Playwright preferred the browser layer is ``PLAYWRIGHT -> AGENT_BROWSER
    -> failed``; with agent-browser preferred the browser entry is
    ``AGENT_BROWSER`` directly and its failure is terminal. Terminal states
    reject every later event. When Tavily is not allowed, the caller maps
    that to ``UNAVAILABLE`` so the Tavily layer is skipped without an attempt.
    """
    if not isinstance(event, RouteEvent):
        raise TypeError(f"expected RouteEvent, got {type(event).__name__}")
    if state.status is not RouteStatus.ACTIVE:
        raise ValueError("terminal states reject further route events")
    current = state.current_implementation
    if current is None:
        raise ValueError("active route has no current implementation")
    attempted = _append_once(state.attempted, current)

    if event is RouteEvent.SUCCESS:
        return RouteState(
            current_implementation=None,
            status=RouteStatus.SUCCEEDED,
            attempted=attempted,
            preferred_browser=state.preferred_browser,
        )

    if current is RouteImplementation.AGENT_BROWSER:
        return RouteState(
            current_implementation=None,
            status=RouteStatus.FAILED,
            attempted=attempted,
            preferred_browser=state.preferred_browser,
        )
    if current is RouteImplementation.TAVILY:
        next_implementation = state.preferred_browser
    else:
        next_implementation = {
            RouteImplementation.DIRECT: RouteImplementation.TAVILY,
            RouteImplementation.PLAYWRIGHT: RouteImplementation.AGENT_BROWSER,
        }[current]
    return RouteState(
        current_implementation=next_implementation,
        status=RouteStatus.ACTIVE,
        attempted=attempted,
        preferred_browser=state.preferred_browser,
    )


def normalize_url(raw: str) -> str:
    """Canonicalize an http/https URL for scope comparison.

    Lowercases the scheme, normalizes the hostname (lowercase, FQDN
    trailing dot removed, IDNA-encoded to ASCII), drops the fragment,
    replaces an empty path with ``/``, and removes the scheme default
    port. Any userinfo in the URL is dropped from the normalized form.
    Non-http(s) schemes and missing hostnames are rejected with
    ``ValueError``.
    """
    if not isinstance(raw, str):
        raise TypeError("URL must be a string")
    parts = urlsplit(raw.strip())
    scheme = parts.scheme.lower()
    if scheme not in HTTP_SCHEMES:
        raise ValueError(f"only http/https URLs are allowed: {raw!r}")
    hostname = parts.hostname
    if not hostname:
        raise ValueError(f"URL must include a hostname: {raw!r}")
    hostname = _normalize_hostname(hostname)
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    port = parts.port
    if port is None or port == DEFAULT_PORTS[scheme]:
        netloc = hostname
    else:
        netloc = f"{hostname}:{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def _normalize_hostname(hostname: str) -> str:
    """Canonicalize a URL hostname for scope comparison.

    Lowercases the hostname, strips the FQDN trailing dot, and IDNA-encodes
    Unicode names to ASCII so Unicode and punycode spellings compare equal.
    IPv6 literals are returned unchanged (the caller adds brackets).
    """
    if ":" in hostname:
        return hostname
    hostname = hostname.rstrip(".")
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    return hostname.lower()


def same_domain(left: str, right: str) -> bool:
    """Whether two URLs share the same normalized hostname.

    Scheme and port are ignored: http and https, and default or
    non-default ports, are the same domain as long as the hostname
    matches. A subdomain or a different domain is not the same domain.
    """
    left_parts = urlsplit(normalize_url(left))
    right_parts = urlsplit(normalize_url(right))
    return left_parts.hostname == right_parts.hostname


same_host = same_domain


@dataclass(frozen=True)
class Scope:
    """Approved collection scope for one task.

    ``follow_same_domain=False`` (the default) is exact-page scope: only
    the start page itself is in scope. ``follow_same_domain=True`` widens
    the scope to every page on the same domain. Same domain means the
    normalized hostname matches; scheme and port are ignored, so http and
    https or any port on the same hostname are in scope. A subdomain is a
    different domain. Cross-domain expansion never happens automatically.
    """

    start_url: str
    follow_same_domain: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_url", normalize_url(self.start_url))

    def allows_candidate(self, candidate_url: str) -> bool:
        """Whether a linked/followed URL stays inside the scope."""
        candidate = normalize_url(candidate_url)
        if self.follow_same_domain:
            return same_host(self.start_url, candidate)
        return candidate == self.start_url

    def allows_redirect(self, target_url: str) -> bool:
        """Whether an automatic redirect target stays inside the scope.

        Redirects are server-driven rather than user-authorized page
        following: the scope rejects targets on a different domain, while
        a same-domain redirect may continue even in exact-page mode.
        """
        return same_domain(self.start_url, target_url)

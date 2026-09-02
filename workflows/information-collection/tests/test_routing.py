from __future__ import annotations

from pathlib import Path
import sys

import pytest


SCRIPTS_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from routing import (
    RouteEvent,
    RouteImplementation,
    RouteState,
    RouteStatus,
    Scope,
    normalize_url,
    same_domain,
    same_host,
    transition,
)


@pytest.mark.parametrize(
    ("events", "attempts", "terminal"),
    [
        (["direct_success"], ["direct"], "succeeded"),
        (["direct_failed", "tavily_success"], ["direct", "tavily"], "succeeded"),
        (
            ["direct_failed", "tavily_unavailable", "playwright_success"],
            ["direct", "tavily", "playwright"],
            "succeeded",
        ),
        (
            [
                "direct_failed",
                "tavily_failed",
                "playwright_unavailable",
                "agent_browser_success",
            ],
            ["direct", "tavily", "playwright", "agent-browser"],
            "succeeded",
        ),
        (
            [
                "direct_failed",
                "tavily_failed",
                "playwright_failed",
                "agent_browser_failed",
            ],
            ["direct", "tavily", "playwright", "agent-browser"],
            "failed",
        ),
    ],
)
def test_route_scenarios(events: list[str], attempts: list[str], terminal: str) -> None:
    state = RouteState.start()
    observed = [state.current_implementation]
    for event in events:
        state = transition(state, RouteEvent(event.rsplit("_", 1)[-1]))
        if state.current_implementation is not None:
            observed.append(state.current_implementation)
    assert observed == attempts
    assert state.status == terminal


def test_success_is_terminal_and_rejects_further_events() -> None:
    state = transition(RouteState.start(), RouteEvent.SUCCESS)

    assert state.status is RouteStatus.SUCCEEDED
    assert state.current_implementation is None
    assert state.attempted == (RouteImplementation.DIRECT,)
    for event in (RouteEvent.SUCCESS, RouteEvent.FAILED, RouteEvent.UNAVAILABLE):
        with pytest.raises(ValueError, match="terminal"):
            transition(state, event)


def test_total_failure_is_terminal_and_rejects_further_events() -> None:
    state = RouteState.start()
    for event in (
        RouteEvent.FAILED,
        RouteEvent.FAILED,
        RouteEvent.FAILED,
        RouteEvent.FAILED,
    ):
        state = transition(state, event)

    assert state.status is RouteStatus.FAILED
    assert state.current_implementation is None
    with pytest.raises(ValueError, match="terminal"):
        transition(state, RouteEvent.UNAVAILABLE)


def test_transition_rejects_non_event_values() -> None:
    state = RouteState.start()

    with pytest.raises(TypeError):
        transition(state, "success")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        transition(state, None)  # type: ignore[arg-type]


def test_route_event_rejects_unknown_members() -> None:
    with pytest.raises(ValueError):
        RouteEvent("bogus")


def test_route_cannot_skip_layers() -> None:
    state = transition(RouteState.start(), RouteEvent.FAILED)
    assert state.current_implementation is RouteImplementation.TAVILY

    state = transition(state, RouteEvent.UNAVAILABLE)
    assert state.current_implementation is RouteImplementation.PLAYWRIGHT


def test_attempted_records_each_implementation_once() -> None:
    state = RouteState.start()
    for event in (
        RouteEvent.FAILED,
        RouteEvent.UNAVAILABLE,
        RouteEvent.FAILED,
        RouteEvent.FAILED,
    ):
        state = transition(state, event)

    assert state.attempted == (
        RouteImplementation.DIRECT,
        RouteImplementation.TAVILY,
        RouteImplementation.PLAYWRIGHT,
        RouteImplementation.AGENT_BROWSER,
    )


def test_explicit_agent_browser_preference_skips_playwright() -> None:
    state = RouteState.start(prefer_agent_browser=True)
    assert state.preferred_browser is RouteImplementation.AGENT_BROWSER

    state = transition(state, RouteEvent.FAILED)
    assert state.current_implementation is RouteImplementation.TAVILY

    state = transition(state, RouteEvent.FAILED)
    assert state.current_implementation is RouteImplementation.AGENT_BROWSER

    state = transition(state, RouteEvent.FAILED)
    assert state.status is RouteStatus.FAILED
    assert state.attempted == (
        RouteImplementation.DIRECT,
        RouteImplementation.TAVILY,
        RouteImplementation.AGENT_BROWSER,
    )


def test_normalize_url_lowercases_scheme_and_host_and_strips_fragment() -> None:
    assert normalize_url("HTTPS://Example.COM/a?q=1#section") == "https://example.com/a?q=1"


def test_normalize_url_removes_default_ports_and_keeps_explicit_ports() -> None:
    assert normalize_url("https://example.com:443/a") == "https://example.com/a"
    assert normalize_url("http://example.com:80/a") == "http://example.com/a"
    assert normalize_url("https://example.com:8443/a") == "https://example.com:8443/a"


def test_normalize_url_idna_encodes_unicode_hosts_and_strips_trailing_dot() -> None:
    assert normalize_url("https://例え.jp/") == "https://xn--r8jz45g.jp/"
    assert normalize_url("https://example.com./a") == "https://example.com/a"


def test_normalize_url_keeps_ipv6_host_brackets() -> None:
    assert normalize_url("https://[::1]:8080/a#f") == "https://[::1]:8080/a"


def test_normalize_url_rejects_non_http_schemes_and_missing_host() -> None:
    for raw in (
        "ftp://example.com/a",
        "file:///a",
        "javascript:alert(1)",
        "https:///a",
    ):
        with pytest.raises(ValueError):
            normalize_url(raw)


def test_same_domain_compares_normalized_hostname_only() -> None:
    assert same_domain("https://example.com/a", "https://example.com/b")
    assert same_domain("https://example.com:443/a", "https://example.com/b")
    assert same_domain("http://example.com:80/a", "http://example.com/b")
    assert same_domain("https://example.com/a", "http://example.com/b")
    assert same_domain("https://example.com/a", "http://example.com:8443/b")
    assert not same_domain("https://sub.example.com/a", "https://example.com/b")
    assert not same_domain("https://example.com/a", "https://other.com/b")


def test_same_domain_accepts_idn_and_punycode_equivalents() -> None:
    assert same_domain("https://例え.jp/a", "https://xn--r8jz45g.jp/b")
    assert same_domain("https://münchen.de/a", "https://xn--mnchen-3ya.de/b")


def test_same_domain_strips_fqdn_trailing_dot() -> None:
    assert same_domain("https://example.com./a", "https://example.com/b")


def test_same_host_remains_an_alias_for_same_domain() -> None:
    assert same_host is same_domain
    assert same_host("https://example.com/a", "http://example.com/b")


def test_scope_defaults_to_exact_page() -> None:
    scope = Scope("https://example.com/a")

    assert scope.follow_same_domain is False
    assert scope.allows_candidate("https://example.com/a#fragment")
    assert scope.allows_candidate("HTTPS://EXAMPLE.COM/a")
    assert not scope.allows_candidate("https://example.com/b")
    assert not scope.allows_candidate("https://example.com/a?x=1")
    assert not scope.allows_candidate("http://example.com/a")
    assert not scope.allows_candidate("https://example.com:8443/a")
    assert not scope.allows_candidate("https://other.com/a")
    assert not scope.allows_candidate("https://sub.example.com/a")


def test_scope_explicit_same_domain_allows_only_same_domain() -> None:
    scope = Scope("https://example.com/a", follow_same_domain=True)

    assert scope.allows_candidate("https://example.com/b")
    assert scope.allows_candidate("http://example.com/c")
    assert scope.allows_candidate("https://example.com:8443/b")
    assert not scope.allows_candidate("https://sub.example.com/b")
    assert not scope.allows_candidate("https://other.com/b")


def test_scope_allows_same_domain_redirects_and_rejects_cross_domain() -> None:
    exact_scope = Scope("https://example.com/a")
    same_domain_scope = Scope("https://example.com/a", follow_same_domain=True)

    for active_scope in (exact_scope, same_domain_scope):
        assert active_scope.allows_redirect("https://example.com/b")
        assert active_scope.allows_redirect("http://example.com/a")
        assert active_scope.allows_redirect("https://example.com:8443/b")
        assert not active_scope.allows_redirect("https://other.com/b")
        assert not active_scope.allows_redirect("https://sub.example.com/b")

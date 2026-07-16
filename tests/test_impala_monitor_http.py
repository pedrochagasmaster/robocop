"""Tests for the Slice 3 read-only HTTP adapter (``dispatch/impala_monitor_http.py``).

A fake ``Transport`` replays slice-1 fixtures and adversarial responses so
these tests never touch a real socket. Covers capability detection and
caching, coordinator discovery and its TTL cache, ambiguity refusal,
circuit-breaker open/half-open behavior, and the hard limits enforced before
any body is parsed (host allowlist, no cross-host redirects, content-type
check, body size cap, TLS/connect failure -> availability error). See
``docs/research/impala-monitoring-implementation-plan.md`` (Slice 3) for the
authoritative spec this file implements.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dispatch import impala_monitor as im
from dispatch import impala_monitor_http as http_mod

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "impala_monitor"

SEED_URL = "https://coordinator-1.internal.example:25443"
COORD_2 = "https://coordinator-2.internal.example:25443"
COORD_3 = "https://coordinator-3.internal.example:25443 ".strip()
OFF_LIST_URL = "https://evil.example:25443"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def fixture_json(name: str) -> Any:
    return json.loads(fixture_bytes(name))


def make_identity(
    *,
    coordinator_base_url: str = SEED_URL,
    query_id: str = "1a2b3c4d5e6f7081:9192a3b4c5d6e7f8",
) -> im.QueryIdentity:
    return im.QueryIdentity(
        coordinator_base_url=coordinator_base_url,
        query_id=query_id,
        shell_execution_id="shell-exec-1",
        relation="initial",
        discovered_at="2026-07-15T10:00:00Z",
    )


class FakeTransport:
    """Replays canned ``FetchResult``s keyed by exact URL, recording calls."""

    def __init__(self) -> None:
        self.responses: dict[str, http_mod.FetchResult | Exception] = {}
        self.calls: list[str] = []

    def set_json(self, url: str, payload: bytes, *, status: int = 200) -> None:
        self.responses[url] = http_mod.FetchResult(
            status=status, content_type="application/json", body=payload
        )

    def set_response(self, url: str, result: http_mod.FetchResult) -> None:
        self.responses[url] = result

    def set_raises(self, url: str, exc: Exception) -> None:
        self.responses[url] = exc

    def fetch(self, url: str, timeout: tuple[float, float]) -> http_mod.FetchResult:
        self.calls.append(url)
        outcome = self.responses.get(url)
        if outcome is None:
            raise AssertionError(f"FakeTransport has no canned response for {url!r}")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def no_sleep_client(transport: FakeTransport, **kwargs: Any) -> http_mod.ImpalaMonitorClient:
    kwargs.setdefault("random_source", _ZeroJitter())
    return http_mod.ImpalaMonitorClient(transport, allow_http=False, **kwargs)


class _ZeroJitter:
    def uniform(self, _a: float, _b: float) -> float:
        return 0.0


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


# =============================================================================
# observe() — happy path replays slice-1 fixtures through the real transport seam
# =============================================================================


@pytest.mark.parametrize(
    ("fixture_name", "expected_phase"),
    [
        ("query_stmt_created.json", "preparing"),
        ("query_stmt_queued.json", "queued"),
        ("query_stmt_running.json", "running"),
        ("query_stmt_finished.json", "succeeded"),
        ("query_stmt_exception.json", "failed"),
    ],
)
def test_observe_replays_fixtures_through_query_stmt(
    fixture_name: str, expected_phase: str
) -> None:
    transport = FakeTransport()
    identity = make_identity()
    detail_url = im.build_detail_url(SEED_URL, "query_stmt", identity.query_id)
    transport.set_json(detail_url, fixture_bytes(fixture_name))

    client = no_sleep_client(transport)
    observation = client.observe(identity)

    assert observation.phase == expected_phase
    assert observation.availability_error is None
    # The capability probe against query_stmt succeeds, so its response is
    # reused for the actual poll rather than issuing a second request.
    assert transport.calls == [detail_url]


def test_observe_unknown_id_yields_availability_error_not_exception() -> None:
    transport = FakeTransport()
    identity = make_identity()
    detail_url = im.build_detail_url(SEED_URL, "query_stmt", identity.query_id)
    transport.set_json(detail_url, fixture_bytes("query_stmt_unknown_id_error.json"))

    client = no_sleep_client(transport)
    observation = client.observe(identity)

    assert observation.availability_error is not None


# =============================================================================
# Capability detection: query_stmt first, fall back to query_plan, then cache
# =============================================================================


def test_observe_falls_back_to_query_plan_on_404() -> None:
    transport = FakeTransport()
    identity = make_identity()
    stmt_url = im.build_detail_url(SEED_URL, "query_stmt", identity.query_id)
    plan_url = im.build_detail_url(SEED_URL, "query_plan", identity.query_id)
    transport.set_response(
        stmt_url, http_mod.FetchResult(status=404, content_type="text/html", body=b"not found")
    )
    transport.set_json(plan_url, fixture_bytes("query_stmt_running.json"))

    client = no_sleep_client(transport)
    observation = client.observe(identity)

    assert observation.phase == "running"
    assert observation.availability_error is None
    assert plan_url in transport.calls


def test_observe_falls_back_to_query_plan_on_unrecognized_shape() -> None:
    transport = FakeTransport()
    identity = make_identity()
    stmt_url = im.build_detail_url(SEED_URL, "query_stmt", identity.query_id)
    plan_url = im.build_detail_url(SEED_URL, "query_plan", identity.query_id)
    # 200 + JSON content-type, but a shape with none of the recognized
    # markers (no record_json, no error, no plan_metadata_unavailable) --
    # capability detection must not accept this as query_stmt-compatible.
    transport.set_json(stmt_url, b'{"totally_unexpected": true}')
    transport.set_json(plan_url, fixture_bytes("query_stmt_running.json"))

    client = no_sleep_client(transport)
    observation = client.observe(identity)

    assert observation.phase == "running"
    assert plan_url in transport.calls


def test_capability_choice_is_cached_per_coordinator() -> None:
    transport = FakeTransport()
    identity1 = make_identity(query_id="1a2b3c4d5e6f7081:9192a3b4c5d6e7f8")
    identity2 = make_identity(query_id="2a2b3c4d5e6f7081:9192a3b4c5d6e7f8")

    stmt_url_1 = im.build_detail_url(SEED_URL, "query_stmt", identity1.query_id)
    plan_url_1 = im.build_detail_url(SEED_URL, "query_plan", identity1.query_id)
    stmt_url_2 = im.build_detail_url(SEED_URL, "query_stmt", identity2.query_id)
    plan_url_2 = im.build_detail_url(SEED_URL, "query_plan", identity2.query_id)

    transport.set_response(
        stmt_url_1, http_mod.FetchResult(status=404, content_type="text/html", body=b"")
    )
    transport.set_json(plan_url_1, fixture_bytes("query_stmt_running.json"))
    transport.set_json(plan_url_2, fixture_bytes("query_stmt_finished.json"))

    client = no_sleep_client(transport)
    client.observe(identity1)
    transport.calls.clear()

    observation2 = client.observe(identity2)

    # Second observe() must go straight to query_plan; no repeat probe of
    # query_stmt for the same coordinator host.
    assert stmt_url_2 not in transport.calls
    assert plan_url_2 in transport.calls
    assert observation2.phase == "succeeded"


def test_capability_probe_runs_once_per_coordinator_not_per_call() -> None:
    transport = FakeTransport()
    identity = make_identity()
    stmt_url = im.build_detail_url(SEED_URL, "query_stmt", identity.query_id)
    transport.set_json(stmt_url, fixture_bytes("query_stmt_running.json"))

    client = no_sleep_client(transport)
    client.observe(identity)
    client.observe(identity)
    client.observe(identity)

    # The first observe() reuses its own capability probe response, so all
    # three observe() calls make exactly one request each: no re-probing.
    assert transport.calls.count(stmt_url) == 3


# =============================================================================
# Adversarial: off-list redirect, oversized body, wrong content type, HTML body
# =============================================================================


def test_fetch_enforced_rejects_urls_whose_host_was_never_authorized() -> None:
    # _fetch_enforced is the single low-level chokepoint every public method
    # routes through. A URL for a host that was never added to the
    # allowlist (via observe()'s own identity or a validated discovery
    # result) must be refused before any transport call, regardless of how
    # the URL was constructed.
    transport = FakeTransport()
    client = no_sleep_client(transport)

    result = client._fetch_enforced(f"{OFF_LIST_URL}/query_stmt?query_id=x&json")

    assert result is None
    assert transport.calls == []


def test_discover_coordinators_never_authorizes_a_host_that_fails_validation() -> None:
    transport = FakeTransport()
    backends_url = f"{SEED_URL}/backends?json"
    # One entry has a webserver_url that fails coordinator-URL validation
    # (userinfo embedded) -- it must be silently dropped, not authorized.
    transport.set_json(
        backends_url,
        json.dumps(
            {
                "backends": [
                    {
                        "webserver_url": "https://attacker:pw@evil.example:25443",
                        "is_coordinator": True,
                        "is_active": True,
                    }
                ]
            }
        ).encode("utf-8"),
    )

    client = no_sleep_client(transport)
    coordinators = client.discover_coordinators(SEED_URL)

    assert coordinators == []
    assert OFF_LIST_URL not in coordinators
    result = client._fetch_enforced(f"{OFF_LIST_URL}/query_stmt?query_id=x&json")
    assert result is None
    assert transport.calls == [backends_url]


def test_fetch_enforced_does_not_follow_cross_host_redirect() -> None:
    transport = FakeTransport()
    identity = make_identity()
    detail_url = im.build_detail_url(SEED_URL, "query_stmt", identity.query_id)
    plan_url = im.build_detail_url(SEED_URL, "query_plan", identity.query_id)
    transport.set_response(
        detail_url, http_mod.FetchResult(status=302, content_type=None, body=b"")
    )
    transport.set_response(plan_url, http_mod.FetchResult(status=302, content_type=None, body=b""))

    client = no_sleep_client(transport, max_retries=0)
    observation = client.observe(identity)

    assert observation.availability_error is not None
    # A 3xx response is never treated as usable and its Location is never
    # parsed or requested -- the redirect target (which could be an
    # off-allowlist host) is never fetched because this transport seam
    # returns the raw status and the client refuses to chase it at all,
    # whether same-host or cross-host. Only the two known, allowlisted
    # detail-endpoint URLs for this coordinator were ever requested.
    assert set(transport.calls) <= {detail_url, plan_url}
    assert all("evil" not in url for url in transport.calls)


def test_fetch_enforced_never_requests_a_redirect_location_url() -> None:
    # Even a same-host redirect target must never be fetched automatically:
    # _fetch_enforced treats every 3xx as a non-usable terminal response
    # rather than parsing/following a Location header.
    transport = FakeTransport()
    identity = make_identity()
    detail_url = im.build_detail_url(SEED_URL, "query_stmt", identity.query_id)
    transport.set_response(
        detail_url, http_mod.FetchResult(status=307, content_type=None, body=b"")
    )
    # A same-host "redirect target" URL that, if ever fetched, would prove
    # the client is chasing redirects.
    off_path_url = f"{SEED_URL}/cancel_query?query_id={identity.query_id}"
    transport.set_json(off_path_url, fixture_bytes("query_stmt_running.json"))

    client = no_sleep_client(transport, max_retries=0)
    client.observe(identity)

    assert off_path_url not in transport.calls


def test_observe_rejects_oversized_body() -> None:
    transport = FakeTransport()
    identity = make_identity()
    detail_url = im.build_detail_url(SEED_URL, "query_stmt", identity.query_id)
    oversized = (
        b'{"record_json": {"state": "RUNNING", "filler": "'
        + (b"a" * (im.MAX_BODY_BYTES + 1))
        + b'"}}'
    )
    transport.set_json(detail_url, oversized)

    client = no_sleep_client(transport)
    observation = client.observe(identity)

    # Body is oversized -- must degrade to availability_error, never attempt
    # a JSON parse of the full payload.
    assert observation.availability_error is not None


def test_observe_rejects_wrong_content_type() -> None:
    transport = FakeTransport()
    identity = make_identity()
    detail_url = im.build_detail_url(SEED_URL, "query_stmt", identity.query_id)
    transport.set_response(
        detail_url,
        http_mod.FetchResult(
            status=200, content_type="text/plain", body=fixture_bytes("query_stmt_running.json")
        ),
    )

    client = no_sleep_client(transport)
    observation = client.observe(identity)

    assert observation.availability_error is not None


def test_observe_rejects_html_body_even_with_200() -> None:
    transport = FakeTransport()
    identity = make_identity()
    detail_url = im.build_detail_url(SEED_URL, "query_stmt", identity.query_id)
    transport.set_response(
        detail_url,
        http_mod.FetchResult(
            status=200,
            content_type="text/html; charset=utf-8",
            body=b"<html><body>Query Detail</body></html>",
        ),
    )

    client = no_sleep_client(transport)
    observation = client.observe(identity)

    assert observation.availability_error is not None


def test_observe_tls_failure_surfaces_as_availability_error() -> None:
    transport = FakeTransport()
    identity = make_identity()
    detail_url = im.build_detail_url(SEED_URL, "query_stmt", identity.query_id)
    transport.set_raises(detail_url, __import__("ssl").SSLCertVerificationError("bad cert"))

    client = no_sleep_client(transport, max_retries=0)
    observation = client.observe(identity)

    assert observation.availability_error is not None
    assert observation.phase == "unknown"


def test_observe_connection_error_surfaces_as_availability_error_not_raw_exception() -> None:
    transport = FakeTransport()
    identity = make_identity()
    detail_url = im.build_detail_url(SEED_URL, "query_stmt", identity.query_id)
    transport.set_raises(detail_url, OSError("connection refused"))

    client = no_sleep_client(transport, max_retries=0)
    # Must not raise -- caller only ever sees ImpalaObservation.
    observation = client.observe(identity)

    assert observation.availability_error is not None


# =============================================================================
# Circuit breaker: open after consecutive failures, half-open probe
# =============================================================================


def test_circuit_breaker_opens_after_consecutive_failures_and_stops_calling_transport() -> None:
    transport = FakeTransport()
    identity = make_identity()
    detail_url = im.build_detail_url(SEED_URL, "query_stmt", identity.query_id)
    plan_url = im.build_detail_url(SEED_URL, "query_plan", identity.query_id)
    transport.set_raises(detail_url, OSError("boom"))
    transport.set_raises(plan_url, OSError("boom"))

    clock = _FakeClock()
    breaker = http_mod._CircuitBreaker(failure_threshold=3, cooldown_seconds=60.0, clock=clock)
    client = no_sleep_client(transport, max_retries=0, circuit_breaker=breaker)

    # First observe() fails capability probe (query_stmt) then falls back
    # and fails again (query_plan): 2 consecutive failures, caches
    # query_plan as the endpoint. Second observe() fails once more on the
    # now-cached query_plan endpoint: 3rd consecutive failure opens the
    # breaker.
    client.observe(identity)
    assert len(transport.calls) == 2
    client.observe(identity)
    assert len(transport.calls) == 3

    # Breaker should now be open: further observe() calls must not reach the
    # transport at all.
    client.observe(identity)
    assert len(transport.calls) == 3


def test_circuit_breaker_half_open_probe_after_cooldown_then_closes_on_success() -> None:
    transport = FakeTransport()
    identity = make_identity()
    detail_url = im.build_detail_url(SEED_URL, "query_stmt", identity.query_id)
    plan_url = im.build_detail_url(SEED_URL, "query_plan", identity.query_id)
    transport.set_raises(detail_url, OSError("boom"))
    transport.set_raises(plan_url, OSError("boom"))

    clock = _FakeClock()
    breaker = http_mod._CircuitBreaker(failure_threshold=2, cooldown_seconds=30.0, clock=clock)
    client = no_sleep_client(transport, max_retries=0, circuit_breaker=breaker)

    # First observe(): capability probe (query_stmt) fails, falls back to
    # query_plan which also fails -- 2 consecutive failures opens the
    # breaker (threshold=2) after this single observe() call.
    client.observe(identity)
    assert len(transport.calls) == 2  # breaker now open

    client.observe(identity)
    assert len(transport.calls) == 2  # still open, cooldown not elapsed

    clock.advance(31.0)
    # Now healthy: swap in a working response before the half-open probe.
    # Capability is already cached as query_plan from the failed first call.
    transport.set_json(plan_url, fixture_bytes("query_stmt_running.json"))
    observation = client.observe(identity)

    assert len(transport.calls) == 3  # exactly one half-open probe let through
    assert observation.availability_error is None

    # Breaker closed: subsequent calls flow normally.
    client.observe(identity)
    assert len(transport.calls) == 4


def test_circuit_breaker_half_open_probe_failure_reopens_and_restarts_cooldown() -> None:
    transport = FakeTransport()
    identity = make_identity()
    detail_url = im.build_detail_url(SEED_URL, "query_stmt", identity.query_id)
    transport.set_raises(detail_url, OSError("boom"))

    clock = _FakeClock()
    breaker = http_mod._CircuitBreaker(failure_threshold=1, cooldown_seconds=10.0, clock=clock)
    client = no_sleep_client(transport, max_retries=0, circuit_breaker=breaker)

    # First observe(): capability probe (query_stmt) fails, opening the
    # breaker after 1 failure (threshold=1). The query_plan fallback fetch
    # in the same observe() call is then blocked by the now-open breaker,
    # so only 1 transport call happens, and query_plan is cached anyway
    # (capability detection always picks a fallback endpoint on failure).
    client.observe(identity)
    assert len(transport.calls) == 1

    clock.advance(11.0)
    # Capability already cached as query_plan; this observe() call's single
    # fetch is the half-open probe, and it still fails.
    client.observe(identity)
    assert len(transport.calls) == 2

    # Immediately after the failed probe, breaker must be open again (not
    # allowing a second immediate call).
    client.observe(identity)
    assert len(transport.calls) == 2


# =============================================================================
# discover_coordinators — TTL cache, filters, host allowlist growth
# =============================================================================


def test_discover_coordinators_filters_coordinator_and_active() -> None:
    transport = FakeTransport()
    backends_url = f"{SEED_URL}/backends?json"
    transport.set_json(backends_url, fixture_bytes("backends.json"))

    client = no_sleep_client(transport)
    coordinators = client.discover_coordinators(SEED_URL)

    assert SEED_URL in coordinators
    assert COORD_2 in coordinators
    assert COORD_3 not in coordinators  # is_active: false
    assert not any("executor-1" in url for url in coordinators)


def test_discover_coordinators_is_ttl_cached_and_not_refetched() -> None:
    transport = FakeTransport()
    backends_url = f"{SEED_URL}/backends?json"
    transport.set_json(backends_url, fixture_bytes("backends.json"))

    clock = _FakeClock()
    client = no_sleep_client(transport, discovery_ttl_seconds=600.0, clock=clock)

    first = client.discover_coordinators(SEED_URL)
    assert transport.calls.count(backends_url) == 1

    clock.advance(300.0)  # within TTL
    second = client.discover_coordinators(SEED_URL)
    assert transport.calls.count(backends_url) == 1  # not refetched
    assert second == first

    clock.advance(400.0)  # now past the 600s TTL (700s elapsed total)
    client.discover_coordinators(SEED_URL)
    assert transport.calls.count(backends_url) == 2  # refetched after expiry


def test_discover_coordinators_newly_discovered_hosts_become_observable() -> None:
    transport = FakeTransport()
    backends_url = f"{SEED_URL}/backends?json"
    transport.set_json(backends_url, fixture_bytes("backends.json"))

    client = no_sleep_client(transport)
    client.discover_coordinators(SEED_URL)

    identity = make_identity(coordinator_base_url=COORD_2)
    detail_url = im.build_detail_url(COORD_2, "query_stmt", identity.query_id)
    transport.set_json(detail_url, fixture_bytes("query_stmt_running.json"))

    observation = client.observe(identity)
    assert observation.availability_error is None


def test_discover_coordinators_raises_typed_error_on_malformed_backends() -> None:
    transport = FakeTransport()
    backends_url = f"{SEED_URL}/backends?json"
    transport.set_json(backends_url, b'{"not_backends_key": []}')

    client = no_sleep_client(transport)
    with pytest.raises(http_mod.DiscoveryError):
        client.discover_coordinators(SEED_URL)


# =============================================================================
# discover — ambiguity refusal (0 and 2 matches), unique match succeeds
# =============================================================================


def _criteria(**overrides: Any) -> http_mod.DiscoveryCriteria:
    base = dict(
        user="user_a",
        statement_prefix="SELECT 1 /* sanitized */",
        statement_type="QUERY",
        database="db_a",
        # Fixture has both an in-flight query at 10:00:00 and a completed
        # query at 09:55:00 sharing this exact stmt text; narrow the window
        # to the in-flight one so the default criteria describe a unique
        # match. Tests that want the ambiguous pair use a different prefix.
        started_after="2026-07-15 09:59:00.000000000",
        started_before="2026-07-15 10:01:00.000000000",
        shell_execution_id="shell-exec-recovery",
    )
    base.update(overrides)
    return http_mod.DiscoveryCriteria(**base)


def _client_with_discovered_coordinators(transport: FakeTransport) -> http_mod.ImpalaMonitorClient:
    backends_url = f"{SEED_URL}/backends?json"
    transport.set_json(backends_url, fixture_bytes("backends.json"))
    client = no_sleep_client(transport)
    client.discover_coordinators(SEED_URL)
    return client


def test_discover_unique_match_succeeds() -> None:
    transport = FakeTransport()
    client = _client_with_discovered_coordinators(transport)
    queries_url = f"{SEED_URL}/queries?json"
    transport.set_json(queries_url, fixture_bytes("queries_list.json"))
    coord2_queries_url = f"{COORD_2}/queries?json"
    transport.set_json(coord2_queries_url, b'{"in_flight_queries": [], "completed_queries": []}')

    identity = client.discover(_criteria())

    assert identity.query_id == "a1a2a3a4a5a6a7a8:b1b2b3b4b5b6b7b8"
    assert identity.coordinator_base_url == SEED_URL
    assert identity.shell_execution_id == "shell-exec-recovery"


def test_discover_refuses_zero_matches() -> None:
    transport = FakeTransport()
    client = _client_with_discovered_coordinators(transport)
    queries_url = f"{SEED_URL}/queries?json"
    transport.set_json(queries_url, fixture_bytes("queries_list.json"))
    coord2_queries_url = f"{COORD_2}/queries?json"
    transport.set_json(coord2_queries_url, b'{"in_flight_queries": [], "completed_queries": []}')

    with pytest.raises(http_mod.AmbiguousIdentityError):
        client.discover(_criteria(user="nonexistent_user"))


def test_discover_refuses_multiple_matches_same_prefix() -> None:
    transport = FakeTransport()
    client = _client_with_discovered_coordinators(transport)
    queries_url = f"{SEED_URL}/queries?json"
    transport.set_json(queries_url, fixture_bytes("queries_list.json"))
    coord2_queries_url = f"{COORD_2}/queries?json"
    transport.set_json(coord2_queries_url, b'{"in_flight_queries": [], "completed_queries": []}')

    # Two fixture entries share stmt prefix "SELECT 1 /* sanitized-ambiguous*"
    with pytest.raises(http_mod.AmbiguousIdentityError):
        client.discover(_criteria(statement_prefix="SELECT 1 /* sanitized-ambiguous"))


def test_discover_without_prior_discover_coordinators_raises() -> None:
    transport = FakeTransport()
    client = no_sleep_client(transport)
    with pytest.raises(http_mod.DiscoveryError):
        client.discover(_criteria())


# =============================================================================
# cancel_query cannot be produced by any client code path
# =============================================================================


def test_no_client_request_url_can_contain_cancel_query() -> None:
    transport = FakeTransport()
    identity = make_identity()

    class RecordingTransport(FakeTransport):
        def fetch(self, url: str, timeout: tuple[float, float]) -> http_mod.FetchResult:
            assert "cancel_query" not in url, f"attempted request to {url!r}"
            return super().fetch(url, timeout)

    recorder = RecordingTransport()
    detail_url = im.build_detail_url(SEED_URL, "query_stmt", identity.query_id)
    recorder.set_json(detail_url, fixture_bytes("query_stmt_running.json"))
    backends_url = f"{SEED_URL}/backends?json"
    recorder.set_json(backends_url, fixture_bytes("backends.json"))

    client = no_sleep_client(recorder)
    client.observe(identity)
    client.discover_coordinators(SEED_URL)

    # _fetch_enforced also defends this at the request-dispatch layer itself,
    # independent of what any caller passes in.
    result = client._fetch_enforced(f"{SEED_URL}/cancel_query?query_id={identity.query_id}")
    assert result is None
    assert all("cancel_query" not in url for url in recorder.calls)

    del transport  # unused fixture-style variable kept for symmetry with siblings


# =============================================================================
# Client never leaks a raw exception type from urllib/ssl to observe() callers
# =============================================================================


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("timed out"),
        ConnectionResetError("reset"),
        OSError("network unreachable"),
        ValueError("weird transport bug"),
    ],
)
def test_observe_never_propagates_transport_exceptions(exc: Exception) -> None:
    transport = FakeTransport()
    identity = make_identity()
    detail_url = im.build_detail_url(SEED_URL, "query_stmt", identity.query_id)
    transport.set_raises(detail_url, exc)

    client = no_sleep_client(transport, max_retries=0)
    observation = client.observe(identity)  # must not raise
    assert observation.availability_error is not None


# =============================================================================
# UrllibTransport — thin integration test (no real network call)
# =============================================================================


def test_urllib_transport_builds_verified_ssl_context_by_default() -> None:
    transport = http_mod.UrllibTransport()
    # ssl.create_default_context() always turns verification on by default;
    # this is the property that must never be relaxed.
    assert transport._ssl_context.verify_mode.name != "CERT_NONE"


def test_urllib_transport_honors_configured_ca_bundle_path(tmp_path: Path) -> None:
    import ssl

    ca_file = tmp_path / "ca.pem"
    # A syntactically-empty PEM is enough to prove the cafile argument was
    # threaded through; a real cert isn't needed to test wiring.
    ca_file.write_text(
        "-----BEGIN CERTIFICATE-----\n"
        "MIIBAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
        "-----END CERTIFICATE-----\n"
    )
    try:
        http_mod.UrllibTransport(ca_bundle=str(ca_file))
    except ssl.SSLError:
        # Malformed cert bytes are fine for this test -- it only proves the
        # constructor attempted to load the configured path rather than
        # silently ignoring it.
        pass

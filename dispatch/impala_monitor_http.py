"""Read-only HTTP adapter for Impala's debug web server.

Slice 3 of ``docs/research/impala-monitoring-implementation-plan.md``. This
module extends the pure layer in ``dispatch/impala_monitor.py`` (identity
dataclasses, state/progress parsing, URL validation) with the transport half:
an injected ``Transport`` protocol, a stdlib ``urllib.request``-backed
production implementation, and ``ImpalaMonitorClient``, which is the only
object in Dispatch that knows how to turn a ``QueryIdentity`` or discovery
criteria into an HTTP request.

Kept in a sibling module (not merged into ``dispatch/impala_monitor.py``) so
the pure layer stays importable with no HTTP side effects — see
``tests/test_impala_monitor.py::test_importing_impala_monitor_pulls_in_no_http_or_textual_modules``.

Security posture (never relaxed here or by any caller):

- TLS verification is never disabled; ``ssl.create_default_context()`` is
  always used, optionally pointed at a configured CA bundle.
- Every request's host must already be on the approved allowlist (the seed
  coordinator plus coordinators returned by a validated ``/backends?json``
  discovery) before a socket is opened.
- Redirects are never followed automatically; a redirect to a host outside
  the allowlist is rejected outright.
- Only the fixed detail-endpoint enum from ``dispatch.impala_monitor`` can be
  requested — there is no function here that fetches an arbitrary path, and
  a dedicated test asserts no code path can produce a ``cancel_query`` URL.
- This client is synchronous/blocking by design. It imports no ``textual``
  and no ``asyncio``; callers are responsible for running it in a thread
  (e.g. ``asyncio.to_thread``) off Textual's event loop.
"""

from __future__ import annotations

import json
import random
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import urlsplit

from . import config
from .impala_monitor import MAX_BODY_BYTES as MAX_BODY_BYTES_TRANSPORT
from .impala_monitor import (
    ImpalaObservation,
    QueryIdentity,
    Relation,
    build_detail_url,
    parse_query_detail,
    validate_coordinator_url,
    validate_query_id,
)

CONNECT_TIMEOUT_SECONDS = 3.0
READ_TIMEOUT_SECONDS = 10.0

MAX_RETRIES = 2
RETRY_BASE_DELAY_SECONDS = 0.2
RETRY_JITTER_SECONDS = 0.2

CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
CIRCUIT_BREAKER_COOLDOWN_SECONDS = 30.0

DISCOVERY_TTL_SECONDS = 10 * 60

_JSON_CONTENT_TYPE_PREFIX = "application/json"

_CAPABILITY_ENDPOINTS = ("query_stmt", "query_plan")

_RELATION_VALUES = ("initial", "transparent_retry")


class MonitorHttpError(Exception):
    """Base class for exceptions raised by discovery operations.

    ``observe`` never raises this (or any exception) to callers — targeted
    poll failures always degrade to ``ImpalaObservation.availability_error``.
    These types are for the discovery surface (``discover_coordinators`` and
    ``discover``), which plan §Slice 3 allows to raise typed exceptions.
    """


class AmbiguousIdentityError(MonitorHttpError):
    """Raised by ``discover`` when zero or multiple queries match criteria."""


class DiscoveryError(MonitorHttpError):
    """Raised when coordinator discovery itself cannot produce a result."""


@dataclass(frozen=True)
class FetchResult:
    status: int
    content_type: str | None
    body: bytes


class Transport(Protocol):
    """Injected transport seam. Production impl is ``UrllibTransport``."""

    def fetch(self, url: str, timeout: tuple[float, float]) -> FetchResult:
        """Perform one GET request and return (status, content_type, body).

        Implementations must not follow redirects across hosts and must cap
        the number of bytes read from the body at ``MAX_BODY_BYTES`` (see
        ``dispatch.impala_monitor.MAX_BODY_BYTES``) to avoid buffering an
        unbounded response before the caller gets a chance to reject it.
        """
        ...


@dataclass(frozen=True)
class DiscoveryCriteria:
    """Bounded recovery-discovery criteria for ``ImpalaMonitorClient.discover``.

    Operator-triggered only; never called from a refresh loop (plan §Slice
    3 / research note "Query identity and coordinator discovery").
    """

    user: str
    statement_prefix: str
    statement_type: str
    database: str
    started_after: str  # ISO-8601 UTC, inclusive
    started_before: str  # ISO-8601 UTC, inclusive
    shell_execution_id: str
    relation: Relation = "initial"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class _CircuitBreaker:
    """Per-coordinator circuit breaker: open after N consecutive failures.

    Half-open: after the cooldown elapses, exactly one probe request is let
    through; success closes the breaker, failure re-opens it and restarts
    the cooldown.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        cooldown_seconds: float = CIRCUIT_BREAKER_COOLDOWN_SECONDS,
        clock: object | None = None,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._consecutive_failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._probing: set[str] = set()
        self._clock = clock if clock is not None else time.monotonic

    def _now(self) -> float:
        return self._clock()  # type: ignore[operator, no-any-return]

    def allow(self, host: str) -> bool:
        opened_at = self._opened_at.get(host)
        if opened_at is None:
            return True
        if self._now() - opened_at < self._cooldown_seconds:
            return False
        # Cooldown elapsed: allow exactly one half-open probe.
        if host in self._probing:
            return False
        self._probing.add(host)
        return True

    def record_success(self, host: str) -> None:
        self._consecutive_failures[host] = 0
        self._opened_at.pop(host, None)
        self._probing.discard(host)

    def record_failure(self, host: str) -> None:
        self._probing.discard(host)
        count = self._consecutive_failures.get(host, 0) + 1
        self._consecutive_failures[host] = count
        if count >= self._failure_threshold:
            self._opened_at[host] = self._now()

    def is_open(self, host: str) -> bool:
        return not self.allow(host)


class UrllibTransport:
    """Production ``Transport`` on ``urllib.request`` with verified TLS.

    Verification is never disabled. The SSL context always comes from
    ``ssl.create_default_context()``, optionally pointed at a configured CA
    bundle path (``dispatch.config.impala_monitor_ca_bundle``); the default
    (no override) is the system trust store. Redirects are never followed
    automatically — this transport builds a single request with
    ``urllib.request.urlopen`` via a custom opener with no
    ``HTTPRedirectHandler`` reinstalled, so a redirect response is returned
    as-is (a 3xx status) rather than silently re-requested by the library.
    """

    def __init__(self, *, ca_bundle: str | None = None) -> None:
        self._ssl_context = ssl.create_default_context(cafile=ca_bundle)

    def fetch(self, url: str, timeout: tuple[float, float]) -> FetchResult:
        connect_timeout, read_timeout = timeout
        # urllib.request has one socket timeout knob; use the larger of the
        # two so a slow-but-connecting host isn't cut off mid-read, while
        # still bounding total wait time to a small multiple of read_timeout.
        socket_timeout = max(connect_timeout, read_timeout)
        request = urllib.request.Request(url, method="GET")
        opener = urllib.request.build_opener(
            _NoRedirectHandler(), urllib.request.HTTPSHandler(context=self._ssl_context)
        )
        try:
            response = opener.open(request, timeout=socket_timeout)
        except urllib.error.HTTPError as exc:
            body = _read_capped(exc, MAX_BODY_BYTES_TRANSPORT)
            return FetchResult(
                status=exc.code, content_type=exc.headers.get("Content-Type"), body=body
            )
        with response:
            body = _read_capped(response, MAX_BODY_BYTES_TRANSPORT)
            status = getattr(response, "status", None) or response.getcode()
            content_type = response.headers.get("Content-Type")
            return FetchResult(status=status, content_type=content_type, body=body)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Disables automatic redirect following; surfaces the raw 3xx response."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _read_capped(response: object, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    read = getattr(response, "read")  # noqa: B009
    while True:
        chunk = read(65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            # Stop reading immediately; caller enforces the hard cap on the
            # concatenated body, this just prevents unbounded buffering.
            break
    return b"".join(chunks)


def _is_json_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    return content_type.split(";", 1)[0].strip().lower() == _JSON_CONTENT_TYPE_PREFIX


@dataclass
class _CoordinatorCapability:
    endpoint: str  # one of _CAPABILITY_ENDPOINTS


@dataclass
class _DiscoveryCache:
    coordinators: list[str]
    fetched_at: float


class ImpalaMonitorClient:
    """Synchronous, read-only client for the Impala debug web server.

    Public surface: ``observe``, ``discover_coordinators``, ``discover``.
    Nothing else is exposed — there is deliberately no generic "fetch this
    URL" method.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        allow_http: bool | None = None,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        read_timeout: float = READ_TIMEOUT_SECONDS,
        max_retries: int = MAX_RETRIES,
        discovery_ttl_seconds: float = DISCOVERY_TTL_SECONDS,
        circuit_breaker: _CircuitBreaker | None = None,
        random_source: object | None = None,
        clock: object | None = None,
    ) -> None:
        self._transport = transport
        self._allow_http = config.impala_monitor_allow_http() if allow_http is None else allow_http
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._max_retries = max_retries
        self._discovery_ttl_seconds = discovery_ttl_seconds
        self._breaker = circuit_breaker if circuit_breaker is not None else _CircuitBreaker()
        self._random = random_source if random_source is not None else random
        self._clock = clock if clock is not None else time.monotonic

        self._allowed_hosts: set[str] = set()
        self._capabilities: dict[str, _CoordinatorCapability] = {}
        self._discovery_cache: dict[str, _DiscoveryCache] = {}

    def _now(self) -> float:
        return self._clock()  # type: ignore[operator, no-any-return]

    def _jitter(self) -> float:
        return self._random.uniform(0.0, RETRY_JITTER_SECONDS)  # type: ignore[union-attr, no-any-return]

    # -- Host allowlist ----------------------------------------------------

    def _authorize_seed(self, base_url: str) -> str:
        validated = validate_coordinator_url(base_url, allow_http=self._allow_http)
        self._allowed_hosts.add(_host_of(validated))
        return validated

    def _authorize_discovered(self, base_url: str) -> str | None:
        try:
            validated = validate_coordinator_url(base_url, allow_http=self._allow_http)
        except Exception:
            return None
        self._allowed_hosts.add(_host_of(validated))
        return validated

    # -- Low-level fetch with enforcement -----------------------------------

    def _fetch_enforced(self, url: str) -> FetchResult | None:
        """Fetch ``url`` with all Slice-3 hard limits enforced.

        Returns ``None`` (never raises) when every retry has been exhausted
        or the circuit breaker is open, so ``observe`` can degrade to
        ``availability_error`` and discovery callers can raise their own
        typed exception with proper context.
        """
        host = _host_of(url)
        if host not in self._allowed_hosts:
            return None
        if "cancel_query" in url:
            # Defense in depth: build_detail_url already restricts the
            # endpoint enum, but no request may ever leave this method if
            # its URL contains this substring, full stop.
            return None
        if self._breaker.is_open(host):
            return None

        timeout = (self._connect_timeout, self._read_timeout)
        attempts = self._max_retries + 1
        for attempt in range(attempts):
            if attempt > 0:
                if self._breaker.is_open(host):
                    # A failure on a prior attempt this call opened the
                    # breaker (or consumed the single half-open probe slot).
                    # Stop retrying immediately rather than firing further
                    # requests at a host the breaker just clamped down on.
                    return None
                time.sleep(RETRY_BASE_DELAY_SECONDS * attempt + self._jitter())
            try:
                result = self._transport.fetch(url, timeout)
            except Exception:
                self._breaker.record_failure(host)
                continue

            if result.status >= 500:
                self._breaker.record_failure(host)
                continue

            if 300 <= result.status < 400:
                # No cross-host redirect following. A same-host redirect is
                # still not followed automatically (no Location parsing
                # here) — the caller only ever gets the final validated
                # endpoint URL it asked for, so any 3xx is treated as a
                # non-usable response rather than being chased. Counts as
                # one breaker failure, same as any other unusable response,
                # and does not consume further retry attempts.
                self._breaker.record_failure(host)
                return None

            self._breaker.record_success(host)
            return result

        # Every attempt failed (exception or 5xx): each already recorded
        # exactly one breaker failure above, so nothing more to record here.
        return None

    def _fetch_json_object(self, url: str) -> dict | None:
        """Fetch ``url`` and return a parsed JSON object, or ``None`` on any failure."""
        result = self._fetch_enforced(url)
        if result is None:
            return None
        if result.status != 200:
            return None
        if not _is_json_content_type(result.content_type):
            return None
        if len(result.body) > MAX_BODY_BYTES_TRANSPORT:
            return None
        try:
            data = json.loads(result.body)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    # -- observe -------------------------------------------------------------

    def observe(self, identity: QueryIdentity) -> ImpalaObservation:
        """Poll one coordinator/query and return a parsed observation.

        Never raises. Capability detection tries ``/query_stmt?json`` once
        per coordinator, falling back to ``/query_plan?json`` on a 404 or an
        unrecognized body shape, and caches the winning endpoint per
        coordinator host so subsequent polls skip the probe.
        """
        observed_at = _utc_now_iso()
        try:
            base_url = self._authorize_seed(identity.coordinator_base_url)
        except Exception:
            return _unavailable(
                "invalid coordinator URL or query id", detail_url="", observed_at=observed_at
            )

        try:
            validate_query_id(identity.query_id)
        except Exception:
            return _unavailable(
                "invalid coordinator URL or query id", detail_url="", observed_at=observed_at
            )

        host = _host_of(base_url)
        endpoint, probe_result = self._capability_for(host, base_url, identity.query_id)
        detail_url = build_detail_url(
            base_url, endpoint, identity.query_id, allow_http=self._allow_http
        )

        # Reuse the capability probe's response instead of refetching: the
        # probe already requested exactly this URL when it decided
        # query_stmt was usable, so there is no need for a second round trip
        # against the same coordinator for the same query.
        result = probe_result if probe_result is not None else self._fetch_enforced(detail_url)
        if result is None:
            return _unavailable(
                "monitoring unavailable", detail_url=detail_url, observed_at=observed_at
            )
        if result.status != 200:
            return _unavailable(
                "monitoring unavailable", detail_url=detail_url, observed_at=observed_at
            )
        if not _is_json_content_type(result.content_type):
            return _unavailable(
                "unexpected content type", detail_url=detail_url, observed_at=observed_at
            )

        observation = parse_query_detail(result.body, identity)
        # parse_query_detail derives detail_url itself from identity+endpoint
        # "query_stmt"; when capability fallback chose query_plan, keep the
        # observation's detail_url in sync with what was actually fetched.
        if endpoint != "query_stmt":
            observation = _with_detail_url(observation, detail_url)
        return observation

    def _capability_for(
        self, host: str, base_url: str, query_id: str
    ) -> tuple[str, FetchResult | None]:
        """Return the coordinator's chosen detail endpoint.

        On the first call for a given coordinator host this also probes
        ``query_stmt`` and returns that probe's raw ``FetchResult`` alongside
        the choice, so ``observe`` can reuse the same response instead of
        making a second request for the same URL. On every subsequent call
        for an already-cached host, no request is made and ``None`` is
        returned for the (unused) probe result.
        """
        cached = self._capabilities.get(host)
        if cached is not None:
            return cached.endpoint, None

        # Probe query_stmt once; fall back to query_plan on 404 or an
        # unrecognized shape (missing record_json and no top-level error /
        # plan_metadata_unavailable marker, which would otherwise be a
        # legitimate empty/early-planning response).
        primary_url = build_detail_url(
            base_url, "query_stmt", query_id, allow_http=self._allow_http
        )
        result = self._fetch_enforced(primary_url)
        if (
            result is not None
            and result.status == 200
            and _is_json_content_type(result.content_type)
            and len(result.body) <= MAX_BODY_BYTES_TRANSPORT
        ):
            data = _try_parse_object(result.body)
            if data is not None and _looks_like_query_detail(data):
                self._capabilities[host] = _CoordinatorCapability(endpoint="query_stmt")
                return "query_stmt", result

        self._capabilities[host] = _CoordinatorCapability(endpoint="query_plan")
        return "query_plan", None

    # -- discover_coordinators -----------------------------------------------

    def discover_coordinators(self, seed_base_url: str) -> list[str]:
        """Return validated, active coordinator base URLs from ``/backends?json``.

        Cached per seed URL with a 10-minute TTL by default; never refetched
        within the TTL window regardless of how many observation ticks occur.
        Raises ``DiscoveryError`` if the seed itself is invalid or the
        backends document cannot be fetched/parsed.
        """
        try:
            seed = self._authorize_seed(seed_base_url)
        except Exception as exc:
            raise DiscoveryError(f"invalid seed coordinator URL: {seed_base_url!r}") from exc

        cached = self._discovery_cache.get(seed)
        if cached is not None and (self._now() - cached.fetched_at) < self._discovery_ttl_seconds:
            return list(cached.coordinators)

        backends_url = f"{seed}/backends?json"
        data = self._fetch_json_object(backends_url)
        if data is None:
            raise DiscoveryError(f"could not fetch /backends?json from {seed}")

        backends = data.get("backends")
        if not isinstance(backends, list):
            raise DiscoveryError("unexpected /backends?json shape")

        coordinators: list[str] = []
        for entry in backends:
            if not isinstance(entry, dict):
                continue
            if not (entry.get("is_coordinator") and entry.get("is_active")):
                continue
            webserver_url = entry.get("webserver_url")
            if not isinstance(webserver_url, str):
                continue
            validated = self._authorize_discovered(webserver_url)
            if validated is not None:
                coordinators.append(validated)

        self._discovery_cache[seed] = _DiscoveryCache(
            coordinators=coordinators, fetched_at=self._now()
        )
        return list(coordinators)

    # -- discover --------------------------------------------------------------

    def discover(self, criteria: DiscoveryCriteria) -> QueryIdentity:
        """Bounded recovery sweep over cached coordinators' ``/queries?json``.

        Matches on user + start window + statement prefix + statement type +
        database. Raises ``AmbiguousIdentityError`` on zero or more than one
        match across every coordinator swept — never guesses. Operator-
        triggered only; must not be called from a background refresh loop.
        """
        coordinators = self._cached_coordinators()
        if not coordinators:
            raise DiscoveryError("no cached coordinators available for discovery")

        matches: list[tuple[str, dict]] = []
        for base_url in coordinators:
            queries_url = f"{base_url}/queries?json"
            data = self._fetch_json_object(queries_url)
            if data is None:
                continue
            for bucket in ("in_flight_queries", "completed_queries"):
                entries = data.get(bucket)
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if isinstance(entry, dict) and _matches_criteria(entry, criteria):
                        matches.append((base_url, entry))

        if len(matches) != 1:
            raise AmbiguousIdentityError(
                f"discovery matched {len(matches)} queries for criteria (expected exactly 1)"
            )

        base_url, entry = matches[0]
        query_id = entry.get("query_id")
        if not isinstance(query_id, str):
            raise AmbiguousIdentityError("matched entry had no usable query_id")
        try:
            validate_query_id(query_id)
        except Exception as exc:
            raise AmbiguousIdentityError("matched entry had an invalid query_id") from exc

        relation = _validate_relation(criteria.relation)

        return QueryIdentity(
            coordinator_base_url=base_url,
            query_id=query_id,
            shell_execution_id=criteria.shell_execution_id,
            relation=relation,
            discovered_at=_utc_now_iso(),
        )

    def _cached_coordinators(self) -> list[str]:
        coordinators: list[str] = []
        for cache in self._discovery_cache.values():
            coordinators.extend(cache.coordinators)
        # De-duplicate while preserving order.
        seen: set[str] = set()
        unique: list[str] = []
        for url in coordinators:
            if url not in seen:
                seen.add(url)
                unique.append(url)
        return unique


def _validate_relation(value: str) -> Relation:
    """Narrow an arbitrary ``str`` to the ``Relation`` literal at runtime.

    ``DiscoveryCriteria.relation`` is statically typed as ``Relation``, but a
    caller can still hand ``discover`` an arbitrary string at runtime (e.g.
    from untrusted input); this rejects anything outside the declared
    literal instead of forwarding it into ``QueryIdentity`` unchecked.
    """
    if value not in _RELATION_VALUES:
        raise AmbiguousIdentityError(
            f"invalid relation {value!r}; expected one of {_RELATION_VALUES}"
        )
    return value  # type: ignore[return-value]


def _try_parse_object(body: bytes) -> dict | None:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _looks_like_query_detail(data: dict) -> bool:
    if isinstance(data.get("error"), str) and data.get("error"):
        return True  # a recognized "Unknown query id" shape, still query_stmt-compatible
    if data.get("plan_metadata_unavailable"):
        return True
    return isinstance(data.get("record_json"), dict)


def _matches_criteria(entry: dict, criteria: DiscoveryCriteria) -> bool:
    if entry.get("effective_user") != criteria.user:
        return False
    if entry.get("default_db") != criteria.database:
        return False
    if entry.get("stmt_type") != criteria.statement_type:
        return False
    stmt = entry.get("stmt")
    if not isinstance(stmt, str) or not stmt.startswith(criteria.statement_prefix):
        return False
    start_time = entry.get("start_time")
    if not isinstance(start_time, str):
        return False
    if not (criteria.started_after <= start_time <= criteria.started_before):
        return False
    return True


def _unavailable(message: str, *, detail_url: str, observed_at: str) -> ImpalaObservation:
    return ImpalaObservation(
        raw_state=None,
        phase="unknown",
        pool=None,
        scan_progress=None,
        query_progress=None,
        queued_duration=None,
        bytes_read=None,
        rows_fetched=None,
        last_event=None,
        status_summary=None,
        detail_url=detail_url,
        observed_at=observed_at,
        availability_error=message,
    )


def _with_detail_url(observation: ImpalaObservation, detail_url: str) -> ImpalaObservation:
    return ImpalaObservation(
        raw_state=observation.raw_state,
        phase=observation.phase,
        pool=observation.pool,
        scan_progress=observation.scan_progress,
        query_progress=observation.query_progress,
        queued_duration=observation.queued_duration,
        bytes_read=observation.bytes_read,
        rows_fetched=observation.rows_fetched,
        last_event=observation.last_event,
        status_summary=observation.status_summary,
        detail_url=detail_url,
        observed_at=observation.observed_at,
        availability_error=observation.availability_error,
    )

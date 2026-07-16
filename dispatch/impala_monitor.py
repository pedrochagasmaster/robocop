"""Pure, read-only Impala debug-web monitoring: identity, parsing, validation.

This module is the compatibility adapter described in
``docs/research/impala-debug-web-monitoring-contract.md``: Impala's debug web
server (``?json`` mode) is an implementation contract, not a versioned public
API, so every field here is optional-by-presence and unknown values are
preserved rather than rejected.

Slice 1 scope only (see
``docs/research/impala-monitoring-implementation-plan.md``): dataclasses,
state mapping, progress parsing, and URL/identity validation. There is
deliberately no network code and no Textual code in this module — see the
module-hygiene tests in ``tests/test_impala_monitor.py``.

Security posture (never relaxed here or by any caller):

- no function in this module can construct a ``cancel_query`` URL or any URL
  outside the fixed detail-endpoint enum;
- no function fetches an arbitrary URL;
- TLS verification is out of scope for this module (see Slice 3) and must
  never be disabled in product code.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Literal
from urllib.parse import quote, urlsplit

MAX_BODY_BYTES = 4 * 1024 * 1024  # 4 MiB; observed detail pages are ~80 KB.

Relation = Literal["initial", "transparent_retry"]
Phase = Literal[
    "preparing",
    "queued",
    "running",
    "succeeded",
    "failed",
    "retrying",
    "unknown",
]

_ALLOWED_DETAIL_ENDPOINTS = frozenset(
    {"query_stmt", "query_summary", "query_plan_text", "query_plan"}
)

_QUERY_ID_RE = re.compile(r"\A[0-9a-f]{16}:[0-9a-f]{16}\Z")
_PROGRESS_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\b")

_DEFAULT_PORTS = {"https": 443, "http": 80}


class IdentityValidationError(ValueError):
    """Raised when a coordinator URL or query ID fails validation."""


@dataclass(frozen=True)
class QueryIdentity:
    """Exact (coordinator, query) identity captured at the impala-shell seam."""

    coordinator_base_url: str  # scheme+host+port only, validated
    query_id: str  # validated hex:hex
    shell_execution_id: str
    relation: Relation
    discovered_at: str  # ISO-8601 UTC


@dataclass(frozen=True)
class ProgressCounter:
    """A defensively-parsed ``n / total`` counter with the original text."""

    completed: int | None
    total: int | None
    display: str  # original string, always retained


@dataclass(frozen=True)
class ImpalaObservation:
    """One poll's worth of parsed status for a single query attempt."""

    raw_state: str | None
    phase: Phase
    pool: str | None
    scan_progress: ProgressCounter | None
    query_progress: ProgressCounter | None
    queued_duration: str | None
    bytes_read: str | None
    rows_fetched: int | None
    last_event: str | None
    status_summary: str | None  # one line, truncated, never full error body
    detail_url: str
    observed_at: str
    availability_error: str | None  # set for plan_metadata_unavailable / unknown-id / parse failure


def map_state(raw_state: str | None, last_event: str | None) -> Phase:
    """Map Impala's web-facing ``state`` (+ ``last_event``) to a Dispatch phase.

    Exactly the research note's table:
    ``CREATED``->preparing; ``COMPILED``->queued iff
    ``last_event == "Queued"`` else preparing; ``RUNNING``->running;
    ``FINISHED``->succeeded; ``EXCEPTION``->failed; ``RETRYING``/``RETRIED``
    ->retrying; anything else (including ``None``) -> unknown, with
    ``raw_state`` preserved by the caller.
    """
    if raw_state == "CREATED":
        return "preparing"
    if raw_state == "COMPILED":
        return "queued" if last_event == "Queued" else "preparing"
    if raw_state == "RUNNING":
        return "running"
    if raw_state == "FINISHED":
        return "succeeded"
    if raw_state == "EXCEPTION":
        return "failed"
    if raw_state in ("RETRYING", "RETRIED"):
        return "retrying"
    return "unknown"


def parse_progress(display: str | None) -> ProgressCounter:
    """Defensively parse a ``n / total`` display string.

    ``N/A``, empty, ``None``, or any unparseable text keeps ``display`` with
    ``None`` counts rather than raising — the field shape has changed across
    Impala releases and some statements report no progress at all.
    """
    text = display if display is not None else ""
    match = _PROGRESS_RE.match(text)
    if not match:
        return ProgressCounter(completed=None, total=None, display=text)
    return ProgressCounter(completed=int(match.group(1)), total=int(match.group(2)), display=text)


def validate_query_id(qid: str) -> str:
    """Validate a query ID as anchored ``^[0-9a-f]{16}:[0-9a-f]{16}$``.

    Returns the same string on success; raises ``IdentityValidationError``
    otherwise. Case-sensitive (lowercase hex only) so callers cannot smuggle
    path-unsafe or ambiguous variants through.
    """
    if not isinstance(qid, str) or not _QUERY_ID_RE.match(qid):
        raise IdentityValidationError(f"invalid query id: {qid!r}")
    return qid


def _is_valid_hostname(host: str) -> bool:
    if not host or len(host) > 253:
        return False
    try:
        address = ip_address(host)
    except ValueError:
        pass
    else:
        # Coordinators are documented as DNS hostnames only (see the
        # research note); IPv4 literals are accepted for local/dev use, but
        # IPv6 literals are rejected rather than accepted-and-mangled, since
        # ``urlsplit().hostname`` strips the ``[...]`` brackets an IPv6
        # literal needs to round-trip through an HTTP client (Slice 3).
        return address.version == 4
    if host.endswith("."):
        host = host[:-1]
    labels = host.split(".")
    label_re = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")
    return all(label_re.match(label) for label in labels) and len(labels) >= 1


def validate_coordinator_url(url: str, *, allow_http: bool = False) -> str:
    """Normalize and validate a coordinator base URL.

    Returns the normalized ``scheme://host:port`` (no trailing slash, no
    userinfo, no path/query/fragment beyond an optional trailing ``/``) or
    raises ``IdentityValidationError``. Scheme must be ``https`` unless
    ``allow_http`` is explicitly set by config (dev/mock only) — this
    function never inspects environment state itself.
    """
    if not isinstance(url, str) or not url:
        raise IdentityValidationError("coordinator URL must be a non-empty string")

    parts = urlsplit(url)

    allowed_schemes = {"https"} | ({"http"} if allow_http else set())
    if parts.scheme not in allowed_schemes:
        raise IdentityValidationError(f"unsupported scheme: {parts.scheme!r}")

    if parts.username is not None or parts.password is not None:
        raise IdentityValidationError("coordinator URL must not contain userinfo")

    if parts.query or parts.fragment:
        raise IdentityValidationError("coordinator URL must not contain query or fragment")

    if parts.path not in ("", "/"):
        raise IdentityValidationError("coordinator URL must not contain a path")

    hostname = parts.hostname
    if not hostname or not _is_valid_hostname(hostname):
        raise IdentityValidationError(f"invalid hostname: {hostname!r}")

    try:
        port = parts.port
    except ValueError as exc:
        raise IdentityValidationError("invalid port") from exc

    if port is None:
        port = _DEFAULT_PORTS[parts.scheme]
    if not (0 < port <= 65535):
        raise IdentityValidationError(f"invalid port: {port!r}")

    return f"{parts.scheme}://{hostname}:{port}"


def build_detail_url(base: str, endpoint: str, query_id: str, *, allow_http: bool = False) -> str:
    """Build a detail URL for one of the fixed, read-only endpoints.

    ``endpoint`` must be one of ``query_stmt``, ``query_summary``,
    ``query_plan_text``, ``query_plan``. There is deliberately no function
    that fetches or builds an arbitrary path: any other endpoint (including
    ``cancel_query``) raises ``ValueError`` before the URL is assembled.

    ``allow_http`` defaults to ``False`` and is never inferred from ``base``
    itself — accepting an ``http://`` scheme is a caller-supplied, config-
    controlled dev/mock opt-in (see ``validate_coordinator_url``), not a
    property of the untrusted base URL string. A ``base`` carrying an
    ``http://`` scheme is rejected unless the caller explicitly passes
    ``allow_http=True``.
    """
    if endpoint not in _ALLOWED_DETAIL_ENDPOINTS:
        raise ValueError(f"endpoint not permitted: {endpoint!r}")
    validated_base = validate_coordinator_url(base, allow_http=allow_http)
    validated_qid = validate_query_id(query_id)
    return f"{validated_base}/{endpoint}?query_id={quote(validated_qid, safe='')}&json"


def _one_line(text: str, *, max_len: int = 200) -> str:
    """Collapse a possibly-multiline error into one truncated line."""
    first_line = text.splitlines()[0] if text else ""
    if len(first_line) > max_len:
        first_line = first_line[: max_len - 1] + "…"
    return first_line


def _availability_observation(
    *,
    availability_error: str,
    detail_url: str,
    observed_at: str,
    phase: Phase = "unknown",
    raw_state: str | None = None,
) -> ImpalaObservation:
    return ImpalaObservation(
        raw_state=raw_state,
        phase=phase,
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
        availability_error=availability_error,
    )


def parse_query_detail(payload: bytes, identity: QueryIdentity) -> ImpalaObservation:
    """Parse one ``/query_stmt?json``-shaped body into an ``ImpalaObservation``.

    Never raises: every failure (oversized body, invalid JSON, non-object
    JSON, top-level ``error``, ``plan_metadata_unavailable``, missing
    ``record_json``, or an ``identity.coordinator_base_url`` that fails
    validation — e.g. an ``http://`` URL with no dev/mock opt-in) is mapped
    to an observation carrying ``availability_error`` instead of an
    exception escaping to callers. The "phase preserved from prior
    knowledge" behavior mentioned in the plan for an evicted/unknown-id
    response is the *service* layer's job (later slices); this function only
    reports what today's payload says, which for an unknown-id response is
    "unknown".

    ``build_detail_url`` is always called with ``allow_http=False`` here:
    this pure layer has no config to consult, so it never self-authorizes an
    ``http://`` coordinator URL. A coordinator base URL that requires the
    dev/mock HTTP opt-in must already have been validated with that opt-in
    by the caller that produced ``identity`` (see
    ``validate_coordinator_url``); this function will not silently downgrade
    to HTTP on its own, and will not silently upgrade an already-invalid
    identity into a working detail URL either — it reports
    ``availability_error`` instead.
    """
    observed_at = identity.discovered_at
    try:
        detail_url = build_detail_url(
            identity.coordinator_base_url, "query_stmt", identity.query_id
        )
    except IdentityValidationError:
        return _availability_observation(
            availability_error="invalid coordinator URL or query id",
            detail_url="",
            observed_at=observed_at,
        )

    if len(payload) > MAX_BODY_BYTES:
        return _availability_observation(
            availability_error="response body too large",
            detail_url=detail_url,
            observed_at=observed_at,
        )

    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        return _availability_observation(
            availability_error="malformed response body",
            detail_url=detail_url,
            observed_at=observed_at,
        )

    if not isinstance(data, dict):
        return _availability_observation(
            availability_error="unexpected response shape",
            detail_url=detail_url,
            observed_at=observed_at,
        )

    top_level_error = data.get("error")
    if isinstance(top_level_error, str) and top_level_error:
        return _availability_observation(
            availability_error=_one_line(top_level_error),
            detail_url=detail_url,
            observed_at=observed_at,
        )

    if data.get("plan_metadata_unavailable"):
        return _availability_observation(
            availability_error="plan metadata unavailable",
            detail_url=detail_url,
            observed_at=observed_at,
            phase="preparing",
        )

    record = data.get("record_json")
    if not isinstance(record, dict):
        return _availability_observation(
            availability_error="missing record_json",
            detail_url=detail_url,
            observed_at=observed_at,
        )

    raw_state = record.get("state")
    raw_state = raw_state if isinstance(raw_state, str) else None
    last_event = record.get("last_event")
    last_event = last_event if isinstance(last_event, str) else None
    phase = map_state(raw_state, last_event)

    pool = record.get("resource_pool")
    pool = pool if isinstance(pool, str) else None

    progress_display = record.get("progress")
    scan_progress = parse_progress(progress_display) if isinstance(progress_display, str) else None

    query_progress_display = record.get("query_progress")
    query_progress = (
        parse_progress(query_progress_display) if isinstance(query_progress_display, str) else None
    )

    queued_duration = record.get("queued_duration")
    queued_duration = queued_duration if isinstance(queued_duration, str) else None

    bytes_read = record.get("bytes_read")
    bytes_read = bytes_read if isinstance(bytes_read, str) else None

    rows_fetched = record.get("rows_fetched")
    rows_fetched = rows_fetched if isinstance(rows_fetched, int) else None

    status_summary: str | None = None
    top_level_status = data.get("status")
    if isinstance(top_level_status, str) and top_level_status and top_level_status != "OK":
        status_summary = _one_line(top_level_status)

    return ImpalaObservation(
        raw_state=raw_state,
        phase=phase,
        pool=pool,
        scan_progress=scan_progress,
        query_progress=query_progress,
        queued_duration=queued_duration,
        bytes_read=bytes_read,
        rows_fetched=rows_fetched,
        last_event=last_event,
        status_summary=status_summary,
        detail_url=detail_url,
        observed_at=observed_at,
        availability_error=None,
    )

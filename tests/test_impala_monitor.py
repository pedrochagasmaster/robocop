"""Table-driven tests for the pure Impala monitoring layer.

Covers every fixture in ``tests/fixtures/impala_monitor/``, the state-mapping
table, availability-error paths, validator rejections, field-deletion
robustness, and two tripwires: no fixture leaks sensitive content, and the
module pulls in no HTTP/Textual symbols.

No network. No UI. No ``scr/``. See
``docs/research/impala-monitoring-implementation-plan.md`` (Slice 1) for the
authoritative spec this file implements.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from dispatch import impala_monitor as im

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "impala_monitor"

BASE_URL = "https://coordinator-1.internal.example:25443"


def load_fixture(name: str) -> Any:
    path = FIXTURES_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


def fixture_bytes(name: str) -> bytes:
    path = FIXTURES_DIR / name
    return path.read_bytes()


def make_identity(query_id: str = "1a2b3c4d5e6f7081:9192a3b4c5d6e7f8") -> im.QueryIdentity:
    return im.QueryIdentity(
        coordinator_base_url=BASE_URL,
        query_id=query_id,
        shell_execution_id="shell-exec-1",
        relation="initial",
        discovered_at="2026-07-15T10:00:00Z",
    )


# =============================================================================
# Fixture inventory presence
# =============================================================================

EXPECTED_FIXTURES = {
    "query_stmt_created.json",
    "query_stmt_queued.json",
    "query_stmt_compiled_not_queued.json",
    "query_stmt_running.json",
    "query_stmt_finished.json",
    "query_stmt_exception.json",
    "query_stmt_retrying.json",
    "query_stmt_retried.json",
    "query_stmt_archived.json",
    "query_stmt_plan_metadata_unavailable.json",
    "query_stmt_unknown_id_error.json",
    "query_stmt_future_state.json",
    "query_stmt_minimal.json",
    "queries_list.json",
    "backends.json",
    "malformed.txt",
    "not_object.json",
}


def test_fixture_inventory_matches_plan() -> None:
    actual = {p.name for p in FIXTURES_DIR.iterdir()}
    assert actual == EXPECTED_FIXTURES


# =============================================================================
# map_state — exact table from the research note
# =============================================================================


@pytest.mark.parametrize(
    ("raw_state", "last_event", "expected_phase"),
    [
        ("CREATED", None, "preparing"),
        ("CREATED", "Something", "preparing"),
        ("COMPILED", "Queued", "queued"),
        ("COMPILED", "Planning finished", "preparing"),
        ("COMPILED", None, "preparing"),
        ("RUNNING", None, "running"),
        ("RUNNING", "Rows available", "running"),
        ("FINISHED", None, "succeeded"),
        ("EXCEPTION", None, "failed"),
        ("EXCEPTION", "Cancelled", "failed"),
        ("RETRYING", None, "retrying"),
        ("RETRIED", None, "retrying"),
        ("SOME_FUTURE_STATE", None, "unknown"),
        (None, None, "unknown"),
    ],
)
def test_map_state_table(
    raw_state: str | None, last_event: str | None, expected_phase: str
) -> None:
    assert im.map_state(raw_state, last_event) == expected_phase


# =============================================================================
# parse_query_detail — table-driven over fixtures
# =============================================================================


@pytest.mark.parametrize(
    ("fixture_name", "expected_phase", "expect_availability_error"),
    [
        ("query_stmt_created.json", "preparing", False),
        ("query_stmt_queued.json", "queued", False),
        ("query_stmt_compiled_not_queued.json", "preparing", False),
        ("query_stmt_running.json", "running", False),
        ("query_stmt_finished.json", "succeeded", False),
        ("query_stmt_exception.json", "failed", False),
        ("query_stmt_retrying.json", "retrying", False),
        ("query_stmt_retried.json", "retrying", False),
        ("query_stmt_archived.json", "succeeded", False),
        ("query_stmt_plan_metadata_unavailable.json", "preparing", True),
        ("query_stmt_unknown_id_error.json", "unknown", True),
        ("query_stmt_future_state.json", "unknown", False),
        ("query_stmt_minimal.json", "preparing", False),
    ],
)
def test_parse_query_detail_over_fixtures(
    fixture_name: str, expected_phase: str, expect_availability_error: bool
) -> None:
    identity = make_identity()
    observation = im.parse_query_detail(fixture_bytes(fixture_name), identity)
    assert observation.phase == expected_phase
    if expect_availability_error:
        assert observation.availability_error is not None
    else:
        assert observation.availability_error is None
    assert observation.detail_url.startswith(BASE_URL)
    assert observation.observed_at


def test_parse_query_detail_exception_keeps_one_line_status() -> None:
    identity = make_identity()
    observation = im.parse_query_detail(fixture_bytes("query_stmt_exception.json"), identity)
    assert observation.status_summary is not None
    assert "\n" not in observation.status_summary


def test_parse_query_detail_running_progress_and_bytes() -> None:
    identity = make_identity()
    observation = im.parse_query_detail(fixture_bytes("query_stmt_running.json"), identity)
    assert observation.scan_progress is not None
    assert observation.scan_progress.display == "42 / 100 (42%)"
    assert observation.scan_progress.completed == 42
    assert observation.scan_progress.total == 100
    assert observation.bytes_read == "1.20 MB"


def test_parse_query_detail_queued_has_queued_duration_no_query_progress() -> None:
    identity = make_identity()
    observation = im.parse_query_detail(fixture_bytes("query_stmt_queued.json"), identity)
    assert observation.queued_duration == "3s412ms"
    # Deployed shape: query_progress is absent, unlike upstream master.
    assert observation.query_progress is None


def test_parse_query_detail_archived_still_parses_not_inflight() -> None:
    identity = make_identity()
    observation = im.parse_query_detail(fixture_bytes("query_stmt_archived.json"), identity)
    assert observation.phase == "succeeded"
    assert observation.availability_error is None


def test_parse_query_detail_unknown_id_preserves_no_prior_phase() -> None:
    identity = make_identity()
    observation = im.parse_query_detail(fixture_bytes("query_stmt_unknown_id_error.json"), identity)
    assert observation.availability_error is not None
    assert observation.raw_state is None


def test_parse_query_detail_plan_metadata_unavailable() -> None:
    identity = make_identity()
    observation = im.parse_query_detail(
        fixture_bytes("query_stmt_plan_metadata_unavailable.json"), identity
    )
    assert observation.phase == "preparing"
    assert observation.availability_error == "plan metadata unavailable"


def test_parse_query_detail_future_state_preserves_raw_state() -> None:
    identity = make_identity()
    observation = im.parse_query_detail(fixture_bytes("query_stmt_future_state.json"), identity)
    assert observation.phase == "unknown"
    assert observation.raw_state == "PAUSED_FOR_FUTURE_FEATURE"


def test_parse_query_detail_minimal_only_required_fields() -> None:
    identity = make_identity()
    observation = im.parse_query_detail(fixture_bytes("query_stmt_minimal.json"), identity)
    assert observation.phase == "preparing"
    assert observation.pool is None
    assert observation.scan_progress is None
    assert observation.bytes_read is None


# =============================================================================
# Malformed / oversized payload handling — never raises
# =============================================================================


def test_parse_query_detail_malformed_text_yields_availability_error() -> None:
    identity = make_identity()
    observation = im.parse_query_detail(fixture_bytes("malformed.txt"), identity)
    assert observation.phase == "unknown"
    assert observation.availability_error is not None


def test_parse_query_detail_not_object_yields_availability_error() -> None:
    identity = make_identity()
    observation = im.parse_query_detail(fixture_bytes("not_object.json"), identity)
    assert observation.phase == "unknown"
    assert observation.availability_error is not None


def test_parse_query_detail_oversized_payload_rejected_before_json_parse() -> None:
    identity = make_identity()
    # Larger than MAX_BODY_BYTES; must not attempt json.loads on this.
    oversized = b'{"query_id": "x", "filler": "' + b"a" * (im.MAX_BODY_BYTES + 1) + b'"}'
    observation = im.parse_query_detail(oversized, identity)
    assert observation.phase == "unknown"
    assert observation.availability_error is not None


def test_parse_query_detail_never_raises_on_garbage() -> None:
    identity = make_identity()
    for garbage in (b"", b"\x00\x01\x02", b"null", b"42", b'"just a string"', b"{"):
        observation = im.parse_query_detail(garbage, identity)
        assert observation.availability_error is not None


def test_parse_query_detail_never_raises_on_deeply_nested_json() -> None:
    # CPython's json.loads raises RecursionError (not JSONDecodeError) on
    # sufficiently deep nesting, well under MAX_BODY_BYTES and well under the
    # ~80 KB the plan cites for real detail pages. Must still be mapped to an
    # availability_error, never escape as an exception.
    depth = 3000
    deeply_nested = b'{"a": ' * depth + b"1" + b"}" * depth
    identity = make_identity()
    observation = im.parse_query_detail(deeply_nested, identity)
    assert observation.phase == "unknown"
    assert observation.availability_error is not None


def test_parse_query_detail_never_downgrades_to_http() -> None:
    # Regression for the TLS-downgrade finding: an identity carrying an
    # http:// coordinator_base_url (e.g. from a spoofed/misconfigured
    # discovery source) must not silently produce a plaintext http://
    # detail_url. parse_query_detail has no config/dev-flag to consult, so
    # it must reject rather than inherit a self-authorizing decision.
    identity = im.QueryIdentity(
        coordinator_base_url="http://coordinator-1.internal.example:25443",
        query_id="1a2b3c4d5e6f7081:9192a3b4c5d6e7f8",
        shell_execution_id="shell-exec-1",
        relation="initial",
        discovered_at="2026-07-15T10:00:00Z",
    )
    observation = im.parse_query_detail(fixture_bytes("query_stmt_running.json"), identity)
    assert observation.availability_error is not None
    assert not observation.detail_url.startswith("http://")
    assert "http://" not in observation.detail_url


# =============================================================================
# Field-deletion robustness — every optional field removed still parses
# =============================================================================

OPTIONAL_RECORD_FIELDS = [
    "last_event",
    "resource_pool",
    "queued_duration",
    "progress",
    "query_progress",
    "bytes_read",
    "bytes_sent",
    "rows_fetched",
    "stmt",
    "stmt_type",
    "effective_user",
    "default_db",
]


@pytest.mark.parametrize("field", OPTIONAL_RECORD_FIELDS)
def test_field_deletion_from_running_fixture_still_parses(field: str) -> None:
    payload = load_fixture("query_stmt_running.json")
    payload["record_json"].pop(field, None)
    identity = make_identity()
    observation = im.parse_query_detail(json.dumps(payload).encode("utf-8"), identity)
    assert observation.phase == "running"
    assert observation.availability_error is None


def test_field_deletion_all_optional_fields_at_once_still_parses() -> None:
    payload = load_fixture("query_stmt_running.json")
    for field in OPTIONAL_RECORD_FIELDS:
        payload["record_json"].pop(field, None)
    identity = make_identity()
    observation = im.parse_query_detail(json.dumps(payload).encode("utf-8"), identity)
    assert observation.phase == "running"
    assert observation.availability_error is None


# =============================================================================
# parse_progress
# =============================================================================


@pytest.mark.parametrize(
    ("display", "completed", "total"),
    [
        ("42 / 100 (42%)", 42, 100),
        ("0 / 0 (0%)", 0, 0),
        ("N/A", None, None),
        ("", None, None),
        ("garbage", None, None),
        ("5/10", 5, 10),
    ],
)
def test_parse_progress(display: str, completed: int | None, total: int | None) -> None:
    counter = im.parse_progress(display)
    assert counter.display == display
    assert counter.completed == completed
    assert counter.total == total


# =============================================================================
# validate_coordinator_url
# =============================================================================


def test_validate_coordinator_url_accepts_https() -> None:
    assert (
        im.validate_coordinator_url("https://coordinator-1.internal.example:25443")
        == "https://coordinator-1.internal.example:25443"
    )


def test_validate_coordinator_url_rejects_http_without_dev_flag() -> None:
    with pytest.raises(im.IdentityValidationError):
        im.validate_coordinator_url("http://coordinator-1.internal.example:25443")


def test_validate_coordinator_url_accepts_http_with_dev_flag() -> None:
    assert (
        im.validate_coordinator_url("http://coordinator-1.internal.example:25443", allow_http=True)
        == "http://coordinator-1.internal.example:25443"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@coordinator-1.internal.example:25443",
        "https://coordinator-1.internal.example:25443/cancel_query",
        "https://coordinator-1.internal.example:25443/query_stmt?query_id=x",
        "file:///etc/passwd",
        "ftp://coordinator-1.internal.example:25443",
        "https://coordinator-1.internal.example:25443#frag",
        "not a url at all",
        "",
        "https://",
        "https://coordinator-1.internal.example:99999",
        "https://coordinator-1.internal.example:-1",
    ],
)
def test_validate_coordinator_url_rejects_bad_urls(url: str) -> None:
    with pytest.raises(im.IdentityValidationError):
        im.validate_coordinator_url(url)


def test_validate_coordinator_url_rejects_redirect_bait_in_path() -> None:
    with pytest.raises(im.IdentityValidationError):
        im.validate_coordinator_url("https://coordinator-1.internal.example:25443/@evil.example/")


def test_validate_coordinator_url_allows_trailing_slash_only() -> None:
    assert (
        im.validate_coordinator_url("https://coordinator-1.internal.example:25443/")
        == "https://coordinator-1.internal.example:25443"
    )


def test_validate_coordinator_url_rejects_ipv6_literal() -> None:
    # Coordinators are documented as DNS hostnames only. An IPv6 literal
    # must be rejected outright rather than accepted-and-mangled: urlsplit()
    # strips the required "[...]" brackets from .hostname, so naively
    # re-assembling "scheme://host:port" would silently produce a URL that
    # cannot round-trip through an HTTP client (RFC 3986 requires brackets
    # around an IPv6 literal in a URL authority).
    with pytest.raises(im.IdentityValidationError):
        im.validate_coordinator_url("https://[::1]:25443")


# =============================================================================
# validate_query_id
# =============================================================================


@pytest.mark.parametrize(
    "qid",
    [
        "1a2b3c4d5e6f7081:9192a3b4c5d6e7f8",
        "0000000000000000:0000000000000000",
        "ffffffffffffffff:ffffffffffffffff",
    ],
)
def test_validate_query_id_accepts_valid(qid: str) -> None:
    im.validate_query_id(qid)  # must not raise


@pytest.mark.parametrize(
    "qid",
    [
        "",
        "1a2b3c4d5e6f7081",
        "1A2B3C4D5E6F7081:9192A3B4C5D6E7F8",  # uppercase hex rejected
        "1a2b3c4d5e6f708:9192a3b4c5d6e7f8",  # too short
        "1a2b3c4d5e6f70811:9192a3b4c5d6e7f8",  # too long
        "1a2b3c4d5e6f7081-9192a3b4c5d6e7f8",  # wrong separator
        "1a2b3c4d5e6f7081:9192a3b4c5d6e7f8;DROP TABLE",
        "../../../etc/passwd",
        "1a2b3c4d5e6f7081:9192a3b4c5d6e7f8\n",
        " 1a2b3c4d5e6f7081:9192a3b4c5d6e7f8",
    ],
)
def test_validate_query_id_rejects_invalid(qid: str) -> None:
    with pytest.raises(im.IdentityValidationError):
        im.validate_query_id(qid)


# =============================================================================
# build_detail_url
# =============================================================================


@pytest.mark.parametrize(
    "endpoint", ["query_stmt", "query_summary", "query_plan_text", "query_plan"]
)
def test_build_detail_url_allowed_endpoints(endpoint: str) -> None:
    url = im.build_detail_url(BASE_URL, endpoint, "1a2b3c4d5e6f7081:9192a3b4c5d6e7f8")
    assert url.startswith(BASE_URL)
    assert f"/{endpoint}" in url
    assert "query_id=1a2b3c4d5e6f7081%3A9192a3b4c5d6e7f8" in url or "query_id=" in url
    assert "json" in url


def test_build_detail_url_rejects_cancel_query() -> None:
    with pytest.raises(ValueError):
        im.build_detail_url(BASE_URL, "cancel_query", "1a2b3c4d5e6f7081:9192a3b4c5d6e7f8")


def test_build_detail_url_rejects_arbitrary_endpoint() -> None:
    with pytest.raises(ValueError):
        im.build_detail_url(BASE_URL, "../admin", "1a2b3c4d5e6f7081:9192a3b4c5d6e7f8")


def test_build_detail_url_rejects_bad_query_id() -> None:
    with pytest.raises(im.IdentityValidationError):
        im.build_detail_url(BASE_URL, "query_stmt", "not-a-valid-id")


def test_build_detail_url_rejects_http_base_without_dev_flag() -> None:
    # This is the TLS-downgrade regression: build_detail_url must not infer
    # the http-allow decision from the base URL string itself. An http://
    # base is only usable when the caller explicitly opts in via allow_http.
    with pytest.raises(im.IdentityValidationError):
        im.build_detail_url(
            "http://coordinator-1.internal.example:25443",
            "query_stmt",
            "1a2b3c4d5e6f7081:9192a3b4c5d6e7f8",
        )


def test_build_detail_url_accepts_http_base_with_explicit_dev_flag() -> None:
    url = im.build_detail_url(
        "http://coordinator-1.internal.example:25443",
        "query_stmt",
        "1a2b3c4d5e6f7081:9192a3b4c5d6e7f8",
        allow_http=True,
    )
    assert url.startswith("http://coordinator-1.internal.example:25443/query_stmt")


def test_build_detail_url_defaults_to_no_http_allowance() -> None:
    # allow_http must default to False, not to "whatever scheme base uses".
    with pytest.raises(im.IdentityValidationError):
        im.build_detail_url(
            "http://coordinator-1.internal.example:25443",
            "query_stmt",
            "1a2b3c4d5e6f7081:9192a3b4c5d6e7f8",
            allow_http=False,
        )


def test_no_function_can_produce_cancel_query_url() -> None:
    # Enumerate the only endpoint enum members and confirm none is cancel_query,
    # and that passing it explicitly is rejected — this is the closest a pure
    # module can get to proving "no such capability exists".
    for endpoint in ("query_stmt", "query_summary", "query_plan_text", "query_plan"):
        assert "cancel" not in endpoint
    with pytest.raises(ValueError):
        im.build_detail_url(BASE_URL, "cancel_query", "1a2b3c4d5e6f7081:9192a3b4c5d6e7f8")


# =============================================================================
# Sensitive-content tripwire
# =============================================================================

REAL_HOSTNAME_PATTERN_HINTS = ("mastercard", ".prod.", ".corp.", "dw.prod")


def test_fixtures_contain_no_sensitive_content() -> None:
    for path in FIXTURES_DIR.iterdir():
        text = path.read_text(encoding="utf-8", errors="strict")
        lowered = text.lower()
        assert "mastercard" not in lowered, f"{path.name} contains 'mastercard'"
        for hint in REAL_HOSTNAME_PATTERN_HINTS:
            assert hint not in lowered, f"{path.name} contains suspicious hostname hint {hint!r}"
        # No statement (or any single line) may exceed 260 chars — the plan's
        # tripwire threshold, chosen above the 253-char truncated synthetic
        # discovery statement but below anything resembling a captured body.
        for line in text.splitlines():
            assert len(line) <= 2000, f"{path.name} has a suspiciously long line"
        if path.suffix == ".json" and path.name != "malformed.txt":
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            _assert_no_long_statements(data, path.name)


def _assert_no_long_statements(data: Any, fixture_name: str) -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "stmt" and isinstance(value, str):
                assert len(value) <= 260, (
                    f"{fixture_name} has a stmt longer than 260 chars ({len(value)})"
                )
            _assert_no_long_statements(value, fixture_name)
    elif isinstance(data, list):
        for item in data:
            _assert_no_long_statements(item, fixture_name)


def test_synthetic_retry_fixtures_are_flagged() -> None:
    for name in ("query_stmt_retrying.json", "query_stmt_retried.json"):
        data = load_fixture(name)
        assert data.get("_synthetic") is True


# =============================================================================
# Module hygiene — no HTTP, no Textual
# =============================================================================


def test_importing_impala_monitor_pulls_in_no_http_or_textual_modules() -> None:
    # Check the import in a fresh subprocess interpreter. Mutating this
    # process's sys.modules instead (deleting textual/urllib entries) leaves
    # two generations of those modules alive and breaks every Textual test
    # that later runs in the same pytest-xdist worker.
    probe = (
        "import sys\n"
        "import dispatch.impala_monitor\n"
        "forbidden = ('http.client', 'urllib.request', 'textual')\n"
        "bad = [m for m in sys.modules\n"
        "       if any(m == f or m.startswith(f + '.') for f in forbidden)]\n"
        "if bad:\n"
        "    print('pulled in: ' + ', '.join(sorted(bad)))\n"
        "    sys.exit(1)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, (
        f"importing dispatch.impala_monitor in a fresh interpreter loaded "
        f"forbidden modules: {result.stdout}{result.stderr}"
    )


def test_module_source_has_no_http_or_textual_imports() -> None:
    source = Path(im.__file__).read_text(encoding="utf-8")
    for token in ("import urllib", "import http.client", "import textual", "from textual"):
        assert token not in source, f"found forbidden import token {token!r}"


# =============================================================================
# Dataclasses are frozen
# =============================================================================


def test_dataclasses_are_frozen() -> None:
    identity = make_identity()
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError subclasses Exception
        identity.query_id = "changed"  # type: ignore[misc]

    observation = im.parse_query_detail(fixture_bytes("query_stmt_running.json"), identity)
    with pytest.raises(Exception):  # noqa: B017
        observation.phase = "unknown"  # type: ignore[misc]

    counter = im.parse_progress("1 / 2")
    with pytest.raises(Exception):  # noqa: B017
        counter.completed = 5  # type: ignore[misc]


def test_deep_copy_of_fixture_not_mutated_by_parse() -> None:
    # Guard against parse_query_detail mutating shared/cached payload structures.
    payload = load_fixture("query_stmt_running.json")
    original = copy.deepcopy(payload)
    identity = make_identity()
    im.parse_query_detail(json.dumps(payload).encode("utf-8"), identity)
    assert payload == original

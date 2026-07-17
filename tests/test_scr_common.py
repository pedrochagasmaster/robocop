"""Focused argv-boundary tests for the stdlib-only production orchestrators."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCR_DIR = Path(__file__).resolve().parents[1] / "scr"
if str(SCR_DIR) not in sys.path:
    sys.path.insert(0, str(SCR_DIR))

import _common  # noqa: E402
import download_to_csv  # noqa: E402
import monthly_query_processor  # noqa: E402


@pytest.fixture(autouse=True)
def _monitor_lineage_env(monkeypatch):
    monkeypatch.setenv("DISPATCH_ORCHESTRATOR_CALL_ID", "call-0001")
    monkeypatch.setenv("DISPATCH_ORCHESTRATOR_CALL_INDEX", "1")
    monkeypatch.setenv("DISPATCH_ORCHESTRATOR_SCRIPT", "test_orchestrator.py")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("dispatch_smoke_1", True),
        ("", False),
        ("bad-name", False),
        ("t;drop", False),
        ("schema.table", False),
    ],
)
def test_validate_identifier_accepts_only_plain_names(value: str, expected: bool) -> None:
    assert _common.validate_identifier(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("aa_enc.dispatch_smoke_1", True),
        ("", False),
        ("schema.table.extra", False),
        ("schema.bad-name", False),
        ("schema.t;drop", False),
    ],
)
def test_validate_full_table_requires_exact_schema_and_table(value: str, expected: bool) -> None:
    assert _common.validate_full_table(value) is expected


def test_download_table_mode_rejects_unsafe_full_table_before_retry(monkeypatch, capsys) -> None:
    retry_calls: list[str] = []
    monkeypatch.setattr(
        download_to_csv,
        "retry_loop",
        lambda query, _output, _queues: retry_calls.append(query),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "download_to_csv.py",
            "--table-name",
            "schema.table.extra",
            "--output-file",
            "output.csv",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        download_to_csv.main()

    assert exc_info.value.code == 2
    assert "plain Impala identifiers" in capsys.readouterr().err
    assert retry_calls == []


def test_download_csv_main_passes_optional_email_args_to_retry(monkeypatch, tmp_path) -> None:
    query_file = tmp_path / "query.sql"
    query_file.write_text("select 1", encoding="utf-8")
    retry_calls: list[tuple[str, str, list[str], str, str]] = []
    monkeypatch.setattr(
        download_to_csv,
        "retry_loop",
        lambda query, output, queues, *, to_email="", subject="": retry_calls.append(
            (query, output, queues, to_email, subject)
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "download_to_csv.py",
            "--query-file",
            str(query_file),
            "--output-file",
            "output.csv",
            "--to-email",
            "test@example.com",
            "--subject",
            "Dispatch Export",
        ],
    )

    download_to_csv.main()

    assert retry_calls == [
        (
            "select 1",
            "output.csv",
            ["adhoc_fast", "adhoc_small", "adhoc"],
            "test@example.com",
            "Dispatch Export",
        )
    ]


@pytest.mark.parametrize(
    ("flag", "unsafe_value"),
    [
        ("--schema", "bad-schema"),
        ("--table-name", "bad/name"),
        ("--user", "user;drop"),
    ],
)
def test_monthly_argv_rejects_unsafe_identifiers_before_processing(
    monkeypatch, capsys, flag: str, unsafe_value: str
) -> None:
    process_calls: list[object] = []
    argv = [
        "monthly_query_processor.py",
        "--sql-file",
        "monthly.sql",
        "--schema",
        "aa_enc",
        "--table-name",
        "dispatch_smoke_1",
        "--start-date",
        "01/01/2026",
        "--end-date",
        "02/01/2026",
        "--user",
        "e123456",
        "--to-email",
        "test@example.com",
        "--subject",
        "Smoke",
    ]
    argv[argv.index(flag) + 1] = unsafe_value
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(
        monthly_query_processor,
        "process_monthly_job",
        lambda args: process_calls.append(args),
    )

    with pytest.raises(SystemExit) as exc_info:
        monthly_query_processor.main()

    assert exc_info.value.code == 2
    assert "plain Impala identifier" in capsys.readouterr().err
    assert process_calls == []


@pytest.mark.parametrize(
    ("stderr_text", "category"),
    [
        ("Memory limit exceeded: Not enough memory available", "MEMORY_EXCEEDED"),
        ("Query timed out while fetching results", "TIMEOUT"),
        ("Admission rejected: queue is full", "QUEUE_FULL"),
        ("Could not connect to host: connection refused", "CONNECTION_ERROR"),
        ("RPC dropped due to backpressure", "BACKPRESSURE"),
        ("Name or service not known for edge-node", "HOST_RESOLUTION_ERROR"),
        ("Coordinator unreachable", "HOST_UNREACHABLE"),
        ("Disk full while writing parquet data", "DISK_FULL"),
        ("Memory available below required reservation", "MEMORY_AVAILABLE"),
        ("Scratch space limit exceeded", "SPACE_LIMIT"),
        ("AnalysisException: duplicate column name id", "DUPLICATE_COLUMN"),
        ("AuthenticationException: unable to obtain Kerberos principal", "AUTH_ERROR"),
        ("AnalysisException: could not resolve path to table: missing_table", "TABLE_NOT_FOUND"),
        ("ParseException: syntax error at line 1", "SYNTAX_ERROR"),
    ],
)
def test_impala_error_classifier_categories(stderr_text: str, category: str) -> None:
    assert _common.classificar_erro_impala(stderr_text)["categoria"] == category


def test_unmatched_stderr_maps_to_generic_error() -> None:
    assert (
        _common.classificar_erro_impala("unexpected impala stderr")["categoria"] == "GENERIC_ERROR"
    )


@pytest.mark.parametrize(
    ("stderr_text", "category"),
    [
        ("AnalysisException: could not resolve path to table: missing_table", "TABLE_NOT_FOUND"),
        ("ParseException: syntax error at line 1", "SYNTAX_ERROR"),
        ("AnalysisException: duplicate column name id", "DUPLICATE_COLUMN"),
        ("AuthenticationException: unable to obtain Kerberos principal", "AUTH_ERROR"),
        ("unexpected impala stderr", "GENERIC_ERROR"),
    ],
)
def test_fatal_error_categories_are_in_fatal_set(stderr_text: str, category: str) -> None:
    result = _common.classificar_erro_impala(stderr_text)
    assert result["categoria"] == category
    assert result["categoria"] in _common.FATAL_ERRORS


@pytest.mark.parametrize(
    "category",
    ["TABLE_NOT_FOUND", "SYNTAX_ERROR", "DUPLICATE_COLUMN", "AUTH_ERROR", "GENERIC_ERROR"],
)
def test_each_declared_fatal_error_is_pinned(category: str) -> None:
    assert category in _common.FATAL_ERRORS


DEFAULT_POOLS = ["adhoc_fast", "acs_small", "adhoc_small", "acs_large", "adhoc"]


def test_resolve_pools_defaults_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv("DISPATCH_REQUEST_POOL", raising=False)
    assert _common.resolve_pools(DEFAULT_POOLS) == DEFAULT_POOLS


def test_resolve_pools_defaults_when_env_blank(monkeypatch) -> None:
    monkeypatch.setenv("DISPATCH_REQUEST_POOL", "   ")
    assert _common.resolve_pools(DEFAULT_POOLS) == DEFAULT_POOLS


def test_resolve_pools_pins_single_selected_queue(monkeypatch) -> None:
    monkeypatch.setenv("DISPATCH_REQUEST_POOL", "acs_large")
    assert _common.resolve_pools(DEFAULT_POOLS) == ["acs_large"]


def test_resolve_pools_parses_comma_separated_list(monkeypatch) -> None:
    monkeypatch.setenv("DISPATCH_REQUEST_POOL", " adhoc_fast , adhoc ")
    assert _common.resolve_pools(DEFAULT_POOLS) == ["adhoc_fast", "adhoc"]


def test_resolve_pools_does_not_mutate_default(monkeypatch) -> None:
    monkeypatch.delenv("DISPATCH_REQUEST_POOL", raising=False)
    default = ["adhoc_fast", "adhoc"]
    result = _common.resolve_pools(default)
    result.append("mutated")
    assert default == ["adhoc_fast", "adhoc"]


def test_cycle_through_pools_propagates_unexpected_operation_errors(monkeypatch) -> None:
    failures: list[int] = []
    sleeps: list[int] = []

    monkeypatch.setattr(_common.time, "sleep", lambda seconds: sleeps.append(seconds))

    def operation(_pool: str) -> bool:
        raise RuntimeError("local subprocess startup failed")

    with pytest.raises(RuntimeError, match="local subprocess startup failed"):
        _common.cycle_through_pools(
            ["adhoc_fast"],
            operation,
            failures.append,
            retry_interval=30,
            max_cycles=1,
        )

    assert failures == []
    assert sleeps == []


def test_cycle_through_pools_raises_timeout_without_promising_unavailable_retry(
    monkeypatch,
) -> None:
    failures: list[int] = []
    sleeps: list[int] = []

    monkeypatch.setattr(_common.time, "sleep", lambda seconds: sleeps.append(seconds))

    with pytest.raises(TimeoutError, match="Retry cycle limit reached"):
        _common.cycle_through_pools(
            ["adhoc_fast"],
            lambda _pool: False,
            failures.append,
            retry_interval=30,
            max_cycles=1,
        )

    assert failures == []
    assert sleeps == []


def test_cycle_through_pools_keeps_retry_interval_between_retryable_cycles(
    monkeypatch,
) -> None:
    attempts: list[str] = []
    failures: list[int] = []
    sleeps: list[int] = []

    monkeypatch.setattr(_common.time, "sleep", lambda seconds: sleeps.append(seconds))

    def operation(pool: str) -> bool:
        attempts.append(pool)
        return False

    with pytest.raises(TimeoutError, match="Retry cycle limit reached"):
        _common.cycle_through_pools(
            ["adhoc_fast"],
            operation,
            failures.append,
            retry_interval=30,
            max_cycles=2,
        )

    assert attempts == ["adhoc_fast", "adhoc_fast"]
    assert failures == [1]
    assert sleeps == [30]


def test_download_csv_retry_loop_sends_start_notification(monkeypatch) -> None:
    sent_emails: list[tuple[str, str, str]] = []
    attempts: list[str] = []

    def fake_run_export(
        query: str, output_file: str, *, to_email: str, subject: str, queue: str
    ) -> bool:
        attempts.append(queue)
        return queue == "adhoc_small"

    monkeypatch.setattr(
        download_to_csv,
        "send_email",
        lambda body, subject, to_email: sent_emails.append((subject, body, to_email)),
    )
    monkeypatch.setattr(download_to_csv, "run_export_on_impala", fake_run_export)

    download_to_csv.retry_loop(
        "select 1",
        "output.csv",
        ["adhoc_fast", "adhoc_small"],
        to_email="test@example.com",
        subject="Dispatch Export",
    )

    assert attempts == ["adhoc_fast", "adhoc_small"]
    assert [item[0] for item in sent_emails] == [
        "Dispatch Export - PROCESSO INICIADO",
    ]
    assert all(item[2] == "test@example.com" for item in sent_emails)


def test_download_csv_run_export_sends_success_notification(monkeypatch, tmp_path) -> None:
    output_file = tmp_path / "export.csv"
    sent_emails: list[tuple[str, str, str]] = []

    def fake_run_impala_shell(command: list[str], *, pool: str = "") -> tuple[int, bytes, bytes]:
        target = Path(command[command.index("-o") + 1])
        target.write_text("csv\n", encoding="utf-8")
        return 0, b"ok", b""

    monkeypatch.setattr(download_to_csv, "run_impala_shell", fake_run_impala_shell)
    monkeypatch.setattr(
        download_to_csv,
        "send_email",
        lambda body, subject, to_email: sent_emails.append((subject, body, to_email)),
    )

    assert download_to_csv.run_export_on_impala(
        "select 1",
        str(output_file),
        to_email="test@example.com",
        subject="Dispatch Export",
        queue="adhoc_fast",
    )

    assert sent_emails[0][0] == "Dispatch Export - PROCESSO FINALIZADO"
    assert "Status: SUCCESS" in sent_emails[0][1]
    assert sent_emails[0][2] == "test@example.com"


def test_download_csv_run_export_sends_fatal_error_notification(monkeypatch, tmp_path) -> None:
    output_file = tmp_path / "export.csv"
    sent_emails: list[tuple[str, str, str]] = []

    def fake_run_impala_shell(command: list[str], *, pool: str = "") -> tuple[int, bytes, bytes]:
        return 1, b"", b"ParseException: syntax error at line 1"

    monkeypatch.setattr(download_to_csv, "run_impala_shell", fake_run_impala_shell)
    monkeypatch.setattr(
        download_to_csv,
        "send_email",
        lambda body, subject, to_email: sent_emails.append((subject, body, to_email)),
    )

    with pytest.raises(SystemExit) as exc_info:
        download_to_csv.run_export_on_impala(
            "select",
            str(output_file),
            to_email="test@example.com",
            subject="Dispatch Export",
            queue="adhoc_fast",
        )

    assert exc_info.value.code == 1
    assert sent_emails[0][0] == "Dispatch Export - ERRO (SYNTAX_ERROR)"
    assert "FATAL ERROR" in sent_emails[0][1]
    assert sent_emails[0][2] == "test@example.com"


def test_send_email_uses_finite_timeout_and_closes_connection(monkeypatch) -> None:
    smtp_calls: list[tuple[str, int, float]] = []
    sent: list[tuple[str, list[str], str]] = []
    closed: list[str] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            smtp_calls.append((host, port, timeout))

        def sendmail(self, from_email: str, recipients: list[str], message: str) -> None:
            sent.append((from_email, recipients, message))

        def quit(self) -> None:
            closed.append("quit")

    monkeypatch.setenv("MAILHOST", "smtp.example.test:2525")
    monkeypatch.setattr(_common.smtplib, "SMTP", FakeSMTP)

    _common.send_email("body", "Subject", "a@example.com;b@example.com")

    assert smtp_calls == [("smtp.example.test", 2525, _common.SMTP_TIMEOUT_SECONDS)]
    assert sent[0][0] == "AutoQueryExecution_Analytics@mastercard.com"
    assert sent[0][1] == ["a@example.com", "b@example.com"]
    assert "Subject" in sent[0][2]
    assert closed == ["quit"]


def test_send_email_closes_connection_when_sendmail_fails(monkeypatch) -> None:
    closed: list[str] = []

    class FakeSMTP:
        def __init__(self, _host: str, _port: int, timeout: float) -> None:
            pass

        def sendmail(self, *_args: Any) -> None:
            raise OSError("relay unavailable")

        def quit(self) -> None:
            closed.append("quit")

    monkeypatch.setattr(_common.smtplib, "SMTP", FakeSMTP)

    _common.send_email("body", "Subject", "a@example.com")

    assert closed == ["quit"]


# =============================================================================
# run_impala_shell: execution identity event protocol
# =============================================================================

# A child process that writes several MB to stdout *before* writing anything
# to stderr, then writes the monitor line to stderr, then exits. If stdout
# and stderr were drained sequentially (stderr first) rather than
# concurrently, this child would block forever writing to a full stdout
# pipe while the parent waits on stderr — this reproduces the exact deadlock
# ``run_impala_shell`` must avoid.
_BIG_STDOUT_CHILD = """
import sys
sys.stdout.write("x" * (6 * 1024 * 1024))
sys.stdout.flush()
sys.stderr.write("Query state can be monitored at: https://coordinator-1.internal.example:25443/query_plan?query_id=1a2b3c4d5e6f7081:9192a3b4c5d6e7f8\\n")
sys.stderr.flush()
sys.exit(0)
"""


def _read_events(events_path: Path) -> list[dict]:
    if not events_path.exists():
        return []
    lines = events_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class TestRunImpalaShellConcurrentDrain:
    def test_multi_mb_stdout_with_stderr_monitor_line_does_not_deadlock(
        self, tmp_path, monkeypatch
    ) -> None:
        """Concurrent draining must not deadlock on a large stdout child."""
        script = tmp_path / "big_stdout_child.py"
        script.write_text(_BIG_STDOUT_CHILD, encoding="utf-8")
        events_path = tmp_path / "monitor.events.jsonl"
        monkeypatch.setenv("DISPATCH_MONITOR_EVENTS_PATH", str(events_path))
        monkeypatch.setenv("DISPATCH_JOB_ID", "job-deadlock")

        returncode, stdout, stderr = _common.run_impala_shell(
            [sys.executable, str(script)], pool="adhoc_fast"
        )

        assert returncode == 0
        assert len(stdout) == 6 * 1024 * 1024
        assert b"Query state can be monitored at:" in stderr

        events = _read_events(events_path)
        types = [event["type"] for event in events]
        assert types == ["shell_started", "query_discovered", "shell_finished"]


class TestRunImpalaShellMonitorLineExtraction:
    def test_monitor_line_emits_query_discovered_event(self, tmp_path, monkeypatch) -> None:
        script = tmp_path / "monitor_child.py"
        script.write_text(
            "import sys\n"
            "sys.stderr.write("
            "'Query state can be monitored at: "
            "https://coordinator-1.internal.example:25443/query_plan?"
            "query_id=1a2b3c4d5e6f7081:9192a3b4c5d6e7f8\\n')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        events_path = tmp_path / "monitor.events.jsonl"
        monkeypatch.setenv("DISPATCH_MONITOR_EVENTS_PATH", str(events_path))
        monkeypatch.setenv("DISPATCH_JOB_ID", "job-1")

        returncode, _stdout, stderr = _common.run_impala_shell(
            [sys.executable, str(script)], pool="adhoc"
        )

        assert returncode == 0
        assert b"Query state can be monitored at:" in stderr

        events = _read_events(events_path)
        discovered = [e for e in events if e["type"] == "query_discovered"]
        assert len(discovered) == 1
        assert (
            discovered[0]["coordinator_base_url"] == "https://coordinator-1.internal.example:25443"
        )
        assert discovered[0]["query_id"] == "1a2b3c4d5e6f7081:9192a3b4c5d6e7f8"

    def test_retried_link_emits_query_retried_event(self, tmp_path, monkeypatch) -> None:
        script = tmp_path / "retried_child.py"
        script.write_text(
            "import sys\n"
            "sys.stderr.write("
            "'Retried query link: "
            "https://coordinator-1.internal.example:25443/query_plan?"
            "query_id=2b3c4d5e6f708192:a3b4c5d6e7f89101\\n')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        events_path = tmp_path / "monitor.events.jsonl"
        monkeypatch.setenv("DISPATCH_MONITOR_EVENTS_PATH", str(events_path))
        monkeypatch.setenv("DISPATCH_JOB_ID", "job-1")

        _common.run_impala_shell([sys.executable, str(script)], pool="adhoc")

        events = _read_events(events_path)
        retried = [e for e in events if e["type"] == "query_retried"]
        assert len(retried) == 1
        assert retried[0]["query_id"] == "2b3c4d5e6f708192:a3b4c5d6e7f89101"

    def test_unrelated_stderr_text_emits_no_discovery_event(self, tmp_path, monkeypatch) -> None:
        """Only the two anchored line shapes are interpreted; everything else passes through."""
        script = tmp_path / "plain_child.py"
        script.write_text(
            "import sys\n"
            "sys.stderr.write('AnalysisException: Syntax error in line 1\\n')\n"
            "sys.exit(1)\n",
            encoding="utf-8",
        )
        events_path = tmp_path / "monitor.events.jsonl"
        monkeypatch.setenv("DISPATCH_MONITOR_EVENTS_PATH", str(events_path))
        monkeypatch.setenv("DISPATCH_JOB_ID", "job-1")

        returncode, _stdout, stderr = _common.run_impala_shell(
            [sys.executable, str(script)], pool="adhoc"
        )

        assert returncode == 1
        assert b"AnalysisException" in stderr
        events = _read_events(events_path)
        assert [e["type"] for e in events] == ["shell_started", "shell_finished"]


class TestRunImpalaShellMalformedUrlRejection:
    @pytest.mark.parametrize(
        "stderr_line",
        [
            "error: Query state can be monitored at: https://host.example/query_plan?query_id=1a2b3c4d5e6f7081:9192a3b4c5d6e7f8",
            "Query state can be monitored at: https://host.example/query_plan?query_id=1a2b3c4d5e6f7081:9192a3b4c5d6e7f8 trailing",
            "Query state can be monitored at: https://host.example/cancel_query?query_id=1a2b3c4d5e6f7081:9192a3b4c5d6e7f8",
            "Query state can be monitored at: https://host.example/query_stmt?query_id=1a2b3c4d5e6f7081:9192a3b4c5d6e7f8",
            "Query state can be monitored at: https://host.example/query_plan?query_id=1a2b3c4d5e6f7081:9192a3b4c5d6e7f8&x=1",
            "Query state can be monitored at: https://host.example/query_plan?query_id=1a2b3c4d5e6f7081:9192a3b4c5d6e7f8&query_id=1a2b3c4d5e6f7081:9192a3b4c5d6e7f8",
            "Query state can be monitored at: https://user:secret@host.example/query_plan?query_id=1a2b3c4d5e6f7081:9192a3b4c5d6e7f8",
            "Query state can be monitored at: https://host.example/query_plan?query_id=1a2b3c4d5e6f7081:9192a3b4c5d6e7f8#fragment",
            "Query state can be monitored at: https://host.example:99999/query_plan?query_id=1a2b3c4d5e6f7081:9192a3b4c5d6e7f8",
            "Query state can be monitored at: https://host.example/query_plan?query_id=1A2b3c4d5e6f7081:9192a3b4c5d6e7f8",
        ],
    )
    def test_non_exact_monitor_lines_are_rejected_and_preserve_bytes(
        self, stderr_line: str, tmp_path, monkeypatch
    ) -> None:
        expected = (stderr_line + "\r\n").encode()
        script = tmp_path / "adversarial_child.py"
        script.write_text(
            "import sys\nsys.stderr.buffer.write(" + repr(expected) + ")\n",
            encoding="utf-8",
        )
        events_path = tmp_path / "monitor.events.jsonl"
        monkeypatch.setenv("DISPATCH_MONITOR_EVENTS_PATH", str(events_path))
        monkeypatch.setenv("DISPATCH_JOB_ID", "job-1")

        returncode, stdout, stderr = _common.run_impala_shell(
            [sys.executable, str(script)], pool="adhoc"
        )

        assert (returncode, stdout, stderr) == (0, b"", expected)
        assert [event["type"] for event in _read_events(events_path)] == [
            "shell_started",
            "shell_finished",
        ]

    @pytest.mark.parametrize("line_ending", [b"\n", b"\r\n"])
    def test_exact_monitor_line_accepts_lf_or_crlf_and_preserves_bytes(
        self, line_ending: bytes, tmp_path, monkeypatch
    ) -> None:
        expected = (
            b"Query state can be monitored at: "
            b"https://host.example:25443/query_plan?"
            b"query_id=1a2b3c4d5e6f7081:9192a3b4c5d6e7f8" + line_ending
        )
        script = tmp_path / "exact_child.py"
        script.write_text(
            "import sys\nsys.stderr.buffer.write(" + repr(expected) + ")\n",
            encoding="utf-8",
        )
        events_path = tmp_path / "monitor.events.jsonl"
        monkeypatch.setenv("DISPATCH_MONITOR_EVENTS_PATH", str(events_path))
        monkeypatch.setenv("DISPATCH_JOB_ID", "job-1")

        returncode, stdout, stderr = _common.run_impala_shell(
            [sys.executable, str(script)], pool="adhoc"
        )

        assert (returncode, stdout, stderr) == (0, b"", expected)
        assert [event["type"] for event in _read_events(events_path)] == [
            "shell_started",
            "query_discovered",
            "shell_finished",
        ]

    def test_malformed_url_is_rejected_without_affecting_execution(
        self, tmp_path, monkeypatch
    ) -> None:
        """A monitor line with an unparseable/invalid URL emits no event but
        does not alter the child's exit code or returned bytes."""
        script = tmp_path / "malformed_child.py"
        script.write_text(
            "import sys\n"
            "sys.stderr.write('Query state can be monitored at: not-a-valid-url\\n')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        events_path = tmp_path / "monitor.events.jsonl"
        monkeypatch.setenv("DISPATCH_MONITOR_EVENTS_PATH", str(events_path))
        monkeypatch.setenv("DISPATCH_JOB_ID", "job-1")

        returncode, _stdout, stderr = _common.run_impala_shell(
            [sys.executable, str(script)], pool="adhoc"
        )

        assert returncode == 0
        assert b"not-a-valid-url" in stderr
        events = _read_events(events_path)
        assert [e["type"] for e in events] == ["shell_started", "shell_finished"]

    def test_url_missing_query_id_is_rejected(self, tmp_path, monkeypatch) -> None:
        script = tmp_path / "no_qid_child.py"
        script.write_text(
            "import sys\n"
            "sys.stderr.write("
            "'Query state can be monitored at: "
            "https://coordinator-1.internal.example:25443/query_plan\\n')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        events_path = tmp_path / "monitor.events.jsonl"
        monkeypatch.setenv("DISPATCH_MONITOR_EVENTS_PATH", str(events_path))
        monkeypatch.setenv("DISPATCH_JOB_ID", "job-1")

        _common.run_impala_shell([sys.executable, str(script)], pool="adhoc")

        events = _read_events(events_path)
        assert [e["type"] for e in events] == ["shell_started", "shell_finished"]

    def test_url_with_bad_query_id_shape_is_rejected(self, tmp_path, monkeypatch) -> None:
        script = tmp_path / "bad_qid_child.py"
        script.write_text(
            "import sys\n"
            "sys.stderr.write("
            "'Query state can be monitored at: "
            "https://coordinator-1.internal.example:25443/query_plan?query_id=not-hex\\n')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        events_path = tmp_path / "monitor.events.jsonl"
        monkeypatch.setenv("DISPATCH_MONITOR_EVENTS_PATH", str(events_path))
        monkeypatch.setenv("DISPATCH_JOB_ID", "job-1")

        _common.run_impala_shell([sys.executable, str(script)], pool="adhoc")

        events = _read_events(events_path)
        assert [e["type"] for e in events] == ["shell_started", "shell_finished"]


class TestRunImpalaShellEventsPathUnset:
    def test_events_path_unset_degrades_to_plain_drain(self, tmp_path, monkeypatch) -> None:
        """No DISPATCH_MONITOR_EVENTS_PATH => no events, execution unaffected."""
        monkeypatch.delenv("DISPATCH_MONITOR_EVENTS_PATH", raising=False)
        monkeypatch.delenv("DISPATCH_JOB_ID", raising=False)
        script = tmp_path / "monitor_child.py"
        script.write_text(
            "import sys\n"
            "sys.stdout.write('hello\\n')\n"
            "sys.stderr.write("
            "'Query state can be monitored at: "
            "https://coordinator-1.internal.example:25443/query_plan?"
            "query_id=1a2b3c4d5e6f7081:9192a3b4c5d6e7f8\\n')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )

        returncode, stdout, stderr = _common.run_impala_shell(
            [sys.executable, str(script)], pool="adhoc"
        )

        assert returncode == 0
        assert stdout.replace(b"\r\n", b"\n") == b"hello\n"
        assert b"Query state can be monitored at:" in stderr
        # No sidecar file should exist anywhere the test can observe:
        # nothing in tmp_path was created by the writer.
        assert list(tmp_path.glob("*.jsonl")) == []


class TestRunImpalaShellUnwritableEventsPath:
    def test_unwritable_events_path_is_harmless(self, tmp_path, monkeypatch) -> None:
        """An events path in a non-existent directory must not affect exit
        code or returned bytes; the writer degrades silently."""
        bad_path = tmp_path / "does" / "not" / "exist" / "monitor.events.jsonl"
        monkeypatch.setenv("DISPATCH_MONITOR_EVENTS_PATH", str(bad_path))
        monkeypatch.setenv("DISPATCH_JOB_ID", "job-1")
        script = tmp_path / "monitor_child.py"
        script.write_text(
            "import sys\n"
            "sys.stdout.write('hello\\n')\n"
            "sys.stderr.write("
            "'Query state can be monitored at: "
            "https://coordinator-1.internal.example:25443/query_plan?"
            "query_id=1a2b3c4d5e6f7081:9192a3b4c5d6e7f8\\n')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )

        returncode, stdout, stderr = _common.run_impala_shell(
            [sys.executable, str(script)], pool="adhoc"
        )

        assert returncode == 0
        assert stdout.replace(b"\r\n", b"\n") == b"hello\n"
        assert b"Query state can be monitored at:" in stderr
        assert not bad_path.exists()
        assert not bad_path.parent.exists()


class TestRunImpalaShellEventShape:
    def test_events_carry_v2_call_lineage_and_utc_timestamp(self, tmp_path, monkeypatch) -> None:
        script = tmp_path / "monitor_child.py"
        script.write_text(
            "import sys\n"
            "sys.stderr.write("
            "'Query state can be monitored at: "
            "https://coordinator-1.internal.example:25443/query_plan?"
            "query_id=1a2b3c4d5e6f7081:9192a3b4c5d6e7f8\\n')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        events_path = tmp_path / "monitor.events.jsonl"
        monkeypatch.setenv("DISPATCH_MONITOR_EVENTS_PATH", str(events_path))
        monkeypatch.setenv("DISPATCH_JOB_ID", "job-shape-test")
        monkeypatch.setenv("DISPATCH_ORCHESTRATOR_CALL_ID", "call-0002")
        monkeypatch.setenv("DISPATCH_ORCHESTRATOR_CALL_INDEX", "2")
        monkeypatch.setenv("DISPATCH_ORCHESTRATOR_SCRIPT", "download_to_csv.py")
        monkeypatch.setattr(_common, "_SHELL_EXECUTION_COUNTER", 0)

        _common.run_impala_shell([sys.executable, str(script)], pool="acs_large")

        events = _read_events(events_path)
        assert len(events) == 3
        seqs = [event["seq"] for event in events]
        assert seqs == [1, 2, 3]
        for event in events:
            assert event["v"] == 2
            assert event["job_id"] == "job-shape-test"
            assert event["pool"] == "acs_large"
            assert event["orchestrator_call_id"] == "call-0002"
            assert event["orchestrator_call_index"] == 2
            assert event["orchestrator_script"] == "download_to_csv.py"
            assert event["shell_relation"] == "initial"
            assert "shell_execution_id" in event and event["shell_execution_id"]
            assert event["ts"].endswith("Z")
        assert events[0]["type"] == "shell_started"
        assert events[1]["type"] == "query_discovered"
        assert events[2]["type"] == "shell_finished"
        assert events[2]["returncode"] == 0
        # Never SQL, never error bodies.
        for event in events:
            assert "sql" not in event
            assert "stderr" not in event
            assert "detalhes" not in event

    def test_shell_execution_id_is_stable_across_events_in_one_run(
        self, tmp_path, monkeypatch
    ) -> None:
        script = tmp_path / "monitor_child.py"
        script.write_text(
            "import sys\n"
            "sys.stderr.write("
            "'Query state can be monitored at: "
            "https://coordinator-1.internal.example:25443/query_plan?"
            "query_id=1a2b3c4d5e6f7081:9192a3b4c5d6e7f8\\n')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        events_path = tmp_path / "monitor.events.jsonl"
        monkeypatch.setenv("DISPATCH_MONITOR_EVENTS_PATH", str(events_path))
        monkeypatch.setenv("DISPATCH_JOB_ID", "job-1")

        _common.run_impala_shell([sys.executable, str(script)], pool="adhoc")

        events = _read_events(events_path)
        ids = {event["shell_execution_id"] for event in events}
        assert len(ids) == 1

    def test_two_runs_get_distinct_shell_execution_ids(self, tmp_path, monkeypatch) -> None:
        script = tmp_path / "plain_child.py"
        script.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        events_path = tmp_path / "monitor.events.jsonl"
        monkeypatch.setenv("DISPATCH_MONITOR_EVENTS_PATH", str(events_path))
        monkeypatch.setenv("DISPATCH_JOB_ID", "job-1")

        _common.run_impala_shell([sys.executable, str(script)], pool="adhoc")
        _common.run_impala_shell([sys.executable, str(script)], pool="adhoc")

        events = _read_events(events_path)
        ids = {event["shell_execution_id"] for event in events}
        assert len(ids) == 2

    def test_shell_runs_are_numbered_as_initial_then_pool_fallback(
        self, tmp_path, monkeypatch
    ) -> None:
        script = tmp_path / "plain_child.py"
        script.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        events_path = tmp_path / "monitor.events.jsonl"
        monkeypatch.setenv("DISPATCH_MONITOR_EVENTS_PATH", str(events_path))
        monkeypatch.setenv("DISPATCH_JOB_ID", "job-1")
        monkeypatch.setenv("DISPATCH_ORCHESTRATOR_CALL_ID", "call-0001")
        monkeypatch.setenv("DISPATCH_ORCHESTRATOR_CALL_INDEX", "1")
        monkeypatch.setenv("DISPATCH_ORCHESTRATOR_SCRIPT", "download_to_csv.py")
        monkeypatch.setattr(_common, "_SHELL_EXECUTION_COUNTER", 0)

        _common.run_impala_shell([sys.executable, str(script)], pool="acs_small")
        _common.run_impala_shell([sys.executable, str(script)], pool="acs_large")

        started = [event for event in _read_events(events_path) if event["type"] == "shell_started"]
        assert [event["shell_relation"] for event in started] == [
            "initial",
            "orchestrator_pool_fallback",
        ]


class TestRunImpalaShellByteEquivalence:
    """The returned tuple must be exactly what ``Popen(...).communicate()``
    returns today, so downstream error classification sees identical input."""

    def test_exit_code_and_bytes_match_communicate_for_success(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("DISPATCH_MONITOR_EVENTS_PATH", raising=False)
        script = tmp_path / "echo_child.py"
        script.write_text(
            "import sys\n"
            "sys.stdout.write('stdout-line\\n')\n"
            "sys.stderr.write('stderr-line\\n')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        argv = [sys.executable, str(script)]

        reference = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        expected_stdout, expected_stderr = reference.communicate()
        expected_returncode = reference.returncode

        returncode, stdout, stderr = _common.run_impala_shell(argv)

        assert returncode == expected_returncode == 0
        assert stdout == expected_stdout
        assert stderr == expected_stderr

    def test_exit_code_and_bytes_match_communicate_for_failure(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("DISPATCH_MONITOR_EVENTS_PATH", raising=False)
        script = tmp_path / "fail_child.py"
        script.write_text(
            "import sys\nsys.stderr.write('AnalysisException: boom\\n')\nsys.exit(1)\n",
            encoding="utf-8",
        )
        argv = [sys.executable, str(script)]

        reference = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        expected_stdout, expected_stderr = reference.communicate()
        expected_returncode = reference.returncode

        returncode, stdout, stderr = _common.run_impala_shell(argv)

        assert returncode == expected_returncode == 1
        assert stdout == expected_stdout == b""
        assert stderr == expected_stderr


class TestRunnerOrchestratorEnvMonitorVars:
    """dispatch.runner._orchestrator_env always sets the monitor env vars."""

    def test_monitor_vars_set_when_queue_unset(self, tmp_path) -> None:
        from dispatch import runner

        job_dir = tmp_path / "job-1"
        job_dir.mkdir()
        env = runner._orchestrator_env(
            {"id": "job-1", "params": {}},
            job_dir,
            {"script": "Query_Impala_Parametrized.py", "argv": []},
            1,
        )

        assert env["DISPATCH_JOB_ID"] == "job-1"
        assert env["DISPATCH_MONITOR_EVENTS_PATH"] == str(job_dir / "monitor.events.jsonl")
        assert env["DISPATCH_ORCHESTRATOR_CALL_ID"] == "call-0001"
        assert env["DISPATCH_ORCHESTRATOR_CALL_INDEX"] == "1"
        assert env["DISPATCH_ORCHESTRATOR_SCRIPT"] == "Query_Impala_Parametrized.py"
        assert "DISPATCH_REQUEST_POOL" not in env

    def test_monitor_vars_set_when_queue_pinned(self, tmp_path) -> None:
        from dispatch import runner

        job_dir = tmp_path / "job-2"
        job_dir.mkdir()
        env = runner._orchestrator_env(
            {"id": "job-2", "params": {"queue": "acs_large"}},
            job_dir,
            {"script": "download_to_csv.py", "argv": []},
            2,
        )

        assert env["DISPATCH_JOB_ID"] == "job-2"
        assert env["DISPATCH_MONITOR_EVENTS_PATH"] == str(job_dir / "monitor.events.jsonl")
        assert env["DISPATCH_REQUEST_POOL"] == "acs_large"

    def test_monitor_vars_set_when_queue_is_auto(self, tmp_path) -> None:
        from dispatch import runner

        job_dir = tmp_path / "job-3"
        job_dir.mkdir()
        env = runner._orchestrator_env(
            {"id": "job-3", "params": {"queue": "auto"}},
            job_dir,
            {"script": "download_to_csv.py", "argv": []},
            1,
        )

        assert env["DISPATCH_JOB_ID"] == "job-3"
        assert env["DISPATCH_MONITOR_EVENTS_PATH"] == str(job_dir / "monitor.events.jsonl")
        assert "DISPATCH_REQUEST_POOL" not in env

    def test_env_copies_os_environ(self, tmp_path, monkeypatch) -> None:
        from dispatch import runner

        monkeypatch.setenv("SOME_UNRELATED_VAR", "keep-me")
        job_dir = tmp_path / "job-4"
        job_dir.mkdir()
        env = runner._orchestrator_env(
            {"id": "job-4", "params": {}},
            job_dir,
            {"script": "download_to_csv.py", "argv": []},
            1,
        )

        assert env["SOME_UNRELATED_VAR"] == "keep-me"

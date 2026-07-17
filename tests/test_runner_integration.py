"""Runner ↔ orchestrator integration tests.

ADR-0004: "the most interesting bugs live at the runner ↔ orchestrator
boundary, which unit tests can't cover."

Each test:
- Creates a real manifest on disk via ``manifest.create_job``.
- Spawns ``dispatch.runner`` as a subprocess (avoiding global-state pollution
  and signal-handler side-effects in the test process).
- Asserts the final manifest state written to disk by the runner.

The ``mock_env`` fixture from ``conftest.py`` ensures:
- ``mocks/bin/`` is prepended to ``PATH`` so the real orchestrators find the
  fake ``impala-shell``, ``klist``, and ``kinit``.
- ``DISPATCH_MOCK_STATE_DIR`` is a fresh ``tmp_path`` sub-directory, so the
  call-count state used by ``memory_exceeded`` never leaks between tests.
- ``DISPATCH_DATA_ROOT`` redirects all Job directories to a temp location.
- ``DISPATCH_SCR_DIR`` points at the real ``scr/`` directory so orchestrator
  imports resolve correctly.
- ``MAILHOST`` points at a closed port so email attempts fail fast.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dispatch import manifest


def _create_csv_job(tmp_path: Path, user: str = "testuser") -> tuple[Path, manifest.JobManifest]:
    """Create a minimal SqlFile → Csv job manifest and return (job_dir, manifest)."""
    sql_file = tmp_path / "test.sql"
    sql_file.write_text("SELECT 1;", encoding="utf-8")
    return manifest.create_job(
        source={"type": "SqlFile", "sql_path_at_launch": str(sql_file)},
        destination={"type": "Csv", "table_name": "test_export", "schema": "", "csv_path": ""},
        params={"to_email": "test@example.com", "subject": "Test"},
        launch_cwd=tmp_path,
        sql_text="SELECT 1;",
        user=user,
    )


def _create_csv_job_with_queue(
    tmp_path: Path, queue: str, user: str = "testuser"
) -> tuple[Path, manifest.JobManifest]:
    """Create a SqlFile → Csv job that requests a specific execution queue."""
    sql_file = tmp_path / "queued.sql"
    sql_file.write_text("SELECT 1;", encoding="utf-8")
    return manifest.create_job(
        source={"type": "SqlFile", "sql_path_at_launch": str(sql_file)},
        destination={"type": "Csv", "table_name": "queued_export", "schema": "", "csv_path": ""},
        params={"to_email": "test@example.com", "subject": "Test", "queue": queue},
        launch_cwd=tmp_path,
        sql_text="SELECT 1;",
        user=user,
    )


def _create_sqlfile_table_job(
    tmp_path: Path, user: str = "testuser"
) -> tuple[Path, manifest.JobManifest]:
    """Create a minimal SqlFile to Table job manifest."""
    sql_file = tmp_path / "table.sql"
    sql_text = "SELECT 1 AS smoke_test_value"
    sql_file.write_text(sql_text, encoding="utf-8")
    return manifest.create_job(
        source={"type": "SqlFile", "sql_path_at_launch": str(sql_file)},
        destination={"type": "Table", "schema": "aa_enc", "table_name": "dispatch_smoke_table"},
        params={"to_email": "test@example.com", "subject": "Test Table"},
        launch_cwd=tmp_path,
        sql_text=sql_text,
        user=user,
    )


def _create_sqlfile_table_plus_csv_job(
    tmp_path: Path, user: str = "testuser"
) -> tuple[Path, manifest.JobManifest]:
    """Create a minimal SqlFile to Table+Csv job manifest."""
    sql_file = tmp_path / "table_plus_csv.sql"
    sql_text = "SELECT 1 AS smoke_test_value"
    sql_file.write_text(sql_text, encoding="utf-8")
    return manifest.create_job(
        source={"type": "SqlFile", "sql_path_at_launch": str(sql_file)},
        destination={
            "type": "Table+Csv",
            "schema": "aa_enc",
            "table_name": "dispatch_smoke_table",
            "csv_path": str(tmp_path / "dispatch_smoke_table.csv"),
        },
        params={"to_email": "test@example.com", "subject": "Test Table Csv"},
        launch_cwd=tmp_path,
        sql_text=sql_text,
        user=user,
    )


def _create_sqltemplate_table_job(
    tmp_path: Path, user: str = "testuser"
) -> tuple[Path, manifest.JobManifest]:
    """Create a one-month SqlTemplate to Table job manifest."""
    sql_file = tmp_path / "template.sql"
    sql_text = (
        "SELECT 1 AS smoke_test_value, "
        "CAST('{date_inicio}' AS STRING) AS date_inicio, "
        "CAST('{date_fim}' AS STRING) AS date_fim"
    )
    sql_file.write_text(sql_text, encoding="utf-8")
    return manifest.create_job(
        source={"type": "SqlTemplate", "sql_path_at_launch": str(sql_file)},
        destination={"type": "Table", "schema": "aa_enc", "table_name": "dispatch_smoke_monthly"},
        params={
            "to_email": "test@example.com",
            "subject": "Test Monthly",
            "start_date": "01/01/2026",
            "end_date": "01/31/2026",
        },
        launch_cwd=tmp_path,
        sql_text=sql_text,
        user=user,
    )


def _create_existingtable_csv_job(
    tmp_path: Path, user: str = "testuser"
) -> tuple[Path, manifest.JobManifest]:
    """Create a minimal ExistingTable to Csv job manifest."""
    return manifest.create_job(
        source={"type": "ExistingTable", "table_name": "aa_enc.dispatch_smoke_existing"},
        destination={
            "type": "Csv",
            "schema": "aa_enc",
            "table_name": "dispatch_smoke_existing",
            "csv_path": str(tmp_path / "dispatch_smoke_existing.csv"),
        },
        params={"to_email": "test@example.com", "subject": "Test Existing Table"},
        launch_cwd=tmp_path,
        user=user,
    )


def _spawn_runner(job_dir: Path) -> subprocess.CompletedProcess:
    """Run dispatch.runner synchronously and return the completed process."""
    return subprocess.run(
        [sys.executable, "-m", "dispatch.runner", "--job-dir", str(job_dir)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _read_log(job_dir: Path) -> str:
    log = job_dir / "run.log"
    return log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""


# Legal runner coverage matrix:
# - SqlFile -> Csv: happy path, CSV output, fatal scenarios, retry scenario.
# - SqlFile -> Table: happy path, Query_Impala_Parametrized.py call contract.
# - SqlFile -> Table+Csv: happy path, table call then CSV export call, plain CSV output.
# - SqlTemplate -> Table: one-month happy path through monthly_query_processor.py.
# - ExistingTable -> Csv: happy path, download by --table-name rather than --query-file.


# =============================================================================
# Lifecycle transitions per scenario
# =============================================================================


class TestRunnerLifecycle:
    def test_happy_path_reaches_succeeded(self, mock_env, tmp_path):
        """happy_path scenario: runner sets manifest state to Succeeded."""
        job_dir, _ = _create_csv_job(tmp_path)
        result = _spawn_runner(job_dir)
        assert result.returncode == 0, result.stderr or result.stdout or _read_log(job_dir)

        final = manifest.load(job_dir / "manifest.json")
        assert final["state"] == "Succeeded", _read_log(job_dir)
        assert final["exit_code"] == 0
        assert final["finished_at"] is not None
        assert result.returncode == 0

    def test_csv_job_writes_plain_csv_to_launch_cwd(self, mock_env, tmp_path):
        """Csv jobs write an uncompressed CSV beside the launch-time SQL files."""
        job_dir, _ = _create_csv_job(tmp_path)
        result = _spawn_runner(job_dir)
        assert result.returncode == 0, result.stderr or result.stdout or _read_log(job_dir)

        final = manifest.load(job_dir / "manifest.json")
        csv_path = Path(final["destination"]["csv_path"])

        assert final["state"] == "Succeeded", _read_log(job_dir)
        assert csv_path == tmp_path / "test_export.csv"
        assert csv_path.exists()
        assert csv_path.suffix == ".csv"
        assert not (tmp_path / "test_export.csv.gz").exists()
        assert not any(job_dir.glob("*.csv*"))

    def test_sqlfile_table_reaches_succeeded_with_query_impala(self, mock_env, tmp_path):
        """SqlFile -> Table runs Query_Impala_Parametrized.py and succeeds."""
        job_dir, initial = _create_sqlfile_table_job(tmp_path)
        assert [call["script"] for call in initial["orchestrator_calls"]] == [
            "Query_Impala_Parametrized.py"
        ]

        result = _spawn_runner(job_dir)
        assert result.returncode == 0, result.stderr or result.stdout or _read_log(job_dir)

        final = manifest.load(job_dir / "manifest.json")
        assert final["state"] == "Succeeded", _read_log(job_dir)

    def test_sqlfile_table_plus_csv_decomposes_and_writes_plain_csv(self, mock_env, tmp_path):
        """SqlFile -> Table+Csv creates the table first, then exports plain CSV."""
        job_dir, initial = _create_sqlfile_table_plus_csv_job(tmp_path)
        assert [call["script"] for call in initial["orchestrator_calls"]] == [
            "Query_Impala_Parametrized.py",
            "download_to_csv.py",
        ]

        result = _spawn_runner(job_dir)
        assert result.returncode == 0, result.stderr or result.stdout or _read_log(job_dir)

        final = manifest.load(job_dir / "manifest.json")
        csv_path = Path(final["destination"]["csv_path"])
        assert final["state"] == "Succeeded", _read_log(job_dir)
        assert csv_path == tmp_path / "dispatch_smoke_table.csv"
        assert csv_path.exists()
        assert csv_path.suffix == ".csv"
        assert not (tmp_path / "dispatch_smoke_table.csv.gz").exists()

    def test_existingtable_csv_reaches_succeeded_using_table_name(self, mock_env, tmp_path):
        """ExistingTable -> Csv exports by --table-name, not --query-file."""
        job_dir, initial = _create_existingtable_csv_job(tmp_path)
        argv = initial["orchestrator_calls"][0]["argv"]
        assert initial["orchestrator_calls"][0]["script"] == "download_to_csv.py"
        assert "--table-name" in argv
        assert "--query-file" not in argv

        result = _spawn_runner(job_dir)
        assert result.returncode == 0, result.stderr or result.stdout or _read_log(job_dir)

        final = manifest.load(job_dir / "manifest.json")
        csv_path = Path(final["destination"]["csv_path"])
        assert final["state"] == "Succeeded", _read_log(job_dir)
        assert csv_path == tmp_path / "dispatch_smoke_existing.csv"
        assert csv_path.exists()

    def test_sqltemplate_table_runner_contract(self, mock_env, tmp_path):
        """SqlTemplate -> Table runs monthly_query_processor.py for a one-month range."""
        job_dir, initial = _create_sqltemplate_table_job(tmp_path)
        assert [call["script"] for call in initial["orchestrator_calls"]] == [
            "monthly_query_processor.py"
        ]

        result = _spawn_runner(job_dir)
        assert result.returncode == 0, result.stderr or result.stdout or _read_log(job_dir)

        final = manifest.load(job_dir / "manifest.json")
        assert final["state"] == "Succeeded", _read_log(job_dir)

    def test_syntax_error_reaches_failed(self, mock_env, monkeypatch, tmp_path):
        """syntax_error is a FATAL_ERRORS member → orchestrator exits 1 → Failed."""
        monkeypatch.setenv("DISPATCH_MOCK_SCENARIO", "syntax_error")
        job_dir, _ = _create_csv_job(tmp_path)
        result = _spawn_runner(job_dir)

        final = manifest.load(job_dir / "manifest.json")
        assert final["state"] == "Failed", _read_log(job_dir)
        assert final["exit_code"] != 0
        assert result.returncode != 0

    def test_auth_error_reaches_failed(self, mock_env, monkeypatch, tmp_path):
        """auth_error is a FATAL_ERRORS member → orchestrator exits 1 → Failed."""
        monkeypatch.setenv("DISPATCH_MOCK_SCENARIO", "auth_error")
        job_dir, _ = _create_csv_job(tmp_path)
        _spawn_runner(job_dir)

        final = manifest.load(job_dir / "manifest.json")
        assert final["state"] == "Failed", _read_log(job_dir)

    def test_table_not_found_reaches_failed(self, mock_env, monkeypatch, tmp_path):
        """table_not_found is TABLE_NOT_FOUND (FATAL) → Failed."""
        monkeypatch.setenv("DISPATCH_MOCK_SCENARIO", "table_not_found")
        job_dir, _ = _create_csv_job(tmp_path)
        _spawn_runner(job_dir)

        final = manifest.load(job_dir / "manifest.json")
        assert final["state"] == "Failed", _read_log(job_dir)

    def test_memory_exceeded_eventually_succeeds(self, mock_env, monkeypatch, tmp_path):
        """memory_exceeded fails twice then succeeds on 3rd pool → Succeeded."""
        monkeypatch.setenv("DISPATCH_MOCK_SCENARIO", "memory_exceeded")
        job_dir, _ = _create_csv_job(tmp_path)
        result = _spawn_runner(job_dir)
        assert result.returncode == 0, result.stderr or result.stdout or _read_log(job_dir)

        final = manifest.load(job_dir / "manifest.json")
        assert final["state"] == "Succeeded", _read_log(job_dir)


# =============================================================================
# Execution identity event protocol sidecar (monitor.events.jsonl)
# =============================================================================


def _read_monitor_events(job_dir: Path) -> list[dict]:
    events_path = job_dir / "monitor.events.jsonl"
    if not events_path.exists():
        return []
    import json

    lines = events_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class TestRunnerMonitorEventsSidecar:
    """DISPATCH_MONITOR_EVENTS_PATH/DISPATCH_JOB_ID wiring end-to-end via the
    mock impala-shell's monitor-line scenarios."""

    def test_monitor_line_scenario_writes_query_discovered_event(
        self, mock_env, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("DISPATCH_MOCK_SCENARIO", "monitor_line")
        job_dir, initial = _create_csv_job(tmp_path)
        result = _spawn_runner(job_dir)
        assert result.returncode == 0, result.stderr or result.stdout or _read_log(job_dir)

        final = manifest.load(job_dir / "manifest.json")
        assert final["state"] == "Succeeded", _read_log(job_dir)

        events = _read_monitor_events(job_dir)
        types = [event["type"] for event in events]
        assert "shell_started" in types
        assert "query_discovered" in types
        assert "shell_finished" in types
        discovered = next(event for event in events if event["type"] == "query_discovered")
        assert discovered["job_id"] == initial["id"]
        assert discovered["coordinator_base_url"].startswith("https://")
        assert ":" in discovered["query_id"]

    def test_transparent_retry_scenario_writes_query_retried_event(
        self, mock_env, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("DISPATCH_MOCK_SCENARIO", "transparent_retry")
        job_dir, initial = _create_csv_job(tmp_path)
        result = _spawn_runner(job_dir)
        assert result.returncode == 0, result.stderr or result.stdout or _read_log(job_dir)

        events = _read_monitor_events(job_dir)
        types = [event["type"] for event in events]
        assert "query_discovered" in types
        assert "query_retried" in types
        retried = next(event for event in events if event["type"] == "query_retried")
        assert retried["job_id"] == initial["id"]

    def test_happy_path_scenario_writes_no_discovery_events(self, mock_env, tmp_path):
        """No monitor line in the mock output => shell events but no discovery."""
        job_dir, _ = _create_csv_job(tmp_path)
        result = _spawn_runner(job_dir)
        assert result.returncode == 0, result.stderr or result.stdout or _read_log(job_dir)

        events = _read_monitor_events(job_dir)
        types = [event["type"] for event in events]
        assert "shell_started" in types
        assert "shell_finished" in types
        assert "query_discovered" not in types
        assert "query_retried" not in types

    def test_table_plus_csv_calls_receive_distinct_deterministic_lineage(
        self, mock_env, tmp_path
    ):
        job_dir, _ = _create_sqlfile_table_plus_csv_job(tmp_path)

        result = _spawn_runner(job_dir)

        assert result.returncode == 0, result.stderr or result.stdout or _read_log(job_dir)
        started = [
            event
            for event in _read_monitor_events(job_dir)
            if event["type"] == "shell_started"
        ]
        assert [(event["orchestrator_call_id"], event["orchestrator_call_index"]) for event in started] == [
            ("call-0001", 1),
            ("call-0002", 2),
        ]
        assert [event["orchestrator_script"] for event in started] == [
            "Query_Impala_Parametrized.py",
            "download_to_csv.py",
        ]
        assert [event["shell_relation"] for event in started] == ["initial", "initial"]


# =============================================================================
# Execution-queue selection (DISPATCH_REQUEST_POOL wiring)
# =============================================================================


class TestRunnerQueueSelection:
    """The queue chosen in New Job must be the pool the orchestrator runs on.

    ``download_to_csv`` logs ``Attempting export with queue: <fila>`` at INFO
    for every pool it tries, so the run log is a faithful record of which queue
    actually executed the query.
    """

    def test_selected_queue_pins_request_pool(self, mock_env, tmp_path):
        """A specific queue selection runs the query on exactly that pool."""
        job_dir, _ = _create_csv_job_with_queue(tmp_path, queue="acs_large")
        result = _spawn_runner(job_dir)
        assert result.returncode == 0, result.stderr or result.stdout or _read_log(job_dir)

        log = _read_log(job_dir)
        assert "[runner] pinning request_pool=acs_large" in log, log
        assert "Attempting export with queue: acs_large" in log, log
        # No other pool from the download_to_csv default list should run.
        assert "Attempting export with queue: adhoc_fast" not in log, log
        assert manifest.load(job_dir / "manifest.json")["state"] == "Succeeded"

    def test_auto_queue_uses_orchestrator_default_first_pool(self, mock_env, tmp_path):
        """``auto`` inherits the environment: the orchestrator's default list runs.

        download_to_csv's default list starts with ``adhoc_fast``, and the happy
        path succeeds on the first pool, so that is the pool that must appear.
        """
        job_dir, _ = _create_csv_job_with_queue(tmp_path, queue="auto")
        result = _spawn_runner(job_dir)
        assert result.returncode == 0, result.stderr or result.stdout or _read_log(job_dir)

        log = _read_log(job_dir)
        assert "Attempting export with queue: adhoc_fast" in log, log
        assert "[runner] pinning request_pool" not in log, log

    def test_missing_queue_param_preserves_default_behavior(self, mock_env, tmp_path):
        """A job manifest without a ``queue`` param behaves exactly as before."""
        job_dir, _ = _create_csv_job(tmp_path)
        result = _spawn_runner(job_dir)
        assert result.returncode == 0, result.stderr or result.stdout or _read_log(job_dir)

        log = _read_log(job_dir)
        assert "Attempting export with queue: adhoc_fast" in log, log
        assert "[runner] pinning request_pool" not in log, log

    def test_multiple_selected_queues_are_cycled_in_order(self, mock_env, monkeypatch, tmp_path):
        """Several selected queues are tried in order until one succeeds.

        ``memory_exceeded`` fails the first two attempts and succeeds on the
        third. With three pinned pools the third attempt (``adhoc``) succeeds
        within the first cycle, so all three pools appear in the log in order
        and no 30s retry-cycle sleep is incurred.
        """
        monkeypatch.setenv("DISPATCH_MOCK_SCENARIO", "memory_exceeded")
        job_dir, _ = _create_csv_job_with_queue(tmp_path, queue="acs_small,acs_large,adhoc")
        result = _spawn_runner(job_dir)
        assert result.returncode == 0, result.stderr or result.stdout or _read_log(job_dir)

        log = _read_log(job_dir)
        assert "[runner] pinning request_pool=acs_small,acs_large,adhoc" in log, log
        first = log.index("Attempting export with queue: acs_small")
        second = log.index("Attempting export with queue: acs_large")
        third = log.index("Attempting export with queue: adhoc")
        assert first < second < third, log
        # The default list must not leak in when queues are pinned.
        assert "Attempting export with queue: adhoc_fast" not in log, log
        assert manifest.load(job_dir / "manifest.json")["state"] == "Succeeded"


# =============================================================================
# Manifest state guard (prevents double-spawn)
# =============================================================================


class TestRunnerStateGuard:
    def test_runner_exits_4_when_state_is_not_pending(self, mock_env, tmp_path):
        """Runner exits with code 4 if manifest.state != Pending."""
        job_dir, _ = _create_csv_job(tmp_path)
        # Manually transition to Running to simulate a double-spawn attempt
        manifest.update(job_dir / "manifest.json", state="Running")

        result = _spawn_runner(job_dir)
        assert result.returncode == 4

        # Manifest must not be mutated by the second spawn
        final = manifest.load(job_dir / "manifest.json")
        assert final["state"] == "Running"

    def test_runner_writes_error_file_on_bad_manifest(self, mock_env, tmp_path):
        """Corrupt manifest.json causes runner to write manifest.error.json and exit 3."""
        job_dir = Path(str(tmp_path)) / "badjob"
        job_dir.mkdir()
        (job_dir / "manifest.json").write_text("not valid json", encoding="utf-8")

        result = _spawn_runner(job_dir)
        assert result.returncode == 3
        assert (job_dir / "manifest.error.json").exists()


# =============================================================================
# Manifest state transitions during the run
# =============================================================================


class TestRunnerStateTransitions:
    def test_started_at_populated_after_run(self, mock_env, tmp_path):
        job_dir, _ = _create_csv_job(tmp_path)
        result = _spawn_runner(job_dir)
        assert result.returncode == 0, result.stderr or result.stdout or _read_log(job_dir)

        final = manifest.load(job_dir / "manifest.json")
        assert final["started_at"] is not None

    def test_pid_populated_during_run_then_present_in_final(self, mock_env, tmp_path):
        job_dir, _ = _create_csv_job(tmp_path)
        result = _spawn_runner(job_dir)
        assert result.returncode == 0, result.stderr or result.stdout or _read_log(job_dir)

        final = manifest.load(job_dir / "manifest.json")
        assert final["pid"] is not None

    def test_run_log_created_and_non_empty(self, mock_env, tmp_path):
        job_dir, _ = _create_csv_job(tmp_path)
        result = _spawn_runner(job_dir)
        assert result.returncode == 0, result.stderr or result.stdout or _read_log(job_dir)

        log = job_dir / "run.log"
        assert log.exists()
        assert log.stat().st_size > 0


# =============================================================================
# Cancellation via SIGTERM
# =============================================================================


class TestRunnerCancellation:
    @pytest.mark.skipif(
        os.name == "nt", reason="Windows SIGTERM does not exercise the POSIX runner handler"
    )
    def test_sigterm_sets_state_to_cancelled(self, mock_env, tmp_path):
        """SIGTERM during an in-flight Job sets manifest.state to Cancelled.

        Uses a minimal fake orchestrator (a sleep script) so the test does not
        depend on the real orchestrators or mock impala-shell.  The purpose
        here is to verify the runner's SIGTERM → Cancelled path, not the full
        runner ↔ orchestrator integration.
        """
        # Fake orchestrator: write a marker then sleep indefinitely
        marker = tmp_path / "started.txt"
        fake_orch = tmp_path / "sleeping_orch.py"
        fake_orch.write_text(
            f"import time, pathlib\npathlib.Path({str(marker)!r}).touch()\ntime.sleep(300)\n"
        )

        # Build the manifest manually to point at the fake orchestrator
        job_dir, initial = _create_csv_job(tmp_path)
        import json

        m = json.loads((job_dir / "manifest.json").read_text())
        m["orchestrator_calls"] = [{"script": "fake", "argv": [sys.executable, str(fake_orch)]}]
        (job_dir / "manifest.json").write_text(json.dumps(m, indent=2))

        proc = subprocess.Popen(
            [sys.executable, "-m", "dispatch.runner", "--job-dir", str(job_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Wait until the fake orchestrator has started (marker file created)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if marker.exists():
                break
            time.sleep(0.05)
        else:
            proc.kill()
            proc.wait()
            pytest.fail("Fake orchestrator did not start in time")

        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail("Runner did not exit after SIGTERM within 15s")

        final = manifest.load(job_dir / "manifest.json")
        assert final["state"] == "Cancelled", _read_log(job_dir)


class TestScriptArgv:
    """``script_argv`` must not exec a shebang-less script directly.

    On the edge node the scr/ orchestrators are marked executable but start
    with ``# flake8: noqa`` rather than a shebang, so exec'ing them directly
    fails with ENOEXEC. ``script_argv`` must fall back to invoking them with a
    Python interpreter in that case.
    """

    def test_executable_without_shebang_falls_back_to_python(self, tmp_path, monkeypatch):
        script = tmp_path / "Query_Impala_Parametrized.py"
        script.write_text("# flake8: noqa\nimport sys\n", encoding="utf-8")
        script.chmod(0o755)
        monkeypatch.setenv("DISPATCH_SCR_DIR", str(tmp_path))

        argv = manifest.script_argv("Query_Impala_Parametrized.py")

        assert len(argv) == 2, argv
        assert argv[0] != str(script)
        assert argv[1] == str(script)

    def test_executable_with_shebang_runs_directly(self, tmp_path, monkeypatch):
        script = tmp_path / "with_shebang.py"
        script.write_text("#!/usr/bin/env python3\nimport sys\n", encoding="utf-8")
        script.chmod(0o755)
        monkeypatch.setenv("DISPATCH_SCR_DIR", str(tmp_path))

        argv = manifest.script_argv("with_shebang.py")

        # When a real shebang is present the executable bit is trusted; on
        # platforms without an executable bit (Windows) it still falls back to
        # a two-element python invocation, which is also correct.
        assert argv[-1] == str(script)
        if len(argv) == 1:
            assert argv[0] == str(script)

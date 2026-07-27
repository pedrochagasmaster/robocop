"""Tests for the notebook API in ``dispatch/notebook.py``.

The library is a subprocess wrapper, so behavior is proven three ways:

- argv building is checked against the real ``dispatch job`` argparse parser, so
  a renamed CLI flag fails here instead of in a notebook;
- exit-code and stderr handling is checked against a stub CLI;
- launch, wait, logs, list, and cancel run the real CLI end to end under the
  ``mock_env`` fixture, including the detached runner.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import time
from datetime import date
from pathlib import Path
from typing import get_args

import pytest

from dispatch import cli_job, job_ops, jobs, manifest, notebook, notebook_display
from dispatch.notebook import (
    Dispatch,
    DispatchError,
    JobList,
    JobUnsuccessful,
    MissingResultError,
    OperationalError,
    ResultParseError,
    UnknownJobError,
    UsageError,
    WaitTimeout,
)

WAIT_TIMEOUT = 60.0
POLL_INTERVAL = 0.2


def _dispatch(cwd: Path, **kwargs) -> Dispatch:
    """A session bound to the interpreter running the tests."""
    return Dispatch(cwd, command=[sys.executable, "-m", "dispatch"], **kwargs)


def _write_sql(cwd: Path, name: str = "query.sql", text: str = "SELECT 1\n") -> Path:
    cwd.mkdir(parents=True, exist_ok=True)
    path = cwd / name
    path.write_text(text, encoding="utf-8")
    return path


def _stub_cli(
    tmp_path: Path,
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    name: str = "stub_cli",
) -> list[str]:
    """A fake CLI that replays one fixed stdout/stderr/exit-code triple."""
    spec = tmp_path / f"{name}.json"
    spec.write_text(
        json.dumps({"stdout": stdout, "stderr": stderr, "exit_code": exit_code}),
        encoding="utf-8",
    )
    script = tmp_path / f"{name}.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import json
            import sys
            from pathlib import Path

            spec = json.loads(Path({str(spec)!r}).read_text(encoding="utf-8"))
            sys.stdout.write(spec["stdout"])
            sys.stderr.write(spec["stderr"])
            sys.exit(spec["exit_code"])
            """
        ),
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


def _seed_pending_job(tmp_path: Path, *, table_name: str = "seeded") -> str:
    """Create a Pending Job on disk without a runner, and return its ID."""
    return _seed_job_dir(tmp_path, table_name=table_name).name


def _seed_running_job(tmp_path: Path, *, table_name: str) -> Path:
    """Create a Job that occupies a launch slot: Running with a live PID."""
    job_dir = _seed_job_dir(tmp_path, table_name=table_name)
    manifest.update(job_dir / "manifest.json", state="Running", pid=os.getpid())
    return job_dir


def _seed_job_dir(tmp_path: Path, *, table_name: str = "seeded") -> Path:
    """Create a Pending Job on disk without a runner, and return its directory."""
    launch_cwd = tmp_path / "seed"
    sql_path = _write_sql(launch_cwd, f"{table_name}.sql")
    job_dir, _item = manifest.create_job(
        source={"type": "SqlFile", "sql_path_at_launch": str(sql_path)},
        destination={
            "type": "Csv",
            "schema": "aa_enc",
            "table_name": table_name,
            "csv_path": str(launch_cwd / f"{table_name}.csv"),
        },
        params={"to_email": "", "subject": "t", "queue": "auto"},
        launch_cwd=launch_cwd,
        sql_text="SELECT 1",
    )
    return job_dir


def _job_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dispatch")
    subparsers = parser.add_subparsers(dest="command")
    cli_job.add_job_parser(subparsers)
    return parser


# =============================================================================
# Argument building and CLI-contract drift
# =============================================================================


class TestLaunchArgv:
    def test_unset_options_are_omitted(self) -> None:
        argv = notebook._launch_argv(
            source="SqlFile",
            destination="Csv",
            sql="query.sql",
            existing_table=None,
            schema=None,
            table=None,
            start_date=None,
            end_date=None,
            email=None,
            subject=None,
            queue=None,
            acknowledge_advisor=False,
        )
        assert argv == [
            "launch",
            "--source",
            "SqlFile",
            "--destination",
            "Csv",
            "--yes",
            "--sql",
            "query.sql",
        ]

    def test_dates_paths_and_queues_are_serialized(self, tmp_path: Path) -> None:
        argv = notebook._launch_argv(
            source="SqlTemplate",
            destination="Table",
            sql=tmp_path / "monthly.sql",
            existing_table=None,
            schema="aa_enc",
            table="monthly_out",
            start_date=date(2026, 1, 1),
            end_date="2026-03-31",
            email="analyst@example.com",
            subject="Monthly",
            queue=["adhoc_fast", "acs_large"],
            acknowledge_advisor=True,
        )
        assert argv[argv.index("--sql") + 1] == str(tmp_path / "monthly.sql")
        assert argv[argv.index("--start-date") + 1] == "2026-01-01"
        assert argv[argv.index("--end-date") + 1] == "2026-03-31"
        assert argv[argv.index("--queue") + 1] == "adhoc_fast,acs_large"
        assert "--acknowledge-advisor" in argv

    def test_launch_argv_is_accepted_by_the_cli_parser(self) -> None:
        argv = notebook._launch_argv(
            source="SqlFile",
            destination="Table+Csv",
            sql="query.sql",
            existing_table="aa_enc.events",
            schema="aa_enc",
            table="out",
            start_date="2026-01-01",
            end_date="2026-01-31",
            email="analyst@example.com",
            subject="Subject",
            queue="auto",
            acknowledge_advisor=True,
        )
        args = _job_parser().parse_args(["job", *argv, "--json"])
        assert args.job_command == "launch"
        assert (args.source, args.destination) == ("SqlFile", "Table+Csv")
        assert args.yes and args.acknowledge_advisor and args.json_output

    @pytest.mark.parametrize(
        "argv",
        [
            ["list", "--json"],
            ["list", "--state", "Running", "--json"],
            ["show", "20260101T000000Z_abcdef", "--json"],
            ["logs", "20260101T000000Z_abcdef", "--lines", "10"],
            ["logs", "20260101T000000Z_abcdef", "--lines", "10", "--follow"],
            ["cancel", "20260101T000000Z_abcdef", "--yes", "--json"],
        ],
    )
    def test_supervision_argv_is_accepted_by_the_cli_parser(self, argv: list[str]) -> None:
        args = _job_parser().parse_args(["job", *argv])
        assert args.job_command == argv[0]


class TestCliContract:
    """The library's literals must track the CLI's own vocabulary."""

    def test_job_states_match_job_ops(self) -> None:
        assert get_args(notebook.JobState) == job_ops.JOB_STATES

    def test_terminal_states_match_job_ops(self) -> None:
        assert notebook.TERMINAL_STATES == job_ops.TERMINAL_STATES

    def test_source_and_destination_match_legal_cells(self) -> None:
        assert set(get_args(notebook.SourceType)) == {cell[0] for cell in manifest.LEGAL_CELLS}
        assert set(get_args(notebook.DestinationType)) == {cell[1] for cell in manifest.LEGAL_CELLS}


class TestCliCommand:
    def test_defaults_to_the_running_interpreter(self, monkeypatch) -> None:
        monkeypatch.delenv(notebook.CLI_ENV_VAR, raising=False)
        assert notebook.cli_command() == [sys.executable, "-m", "dispatch"]

    def test_env_override_is_split_like_a_shell_command(self, monkeypatch) -> None:
        monkeypatch.setenv(notebook.CLI_ENV_VAR, "/ads_storage/dispatch/bin/dispatch --quiet")
        assert notebook.cli_command() == ["/ads_storage/dispatch/bin/dispatch", "--quiet"]


# =============================================================================
# Exit codes, stderr, and unusable CLIs
# =============================================================================


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("exit_code", "expected"),
        [
            (1, JobUnsuccessful),
            (2, UsageError),
            (3, UnknownJobError),
            (4, OperationalError),
            (7, DispatchError),
        ],
    )
    def test_exit_code_selects_the_exception(
        self, tmp_path: Path, exit_code: int, expected: type[DispatchError]
    ) -> None:
        command = _stub_cli(
            tmp_path,
            exit_code=exit_code,
            stderr=json.dumps({"error": "refused", "exit_code": exit_code}),
        )
        session = Dispatch(tmp_path, command=command)
        with pytest.raises(expected) as excinfo:
            session.jobs()
        assert str(excinfo.value) == "refused"
        assert excinfo.value.exit_code == exit_code

    def test_exception_type_is_exact_not_just_a_base(self, tmp_path: Path) -> None:
        command = _stub_cli(tmp_path, exit_code=3, stderr="Unknown Job ID: x")
        with pytest.raises(UnknownJobError):
            Dispatch(tmp_path, command=command).jobs()

    def test_plain_stderr_is_used_when_not_json(self, tmp_path: Path) -> None:
        command = _stub_cli(tmp_path, exit_code=2, stderr="error: argument --source: invalid")
        with pytest.raises(UsageError, match="invalid"):
            Dispatch(tmp_path, command=command).jobs()

    def test_empty_stderr_falls_back_to_the_command_line(self, tmp_path: Path) -> None:
        command = _stub_cli(tmp_path, exit_code=4)
        with pytest.raises(OperationalError, match="exited with code 4"):
            Dispatch(tmp_path, command=command).jobs()

    def test_unparsable_stdout_is_operational(self, tmp_path: Path) -> None:
        command = _stub_cli(tmp_path, exit_code=0, stdout="not json")
        with pytest.raises(OperationalError, match="Could not parse JSON"):
            Dispatch(tmp_path, command=command).jobs()

    def test_missing_cli_explains_how_to_point_at_it(self, tmp_path: Path) -> None:
        session = Dispatch(tmp_path, command=["dispatch-does-not-exist"])
        with pytest.raises(OperationalError, match=notebook.CLI_ENV_VAR):
            session.jobs()

    def test_command_timeout_is_operational(self, tmp_path: Path) -> None:
        script = tmp_path / "sleeper.py"
        script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        session = Dispatch(tmp_path, command=[sys.executable, str(script)], timeout=0.5)
        with pytest.raises(OperationalError, match="command timeout"):
            session.jobs()


# =============================================================================
# End-to-end against the real CLI and the mock layer
# =============================================================================


class TestLaunchAndMonitor:
    def test_launch_wait_reports_success_and_writes_csv(self, mock_env, tmp_path: Path) -> None:
        cwd = tmp_path / "sql"
        _write_sql(cwd)
        session = _dispatch(cwd)

        job = session.launch(source="SqlFile", destination="Csv", sql="query.sql", table="report")
        job.wait(timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL)

        assert job.succeeded, job.logs()
        assert job.exit_code == 0
        assert job.is_terminal and not job.failed
        assert job.csv_path == str(cwd / "report.csv")
        assert Path(job.csv_path).exists()
        assert job.source == "SqlFile" and job.destination == "Csv"
        assert job.elapsed_seconds is not None

    def test_watch_returns_the_terminal_job(self, mock_env, tmp_path: Path) -> None:
        cwd = tmp_path / "sql"
        _write_sql(cwd)
        job = _dispatch(cwd).launch(
            source="SqlFile", destination="Csv", sql="query.sql", table="watched"
        )

        watched = job.watch(timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL, lines=5)

        assert watched is job
        assert watched.is_terminal

    def test_failed_job_is_returned_not_raised(self, mock_env, tmp_path: Path) -> None:
        cwd = tmp_path / "sql"
        _write_sql(cwd)
        session = _dispatch(cwd, env={"DISPATCH_MOCK_SCENARIO": "syntax_error"})

        job = session.launch(
            source="SqlFile", destination="Csv", sql="query.sql", table="broken"
        ).wait(timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL)

        assert job.failed and not job.succeeded
        assert job.exit_code not in (None, 0)

    def test_advisor_errors_gate_the_launch_until_acknowledged(
        self, mock_env, tmp_path: Path
    ) -> None:
        cwd = tmp_path / "sql"
        _write_sql(cwd, "cross.sql", "SELECT a.x FROM aa_enc.t1 a CROSS JOIN aa_enc.t2 b\n")
        session = _dispatch(cwd)

        with pytest.raises(UsageError, match="acknowledge"):
            session.launch(source="SqlFile", destination="Csv", sql="cross.sql", table="crossed")

        job = session.launch(
            source="SqlFile",
            destination="Csv",
            sql="cross.sql",
            table="crossed",
            acknowledge_advisor=True,
        )
        assert job.id

    def test_missing_sql_file_is_a_usage_error(self, mock_env, tmp_path: Path) -> None:
        cwd = tmp_path / "sql"
        cwd.mkdir()
        with pytest.raises(UsageError, match="SQL file not found"):
            _dispatch(cwd).launch(source="SqlFile", destination="Csv", sql="absent.sql")

    def test_unknown_job_raises_unknown_job_error(self, mock_env, tmp_path: Path) -> None:
        with pytest.raises(UnknownJobError):
            _dispatch(tmp_path).job("20260101T000000Z_absent")

    def test_wait_timeout_leaves_the_job_alone(self, mock_env, tmp_path: Path) -> None:
        job_id = _seed_pending_job(tmp_path)
        job = _dispatch(tmp_path).job(job_id)

        with pytest.raises(WaitTimeout, match="Pending"):
            job.wait(timeout=0.4, poll_interval=0.1)

        assert job.refresh().state == "Pending"

    def test_wait_rejects_a_non_positive_poll_interval(self, mock_env, tmp_path: Path) -> None:
        job_id = _seed_pending_job(tmp_path)
        with pytest.raises(ValueError):
            _dispatch(tmp_path).job(job_id).wait(poll_interval=0)


class TestQueriesAndLogs:
    def test_jobs_lists_and_filters_by_state(self, mock_env, tmp_path: Path) -> None:
        cwd = tmp_path / "sql"
        _write_sql(cwd)
        session = _dispatch(cwd)
        job = session.launch(
            source="SqlFile", destination="Csv", sql="query.sql", table="listed"
        ).wait(timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL)

        listed = session.jobs()
        assert isinstance(listed, JobList)
        assert job.id in [item.id for item in listed]
        assert [item.id for item in session.jobs(state="Succeeded")] == [job.id]
        assert session.jobs(state="Running") == []
        assert listed.to_dicts()[0]["id"] == job.id

    def test_logs_and_stream_logs_return_orchestrator_output(
        self, mock_env, tmp_path: Path
    ) -> None:
        cwd = tmp_path / "sql"
        _write_sql(cwd)
        job = (
            _dispatch(cwd)
            .launch(source="SqlFile", destination="Csv", sql="query.sql", table="logged")
            .wait(timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL)
        )

        text = job.logs(lines=200)
        streamed = list(job.stream_logs(lines=200))

        assert "SUCCESS" in text
        assert any("SUCCESS" in line for line in streamed)
        assert len(job.logs(lines=2).splitlines()) <= 2

    def test_print_logs_writes_to_stdout(self, mock_env, tmp_path: Path, capfd) -> None:
        cwd = tmp_path / "sql"
        _write_sql(cwd)
        job = (
            _dispatch(cwd)
            .launch(source="SqlFile", destination="Csv", sql="query.sql", table="printed")
            .wait(timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL)
        )

        job.print_logs(lines=5)

        assert "runner" in capfd.readouterr().out

    def test_job_exposes_manifest_and_params(self, mock_env, tmp_path: Path) -> None:
        cwd = tmp_path / "sql"
        _write_sql(cwd)
        job = _dispatch(cwd).launch(
            source="SqlFile",
            destination="Csv",
            sql="query.sql",
            table="described",
            queue="acs_large",
            email="analyst@example.com",
        )

        assert job.params["queue"] == "acs_large"
        assert job.params["to_email"] == "analyst@example.com"
        assert job.manifest["id"] == job.id
        assert job.user
        assert job.to_dict()["id"] == job.id

    def test_cancel_marks_a_pending_job_cancelled(self, mock_env, tmp_path: Path) -> None:
        job_id = _seed_pending_job(tmp_path)
        job = _dispatch(tmp_path).job(job_id)

        job.cancel()

        assert job.cancelled
        assert job.is_terminal

    def test_cancel_of_a_terminal_job_is_operational(self, mock_env, tmp_path: Path) -> None:
        cwd = tmp_path / "sql"
        _write_sql(cwd)
        job = (
            _dispatch(cwd)
            .launch(source="SqlFile", destination="Csv", sql="query.sql", table="done")
            .wait(timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL)
        )

        with pytest.raises(OperationalError):
            job.cancel()


class TestInlineSql:
    """``sql()`` writes Inline SQL into the workspace and launches it (ADR-0009)."""

    def test_inline_sql_runs_and_loads_a_dataframe(self, mock_env, tmp_path: Path) -> None:
        cwd = tmp_path / "sql"
        cwd.mkdir()
        session = _dispatch(cwd)

        job = session.sql("SELECT 1 AS answer")
        frame = job.to_df(timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL)

        assert job.succeeded, job.logs()
        assert list(frame.columns) == ["id", "value"]
        assert frame.shape == (1, 2)
        assert job.columns == ["id", "value"]
        assert job.rows() == [{"id": "1", "value": "mock"}]
        assert notebook.Job.to_pandas is notebook.Job.to_df

    def test_result_lands_in_the_workspace_not_the_working_directory(
        self, mock_env, tmp_path: Path
    ) -> None:
        cwd = tmp_path / "sql"
        cwd.mkdir()
        session = _dispatch(cwd)

        job = session.sql("SELECT 1").wait(timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL)

        result = job.result_path
        assert result is not None
        assert result.is_relative_to(session.workspace)
        assert result.parent.parent == session.workspace
        assert list(cwd.iterdir()) == []

    def test_inline_sql_is_saved_beside_its_result(self, mock_env, tmp_path: Path) -> None:
        session = _dispatch(tmp_path)

        job = session.sql("SELECT 42 AS answer")

        query_dir = job.result_path.parent
        saved = list(query_dir.glob("*.sql"))
        assert len(saved) == 1
        assert saved[0].read_text(encoding="utf-8") == "SELECT 42 AS answer\n"
        assert job.source_detail == str(saved[0])

    def test_each_query_gets_its_own_directory(self, mock_env, tmp_path: Path) -> None:
        session = _dispatch(tmp_path)

        first = session.sql("SELECT 1").wait(timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL)
        second = session.sql("SELECT 2").wait(timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL)

        assert first.result_path != second.result_path
        assert first.result_path.parent != second.result_path.parent

    def test_empty_sql_is_refused_before_launching(self, mock_env, tmp_path: Path) -> None:
        session = _dispatch(tmp_path)

        with pytest.raises(UsageError, match="empty"):
            session.sql("   \n")

        assert not session.workspace.exists() or list(session.workspace.iterdir()) == []

    def test_refused_launch_leaves_no_workspace_directory(self, mock_env, tmp_path: Path) -> None:
        session = _dispatch(tmp_path)

        with pytest.raises(UsageError, match="acknowledge"):
            session.sql("SELECT a.x FROM aa_enc.t1 a CROSS JOIN aa_enc.t2 b")

        assert list(session.workspace.iterdir()) == []

    def test_table_destination_has_no_result(self, mock_env, tmp_path: Path) -> None:
        session = _dispatch(tmp_path)

        job = session.sql("SELECT 1 AS answer", destination="Table", table="inline_out")

        assert job.destination == "Table"
        assert job.result_path is None
        with pytest.raises(MissingResultError, match="destination='Csv'"):
            job.to_df(timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL)

    @pytest.mark.parametrize("destination", ["Table", "Table+Csv"])
    def test_table_destinations_require_an_explicit_name(
        self, mock_env, tmp_path: Path, destination: str
    ) -> None:
        """A generated table name would leave randomly named tables in Impala."""
        with pytest.raises(UsageError, match="explicit table="):
            _dispatch(tmp_path).sql("SELECT 1", destination=destination)

    def test_inline_template_passes_the_date_range(self, mock_env, tmp_path: Path) -> None:
        session = _dispatch(tmp_path)

        job = session.sql(
            "SELECT '{date_inicio}' AS a, '{date_fim}' AS b",
            source="SqlTemplate",
            destination="Table",
            table="monthly_out",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        )

        assert job.source == "SqlTemplate"
        assert job.params["start_date"] == "01/01/2026"
        assert job.params["end_date"] == "01/31/2026"


class TestTableReads:
    def test_unlimited_read_uses_the_existing_table_source(self, mock_env, tmp_path: Path) -> None:
        session = _dispatch(tmp_path)

        job = session.table("aa_enc.events_existing").wait(
            timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL
        )

        assert job.source == "ExistingTable"
        assert job.succeeded, job.logs()
        assert job.result_path.name == "events_existing.csv"
        assert job.result_path.is_relative_to(session.workspace)

    def test_limited_read_generates_inline_sql(self, mock_env, tmp_path: Path) -> None:
        session = _dispatch(tmp_path)

        job = session.table("aa_enc.events_existing", limit=5)

        assert job.source == "SqlFile"
        saved = list(job.result_path.parent.glob("*.sql"))
        assert (
            saved[0].read_text(encoding="utf-8") == "SELECT * FROM aa_enc.events_existing LIMIT 5\n"
        )

    @pytest.mark.parametrize("name", ["events", "aa_enc.events; DROP TABLE x", "1bad.events", ""])
    def test_malformed_table_names_are_refused(self, mock_env, tmp_path: Path, name: str) -> None:
        with pytest.raises(UsageError, match="schema.table"):
            _dispatch(tmp_path).table(name)

    @pytest.mark.parametrize("limit", [0, -5])
    def test_non_positive_limits_are_refused(self, mock_env, tmp_path: Path, limit: int) -> None:
        with pytest.raises(UsageError, match="positive"):
            _dispatch(tmp_path).table("aa_enc.events", limit=limit)


class TestResultReading:
    def test_unsuccessful_job_has_no_result(self, mock_env, tmp_path: Path) -> None:
        session = _dispatch(tmp_path, env={"DISPATCH_MOCK_SCENARIO": "syntax_error"})

        job = session.sql("SELECT bad syntax")

        with pytest.raises(JobUnsuccessful, match="job.logs()"):
            job.to_df(timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL)
        assert job.failed

    def test_reading_waits_for_a_running_job(self, mock_env, tmp_path: Path) -> None:
        session = _dispatch(tmp_path)

        job = session.sql("SELECT 1")
        rows = job.rows(timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL)

        assert job.is_terminal
        assert rows == [{"id": "1", "value": "mock"}]

    def test_ragged_result_raises_instead_of_shifting_columns(
        self, mock_env, tmp_path: Path
    ) -> None:
        session = _dispatch(tmp_path)
        job = session.sql("SELECT 1").wait(timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL)
        job.result_path.write_text("id,value\n1,a,b\n", encoding="utf-8")

        with pytest.raises(ResultParseError, match="line 2 has 3 fields"):
            job.rows()
        with pytest.raises(ResultParseError):
            job.to_df()

    def test_to_csv_copies_the_result_where_asked(self, mock_env, tmp_path: Path) -> None:
        session = _dispatch(tmp_path)
        job = session.sql("SELECT 1")

        target = job.to_csv(
            tmp_path / "exports" / "report.csv",
            timeout=WAIT_TIMEOUT,
            poll_interval=POLL_INTERVAL,
        )

        assert target == tmp_path / "exports" / "report.csv"
        assert target.read_text(encoding="utf-8") == job.result_path.read_text(encoding="utf-8")

    def test_to_csv_accepts_a_directory(self, mock_env, tmp_path: Path) -> None:
        session = _dispatch(tmp_path)
        job = session.sql("SELECT 1")
        destination = tmp_path / "outbox"
        destination.mkdir()

        target = job.to_csv(destination, timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL)

        assert target.parent == destination
        assert target.name == job.result_path.name

    def test_a_cli_launched_csv_job_also_has_a_result(self, mock_env, tmp_path: Path) -> None:
        """Reading a Result is not notebook-specific (ADR-0010)."""
        cwd = tmp_path / "sql"
        _write_sql(cwd)
        session = _dispatch(cwd)

        job = session.launch(
            source="SqlFile", destination="Csv", sql="query.sql", table="from_cli"
        ).wait(timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL)

        assert job.result_path == cwd / "from_cli.csv"
        assert job.rows() == [{"id": "1", "value": "mock"}]


class TestCapacityBackpressure:
    def _fill_capacity(self, tmp_path: Path) -> None:
        for index in range(2):
            _seed_running_job(tmp_path, table_name=f"active{index}")

    def test_third_launch_is_refused_immediately(self, mock_env, tmp_path: Path) -> None:
        self._fill_capacity(tmp_path)

        with pytest.raises(OperationalError, match="capacity"):
            _dispatch(tmp_path).sql("SELECT 1")

    def test_wait_for_slot_retries_until_the_deadline(self, mock_env, tmp_path: Path) -> None:
        self._fill_capacity(tmp_path)
        started = time.monotonic()

        with pytest.raises(OperationalError, match="capacity"):
            _dispatch(tmp_path).sql("SELECT 1", wait_for_slot=0.6)

        assert time.monotonic() - started >= 0.6

    def test_wait_for_slot_does_not_retry_other_refusals(self, tmp_path: Path) -> None:
        command = _stub_cli(
            tmp_path,
            exit_code=4,
            stderr=json.dumps({"error": job_ops.MSG_KERBEROS_MISSING, "exit_code": 4}),
        )
        session = Dispatch(tmp_path, command=command, workspace=tmp_path / "ws")
        started = time.monotonic()

        with pytest.raises(OperationalError, match="Kerberos"):
            session.sql("SELECT 1", wait_for_slot=30)

        assert time.monotonic() - started < 10

    def test_negative_wait_for_slot_is_rejected(self, mock_env, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            _dispatch(tmp_path).sql("SELECT 1", wait_for_slot=-1)


class TestCleanup:
    def test_old_query_directories_are_removed(self, mock_env, tmp_path: Path) -> None:
        session = _dispatch(tmp_path)
        job = session.sql("SELECT 1").wait(timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL)
        query_dir = job.result_path.parent

        report = session.cleanup(older_than_days=0)

        assert report.directories == 1
        assert report.bytes_freed > 0
        assert not query_dir.exists()
        assert "directories=1" in repr(report)

    def test_recent_directories_survive_the_default_window(self, mock_env, tmp_path: Path) -> None:
        session = _dispatch(tmp_path)
        job = session.sql("SELECT 1").wait(timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL)

        report = session.cleanup()

        assert report.directories == 0
        assert job.result_path.exists()

    def test_cleanup_without_a_workspace_is_a_no_op(self, mock_env, tmp_path: Path) -> None:
        session = Dispatch(tmp_path, workspace=tmp_path / "never-created")

        assert session.cleanup().directories == 0

    def test_negative_window_is_rejected(self, mock_env, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            _dispatch(tmp_path).cleanup(older_than_days=-1)

    def test_default_window_matches_the_dashboard(self) -> None:
        assert notebook.DEFAULT_CLEANUP_DAYS == jobs.ACTIVE_WINDOW.total_seconds() / 86400


class TestWorkspaceLocation:
    def test_workspace_follows_the_data_root(self, mock_env, tmp_path: Path) -> None:
        session = _dispatch(tmp_path)

        assert (
            session.workspace == Path(os.environ["DISPATCH_DATA_ROOT"]) / ".dispatch" / "notebook"
        )

    def test_env_override_moves_the_workspace(self, tmp_path: Path) -> None:
        root = tmp_path / "other-root"
        session = Dispatch(tmp_path, env={"DISPATCH_DATA_ROOT": str(root)})

        assert session.workspace == root / ".dispatch" / "notebook"

    def test_explicit_workspace_wins(self, tmp_path: Path) -> None:
        assert Dispatch(tmp_path, workspace=tmp_path / "ws").workspace == tmp_path / "ws"

    def test_workspace_is_private(self, mock_env, tmp_path: Path) -> None:
        session = _dispatch(tmp_path)
        session.sql("SELECT 1")

        assert oct(session.workspace.stat().st_mode)[-3:] == "700"


class TestSessionConfiguration:
    def test_cwd_decides_where_csv_results_land(self, mock_env, tmp_path: Path) -> None:
        cwd = tmp_path / "elsewhere"
        _write_sql(cwd)
        session = Dispatch(str(cwd), command=[sys.executable, "-m", "dispatch"])

        job = session.launch(
            source="SqlFile", destination="Csv", sql="query.sql", table="located"
        ).wait(timeout=WAIT_TIMEOUT, poll_interval=POLL_INTERVAL)

        assert session.cwd == cwd
        assert job.csv_path == str(cwd / "located.csv")

    def test_home_relative_cwd_is_expanded(self, tmp_path: Path) -> None:
        assert Dispatch("~/sql").cwd == Path.home() / "sql"

    def test_repr_shows_the_working_directory(self, tmp_path: Path) -> None:
        assert str(tmp_path) in repr(Dispatch(tmp_path))


# =============================================================================
# Notebook rendering
# =============================================================================


class TestRendering:
    def test_job_repr_html_shows_id_and_state(self, mock_env, tmp_path: Path) -> None:
        job_id = _seed_pending_job(tmp_path)
        job = _dispatch(tmp_path).job(job_id)

        markup = job._repr_html_()

        assert job_id in markup
        assert "Pending" in markup
        assert repr(job) == f"<Job {job_id} Pending SqlFile->Csv>"

    def test_job_list_renders_a_table(self, mock_env, tmp_path: Path) -> None:
        job_id = _seed_pending_job(tmp_path)
        listed = _dispatch(tmp_path).jobs()

        assert job_id in listed._repr_html_()
        assert "No Jobs" in JobList()._repr_html_()

    def test_watch_html_escapes_log_output(self, mock_env, tmp_path: Path) -> None:
        job_id = _seed_pending_job(tmp_path)
        job = _dispatch(tmp_path).job(job_id)

        markup = notebook_display.watch_html(job, ["<script>alert(1)</script>"])

        assert "<script>" not in markup
        assert "&lt;script&gt;" in markup

    def test_live_view_without_ipython_prints_only_on_change(
        self, mock_env, tmp_path: Path, capsys
    ) -> None:
        job_id = _seed_pending_job(tmp_path)
        job = _dispatch(tmp_path).job(job_id)
        view = notebook_display.LiveView(rich=False)

        view.update(job, [])
        view.update(job, [])

        assert capsys.readouterr().out.count(job_id) == 1

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(None, "--"), (0, "0s"), (42.7, "42s"), (65, "1m 05s"), (3900, "1h 05m")],
    )
    def test_duration_formatting(self, seconds: float | None, expected: str) -> None:
        assert notebook_display.format_duration(seconds) == expected

    @pytest.mark.parametrize("value", ["2026-07-27T17:38:48Z", "20260727T173848Z"])
    def test_both_timestamp_shapes_parse(self, value: str) -> None:
        parsed = notebook._parse_timestamp(value)
        assert parsed is not None
        assert parsed.year == 2026

    def test_unknown_timestamp_shapes_are_ignored(self) -> None:
        assert notebook._parse_timestamp("not-a-time") is None
        assert notebook._parse_timestamp(None) is None

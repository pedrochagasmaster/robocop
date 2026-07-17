"""Tests for features added in Phase 2-4 hardening."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from pathlib import Path

import pytest

from dispatch import capacity, config, impala, manifest, telemetry
from dispatch.app import DispatchApp
from dispatch.screens.browser import BrowserScreen
from dispatch.screens.help import HelpScreen
from dispatch.screens.job_detail import JobDetailScreen
from dispatch.screens.new_job import NewJobScreen
from dispatch.screens.preview import PreviewScreen, sql_syntax


async def _collect_table_sizes(
    schema: str, table_names: list[str]
) -> list[tuple[str, impala.TableStats]]:
    return [item async for item in impala.iter_table_sizes(schema, table_names)]


# =============================================================================
# Preview SQL highlighting and scrolling
# =============================================================================


class TestPreviewHighlighting:
    def test_sql_syntax_uses_sql_lexer_with_line_numbers(self) -> None:
        syntax = sql_syntax("SELECT id FROM users WHERE active = 1")
        assert syntax.lexer is not None
        assert syntax.lexer.name.lower().startswith("sql")
        assert syntax.line_numbers is True

    def test_sql_syntax_renders_keywords_with_style(self) -> None:
        from rich.console import Console

        console = Console(force_terminal=True, color_system="truecolor", width=100)
        with console.capture() as capture:
            console.print(sql_syntax("SELECT 1\nFROM dual"))
        output = capture.get()
        assert "SELECT" in output
        assert "\x1b[" in output, "Expected ANSI styling from the SQL lexer"


# =============================================================================
# Browser DESCRIBE parsing
# =============================================================================


class TestBrowserDescribeParsing:
    def test_parse_describe_pipe_delimited(self) -> None:
        raw = "id|string|primary key\nname|varchar|user name\nage|int|"
        columns = BrowserScreen._parse_describe(raw)
        assert len(columns) == 3
        assert columns[0] == {"name": "id", "type": "string", "comment": "primary key"}
        assert columns[1]["name"] == "name"
        assert columns[2]["comment"] == ""

    def test_parse_describe_empty_input(self) -> None:
        assert BrowserScreen._parse_describe("") == []

    def test_parse_describe_skips_comments(self) -> None:
        raw = "# Header line\nid|int|pk"
        columns = BrowserScreen._parse_describe(raw)
        assert len(columns) == 1

    def test_browser_surfaces_typed_capacity_error_in_status_and_notification(
        self, mock_env_with_config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        failure = capacity.CapacityLedgerError("cannot read capacity ledger")

        async def fail_show_tables(_schema: str, _pattern: str = "*") -> list[str]:
            raise failure

        monkeypatch.setattr(impala, "show_tables", fail_show_tables)

        async def run() -> tuple[str, list[tuple[str, str | None]]]:
            app = DispatchApp()
            async with app.run_test(size=(120, 40)) as pilot:
                screen = BrowserScreen(auto_load=False)
                app.push_screen(screen)
                await pilot.pause()
                notifications: list[tuple[str, str | None]] = []
                monkeypatch.setattr(
                    screen,
                    "notify",
                    lambda message, severity=None, **_kwargs: notifications.append(
                        (str(message), severity)
                    ),
                )

                await screen.action_show_tables()
                status = str(screen.query_one("#browser-action-status").render())
                return status, notifications

        status, notifications = asyncio.run(run())

        assert "cannot read capacity ledger" in status
        assert ("SHOW TABLES failed: cannot read capacity ledger", "error") in notifications


class TestDataSizeFormatting:
    def test_parse_data_size_units(self) -> None:
        from dispatch.formatting import parse_data_size

        assert parse_data_size("0B") == 0
        assert parse_data_size("12.60MB") == 13_212_057
        assert parse_data_size("370.45MB") == 388_444_979
        assert parse_data_size("1.25GB") == 1_342_177_280

    def test_format_data_size_is_consistent(self) -> None:
        from dispatch.formatting import format_data_size

        assert format_data_size(0) == "0 B"
        assert format_data_size(13_212_057) == "12.6 MB"
        assert format_data_size(1_342_177_280) == "1.2 GB"
        assert format_data_size(None) == "—"


class TestImpalaTableStatsParsing:
    def test_parse_table_stats_output_sums_partitions(self) -> None:
        from dispatch.impala import parse_table_stats_output

        raw = (
            "#Rows|#Files|Size|Bytes Cached|Format|Incremental stats\n"
            "-1|1|12.60MB|NOT CACHED|TEXT|false\n"
            "-1|2|1.25GB|NOT CACHED|PARQUET|false\n"
        )
        stats = parse_table_stats_output(raw)
        assert stats.size_bytes == 13_212_057 + 1_342_177_280
        assert stats.size_display == "1.3 GB"

    @pytest.mark.parametrize("operation", ["show_tables", "table_stats", "describe", "drop"])
    def test_every_metadata_command_holds_a_shared_capacity_lease(
        self,
        operation: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))

        async def run() -> None:
            entered = asyncio.Event()
            release = asyncio.Event()

            async def fake_run_exec(*_argv: str, **_kwargs: object) -> tuple[int, str, str]:
                entered.set()
                await release.wait()
                if operation == "show_tables":
                    return 0, "name\none\n", ""
                if operation == "table_stats":
                    return 0, "#Rows|#Files|Size\n1|1|42B\n", ""
                if operation == "describe":
                    return 0, "name|type|comment\nid|int|\n", ""
                return 0, "", ""

            monkeypatch.setattr("dispatch.process.run_exec", fake_run_exec)
            existing = capacity.try_acquire_metadata("existing")
            try:
                if operation == "show_tables":
                    command = impala.show_tables("aa_enc")
                elif operation == "table_stats":
                    command = impala.table_stats("aa_enc.one")
                elif operation == "describe":
                    command = impala.describe_table("aa_enc.one")
                else:
                    command = impala.drop_table("aa_enc.one")
                task = asyncio.create_task(command)
                await asyncio.wait_for(entered.wait(), timeout=1)
                with pytest.raises(capacity.CapacityBusy):
                    capacity.try_acquire_metadata("probe")
                release.set()
                await task
            finally:
                existing.release()

        asyncio.run(run())

    def test_metadata_fails_fast_before_starting_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))
        subprocess_called = False

        async def fake_run_exec(*_argv: str, **_kwargs: object) -> tuple[int, str, str]:
            nonlocal subprocess_called
            subprocess_called = True
            return 0, "", ""

        monkeypatch.setattr("dispatch.process.run_exec", fake_run_exec)
        first = capacity.try_acquire_metadata("first")
        second = capacity.try_acquire_metadata("second")
        try:
            with pytest.raises(capacity.CapacityBusy):
                asyncio.run(impala.show_tables("aa_enc"))
        finally:
            first.release()
            second.release()
        assert subprocess_called is False

    def test_cancelled_metadata_acquisition_releases_threaded_lease(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))
        acquired = threading.Event()
        allow_handoff = threading.Event()
        handed_off = threading.Event()
        original_acquire = capacity.try_acquire_metadata

        def delayed_acquire(operation: str) -> capacity.MetadataLease:
            lease = original_acquire(operation)
            acquired.set()
            assert allow_handoff.wait(2)
            handed_off.set()
            return lease

        async def unexpected_run_exec(*_argv: str, **_kwargs: object) -> tuple[int, str, str]:
            raise AssertionError("cancelled acquisition must not run impala-shell")

        monkeypatch.setattr(capacity, "try_acquire_metadata", delayed_acquire)
        monkeypatch.setattr("dispatch.process.run_exec", unexpected_run_exec)

        async def run() -> None:
            task = asyncio.create_task(impala.show_tables("aa_enc"))
            assert await asyncio.to_thread(acquired.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            allow_handoff.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await asyncio.to_thread(handed_off.wait, 2)

        asyncio.run(run())

        first = original_acquire("probe-one")
        second = original_acquire("probe-two")
        first.release()
        second.release()

    def test_stats_map_capacity_busy_to_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))

        async def unexpected_run_exec(*_argv: str, **_kwargs: object) -> tuple[int, str, str]:
            raise AssertionError("busy stats must not start impala-shell")

        monkeypatch.setattr("dispatch.process.run_exec", unexpected_run_exec)
        first = capacity.try_acquire_metadata("first")
        second = capacity.try_acquire_metadata("second")
        try:
            results = asyncio.run(_collect_table_sizes("aa_enc", ["one", "two"]))
        finally:
            first.release()
            second.release()

        assert [name for name, _stats in results] == ["one", "two"]
        assert all(stats == impala.TableStats(None, "—") for _name, stats in results)

    def test_stats_map_impala_query_failure_to_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))

        async def failed_run_exec(*_argv: str, **_kwargs: object) -> tuple[int, str, str]:
            return 1, "", "stats unavailable"

        monkeypatch.setattr("dispatch.process.run_exec", failed_run_exec)

        results = asyncio.run(_collect_table_sizes("aa_enc", ["broken"]))

        assert results == [("broken", impala.TableStats(None, "—"))]

    def test_impala_execution_failures_use_dedicated_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))

        async def failed_run_exec(*_argv: str, **_kwargs: object) -> tuple[int, str, str]:
            return 1, "", "query rejected"

        monkeypatch.setattr("dispatch.process.run_exec", failed_run_exec)

        with pytest.raises(impala.ImpalaExecutionError, match="query rejected"):
            asyncio.run(impala.table_stats("aa_enc.broken"))

    def test_stats_do_not_hide_arbitrary_runtime_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))

        async def broken_subprocess_adapter(*_argv: str, **_kwargs: object) -> tuple[int, str, str]:
            raise RuntimeError("subprocess adapter bug")

        monkeypatch.setattr("dispatch.process.run_exec", broken_subprocess_adapter)

        with pytest.raises(RuntimeError, match="subprocess adapter bug"):
            asyncio.run(_collect_table_sizes("aa_enc", ["broken"]))

    def test_stats_surface_capacity_ledger_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))
        lease = capacity.try_acquire_metadata("initialize")
        lease.release()
        (config.dispatch_home() / "capacity.json").write_text("{not-json", encoding="utf-8")

        with pytest.raises(capacity.CapacityLedgerError):
            asyncio.run(_collect_table_sizes("aa_enc", ["one"]))

    def test_process_local_query_ledger_api_is_removed(self) -> None:
        assert not hasattr(impala, "QueryLedger")
        assert not hasattr(impala, "query_ledger")
        assert not hasattr(impala, "reset_query_ledger_for_tests")


# =============================================================================
# Job Detail elapsed time
# =============================================================================


class TestJobDetailElapsed:
    def test_format_elapsed_running_job(self) -> None:
        from datetime import datetime, timezone

        from dispatch.formatting import format_elapsed

        now = datetime.now(timezone.utc)
        started = (now - __import__("datetime").timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        item = {"state": "Running", "started_at": started}
        result = format_elapsed(item)
        assert "5m" in result or "4m" in result

    def test_format_elapsed_no_started_at(self) -> None:
        item = {"state": "Running", "started_at": None}
        from dispatch.formatting import format_elapsed

        assert format_elapsed(item) == "--"

    def test_format_elapsed_succeeded_job(self) -> None:
        from dispatch.formatting import format_elapsed

        item = {
            "state": "Succeeded",
            "started_at": "2026-05-16T10:00:00Z",
            "finished_at": "2026-05-16T10:45:00Z",
        }
        result = format_elapsed(item)
        assert "45m" in result

    def test_style_log_line_dims_timestamp(self) -> None:
        line = "[2026-05-16 10:00:00] Starting job"
        styled = JobDetailScreen._style_log_line(line)
        assert "[dim]" in styled
        assert "Starting job" in styled

    def test_style_log_line_no_timestamp_unchanged(self) -> None:
        line = "plain log line"
        assert JobDetailScreen._style_log_line(line) == "plain log line"


# =============================================================================
# Config form defaults persistence
# =============================================================================


class TestFormDefaults:
    def test_read_form_defaults_missing_file(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))
        result = config.read_form_defaults()
        assert result == {}

    def test_save_and_read_form_defaults(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))
        dispatch_home = tmp_path / ".dispatch"
        dispatch_home.mkdir(parents=True)
        (dispatch_home / "config.json").write_text("{}", encoding="utf-8")

        config.save_form_defaults({"schema": "dw_test", "email": "a@b.com"})
        result = config.read_form_defaults()
        assert result["schema"] == "dw_test"
        assert result["email"] == "a@b.com"

    def test_save_form_defaults_preserves_existing_config(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("DISPATCH_DATA_ROOT", str(tmp_path))
        dispatch_home = tmp_path / ".dispatch"
        dispatch_home.mkdir(parents=True)
        (dispatch_home / "config.json").write_text(
            json.dumps({"to_email": "existing@example.com"}),
            encoding="utf-8",
        )

        config.save_form_defaults({"schema": "dw_new"})
        cfg = config.read_config()
        assert cfg["to_email"] == "existing@example.com"
        assert cfg["form_defaults"]["schema"] == "dw_new"


# =============================================================================
# Kerberos graceful handling
# =============================================================================


class TestKerberosGraceful:
    def test_parse_ttl_garbage_returns_none(self) -> None:
        from dispatch.kerberos import parse_ttl_seconds

        assert parse_ttl_seconds("completely invalid") is None

    def test_parse_ttl_empty_returns_none(self) -> None:
        from dispatch.kerberos import parse_ttl_seconds

        assert parse_ttl_seconds("") is None


# =============================================================================
# Dashboard display ID
# =============================================================================


class TestDashboardDisplayId:
    def test_display_id_strips_date_prefix(self) -> None:
        from dispatch.formatting import format_job_id

        job_id = "20260516T100000Z_aabbcc"
        result = format_job_id(job_id)
        assert "aabbcc" in result
        assert "20260516" not in result

    def test_display_id_short_id_unchanged(self) -> None:
        from dispatch.formatting import format_job_id

        assert format_job_id("short") == "short"


# =============================================================================
# Help screen
# =============================================================================


class TestHelpScreen:
    def test_help_screen_renders(self) -> None:
        async def run() -> None:
            app = DispatchApp()
            async with app.run_test(size=(100, 40)) as pilot:
                app.push_screen(HelpScreen())
                await pilot.pause()
                body = app.screen.query_one("#help-body")
                text = str(body.render())
                assert "Overview" in text
                assert "New Job" in text
                assert "Browser" in text

        asyncio.run(run())


# =============================================================================
# New Job Kerberos launch gating
# =============================================================================


class TestNewJobKerberosGating:
    def test_launch_button_disabled_when_kerberos_missing(
        self, mock_env_with_config, monkeypatch
    ) -> None:
        async def fake_ttl() -> None:
            return None

        monkeypatch.setattr("dispatch.kerberos.ticket_ttl_seconds", fake_ttl)

        async def run() -> None:
            app = DispatchApp()
            async with app.run_test(size=(140, 50)) as pilot:
                screen = NewJobScreen(Path.cwd())
                app.push_screen(screen)
                await pilot.pause()
                launch_btn = screen.query_one("#launch")
                assert launch_btn.disabled is True

        asyncio.run(run())

    def test_launch_button_enabled_when_kerberos_healthy(
        self, mock_env_with_config, monkeypatch
    ) -> None:
        async def fake_ttl() -> int:
            return 7200

        monkeypatch.setattr("dispatch.kerberos.ticket_ttl_seconds", fake_ttl)

        async def run() -> None:
            app = DispatchApp()
            async with app.run_test(size=(140, 50)) as pilot:
                screen = NewJobScreen(Path.cwd())
                app.push_screen(screen)
                await pilot.pause()
                launch_btn = screen.query_one("#launch")
                assert launch_btn.disabled is False

        asyncio.run(run())

    def test_low_kerberos_ttl_disables_launch_and_explains_issue(
        self, mock_env_with_config, monkeypatch, tmp_path: Path
    ) -> None:
        (tmp_path / "query.sql").write_text("SELECT 1", encoding="utf-8")

        async def fake_ttl() -> int:
            return 299

        monkeypatch.setattr("dispatch.kerberos.ticket_ttl_seconds", fake_ttl)

        async def run() -> None:
            app = DispatchApp()
            async with app.run_test(size=(140, 50)) as pilot:
                screen = NewJobScreen(tmp_path)
                app.push_screen(screen)
                await pilot.pause()
                launch_btn = screen.query_one("#launch")
                warning = str(screen.query_one("#warning-text").render())
                summary = str(screen.query_one("#validation-summary").render())
                assert launch_btn.disabled is True
            assert "Kerberos TTL low" in warning
            assert "Kerberos ticket TTL is under 5 minutes" in summary

        asyncio.run(run())

    def test_kinit_action_runs_interactive_kinit_and_refreshes_ttl(
        self, mock_env_with_config, monkeypatch
    ) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_run_interactive(*argv: str) -> int:
            calls.append(argv)
            return 0

        async def fake_ttl() -> int:
            return 7200

        monkeypatch.setattr("dispatch.process.run_interactive", fake_run_interactive)
        monkeypatch.setattr("dispatch.kerberos.ticket_ttl_seconds", fake_ttl)

        async def run() -> None:
            app = DispatchApp()
            app.suspend = contextlib.nullcontext  # type: ignore[method-assign]
            async with app.run_test(size=(140, 50)) as pilot:
                screen = NewJobScreen(Path.cwd())
                app.push_screen(screen)
                await pilot.pause()
                await screen.action_kinit()
                await pilot.pause()

                assert calls == [("kinit",)]
                assert screen.kerberos_ttl == 7200
                assert app.kerberos_ttl == 7200
                assert screen.query_one("#launch").disabled is False

        asyncio.run(run())


# =============================================================================
# New Job inline validation
# =============================================================================


class TestNewJobInlineValidation:
    def test_validation_summary_is_debounced_during_typing(
        self, mock_env_with_config, monkeypatch, tmp_path: Path
    ) -> None:
        sql_path = tmp_path / "query.sql"
        sql_path.write_text("SELECT 1", encoding="utf-8")

        async def fake_ttl() -> int:
            return 7200

        monkeypatch.setattr("dispatch.kerberos.ticket_ttl_seconds", fake_ttl)

        async def run() -> None:
            app = DispatchApp()
            async with app.run_test(size=(140, 50)) as pilot:
                screen = NewJobScreen(tmp_path, prefill={"sql_file": str(sql_path)})
                app.push_screen(screen)
                await pilot.pause(0.5)

                calls = 0
                original = screen._update_validation_summary

                def counting_update() -> None:
                    nonlocal calls
                    calls += 1
                    original()

                screen._update_validation_summary = counting_update  # type: ignore[method-assign]
                screen.query_one("#table-name-suffix").value = "dispatch_result_2"
                await pilot.pause(0.05)

                assert calls == 0

                await pilot.pause(0.3)
                assert calls == 1

        asyncio.run(run())

    def test_inline_validation_shows_kerberos_status(
        self, mock_env_with_config, monkeypatch
    ) -> None:
        async def fake_ttl() -> int:
            return 7200

        monkeypatch.setattr("dispatch.kerberos.ticket_ttl_seconds", fake_ttl)

        async def run() -> None:
            app = DispatchApp()
            async with app.run_test(size=(140, 50)) as pilot:
                screen = NewJobScreen(Path.cwd())
                app.push_screen(screen)
                await pilot.pause()
                warning = str(screen.query_one("#warning-text").render())
                assert "Kerberos" in warning

        asyncio.run(run())

    def test_table_destination_rejects_unsafe_table_name(
        self, mock_env_with_config, monkeypatch, tmp_path: Path
    ) -> None:
        sql_path = tmp_path / "query.sql"
        sql_path.write_text("SELECT 1", encoding="utf-8")

        async def fake_ttl() -> int:
            return 7200

        monkeypatch.setattr("dispatch.kerberos.ticket_ttl_seconds", fake_ttl)

        async def run() -> None:
            app = DispatchApp()
            prefill = {
                "source_type": "SqlFile",
                "dest_type": "Table+Csv",
                "sql_file": str(sql_path),
                "schema": "aa_enc",
                "table_name": "../escape",
            }
            async with app.run_test(size=(140, 50)) as pilot:
                screen = NewJobScreen(tmp_path, prefill=prefill)
                app.push_screen(screen)
                await pilot.pause(0.5)

                issues = screen._validation_issues()
                assert "Table name suffix must be a plain Impala identifier" in issues
                assert screen._validate() == "Table name suffix must be a plain Impala identifier"

        asyncio.run(run())

    def test_existing_table_source_requires_safe_full_table(
        self, mock_env_with_config, monkeypatch, tmp_path: Path
    ) -> None:
        async def fake_ttl() -> int:
            return 7200

        monkeypatch.setattr("dispatch.kerberos.ticket_ttl_seconds", fake_ttl)

        async def run() -> None:
            app = DispatchApp()
            prefill = {
                "source_type": "ExistingTable",
                "dest_type": "Csv",
                "existing_table": "schema.table.extra",
            }
            async with app.run_test(size=(140, 50)) as pilot:
                screen = NewJobScreen(tmp_path, prefill=prefill)
                app.push_screen(screen)
                await pilot.pause(0.5)

                expected = "Existing table must be schema.table using plain Impala identifiers"
                assert expected in screen._validation_issues()
                assert screen._validate() == expected

        asyncio.run(run())

    def test_csv_destination_uses_resolved_launch_directory(
        self, mock_env_with_config, monkeypatch, tmp_path: Path
    ) -> None:
        sql_path = tmp_path / "query.sql"
        sql_path.write_text("SELECT 1", encoding="utf-8")
        (tmp_path / "nested").mkdir()
        launch_cwd = tmp_path / "nested" / ".."

        async def fake_ttl() -> int:
            return 7200

        monkeypatch.setattr("dispatch.kerberos.ticket_ttl_seconds", fake_ttl)

        async def run() -> None:
            app = DispatchApp()
            prefill = {
                "source_type": "SqlFile",
                "dest_type": "Table+Csv",
                "sql_file": str(sql_path),
                "schema": "aa_enc",
                "table_name": "dispatch_smoke_1",
            }
            async with app.run_test(size=(140, 50)) as pilot:
                screen = NewJobScreen(launch_cwd, prefill=prefill)
                app.push_screen(screen)
                await pilot.pause(0.5)

                _source, destination = screen._source_destination()
                eid = config.current_user()
                assert Path(destination["csv_path"]) == (
                    tmp_path.resolve() / f"{eid}_dispatch_smoke_1.csv"
                )
                assert destination["table_name"] == f"{eid}_dispatch_smoke_1"

        asyncio.run(run())

    def test_csv_destination_validates_computed_filename_stem(
        self, mock_env_with_config, monkeypatch, tmp_path: Path
    ) -> None:
        sql_path = tmp_path / "query.sql"
        sql_path.write_text("SELECT 1", encoding="utf-8")

        async def fake_ttl() -> int:
            return 7200

        monkeypatch.setattr("dispatch.kerberos.ticket_ttl_seconds", fake_ttl)

        async def run() -> None:
            app = DispatchApp()
            prefill = {
                "source_type": "SqlFile",
                "dest_type": "Csv",
                "sql_file": str(sql_path),
                "table_name": r"..\escape",
            }
            async with app.run_test(size=(140, 50)) as pilot:
                screen = NewJobScreen(tmp_path, prefill=prefill)
                app.push_screen(screen)
                await pilot.pause(0.5)

                expected = "Table name must be a safe CSV filename stem"
                assert expected in screen._validation_issues()
                assert screen._validate() == expected

        asyncio.run(run())

    def test_launch_runner_failure_marks_manifest_failed(
        self, mock_env_with_config, monkeypatch, tmp_path: Path
    ) -> None:
        sql_path = tmp_path / "query.sql"
        sql_path.write_text("SELECT 1", encoding="utf-8")

        async def fake_ttl() -> int:
            return 7200

        async def fail_launch(_job_dir: Path) -> int:
            raise OSError("nohup unavailable")

        monkeypatch.setattr("dispatch.kerberos.ticket_ttl_seconds", fake_ttl)
        monkeypatch.setattr("dispatch.process.launch_runner", fail_launch)

        async def run() -> None:
            app = DispatchApp()
            prefill = {
                "source_type": "SqlFile",
                "dest_type": "Csv",
                "sql_file": str(sql_path),
                "table_name": "dispatch_spawn_failure",
            }
            async with app.run_test(size=(140, 50)) as pilot:
                screen = NewJobScreen(tmp_path, prefill=prefill)
                app.push_screen(screen)
                await pilot.pause(0.5)
                monkeypatch.setattr(screen, "_confirm_launch", _async_true)

                await screen._launch_flow()
                await pilot.pause()

        asyncio.run(run())

        manifests = list((tmp_path / "data" / ".dispatch" / "jobs").glob("*/manifest.json"))
        assert len(manifests) == 1
        final = manifest.load(manifests[0])
        assert final["state"] == "Failed"
        assert final["exit_code"] == -1
        assert final["finished_at"] is not None
        assert telemetry.flush(timeout=1)
        events = [
            json.loads(line)
            for line in telemetry.private_events_path().read_text(encoding="utf-8").splitlines()
        ]
        assert any(
            event["event"] == "job_launched" and event["props"]["job_id"] == final["id"]
            for event in events
        )


# =============================================================================
# Preview screen
# =============================================================================


class TestPreviewScreen:
    def test_preview_stores_source_and_dest_types(self) -> None:
        screen = PreviewScreen(
            "SQL Preview",
            "SELECT 1",
            schema="dw",
            table="result",
            source_type="SqlFile",
            dest_type="Table",
        )
        assert screen.source_type == "SqlFile"
        assert screen.dest_type == "Table"

    def test_preview_body_is_scrollable_richlog(self) -> None:
        from textual.widgets import RichLog

        async def run() -> None:
            app = DispatchApp()
            async with app.run_test(size=(120, 40)) as pilot:
                screen = PreviewScreen("Test", "SELECT 1\n" * 100, schema="dw", table="t")
                app.push_screen(screen)
                await pilot.pause()
                log = screen.query_one("#preview-body", RichLog)
                assert log is not None

        asyncio.run(run())


async def _async_true(*_args, **_kwargs) -> bool:
    return True

"""Tests for features added in Phase 2-4 hardening."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

from dispatch import config, impala, manifest, telemetry
from dispatch.app import DispatchApp
from dispatch.screens.browser import BrowserScreen
from dispatch.screens.help import HelpScreen
from dispatch.screens.job_detail import JobDetailScreen
from dispatch.screens.new_job import NewJobScreen
from dispatch.screens.preview import PreviewScreen, sql_syntax

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

    def test_size_fetch_concurrency_accounts_for_other_queries(self, monkeypatch) -> None:
        """Size fetch uses max(0, 2 - running) slots across TUI + Running jobs."""
        from dispatch import impala

        impala.reset_query_ledger_for_tests()
        monkeypatch.setattr(impala, "external_running_query_count", lambda: 0)
        assert impala.size_fetch_concurrency() == 2

        monkeypatch.setattr(impala, "external_running_query_count", lambda: 1)
        assert impala.size_fetch_concurrency() == 1

        monkeypatch.setattr(impala, "external_running_query_count", lambda: 2)
        assert impala.size_fetch_concurrency() == 0

        monkeypatch.setattr(impala, "external_running_query_count", lambda: 0)

        async def hold_one_slot() -> None:
            async with impala.query_ledger.occupy():
                assert impala.size_fetch_concurrency() == 1
                await asyncio.sleep(0)

        asyncio.run(hold_one_slot())
        assert impala.size_fetch_concurrency() == 2

    def test_iter_table_sizes_runs_two_at_a_time_when_idle(self, monkeypatch) -> None:
        """When no other queries are running, size fetch may use both slots."""
        from dispatch import impala

        impala.reset_query_ledger_for_tests()
        monkeypatch.setattr(impala, "external_running_query_count", lambda: 0)
        in_flight = 0
        max_in_flight = 0
        release = asyncio.Event()

        async def fake_table_stats(full_table: str) -> impala.TableStats:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await release.wait()
            in_flight -= 1
            return impala.TableStats(size_bytes=42, size_display="42 B")

        monkeypatch.setattr(impala, "table_stats", fake_table_stats)

        async def run() -> list[tuple[str, impala.TableStats]]:
            results: list[tuple[str, impala.TableStats]] = []

            async def consume() -> None:
                async for item in impala.iter_table_sizes("aa_enc", ["one", "two", "three"]):
                    results.append(item)

            task = asyncio.create_task(consume())
            for _ in range(50):
                if max_in_flight >= 2:
                    break
                await asyncio.sleep(0.01)
            assert max_in_flight == 2
            release.set()
            await task
            return results

        results = asyncio.run(run())
        assert [name for name, _ in results] == ["one", "two", "three"]
        assert all(stats.size_bytes == 42 for _name, stats in results)

    def test_iter_table_sizes_skips_when_no_slots_available(self, monkeypatch) -> None:
        """With 2 queries already running, sizes stay unavailable (not queued)."""
        from dispatch import impala

        impala.reset_query_ledger_for_tests()
        monkeypatch.setattr(impala, "external_running_query_count", lambda: 2)
        calls: list[str] = []

        async def fake_table_stats(full_table: str) -> impala.TableStats:
            calls.append(full_table)
            return impala.TableStats(size_bytes=42, size_display="42 B")

        monkeypatch.setattr(impala, "table_stats", fake_table_stats)

        async def run() -> list[tuple[str, impala.TableStats]]:
            return [item async for item in impala.iter_table_sizes("aa_enc", ["one", "two"])]

        results = asyncio.run(run())
        assert calls == []
        assert [name for name, _ in results] == ["one", "two"]
        assert all(stats.size_bytes is None for _name, stats in results)
        assert all(stats.size_display == "—" for _name, stats in results)

    def test_iter_table_sizes_uses_one_slot_when_one_query_running(self, monkeypatch) -> None:
        """One external/TUI query leaves a single size-fetch slot."""
        from dispatch import impala

        impala.reset_query_ledger_for_tests()
        monkeypatch.setattr(impala, "external_running_query_count", lambda: 1)
        in_flight = 0
        max_in_flight = 0

        async def fake_table_stats(full_table: str) -> impala.TableStats:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0)
            in_flight -= 1
            if full_table.endswith("broken"):
                raise RuntimeError("stats unavailable")
            return impala.TableStats(size_bytes=42, size_display="42 B")

        monkeypatch.setattr(impala, "table_stats", fake_table_stats)

        async def run() -> list[tuple[str, impala.TableStats]]:
            return [
                item async for item in impala.iter_table_sizes("aa_enc", ["one", "broken", "two"])
            ]

        results = asyncio.run(run())
        assert max_in_flight == 1
        assert [name for name, _stats in results] == ["one", "broken", "two"]
        assert results[0][1].size_bytes == 42
        assert results[1][1].size_bytes is None
        assert results[1][1].size_display == "—"
        assert results[2][1].size_bytes == 42

    def test_occupy_never_exceeds_two_under_contention(self, monkeypatch) -> None:
        """Overlapping occupy/try_occupy calls never grant more than 2 total slots."""
        from dispatch import impala

        impala.reset_query_ledger_for_tests()
        monkeypatch.setattr(impala, "external_running_query_count", lambda: 0)
        peaks: list[int] = []

        async def hold(duration: float, *, try_slot: bool = False) -> None:
            if try_slot:
                async with impala.query_ledger.try_occupy() as acquired:
                    if not acquired:
                        return
                    peaks.append(impala.query_ledger.in_flight)
                    await asyncio.sleep(duration)
            else:
                async with impala.query_ledger.occupy():
                    peaks.append(impala.query_ledger.in_flight)
                    await asyncio.sleep(duration)

        async def run() -> None:
            # Flood with overlapping interactive + size-style acquires.
            await asyncio.gather(
                hold(0.05),
                hold(0.05),
                hold(0.05),
                hold(0.05, try_slot=True),
                hold(0.05, try_slot=True),
                hold(0.05, try_slot=True),
                hold(0.05),
                hold(0.05, try_slot=True),
            )

        asyncio.run(run())
        assert peaks
        assert max(peaks) <= 2
        assert impala.query_ledger.in_flight == 0
        assert impala.query_ledger.peak_total <= 2

    def test_occupy_counts_running_jobs_toward_cap(self, monkeypatch) -> None:
        """Interactive query waits when Running Jobs already fill both slots."""
        from dispatch import impala

        impala.reset_query_ledger_for_tests()
        external = {"n": 2}
        monkeypatch.setattr(impala, "external_running_query_count", lambda: external["n"])
        entered = asyncio.Event()

        async def run() -> None:
            async def waiter() -> None:
                async with impala.query_ledger.occupy():
                    entered.set()

            task = asyncio.create_task(waiter())
            await asyncio.sleep(0.3)
            assert not entered.is_set()
            assert impala.query_ledger.in_flight == 0
            external["n"] = 1
            await asyncio.wait_for(entered.wait(), timeout=2.0)
            await task

        asyncio.run(run())
        assert impala.query_ledger.peak_total <= 2


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

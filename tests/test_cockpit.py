"""Regression tests for the supervision-cockpit wireframe and file-first launch."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from textual.widgets import Collapsible, DataTable, Input, Static

from dispatch import jobs, manifest
from dispatch.app import DispatchApp
from dispatch.screens.job_detail import JobDetailScreen
from dispatch.screens.new_job import NewJobScreen
from dispatch.version import __version__


def _seed_job(
    jobs_dir: Path,
    job_id: str,
    state: str,
    *,
    pid: int | None = None,
    log_lines: list[str] | None = None,
) -> str:
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    finished = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if state not in ("Running", "Pending")
        else None
    )
    manifest.write(
        job_dir / "manifest.json",
        {
            "schema_version": 1,
            "id": job_id,
            "tool": "dispatch",
            "user": "testuser",
            "source": {"type": "SqlFile", "sql_path_at_launch": f"/tmp/{job_id}.sql"},
            "destination": {
                "type": "Csv",
                "schema": "aa_enc",
                "table_name": f"t_{job_id[-6:]}",
                "csv_path": "/tmp/t.csv",
            },
            "params": {},
            "orchestrator_calls": [{"script": "download_to_csv.py", "argv": ["python3", "x.py"]}],
            "state": state,  # type: ignore[typeddict-item]
            "pid": pid,
            "started_at": "2026-05-20T12:00:00Z",
            "finished_at": finished,
            "exit_code": 0 if state == "Succeeded" else None,
        },
    )
    (job_dir / "run.log").write_text(
        "\n".join(log_lines or ["[2026-05-20 12:00:01] started"]) + "\n",
        encoding="utf-8",
    )
    return job_id


def test_cockpit_merges_running_and_recent_with_running_first(
    mock_env_with_config, monkeypatch
) -> None:
    monkeypatch.setattr(jobs, "pid_is_alive", lambda pid: True)
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    jobs_dir = data_root / ".dispatch" / "jobs"
    # Seed so a finished job sorts *before* the running one by id; the cockpit
    # must still pin the running job to the top.
    _seed_job(jobs_dir, "20260520T130000Z_done01", "Succeeded")
    running = _seed_job(jobs_dir, "20260520T120000Z_run001", "Running", pid=4242)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            table = app.screen.query_one("#jobs-table", DataTable)
            assert table.row_count == 2
            first_key = table.coordinate_to_cell_key((0, 0)).row_key.value
            assert first_key == running

    asyncio.run(run())


def test_cockpit_status_strip_replaces_stat_cards(mock_env_with_config) -> None:
    _seed = None

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            strip = str(app.screen.query_one("#status-strip", Static).render())
            assert "RUNNING" in strip
            assert "FINISHED" in strip
            assert "KERBEROS" in strip
            assert list(app.screen.query(".stat-card")) == []

    asyncio.run(run())


def test_cockpit_empty_state_and_startup_event_are_visible(mock_env_with_config) -> None:
    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            empty_state = app.screen.query_one("#jobs-empty")
            empty_text = empty_state.query_one(Static)
            event_trail = str(app.screen.query_one("#event-trail", Static).render())

            assert empty_state.display is True
            assert "No jobs in the last 7 days" in str(empty_text.render())
            assert "press N to launch one" in str(empty_text.render())
            assert "dispatch started" in event_trail

    asyncio.run(run())


def test_app_startup_logs_and_reports_version_mismatch(mock_env_with_config) -> None:
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    dispatch_home = data_root / ".dispatch"
    (dispatch_home / "installed_version").write_text("0.9.0", encoding="utf-8")

    app = DispatchApp()
    warning = app._build_version_warning()
    for handler in logging.getLogger("dispatch").handlers:
        handler.flush()

    log_text = (dispatch_home / "dispatch.log").read_text(encoding="utf-8")
    assert f"Version mismatch: installed 0.9.0, running {__version__}. Run install.sh." == warning
    assert f"Dispatch {__version__} starting" in log_text


def test_cockpit_status_strip_renders_kerberos_state_matrix(mock_env_with_config) -> None:
    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            app.screen.kerberos_ttl = None  # type: ignore[attr-defined]
            app.screen._update_status_strip(0, 0, 0)  # type: ignore[attr-defined]
            missing = str(app.screen.query_one("#status-strip", Static).render())
            assert "KERBEROS" in missing
            assert "MISSING" in missing

            app.screen.kerberos_ttl = 299  # type: ignore[attr-defined]
            app.screen._update_status_strip(0, 0, 0)  # type: ignore[attr-defined]
            low = str(app.screen.query_one("#status-strip", Static).render())
            assert "4m" in low or "299s" in low

            app.screen.kerberos_ttl = 7200  # type: ignore[attr-defined]
            app.screen._update_status_strip(0, 0, 0)  # type: ignore[attr-defined]
            healthy = str(app.screen.query_one("#status-strip", Static).render())
            assert "2h" in healthy

    asyncio.run(run())


def test_cockpit_detail_pane_tails_selected_job_log(mock_env_with_config) -> None:
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    jobs_dir = data_root / ".dispatch" / "jobs"
    _seed_job(
        jobs_dir,
        "20260520T120000Z_tail01",
        "Succeeded",
        log_lines=["[2026-05-20 12:00:01] starting", "[2026-05-20 12:00:09] 1,284,003 rows"],
    )

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.5)
            detail_log = str(app.screen.query_one("#detail-log", Static).render())
            assert "1,284,003 rows" in detail_log
            title = str(app.screen.query_one("#detail-title", Static).render())
            assert "tail01" in title
            assert "SUCCEEDED" in title

    asyncio.run(run())


def test_dashboard_refresh_in_flight_skips_overlapping_tick(
    mock_env_with_config, monkeypatch
) -> None:
    calls = 0
    hold = False
    entered = threading.Event()
    release = threading.Event()

    def slow_active_jobs() -> list[dict]:
        nonlocal calls
        calls += 1
        if hold:
            entered.set()
            assert release.wait(timeout=2.0), "refresh held the in-flight lock too long"
        return []

    monkeypatch.setattr(jobs, "active_jobs", slow_active_jobs)

    async def run() -> None:
        nonlocal hold
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            calls_at_mount = calls
            hold = True
            entered.clear()
            first = asyncio.create_task(app.screen._refresh_jobs_async())  # type: ignore[attr-defined]
            await asyncio.to_thread(entered.wait, 2.0)
            assert entered.is_set(), "first refresh never entered active_jobs"
            second = asyncio.create_task(app.screen._refresh_jobs_async())  # type: ignore[attr-defined]
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)

            assert calls - calls_at_mount == 1

    asyncio.run(run())


def test_hidden_dashboard_refresh_returns_without_listing_jobs(
    mock_env_with_config, monkeypatch, tmp_path: Path
) -> None:
    calls = 0

    def counted_active_jobs() -> list[dict]:
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(jobs, "active_jobs", counted_active_jobs)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            dashboard = app.screen
            await pilot.pause(0.2)
            app.push_screen(NewJobScreen(tmp_path))
            await pilot.pause(0.2)
            calls_before = calls
            await dashboard._refresh_jobs_async()  # type: ignore[attr-defined]

            assert calls == calls_before

    asyncio.run(run())


def test_job_detail_refresh_in_flight_skips_overlapping_tick(
    mock_env_with_config, monkeypatch
) -> None:
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    jobs_dir = data_root / ".dispatch" / "jobs"
    job_id = _seed_job(jobs_dir, "20260520T120000Z_detail", "Running", pid=4242)
    calls = 0
    hold = False
    entered = threading.Event()
    release = threading.Event()
    original_load = manifest.load

    def slow_load(path: Path) -> manifest.JobManifest:
        nonlocal calls
        calls += 1
        if hold:
            entered.set()
            assert release.wait(timeout=2.0), "refresh held the in-flight lock too long"
        return original_load(path)

    monkeypatch.setattr(manifest, "load", slow_load)

    async def run() -> None:
        nonlocal hold
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = JobDetailScreen(job_id)
            app.push_screen(screen)
            await pilot.pause(0.2)
            calls_at_mount = calls
            screen._manifest_item = None
            screen._manifest_mtime = None
            hold = True
            entered.clear()
            first = asyncio.create_task(screen._refresh_detail_async())
            await asyncio.to_thread(entered.wait, 2.0)
            assert entered.is_set(), "first refresh never entered manifest.load"
            second = asyncio.create_task(screen._refresh_detail_async())
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)

            assert calls - calls_at_mount == 1

    asyncio.run(run())


def test_cockpit_slash_filter_narrows_jobs(mock_env_with_config) -> None:
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    jobs_dir = data_root / ".dispatch" / "jobs"
    _seed_job(jobs_dir, "20260520T120000Z_aaaaaa", "Succeeded")
    _seed_job(jobs_dir, "20260520T120001Z_bbbbbb", "Failed")

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            screen = app.screen
            table = screen.query_one("#jobs-table", DataTable)
            assert table.row_count == 2

            await pilot.press("slash")
            await pilot.pause()
            filter_input = screen.query_one("#jobs-filter", Input)
            assert filter_input.display is True
            assert filter_input.has_focus

            await pilot.press("b", "b", "b")
            await pilot.pause(0.5)
            assert table.row_count == 1
            only_key = table.coordinate_to_cell_key((0, 0)).row_key.value
            assert only_key.endswith("bbbbbb")

            await pilot.press("escape")
            await pilot.pause(0.5)
            assert filter_input.display is False
            assert table.row_count == 2

    asyncio.run(run())


def test_new_job_picker_lists_cwd_files_and_fills_form(mock_env_with_config, tmp_path) -> None:
    (tmp_path / "alpha.sql").write_text("select 1\n", encoding="utf-8")
    (tmp_path / "beta_template.sql").write_text(
        "select * from t where d between '{date_inicio}' and '{date_fim}'\n",
        encoding="utf-8",
    )

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(140, 50)) as pilot:
            screen = NewJobScreen(tmp_path)
            app.push_screen(screen)
            await pilot.pause(1.0)

            picker = screen.query_one("#sql-file-picker", DataTable)
            assert picker.display is True
            assert picker.row_count == 2

            picker.focus()
            await pilot.press("down")
            await pilot.pause(0.5)

            assert screen.query_one("#sql-file", Input).value == str(tmp_path / "beta_template.sql")
            # Picking the template flips source detection to SqlTemplate.
            assert screen._selected_source() == "SqlTemplate"

    asyncio.run(run())


def test_new_job_matrix_shows_legal_cells_and_toggles(mock_env_with_config, tmp_path) -> None:
    """The Source x Destination matrix is visible, accurate, and keyboard-toggleable."""

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(140, 50)) as pilot:
            screen = NewJobScreen(tmp_path)
            app.push_screen(screen)
            await pilot.pause(1.0)

            matrix = screen.query_one("#matrix-table", DataTable)
            assert matrix.row_count == 3
            assert matrix.get_row_at(0) == [
                "SqlFile",
                "[green]\u2713[/]",
                "[green]\u2713[/]",
                "[green]\u2713[/]",
            ]
            assert matrix.get_row_at(1) == [
                "SqlTemplate",
                "[green]\u2713[/]",
                "[dim]\u2014[/]",
                "[dim]\u2014[/]",
            ]
            assert matrix.get_row_at(2) == [
                "ExistingTable",
                "[dim]\u2014[/]",
                "[green]\u2713[/]",
                "[dim]\u2014[/]",
            ]

            collapsible = screen.query_one("#matrix-collapsible", Collapsible)
            assert collapsible.collapsed is False

            await pilot.press("m")
            await pilot.pause()
            assert collapsible.collapsed is True

            await pilot.press("m")
            await pilot.pause()
            assert collapsible.collapsed is False

    asyncio.run(run())


def test_command_palette_exposes_dispatch_commands(mock_env_with_config) -> None:
    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            titles = [command.title for command in app.get_system_commands(app.screen)]
            for expected in ("New Job", "History", "Browse metadata", "Refresh Kerberos (kinit)"):
                assert expected in titles, f"Missing palette command: {expected}"

    asyncio.run(run())

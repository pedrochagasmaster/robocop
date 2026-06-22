"""Regression tests for the QA-closure-loop fixes.

Each test maps to a logistical or UX error found while testing the canonical
feature/user-story matrix (docs/qa/feature-user-stories.csv) and fixed in the
same change. Test ids reference the matrix where useful.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from textual.widgets import DataTable, RichLog, Static

from dispatch import impala, manifest, process, sql
from dispatch.app import DispatchApp
from dispatch.screens.browser import BrowserScreen, NO_TABLES_PLACEHOLDER
from dispatch.screens.job_detail import JobDetailScreen, LOG_VIEW_LINES
from dispatch.screens.new_job import NewJobScreen


# ── sql.py date helpers (F2 / NJ-15) ─────────────────────────────────────

def test_validate_date_range_accepts_valid() -> None:
    assert sql.validate_date_range("2026-01-01", "2026-03-31") is None


def test_validate_date_range_rejects_bad_format() -> None:
    assert sql.validate_date_range("2026/01/01", "2026-03-31") is not None
    assert sql.validate_date_range("2026-01-01", "nonsense") is not None


def test_validate_date_range_rejects_start_after_end() -> None:
    msg = sql.validate_date_range("2026-05-01", "2026-01-01")
    assert msg is not None and "after" in msg


def test_orchestrator_date_roundtrip() -> None:
    assert sql.to_orchestrator_date("2026-02-09") == "02/09/2026"
    assert sql.from_orchestrator_date("02/09/2026") == "2026-02-09"
    # Non-orchestrator input is returned unchanged (graceful clone prefill).
    assert sql.from_orchestrator_date("") == ""
    assert sql.from_orchestrator_date("2026-02-09") == "2026-02-09"


# ── Clone prefill mapping (F6 / JD-45) ───────────────────────────────────

def test_clone_prefill_reads_params_email_subject_and_dates() -> None:
    item = {
        "source": {"type": "SqlTemplate", "sql_path_at_launch": "/tmp/q.sql"},
        "destination": {"type": "Table", "schema": "aa_enc", "table_name": "t"},
        "params": {
            "to_email": "ops@example.com",
            "subject": "Monthly load",
            "start_date": "01/01/2026",
            "end_date": "03/31/2026",
        },
    }
    prefill = JobDetailScreen._prefill_from_manifest(item)
    assert prefill["email"] == "ops@example.com"
    assert prefill["subject"] == "Monthly load"
    assert prefill["start_date"] == "2026-01-01"
    assert prefill["end_date"] == "2026-03-31"
    assert prefill["source_type"] == "SqlTemplate"


# ── Impala timeout message (F10 / IMP-06) ────────────────────────────────

def test_impala_query_timeout_has_message(monkeypatch) -> None:
    async def fake_run_exec(*argv, timeout=None):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(process, "run_exec", fake_run_exec)

    async def run() -> None:
        with pytest.raises(RuntimeError) as excinfo:
            await impala.query("SHOW TABLES")
        assert "timed out" in str(excinfo.value)
        assert str(excinfo.value)  # non-blank, unlike str(TimeoutError())

    asyncio.run(run())


# ── New Job validation (F1-F5) ───────────────────────────────────────────

def _new_job_screen(app: DispatchApp, cwd: Path) -> NewJobScreen:
    screen = NewJobScreen(cwd)
    app.push_screen(screen)
    return screen


def test_preview_csv_destination_not_wrapped(mock_env_with_config, tmp_path) -> None:
    (tmp_path / "q.sql").write_text("select 1 as a\n", encoding="utf-8")

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(140, 50)) as pilot:
            screen = _new_job_screen(app, tmp_path)
            await pilot.pause(1.0)
            screen.query_one("#sql-file").value = str(tmp_path / "q.sql")
            # SqlFile -> Csv (default destination): preview must be the raw SELECT.
            screen.query_one("#dst-csv").value = True
            await pilot.pause(0.3)
            screen.action_preview()
            await pilot.pause(0.3)
            body = app.screen.body.lower()
            assert "select 1 as a" in body
            assert "create table" not in body

    asyncio.run(run())


def test_preview_table_destination_is_wrapped(mock_env_with_config, tmp_path) -> None:
    (tmp_path / "q.sql").write_text("select 1 as a\n", encoding="utf-8")

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(140, 50)) as pilot:
            screen = _new_job_screen(app, tmp_path)
            await pilot.pause(1.0)
            screen.query_one("#sql-file").value = str(tmp_path / "q.sql")
            screen.query_one("#dst-table").value = True
            await pilot.pause(0.3)
            screen.action_preview()
            await pilot.pause(0.3)
            assert "create table" in app.screen.body.lower()

    asyncio.run(run())


def test_launch_blocks_invalid_email(mock_env_with_config, tmp_path) -> None:
    (tmp_path / "q.sql").write_text("select 1\n", encoding="utf-8")

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(140, 50)) as pilot:
            screen = _new_job_screen(app, tmp_path)
            await pilot.pause(1.0)
            screen.query_one("#sql-file").value = str(tmp_path / "q.sql")
            screen.query_one("#email").value = "not-an-email"
            await pilot.pause(0.2)
            assert screen._validate() == "Invalid email format"

    asyncio.run(run())


def test_launch_blocks_bad_template_dates(mock_env_with_config, tmp_path) -> None:
    (tmp_path / "t.sql").write_text(
        "select * from x where d between '{date_inicio}' and '{date_fim}'\n",
        encoding="utf-8",
    )

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(140, 50)) as pilot:
            screen = _new_job_screen(app, tmp_path)
            await pilot.pause(1.0)
            screen.query_one("#sql-file").value = str(tmp_path / "t.sql")
            screen.query_one("#src-sqltemplate").value = True
            screen.query_one("#dst-table").value = True
            await pilot.pause(0.3)
            screen.query_one("#start-date").value = "2026-05-01"
            screen.query_one("#end-date").value = "2026-01-01"
            await pilot.pause(0.2)
            assert screen._validate() is not None  # does not raise, returns message

    asyncio.run(run())


def test_preview_bad_template_dates_does_not_crash(mock_env_with_config, tmp_path) -> None:
    (tmp_path / "t.sql").write_text(
        "select '{date_inicio}' , '{date_fim}'\n", encoding="utf-8"
    )

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(140, 50)) as pilot:
            screen = _new_job_screen(app, tmp_path)
            await pilot.pause(1.0)
            screen.query_one("#sql-file").value = str(tmp_path / "t.sql")
            screen.query_one("#src-sqltemplate").value = True
            screen.query_one("#dst-table").value = True
            await pilot.pause(0.3)
            screen.query_one("#start-date").value = "bad-date"
            await pilot.pause(0.2)
            # Must surface a message rather than raising out of the action.
            screen.action_preview()
            await pilot.pause(0.2)
            msg = str(screen.query_one("#warning-text").render())
            assert "date" in msg.lower()

    asyncio.run(run())


def test_validation_summary_reflects_running_cap(mock_env_with_config, tmp_path) -> None:
    # Seed two Running jobs so the concurrency cap is hit.
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    jobs_dir = data_root / ".dispatch" / "jobs"
    for n in range(2):
        _seed_job(jobs_dir, f"20260520T12000{n}Z_run00{n}", "Running", pid=1000 + n)
    (tmp_path / "q.sql").write_text("select 1\n", encoding="utf-8")

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(140, 50)) as pilot:
            screen = _new_job_screen(app, tmp_path)
            await pilot.pause(1.0)
            issues = screen._validation_issues()
            assert any("cap" in i.lower() for i in issues)

    asyncio.run(run())


# ── Job Detail log view (F7 / F8 / F9) ───────────────────────────────────

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


def test_log_view_bounded_and_truncation_hint_truthful(mock_env_with_config) -> None:
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    jobs_dir = data_root / ".dispatch" / "jobs"
    big = [f"line {i}" for i in range(LOG_VIEW_LINES + 75)]
    job_id = _seed_job(jobs_dir, "20260520T120000Z_biglog", "Succeeded", log_lines=big)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            app.push_screen(JobDetailScreen(job_id))
            await pilot.pause(1.0)
            screen = app.screen
            log = screen.query_one("#log-display", RichLog)
            # Widget is bounded to the same window as the in-memory tail.
            assert len(log.lines) <= LOG_VIEW_LINES
            assert screen._evicted_line_count > 0
            # The hint claims hidden lines only because they really are hidden.
            hint = screen.query_one("#truncation-hint", Static)
            assert hint.display is True

    asyncio.run(run())


def test_log_styled_line_highlights_search_query(mock_env_with_config) -> None:
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    jobs_dir = data_root / ".dispatch" / "jobs"
    job_id = _seed_job(jobs_dir, "20260520T120000Z_search", "Succeeded")

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            app.push_screen(JobDetailScreen(job_id))
            await pilot.pause(0.5)
            screen = app.screen
            screen._search_query = "started"
            styled = screen._styled_log_line("the job started ok")
            assert "[reverse]" in styled
            assert "[reverse]" not in screen._styled_log_line("no match here")

    asyncio.run(run())


def test_log_rotation_resets_without_duplicates(mock_env_with_config) -> None:
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    jobs_dir = data_root / ".dispatch" / "jobs"
    job_id = _seed_job(jobs_dir, "20260520T120000Z_rotate", "Running", pid=1)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            app.push_screen(JobDetailScreen(job_id))
            await pilot.pause(0.5)
            screen = app.screen
            screen._append_log_lines(["a", "b", "c"], 30)
            log = screen.query_one("#log-display", RichLog)
            before = len(log.lines)
            assert before >= 3
            # Simulate rotation: log shrank, re-read from scratch.
            screen._append_log_lines(["x", "y"], 6, reset=True)
            assert list(screen._tail_lines) == ["x", "y"]
            assert screen._evicted_line_count == 0
            assert len(log.lines) <= before  # old lines were cleared, not appended

    asyncio.run(run())


# ── Browser empty-placeholder gating (F9 / BRW-05) ───────────────────────

def test_browser_placeholder_not_actionable(mock_env_with_config) -> None:
    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = BrowserScreen(auto_load=False)
            app.push_screen(screen)
            await pilot.pause(0.3)
            table = screen.query_one("#browser-table", DataTable)
            table.add_row(NO_TABLES_PLACEHOLDER, "")
            table.cursor_coordinate = (0, 0)
            await pilot.pause(0.1)
            assert screen._full_table() == ""
            screen._update_action_state()
            assert screen.query_one("#describe").disabled is True
            assert screen.query_one("#drop").disabled is True

    asyncio.run(run())


# ── Dashboard selection/cancel feedback (F11 / F12 / F13) ────────────────

def test_dashboard_cancel_no_selection_notifies(mock_env_with_config) -> None:
    notes: list[str] = []

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            screen = app.screen
            screen.notify = lambda msg, **k: notes.append(msg)  # type: ignore[assignment]
            screen.action_cancel()
            await pilot.pause(0.1)
            assert any("select a job" in n.lower() for n in notes)

    asyncio.run(run())


def test_dashboard_cancel_non_running_notifies(mock_env_with_config) -> None:
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    jobs_dir = data_root / ".dispatch" / "jobs"
    _seed_job(jobs_dir, "20260520T120000Z_donejb", "Succeeded")
    notes: list[str] = []

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            screen = app.screen
            screen.notify = lambda msg, **k: notes.append(msg)  # type: ignore[assignment]
            screen.action_cancel()
            await pilot.pause(0.1)
            assert any("running" in n.lower() for n in notes)

    asyncio.run(run())


def test_dashboard_filter_zero_match_clears_selection(mock_env_with_config) -> None:
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    jobs_dir = data_root / ".dispatch" / "jobs"
    _seed_job(jobs_dir, "20260520T120000Z_aaaaaa", "Succeeded")
    notes: list[str] = []

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            screen = app.screen
            await pilot.press("slash")
            await pilot.pause()
            await pilot.press("z", "z", "z", "z")  # match nothing
            await pilot.pause(0.5)
            table = screen.query_one("#jobs-table", DataTable)
            assert table.row_count == 0
            screen.notify = lambda msg, **k: notes.append(msg)  # type: ignore[assignment]
            screen.action_view_logs()
            await pilot.pause(0.1)
            # No off-screen job is targeted; user gets feedback instead.
            assert any("select a job" in n.lower() for n in notes)

    asyncio.run(run())


# ── Status strip ordering (F15 / OVW-02) ─────────────────────────────────

def test_status_strip_kerberos_first(mock_env_with_config) -> None:
    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            strip = str(app.screen.query_one("#status-strip", Static).render())
            assert strip.index("KERBEROS") < strip.index("RUNNING")

    asyncio.run(run())


# ── Sidebar manual collapse persistence (F16 / SIDE-08) ──────────────────

def test_sidebar_manual_collapse_survives_resize(mock_env_with_config) -> None:
    from dispatch.screens.sidebar import Sidebar

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.5)
            sidebar = app.screen.query_one(Sidebar)
            assert sidebar.collapsed is False
            sidebar.toggle_collapsed()  # user collapses at a wide width
            assert sidebar.collapsed is True
            sidebar._sync_collapse_from_app()  # a resize event fires
            assert sidebar.collapsed is True  # choice is respected, not undone

    asyncio.run(run())


# ── Help accuracy (F17 / HELP-02) ────────────────────────────────────────

def test_help_quick_reference_lists_cancel_and_filter() -> None:
    from dispatch.screens.help import QUICK_HELP

    assert "Cancel" in QUICK_HELP
    assert "Filter" in QUICK_HELP

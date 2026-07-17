"""Phase 1 safety interaction tests."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from textual.widgets import Input

from dispatch import config, jobs
from dispatch.app import DispatchApp
from dispatch.screens.browser import BrowserScreen
from dispatch.screens.job_detail import JobDetailScreen
from dispatch.screens.new_job import NewJobScreen
from dispatch.screens.preview import PreviewScreen


def test_browser_drop_requires_confirmation(mock_env_with_config, monkeypatch) -> None:
    """DROP TABLE only reaches Impala after an affirmative confirmation."""
    calls: list[str] = []

    async def fake_drop_table(full_table: str) -> str:
        calls.append(full_table)
        return f"Dropped {full_table}"

    monkeypatch.setattr("dispatch.impala.drop_table", fake_drop_table)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = BrowserScreen(auto_load=False)
            app.push_screen(screen)
            await pilot.pause()
            screen._tables = ["danger_table"]
            screen._checked = {"danger_table"}
            table = screen.query_one("#browser-table")
            table.clear()
            table.add_row("[X]", "danger_table", "12.6 MB", "table", key="danger_table")
            table.cursor_coordinate = (0, 0)
            screen._update_action_state()

            worker = screen.action_drop()
            await pilot.pause()
            await pilot.press("escape")
            await worker.wait()
            assert calls == []

            worker = screen.action_drop()
            await pilot.pause()
            app.screen.query_one("#confirm-input", Input).value = "I AM SURE"
            app.screen.query_one("#confirm-input-secondary", Input).value = "DROP"
            await pilot.press("enter")
            await worker.wait()
            assert calls == ["aa_enc.danger_table"]

    asyncio.run(run())


def test_job_cancel_requires_confirmation(mock_env_with_config, monkeypatch) -> None:
    """Cancel Job must show a confirmation before sending SIGTERM."""
    monkeypatch.setattr(jobs, "pid_is_alive", lambda pid: True)
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    job_id = "20260516T100000Z_cancel"
    job_dir = data_root / ".dispatch" / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "manifest.json").write_text(
        """
{
  "destination": {"csv_path": "", "schema": "aa_enc", "table_name": "target", "type": "Csv"},
  "exit_code": null,
  "finished_at": null,
  "id": "20260516T100000Z_cancel",
  "orchestrator_calls": [{"argv": ["python3", "x.py"], "script": "download_to_csv.py"}],
  "params": {},
  "pid": 4242,
  "schema_version": 1,
  "source": {"type": "SqlFile"},
  "started_at": "2026-05-16T10:00:00Z",
  "state": "Running",
  "tool": "dispatch",
  "user": "testuser"
}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    cancelled: list[int] = []
    monkeypatch.setattr("dispatch.process.cancel_process_group", cancelled.append)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = JobDetailScreen(job_id)
            app.push_screen(screen)
            await pilot.pause()

            worker = screen.action_cancel()
            await pilot.pause()
            await pilot.press("n")
            await worker.wait()
            assert cancelled == []

            worker = screen.action_cancel()
            await pilot.pause()
            await pilot.press("y")
            await worker.wait()
            assert cancelled == [4242]

    asyncio.run(run())


def test_new_job_launch_requires_confirmation(mock_env_with_config, monkeypatch, tmp_path) -> None:
    """Launch creates no manifest or runner process until explicitly confirmed."""
    sql_file = tmp_path / "query.sql"
    sql_file.write_text("select 1\n", encoding="utf-8")
    launched: list[Path] = []

    async def fake_ttl() -> int:
        return 3600

    async def fake_launch_runner(job_dir: Path) -> int:
        launched.append(job_dir)
        return 123

    monkeypatch.setattr("dispatch.kerberos.ticket_ttl_seconds", fake_ttl)
    monkeypatch.setattr("dispatch.process.launch_runner", fake_launch_runner)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(140, 50)) as pilot:
            screen = NewJobScreen(tmp_path)
            app.push_screen(screen)
            await pilot.pause()

            worker = screen.action_launch()
            await pilot.pause()
            await pilot.press("n")
            await worker.wait()
            assert launched == []

            worker = screen.action_launch()
            await pilot.pause()
            await pilot.press("y")
            await worker.wait()
            assert len(launched) == 1

    asyncio.run(run())


def test_new_job_rechecks_preflight_after_confirm_before_manifest(
    mock_env_with_config, monkeypatch, tmp_path
) -> None:
    """Launch refuses if Kerberos drops below five minutes while confirm is open."""
    sql_file = tmp_path / "query.sql"
    sql_file.write_text("select 1\n", encoding="utf-8")
    ttl = 3600
    launched: list[Path] = []

    async def fake_ttl() -> int:
        return ttl

    async def fake_launch_runner(job_dir: Path) -> int:
        launched.append(job_dir)
        return 123

    async def fake_confirm(self, source, destination) -> bool:
        nonlocal ttl
        ttl = 299
        self.app.kerberos_ttl = 299
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr("dispatch.kerberos.ticket_ttl_seconds", fake_ttl)
    monkeypatch.setattr("dispatch.process.launch_runner", fake_launch_runner)
    monkeypatch.setattr(NewJobScreen, "_confirm_launch", fake_confirm)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(140, 50)) as pilot:
            screen = NewJobScreen(tmp_path)
            app.push_screen(screen)
            await pilot.pause()

            worker = screen.action_launch()
            await worker.wait()
            await pilot.pause()

            assert launched == []
            assert list(config.jobs_dir().glob("*/manifest.json")) == []
            warning = str(screen.query_one("#warning-text").render())
            assert "Kerberos ticket TTL is under 5 minutes" in warning

    asyncio.run(run())


def test_sql_preview_missing_file_shows_actionable_error(
    mock_env_with_config, monkeypatch, tmp_path
) -> None:
    """Preview handles a missing SQL file in the TUI instead of crashing."""

    async def fake_ttl() -> int:
        return 3600

    monkeypatch.setattr("dispatch.kerberos.ticket_ttl_seconds", fake_ttl)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(140, 50)) as pilot:
            screen = NewJobScreen(tmp_path)
            app.push_screen(screen)
            await pilot.pause()

            screen.action_preview()
            await pilot.pause()

            warning = screen.query_one("#warning-text").render()
            assert "Cannot read SQL file" in str(warning)
            assert str(tmp_path / "query.sql") in str(warning)

    asyncio.run(run())


def test_preview_action_copy_matches_behavior(mock_env_with_config) -> None:
    """Preview must not advertise a Launch action that only returns."""

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(100, 30)) as pilot:
            screen = PreviewScreen("SQL Preview", "select 1", schema="dw", table="target")
            app.push_screen(screen)
            await pilot.pause()
            assert "Launch" not in str(screen.render())

    asyncio.run(run())

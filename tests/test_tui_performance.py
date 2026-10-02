from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from textual.widgets import DataTable, Input

from dispatch.app import DispatchApp
from dispatch.screens.browser import BrowserScreen
from dispatch.screens.job_detail import JobDetailScreen
from dispatch.screens.new_job import NewJobScreen


def test_browser_toggle_patches_cell_without_rebuilding_table(mock_env_with_config) -> None:
    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = BrowserScreen(auto_load=False)
            app.push_screen(screen)
            await pilot.pause(0.2)
            screen._tables = [f"table_{index:03d}" for index in range(100)]
            screen._rebuild_table_rows()
            screen._render_table_list(selected_before="table_050")
            table = screen.query_one("#browser-table", DataTable)
            before_rows = table.row_count
            before_cursor = screen._selected_table()

            def fail_rebuild(*_args, **_kwargs) -> None:
                raise AssertionError("selection toggle rebuilt the complete table")

            screen._render_table_list = fail_rebuild  # type: ignore[method-assign]
            screen._toggle_check_named("table_050")

            assert table.row_count == before_rows
            assert screen._selected_table() == before_cursor
            assert "table_050" in screen._checked
            assert screen._sel_column_key is not None
            assert str(table.get_cell("table_050", screen._sel_column_key)) == "[X]"

    asyncio.run(run())


def test_log_search_coalesces_rapid_repaints(mock_env_with_config, monkeypatch) -> None:
    class FakeTimer:
        def __init__(self, callback: Callable[[], None]) -> None:
            self.callback = callback
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = JobDetailScreen("20260520T120000Z_perf01")
            app.push_screen(screen)
            await pilot.pause(0.2)
            calls = 0
            original = screen._rebuild_log

            def count_rebuild() -> None:
                nonlocal calls
                calls += 1
                original()

            screen._rebuild_log = count_rebuild  # type: ignore[method-assign]
            timers: list[FakeTimer] = []

            def fake_set_timer(_delay: float, callback: Callable[[], None], *_args, **_kwargs):
                timer = FakeTimer(callback)
                timers.append(timer)
                return timer

            monkeypatch.setattr(screen, "set_timer", fake_set_timer)
            search = screen.query_one("#log-search-input", Input)
            search.display = True
            search.focus()
            for value in ("l", "li", "lin", "line"):
                search.value = value
                await pilot.pause()

            assert screen._search_query == "line"
            assert calls == 0
            assert len(timers) == 4
            assert all(timer.stopped for timer in timers[:-1])
            assert not timers[-1].stopped

            timers[-1].callback()
            assert calls == 1

    asyncio.run(run())


def test_new_job_inline_feedback_never_stats_uncached_path(
    mock_env_with_config, tmp_path: Path, monkeypatch
) -> None:
    sql_path = tmp_path / "query.sql"
    sql_path.write_text("SELECT 1;\n", encoding="utf-8")

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = NewJobScreen(tmp_path, prefill={"sql_file": str(sql_path)})
            app.push_screen(screen)
            await pilot.pause(0.4)
            screen._sql_exists_cache = None

            def forbidden_exists(_self: Path) -> bool:
                raise AssertionError("filesystem stat ran on the UI feedback path")

            with monkeypatch.context() as scoped:
                scoped.setattr(Path, "exists", forbidden_exists)
                screen._inline_validate()
                screen._refresh_path_hint()
                warning = str(screen.query_one("#warning-text").render())
                hint = str(screen.query_one("#path-hint").render())

            assert "checking" in warning
            assert sql_path.name in hint

    asyncio.run(run())


def test_new_job_validation_worker_ignores_stale_result(
    mock_env_with_config, tmp_path: Path, monkeypatch
) -> None:
    original_set_timer = NewJobScreen.set_timer

    def positive_set_timer(self, delay, *args, **kwargs):
        assert delay > 0, "Textual timers must have a positive interval"
        return original_set_timer(self, delay, *args, **kwargs)

    monkeypatch.setattr(NewJobScreen, "set_timer", positive_set_timer)
    first = tmp_path / "first.sql"
    second = tmp_path / "second.sql"
    first.write_text("SELECT 1;\n", encoding="utf-8")
    second.write_text("SELECT 2;\n", encoding="utf-8")

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = NewJobScreen(tmp_path, prefill={"sql_file": str(first)})
            app.push_screen(screen)
            await pilot.pause(0.4)
            sql_input = screen.query_one("#sql-file", Input)
            sql_input.value = str(first)
            screen._schedule_validation_summary(delay=0.0)
            sql_input.value = str(second)
            screen._schedule_validation_summary(delay=0.0)
            for _ in range(40):
                await pilot.pause(0.05)
                if screen._sql_exists_cache == (str(second), True):
                    break
            assert screen._sql_exists_cache == (str(second), True)

    asyncio.run(run())

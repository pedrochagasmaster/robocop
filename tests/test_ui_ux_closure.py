"""Focused UI/UX closure tests for high-risk screenshot review findings."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.text import Text
from textual.widgets import DataTable, Input

from dispatch import impala, manifest, telemetry
from dispatch.app import DispatchApp
from dispatch.screens.browser import CHECKED_MARKER, UNCHECKED_MARKER, BrowserScreen
from dispatch.screens.history import PAGE_SIZE, HistoryScreen
from dispatch.screens.job_detail import JobDetailScreen
from dispatch.screens.new_job import NewJobScreen
from dispatch.screens.sidebar import NavItem


def _prepare_checked_table(screen: BrowserScreen, table_name: str = "danger_table") -> DataTable:
    screen._tables = [table_name]
    screen._checked = {table_name}
    table = screen.query_one("#browser-table", DataTable)
    table.clear()
    table.add_row(CHECKED_MARKER, table_name, "—", "table", key=table_name)
    table.cursor_coordinate = (0, 0)
    screen._update_action_state()
    return table


def _sel_plain(cell: object) -> str:
    return cell.plain if isinstance(cell, Text) else str(cell)


async def _confirm_bulk_drop(pilot, app: DispatchApp) -> None:
    app.screen.query_one("#confirm-input", Input).value = "I AM SURE"
    app.screen.query_one("#confirm-input-secondary", Input).value = "DROP"
    await pilot.press("enter")


def _seed_history_job(jobs_dir: Path, index: int) -> str:
    job_id = f"20260401T10{index:04d}00Z_hist{index:02d}"
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    data: manifest.JobManifest = {
        "schema_version": 1,
        "id": job_id,
        "tool": "dispatch",
        "user": "testuser",
        "source": {"type": "SqlFile", "sql_path_at_launch": f"/tmp/query_{index}.sql"},
        "destination": {
            "type": "Csv",
            "schema": "aa_enc",
            "table_name": f"history_{index}",
            "csv_path": f"/tmp/history_{index}.csv",
        },
        "params": {},
        "orchestrator_calls": [{"script": "download_to_csv.py", "argv": ["python3", "x.py"]}],
        "state": "Succeeded",
        "pid": None,
        "started_at": "2026-04-01T10:00:00Z",
        "finished_at": "2026-04-01T10:05:00Z",
        "exit_code": 0,
    }
    manifest.write(job_dir / "manifest.json", data)
    return job_id


async def _click_sidebar_item(pilot, screen, item_id: str) -> None:
    target = next(widget for widget in screen.query(NavItem) if widget.item_id == item_id)
    await pilot.click(target)


def _seed_recent_job(jobs_dir: Path, suffix: str) -> str:
    job_id = f"20260520T1200{suffix}Z_recent{suffix}"
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    manifest.write(
        job_dir / "manifest.json",
        {
            "schema_version": 1,
            "id": job_id,
            "tool": "dispatch",
            "user": "testuser",
            "source": {"type": "SqlFile", "sql_path_at_launch": f"/tmp/recent_{suffix}.sql"},
            "destination": {
                "type": "Csv",
                "schema": "aa_enc",
                "table_name": f"recent_{suffix}",
                "csv_path": f"/tmp/recent_{suffix}.csv",
            },
            "params": {},
            "orchestrator_calls": [{"script": "download_to_csv.py", "argv": ["python3", "x.py"]}],
            "state": "Succeeded",
            "pid": None,
            "started_at": "2026-05-20T12:00:00Z",
            "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "exit_code": 0,
        },
    )
    return job_id


def _seed_old_history_job(jobs_dir: Path, suffix: str) -> str:
    finished = datetime.now(timezone.utc) - timedelta(days=10)
    started = finished - timedelta(minutes=5)
    job_id = f"20260401T1000{suffix}Z_old{suffix}"
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    manifest.write(
        job_dir / "manifest.json",
        {
            "schema_version": 1,
            "id": job_id,
            "tool": "dispatch",
            "user": "testuser",
            "source": {"type": "SqlFile", "sql_path_at_launch": f"/tmp/old_{suffix}.sql"},
            "destination": {
                "type": "Csv",
                "schema": "aa_enc",
                "table_name": f"old_{suffix}",
                "csv_path": f"/tmp/old_{suffix}.csv",
            },
            "params": {},
            "orchestrator_calls": [{"script": "download_to_csv.py", "argv": ["python3", "x.py"]}],
            "state": "Succeeded",
            "pid": None,
            "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "exit_code": 0,
        },
    )
    return job_id


def test_history_pagination_keys_move_between_pages(mock_env_with_config) -> None:
    """History next/previous bindings move the visible page for large histories."""
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    jobs_dir = data_root / ".dispatch" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    for index in range(PAGE_SIZE + 3):
        _seed_history_job(jobs_dir, index)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = HistoryScreen()
            app.push_screen(screen)
            await pilot.pause()

            page_info = screen.query_one("#page-info")
            page_controls = screen.query_one("#page-controls")
            assert "Showing 1-17 of 20" in str(page_info.render())
            assert "Page 1 of 2" in str(page_controls.render())

            await pilot.press("]")
            await pilot.pause()
            assert "Showing 18-20 of 20" in str(page_info.render())
            assert "Page 2 of 2" in str(page_controls.render())

            await pilot.press("[")
            await pilot.pause()
            assert "Showing 1-17 of 20" in str(page_info.render())
            assert "Page 1 of 2" in str(page_controls.render())

    asyncio.run(run())


def test_history_enter_opens_full_job_id_not_truncated_value(
    mock_env_with_config,
) -> None:
    """Enter on a history row opens the full durable row key, not the visible ID."""
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    jobs_dir = data_root / ".dispatch" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_id = _seed_history_job(jobs_dir, 12345)
    assert len(job_id) > 24

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = HistoryScreen()
            app.push_screen(screen)
            await pilot.pause()

            screen.action_view_logs()
            await pilot.pause()
            assert isinstance(app.screen, JobDetailScreen)
            assert app.screen.job_id == job_id

    asyncio.run(run())
    assert telemetry.flush(timeout=1)
    events = [
        json.loads(line)
        for line in telemetry.private_events_path().read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        event["event"] == "screen_view" and event["props"].get("screen") == "job_detail"
        for event in events
    )


def test_sidebar_click_navigation_switches_screens_from_nested_state(
    mock_env_with_config, tmp_path
) -> None:
    """Sidebar clicks navigate reliably from nested screens and keep a flat stack."""
    (tmp_path / "query.sql").write_text("select 1\n", encoding="utf-8")

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            app.push_screen(NewJobScreen(tmp_path))
            await pilot.pause()

            await _click_sidebar_item(pilot, app.screen, "history")
            await pilot.pause()
            assert isinstance(app.screen, HistoryScreen)
            assert [type(screen).__name__ for screen in app.screen_stack] == [
                "Screen",
                "DashboardScreen",
                "HistoryScreen",
            ]

            await _click_sidebar_item(pilot, app.screen, "browse")
            await pilot.pause()
            assert isinstance(app.screen, BrowserScreen)
            assert [type(screen).__name__ for screen in app.screen_stack] == [
                "Screen",
                "DashboardScreen",
                "BrowserScreen",
            ]

    asyncio.run(run())


def test_sidebar_view_logs_from_history_uses_selected_job(
    mock_env_with_config,
) -> None:
    """Sidebar View Logs opens the selected History job on a flat stack."""
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    jobs_dir = data_root / ".dispatch" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_id = _seed_history_job(jobs_dir, 99)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            app.push_screen(HistoryScreen())
            await pilot.pause()

            await _click_sidebar_item(pilot, app.screen, "view_logs")
            await pilot.pause()
            assert isinstance(app.screen, JobDetailScreen)
            assert app.screen.job_id == job_id
            assert [type(screen).__name__ for screen in app.screen_stack] == [
                "Screen",
                "DashboardScreen",
                "JobDetailScreen",
            ]

    asyncio.run(run())


def test_dashboard_top_level_actions_open_expected_screens(mock_env_with_config, tmp_path) -> None:
    """Dashboard keyboard actions should still open the same top-level screens."""
    (tmp_path / "query.sql").write_text("select 1\n", encoding="utf-8")

    async def run() -> None:
        app = DispatchApp()
        app.launch_cwd = tmp_path
        async with app.run_test(size=(120, 40)) as pilot:
            dashboard = app.screen
            dashboard.action_new_job()
            await pilot.pause()
            assert isinstance(app.screen, NewJobScreen)
            assert [type(screen).__name__ for screen in app.screen_stack] == [
                "Screen",
                "DashboardScreen",
                "NewJobScreen",
            ]

            app.pop_screen()
            await pilot.pause()
            dashboard.action_history()
            await pilot.pause()
            assert isinstance(app.screen, HistoryScreen)
            assert [type(screen).__name__ for screen in app.screen_stack] == [
                "Screen",
                "DashboardScreen",
                "HistoryScreen",
            ]

            app.pop_screen()
            await pilot.pause()
            dashboard.action_browser()
            await pilot.pause()
            assert isinstance(app.screen, BrowserScreen)
            assert [type(screen).__name__ for screen in app.screen_stack] == [
                "Screen",
                "DashboardScreen",
                "BrowserScreen",
            ]

    asyncio.run(run())


def test_dashboard_jobs_table_takes_focus_and_arrow_selection_works(
    mock_env_with_config,
) -> None:
    """Arrow navigation and view logs should target the unified jobs table."""
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    jobs_dir = data_root / ".dispatch" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    newer_job = _seed_recent_job(jobs_dir, "1")
    older_job = _seed_recent_job(jobs_dir, "0")

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            jobs_table = screen.query_one("#jobs-table", DataTable)

            assert jobs_table.has_focus is True

            await pilot.press("down")
            await pilot.pause()
            screen.action_view_logs()
            await pilot.pause()

            assert isinstance(app.screen, JobDetailScreen)
            assert app.screen.job_id == older_job
            assert app.screen.job_id != newer_job

    asyncio.run(run())


def test_history_refresh_rereads_manifests_while_screen_is_mounted(
    mock_env_with_config,
) -> None:
    """Refreshing History should pick up jobs written after the screen mounted."""
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    jobs_dir = data_root / ".dispatch" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    _seed_old_history_job(jobs_dir, "0")

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = HistoryScreen()
            app.push_screen(screen)
            await pilot.pause()

            page_info = screen.query_one("#page-info")
            assert "of 1" in str(page_info.render())

            _seed_old_history_job(jobs_dir, "1")
            screen.refresh_history()
            await pilot.pause()

            assert "of 2" in str(page_info.render())

    asyncio.run(run())


def test_history_empty_state_focuses_search_for_keyboard_use(
    mock_env_with_config,
) -> None:
    """Empty History should move focus to the visible search box."""

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = HistoryScreen()
            app.push_screen(screen)
            await pilot.pause()

            search = screen.query_one("#search", Input)
            table = screen.query_one("#history-table", DataTable)

            assert table.display is False
            assert search.has_focus is True

            await pilot.press("x")
            await pilot.pause()
            assert search.value == "x"

    asyncio.run(run())


def _fake_iter_table_sizes(
    sizes: dict[str, tuple[int | None, str]],
):
    async def fake(schema: str, table_names: list[str]):
        for name in table_names:
            size_bytes, size_display = sizes.get(name, (None, "—"))
            yield name, impala.TableStats(size_bytes=size_bytes, size_display=size_display)

    return fake


def test_browser_placeholder_and_auto_describe_after_show_tables(
    mock_env_with_config, monkeypatch
) -> None:
    """Browser explains the empty detail pane and fills it after loading tables."""

    async def fake_show_tables(schema: str, pattern: str = "*") -> list[str]:
        return ["dispatch_result", "dispatch_archive"]

    async def fake_describe_table(full_table: str) -> str:
        return "name|type|comment\nid|string|primary key"

    monkeypatch.setattr("dispatch.impala.show_tables", fake_show_tables)
    monkeypatch.setattr(
        "dispatch.impala.iter_table_sizes",
        _fake_iter_table_sizes(
            {
                "dispatch_result": (13_212_057, "12.6 MB"),
                "dispatch_archive": (1_342_177_280, "1.2 GB"),
            }
        ),
    )
    monkeypatch.setattr("dispatch.impala.describe_table", fake_describe_table)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = BrowserScreen()
            app.push_screen(screen)
            await pilot.pause(0.5)

            assert "aa_enc.dispatch_result" in str(screen.query_one("#file-preview-title").render())
            # The data is now in the DataTable, not the Static body
            describe_table = screen.query_one("#describe-table", DataTable)
            assert describe_table.display is True
            # Check for column name in any row
            all_row_data = [describe_table.get_row_at(i) for i in range(describe_table.row_count)]
            assert any("id" in row for row in all_row_data)

    asyncio.run(run())


def test_browser_show_tables_failure_replaces_stale_schema_with_error(
    mock_env_with_config, monkeypatch
) -> None:
    """SHOW TABLES reruns should hide stale schema content when they fail."""
    show_tables_calls = 0

    async def fake_show_tables(schema: str, pattern: str = "*") -> list[str]:
        nonlocal show_tables_calls
        show_tables_calls += 1
        if show_tables_calls <= 1:
            return ["dispatch_result"]
        raise RuntimeError("metadata backend offline")

    async def fake_describe_table(full_table: str) -> str:
        return "name|type|comment\nid|string|primary key"

    monkeypatch.setattr("dispatch.impala.show_tables", fake_show_tables)
    monkeypatch.setattr(
        "dispatch.impala.iter_table_sizes",
        _fake_iter_table_sizes({"dispatch_result": (13_212_057, "12.6 MB")}),
    )
    monkeypatch.setattr("dispatch.impala.describe_table", fake_describe_table)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = BrowserScreen()
            app.push_screen(screen)
            await pilot.pause(0.5)
            assert screen.query_one("#describe-table", DataTable).display is True

            await screen.action_show_tables()
            await pilot.pause(0.5)

            describe_table = screen.query_one("#describe-table", DataTable)
            describe_body = screen.query_one("#describe-body")
            assert describe_table.display is False
            assert describe_body.display is True
            assert "metadata backend offline" in str(describe_body.render())

    asyncio.run(run())


def test_browser_sorts_by_table_size(mock_env_with_config, monkeypatch) -> None:
    """Browser can cycle sort modes and order rows by on-disk size."""

    async def fake_show_tables(schema: str, pattern: str = "*") -> list[str]:
        return ["dispatch_alpha", "dispatch_zulu"]

    async def fake_describe_table(full_table: str) -> str:
        return "name|type|comment\nid|string|primary key"

    monkeypatch.setattr("dispatch.impala.show_tables", fake_show_tables)
    monkeypatch.setattr(
        "dispatch.impala.iter_table_sizes",
        _fake_iter_table_sizes(
            {
                "dispatch_alpha": (13_212_057, "12.6 MB"),
                "dispatch_zulu": (1_342_177_280, "1.2 GB"),
            }
        ),
    )
    monkeypatch.setattr("dispatch.impala.describe_table", fake_describe_table)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = BrowserScreen(auto_load=False)
            app.push_screen(screen)
            await pilot.pause()
            await screen.action_show_tables(describe_selection=False)
            await app.workers.wait_for_complete()
            await pilot.pause()

            table = screen.query_one("#browser-table", DataTable)
            assert table.get_row_at(0)[1] == "dispatch_alpha"
            assert table.get_row_at(1)[1] == "dispatch_zulu"

            screen.action_cycle_sort()
            assert table.get_row_at(0)[1] == "dispatch_zulu"
            assert table.get_row_at(1)[1] == "dispatch_alpha"
            assert table.get_row_at(0)[2] == "1.2 GB"
            # Largest-first is a descending display, so the arrow points down.
            indicator = str(screen.query_one("#browser-sort-indicator").render())
            assert "Sorted by: size ↓" in indicator

            # Clicking the Size header toggles ascending / descending.
            assert screen._size_column_key is not None
            screen.on_data_table_header_selected(
                DataTable.HeaderSelected(
                    table,
                    screen._size_column_key,
                    2,
                    table.columns[screen._size_column_key].label,
                )
            )
            await pilot.pause()
            assert table.get_row_at(0)[1] == "dispatch_alpha"
            assert table.get_row_at(1)[1] == "dispatch_zulu"
            assert "Sorted by: size ↑" in str(screen.query_one("#browser-sort-indicator").render())

            screen.on_data_table_header_selected(
                DataTable.HeaderSelected(
                    table,
                    screen._size_column_key,
                    2,
                    table.columns[screen._size_column_key].label,
                )
            )
            await pilot.pause()
            assert table.get_row_at(0)[1] == "dispatch_zulu"
            assert "Sorted by: size ↓" in str(screen.query_one("#browser-sort-indicator").render())

    asyncio.run(run())


def test_browser_renders_list_before_sizes_and_fills_them_in_background(
    mock_env_with_config, monkeypatch
) -> None:
    """The table list is usable immediately; sizes stream in in the background."""
    release_sizes = asyncio.Event()

    async def fake_show_tables(schema: str, pattern: str = "*") -> list[str]:
        return ["dispatch_alpha", "dispatch_zulu"]

    async def gated_iter_table_sizes(schema: str, table_names: list[str]):
        await release_sizes.wait()
        for name in table_names:
            yield name, impala.TableStats(size_bytes=1_342_177_280, size_display="1.2 GB")

    monkeypatch.setattr("dispatch.impala.show_tables", fake_show_tables)
    monkeypatch.setattr("dispatch.impala.iter_table_sizes", gated_iter_table_sizes)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = BrowserScreen(auto_load=False)
            app.push_screen(screen)
            await pilot.pause()
            await screen.action_show_tables(describe_selection=False)
            await pilot.pause()

            table = screen.query_one("#browser-table", DataTable)
            assert table.row_count == 2
            assert table.get_row_at(0)[2] == "…"
            assert "sizes loading" in str(screen.query_one("#browser-sort-indicator").render())

            table.cursor_coordinate = (1, 0)
            release_sizes.set()
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert table.get_row_at(0)[2] == "1.2 GB"
            assert table.get_row_at(1)[2] == "1.2 GB"
            # In-place cell updates must not disturb the cursor.
            assert table.cursor_coordinate.row == 1
            assert "sizes loading" not in str(screen.query_one("#browser-sort-indicator").render())

    asyncio.run(run())


def test_browser_click_sel_toggles_check_space_does_not(mock_env_with_config, monkeypatch) -> None:
    """Drop-selection is toggled by clicking Sel, not by pressing Space."""

    async def fake_show_tables(schema: str, pattern: str = "*") -> list[str]:
        return ["dispatch_alpha", "dispatch_zulu"]

    monkeypatch.setattr("dispatch.impala.show_tables", fake_show_tables)
    monkeypatch.setattr("dispatch.impala.iter_table_sizes", _fake_iter_table_sizes({}))

    async def run() -> None:
        from dispatch.screens.browser import BrowserTable

        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = BrowserScreen(auto_load=False)
            app.push_screen(screen)
            await pilot.pause()
            await screen.action_show_tables(describe_selection=False)
            await pilot.pause()

            table = screen.query_one("#browser-table", BrowserTable)
            assert screen._checked == set()
            assert "Click [ ] to select" in str(
                screen.query_one("#browser-selection-count").render()
            )

            # Space must not toggle selection.
            await pilot.press("space")
            await pilot.pause()
            assert screen._checked == set()
            assert _sel_plain(table.get_row_at(0)[0]) == UNCHECKED_MARKER.plain

            # Clicking the Sel cell toggles the checkbox for that row.
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            screen.on_browser_table_sel_clicked(BrowserTable.SelClicked(table, row_key))
            await pilot.pause()
            assert screen._checked == {"dispatch_alpha"}
            # Selected state must show a visible mark inside the box (not blank).
            assert _sel_plain(table.get_row_at(0)[0]) == "[X]"
            assert screen.query_one("#drop").disabled is False

            # Column order is Name then Size.
            assert list(table.columns.keys())  # non-empty
            labels = [str(col.label) for col in table.columns.values()]
            assert labels == ["Sel", "Name", "Size", "Type"]

    asyncio.run(run())


def test_browser_size_column_visible_on_typical_ssh_width(
    mock_env_with_config, monkeypatch, tmp_path
) -> None:
    """Size must remain on-screen at common SSH widths with long table names.

    Analysts often run Dispatch over SSH at ~100×30. Auto-sized Name columns
    used to push Size off the right edge of the Browse list, so sizes looked
    like they never loaded even though the worker had finished.
    """

    async def fake_show_tables(schema: str, pattern: str = "*") -> list[str]:
        return ["dispatch_monthly_fulljoin", "dispatch_result"]

    monkeypatch.setattr("dispatch.impala.show_tables", fake_show_tables)
    monkeypatch.setattr(
        "dispatch.impala.iter_table_sizes",
        _fake_iter_table_sizes(
            {
                "dispatch_monthly_fulljoin": (388_444_979, "370.4 MB"),
                "dispatch_result": (13_212_057, "12.6 MB"),
            }
        ),
    )

    async def run_at(size: tuple[int, int]) -> None:
        app = DispatchApp()
        async with app.run_test(size=size) as pilot:
            screen = BrowserScreen(auto_load=False)
            app.push_screen(screen)
            await pilot.pause()
            await screen.action_show_tables(describe_selection=False)
            await app.workers.wait_for_complete()
            await pilot.pause()

            table = screen.query_one("#browser-table", DataTable)
            assert table.get_row_at(0)[2] in {"370.4 MB", "12.6 MB"}
            # Size sits to the right of Name and stays visible when Name is capped.
            assert table.scroll_x == 0

            svg_path = tmp_path / f"browse-sizes-{size[0]}x{size[1]}.svg"
            app.save_screenshot(filename=str(svg_path))
            svg = svg_path.read_text(encoding="utf-8")
            assert "Size" in svg
            assert "MB" in svg

    async def run() -> None:
        await run_at((100, 30))
        # 80×24 still exposes the Size header beside Name; data rows may be
        # clipped vertically by the minimum terminal layout, so only assert
        # the header remains on-screen there.
        app = DispatchApp()
        async with app.run_test(size=(80, 24)) as pilot:
            screen = BrowserScreen(auto_load=False)
            app.push_screen(screen)
            await pilot.pause()
            await screen.action_show_tables(describe_selection=False)
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = screen.query_one("#browser-table", DataTable)
            assert table.get_row_at(0)[2] in {"370.4 MB", "12.6 MB"}
            svg_path = tmp_path / "browse-sizes-80x24.svg"
            app.save_screenshot(filename=str(svg_path))
            assert "Size" in svg_path.read_text(encoding="utf-8")

    asyncio.run(run())


def test_browser_drop_requires_typing_i_am_sure_and_drop(mock_env_with_config, monkeypatch) -> None:
    """DROP confirmation requires I AM SURE and DROP, not the table name."""
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
            _prepare_checked_table(screen)

            worker = screen.action_drop()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert calls == []
            assert worker.is_running

            app.screen.query_one("#confirm-input", Input).value = "I AM SURE"
            app.screen.query_one("#confirm-input-secondary", Input).value = "DROP"
            await pilot.press("enter")
            await worker.wait()
            assert calls == ["aa_enc.danger_table"]

    asyncio.run(run())


def test_typed_drop_confirmation_button_does_not_bypass_input(
    mock_env_with_config, monkeypatch
) -> None:
    """The danger button stays disabled until both confirmation phrases match."""
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
            _prepare_checked_table(screen)

            worker = screen.action_drop()
            await pilot.pause()
            confirm_screen = app.screen
            assert confirm_screen.query_one("#confirm-yes").disabled is True
            confirm_screen.query_one("#confirm-yes").press()
            await pilot.pause()
            assert calls == []
            assert worker.is_running

            confirm_screen.query_one("#confirm-input", Input).value = "I AM SURE"
            confirm_screen.query_one("#confirm-input-secondary", Input).value = "DROP"
            confirm_screen._update_confirm_enabled()
            confirm_screen.query_one("#confirm-yes").press()
            await worker.wait()
            assert calls == ["aa_enc.danger_table"]

    asyncio.run(run())


def test_sidebar_view_logs_from_job_detail_is_a_noop(
    mock_env_with_config,
) -> None:
    """Clicking the active View Logs nav item should not warn or navigate."""
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    jobs_dir = data_root / ".dispatch" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_id = _seed_history_job(jobs_dir, 88)

    async def run() -> None:
        notifications: list[tuple[str, dict]] = []

        def fake_notify(message: str, **kwargs) -> None:
            notifications.append((message, kwargs))

        app = DispatchApp()
        app.notify = fake_notify  # type: ignore[method-assign]

        async with app.run_test(size=(120, 40)) as pilot:
            app.push_screen(JobDetailScreen(job_id))
            await pilot.pause()
            notifications.clear()

            expected_stack = [type(screen).__name__ for screen in app.screen_stack]
            await _click_sidebar_item(pilot, app.screen, "view_logs")
            await pilot.pause()

            assert isinstance(app.screen, JobDetailScreen)
            assert app.screen.job_id == job_id
            assert [type(screen).__name__ for screen in app.screen_stack] == expected_stack
            assert notifications == []

    asyncio.run(run())


def test_browser_drop_replaces_schema_table_with_persistent_result_message(
    mock_env_with_config, monkeypatch
) -> None:
    """DROP feedback should be visible in the detail pane after describing a table."""
    calls: list[str] = []

    async def fake_describe_table(full_table: str) -> str:
        return "name|type|comment\nid|string|primary key"

    async def fake_drop_table(full_table: str) -> str:
        calls.append(full_table)
        return f"Dropped {full_table}"

    monkeypatch.setattr("dispatch.impala.describe_table", fake_describe_table)
    monkeypatch.setattr("dispatch.impala.drop_table", fake_drop_table)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = BrowserScreen(auto_load=False)
            app.push_screen(screen)
            await pilot.pause()
            _prepare_checked_table(screen)

            await screen.action_describe()
            await pilot.pause()
            assert screen.query_one("#describe-table", DataTable).display is True

            worker = screen.action_drop()
            await pilot.pause()
            await _confirm_bulk_drop(pilot, app)
            await worker.wait()
            await pilot.pause()

            describe_table = screen.query_one("#describe-table", DataTable)
            describe_body = screen.query_one("#describe-body")
            assert describe_table.display is False
            assert describe_body.display is True
            assert "aa_enc.danger_table" in str(describe_body.render())
            assert calls == ["aa_enc.danger_table"]

    asyncio.run(run())


def test_browser_drop_refreshes_table_list_after_success(mock_env_with_config, monkeypatch) -> None:
    """Successful DROP should reload the Browse table list without manual refresh."""
    show_calls = 0
    tables_state = ["table_a", "table_b"]

    async def fake_show_tables(schema: str, pattern: str = "*") -> list[str]:
        nonlocal show_calls
        show_calls += 1
        return list(tables_state)

    async def fake_drop_table(full_table: str) -> str:
        short_name = full_table.rsplit(".", 1)[-1]
        if short_name in tables_state:
            tables_state.remove(short_name)
        return f"Dropped {full_table}"

    monkeypatch.setattr("dispatch.impala.show_tables", fake_show_tables)
    monkeypatch.setattr("dispatch.impala.iter_table_sizes", _fake_iter_table_sizes({}))
    monkeypatch.setattr("dispatch.impala.drop_table", fake_drop_table)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = BrowserScreen(auto_load=False)
            app.push_screen(screen)
            await pilot.pause()
            await screen.action_show_tables()
            await pilot.pause()

            table = screen.query_one("#browser-table", DataTable)
            screen._checked = {"table_a"}
            screen._render_table_list(selected_before="table_a")

            worker = screen.action_drop()
            await pilot.pause()
            await _confirm_bulk_drop(pilot, app)
            await worker.wait()
            await pilot.pause()

            rows = [table.get_row_at(i)[1] for i in range(table.row_count)]
            assert rows == ["table_b"]
            assert "1 tables" in str(screen.query_one("#browser-count").render())
            assert show_calls == 2

    asyncio.run(run())


def test_browser_bulk_drop_only_checked_tables(mock_env_with_config, monkeypatch) -> None:
    """DROP applies only to checked tables, not merely the highlighted row."""
    calls: list[str] = []

    async def fake_drop_table(full_table: str) -> str:
        calls.append(full_table)
        return f"Dropped {full_table}"

    async def fake_show_tables(schema: str, pattern: str = "*") -> list[str]:
        return []

    monkeypatch.setattr("dispatch.impala.drop_table", fake_drop_table)
    monkeypatch.setattr("dispatch.impala.show_tables", fake_show_tables)
    monkeypatch.setattr("dispatch.impala.iter_table_sizes", _fake_iter_table_sizes({}))

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = BrowserScreen(auto_load=False)
            app.push_screen(screen)
            await pilot.pause()
            screen._tables = ["keep_me", "drop_me"]
            screen._checked = {"drop_me"}
            table = screen.query_one("#browser-table", DataTable)
            table.clear()
            table.add_row(UNCHECKED_MARKER, "keep_me", "—", "table", key="keep_me")
            table.add_row(CHECKED_MARKER, "drop_me", "—", "table", key="drop_me")
            table.cursor_coordinate = (0, 0)
            screen._update_action_state()

            worker = screen.action_drop()
            await pilot.pause()
            await _confirm_bulk_drop(pilot, app)
            await worker.wait()

            assert calls == ["aa_enc.drop_me"]

    asyncio.run(run())


def test_browser_drop_last_table_shows_placeholder(mock_env_with_config, monkeypatch) -> None:
    """Dropping the only visible table should show the empty-list placeholder."""
    tables_state = ["only_table"]

    async def fake_show_tables(schema: str, pattern: str = "*") -> list[str]:
        return list(tables_state)

    async def fake_drop_table(full_table: str) -> str:
        tables_state.clear()
        return f"Dropped {full_table}"

    monkeypatch.setattr("dispatch.impala.show_tables", fake_show_tables)
    monkeypatch.setattr("dispatch.impala.iter_table_sizes", _fake_iter_table_sizes({}))
    monkeypatch.setattr("dispatch.impala.drop_table", fake_drop_table)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = BrowserScreen(auto_load=False)
            app.push_screen(screen)
            await pilot.pause()
            await screen.action_show_tables()
            await pilot.pause()

            table = screen.query_one("#browser-table", DataTable)
            screen._checked = {"only_table"}
            screen._render_table_list(selected_before="only_table")

            worker = screen.action_drop()
            await pilot.pause()
            await _confirm_bulk_drop(pilot, app)
            await worker.wait()
            await pilot.pause()

            rows = [table.get_row_at(i)[1] for i in range(table.row_count)]
            assert rows == ["(no tables)"]

    asyncio.run(run())


def test_browser_select_all_marks_every_loaded_table(mock_env_with_config) -> None:
    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = BrowserScreen(auto_load=False)
            app.push_screen(screen)
            await pilot.pause()
            screen._tables = ["alpha", "beta"]
            screen._rebuild_table_rows()
            screen._render_table_list()
            screen._update_action_state()

            screen.action_select_all()
            await pilot.pause()
            assert screen._checked == {"alpha", "beta"}
            assert screen.query_one("#drop").disabled is False

            screen.action_select_all()
            await pilot.pause()
            assert screen._checked == set()
            assert screen.query_one("#drop").disabled is True

    asyncio.run(run())


def test_browser_multi_table_drop_refreshes_list_once(mock_env_with_config, monkeypatch) -> None:
    """Multi-table DROP should refresh the list after all tables are dropped."""
    show_calls = 0
    tables_state = ["table_a", "table_b", "table_c"]
    drop_calls: list[str] = []

    async def fake_show_tables(schema: str, pattern: str = "*") -> list[str]:
        nonlocal show_calls
        show_calls += 1
        return list(tables_state)

    async def fake_drop_table(full_table: str) -> str:
        drop_calls.append(full_table)
        short_name = full_table.rsplit(".", 1)[-1]
        if short_name in tables_state:
            tables_state.remove(short_name)
        return f"Dropped {full_table}"

    monkeypatch.setattr("dispatch.impala.show_tables", fake_show_tables)
    monkeypatch.setattr("dispatch.impala.iter_table_sizes", _fake_iter_table_sizes({}))
    monkeypatch.setattr("dispatch.impala.drop_table", fake_drop_table)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = BrowserScreen(auto_load=False)
            app.push_screen(screen)
            await pilot.pause()
            await screen.action_show_tables()
            await pilot.pause()

            screen._checked = {"table_a", "table_b"}
            screen._render_table_list()

            worker = screen.action_drop()
            await pilot.pause()
            await _confirm_bulk_drop(pilot, app)
            await worker.wait()
            await pilot.pause()

            table = screen.query_one("#browser-table", DataTable)
            rows = [table.get_row_at(i)[1] for i in range(table.row_count)]
            assert rows == ["table_c"]
            assert sorted(drop_calls) == ["aa_enc.table_a", "aa_enc.table_b"]
            assert show_calls == 2

    asyncio.run(run())


def test_browser_bulk_drop_multiple_tables(mock_env_with_config, monkeypatch) -> None:
    calls: list[str] = []

    async def fake_drop_table(full_table: str) -> str:
        calls.append(full_table)
        return f"Dropped {full_table}"

    async def fake_show_tables(schema: str, pattern: str = "*") -> list[str]:
        return []

    monkeypatch.setattr("dispatch.impala.drop_table", fake_drop_table)
    monkeypatch.setattr("dispatch.impala.show_tables", fake_show_tables)
    monkeypatch.setattr("dispatch.impala.iter_table_sizes", _fake_iter_table_sizes({}))

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = BrowserScreen(auto_load=False)
            app.push_screen(screen)
            await pilot.pause()
            screen._tables = ["one", "two"]
            screen._checked = {"one", "two"}
            table = screen.query_one("#browser-table", DataTable)
            table.clear()
            table.add_row(CHECKED_MARKER, "one", "—", "table", key="one")
            table.add_row(CHECKED_MARKER, "two", "—", "table", key="two")
            screen._update_action_state()

            worker = screen.action_drop()
            await pilot.pause()
            await _confirm_bulk_drop(pilot, app)
            await worker.wait()

            assert calls == ["aa_enc.one", "aa_enc.two"]

    asyncio.run(run())

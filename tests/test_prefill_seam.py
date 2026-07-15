"""Tests for the New Job prefill path and the DISPATCH_TEST_PREFILL seam."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from textual.widgets import Input, RadioButton, RadioSet

from dispatch.app import DispatchApp
from dispatch.screens.new_job import NewJobScreen


def _write_sql(data_root: Path) -> Path:
    sql_path = data_root / "smoke.sql"
    sql_path.write_text("SELECT 1 AS smoke_check;\n", encoding="utf-8")
    return sql_path


def test_prefill_selects_table_destination(mock_env_with_config) -> None:
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    sql_path = _write_sql(data_root)
    prefill = {
        "source_type": "SqlFile",
        "dest_type": "Table",
        "sql_file": str(sql_path),
        "schema": "aa_enc",
        "table_name": "smoke_tbl",
    }

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            app.push_screen(NewJobScreen(app.launch_cwd, prefill=prefill))
            await pilot.pause(0.5)
            screen = app.screen
            assert isinstance(screen, NewJobScreen)
            assert screen._selected_destination() == "Table"
            assert screen._selected_source() == "SqlFile"
            assert screen.query_one("#row-schema").display is True
            assert screen.query_one("#row-table-name").display is True

    asyncio.run(run())


def test_prefill_hides_picker_and_keeps_table_rows(mock_env_with_config) -> None:
    """A prefilled (re-run / test) form suppresses the redundant cwd SQL picker.

    Regression: when the picker-populate worker showed the file list, the taller
    table-producing forms (Table / Table+Csv / SqlTemplate) pushed the Schema and
    Table Name rows below a single SSH pane's fold, so the harness could not see
    them. The SQL path is already known on a prefilled form, so the picker must
    stay hidden even after the background populate worker runs.
    """
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    sql_path = _write_sql(data_root)
    prefill = {
        "source_type": "SqlFile",
        "dest_type": "Table+Csv",
        "sql_file": str(sql_path),
        "schema": "aa_enc",
        "table_name": "smoke_tbl",
    }

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            app.push_screen(NewJobScreen(data_root, prefill=prefill))
            # Let the background picker-populate worker run to completion.
            await pilot.pause(1.0)
            screen = app.screen
            assert isinstance(screen, NewJobScreen)
            assert screen._cwd_sql_files, "worker should have scanned the cwd SQL file"
            assert screen.query_one("#sql-file-picker").display is False
            assert screen.query_one("#picker-caption").display is False
            assert screen.query_one("#row-table-name").display is True
            assert screen.query_one("#row-schema").display is True
            assert screen._selected_destination() == "Table+Csv"

    asyncio.run(run())


def test_non_prefilled_form_still_shows_picker(mock_env_with_config) -> None:
    """The file-first flow is unchanged for a fresh (non-prefilled) New Job."""
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    _write_sql(data_root)

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            app.push_screen(NewJobScreen(data_root))
            await pilot.pause(1.0)
            screen = app.screen
            assert isinstance(screen, NewJobScreen)
            assert screen._cwd_sql_files
            assert screen.query_one("#sql-file-picker").display is True

    asyncio.run(run())


def test_test_prefill_seam_opens_new_job(mock_env_with_config, monkeypatch) -> None:
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    sql_path = _write_sql(data_root)
    prefill_file = data_root / "prefill.json"
    prefill_file.write_text(
        json.dumps(
            {
                "source_type": "SqlFile",
                "dest_type": "Table",
                "sql_file": str(sql_path),
                "schema": "aa_enc",
                "table_name": "smoke_tbl",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DISPATCH_TEST_PREFILL", str(prefill_file))

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            # Prefill radios are re-asserted on timers (0.05 / 0.25s) because
            # Textual RadioSet mount can lag on Windows under xdist load.
            destination = None
            screen = None
            for _ in range(40):
                await pilot.pause(0.05)
                screen = app.screen
                if not isinstance(screen, NewJobScreen):
                    continue
                try:
                    destination = screen._selected_destination()
                except Exception:
                    # Screen pushed but compose/mount not finished yet.
                    continue
                if destination == "Table":
                    break
            assert isinstance(screen, NewJobScreen)
            assert destination == "Table"

    asyncio.run(run())


def test_selected_destination_prefers_button_value_over_pressed(
    mock_env_with_config,
) -> None:
    """Regression: RadioSet.pressed_button can desync from button.value.

    When compose-time Table is on but ``_pressed_button`` is still unset (or
    stuck on the Csv default), selection helpers must trust ``button.value``.
    """
    data_root = Path(os.environ["DISPATCH_DATA_ROOT"])
    sql_path = _write_sql(data_root)
    prefill = {
        "source_type": "SqlFile",
        "dest_type": "Table",
        "sql_file": str(sql_path),
        "schema": "aa_enc",
        "table_name": "smoke_tbl",
    }

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            app.push_screen(NewJobScreen(app.launch_cwd, prefill=prefill))
            await pilot.pause(0.5)
            screen = app.screen
            assert isinstance(screen, NewJobScreen)
            dest = screen.query_one("#destination", RadioSet)
            # Simulate the Windows mount desync: values say Table, pressed says none.
            dest._pressed_button = None
            assert screen._selected_destination() == "Table"

    asyncio.run(run())


def test_existing_table_shows_schema_selector(mock_env_with_config) -> None:
    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            app.push_screen(NewJobScreen(Path(os.environ["DISPATCH_DATA_ROOT"])))
            await pilot.pause(0.5)
            screen = app.screen
            assert isinstance(screen, NewJobScreen)
            assert screen.query_one("#row-existing-schema").display is False
            assert screen.query_one("#row-existing-table").display is False

            screen.query_one("#src-existingtable", RadioButton).value = True
            await pilot.pause(0.2)

            assert screen.query_one("#row-existing-schema").display is True
            assert screen.query_one("#row-existing-table").display is True
            assert screen.query_one("#row-existing-schema-custom").display is False
            assert screen._selected_existing_schema_choice() == "aa_enc"

    asyncio.run(run())


def test_existing_table_other_schema_shows_custom_input(mock_env_with_config) -> None:
    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            app.push_screen(NewJobScreen(Path(os.environ["DISPATCH_DATA_ROOT"])))
            await pilot.pause(0.5)
            screen = app.screen
            assert isinstance(screen, NewJobScreen)
            screen.query_one("#src-existingtable", RadioButton).value = True
            await pilot.pause(0.2)

            screen.query_one("#esc-other", RadioButton).value = True
            await pilot.pause(0.2)

            assert screen.query_one("#row-existing-schema-custom").display is True
            custom = screen.query_one("#existing-schema-custom", Input)
            assert custom.disabled is False

    asyncio.run(run())


def test_existing_table_prefill_splits_known_schema(mock_env_with_config) -> None:
    prefill = {
        "source_type": "ExistingTable",
        "dest_type": "Csv",
        "existing_table": "coe_enc.dispatch_seed",
    }

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            app.push_screen(NewJobScreen(Path(os.environ["DISPATCH_DATA_ROOT"]), prefill=prefill))
            await pilot.pause(0.8)
            screen = app.screen
            assert isinstance(screen, NewJobScreen)
            assert screen._selected_existing_schema_choice() == "coe_enc"
            assert screen._input_value("existing-table") == "dispatch_seed"
            assert screen._existing_full_table() == "coe_enc.dispatch_seed"

    asyncio.run(run())


def test_existing_table_prefill_splits_custom_schema(mock_env_with_config) -> None:
    prefill = {
        "source_type": "ExistingTable",
        "dest_type": "Csv",
        "existing_table": "analytics.dispatch_seed",
    }

    async def run() -> None:
        app = DispatchApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            app.push_screen(NewJobScreen(Path(os.environ["DISPATCH_DATA_ROOT"]), prefill=prefill))
            await pilot.pause(0.8)
            screen = app.screen
            assert isinstance(screen, NewJobScreen)
            assert screen._selected_existing_schema_choice() == "other"
            assert screen._input_value("existing-schema-custom") == "analytics"
            assert screen._input_value("existing-table") == "dispatch_seed"
            assert screen._existing_full_table() == "analytics.dispatch_seed"

    asyncio.run(run())
